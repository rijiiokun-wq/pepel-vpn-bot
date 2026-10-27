"""Async Lava Business API client for one-time VPN payments.

The current Lava contract signs the exact compact JSON bytes sent to the API
with HMAC-SHA256.  Configuration is intentionally environment-only; payment
credentials must never be read from the bot settings database or logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import aiohttp

logger = logging.getLogger(__name__)

LAVA_API_BASE_URL = "https://api.lava.ru"
DEFAULT_INVOICE_TTL_MINUTES = 300
MIN_INVOICE_TTL_MINUTES = 1
MAX_INVOICE_TTL_MINUTES = 7200
MIN_INVOICE_AMOUNT = Decimal("1.00")
MAX_INVOICE_AMOUNT = Decimal("2000000.00")
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15.0
DEFAULT_SAFE_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_BACKOFF_SECONDS = 0.25
PAYMENT_METHOD_CARD = "card"
PAYMENT_METHOD_SBP = "sbp"
DEFAULT_PAYMENT_SERVICES: tuple[str, ...] = (PAYMENT_METHOD_CARD, PAYMENT_METHOD_SBP)

_ORDER_ID_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._:-]{0,127}\Z")
_HEX_SIGNATURE_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_ALLOWED_PAYMENT_SERVICES = frozenset(
    {"card", "sbp", "lava_pay_in", "sber_pay", "mir_pay", "mir_card"}
)
_SUCCEEDED_STATUSES = frozenset({"success"})
_CANCELED_STATUSES = frozenset({"fail", "expired", "refund"})
_TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


class LavaError(RuntimeError):
    """Base class for Lava integration errors."""


class LavaConfigError(LavaError):
    """Raised when environment-only Lava configuration is invalid."""


class LavaValidationError(LavaError, ValueError):
    """Raised before sending an invalid payment request."""


class LavaNetworkError(LavaError):
    """Raised for timeouts and aiohttp transport failures."""

    def __init__(self, operation: str, cause: BaseException):
        self.operation = operation
        self.cause = cause
        super().__init__(f"Lava network failure during {operation}: {type(cause).__name__}")


class LavaAPIError(LavaError):
    """Raised when Lava returns an HTTP or application-level error."""

    def __init__(self, status_code: int, message: str, payload: Mapping[str, Any] | None = None):
        self.status_code = int(status_code)
        self.provider_message = str(message or "Lava API request failed").strip()
        self.payload = dict(payload or {})
        super().__init__(f"Lava API error {self.status_code}: {self.provider_message}")


class LavaProtocolError(LavaError):
    """Raised when a successful response violates the documented contract."""


class LavaCreateIndeterminateError(LavaError):
    """Raised when create may have reached Lava but cannot be reconciled safely."""

    def __init__(
        self,
        order_id: str,
        reason: str,
        *,
        reconciliation: Mapping[str, Any] | None = None,
    ):
        self.order_id = order_id
        self.reason = reason
        self.reconciliation = dict(reconciliation or {})
        super().__init__(f"Lava invoice result is indeterminate for order_id={order_id}: {reason}")


def get_exception_code(exc: BaseException) -> str:
    """Return a compact, non-secret error code suitable for alerts."""
    type_chain: list[str] = []
    cursor: BaseException | None = exc
    seen: set[int] = set()
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        name = type(cursor).__name__
        if name not in type_chain:
            type_chain.append(name)
        cursor = cursor.__cause__
    return "/".join(type_chain) or "UnknownError"


def get_exception_message(exc: BaseException) -> str:
    """Return the first non-empty exception message without exposing config."""
    cursor: BaseException | None = exc
    seen: set[int] = set()
    while cursor is not None and id(cursor) not in seen:
        seen.add(id(cursor))
        message = str(cursor).strip()
        if message:
            return message
        cursor = cursor.__cause__
    return ""


def _required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise LavaConfigError(f"{name} is required")
    return value


def _normalize_uuid(value: Any, field_name: str) -> str:
    raw_value = str(value or "").strip()
    try:
        parsed = uuid.UUID(raw_value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise LavaValidationError(f"{field_name} must be a valid UUID") from exc
    if parsed.int == 0:
        raise LavaValidationError(f"{field_name} must not be the nil UUID")
    return str(parsed)


def _normalize_order_id(value: Any) -> str:
    raw_order_id = str(value or "")
    order_id = raw_order_id.strip()
    if raw_order_id != order_id:
        raise LavaValidationError("order_id must not contain leading or trailing whitespace")
    if not _ORDER_ID_RE.fullmatch(order_id):
        raise LavaValidationError(
            "order_id must be 1-128 ASCII characters using letters, digits, '.', '_', ':' or '-'"
        )
    return order_id


def _normalize_amount(value: Any, *, field_name: str = "amount") -> Decimal:
    if isinstance(value, bool):
        raise LavaValidationError(f"{field_name} must be a monetary number")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LavaValidationError(f"{field_name} must be a monetary number") from exc
    if not amount.is_finite():
        raise LavaValidationError(f"{field_name} must be finite")
    try:
        quantized = amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise LavaValidationError(f"{field_name} has invalid precision") from exc
    if amount != quantized:
        raise LavaValidationError(f"{field_name} must have at most two decimal places")
    if quantized < MIN_INVOICE_AMOUNT or quantized > MAX_INVOICE_AMOUNT:
        raise LavaValidationError(
            f"{field_name} must be between {MIN_INVOICE_AMOUNT} and {MAX_INVOICE_AMOUNT} RUB"
        )
    return quantized


def _amount_to_json_value(amount: Decimal) -> int | float:
    if amount == amount.to_integral_value():
        return int(amount)
    return float(format(amount, "f"))


def _validate_url(value: Any, field_name: str, *, base_url: bool = False) -> str:
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""
    parsed = urlsplit(raw_value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LavaConfigError(f"{field_name} must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise LavaConfigError(f"{field_name} must not contain URL credentials")
    if base_url and (parsed.query or parsed.fragment):
        raise LavaConfigError(f"{field_name} must not contain a query or fragment")
    return raw_value.rstrip("/") if base_url else raw_value


def _read_invoice_ttl() -> int:
    raw_value = str(
        os.environ.get("LAVA_INVOICE_TTL_MINUTES") or DEFAULT_INVOICE_TTL_MINUTES
    ).strip()
    try:
        ttl_minutes = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise LavaConfigError("LAVA_INVOICE_TTL_MINUTES must be an integer") from exc
    if not MIN_INVOICE_TTL_MINUTES <= ttl_minutes <= MAX_INVOICE_TTL_MINUTES:
        raise LavaConfigError(
            f"LAVA_INVOICE_TTL_MINUTES must be between {MIN_INVOICE_TTL_MINUTES} "
            f"and {MAX_INVOICE_TTL_MINUTES}"
        )
    return ttl_minutes


@dataclass(frozen=True, slots=True)
class LavaConfig:
    """Validated environment-only Lava configuration."""

    shop_id: str
    secret_key: str = field(repr=False)
    webhook_secret: str = field(repr=False)
    api_base_url: str = LAVA_API_BASE_URL
    webhook_url: str = ""
    return_url: str = ""
    invoice_ttl_minutes: int = DEFAULT_INVOICE_TTL_MINUTES
    request_timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> LavaConfig:
        shop_id = _normalize_uuid(_required_env("LAVA_SHOP_ID"), "LAVA_SHOP_ID")
        secret_key = _required_env("LAVA_SECRET_KEY")
        webhook_secret = str(
            os.environ.get("LAVA_WEBHOOK_SECRET")
            or os.environ.get("LAVA_ADDITIONAL_KEY")
            or ""
        ).strip()
        if not webhook_secret:
            raise LavaConfigError("LAVA_WEBHOOK_SECRET is required")
        api_base_url = _validate_url(
            os.environ.get("LAVA_API_BASE_URL") or LAVA_API_BASE_URL,
            "LAVA_API_BASE_URL",
            base_url=True,
        )

        public_base_url = _validate_url(
            os.environ.get("LAVA_PUBLIC_BASE_URL"),
            "LAVA_PUBLIC_BASE_URL",
            base_url=True,
        )
        webhook_url = _validate_url(
            os.environ.get("LAVA_WEBHOOK_URL"),
            "LAVA_WEBHOOK_URL",
        )
        if not webhook_url and public_base_url:
            webhook_url = f"{public_base_url}/lava-webhook"

        return_url = _validate_url(
            os.environ.get("LAVA_RETURN_URL"),
            "LAVA_RETURN_URL",
        )
        return cls(
            shop_id=shop_id,
            secret_key=secret_key,
            webhook_secret=webhook_secret,
            api_base_url=api_base_url,
            webhook_url=webhook_url,
            return_url=return_url,
            invoice_ttl_minutes=_read_invoice_ttl(),
        )


def compact_json_bytes(payload: Mapping[str, Any], *, sort_keys: bool = False) -> bytes:
    """Serialize JSON once; these exact bytes must be signed and sent."""
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=sort_keys,
        )
    except (TypeError, ValueError) as exc:
        raise LavaValidationError("payload is not JSON serializable") from exc
    return encoded.encode("utf-8")


def sign_raw_body(raw_body: bytes, secret: str) -> str:
    """Return a lowercase HMAC-SHA256 digest for exact request bytes."""
    if not isinstance(raw_body, bytes):
        raise TypeError("raw_body must be bytes")
    normalized_secret = str(secret or "").strip()
    if not normalized_secret:
        raise LavaConfigError("Lava signing secret is required")
    return hmac.new(normalized_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _normalize_signature(signature: Any) -> str:
    normalized = str(signature or "").strip()
    if normalized.lower().startswith("sha256="):
        normalized = normalized.split("=", 1)[1].strip()
    return normalized.lower() if _HEX_SIGNATURE_RE.fullmatch(normalized) else ""


def _resolve_webhook_secret(secret: str | None) -> str:
    return str(
        secret
        if secret is not None
        else os.environ.get("LAVA_WEBHOOK_SECRET")
        or os.environ.get("LAVA_ADDITIONAL_KEY")
        or ""
    ).strip()


def verify_webhook_signature(
    raw_body: bytes,
    signature: str | None,
    secret: str | None = None,
) -> bool:
    """Verify the current ``Signature`` contract against the untouched body."""
    if not isinstance(raw_body, bytes) or not raw_body:
        return False
    normalized_signature = _normalize_signature(signature)
    normalized_secret = _resolve_webhook_secret(secret)
    if not normalized_signature or not normalized_secret:
        return False
    expected = sign_raw_body(raw_body, normalized_secret)
    return hmac.compare_digest(expected, normalized_signature)


def _legacy_sorted_webhook_body(raw_body: bytes) -> bytes:
    """Reproduce the official legacy SDK's top-level ksort compatibility mode."""
    try:
        decoded = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LavaValidationError("legacy webhook body must be a JSON object") from exc
    if not isinstance(decoded, dict):
        raise LavaValidationError("legacy webhook body must be a JSON object")
    top_level_sorted = {key: decoded[key] for key in sorted(decoded)}
    return compact_json_bytes(top_level_sorted)


def verify_legacy_webhook_signature(
    raw_body: bytes,
    authorization: str | None,
    secret: str | None = None,
) -> bool:
    """Verify legacy ``Authorization``/sorted-JSON mode explicitly."""
    normalized_signature = _normalize_signature(authorization)
    normalized_secret = _resolve_webhook_secret(secret)
    if not normalized_signature or not normalized_secret or not raw_body:
        return False
    try:
        canonical_body = _legacy_sorted_webhook_body(raw_body)
    except LavaValidationError:
        return False
    expected = sign_raw_body(canonical_body, normalized_secret)
    return hmac.compare_digest(expected, normalized_signature)


def verify_webhook_request(
    raw_body: bytes,
    headers: Mapping[str, Any],
    *,
    allow_legacy: bool = False,
    secret: str | None = None,
) -> bool:
    """Verify request headers, keeping legacy compatibility opt-in."""
    normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
    if verify_webhook_signature(raw_body, normalized_headers.get("signature"), secret):
        return True
    return allow_legacy and verify_legacy_webhook_signature(
        raw_body,
        normalized_headers.get("authorization"),
        secret,
    )


def map_lava_status(status: str | None) -> str:
    """Map Lava states to the bot's ``succeeded/canceled/pending`` states."""
    normalized = str(status or "").strip().lower()
    if normalized in _SUCCEEDED_STATUSES:
        return "succeeded"
    if normalized in _CANCELED_STATUSES:
        return "canceled"
    return "pending"


def _normalize_comment(value: Any) -> str:
    comment = str(value or "").strip()
    if not comment:
        return ""
    if len(comment) > 255 or any(ord(char) < 32 for char in comment):
        raise LavaValidationError("comment must be at most 255 characters without control characters")
    return comment


def _normalize_custom_fields(value: Mapping[str, Any] | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        serialized = value.strip()
        if serialized:
            try:
                json.loads(serialized)
            except json.JSONDecodeError as exc:
                raise LavaValidationError("custom_fields string must contain valid JSON") from exc
    elif isinstance(value, Mapping):
        serialized = compact_json_bytes(value, sort_keys=True).decode("utf-8")
    else:
        raise LavaValidationError("custom_fields must be a JSON string or mapping")
    if len(serialized.encode("utf-8")) > 4096:
        raise LavaValidationError("custom_fields must not exceed 4096 UTF-8 bytes")
    return serialized


def _normalize_services(services: Sequence[str] | None) -> list[str]:
    if services is None:
        return []
    normalized: list[str] = []
    for service in services:
        value = str(service or "").strip().lower()
        if value not in _ALLOWED_PAYMENT_SERVICES:
            raise LavaValidationError(f"unsupported Lava payment service: {value or '<empty>'}")
        if value not in normalized:
            normalized.append(value)
    if not normalized:
        raise LavaValidationError("at least one Lava payment service is required")
    return normalized


def _extract_error_message(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, str) and error.strip():
        return error.strip()
    if isinstance(error, Mapping):
        for key in ("message", "description", "detail"):
            value = error.get(key)
            if value:
                return str(value).strip()
    for key in ("message", "description", "detail"):
        value = payload.get(key)
        if value:
            return str(value).strip()
    return "Lava API request failed"


def _require_data_dict(envelope: Mapping[str, Any], operation: str) -> dict[str, Any]:
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise LavaProtocolError(f"Lava {operation} response does not contain an object in data")
    return data


class LavaClient:
    """Small stateless aiohttp client for the Lava Business invoice endpoints."""

    def __init__(
        self,
        config: LavaConfig | None = None,
        *,
        safe_retry_attempts: int = DEFAULT_SAFE_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    ):
        self.config = config or LavaConfig.from_env()
        if not 1 <= int(safe_retry_attempts) <= 6:
            raise LavaConfigError("safe_retry_attempts must be between 1 and 6")
        if not 0 <= float(retry_backoff_seconds) <= 10:
            raise LavaConfigError("retry_backoff_seconds must be between 0 and 10")
        self._safe_retry_attempts = int(safe_retry_attempts)
        self._retry_backoff_seconds = float(retry_backoff_seconds)

    def _endpoint(self, path: str) -> str:
        return f"{self.config.api_base_url.rstrip('/')}/{path.lstrip('/')}"

    async def _post_once(
        self,
        *,
        path: str,
        raw_body: bytes,
        operation: str,
    ) -> dict[str, Any]:
        signature = sign_raw_body(raw_body, self.config.secret_key)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Signature": signature,
        }
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session, session.post(
                self._endpoint(path),
                data=raw_body,
                headers=headers,
            ) as response:
                status_code = int(response.status)
                response_text = await response.text()
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise LavaNetworkError(operation, exc) from exc

        try:
            decoded = json.loads(response_text)
        except json.JSONDecodeError as exc:
            if not 200 <= status_code < 300:
                raise LavaAPIError(
                    status_code,
                    f"Lava {operation} returned a non-JSON error response",
                ) from exc
            raise LavaProtocolError(
                f"Lava {operation} returned non-JSON response with HTTP {status_code}"
            ) from exc
        if not isinstance(decoded, dict):
            if not 200 <= status_code < 300:
                raise LavaAPIError(
                    status_code,
                    f"Lava {operation} returned a non-object error response",
                )
            raise LavaProtocolError(f"Lava {operation} returned a non-object JSON response")

        if not 200 <= status_code < 300 or decoded.get("status_check") is not True:
            raise LavaAPIError(status_code, _extract_error_message(decoded), decoded)
        return decoded

    async def _post_safe(
        self,
        *,
        path: str,
        payload: Mapping[str, Any],
        operation: str,
    ) -> dict[str, Any]:
        raw_body = compact_json_bytes(payload)
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._post_once(path=path, raw_body=raw_body, operation=operation)
            except LavaNetworkError:
                if attempt >= self._safe_retry_attempts:
                    raise
            except LavaAPIError as exc:
                if exc.status_code not in _TRANSIENT_HTTP_STATUSES or attempt >= self._safe_retry_attempts:
                    raise

            delay = min(self._retry_backoff_seconds * (2 ** (attempt - 1)), 5.0)
            logger.warning(
                "[Lava] retrying safe operation=%s attempt=%s/%s",
                operation,
                attempt + 1,
                self._safe_retry_attempts,
            )
            if delay:
                await asyncio.sleep(delay)

    async def create_invoice(
        self,
        *,
        amount: Any,
        order_id: str,
        comment: str = "Пепел ВПН",
        custom_fields: Mapping[str, Any] | str | None = None,
        include_services: Sequence[str] | None = DEFAULT_PAYMENT_SERVICES,
        webhook_url: str | None = None,
        return_url: str | None = None,
        expire_minutes: int | None = None,
    ) -> dict[str, str]:
        """Create one invoice; ambiguous failures are reconciled and fail closed."""
        normalized_amount = _normalize_amount(amount)
        normalized_order_id = _normalize_order_id(order_id)
        normalized_comment = _normalize_comment(comment)
        normalized_custom_fields = _normalize_custom_fields(custom_fields)
        normalized_services = _normalize_services(include_services)

        final_webhook_url = _validate_url(
            webhook_url if webhook_url is not None else self.config.webhook_url,
            "webhook_url",
        )
        final_return_url = _validate_url(
            return_url if return_url is not None else self.config.return_url,
            "return_url",
        )
        ttl_minutes = self.config.invoice_ttl_minutes if expire_minutes is None else expire_minutes
        try:
            ttl_minutes = int(ttl_minutes)
        except (TypeError, ValueError) as exc:
            raise LavaValidationError("expire_minutes must be an integer") from exc
        if not MIN_INVOICE_TTL_MINUTES <= ttl_minutes <= MAX_INVOICE_TTL_MINUTES:
            raise LavaValidationError(
                f"expire_minutes must be between {MIN_INVOICE_TTL_MINUTES} "
                f"and {MAX_INVOICE_TTL_MINUTES}"
            )

        payload: dict[str, Any] = {
            "sum": _amount_to_json_value(normalized_amount),
            "orderId": normalized_order_id,
            "shopId": self.config.shop_id,
        }
        if final_webhook_url:
            payload["hookUrl"] = final_webhook_url
        if normalized_custom_fields:
            payload["customFields"] = normalized_custom_fields
        if normalized_comment:
            payload["comment"] = normalized_comment
        if final_return_url:
            payload["failUrl"] = final_return_url
            payload["successUrl"] = final_return_url
        payload["expire"] = ttl_minutes
        if normalized_services:
            payload["includeService"] = normalized_services

        raw_body = compact_json_bytes(payload)
        try:
            envelope = await self._post_once(
                path="/business/invoice/create",
                raw_body=raw_body,
                operation="invoice_create",
            )
        except LavaNetworkError as exc:
            return await self._reconcile_create_failure(
                order_id=normalized_order_id,
                expected_amount=normalized_amount,
                original_error=exc,
            )
        except LavaAPIError as exc:
            if exc.status_code not in _TRANSIENT_HTTP_STATUSES:
                raise
            return await self._reconcile_create_failure(
                order_id=normalized_order_id,
                expected_amount=normalized_amount,
                original_error=exc,
            )

        data = _require_data_dict(envelope, "invoice_create")
        return self._payment_result_from_data(
            data,
            order_id=normalized_order_id,
            expected_amount=normalized_amount,
            require_url=True,
        )

    async def _reconcile_create_failure(
        self,
        *,
        order_id: str,
        expected_amount: Decimal,
        original_error: LavaError,
    ) -> dict[str, str]:
        logger.warning(
            "[Lava] create result uncertain; reconciling by order_id=%s error=%s",
            order_id,
            type(original_error).__name__,
        )
        try:
            reconciled = await self.get_invoice_status(
                order_id=order_id,
                expected_amount=expected_amount,
            )
        except LavaError as reconciliation_error:
            raise LavaCreateIndeterminateError(
                order_id,
                f"status reconciliation failed ({type(reconciliation_error).__name__})",
            ) from original_error

        payment_url = str(reconciled.get("payment_url") or "").strip()
        if payment_url:
            return {
                "payment_id": str(reconciled["payment_id"]),
                "payment_url": payment_url,
                "order_id": order_id,
            }
        raise LavaCreateIndeterminateError(
            order_id,
            "invoice exists but status response has no reusable payment URL",
            reconciliation=reconciled,
        ) from original_error

    def _payment_result_from_data(
        self,
        data: Mapping[str, Any],
        *,
        order_id: str,
        expected_amount: Decimal,
        require_url: bool,
    ) -> dict[str, str]:
        payment_id = _normalize_uuid(data.get("id"), "Lava invoice id")
        shop_id = _normalize_uuid(data.get("shop_id"), "Lava response shop_id")
        if shop_id != self.config.shop_id:
            raise LavaProtocolError("Lava response shop_id does not match configured project")

        try:
            response_amount = _normalize_amount(data.get("amount"), field_name="Lava response amount")
        except LavaValidationError as exc:
            raise LavaProtocolError(str(exc)) from exc
        if response_amount != expected_amount:
            raise LavaProtocolError("Lava response amount does not match requested amount")

        payment_url = _validate_url(data.get("url"), "Lava payment URL")
        if require_url and not payment_url:
            raise LavaProtocolError("Lava create response does not contain a payment URL")
        return {
            "payment_id": payment_id,
            "payment_url": payment_url,
            "order_id": order_id,
        }

    async def get_invoice_status(
        self,
        *,
        invoice_id: str | None = None,
        order_id: str | None = None,
        expected_amount: Any | None = None,
    ) -> dict[str, Any]:
        """Read invoice status using exactly one provider or merchant identifier."""
        if bool(invoice_id) == bool(order_id):
            raise LavaValidationError("provide exactly one of invoice_id or order_id")

        payload: dict[str, Any] = {"shopId": self.config.shop_id}
        normalized_invoice_id = ""
        normalized_order_id = ""
        if invoice_id:
            normalized_invoice_id = _normalize_uuid(invoice_id, "invoice_id")
            payload["invoiceId"] = normalized_invoice_id
        else:
            normalized_order_id = _normalize_order_id(order_id)
            payload["orderId"] = normalized_order_id

        envelope = await self._post_safe(
            path="/business/invoice/status",
            payload=payload,
            operation="invoice_status",
        )
        data = _require_data_dict(envelope, "invoice_status")

        payment_id = _normalize_uuid(data.get("id"), "Lava invoice id")
        if normalized_invoice_id and payment_id != normalized_invoice_id:
            raise LavaProtocolError("Lava status invoice id does not match the request")

        response_order_id = _normalize_order_id(data.get("order_id"))
        if normalized_order_id and response_order_id != normalized_order_id:
            raise LavaProtocolError("Lava status order_id does not match the request")

        shop_id = _normalize_uuid(data.get("shop_id"), "Lava response shop_id")
        if shop_id != self.config.shop_id:
            raise LavaProtocolError("Lava status shop_id does not match configured project")

        try:
            response_amount = _normalize_amount(data.get("amount"), field_name="Lava response amount")
        except LavaValidationError as exc:
            raise LavaProtocolError(str(exc)) from exc
        if expected_amount is not None:
            normalized_expected = _normalize_amount(expected_amount, field_name="expected_amount")
            if response_amount != normalized_expected:
                raise LavaProtocolError("Lava status amount does not match expected amount")

        provider_status = str(data.get("status") or "").strip().lower()
        if not provider_status:
            raise LavaProtocolError("Lava status response does not contain status")
        payment_url = _validate_url(data.get("url"), "Lava payment URL")
        provider_currency = str(data.get("currency") or "").strip().upper()
        return {
            "payment_id": payment_id,
            "invoice_id": payment_id,
            "payment_url": payment_url,
            "shop_id": shop_id,
            "order_id": response_order_id,
            "status": map_lava_status(provider_status),
            "provider_status": provider_status,
            "amount": format(response_amount, "f"),
            "currency": provider_currency,
        }

    async def get_available_tariffs(self) -> list[dict[str, Any]]:
        """Read the project's enabled payment methods, fees and limits."""
        envelope = await self._post_safe(
            path="/business/invoice/get-available-tariffs",
            payload={"shopId": self.config.shop_id},
            operation="invoice_tariffs",
        )
        data = envelope.get("data")
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise LavaProtocolError("Lava tariffs response does not contain a list of objects")
        return [dict(item) for item in data]


async def create_payment(
    *,
    amount_rub: Any,
    order_id: str,
    description: str = "Пепел ВПН",
    metadata: Mapping[str, Any] | None = None,
    payment_service: str | None = None,
    payment_variant: str | None = None,
) -> dict[str, str]:
    """Handler-friendly one-time checkout wrapper."""
    normalized_service = str(payment_service or "").strip().lower()
    normalized_variant = str(payment_variant or "").strip().lower()
    if normalized_service and normalized_variant and normalized_service != normalized_variant:
        raise LavaValidationError("payment_service and payment_variant must not conflict")
    variant = str(
        normalized_service
        or normalized_variant
        or (metadata or {}).get("lava_payment_variant")
        or ""
    ).strip().lower()
    services: tuple[str, ...]
    if variant in {PAYMENT_METHOD_CARD, PAYMENT_METHOD_SBP}:
        services = (variant,)
    elif variant:
        raise LavaValidationError("payment_service must be 'card' or 'sbp'")
    else:
        services = DEFAULT_PAYMENT_SERVICES

    return await LavaClient().create_invoice(
        amount=amount_rub,
        order_id=order_id,
        comment=description,
        custom_fields=metadata,
        include_services=services,
    )


async def check_status(
    payment_id: str | None = None,
    *,
    invoice_id: str | None = None,
    order_id: str | None = None,
    expected_amount: Any | None = None,
) -> dict[str, Any]:
    """Handler/scheduler-friendly invoice status wrapper."""
    if payment_id and invoice_id and payment_id != invoice_id:
        raise LavaValidationError("payment_id and invoice_id must not conflict")
    return await LavaClient().get_invoice_status(
        invoice_id=invoice_id or payment_id,
        order_id=order_id,
        expected_amount=expected_amount,
    )


async def get_available_tariffs() -> list[dict[str, Any]]:
    """Return currently available Lava tariffs without mutating provider state."""
    return await LavaClient().get_available_tariffs()
