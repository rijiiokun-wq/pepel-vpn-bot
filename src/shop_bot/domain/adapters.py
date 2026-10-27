"""Adapter layer + mode switch for issuance backend migration.

Provides ``IssuanceRouter`` — a single entry point that delegates to
either the existing Python issuance flow (``LegacyAdapter``) or the
future Go backend (``BackendAdapter``) based on feature flags.

Feature flags (environment variables):
- ``ISSUANCE_BACKEND_MODE`` — legacy | shadow | canary | primary
- ``ISSUANCE_CANARY_PERCENT`` — 0-100, for canary mode
- ``ISSUANCE_BACKEND_KILL_SWITCH`` — "1" forces legacy instantly
- ``ISSUANCE_BACKEND_URL`` — Go backend base URL
- ``ISSUANCE_BACKEND_TIMEOUT_SECONDS`` — HTTP timeout for Go backend
- ``ISSUANCE_BACKEND_AUTH_TOKEN`` — optional shared token for Go backend calls

Default: ``legacy`` — zero risk, current behavior preserved.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

BACKEND_FALLBACK_ERRORS = (
    aiohttp.ClientError,
    asyncio.TimeoutError,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    KeyError,
)

# ── Shadow comparison counters ────────────────────────────────────────

_shadow_counters: dict[str, int] = {
    "total": 0,
    "match": 0,
    "diff": 0,
    "error": 0,
}


def get_shadow_stats() -> dict[str, object]:
    total = _shadow_counters["total"] or 1
    return {
        **_shadow_counters,
        "match_rate": round(_shadow_counters["match"] / total * 100, 1),
    }


def _persist_shadow_diff(
    payment_id: str,
    legacy: Any,
    shadow: Any,
    legacy_ms: int,
    shadow_ms: int,
) -> None:
    try:
        from shop_bot.data_manager import database

        legacy_reason = getattr(legacy, "reason", None)
        shadow_reason = getattr(shadow, "reason", None)
        database.record_shadow_diff(
            payment_id=payment_id,
            legacy_status=str(getattr(legacy, "status", legacy)),
            shadow_status=str(getattr(shadow, "status", shadow)),
            legacy_reason=str(legacy_reason) if legacy_reason is not None else None,
            shadow_reason=str(shadow_reason) if shadow_reason is not None else None,
            legacy_ms=legacy_ms,
            shadow_ms=shadow_ms,
        )
    except Exception as exc:  # noqa: BLE001 - shadow telemetry must not affect user issuance.
        logger.warning("Could not persist shadow diff: payment=%s error=%s", payment_id, exc)


# ── Feature flags ────────────────────────────────────────────────────

class IssuanceBackendMode:
    LEGACY = "legacy"
    SHADOW = "shadow"
    CANARY = "canary"
    PRIMARY = "primary"


def _read_mode() -> str:
    return os.environ.get("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.LEGACY)


def _read_canary_percent() -> int:
    try:
        return max(0, min(100, int(os.environ.get("ISSUANCE_CANARY_PERCENT", "0"))))
    except (ValueError, TypeError):
        return 0


def _read_kill_switch() -> bool:
    return os.environ.get("ISSUANCE_BACKEND_KILL_SWITCH", "0") == "1"


def _read_backend_url() -> str:
    return os.environ.get("ISSUANCE_BACKEND_URL", "http://localhost:8090")


def _read_backend_timeout() -> int:
    try:
        return max(5, int(os.environ.get("ISSUANCE_BACKEND_TIMEOUT_SECONDS", "30")))
    except (ValueError, TypeError):
        return 30


def _read_backend_auth_token() -> str:
    return os.environ.get("ISSUANCE_BACKEND_AUTH_TOKEN", "").strip()


def _backend_auth_headers(token: str) -> dict[str, str] | None:
    if not token:
        return None
    return {"X-Issuance-Backend-Token": token}


def _backend_event_id(metadata: dict[str, Any]) -> str | None:
    raw_event_id = metadata.get("idempotency_key") or metadata.get("event_id")
    event_id = str(raw_event_id or "").strip()
    if event_id:
        return event_id

    payment_id = str(metadata.get("external_payment_id") or metadata.get("payment_id") or "").strip()
    if not payment_id:
        return None
    bot_source = str(metadata.get("bot_source") or "main").strip().lower() or "main"
    return f"payment-confirm:{bot_source}:{payment_id}"


def _backend_returned_stub(result: AdapterResult) -> bool:
    return str(result.reason or "").strip().lower().startswith("stub:")


# ── Result DTO ───────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class AdapterResult:
    """Standardised issuance result from any adapter."""
    status: str
    reason: str | None = None
    retry_at: str | None = None
    key_id: int | None = None
    connection_string: str | None = None


# ── Adapters ─────────────────────────────────────────────────────────

class LegacyAdapter:
    """Delegates to the existing Python issuance flow."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot

    async def process_payment(self, metadata: dict[str, Any]) -> AdapterResult:
        from shop_bot.bot import handlers as bot_handlers

        await bot_handlers.process_successful_payment(self._bot, metadata)

        from shop_bot.data_manager.database import is_transaction_fully_issued

        payment_id = str(metadata.get("external_payment_id") or "")
        if payment_id and is_transaction_fully_issued(payment_id):
            return AdapterResult(status="issued")
        return AdapterResult(status="retry_scheduled", reason="legacy_not_marked_paid")


class BackendAdapter:
    """Will call the Go backend via HTTP (stub until Stage C)."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int | None = None,
        auth_token: str | None = None,
    ) -> None:
        self._base_url = (base_url or _read_backend_url()).rstrip("/")
        self._timeout = timeout or _read_backend_timeout()
        self._auth_token = (auth_token if auth_token is not None else _read_backend_auth_token()).strip()

    async def process_payment(self, metadata: dict[str, Any]) -> AdapterResult:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=self._timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            resp = await session.post(
                f"{self._base_url}/v1/payments/confirm",
                headers=_backend_auth_headers(self._auth_token),
                json={
                    "payment_id": metadata.get("external_payment_id"),
                    "provider_status": "succeeded",
                    "metadata": metadata,
                    "bot_source": metadata.get("bot_source", "main"),
                    "event_id": _backend_event_id(metadata),
                },
            )
            resp.raise_for_status()
            data = await resp.json()
            return AdapterResult(
                status=data.get("status", "unknown"),
                reason=data.get("reason"),
                retry_at=data.get("retry_at"),
                key_id=data.get("key_id"),
                connection_string=data.get("connection_string"),
            )


# ── Router ───────────────────────────────────────────────────────────

class IssuanceRouter:
    """Routes issuance to legacy Python or Go backend based on feature flags."""

    def __init__(
        self,
        legacy: LegacyAdapter,
        backend: BackendAdapter | None = None,
    ) -> None:
        self._legacy = legacy
        self._backend = backend

    async def process_payment(self, metadata: dict[str, Any]) -> AdapterResult:
        if _read_kill_switch():
            return await self._legacy.process_payment(metadata)

        mode = _read_mode()

        if mode == IssuanceBackendMode.LEGACY:
            return await self._legacy.process_payment(metadata)

        if mode == IssuanceBackendMode.SHADOW:
            return await self._shadow(metadata)

        if mode == IssuanceBackendMode.CANARY:
            return await self._canary(metadata)

        if mode == IssuanceBackendMode.PRIMARY:
            return await self._primary(metadata)

        return await self._legacy.process_payment(metadata)

    async def _shadow(self, metadata: dict[str, Any]) -> AdapterResult:
        import time as _time

        legacy_start = _time.monotonic()
        result = await self._legacy.process_payment(metadata)
        legacy_ms = int((_time.monotonic() - legacy_start) * 1000)

        if self._backend is None:
            return result

        pid = str(metadata.get("external_payment_id") or "")
        shadow_ms = 0
        try:
            shadow_start = _time.monotonic()
            shadow = await self._backend.process_payment(metadata)
            shadow_ms = int((_time.monotonic() - shadow_start) * 1000)

            match = result.status == shadow.status
            if match:
                logger.info(
                    "Shadow MATCH: payment=%s status=%s legacy_ms=%d shadow_ms=%d",
                    pid, result.status, legacy_ms, shadow_ms,
                )
                _shadow_counters["match"] += 1
            else:
                logger.warning(
                    "Shadow DIFF: payment=%s legacy=%s shadow=%s legacy_ms=%d shadow_ms=%d",
                    pid, result.status, shadow.status, legacy_ms, shadow_ms,
                )
                _shadow_counters["diff"] += 1
                await asyncio.to_thread(_persist_shadow_diff, pid, result, shadow, legacy_ms, shadow_ms)
            _shadow_counters["total"] += 1
        except Exception as exc:
            logger.warning("Shadow backend error: payment=%s error=%s", pid, exc)
            _shadow_counters["error"] += 1
            _shadow_counters["total"] += 1
        return result

    async def _canary(self, metadata: dict[str, Any]) -> AdapterResult:
        if self._backend is None:
            return await self._legacy.process_payment(metadata)

        pid = str(metadata.get("external_payment_id") or "")
        bucket = int(hashlib.md5(pid.encode()).hexdigest()[:8], 16) % 100  # noqa: S324
        if bucket < _read_canary_percent():
            try:
                result = await self._backend.process_payment(metadata)
            except BACKEND_FALLBACK_ERRORS:
                logger.exception("Backend canary failed, falling back.")
                return await self._legacy.process_payment(metadata)
            else:
                if _backend_returned_stub(result):
                    logger.error("Backend canary returned stub result, falling back to legacy.")
                    return await self._legacy.process_payment(metadata)
                return result
        return await self._legacy.process_payment(metadata)

    async def _primary(self, metadata: dict[str, Any]) -> AdapterResult:
        if self._backend is None:
            return await self._legacy.process_payment(metadata)
        try:
            result = await self._backend.process_payment(metadata)
        except BACKEND_FALLBACK_ERRORS:
            logger.exception("Backend primary failed, falling back to legacy.")
            return await self._legacy.process_payment(metadata)
        else:
            if _backend_returned_stub(result):
                logger.error("Backend primary returned stub result, falling back to legacy.")
                return await self._legacy.process_payment(metadata)
            return result


def build_issuance_router(bot: Any) -> IssuanceRouter:
    """Build runtime issuance router for confirmed-payment processing."""
    return IssuanceRouter(
        legacy=LegacyAdapter(bot),
        backend=BackendAdapter(),
    )


async def process_confirmed_payment(bot: Any, metadata: dict[str, Any]) -> AdapterResult:
    """Process confirmed payment via backend mode router."""
    router = build_issuance_router(bot)
    return await router.process_payment(metadata)
