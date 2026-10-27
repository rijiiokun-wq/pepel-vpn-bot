"""
Platega payment integration for reserve bot mode.

This module wraps the SDK with project-friendly helpers:
- create_payment(...) compatible with bot handlers
- verify_webhook_signature(...) for Flask webhook validation
- status normalization to succeeded/canceled/pending
- idempotent checkout reuse for repeated create calls with same order_id
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
import uuid
from dataclasses import dataclass
from hmac import compare_digest
from typing import Any

from shop_bot.data_manager.database import get_setting

logger = logging.getLogger(__name__)

PLATEGA_API_BASE_URL = "https://app.platega.io"
DEFAULT_CHECKOUT_TTL_SECONDS = 1800
DEFAULT_REQUEST_TIMEOUT_SECONDS = 15
DEFAULT_NETWORK_RETRY_ATTEMPTS = 3
DEFAULT_NETWORK_RETRY_BACKOFF_SECONDS = 0.75

_PAID_STATUSES = frozenset({"confirmed", "success", "succeeded", "paid", "completed"})
_CANCELED_STATUSES = frozenset({"canceled", "cancelled", "expired", "failed", "declined"})


class PlategaConfigError(RuntimeError):
    """Raised when required Platega settings are missing."""


try:
    from plategaio import (  # type: ignore
        CreateTransactionRequest,
        PaymentDetails,
        PlategaAPIError,
        PlategaAsyncClient,
        PlategaError,
        PlategaNetworkError,
    )
    _PLATEGA_IMPORT_ERROR: Exception | None = None
except (ImportError, ModuleNotFoundError) as exc:  # pragma: no cover - fallback when plategaio not installed
    CreateTransactionRequest = None  # type: ignore[assignment]
    PaymentDetails = None  # type: ignore[assignment]
    PlategaAsyncClient = None  # type: ignore[assignment]
    PlategaError = Exception  # type: ignore[assignment]
    PlategaAPIError = Exception  # type: ignore[assignment]
    PlategaNetworkError = Exception  # type: ignore[assignment]
    _PLATEGA_IMPORT_ERROR = exc


@dataclass(slots=True)
class _CheckoutEntry:
    payment_id: str
    payment_url: str
    expires_at: float


_CHECKOUT_CACHE: dict[str, _CheckoutEntry] = {}
_CHECKOUT_LOCK = asyncio.Lock()
_CHECKOUT_CACHE_MAX_SIZE = 1000
DEFAULT_CARD_PAYMENT_METHOD_IDS: tuple[int, ...] = (10, 12, 11)
_CARD_METHOD_COMPATIBILITY_ORDER: tuple[int, ...] = DEFAULT_CARD_PAYMENT_METHOD_IDS
_FALLBACK_RETRY_ERROR_MARKERS: tuple[str, ...] = (
    "wrong input",
    "validation",
    "payment method",
    "available requisites",
    "method unavailable",
    "method not available",
)


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
    """
    Return compact type chain for alerts/logs, e.g.:
    PlategaNetworkError/ConnectTimeout
    """
    type_chain: list[str] = []
    for item in _exception_chain(exc):
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


async def _run_with_network_retries(
    *,
    action_name: str,
    key: str,
    operation: Any,
) -> Any:
    max_attempts = _read_positive_int_setting(
        "platega_network_retry_attempts",
        DEFAULT_NETWORK_RETRY_ATTEMPTS,
        min_value=1,
        max_value=6,
    )
    base_backoff_seconds = _read_positive_float_setting(
        "platega_network_retry_backoff_seconds",
        DEFAULT_NETWORK_RETRY_BACKOFF_SECONDS,
        min_value=0.1,
        max_value=10.0,
    )

    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except PlategaNetworkError as exc:
            error_code = get_exception_code(exc)
            if attempt >= max_attempts:
                logger.exception(
                    "[Platega] %s failed after %s/%s attempts key=%s error=%s",
                    action_name,
                    attempt,
                    max_attempts,
                    key,
                    error_code,
                )
                raise

            backoff_seconds = min(base_backoff_seconds * (2 ** (attempt - 1)), 10.0)
            logger.warning(
                "[Platega] %s network retry %s/%s key=%s error=%s sleep=%.2fs",
                action_name,
                attempt,
                max_attempts,
                key,
                error_code,
                backoff_seconds,
            )
            await asyncio.sleep(backoff_seconds)


def map_platega_status(status: str | None) -> str:
    """
    Convert provider status to internal enum.

    Returns one of: succeeded | canceled | pending
    """
    value = str(status or "").strip().lower()
    if value in _PAID_STATUSES:
        return "succeeded"
    if value in _CANCELED_STATUSES:
        return "canceled"
    return "pending"


def verify_webhook_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify HMAC-SHA256 signature.

    Accepts both:
    - '<hex_digest>'
    - 'sha256=<hex_digest>'
    """
    raw_payload = payload or b""
    raw_signature = str(signature or "").strip()
    raw_secret = str(secret or "").strip()
    if not raw_payload or not raw_signature or not raw_secret:
        return False

    if raw_signature.lower().startswith("sha256="):
        raw_signature = raw_signature.split("=", 1)[1].strip()

    expected = hmac.new(raw_secret.encode("utf-8"), raw_payload, hashlib.sha256).hexdigest()
    return compare_digest(expected, raw_signature)


def _require_sdk() -> None:
    if _PLATEGA_IMPORT_ERROR is not None:
        raise RuntimeError(
            "plategaio SDK is not available. Install dependencies before using Platega integration."
        ) from _PLATEGA_IMPORT_ERROR


def _get_credentials() -> tuple[str, str]:
    shop_id = str(get_setting("platega_shop_id") or "").strip()
    api_key = str(get_setting("platega_api_key") or "").strip()
    if not shop_id or not api_key:
        raise PlategaConfigError(
            "[Platega] Missing credentials: set 'platega_shop_id' and 'platega_api_key' in bot settings."
        )
    return shop_id, api_key


def _resolve_return_url() -> str:
    username = str(get_setting("reserve_bot_username") or "").strip()
    if username:
        return f"https://t.me/{username}"
    return "https://t.me"


def _resolve_checkout_ttl(metadata: dict[str, Any] | None) -> int:
    if not isinstance(metadata, dict):
        return DEFAULT_CHECKOUT_TTL_SECONDS
    raw_value = metadata.get("checkout_ttl_seconds")
    if raw_value in (None, ""):
        return DEFAULT_CHECKOUT_TTL_SECONDS
    try:
        ttl = int(raw_value)
    except (TypeError, ValueError):
        return DEFAULT_CHECKOUT_TTL_SECONDS
    if ttl <= 0:
        return DEFAULT_CHECKOUT_TTL_SECONDS
    return ttl


def _compatibility_fallback_methods(*, payment_variant: str) -> list[int]:
    """Return compatibility method candidates for card checkouts."""
    if str(payment_variant or "").strip().lower() != "card":
        return []
    return list(_CARD_METHOD_COMPATIBILITY_ORDER)


def _parse_payment_method_candidates(raw_value: Any) -> list[int]:
    raw_items: list[Any]
    if isinstance(raw_value, str):
        raw_items = raw_value.replace(";", ",").split(",")
    elif isinstance(raw_value, (list, tuple)):
        raw_items = list(raw_value)
    else:
        raw_items = []

    candidates: list[int] = []
    for raw_item in raw_items:
        try:
            candidate = int(str(raw_item).strip())
        except (TypeError, ValueError):
            continue
        if candidate > 0 and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _can_retry_with_fallback(error_text: str) -> bool:
    normalized = str(error_text or "").strip().lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in _FALLBACK_RETRY_ERROR_MARKERS)


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
        # Evict expired entries to prevent unbounded growth
        expired_keys = [k for k, v in _CHECKOUT_CACHE.items() if v.expires_at <= now]
        for k in expired_keys:
            del _CHECKOUT_CACHE[k]

        _CHECKOUT_CACHE[order_id] = _CheckoutEntry(
            payment_id=str(payment_id).strip(),
            payment_url=str(payment_url).strip(),
            expires_at=expires_at,
        )

        # Hard cap: drop oldest entries if cache exceeds max size
        if len(_CHECKOUT_CACHE) > _CHECKOUT_CACHE_MAX_SIZE:
            sorted_keys = sorted(_CHECKOUT_CACHE, key=lambda k: _CHECKOUT_CACHE[k].expires_at)
            for k in sorted_keys[: len(_CHECKOUT_CACHE) - _CHECKOUT_CACHE_MAX_SIZE]:
                del _CHECKOUT_CACHE[k]


class PlategaService:
    """Thin SDK wrapper with unified logging and response normalization."""

    def __init__(self):
        _require_sdk()
        shop_id, api_key = _get_credentials()
        timeout_seconds = _read_positive_int_setting(
            "platega_request_timeout_seconds",
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
            min_value=5,
            max_value=60,
        )
        self._client = PlategaAsyncClient(  # type: ignore[misc]
            merchant_id=shop_id,
            secret=api_key,
            base_url=PLATEGA_API_BASE_URL,
            timeout=timeout_seconds,
        )

    async def __aenter__(self) -> PlategaService:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.close()

    async def create_payment(
        self,
        amount: float,
        order_id: str,
        description: str = "",
        payment_method: int = 10,
        currency: str = "RUB",
        return_url: str = "",
    ) -> dict[str, str]:
        if amount <= 0:
            raise ValueError("[Platega] Amount must be greater than 0.")

        final_return_url = str(return_url or _resolve_return_url()).strip()
        final_description = str(description or f"VPN payment {order_id}").strip()

        request = CreateTransactionRequest(  # type: ignore[misc]
            paymentMethod=payment_method,
            id=uuid.uuid4(),
            paymentDetails=PaymentDetails(amount=amount, currency=currency),  # type: ignore[misc]
            description=final_description,
            payload=str(order_id),
            returnUrl=final_return_url,
            failedUrl=final_return_url,
        )

        response = await self._client.create_transaction(request)
        payment_id = str(
            getattr(response, "transaction_id", None)
            or getattr(response, "id", None)
            or getattr(response, "payment_id", None)
            or ""
        ).strip()
        payment_url = str(
            getattr(response, "redirect", None)
            or getattr(response, "redirect_url", None)
            or getattr(response, "payment_url", None)
            or ""
        ).strip()

        if not payment_id or not payment_url:
            raise PlategaError("[Platega] Provider response does not contain payment_id/payment_url.")

        logger.info("[Platega] created checkout order_id=%s payment_id=%s", order_id, payment_id)
        return {"payment_id": payment_id, "payment_url": payment_url}

    async def check_status(self, payment_id: str) -> dict[str, str]:
        response = await self._client.get_transaction_status(str(payment_id))
        provider_status = str(getattr(response, "status", "") or "").strip().lower()
        return {
            "status": map_platega_status(provider_status),
            "provider_status": provider_status,
        }


async def create_payment(
    amount_rub: float,
    order_id: str,
    description: str,
    metadata: dict[str, Any] | None,
) -> dict[str, str]:
    """
    Idempotent create checkout helper used by handlers.

    Reuses checkout when the same order_id is requested while cached link is alive.
    """
    normalized_order_id = str(order_id or "").strip()
    if not normalized_order_id:
        raise ValueError("[Platega] order_id is required.")

    cached = await _get_cached_checkout(normalized_order_id)
    if cached:
        logger.info("[Platega] reuse checkout order_id=%s payment_id=%s", normalized_order_id, cached.payment_id)
        return {
            "payment_id": cached.payment_id,
            "payment_url": cached.payment_url,
        }

    payment_method = 10
    payment_variant = ""
    payment_method_candidates: list[int] = []
    fallback_methods: list[int] = []
    if isinstance(metadata, dict):
        try:
            payment_method = int(metadata.get("platega_payment_method") or 10)
        except (TypeError, ValueError):
            payment_method = 10
        payment_variant = str(metadata.get("platega_payment_variant") or "").strip().lower()
        payment_method_candidates = _parse_payment_method_candidates(
            metadata.get("platega_payment_method_candidates")
        )
        raw_fallbacks = metadata.get("platega_payment_method_fallbacks")
        fallback_methods = _parse_payment_method_candidates(raw_fallbacks)

    ttl_seconds = _resolve_checkout_ttl(metadata or {})
    methods_to_try: list[int] = list(payment_method_candidates)
    compatibility_methods = _compatibility_fallback_methods(
        payment_variant=payment_variant,
    )
    for compatibility_method in compatibility_methods:
        if compatibility_method > 0 and compatibility_method not in methods_to_try:
            methods_to_try.append(compatibility_method)
    if payment_method > 0 and payment_method not in methods_to_try:
        methods_to_try.append(payment_method)
    for fallback_method in fallback_methods:
        if fallback_method not in methods_to_try:
            methods_to_try.append(fallback_method)

    last_error: Exception | None = None
    for index, method_candidate in enumerate(methods_to_try):
        try:
            async def _operation(payment_method_to_try: int = method_candidate) -> dict[str, str]:
                async with PlategaService() as service:
                    return await service.create_payment(
                        amount=float(amount_rub),
                        order_id=normalized_order_id,
                        description=description,
                        payment_method=payment_method_to_try,
                    )

            result = await _run_with_network_retries(
                action_name="create_checkout",
                key=normalized_order_id,
                operation=_operation,
            )
            if method_candidate != payment_method:
                logger.warning(
                    "[Platega] checkout fallback method used order_id=%s requested=%s actual=%s",
                    normalized_order_id,
                    payment_method,
                    method_candidate,
                )
            break
        except PlategaConfigError:
            raise
        except PlategaAPIError as exc:
            last_error = exc
            error_text = str(exc)
            can_try_next = (
                index + 1 < len(methods_to_try)
                and _can_retry_with_fallback(error_text)
            )
            if can_try_next:
                logger.warning(
                    "[Platega] method=%s rejected for order_id=%s, trying fallback method=%s (%s)",
                    method_candidate,
                    normalized_order_id,
                    methods_to_try[index + 1],
                    exc,
                )
                continue
            logger.exception("[Platega] API error while creating checkout order_id=%s", normalized_order_id)
            raise
        except PlategaError:
            logger.exception("[Platega] Provider error while creating checkout order_id=%s", normalized_order_id)
            raise
    else:
        if last_error is not None:
            raise last_error
        raise PlategaError("[Platega] Could not create checkout using configured payment methods.")

    await _set_cached_checkout(
        order_id=normalized_order_id,
        payment_id=result["payment_id"],
        payment_url=result["payment_url"],
        ttl_seconds=ttl_seconds,
    )
    return result


async def check_status(payment_id: str) -> dict[str, str]:
    """Fallback reconciliation helper."""
    async def _operation() -> dict[str, str]:
        async with PlategaService() as service:
            return await service.check_status(payment_id)

    return await _run_with_network_retries(
        action_name="check_status",
        key=str(payment_id),
        operation=_operation,
    )
