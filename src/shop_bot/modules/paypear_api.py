"""
PayPear payment integration for reserve bot mode.

PayPear API notes used here:
- base URL: https://engine.paypear.io/api/v1 by default, configurable for sandbox/custom hosts
- HTTP Bearer Auth: secret/API key
- create payment: POST /payments
- status: GET /payments/{id}
- webhookUrl can be sent on payment creation; when not configured explicitly we derive it from the shop domain
- webhook signature is sent in the ``Signature`` header; body ``signature`` is accepted for compatibility.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from base64 import b64encode
from contextlib import suppress
from dataclasses import dataclass
from hmac import compare_digest
from typing import Any
from urllib.parse import urlparse

import aiohttp

from shop_bot.data_manager.database import get_setting

logger = logging.getLogger(__name__)

PAYPEAR_API_BASE_URL = "https://engine.paypear.io/api/v1"
_PAYPEAR_LEGACY_DEFAULT_BASE_URL = "https://api.paypear.ru/v1"
_PAYPEAR_LEGACY_API_BASE_URLS = frozenset({_PAYPEAR_LEGACY_DEFAULT_BASE_URL})
_PAYPEAR_FORCE_LEGACY_ENV_NAME = "PAYPEAR_FORCE_LEGACY_API_BASE_URL"
_PAYPEAR_FORCE_LEGACY_SETTING_KEY = "paypear_force_legacy_api_base_url"
DEFAULT_CHECKOUT_TTL_SECONDS = 1800
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15
DEFAULT_NETWORK_RETRY_ATTEMPTS = 3
DEFAULT_NETWORK_RETRY_BACKOFF_SECONDS = 0.75
DEFAULT_PAYMENT_LIFETIME_MINUTES = 60
PAYMENT_METHOD_SBP = "SBP"
PAYMENT_METHOD_CARD = "BASIC_CARD"
PAYMENT_METHOD_CRYPTO = "CRYPTO"
_LEGACY_CARD_METHOD_FALLBACK_ORDER: tuple[str, ...] = (
    "card",
    "basic_card_hpp",
    "p2p_card",
    "bank_card",
)

_PAID_STATUSES = frozenset({"confirmed", "success", "succeeded", "paid", "completed", "complete", "done"})
_CANCELED_STATUSES = frozenset(
    {"canceled", "cancelled", "cancel", "expired", "failed", "declined", "decline", "rejected", "refunded"}
)
_PAYPEAR_VALIDATION_MESSAGES = {
    "amount_must_be_positive": "Amount must be greater than 0.",
    "order_id_required": "order_id is required.",
}


class PayPearConfigError(RuntimeError):
    """Raised when required PayPear settings are missing."""

    def __init__(self) -> None:
        super().__init__("PayPear credentials are not configured.")


class PayPearAPIError(RuntimeError):
    """Raised when PayPear returns a non-success API response."""

    def __init__(self, status_code: int, message: str):
        self.status_code = int(status_code)
        self.message = str(message)
        super().__init__(f"PayPear API error ({self.status_code}): {self.message}")


class PayPearNetworkError(RuntimeError):
    """Raised for PayPear network/timeout failures."""


class PayPearValidationError(ValueError):
    """Raised when client-side PayPear input validation fails."""

    def __init__(self, code: str) -> None:
        self.code = str(code)
        super().__init__(_PAYPEAR_VALIDATION_MESSAGES.get(self.code, "PayPear input validation failed."))


@dataclass(slots=True)
class _CheckoutEntry:
    payment_id: str
    payment_url: str
    expires_at: float


_CHECKOUT_CACHE: dict[str, _CheckoutEntry] = {}
_CHECKOUT_LOCK = asyncio.Lock()
_CHECKOUT_CACHE_MAX_SIZE = 1000


def _read_positive_int_setting(setting_key: str, default: int, *, min_value: int, max_value: int) -> int:
    raw_value = str(get_setting(setting_key) or "").strip()
    if not raw_value:
        return default
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, parsed_value))


def _read_positive_float_setting(
    setting_key: str,
    default: float,
    *,
    min_value: float,
    max_value: float,
) -> float:
    raw_value = str(get_setting(setting_key) or "").strip()
    if not raw_value:
        return default
    try:
        parsed_value = float(raw_value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, parsed_value))


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen_ids: set[int] = set()
    cursor: BaseException | None = exc
    while cursor is not None and id(cursor) not in seen_ids:
        seen_ids.add(id(cursor))
        chain.append(cursor)
        cursor = cursor.__cause__
    return chain


def get_exception_code(exc: BaseException) -> str:
    type_chain: list[str] = []
    for item in _exception_chain(exc):
        if isinstance(item, PayPearAPIError):
            type_name = f"PayPearAPIError:{item.status_code}"
        elif isinstance(item, PayPearValidationError):
            type_name = f"PayPearValidationError:{item.code}"
        else:
            type_name = type(item).__name__
        if type_name not in type_chain:
            type_chain.append(type_name)
    return "/".join(type_chain) or "UnknownError"


def get_exception_message(exc: BaseException) -> str:
    for item in _exception_chain(exc):
        message = str(item).strip()
        if message:
            return message
    return ""


async def _run_with_network_retries(*, action_name: str, key: str, operation: Any) -> Any:
    max_attempts = _read_positive_int_setting(
        "paypear_network_retry_attempts",
        DEFAULT_NETWORK_RETRY_ATTEMPTS,
        min_value=1,
        max_value=6,
    )
    base_backoff_seconds = _read_positive_float_setting(
        "paypear_network_retry_backoff_seconds",
        DEFAULT_NETWORK_RETRY_BACKOFF_SECONDS,
        min_value=0.1,
        max_value=10.0,
    )

    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except PayPearNetworkError as exc:
            error_code = get_exception_code(exc)
            if attempt >= max_attempts:
                logger.exception(
                    "[PayPear] %s failed after %s/%s attempts key=%s error=%s",
                    action_name,
                    attempt,
                    max_attempts,
                    key,
                    error_code,
                )
                raise

            backoff_seconds = min(base_backoff_seconds * (2 ** (attempt - 1)), 10.0)
            logger.warning(
                "[PayPear] %s network retry %s/%s key=%s error=%s sleep=%.2fs",
                action_name,
                attempt,
                max_attempts,
                key,
                error_code,
                backoff_seconds,
            )
            await asyncio.sleep(backoff_seconds)


def map_paypear_status(status: str | None) -> str:
    value = str(status or "").strip().lower()
    if value in _PAID_STATUSES:
        return "succeeded"
    if value in _CANCELED_STATUSES:
        return "canceled"
    return "pending"


def _signature_candidates(payload: dict[str, Any]) -> list[bytes]:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    return [
        json.dumps(unsigned, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        json.dumps(unsigned, separators=(",", ":")).encode("utf-8"),
    ]


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    raw_payload = payload or b""
    raw_signature = str(signature or "").strip()
    raw_secret = str(secret or "").strip()
    if not raw_payload or not raw_signature or not raw_secret:
        return False

    if raw_signature.lower().startswith("sha256="):
        raw_signature = raw_signature.split("=", 1)[1].strip()

    try:
        parsed_payload = json.loads(raw_payload.decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed_payload = None

    bodies_to_sign = [raw_payload]
    if isinstance(parsed_payload, dict):
        bodies_to_sign = _signature_candidates(parsed_payload) + bodies_to_sign

    for body_to_sign in bodies_to_sign:
        expected = hmac.new(raw_secret.encode("utf-8"), body_to_sign, hashlib.sha256).hexdigest()
        if compare_digest(expected, raw_signature):
            return True
    return False


def _get_credentials() -> tuple[str, str]:
    shop_id = str(
        os.environ.get("PAYPEAR_SHOP_ID")
        or get_setting("paypear_shop_id")
        or ""
    ).strip()
    secret_key = str(
        os.environ.get("PAYPEAR_SECRET_KEY")
        or os.environ.get("PAYPEAR_API_KEY")
        or get_setting("paypear_secret_key")
        or get_setting("paypear_api_key")
        or ""
    ).strip()
    if not shop_id or not secret_key:
        raise PayPearConfigError
    return shop_id, secret_key


def _resolve_api_base_url() -> str:
    env_url = str(os.environ.get("PAYPEAR_API_BASE_URL") or "").strip().rstrip("/")
    if env_url:
        return env_url

    configured_url = str(get_setting("paypear_api_base_url") or "").strip().rstrip("/")
    if not configured_url:
        return PAYPEAR_API_BASE_URL

    force_legacy_raw = os.environ.get(_PAYPEAR_FORCE_LEGACY_ENV_NAME)
    if force_legacy_raw is None:
        force_legacy_raw = get_setting(_PAYPEAR_FORCE_LEGACY_SETTING_KEY)
    force_legacy_enabled = str(force_legacy_raw or "").strip().lower() in {"1", "true", "yes", "on"}

    if configured_url == _PAYPEAR_LEGACY_DEFAULT_BASE_URL and not force_legacy_enabled:
        logger.warning(
            "[PayPear] legacy base URL from settings detected; switching to modern default '%s'. "
            "Set %s=true to keep legacy endpoint.",
            PAYPEAR_API_BASE_URL,
            _PAYPEAR_FORCE_LEGACY_ENV_NAME,
        )
        return PAYPEAR_API_BASE_URL
    return configured_url


def _is_legacy_base_url(base_url: str) -> bool:
    normalized = str(base_url or "").strip().rstrip("/")
    return normalized in _PAYPEAR_LEGACY_API_BASE_URLS


def _resolve_return_url() -> str:
    configured_url = str(os.environ.get("PAYPEAR_RETURN_URL") or get_setting("paypear_return_url") or "").strip()
    if configured_url:
        return configured_url
    username = str(get_setting("reserve_bot_username") or "").strip()
    if username:
        return f"https://t.me/{username}"
    return "https://t.me"


def _normalize_absolute_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return value.rstrip("/")


def _resolve_webhook_url() -> str:
    configured_url = str(
        os.environ.get("PAYPEAR_WEBHOOK_URL")
        or get_setting("paypear_webhook_url")
        or ""
    ).strip()
    if configured_url:
        return _normalize_absolute_url(configured_url)

    public_base_url = str(
        os.environ.get("PAYPEAR_PUBLIC_BASE_URL")
        or os.environ.get("PUBLIC_BASE_URL")
        or get_setting("domain")
        or ""
    ).strip()
    normalized_base = _normalize_absolute_url(public_base_url)
    if not normalized_base:
        return ""
    return f"{normalized_base}/paypear-webhook"


def _resolve_checkout_ttl(metadata: dict[str, Any] | None) -> int:
    if isinstance(metadata, dict):
        raw_value = metadata.get("checkout_ttl_seconds")
        if raw_value not in (None, ""):
            try:
                ttl = int(raw_value)
            except (TypeError, ValueError):
                ttl = DEFAULT_CHECKOUT_TTL_SECONDS
            return ttl if ttl > 0 else DEFAULT_CHECKOUT_TTL_SECONDS

    lifetime_minutes = _read_positive_int_setting(
        "paypear_payment_lifetime_minutes",
        DEFAULT_PAYMENT_LIFETIME_MINUTES,
        min_value=1,
        max_value=4320,
    )
    env_lifetime = str(os.environ.get("PAYPEAR_PAYMENT_LIFETIME_MINUTES") or "").strip()
    if env_lifetime:
        with suppress(TypeError, ValueError):
            lifetime_minutes = max(1, min(4320, int(env_lifetime)))
    return lifetime_minutes * 60


def _normalize_payment_method(payment_method_type: str | None) -> str:
    value = str(payment_method_type or "").strip()
    normalized = value.upper()
    aliases = {
        "": PAYMENT_METHOD_SBP,
        "SBP": PAYMENT_METHOD_SBP,
        "P2P_SBP": PAYMENT_METHOD_SBP,
        "BANK_CARD": PAYMENT_METHOD_CARD,
        "CARD": PAYMENT_METHOD_CARD,
        "BASIC_CARD": PAYMENT_METHOD_CARD,
        "CRYPTO": PAYMENT_METHOD_CRYPTO,
        "SBERPAY": "SBERPAY",
        "TPAY": "TPAY",
    }
    if normalized in aliases:
        return aliases[normalized]
    if re.fullmatch(r"[A-Z0-9_]{2,64}", normalized):
        return normalized
    return PAYMENT_METHOD_SBP


def _normalize_legacy_payment_method(payment_method_type: str | None) -> str:
    value = str(payment_method_type or "").strip().upper()
    aliases = {
        "": "sbp",
        "SBP": "sbp",
        "P2P_SBP": "sbp",
        "SBERPAY": "sberpay",
        "TPAY": "tpay",
        "BANK_CARD": "bank_card",
        "CARD": "bank_card",
        "BASIC_CARD": "bank_card",
        "P2P_CARD": "bank_card",
        "BASIC_CARD_HPP": "bank_card",
        "CRYPTO": "crypto",
    }
    return aliases.get(value, "sbp")


def _legacy_payment_method_candidates(payment_method_type: str | None) -> list[str]:
    normalized = str(payment_method_type or "").strip().upper()
    if normalized in {"BANK_CARD", "CARD", "BASIC_CARD", "P2P_CARD", "BASIC_CARD_HPP"}:
        return list(_LEGACY_CARD_METHOD_FALLBACK_ORDER)
    return [_normalize_legacy_payment_method(payment_method_type)]


def _extract_response_payload(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    result = data.get("result")
    if isinstance(result, dict):
        return result
    payment = data.get("payment")
    if isinstance(payment, dict):
        return payment
    return data


def _extract_error_message(data: Any) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or error).strip()
        errors = data.get("errors")
        if isinstance(errors, list):
            messages: list[str] = []
            for item in errors[:3]:
                if isinstance(item, dict):
                    field = str(item.get("field") or item.get("objectName") or "").strip()
                    message = str(item.get("defaultMessage") or item.get("message") or item.get("code") or "").strip()
                    if message:
                        messages.append(f"{field}: {message}" if field else message)
                elif item:
                    messages.append(str(item).strip())
            if messages:
                return "; ".join(messages)
        return str(data.get("message") or data.get("errorMessage") or data.get("error_description") or error or data).strip()
    return str(data).strip()


async def _get_cached_checkout(order_id: str) -> _CheckoutEntry | None:
    now = time.time()
    async with _CHECKOUT_LOCK:
        entry = _CHECKOUT_CACHE.get(order_id)
        if not entry:
            return None
        if entry.expires_at <= now:
            _CHECKOUT_CACHE.pop(order_id, None)
            return None
        return entry


async def _set_cached_checkout(order_id: str, payment_id: str, payment_url: str, ttl_seconds: int) -> None:
    now = time.time()
    expires_at = now + max(1, ttl_seconds)
    async with _CHECKOUT_LOCK:
        expired_keys = [key for key, value in _CHECKOUT_CACHE.items() if value.expires_at <= now]
        for key in expired_keys:
            del _CHECKOUT_CACHE[key]

        _CHECKOUT_CACHE[order_id] = _CheckoutEntry(
            payment_id=str(payment_id).strip(),
            payment_url=str(payment_url).strip(),
            expires_at=expires_at,
        )

        if len(_CHECKOUT_CACHE) > _CHECKOUT_CACHE_MAX_SIZE:
            sorted_keys = sorted(_CHECKOUT_CACHE, key=lambda key: _CHECKOUT_CACHE[key].expires_at)
            for key in sorted_keys[: len(_CHECKOUT_CACHE) - _CHECKOUT_CACHE_MAX_SIZE]:
                del _CHECKOUT_CACHE[key]


class PayPearService:
    """Thin HTTP wrapper with unified response normalization."""

    def __init__(self):
        shop_id, secret_key = _get_credentials()
        self._shop_id = shop_id
        self._secret_key = secret_key
        self._base_url = _resolve_api_base_url()
        self._timeout_seconds = _read_positive_int_setting(
            "paypear_request_timeout_seconds",
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
            min_value=5,
            max_value=60,
        )

    def _headers_modern(self, *, idempotence_key: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._secret_key}",
            "Content-Type": "application/json",
        }
        if idempotence_key:
            headers["Idempotence-Key"] = idempotence_key
        if self._shop_id:
            headers["X-Shop-Id"] = self._shop_id
        return headers

    def _headers_legacy(self, *, idempotence_key: str | None = None) -> dict[str, str]:
        credentials = f"{self._shop_id}:{self._secret_key}"
        encoded_credentials = b64encode(credentials.encode("utf-8")).decode("utf-8")
        headers = {
            "Authorization": f"Basic {encoded_credentials}",
            "Content-Type": "application/json",
        }
        if idempotence_key:
            headers["Idempotence-Key"] = idempotence_key
        return headers

    async def _request_json(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        json_payload: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if method == "POST":
                    async with session.post(url, json=json_payload, headers=headers) as response:
                        raw_body = await response.text()
                        try:
                            data = json.loads(raw_body) if raw_body else {}
                        except json.JSONDecodeError:
                            data = {"raw": raw_body}
                        return response.status, data
                async with session.get(url, headers=headers) as response:
                    raw_body = await response.text()
                    try:
                        data = json.loads(raw_body) if raw_body else {}
                    except json.JSONDecodeError:
                        data = {"raw": raw_body}
                    return response.status, data
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise PayPearNetworkError(str(exc)) from exc

    async def _create_payment_modern(
        self,
        *,
        amount: float,
        order_id: str,
        description: str,
        payment_method_type: str,
        currency: str,
        return_url: str,
        webhook_url: str,
    ) -> dict[str, str]:
        payload: dict[str, Any] = {
            "referenceId": str(order_id),
            "paymentType": "DEPOSIT",
            "paymentMethod": _normalize_payment_method(payment_method_type),
            "amount": round(float(amount), 2),
            "currency": currency,
            "description": description,
        }
        if return_url:
            payload["returnUrl"] = return_url
        if webhook_url:
            payload["webhookUrl"] = webhook_url

        status_code, data = await self._request_json(
            method="POST",
            url=f"{self._base_url}/payments",
            json_payload=payload,
            headers=self._headers_modern(idempotence_key=str(order_id)),
        )
        if status_code not in {200, 201}:
            raise PayPearAPIError(status_code, _extract_error_message(data))

        result = _extract_response_payload(data)
        confirmation = result.get("confirmation") if isinstance(result, dict) else {}
        payment_id = str(
            result.get("id")
            or result.get("paymentId")
            or result.get("payment_id")
            or result.get("transaction_id")
            or ""
        ).strip()
        payment_url = str(
            result.get("redirect_url")
            or result.get("redirectUrl")
            or result.get("payment_url")
            or result.get("paymentUrl")
            or result.get("url")
            or ""
        ).strip()
        if isinstance(confirmation, dict):
            payment_url = payment_url or str(
                confirmation.get("confirmation_url")
                or confirmation.get("redirect_url")
                or confirmation.get("redirectUrl")
                or confirmation.get("url")
                or ""
            ).strip()

        if not payment_id or not payment_url:
            raise PayPearAPIError(200, "Provider response does not contain payment_id/payment_url.")
        return {"payment_id": payment_id, "payment_url": payment_url}

    async def _create_payment_legacy(
        self,
        *,
        amount: float,
        order_id: str,
        description: str,
        payment_method_type: str,
        currency: str,
        return_url: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, str]:
        method_candidates = _legacy_payment_method_candidates(payment_method_type)
        normalized_candidates = [candidate for candidate in method_candidates if str(candidate).strip()]
        if not normalized_candidates:
            normalized_candidates = ["sbp"]

        last_error: PayPearAPIError | None = None
        for index, legacy_method in enumerate(normalized_candidates):
            payload: dict[str, Any] = {
                "amount": {"value": f"{amount:.2f}", "currency": currency},
                "order_id": str(order_id),
                "payment_method_data": {"type": legacy_method},
                "description": description,
            }
            if return_url:
                payload["confirmation"] = {
                    "type": "redirect",
                    "return_url": return_url,
                }
            if metadata:
                payload["metadata"] = metadata

            status_code, data = await self._request_json(
                method="POST",
                url=f"{_PAYPEAR_LEGACY_DEFAULT_BASE_URL}/payment/",
                json_payload=payload,
                headers=self._headers_legacy(idempotence_key=str(order_id)),
            )
            if status_code == 200 and isinstance(data, dict) and data.get("success") is True:
                result = data.get("result") or {}
                confirmation = result.get("confirmation") if isinstance(result, dict) else {}
                payment_id = str(result.get("id") or result.get("payment_id") or result.get("transaction_id") or "").strip()
                payment_url = str(result.get("payment_url") or result.get("url") or "").strip()
                if isinstance(confirmation, dict):
                    payment_url = payment_url or str(
                        confirmation.get("confirmation_url") or confirmation.get("url") or ""
                    ).strip()
                if not payment_id or not payment_url:
                    raise PayPearAPIError(200, "Provider response does not contain payment_id/payment_url.")
                return {"payment_id": payment_id, "payment_url": payment_url}

            error_message = _extract_error_message(data)
            last_error = PayPearAPIError(status_code, error_message)
            has_next_candidate = index + 1 < len(normalized_candidates)
            can_retry_method = (
                has_next_candidate
                and status_code == 400
                and "payment method type is not found" in error_message.lower()
            )
            if can_retry_method:
                logger.warning(
                    "[PayPear] legacy method '%s' rejected (%s), retrying with '%s'",
                    legacy_method,
                    error_message,
                    normalized_candidates[index + 1],
                )
                continue
            raise last_error

        if last_error is not None:
            raise last_error
        raise PayPearAPIError(400, "Unknown legacy payment creation error.")

    async def _check_status_modern(self, payment_id: str) -> dict[str, str]:
        status_code, data = await self._request_json(
            method="GET",
            url=f"{self._base_url}/payments/{payment_id}",
            headers=self._headers_modern(),
        )
        if status_code != 200:
            raise PayPearAPIError(status_code, _extract_error_message(data))

        result = _extract_response_payload(data)
        provider_status = str(
            result.get("state")
            or result.get("paymentState")
            or result.get("status")
            or ""
        ).strip()
        return {
            "status": map_paypear_status(provider_status),
            "provider_status": provider_status,
        }

    async def _check_status_legacy(self, payment_id: str) -> dict[str, str]:
        status_code, data = await self._request_json(
            method="GET",
            url=f"{_PAYPEAR_LEGACY_DEFAULT_BASE_URL}/payment/{payment_id}/",
            headers=self._headers_legacy(),
        )
        if status_code != 200 or not isinstance(data, dict) or data.get("success") is not True:
            raise PayPearAPIError(status_code, _extract_error_message(data))

        result = data.get("result") or {}
        provider_status = str(result.get("status") or "").strip()
        return {
            "status": map_paypear_status(provider_status),
            "provider_status": provider_status,
        }

    @staticmethod
    def _should_try_legacy_fallback(exc: BaseException) -> bool:
        if isinstance(exc, PayPearNetworkError):
            return True
        if isinstance(exc, PayPearAPIError):
            return exc.status_code in {400, 401, 403, 404, 405, 422, 429, 500, 502, 503, 504}
        return False

    async def create_payment(
        self,
        *,
        amount: float,
        order_id: str,
        description: str = "",
        payment_method_type: str = PAYMENT_METHOD_SBP,
        currency: str = "RUB",
        return_url: str = "",
        webhook_url: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        if amount <= 0:
            raise PayPearValidationError("amount_must_be_positive")

        final_return_url = str(return_url or _resolve_return_url()).strip()
        final_webhook_url = str(webhook_url or _resolve_webhook_url()).strip()
        final_description = str(description or f"VPN payment {order_id}").strip()
        normalized_method = _normalize_payment_method(payment_method_type)

        if _is_legacy_base_url(self._base_url):
            result = await self._create_payment_legacy(
                amount=float(amount),
                order_id=order_id,
                description=final_description,
                payment_method_type=normalized_method,
                currency=currency,
                return_url=final_return_url,
                metadata=metadata,
            )
        else:
            try:
                result = await self._create_payment_modern(
                    amount=float(amount),
                    order_id=order_id,
                    description=final_description,
                    payment_method_type=normalized_method,
                    currency=currency,
                    return_url=final_return_url,
                    webhook_url=final_webhook_url,
                )
            except Exception as exc:
                if not self._should_try_legacy_fallback(exc):
                    raise
                logger.warning(
                    "[PayPear] modern create_payment failed, retrying legacy mode: %s (%s)",
                    get_exception_code(exc),
                    get_exception_message(exc),
                )
                result = await self._create_payment_legacy(
                    amount=float(amount),
                    order_id=order_id,
                    description=final_description,
                    payment_method_type=normalized_method,
                    currency=currency,
                    return_url=final_return_url,
                    metadata=metadata,
                )

        payment_id = str(result.get("payment_id") or "").strip()
        logger.info("[PayPear] created checkout order_id=%s payment_id=%s", order_id, payment_id)
        return result

    async def check_status(self, payment_id: str) -> dict[str, str]:
        normalized_id = str(payment_id or "").strip()
        if _is_legacy_base_url(self._base_url):
            return await self._check_status_legacy(normalized_id)
        try:
            return await self._check_status_modern(normalized_id)
        except Exception as exc:
            if not self._should_try_legacy_fallback(exc):
                raise
            logger.warning(
                "[PayPear] modern check_status failed, retrying legacy mode: %s (%s)",
                get_exception_code(exc),
                get_exception_message(exc),
            )
            return await self._check_status_legacy(normalized_id)


async def create_payment(
    *,
    amount_rub: float,
    order_id: str,
    description: str,
    metadata: dict[str, Any] | None,
    payment_method_type: str = PAYMENT_METHOD_SBP,
) -> dict[str, str]:
    normalized_order_id = str(order_id or "").strip()
    if not normalized_order_id:
        raise PayPearValidationError("order_id_required")

    cached = await _get_cached_checkout(normalized_order_id)
    if cached:
        logger.info("[PayPear] reuse checkout order_id=%s payment_id=%s", normalized_order_id, cached.payment_id)
        return {
            "payment_id": cached.payment_id,
            "payment_url": cached.payment_url,
        }

    ttl_seconds = _resolve_checkout_ttl(metadata or {})

    async def _operation() -> dict[str, str]:
        service = PayPearService()
        return await service.create_payment(
            amount=float(amount_rub),
            order_id=normalized_order_id,
            description=description,
            payment_method_type=payment_method_type,
            metadata=metadata,
        )

    result = await _run_with_network_retries(
        action_name="create_checkout",
        key=normalized_order_id,
        operation=_operation,
    )
    await _set_cached_checkout(
        order_id=normalized_order_id,
        payment_id=result["payment_id"],
        payment_url=result["payment_url"],
        ttl_seconds=ttl_seconds,
    )
    return result


async def check_status(payment_id: str) -> dict[str, str]:
    async def _operation() -> dict[str, str]:
        service = PayPearService()
        return await service.check_status(payment_id)

    return await _run_with_network_retries(
        action_name="check_status",
        key=str(payment_id),
        operation=_operation,
    )
