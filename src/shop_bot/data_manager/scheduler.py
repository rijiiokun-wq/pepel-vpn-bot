import asyncio
import concurrent.futures
import contextlib
import functools
import gc
import json
import logging
import os
import random
import re
import smtplib
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from functools import partial
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot import runtime_guard
from shop_bot.bot_controller import BotController
from shop_bot.config import BOT_SOURCE
from shop_bot.config import is_truthy as _is_truthy
from shop_bot.data_manager import database, referrals
from shop_bot.domain.adapters import process_confirmed_payment
from shop_bot.errors import BotError, classify_error, send_critical_alert_nonblocking
from shop_bot.modules import blitz_api, lava_api, paypear_api, platega_api, subscription_dates, xui_api
from shop_bot.payment_metadata import (
    merge_payment_metadata,
    metadata_bot_source_matches_runtime,
    missing_required_payment_metadata_fields,
    normalize_bot_source,
    normalize_payment_metadata,
)

logger = logging.getLogger(__name__)

try:
    _MOSCOW_TZ = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    _MOSCOW_TZ = timezone(timedelta(hours=3))


def _naive_now() -> datetime:
    return datetime.now()  # noqa: DTZ005 - legacy scheduler timestamps are stored as naive datetimes.


def _naive_utcnow() -> datetime:
    return datetime.utcnow()  # noqa: DTZ003 - preserves existing naive UTC age calculations.


def _naive_fromtimestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp)  # noqa: DTZ006 - preserves legacy naive timestamp parsing.


def _get_env_positive_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, fallback to %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%s is below minimum %s, fallback to %s", name, value, minimum, default)
        return default
    return value


CHECK_INTERVAL_SECONDS = _get_env_positive_int("SCHEDULER_SYNC_INTERVAL_SECONDS", 300, minimum=30)
SCHEDULER_INITIAL_DELAY_SECONDS = _get_env_positive_int("SCHEDULER_INITIAL_DELAY_SECONDS", 10, minimum=0)
SCHEDULER_JITTER_SECONDS = _get_env_positive_int("SCHEDULER_JITTER_SECONDS", 10, minimum=1)
HOST_HEALTH_CHECK_INTERVAL_SECONDS = _get_env_positive_int("HOST_HEALTH_CHECK_INTERVAL_SECONDS", 45, minimum=10)
HOST_HEALTH_CHECK_INITIAL_DELAY_SECONDS = _get_env_positive_int("HOST_HEALTH_CHECK_INITIAL_DELAY_SECONDS", 5, minimum=0)
HOST_HEALTH_CHECK_JITTER_SECONDS = _get_env_positive_int("HOST_HEALTH_CHECK_JITTER_SECONDS", 10, minimum=1)
HOST_HEALTH_CHECK_CONCURRENCY = _get_env_positive_int("HOST_HEALTH_CHECK_CONCURRENCY", 4, minimum=1)
HOST_HEALTH_PROBE_TIMEOUT_SECONDS = _get_env_positive_int("HOST_HEALTH_PROBE_TIMEOUT_SECONDS", 20, minimum=5)
HOST_HEALTH_PROBE_LOGIN_RETRIES = _get_env_positive_int("HOST_HEALTH_PROBE_LOGIN_RETRIES", 0, minimum=0)
HOST_SYNC_PROBE_LOGIN_RETRIES = _get_env_positive_int("HOST_SYNC_PROBE_LOGIN_RETRIES", 1, minimum=0)
HOST_HEALTH_ALERT_COOLDOWN_SECONDS = _get_env_positive_int(
    "HOST_HEALTH_ALERT_COOLDOWN_SECONDS",
    18_000,
    minimum=0,
)
HOST_SUBSCRIPTION_PROBE_TIMEOUT_SECONDS = _get_env_positive_int("HOST_SUBSCRIPTION_PROBE_TIMEOUT_SECONDS", 6, minimum=2)
HOST_SUBSCRIPTION_PROBE_ENABLED = _is_truthy(os.environ.get("HOST_SUBSCRIPTION_PROBE_ENABLED", "true"))
HOST_HEALTH_COLLECT_CLIENT_COUNT = _is_truthy(os.environ.get("HOST_HEALTH_COLLECT_CLIENT_COUNT", "0"))
HOST_ALERT_UNHEALTHY_DEBOUNCE_SECONDS = _get_env_positive_int(
    "HOST_ALERT_UNHEALTHY_DEBOUNCE_SECONDS",
    60,
    minimum=0,
)
HOST_ALERT_PRIMARY_UNHEALTHY_DEBOUNCE_SECONDS = _get_env_positive_int(
    "HOST_ALERT_PRIMARY_UNHEALTHY_DEBOUNCE_SECONDS",
    60,
    minimum=0,
)
SHOP_WATCHDOG_INTERVAL_SECONDS = _get_env_positive_int("SHOP_WATCHDOG_INTERVAL_SECONDS", 60, minimum=10)
SHOP_WATCHDOG_INITIAL_DELAY_SECONDS = _get_env_positive_int("SHOP_WATCHDOG_INITIAL_DELAY_SECONDS", 15, minimum=0)
SHOP_WATCHDOG_JITTER_SECONDS = _get_env_positive_int("SHOP_WATCHDOG_JITTER_SECONDS", 5, minimum=1)
SHOP_WATCHDOG_TRANSIENT_UNHEALTHY_THRESHOLD = _get_env_positive_int(
    "SHOP_WATCHDOG_TRANSIENT_UNHEALTHY_THRESHOLD",
    3,
    minimum=1,
)
SHOP_WATCHDOG_TRANSIENT_UNHEALTHY_MAX_SECONDS = _get_env_positive_int(
    "SHOP_WATCHDOG_TRANSIENT_UNHEALTHY_MAX_SECONDS",
    75,
    minimum=30,
)
ALERT_TELEGRAM_SEND_TIMEOUT_SECONDS = _get_env_positive_int("ALERT_TELEGRAM_SEND_TIMEOUT_SECONDS", 15, minimum=5)
ALERT_TELEGRAM_SEND_RETRIES = _get_env_positive_int("ALERT_TELEGRAM_SEND_RETRIES", 3, minimum=1)
ALERT_TELEGRAM_RETRY_BACKOFF_SECONDS = _get_env_positive_int("ALERT_TELEGRAM_RETRY_BACKOFF_SECONDS", 1, minimum=0)
SHOP_WATCHDOG_ALERT_COOLDOWN_SECONDS = 900
SYNC_PER_HOST_TIMEOUT_SECONDS = _get_env_positive_int("SYNC_PER_HOST_TIMEOUT_SECONDS", 45, minimum=10)
DELIVERY_SLA_CHECK_INTERVAL_SECONDS = _get_env_positive_int("DELIVERY_SLA_CHECK_INTERVAL_SECONDS", 3600, minimum=300)
DELIVERY_SLA_P95_THRESHOLD_SECONDS = _get_env_positive_int("DELIVERY_SLA_P95_THRESHOLD_SECONDS", 120, minimum=10)
WHITELIST_LIMIT_CHECK_INTERVAL_SECONDS = _get_env_positive_int(
    "WHITELIST_LIMIT_CHECK_INTERVAL_SECONDS",
    600,
    minimum=60,
)
WHITELIST_LIMIT_CHECK_INITIAL_DELAY_SECONDS = _get_env_positive_int(
    "WHITELIST_LIMIT_CHECK_INITIAL_DELAY_SECONDS",
    120,
    minimum=0,
)
WHITELIST_LIMIT_CHECK_JITTER_SECONDS = _get_env_positive_int(
    "WHITELIST_LIMIT_CHECK_JITTER_SECONDS",
    30,
    minimum=1,
)
# Periodically yield to the asyncio loop while applying many SQLite updates per host.
SYNC_KEYS_DB_YIELD_EVERY = _get_env_positive_int("SYNC_KEYS_DB_YIELD_EVERY", 25, minimum=1)
PANEL_SYNC_ORPHAN_SAMPLE_LIMIT = _get_env_positive_int("PANEL_SYNC_ORPHAN_SAMPLE_LIMIT", 20, minimum=0)
HOST_SYNC_ALERT_COOLDOWN_SECONDS = _get_env_positive_int("HOST_SYNC_ALERT_COOLDOWN_SECONDS", 900, minimum=0)
ENABLE_RESERVE_PANEL_SYNC = _is_truthy(os.environ.get("ENABLE_RESERVE_PANEL_SYNC", "0"))
MAINPANEL_INBOUND_DISCOVERY_ENABLED = _is_truthy(os.environ.get("MAINPANEL_INBOUND_DISCOVERY_ENABLED", "0"))
MAINPANEL_INBOUND_DISCOVERY_INTERVAL_SECONDS = _get_env_positive_int(
    "MAINPANEL_INBOUND_DISCOVERY_INTERVAL_SECONDS",
    600,
    minimum=60,
)
MAINPANEL_INBOUND_DISCOVERY_INITIAL_DELAY_SECONDS = _get_env_positive_int(
    "MAINPANEL_INBOUND_DISCOVERY_INITIAL_DELAY_SECONDS",
    30,
    minimum=0,
)
MAINPANEL_INBOUND_BACKFILL_BATCH_SIZE = _get_env_positive_int(
    "MAINPANEL_INBOUND_BACKFILL_BATCH_SIZE",
    100,
    minimum=1,
)
YOOKASSA_RECONCILE_INTERVAL_SECONDS = 120
YOOKASSA_RECONCILE_LIMIT = 80
YOOKASSA_RECONCILE_MIN_AGE_SECONDS = 90
PLATEGA_RECONCILE_INTERVAL_SECONDS = _get_env_positive_int(
    "PLATEGA_RECONCILE_INTERVAL_SECONDS",
    120,
    minimum=10,
)
PLATEGA_RECONCILE_LIMIT = _get_env_positive_int("PLATEGA_RECONCILE_LIMIT", 80, minimum=1)
PLATEGA_RECONCILE_MIN_AGE_SECONDS = _get_env_positive_int(
    "PLATEGA_RECONCILE_MIN_AGE_SECONDS",
    90,
    minimum=0,
)
PAYPEAR_RECONCILE_INTERVAL_SECONDS = _get_env_positive_int(
    "PAYPEAR_RECONCILE_INTERVAL_SECONDS",
    PLATEGA_RECONCILE_INTERVAL_SECONDS,
    minimum=10,
)
PAYPEAR_RECONCILE_LIMIT = _get_env_positive_int(
    "PAYPEAR_RECONCILE_LIMIT",
    PLATEGA_RECONCILE_LIMIT,
    minimum=1,
)
PAYPEAR_RECONCILE_MIN_AGE_SECONDS = _get_env_positive_int(
    "PAYPEAR_RECONCILE_MIN_AGE_SECONDS",
    PLATEGA_RECONCILE_MIN_AGE_SECONDS,
    minimum=0,
)
LAVA_RECONCILE_INTERVAL_SECONDS = _get_env_positive_int(
    "LAVA_RECONCILE_INTERVAL_SECONDS",
    120,
    minimum=10,
)
LAVA_RECONCILE_LIMIT = _get_env_positive_int("LAVA_RECONCILE_LIMIT", 80, minimum=1)
LAVA_RECONCILE_MIN_AGE_SECONDS = _get_env_positive_int(
    "LAVA_RECONCILE_MIN_AGE_SECONDS",
    90,
    minimum=0,
)
PAYMENT_ISSUANCE_SLA_SECONDS = _get_env_positive_int("PAYMENT_ISSUANCE_SLA_SECONDS", 900, minimum=60)
PAYMENT_ISSUANCE_AUTORETRY_ENABLED = _is_truthy(os.environ.get("PAYMENT_ISSUANCE_AUTORETRY_ENABLED", "true"))
PAYMENT_ISSUANCE_AUTORETRY_INTERVAL_SECONDS = _get_env_positive_int(
    "PAYMENT_ISSUANCE_AUTORETRY_INTERVAL_SECONDS",
    15,
    minimum=3,
)
PAYMENT_ISSUANCE_AUTORETRY_INITIAL_DELAY_SECONDS = _get_env_positive_int(
    "PAYMENT_ISSUANCE_AUTORETRY_INITIAL_DELAY_SECONDS",
    3,
    minimum=0,
)
PAYMENT_ISSUANCE_AUTORETRY_JITTER_SECONDS = _get_env_positive_int(
    "PAYMENT_ISSUANCE_AUTORETRY_JITTER_SECONDS",
    3,
    minimum=1,
)
PAYMENT_ISSUANCE_AUTORETRY_LIMIT = _get_env_positive_int(
    "PAYMENT_ISSUANCE_AUTORETRY_LIMIT",
    50,
    minimum=1,
)
PAYMENT_ISSUANCE_AUTORETRY_MAX_ATTEMPTS = _get_env_positive_int(
    "PAYMENT_ISSUANCE_AUTORETRY_MAX_ATTEMPTS",
    8,
    minimum=1,
)
PAYMENT_ISSUANCE_DAILY_CATCHUP_ENABLED = _is_truthy(os.environ.get("PAYMENT_ISSUANCE_DAILY_CATCHUP_ENABLED", "true"))
PAYMENT_ISSUANCE_DAILY_CATCHUP_INTERVAL_SECONDS = _get_env_positive_int(
    "PAYMENT_ISSUANCE_DAILY_CATCHUP_INTERVAL_SECONDS",
    86400,
    minimum=3600,
)
PAYMENT_ISSUANCE_DAILY_CATCHUP_INITIAL_DELAY_SECONDS = _get_env_positive_int(
    "PAYMENT_ISSUANCE_DAILY_CATCHUP_INITIAL_DELAY_SECONDS",
    90,
    minimum=0,
)
PAYMENT_ISSUANCE_DAILY_CATCHUP_LIMIT = _get_env_positive_int(
    "PAYMENT_ISSUANCE_DAILY_CATCHUP_LIMIT",
    300,
    minimum=1,
)
SUBSCRIPTION_NOTIFY_WINDOW_HOURS = 24
NOTIFICATION_HISTORY_RETENTION_DAYS = 120
_last_shop_watchdog_alert_at: datetime | None = None
_last_yookassa_reconcile_at: datetime | None = None
_last_platega_reconcile_at: datetime | None = None
_last_paypear_reconcile_at: datetime | None = None
_last_lava_reconcile_at: datetime | None = None
_shop_watchdog_transient_unhealthy_cycles = 0
_shop_watchdog_transient_unhealthy_since: datetime | None = None
_recent_shop_ops_alerts: dict[str, datetime] = {}
_ALERT_DEDUPE_MAX_ENTRIES = 500
_host_unhealthy_since: dict[str, datetime] = {}
_host_unhealthy_alert_sent: dict[str, bool] = {}
_HOST_UNHEALTHY_MAX_TRACKED = 200
_host_unhealthy_lock = threading.Lock()


def _gc_recent_alerts(now: datetime, dedupe_window: int) -> None:
    """Remove expired entries and enforce hard cap on _recent_shop_ops_alerts."""
    expiration_before = now - timedelta(seconds=dedupe_window)
    expired = [k for k, v in _recent_shop_ops_alerts.items() if v < expiration_before]
    for k in expired:
        del _recent_shop_ops_alerts[k]
    overflow = len(_recent_shop_ops_alerts) - _ALERT_DEDUPE_MAX_ENTRIES
    if overflow > 0:
        oldest_keys = sorted(_recent_shop_ops_alerts, key=_recent_shop_ops_alerts.get)[:overflow]
        for k in oldest_keys:
            del _recent_shop_ops_alerts[k]


# ── Loop crash isolation (TASK-13) ──────────────────────────────────
_loop_health: dict[str, dict[str, Any]] = {}
_loop_health_lock = threading.Lock()
_LOOP_MAX_BACKOFF_SECONDS = 300
_LOOP_ESCALATION_THRESHOLD = 20


def _safe_alert(error: "BotError", details: str = "", *, respect_cooldown: bool = True) -> None:
    """Fire-and-forget alert that NEVER raises."""
    try:
        send_critical_alert_nonblocking(error, details=details, respect_cooldown=respect_cooldown)
    except Exception:
        logger.exception("_safe_alert: dispatch failed silently.")


def _persistent_alert_key(raw_key: str) -> str:
    raw_key = str(raw_key or "").strip().lower()
    return re.sub(r"[^a-z0-9_.-]+", "-", raw_key).strip("-")[:120] or "unknown"


def _persistent_alert_allowed(
    *,
    prefix: str,
    raw_key: str,
    cooldown_seconds: int,
    log_label: str,
) -> bool:
    cooldown = max(0, int(cooldown_seconds))
    if cooldown <= 0:
        return True

    runtime_dir = runtime_guard.runtime_state_dir()
    key = _persistent_alert_key(raw_key)
    marker_path = runtime_dir / f"{prefix}.{key}.cooldown"
    lock_path = runtime_dir / f"{prefix}.{key}.lock"
    now = time.time()

    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        if lock_path.exists() and now - lock_path.stat().st_mtime > 30:
            lock_path.unlink(missing_ok=True)

        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError as exc:
        logger.warning("%s alert cooldown unavailable; allowing alert. path=%s error=%s", log_label, marker_path, exc)
        return True

    try:
        with os.fdopen(lock_fd, "w", encoding="utf-8") as lock_file:
            lock_file.write(str(os.getpid()))

        try:
            previous = float(marker_path.read_text(encoding="utf-8").strip() or "0")
        except (OSError, ValueError):
            previous = 0.0

        if previous > 0 and now - previous < cooldown:
            return False

        marker_path.write_text(f"{now:.6f}", encoding="utf-8")
        return True
    finally:
        lock_path.unlink(missing_ok=True)


def _host_sync_alert_allowed(host_name: str, reason: str) -> bool:
    return _persistent_alert_allowed(
        prefix="host-sync",
        raw_key=f"{host_name}:{reason}",
        cooldown_seconds=HOST_SYNC_ALERT_COOLDOWN_SECONDS,
        log_label="Host sync",
    )


def _host_health_alert_allowed(host_name: str, host_url: str | None) -> bool:
    canonical_url = str(host_url or "").strip().lower().rstrip("/")
    raw_key = canonical_url or str(host_name or "").strip().lower()
    return _persistent_alert_allowed(
        prefix="host-health",
        raw_key=raw_key,
        cooldown_seconds=HOST_HEALTH_ALERT_COOLDOWN_SECONDS,
        log_label="Host health",
    )


def _update_loop_health(name: str, *, success: bool, consecutive_crashes: int = 0) -> None:
    with _loop_health_lock:
        entry = _loop_health.setdefault(
            name,
            {
                "last_success": None,
                "consecutive_crashes": 0,
                "total_cycles": 0,
                "total_crashes": 0,
            },
        )
        if success:
            entry["last_success"] = _naive_now()
            entry["consecutive_crashes"] = 0
            entry["total_cycles"] = entry.get("total_cycles", 0) + 1
        else:
            entry["consecutive_crashes"] = consecutive_crashes
            entry["total_crashes"] = entry.get("total_crashes", 0) + 1


def get_loop_health() -> dict[str, dict[str, Any]]:
    """Snapshot of all tracked loop health states (for /internal/runtime/status)."""
    with _loop_health_lock:
        snapshot = {k: dict(v) for k, v in _loop_health.items()}

    for entry in snapshot.values():
        last_success = entry.get("last_success")
        if isinstance(last_success, datetime):
            entry["last_success"] = last_success.isoformat()
    return snapshot


def _crash_backoff_seconds(consecutive: int) -> float:
    """Exponential backoff capped at _LOOP_MAX_BACKOFF_SECONDS."""
    return min(_LOOP_MAX_BACKOFF_SECONDS, 2 ** min(consecutive, 8))


def indestructible_loop(
    *,
    name: str,
    interval_fn: Callable[[], float],
    initial_delay_fn: Callable[[], float] | None = None,
):
    """Decorator that wraps a single-cycle async function in a crash-proof while-True loop.

    The decorated function represents ONE iteration of the loop.
    The decorator handles: initial delay, crash isolation, exponential
    backoff, CancelledError propagation, alert-safe error reporting, and
    loop health tracking.
    """

    def decorator(fn: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> None:
            if initial_delay_fn is not None:
                try:
                    await asyncio.sleep(initial_delay_fn())
                except asyncio.CancelledError:
                    logger.info("%s: cancelled during initial delay", name)
                    return

            consecutive_crashes = 0
            while True:
                try:
                    await fn(*args, **kwargs)
                    consecutive_crashes = 0
                    _update_loop_health(name, success=True)
                except asyncio.CancelledError:
                    logger.info("%s: cancelled, shutting down", name)
                    return
                except Exception as exc:
                    consecutive_crashes += 1
                    _update_loop_health(name, success=False, consecutive_crashes=consecutive_crashes)
                    logger.critical(
                        "%s: crash #%d: %s",
                        name,
                        consecutive_crashes,
                        exc,
                        exc_info=True,
                    )
                    if consecutive_crashes >= _LOOP_ESCALATION_THRESHOLD:
                        _safe_alert(
                            BotError.SCHEDULER_CRASH,
                            f"loop={name} crash_count={consecutive_crashes} ESCALATION: manual check required\n{exc}",
                        )
                    else:
                        _safe_alert(
                            BotError.SCHEDULER_CRASH,
                            f"loop={name} crash_count={consecutive_crashes}\n{exc}",
                            respect_cooldown=True,
                        )
                    backoff = _crash_backoff_seconds(consecutive_crashes)
                    try:
                        await asyncio.sleep(backoff)
                    except asyncio.CancelledError:
                        logger.info("%s: cancelled during crash backoff", name)
                        return
                    continue

                try:
                    await asyncio.sleep(interval_fn())
                except asyncio.CancelledError:
                    logger.info("%s: cancelled during interval sleep", name)
                    return

        return wrapper

    return decorator


# ────────────────────────────────────────────────────────────────────

EXPIRY_DELETE_GRACE_DAYS = 5


def _is_whitelist_subscription_key(key_data: dict[str, Any]) -> bool:
    return xui_api._mainpanel_resolve_key_tier(key_data) in {
        xui_api.SUBSCRIPTION_TIER_STANDARD,
        xui_api.SUBSCRIPTION_TIER_SUPER,
    }


def _expiry_delete_grace_days(key_data: dict[str, Any]) -> int:
    # WHITELIST expiry is a renewable traffic-cycle deadline, not the paid
    # access boundary. Both paid tiers rely on the local paid expiry and must
    # be revoked before native traffic resets can revive expired access.
    return 0 if _is_whitelist_subscription_key(key_data) else EXPIRY_DELETE_GRACE_DAYS


_SHOP_OPS_LEVELS = {"info", "warning", "critical", "success"}
_SHOP_OPS_LEVEL_LABELS = {
    "info": "Информация",
    "warning": "Внимание",
    "critical": "Критично",
    "success": "Восстановлено",
}
_SHOP_OPS_LEVEL_ICONS = {
    "critical": "🔴",
    "warning": "🟡",
    "info": "🟡",
    "success": "🟢",
}
_HOST_NETWORK_ERROR_CODES = {
    "host_timeout",
    "host_connection_refused",
    "host_connection_reset",
    "host_dns_failed",
    "host_ssl_error",
    "host_invalid_http_response",
    "host_circuit_open",
    "host_subscription_unreachable",
}


def _build_retryable_failure_summary(rows: list[dict], *, max_items: int = 5) -> str:
    if not rows:
        return "0"
    items: list[str] = []
    for row in rows[: max(1, int(max_items))]:
        payment_id = str(row.get("payment_id") or "unknown")
        retry_count = int(row.get("retry_count") or 0)
        items.append(f"{payment_id} (retry={retry_count})")
    return ", ".join(items)


def _build_retryable_failure_verbose_tail(rows: list[dict], *, max_items: int = 3) -> str:
    if not rows:
        return ""
    lines: list[str] = []
    for row in rows[: max(1, int(max_items))]:
        metadata = row.get("metadata")
        if isinstance(metadata, str):
            try:
                parsed = json.loads(metadata)
            except json.JSONDecodeError:
                parsed = {}
            metadata = parsed if isinstance(parsed, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        payment_id = str(row.get("payment_id") or metadata.get("external_payment_id") or "unknown")
        user_id = str(row.get("user_id") or metadata.get("user_id") or "unknown")
        host_name = str(metadata.get("host_name") or "unknown")
        plan_name = str(metadata.get("plan_name") or "—")
        months = str(metadata.get("months") or "—")
        payment_method = str(row.get("payment_method") or metadata.get("payment_method") or "—")
        paid_at = str(row.get("confirmed_at") or metadata.get("paid_at") or row.get("created_date") or "—")
        retry_count = int(row.get("retry_count") or 0)
        lines.extend(
            [
                f"ID платежа = {payment_id}",
                f"ID пользователя = {user_id}",
                f"Хост = {host_name}",
                f"Тариф = {plan_name}",
                f"Срок (мес) = {months}",
                f"Метод оплаты = {payment_method}",
                f"Дата оплаты = {paid_at}",
                f"Попытка = {retry_count}",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def _claim_retryable_sla_breaches(rows: list[dict]) -> list[dict]:
    claimed: list[dict] = []
    for row in rows:
        payment_id = str(row.get("payment_id") or "").strip()
        if not payment_id:
            continue
        if database.claim_retryable_failure_sla_alert(payment_id):
            claimed.append(row)
    return claimed


def _emit_issuance_sla_alert_if_needed() -> int:
    sla_breaches = _claim_retryable_sla_breaches(
        database.get_retryable_failures_over_sla(
            sla_seconds=PAYMENT_ISSUANCE_SLA_SECONDS,
            limit=20,
        )
    )
    if not sla_breaches:
        return 0

    verbose_tail = _build_retryable_failure_verbose_tail(sla_breaches)
    send_critical_alert_nonblocking(
        BotError.PAYMENT_KEY_ISSUE_FAILED,
        details=(
            f"source={BOT_SOURCE} reason=issuance_sla_breached "
            f"count={len(sla_breaches)} "
            f"sla_seconds={PAYMENT_ISSUANCE_SLA_SECONDS} "
            f"payment_ids={_build_retryable_failure_summary(sla_breaches)}"
            + (f"\n{verbose_tail}" if verbose_tail else "")
        ),
        respect_cooldown=True,
    )
    return len(sla_breaches)


def _parse_alert_recipients(raw_recipients: str) -> list[str]:
    normalized = (raw_recipients or "").replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _dedupe_non_empty_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _resolve_alert_bot_tokens() -> tuple[list[str], bool]:
    env_alert_bot_token = (os.environ.get("ALERT_TELEGRAM_BOT_TOKEN") or "").strip()
    db_alert_bot_token = (database.get_setting("alert_telegram_bot_token") or "").strip()
    runtime_main_token = (database.get_setting("telegram_bot_token") or "").strip()
    runtime_reserve_token = (database.get_setting("reserve_bot_token") or "").strip()
    env_bot_token = (os.environ.get("BOT_TOKEN") or "").strip()

    tokens = _dedupe_non_empty_values(
        [
            env_alert_bot_token,
            db_alert_bot_token,
            runtime_main_token,
            runtime_reserve_token,
            env_bot_token,
        ]
    )
    uses_fallback = bool(tokens) and not (env_alert_bot_token or db_alert_bot_token)
    return tokens, uses_fallback


def _resolve_alert_chat_ids(*, watchdog_only: bool = False) -> tuple[list[str], bool]:
    env_alert_chat_ids = (os.environ.get("ALERT_TELEGRAM_CHAT_ID") or "").strip()
    db_alert_chat_ids = (database.get_setting("alert_telegram_chat_id") or "").strip()
    fallback_admin_chat_id = (database.get_setting("admin_telegram_id") or "").strip()

    raw_chat_ids = env_alert_chat_ids or db_alert_chat_ids
    watchdog_chat_ids = _parse_alert_recipients(raw_chat_ids)
    admin_chat_ids = _parse_alert_recipients(fallback_admin_chat_id)

    if watchdog_chat_ids:
        # Explicit alert chat is the routing source of truth. Do not also send
        # ops alerts to admin_telegram_id: that setting is used for permissions
        # and legacy fallback only.
        return _dedupe_non_empty_values(watchdog_chat_ids), False

    _ = watchdog_only
    return _dedupe_non_empty_values(admin_chat_ids), bool(admin_chat_ids)


def _should_retry_telegram_send(status_code: int | None) -> bool:
    if status_code is None:
        return True
    if status_code == 429:
        return True
    return status_code >= 500


_ALERT_TELEGRAM_MAX_TOTAL_RETRY_SECONDS = 5.0


def _telegram_retry_delay_seconds(attempt_number: int, *, elapsed: float = 0.0) -> float:
    base = max(0, ALERT_TELEGRAM_RETRY_BACKOFF_SECONDS)
    if base <= 0:
        return 0.0
    delay = float(base * (2 ** max(0, attempt_number - 1)))
    remaining = _ALERT_TELEGRAM_MAX_TOTAL_RETRY_SECONDS - elapsed
    if remaining <= 0:
        return 0.0
    return min(delay, remaining)


def _send_telegram_payload(
    *,
    api_url: str,
    payload: dict[str, object],
) -> tuple[bool, int | None, str]:
    request_data = json.dumps(payload).encode("utf-8")
    req = urlrequest.Request(  # noqa: S310
        api_url,
        data=request_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlrequest.urlopen(req, timeout=ALERT_TELEGRAM_SEND_TIMEOUT_SECONDS) as resp:  # noqa: S310
            body_raw = resp.read().decode("utf-8", errors="ignore")
            status = int(getattr(resp, "status", 0) or 0)
            if status != 200:
                return False, status, f"http_status={status} body={body_raw}"
            parsed = json.loads(body_raw) if body_raw else {}
            if isinstance(parsed, dict) and parsed.get("ok") is True:
                return True, status, ""
            return False, status, f"telegram_not_ok body={body_raw}"
    except urlerror.HTTPError as e:
        body_raw = ""
        try:
            body_raw = (e.read() or b"").decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            body_raw = ""
        return False, int(getattr(e, "code", 0) or 0), f"http_error={getattr(e, 'code', 'unknown')} body={body_raw}"
    except urlerror.URLError as e:
        return False, None, f"url_error={e}"
    except Exception as e:  # noqa: BLE001
        return False, None, f"unexpected_error={e}"


def _parse_alert_smtp_port(raw_port: str | None, default: int = 587) -> int:
    try:
        parsed = int(str(raw_port or "").strip() or str(default))
    except (TypeError, ValueError):
        logger.warning("Invalid ALERT_SMTP_PORT=%r, fallback to %d", raw_port, default)
        return default
    if parsed <= 0:
        logger.warning("Invalid ALERT_SMTP_PORT=%r, fallback to %d", raw_port, default)
        return default
    return parsed


def _normalize_shop_ops_level(level: str | None) -> str:
    normalized = str(level or "").strip().lower()
    if normalized in _SHOP_OPS_LEVELS:
        return normalized
    return "critical"


def _format_shop_ops_meta_line(level: str, *, source_label: str | None = None) -> str:
    normalized_level = _normalize_shop_ops_level(level)
    level_label = _SHOP_OPS_LEVEL_LABELS.get(normalized_level, normalized_level.title())
    now_label = datetime.now(_MOSCOW_TZ).strftime("%d.%m.%Y %H:%M")
    parts = [level_label]
    if source_label:
        parts.append(source_label)
    parts.append(now_label)
    return " · ".join(parts)


def _format_shop_watchdog_email_message(
    reason: str,
    *,
    title: str,
    level: str,
    source_label: str | None = None,
) -> str:
    raw_reason = str(reason or "").strip() or "unknown"
    raw_title = str(title or "").strip() or "shop watchdog alert"
    meta_line = _format_shop_ops_meta_line(level, source_label=source_label)

    return f"{raw_title}\n{meta_line}\n\n{raw_reason}"


def _format_shop_watchdog_telegram_message(
    reason: str,
    *,
    title: str,
    level: str,
    source_label: str | None = None,
) -> str:
    raw_reason = str(reason or "").strip() or "unknown"
    raw_title = str(title or "").strip() or "shop watchdog alert"
    normalized_level = _normalize_shop_ops_level(level)
    meta_line = _format_shop_ops_meta_line(normalized_level, source_label=source_label)
    level_icon = _SHOP_OPS_LEVEL_ICONS.get(normalized_level, "🔴")
    rendered_reason = _render_shop_ops_reason_for_telegram(raw_reason)

    return f"{level_icon} *{_escape_markdown_v2(raw_title)}*\n_{_escape_markdown_v2(meta_line)}_\n{rendered_reason}"


def _split_copyable_field_line(raw_line: str) -> tuple[str, str] | None:
    line = str(raw_line or "").strip()
    if not line:
        return None
    if line.startswith("- "):
        line = line[2:].strip()
    if " = " not in line:
        return None
    label, value = line.split(" = ", 1)
    normalized_label = label.strip()
    normalized_value = value.strip()
    if not normalized_label or not normalized_value:
        return None
    return normalized_label, normalized_value


def _escape_markdown_v2(text: str) -> str:
    escaped = str(text or "")
    for char in ("\\", "_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"):
        escaped = escaped.replace(char, f"\\{char}")
    return escaped


def _sanitize_markdown_code_value(text: str) -> str:
    # Prevent accidental closing of fenced code blocks.
    return str(text or "").replace("```", "` ` `")


_REASON_NO_MONO_LABELS = {
    "Причина",
    "Источник",
    "Источник платежа",
    "Текущий инстанс",
    "Провайдер",
    "Метод оплаты",
    "Сценарий",
}


def _render_shop_ops_reason_for_telegram(raw_reason: str) -> str:
    rendered_lines: list[str] = []
    for line in str(raw_reason or "").splitlines():
        stripped = line.strip()
        if not stripped:
            rendered_lines.append("")
            continue

        copyable_pair = _split_copyable_field_line(stripped)
        if copyable_pair:
            label, value = copyable_pair
            if label in _REASON_NO_MONO_LABELS:
                rendered_lines.append(f"*{_escape_markdown_v2(label)}:* {_escape_markdown_v2(value)}")
            else:
                s_value = value.replace("\\", "\\\\").replace("`", "\\`")
                rendered_lines.append(f"*{_escape_markdown_v2(label)}:* `{s_value}`")
            continue

        if stripped.lower().startswith("что сделать:"):
            _, _, hint = stripped.partition(":")
            hint_text = hint.strip()
            if hint_text:
                rendered_lines.append(f"*Что сделать:* {_escape_markdown_v2(hint_text)}")
            else:
                rendered_lines.append("*Что сделать:*")
            continue

        rendered_lines.append(_escape_markdown_v2(stripped))

    rendered_text = "\n".join(rendered_lines).strip()
    return rendered_text or "unknown"


def _send_shop_watchdog_email_alert(
    reason: str,
    *,
    title: str = "strict startup failure",
    level: str = "critical",
    source_label: str | None = None,
) -> bool:
    """
    Send watchdog alert email for strict startup failures.
    Uses env vars to avoid storing SMTP credentials in DB:
      ALERT_SMTP_HOST, ALERT_SMTP_PORT, ALERT_SMTP_USER, ALERT_SMTP_PASSWORD,
      ALERT_SMTP_FROM, ALERT_SMTP_TO, ALERT_SMTP_USE_TLS
    """
    smtp_host = (os.environ.get("ALERT_SMTP_HOST") or "").strip()
    smtp_user = (os.environ.get("ALERT_SMTP_USER") or "").strip()
    smtp_password = (os.environ.get("ALERT_SMTP_PASSWORD") or "").strip()
    smtp_from = (os.environ.get("ALERT_SMTP_FROM") or smtp_user).strip()
    smtp_to = (os.environ.get("ALERT_SMTP_TO") or database.get_setting("receipt_email") or "").strip()
    smtp_recipients = _parse_alert_recipients(smtp_to)
    smtp_port = _parse_alert_smtp_port(os.environ.get("ALERT_SMTP_PORT", "587"))
    smtp_use_tls = _is_truthy(os.environ.get("ALERT_SMTP_USE_TLS", "true"))
    smtp_use_ssl = _is_truthy(os.environ.get("ALERT_SMTP_USE_SSL", "false"))

    if not all([smtp_host, smtp_from]) or not smtp_recipients:
        logger.error(
            "Shop watchdog alert email was not sent: SMTP is not configured. "
            "Set ALERT_SMTP_HOST/ALERT_SMTP_TO (or receipt_email) and sender credentials."
        )
        return False

    normalized_level = _normalize_shop_ops_level(level)
    message = EmailMessage()
    message["Subject"] = f"[PEPEL BOT][{normalized_level}] {title}"
    message["From"] = smtp_from
    message["To"] = ", ".join(smtp_recipients)
    message.set_content(
        _format_shop_watchdog_email_message(
            reason,
            title=title,
            level=normalized_level,
            source_label=source_label,
        )
    )

    try:
        smtp_client = smtplib.SMTP_SSL if smtp_use_ssl else smtplib.SMTP
        with smtp_client(smtp_host, smtp_port, timeout=15) as server:
            if smtp_use_tls and not smtp_use_ssl:
                server.starttls()
            if smtp_user and smtp_password:
                server.login(smtp_user, smtp_password)
            server.send_message(message, to_addrs=smtp_recipients)
    except Exception:
        logger.exception("Failed to send shop watchdog alert email.")
        return False
    logger.info("Shop watchdog alert email sent to %s.", ", ".join(smtp_recipients))
    return True


def _send_shop_watchdog_telegram_alert(
    reason: str,
    *,
    title: str = "strict startup failure",
    level: str = "critical",
    source_label: str | None = None,
    watchdog_only: bool = False,
) -> bool:
    """
    Send watchdog alert to Telegram chat/channel via Bot API.

    Env vars:
      ALERT_TELEGRAM_BOT_TOKEN
      ALERT_TELEGRAM_CHAT_ID (one or multiple IDs separated by comma/semicolon)
      ALERT_TELEGRAM_THREAD_ID (optional, forum topic id)
    """
    bot_tokens, used_fallback_token = _resolve_alert_bot_tokens()
    chat_ids, used_fallback_chat = _resolve_alert_chat_ids(watchdog_only=watchdog_only)
    thread_id_raw = (
        os.environ.get("ALERT_TELEGRAM_THREAD_ID") or database.get_setting("alert_telegram_thread_id") or ""
    ).strip()

    if not bot_tokens or not chat_ids:
        logger.error(
            "Shop watchdog Telegram alert was not sent: set ALERT_TELEGRAM_BOT_TOKEN and ALERT_TELEGRAM_CHAT_ID."
        )
        return False

    if used_fallback_token:
        logger.warning(
            "Shop watchdog Telegram alert uses fallback token (telegram_bot_token/reserve_bot_token/BOT_TOKEN)."
        )
    if used_fallback_chat:
        logger.warning("Shop watchdog Telegram alert uses fallback chat id from admin_telegram_id.")

    message_thread_id: int | None = None
    if thread_id_raw:
        try:
            message_thread_id = int(thread_id_raw)
        except ValueError:
            logger.warning("Invalid ALERT_TELEGRAM_THREAD_ID=%r. Ignoring.", thread_id_raw)

    rendered_text = _format_shop_watchdog_telegram_message(
        reason,
        title=title,
        level=level,
        source_label=source_label,
    )

    max_attempts = min(max(1, ALERT_TELEGRAM_SEND_RETRIES), 3)
    for token_index, bot_token in enumerate(bot_tokens, start=1):
        api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        token_success_count = 0
        for chat_id in chat_ids:
            payload: dict[str, object] = {
                "chat_id": chat_id,
                "text": rendered_text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            }
            if message_thread_id is not None and str(chat_id).strip().startswith("-100"):
                payload["message_thread_id"] = message_thread_id

            delivered = False
            last_status: int | None = None
            last_error = ""
            retry_elapsed = 0.0
            for attempt in range(1, max_attempts + 1):
                delivered, last_status, last_error = _send_telegram_payload(
                    api_url=api_url,
                    payload=payload,
                )
                if delivered:
                    token_success_count += 1
                    break

                should_retry = attempt < max_attempts and _should_retry_telegram_send(last_status)
                if should_retry:
                    delay = _telegram_retry_delay_seconds(attempt, elapsed=retry_elapsed)
                    remaining = _ALERT_TELEGRAM_MAX_TOTAL_RETRY_SECONDS - retry_elapsed
                    if remaining <= 0:
                        break
                    if delay > 0:
                        time.sleep(delay)
                        retry_elapsed += delay
                else:
                    break

            if not delivered:
                logger.error(
                    "Failed to send shop watchdog Telegram alert to chat_id=%s token_index=%s/%s status=%s error=%s",
                    chat_id,
                    token_index,
                    len(bot_tokens),
                    last_status,
                    last_error,
                )

        if token_success_count > 0:
            logger.info(
                "Shop watchdog Telegram alert sent to %d/%d chat(s).",
                token_success_count,
                len(chat_ids),
            )
            if token_index > 1:
                logger.warning(
                    "Shop watchdog Telegram alert required fallback token index=%d.",
                    token_index,
                )
            return True

        if token_index < len(bot_tokens):
            logger.warning(
                "Shop watchdog Telegram alert token index=%d failed for all chats. Trying next token.",
                token_index,
            )

    return False


def send_shop_ops_alert(
    *,
    title: str,
    reason: str,
    level: str = "critical",
    source_label: str | None = None,
    manual_start_required: bool = False,
    respect_cooldown: bool = False,
    now: datetime | None = None,
    dedupe_key: str | None = None,
    dedupe_window_seconds: int = 120,
    watchdog_only: bool = False,
) -> bool:
    """
    Send Shop operational alert to all configured channels (email + Telegram).
    """
    global _last_shop_watchdog_alert_at  # noqa: PLW0603 - scheduler cooldown is module-level runtime state.

    alert_reason = str(reason or "").strip() or "unknown"
    alert_now = now or _naive_now()

    dedupe_window = max(30, int(dedupe_window_seconds))
    if respect_cooldown:
        dedupe_window = max(dedupe_window, SHOP_WATCHDOG_ALERT_COOLDOWN_SECONDS)
    _gc_recent_alerts(alert_now, dedupe_window)

    if dedupe_key:
        previous_time = _recent_shop_ops_alerts.get(dedupe_key)
        if previous_time and (alert_now - previous_time).total_seconds() < dedupe_window:
            logger.info("Shop ops alert suppressed by dedupe key: %s", dedupe_key)
            return False

    if respect_cooldown and dedupe_key is None:
        in_cooldown = (
            _last_shop_watchdog_alert_at is not None
            and (alert_now - _last_shop_watchdog_alert_at).total_seconds() < SHOP_WATCHDOG_ALERT_COOLDOWN_SECONDS
        )
        if in_cooldown:
            return False

    if manual_start_required and "Запустите Shop-бота вручную" not in alert_reason:
        alert_reason = f"{alert_reason}\n\nЧто сделать: Запустите Shop-бота вручную из веб-панели."

    email_sent = _send_shop_watchdog_email_alert(
        alert_reason,
        title=title,
        level=level,
        source_label=source_label,
    )
    telegram_sent = _send_shop_watchdog_telegram_alert(
        alert_reason,
        title=title,
        level=level,
        source_label=source_label,
        watchdog_only=watchdog_only,
    )
    sent = email_sent or telegram_sent

    if not sent:
        logger.critical(
            "Shop ops alert delivery failed. title=%s level=%s source=%s",
            title,
            level,
            source_label or "-",
        )

    if sent and respect_cooldown and dedupe_key is None:
        _last_shop_watchdog_alert_at = alert_now
    if sent and dedupe_key:
        _recent_shop_ops_alerts[dedupe_key] = alert_now
    return sent


def _host_error_status(error_code: str | None) -> str:
    return "unavailable" if str(error_code or "").strip().lower() in _HOST_NETWORK_ERROR_CODES else "degraded"


def _host_error_alert(error_code: str | None, error_message: str | None) -> BotError:
    normalized = str(error_code or "").strip().lower()
    if normalized == "host_login_failed":
        return BotError.HOST_LOGIN_FAILED
    if normalized == "host_inbound_missing":
        return BotError.HOST_INBOUND_MISSING
    if normalized == "host_dns_failed":
        return BotError.DNS_RESOLUTION_FAILED
    if normalized == "host_ssl_error":
        return BotError.SSL_CERTIFICATE_ERROR
    if normalized in _HOST_NETWORK_ERROR_CODES:
        return BotError.HOST_UNREACHABLE
    return classify_error(str(error_message or ""))


def _is_unhealthy_host_status(status: str | None) -> bool:
    from shop_bot.domain.host_health import is_unhealthy

    return is_unhealthy(status)


def _is_primary_host_for_mandatory_alert(host_name: str | None) -> bool:
    normalized_host_name = str(host_name or "").strip().lower()
    if not normalized_host_name:
        return False
    primary_host_name = str(database.get_setting("super_key_host_name") or "").strip().lower()
    if not primary_host_name:
        return False
    return normalized_host_name == primary_host_name


def _host_unhealthy_alert_threshold_seconds(*, is_primary_host: bool) -> int:
    if is_primary_host:
        return max(0, int(HOST_ALERT_PRIMARY_UNHEALTHY_DEBOUNCE_SECONDS))
    return max(0, int(HOST_ALERT_UNHEALTHY_DEBOUNCE_SECONDS))


def _register_host_unhealthy_observation(
    *,
    host_name: str,
    was_unhealthy: bool,
    now: datetime,
    threshold_seconds: int,
) -> tuple[bool, float]:
    """Track unhealthy streak per host and decide whether alert should fire now."""
    normalized_host = str(host_name or "").strip().lower()
    if not normalized_host:
        return False, 0.0

    with _host_unhealthy_lock:
        if len(_host_unhealthy_since) > _HOST_UNHEALTHY_MAX_TRACKED:
            oldest = min(_host_unhealthy_since, key=_host_unhealthy_since.get)
            _host_unhealthy_since.pop(oldest, None)
            _host_unhealthy_alert_sent.pop(oldest, None)

        started_at = _host_unhealthy_since.get(normalized_host)
        already_alerted = bool(_host_unhealthy_alert_sent.get(normalized_host, False))

        if started_at is None or not was_unhealthy:
            started_at = now
            _host_unhealthy_since[normalized_host] = started_at
            _host_unhealthy_alert_sent[normalized_host] = False
            already_alerted = False

        elapsed_seconds = max(0.0, (now - started_at).total_seconds())
        should_alert_now = (elapsed_seconds >= float(max(0, threshold_seconds))) and (not already_alerted)

    return should_alert_now, elapsed_seconds


def _mark_host_unhealthy_alert_sent(host_name: str) -> None:
    normalized_host = str(host_name or "").strip().lower()
    if not normalized_host:
        return

    with _host_unhealthy_lock:
        _host_unhealthy_alert_sent[normalized_host] = True


def _clear_host_unhealthy_tracking(host_name: str) -> bool:
    """Clear unhealthy streak tracking and report if outage alert was sent."""
    normalized_host = str(host_name or "").strip().lower()
    if not normalized_host:
        return False

    with _host_unhealthy_lock:
        was_alerted = bool(_host_unhealthy_alert_sent.pop(normalized_host, False))
        _host_unhealthy_since.pop(normalized_host, None)
    return was_alerted


_MIN_LOOP_SLEEP_SECONDS = 1.0


def _sleep_seconds_with_jitter(base_seconds: int, jitter_seconds: int) -> float:
    base = max(0, int(base_seconds))
    jitter = max(0, int(jitter_seconds))
    raw = float(base) + (random.uniform(0, float(jitter)) if jitter > 0 else 0.0)
    if raw < _MIN_LOOP_SLEEP_SECONDS:
        logger.warning(
            "Sleep clamped from %.2f to %.2f to prevent busy loop",
            raw,
            _MIN_LOOP_SLEEP_SECONDS,
        )
        return _MIN_LOOP_SLEEP_SECONDS
    return raw


def _extract_clients_count_from_login_result(login_result: xui_api.HostLoginResult) -> int | None:
    if not login_result.inbound:
        return None
    try:
        settings = getattr(login_result.inbound, "settings", None)
        if not settings:
            return 0
        clients = settings.clients or []
        return len(clients)
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to read clients_count from panel probe: %s", e)
        return None


def _panel_clients_count_in_probe_worker(login_result: xui_api.HostLoginResult) -> int | None:
    """
    Full client list for load/health snapshot. Must run only on a worker thread
    (inside _probe_host_health while scheduled via run_in_executor), never on the asyncio loop.
    """
    if not HOST_HEALTH_COLLECT_CLIENT_COUNT:
        return None
    if not login_result.api or not login_result.inbound:
        return None
    try:
        full = login_result.api.inbound.get_by_id(login_result.inbound.id)
        if full and getattr(full, "settings", None) is not None:
            return len(full.settings.clients or [])
    except Exception as e:  # noqa: BLE001
        logger.warning("Host probe: inbound.get_by_id for client count failed: %s", e)
    return _extract_clients_count_from_login_result(login_result)


def _probe_host_subscription_endpoint(host: dict) -> tuple[bool, str | None, str | None]:
    if not HOST_SUBSCRIPTION_PROBE_ENABLED:
        return True, None, None

    subscription_url = str(host.get("host_subscription_base_url") or "").strip()
    if not subscription_url:
        return True, None, None

    req = urlrequest.Request(  # noqa: S310
        subscription_url,
        headers={"User-Agent": "subscription-health-monitor/1.0"},
        method="GET",
    )

    try:
        with urlrequest.urlopen(req, timeout=HOST_SUBSCRIPTION_PROBE_TIMEOUT_SECONDS) as resp:  # noqa: S310
            status = int(getattr(resp, "status", 0) or 0)
            # 2xx-4xx means endpoint is reachable on network/TLS level.
            if 200 <= status < 500:
                return True, None, None
            return (
                False,
                "host_subscription_bad_status",
                f"Subscription endpoint returned HTTP {status}: {subscription_url}",
            )
    except urlerror.HTTPError as e:
        # HTTPError still means endpoint is reachable. 5xx is service-side failure.
        if 400 <= int(getattr(e, "code", 0) or 0) < 500:
            return True, None, None
        return (
            False,
            "host_subscription_http_error",
            f"Subscription endpoint HTTP error {getattr(e, 'code', 'unknown')}: {subscription_url}",
        )
    except Exception as e:  # noqa: BLE001
        return (
            False,
            "host_subscription_unreachable",
            f"Subscription endpoint is unreachable: {subscription_url} ({e})",
        )


def _send_host_unhealthy_alert(
    *,
    host_name: str,
    host_url: str,
    error: BotError,
    details: str,
    reason: str | None,
    respect_cooldown: bool,
) -> bool:
    if not _host_health_alert_allowed(host_name, host_url):
        logger.info(
            "Host health monitor: persistent alert cooldown active for host='%s' url='%s' reason=%s cooldown=%ss",
            host_name,
            host_url,
            reason or "-",
            HOST_HEALTH_ALERT_COOLDOWN_SECONDS,
        )
        return False

    sent = send_critical_alert_nonblocking(
        error,
        details=details,
        respect_cooldown=respect_cooldown,
    )
    if sent:
        _mark_host_unhealthy_alert_sent(host_name)
    return sent


def _probe_host_health(
    host: dict,
    *,
    respect_cooldown: bool = True,
    login_retries: int | None = None,
) -> tuple[xui_api.HostLoginResult, str, int | None]:
    host_name = host["host_name"]
    host_url = host["host_url"]
    is_primary_host = _is_primary_host_for_mandatory_alert(host_name)
    previous_status = str(host.get("status") or "").strip().lower()
    previous_raw_status = str(host.get("raw_status") or "").strip().lower()
    previous_error = str(host.get("last_error") or "").strip()
    # Probe workers can race with other monitor loops. Re-read latest persisted state,
    # so we don't lose a "recovered" alert when the incoming host snapshot is stale.
    fresh_host = database.get_host(host_name)
    if fresh_host:
        previous_status = str(fresh_host.get("status") or previous_status).strip().lower()
        previous_raw_status = str(fresh_host.get("raw_status") or previous_raw_status).strip().lower()
        previous_error = str(fresh_host.get("last_error") or previous_error).strip()
    if not previous_raw_status:
        previous_raw_status = previous_status
    previous_status_for_compare = previous_raw_status if previous_raw_status != "unknown" else previous_status
    was_unhealthy = _is_unhealthy_host_status(previous_status_for_compare)
    unhealthy_alert_threshold_seconds = _host_unhealthy_alert_threshold_seconds(is_primary_host=is_primary_host)

    retries = xui_api.XUI_API_LOGIN_RETRIES if login_retries is None else max(0, int(login_retries))
    login_result = xui_api.login_to_host_detailed(
        host_url=host_url,
        username=host["host_username"],
        password=host["host_pass"],
        inbound_id=host["host_inbound_id"],
        api_token=xui_api._resolve_host_api_token(host),
        retries=retries,
        prefer_raw_inbound_lookup=True,
        fallback_to_lightweight_inbound_lookup=True,
        fallback_to_full_inbound_lookup=False,
        allow_minimal_inbound_on_lookup_failure=True,
    )

    if login_result.api and login_result.inbound:
        subscription_ok, subscription_error_code, subscription_error_message = _probe_host_subscription_endpoint(host)
        if not subscription_ok:
            failed_status = _host_error_status(subscription_error_code)
            error_message = subscription_error_message or "Subscription endpoint probe failed."
            database.update_host_status(host_name, failed_status, error_message)
            should_alert_now, unhealthy_elapsed_seconds = _register_host_unhealthy_observation(
                host_name=host_name,
                was_unhealthy=was_unhealthy,
                now=_naive_now(),
                threshold_seconds=unhealthy_alert_threshold_seconds,
            )
            if should_alert_now:
                _send_host_unhealthy_alert(
                    host_name=host_name,
                    host_url=host_url,
                    error=_host_error_alert(subscription_error_code, error_message),
                    details=(
                        f"source={BOT_SOURCE} "
                        f"host={host_name} "
                        f"status={failed_status} "
                        f"url={host_url} "
                        f"outage_seconds={int(unhealthy_elapsed_seconds)} "
                        f"reason={subscription_error_code} "
                        f"\nОшибка: {error_message}"
                    ),
                    reason=subscription_error_code,
                    respect_cooldown=(False if is_primary_host else respect_cooldown),
                )
            else:
                logger.info(
                    "Host health monitor: alert debounce active for host='%s' status=%s elapsed=%.1fs threshold=%ss",
                    host_name,
                    failed_status,
                    unhealthy_elapsed_seconds,
                    unhealthy_alert_threshold_seconds,
                )
            return login_result, failed_status, None

        clients_count = _panel_clients_count_in_probe_worker(login_result)
        database.update_host_status(host_name, "healthy")
        unhealthy_alert_was_sent = _clear_host_unhealthy_tracking(host_name)
        if was_unhealthy and unhealthy_alert_was_sent:
            logger.info(
                "Host health monitor: host '%s' recovered from status='%s'.",
                host_name,
                previous_status_for_compare,
            )
            send_critical_alert_nonblocking(
                BotError.HOST_RECOVERED,
                details=(
                    f"source={BOT_SOURCE} "
                    f"host={host_name} "
                    f"status=healthy "
                    f"previous_status={previous_status_for_compare} "
                    f"url={host_url} "
                    "reason=host_recovered"
                ),
                respect_cooldown=False,
            )
        return login_result, "healthy", clients_count

    error_code = login_result.error_code or "host_login_exception"
    error_message = login_result.error_message or f"Login or inbound lookup failed for host '{host_name}' ({host_url})"
    failed_status = _host_error_status(error_code)
    database.update_host_status(host_name, failed_status, error_message)

    should_alert_now, unhealthy_elapsed_seconds = _register_host_unhealthy_observation(
        host_name=host_name,
        was_unhealthy=was_unhealthy,
        now=_naive_now(),
        threshold_seconds=unhealthy_alert_threshold_seconds,
    )
    if should_alert_now:
        _send_host_unhealthy_alert(
            host_name=host_name,
            host_url=host_url,
            error=_host_error_alert(error_code, error_message),
            details=(
                f"source={BOT_SOURCE} "
                f"host={host_name} "
                f"status={failed_status} "
                f"url={host_url} "
                f"outage_seconds={int(unhealthy_elapsed_seconds)} "
                f"reason={error_code} "
                f"\nОшибка: {error_message}"
            ),
            reason=error_code,
            respect_cooldown=(False if is_primary_host else respect_cooldown),
        )
    else:
        logger.info(
            "Host health monitor: alert debounce active for host='%s' status=%s elapsed=%.1fs threshold=%ss",
            host_name,
            failed_status,
            unhealthy_elapsed_seconds,
            unhealthy_alert_threshold_seconds,
        )

    return login_result, failed_status, None


def _extract_provider_paid_at(*candidates) -> str | None:
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, (int, float)):
            raw = float(candidate)
            epoch_seconds = raw / 1000.0 if raw > 10_000_000_000 else raw
            try:
                return _naive_fromtimestamp(epoch_seconds).isoformat()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to parse provider epoch paid_at '%s': %s", candidate, exc)
                continue
        raw_text = str(candidate).strip()
        if not raw_text:
            continue
        normalized = raw_text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
            return parsed.replace(tzinfo=None).isoformat()
        except ValueError:
            continue
    return None


def _scheduler_metadata_bot_source_matches_runtime(metadata: dict | None) -> bool:
    runtime_source = normalize_bot_source(BOT_SOURCE) or "main"
    normalized_metadata = normalize_payment_metadata(metadata)
    metadata_source = normalize_bot_source(normalized_metadata.get("bot_source"))
    if runtime_source == "reserve":
        # Reserve runtime must not process legacy rows without bot_source marker.
        return metadata_source == "reserve"
    return metadata_bot_source_matches_runtime(normalized_metadata, runtime_source)


def _normalize_transaction_metadata(raw_metadata: object) -> dict:
    normalized = normalize_payment_metadata(raw_metadata)
    if normalized:
        return normalized
    if not isinstance(raw_metadata, str):
        return {}
    try:
        parsed = json.loads(raw_metadata)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return normalize_payment_metadata(parsed)


def _hydrate_retry_metadata_from_transaction(
    *,
    tx: dict,
    metadata: dict,
    payment_id: str,
    retry_source: str,
) -> dict:
    from shop_bot.domain.retry import hydrate_retry_metadata

    return hydrate_retry_metadata(
        tx=tx,
        metadata=metadata,
        payment_id=payment_id,
        retry_source=retry_source,
        normalize_fn=normalize_payment_metadata,
    )


def _normalize_retry_host_alias(metadata: dict) -> dict:
    normalized = normalize_payment_metadata(metadata)
    raw_host = str(normalized.get("host_name") or "").strip()
    if not raw_host:
        return normalized
    if database.get_host(raw_host):
        return normalized

    alias_candidates = [f"{raw_host}@", raw_host.rstrip("@")]
    for candidate in alias_candidates:
        host_alias = str(candidate or "").strip()
        if not host_alias or host_alias == raw_host:
            continue
        if database.get_host(host_alias):
            normalized["host_name"] = host_alias
            logger.warning(
                "Pending-confirmed scan normalized host alias: %s -> %s",
                raw_host,
                host_alias,
            )
            return normalized
    return normalized


def _scan_pending_confirmed_retry_candidates(
    *,
    limit: int,
    min_age_seconds: int,
    host_name: str | None,
    due_only: bool,
    retryable_only: bool,
    retry_source: str,
) -> tuple[list[dict], dict[str, int]]:
    raw_candidates = database.get_pending_confirmed_transactions(
        limit=limit,
        min_age_seconds=min_age_seconds,
        host_name=host_name,
        due_only=due_only,
        retryable_only=retryable_only,
    )
    stats = {
        "scanned": 0,
        "accepted": 0,
        "skipped_foreign": 0,
        "skipped_incomplete": 0,
    }
    candidates: list[dict] = []
    for tx in raw_candidates:
        payment_id = str(tx.get("payment_id") or "").strip()
        if not payment_id:
            continue

        stats["scanned"] += 1
        metadata = normalize_payment_metadata(tx.get("metadata"))
        if not _scheduler_metadata_bot_source_matches_runtime(metadata):
            stats["skipped_foreign"] += 1
            logger.warning(
                "Pending-confirmed retry skipped: payment_id=%s belongs to bot_source=%s, runtime BOT_SOURCE=%s.",
                payment_id,
                normalize_bot_source(metadata.get("bot_source")) or "unknown",
                normalize_bot_source(BOT_SOURCE) or "main",
            )
            continue

        metadata = _hydrate_retry_metadata_from_transaction(
            tx=tx,
            metadata=metadata,
            payment_id=payment_id,
            retry_source=retry_source,
        )
        metadata = _normalize_retry_host_alias(metadata)
        missing_fields = missing_required_payment_metadata_fields(metadata)
        if missing_fields:
            stats["skipped_incomplete"] += 1
            logger.warning(
                "Pending-confirmed scan skipped incomplete metadata: payment_id=%s fields=%s",
                payment_id,
                ",".join(missing_fields),
            )
            continue

        candidates.append(
            {
                "payment_id": payment_id,
                "metadata": metadata,
                "tx": tx,
            }
        )
        stats["accepted"] += 1
    return candidates, stats


async def reconcile_pending_platega_transactions(bot_controller: BotController) -> dict[str, int]:
    """
    Reconcile stale local pending Platega transactions with provider state.
    - canceled on provider -> mark local transaction canceled
    - succeeded on provider -> finalize key issuance from pending metadata
    """
    global _last_platega_reconcile_at  # noqa: PLW0603 - scheduler throttle is module-level runtime state.

    if str(BOT_SOURCE).strip().lower() != "reserve":
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}

    now = _naive_now()
    if (
        _last_platega_reconcile_at is not None
        and (now - _last_platega_reconcile_at).total_seconds() < PLATEGA_RECONCILE_INTERVAL_SECONDS
    ):
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}
    _last_platega_reconcile_at = now

    platega_shop_id = (database.get_setting("platega_shop_id") or "").strip()
    platega_api_key = (database.get_setting("platega_api_key") or "").strip()
    if not platega_shop_id or not platega_api_key:
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}

    candidates = database.get_pending_platega_transactions(
        limit=PLATEGA_RECONCILE_LIMIT,
        min_age_seconds=PLATEGA_RECONCILE_MIN_AGE_SECONDS,
    )
    if not candidates:
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}

    checked = 0
    canceled = 0
    recovered = 0
    errors = 0
    last_error_code = ""

    for tx in candidates:
        payment_id = str(tx.get("payment_id") or "").strip()
        if not payment_id:
            continue

        checked += 1
        try:
            provider_state = await platega_api.check_status(payment_id)
            status = str(provider_state.get("status") or "").strip().lower()
            provider_status = str(provider_state.get("provider_status") or "").strip().lower()
        except Exception as e:
            last_error_code = platega_api.get_exception_code(e)
            logger.exception(
                "Platega reconcile: failed to fetch payment_id=%s (%s)",
                payment_id,
                last_error_code,
            )
            errors += 1
            continue

        if status == "canceled":
            if database.mark_transaction_canceled(payment_id, payment_method="Platega"):
                canceled += 1
            continue

        if status != "succeeded":
            continue

        if database.is_transaction_fully_issued(payment_id):
            continue

        pending_metadata = database.get_pending_transaction_metadata(payment_id)
        metadata = normalize_payment_metadata(pending_metadata)
        missing_fields = missing_required_payment_metadata_fields(metadata)
        if missing_fields:
            logger.error(
                "Platega reconcile: payment_id=%s is succeeded but metadata is incomplete. fields=%s",
                payment_id,
                ",".join(missing_fields),
            )
            errors += 1
            continue

        if not _scheduler_metadata_bot_source_matches_runtime(metadata):
            logger.warning(
                "Platega reconcile skipped: payment_id=%s belongs to bot_source=%s, runtime BOT_SOURCE=%s.",
                payment_id,
                normalize_bot_source(metadata.get("bot_source")) or "unknown",
                normalize_bot_source(BOT_SOURCE) or "main",
            )
            continue

        metadata["external_payment_id"] = payment_id
        metadata["payment_confirmed"] = True
        metadata["retry_source"] = "platega_reconcile"
        metadata["payment_method"] = "Platega"
        if provider_status:
            metadata["provider_status"] = provider_status
        if not metadata.get("paid_at"):
            metadata["paid_at"] = datetime.now(_MOSCOW_TZ).isoformat()

        database.mark_transaction_confirmed(
            payment_id,
            metadata=metadata,
            payment_method="Platega",
            confirmed_at=metadata.get("paid_at"),
        )
        database.update_transaction_metadata(payment_id, metadata, statuses=("pending",))

        if not database.start_transaction_processing(payment_id):
            continue
        database.update_transaction_metadata(payment_id, metadata, statuses=("processing",))

        bot = bot_controller.get_bot_instance()
        if not bot:
            logger.warning(
                "Platega reconcile: payment_id=%s succeeded but bot instance is unavailable. Returning to pending.",
                payment_id,
            )
            database.mark_transaction_retryable_failure(
                payment_id,
                reason="bot_unavailable",
                metadata=metadata,
                payment_method="Platega",
                statuses=("processing", "pending"),
            )
            continue

        try:
            await process_confirmed_payment(bot, metadata)
            if database.is_transaction_fully_issued(payment_id):
                recovered += 1
            elif terminal_state := _get_terminal_issuance_state(payment_id):
                logger.info(
                    "Platega reconcile: payment_id=%s finished with terminal issuance_state=%s.",
                    payment_id,
                    terminal_state,
                )
            else:
                logger.warning(
                    "Platega reconcile: payment_id=%s processing finished but transaction is not paid.",
                    payment_id,
                )
                database.mark_transaction_retryable_failure(
                    payment_id,
                    reason="not_marked_paid_after_processing",
                    metadata=metadata,
                    payment_method="Platega",
                    statuses=("processing", "pending"),
                )
        except Exception as e:
            logger.exception(
                "Platega reconcile: failed to finalize payment_id=%s",
                payment_id,
            )
            database.mark_transaction_retryable_failure(
                payment_id,
                reason=f"platega_reconcile_exception:{type(e).__name__}",
                metadata=metadata,
                payment_method="Platega",
                statuses=("processing", "pending"),
            )
            errors += 1

    if checked:
        logger.info(
            "Platega reconcile: checked=%s canceled=%s recovered=%s errors=%s",
            checked,
            canceled,
            recovered,
            errors,
        )

    if errors > 0:
        details = (
            f"provider=Platega checked={checked} errors={errors} recovered={recovered} reason=platega_reconcile_error"
        )
        if last_error_code:
            details += f" error={last_error_code}"
        send_critical_alert_nonblocking(
            BotError.PAYMENT_RECONCILE_FAILED,
            details=details,
            respect_cooldown=True,
        )

    return {"checked": checked, "canceled": canceled, "recovered": recovered, "errors": errors}


async def reconcile_pending_lava_transactions(bot_controller: BotController) -> dict[str, int]:
    """Recover reserve-bot LAVA invoices when a webhook is delayed or lost."""
    global _last_lava_reconcile_at  # noqa: PLW0603 - scheduler throttle is module-level runtime state.

    if normalize_bot_source(BOT_SOURCE) != "reserve":
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}

    now = _naive_now()
    if (
        _last_lava_reconcile_at is not None
        and (now - _last_lava_reconcile_at).total_seconds() < LAVA_RECONCILE_INTERVAL_SECONDS
    ):
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}
    _last_lava_reconcile_at = now

    configured_shop_id = str(os.environ.get("LAVA_SHOP_ID") or "").strip()
    configured_secret = str(os.environ.get("LAVA_SECRET_KEY") or "").strip()
    if not configured_shop_id or not configured_secret:
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}

    candidates = database.get_pending_lava_transactions(
        limit=LAVA_RECONCILE_LIMIT,
        min_age_seconds=LAVA_RECONCILE_MIN_AGE_SECONDS,
    )
    if not candidates:
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}

    checked = 0
    canceled = 0
    recovered = 0
    errors = 0
    skipped_foreign = 0
    last_error_code = ""

    for tx in candidates:
        payment_id = str(tx.get("payment_id") or "").strip()
        if not payment_id:
            continue

        tx_metadata = _normalize_transaction_metadata(tx.get("metadata"))
        if not _scheduler_metadata_bot_source_matches_runtime(tx_metadata):
            skipped_foreign += 1
            logger.warning(
                "LAVA reconcile skipped before provider call: payment_id=%s belongs to bot_source=%s, "
                "runtime BOT_SOURCE=%s.",
                payment_id,
                normalize_bot_source(tx_metadata.get("bot_source")) or "unknown",
                normalize_bot_source(BOT_SOURCE) or "main",
            )
            continue

        expected_order_id = str(tx_metadata.get("lava_order_id") or "").strip()
        provisional_invoice = (
            bool(tx_metadata.get("lava_invoice_pending"))
            and bool(expected_order_id)
            and payment_id == f"lava-order:{expected_order_id}"
            and not tx_metadata.get("lava_invoice_id")
        )
        checked += 1
        try:
            if provisional_invoice:
                provider_state = await lava_api.check_status(
                    order_id=expected_order_id, expected_amount=tx.get("amount_rub", tx_metadata.get("price")),
                )
            else:
                provider_state = await lava_api.check_status(invoice_id=payment_id)
            status = str(provider_state.get("status") or "").strip().lower()
            provider_status = str(provider_state.get("provider_status") or "").strip().lower()
        except Exception as exc:
            last_error_code = lava_api.get_exception_code(exc)
            logger.exception(
                "LAVA reconcile: failed to fetch payment_id=%s (%s)",
                payment_id,
                last_error_code,
            )
            errors += 1
            continue

        if status not in {"succeeded", "canceled"} and not provisional_invoice:
            continue
        if database.is_transaction_fully_issued(payment_id):
            continue

        pending_metadata = database.get_pending_transaction_metadata(payment_id)
        metadata = merge_payment_metadata(pending_metadata, tx_metadata)
        missing_fields = missing_required_payment_metadata_fields(metadata)
        if missing_fields and status == "succeeded":
            logger.error(
                "LAVA reconcile: payment_id=%s is successful but metadata is incomplete. fields=%s",
                payment_id,
                ",".join(missing_fields),
            )
            errors += 1
            continue
        if not _scheduler_metadata_bot_source_matches_runtime(metadata):
            skipped_foreign += 1
            continue

        expected_order_id = str(metadata.get("lava_order_id") or "").strip()
        provider_invoice_id = str(provider_state.get("invoice_id") or "").strip()
        expected_invoice_id = (
            provider_invoice_id if provisional_invoice else str(metadata.get("lava_invoice_id") or payment_id).strip()
        )
        provider_order_id = str(provider_state.get("order_id") or "").strip()
        provider_shop_id = str(provider_state.get("shop_id") or "").strip()
        provider_currency = str(provider_state.get("currency") or "").strip().upper()
        try:
            expected_amount = Decimal(str(tx.get("amount_rub", metadata.get("price")))).quantize(
                Decimal("0.01")
            )
            provider_amount = Decimal(str(provider_state.get("amount"))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            expected_amount = Decimal("-1")
            provider_amount = Decimal("-2")

        if (
            not expected_order_id
            or not expected_invoice_id
            or (not provisional_invoice and expected_invoice_id != payment_id)
            or provider_invoice_id != expected_invoice_id
            or provider_order_id != expected_order_id
            or provider_shop_id != configured_shop_id
            or not expected_amount.is_finite()
            or not provider_amount.is_finite()
            or expected_amount <= 0
            or provider_amount != expected_amount
            or (provider_currency and provider_currency != "RUB")
        ):
            logger.error("LAVA reconcile rejected invoice integrity mismatch: payment_id=%s", payment_id)
            errors += 1
            last_error_code = "invoice_integrity_mismatch"
            continue

        if provisional_invoice:
            try:
                promoted = await asyncio.to_thread(
                    database.promote_lava_pending_transaction, expected_order_id, provider_invoice_id,
                )
            except Exception:  # Preserve unknown invoice state for a later safe retry.
                logger.exception("LAVA reconcile: failed to bind confirmed provider invoice for order_id=%s", expected_order_id)
                promoted = None
            if not promoted:
                errors += 1
                last_error_code = "invoice_promotion_failed"
                continue
            payment_id = str(promoted["payment_id"])
            metadata = dict(promoted["metadata"])
            if await asyncio.to_thread(database.is_transaction_fully_issued, payment_id):
                continue

        if status == "canceled":
            if await asyncio.to_thread(database.mark_transaction_canceled, payment_id, payment_method="LAVA"):
                canceled += 1
                # Only an authoritative terminal unpaid state proves the original
                # discounted invoice can no longer be paid. A refund is not one.
                if provider_status in {"expired", "fail"} and not metadata.get("payment_confirmed"):
                    try:
                        await asyncio.to_thread(
                            referrals.mark_discount_invoice, metadata.get("referral_discount_claim_id"),
                            payment_id, status="cancelled",
                            note=f"LAVA status API confirmed invoice {provider_status}; original invoice is not payable.",
                        )
                    except Exception:  # Keep the discount reserved until reviewed.
                        logger.exception("LAVA reconcile: canceled invoice discount remains reserved: payment_id=%s", payment_id)
            continue
        if status != "succeeded":
            continue

        metadata["external_payment_id"] = payment_id
        metadata["lava_invoice_id"] = expected_invoice_id
        metadata["lava_order_id"] = expected_order_id
        metadata["payment_confirmed"] = True
        metadata["retry_source"] = "lava_reconcile"
        metadata["payment_method"] = "LAVA"
        if provider_status:
            metadata["provider_status"] = provider_status
        if not metadata.get("paid_at"):
            metadata["paid_at"] = datetime.now(_MOSCOW_TZ).isoformat()

        database.mark_transaction_confirmed(
            payment_id,
            metadata=metadata,
            payment_method="LAVA",
            confirmed_at=metadata.get("paid_at"),
        )
        database.update_transaction_metadata(payment_id, metadata, statuses=("pending",))

        if not database.start_transaction_processing(payment_id):
            continue
        database.update_transaction_metadata(payment_id, metadata, statuses=("processing",))

        bot = bot_controller.get_bot_instance()
        if not bot:
            database.mark_transaction_retryable_failure(
                payment_id,
                reason="bot_unavailable",
                metadata=metadata,
                payment_method="LAVA",
                statuses=("processing", "pending"),
            )
            continue

        try:
            await process_confirmed_payment(bot, metadata)
            if database.is_transaction_fully_issued(payment_id):
                recovered += 1
            elif terminal_state := _get_terminal_issuance_state(payment_id):
                logger.info(
                    "LAVA reconcile: payment_id=%s finished with terminal issuance_state=%s.",
                    payment_id,
                    terminal_state,
                )
            else:
                database.mark_transaction_retryable_failure(
                    payment_id,
                    reason="not_marked_paid_after_processing",
                    metadata=metadata,
                    payment_method="LAVA",
                    statuses=("processing", "pending"),
                )
        except Exception as exc:
            logger.exception("LAVA reconcile: failed to finalize payment_id=%s", payment_id)
            database.mark_transaction_retryable_failure(
                payment_id,
                reason=f"lava_reconcile_exception:{type(exc).__name__}",
                metadata=metadata,
                payment_method="LAVA",
                statuses=("processing", "pending"),
            )
            errors += 1

    if checked:
        logger.info(
            "LAVA reconcile: checked=%s canceled=%s recovered=%s errors=%s skipped_foreign=%s",
            checked,
            canceled,
            recovered,
            errors,
            skipped_foreign,
        )
    if errors > 0:
        details = (
            f"source={BOT_SOURCE} provider=LAVA checked={checked} errors={errors} recovered={recovered} "
            "reason=lava_reconcile_error"
        )
        if last_error_code:
            details += f" error={last_error_code}"
        send_critical_alert_nonblocking(
            BotError.PAYMENT_RECONCILE_FAILED,
            details=details,
            respect_cooldown=True,
        )

    return {"checked": checked, "canceled": canceled, "recovered": recovered, "errors": errors}


async def reconcile_pending_paypear_transactions(bot_controller: BotController) -> dict[str, int]:
    """
    Reconcile stale local pending PayPear transactions with provider state.
    - canceled on provider -> mark local transaction canceled
    - succeeded on provider -> finalize key issuance from pending metadata
    """
    global _last_paypear_reconcile_at  # noqa: PLW0603 - scheduler throttle is module-level runtime state.

    now = _naive_now()
    if (
        _last_paypear_reconcile_at is not None
        and (now - _last_paypear_reconcile_at).total_seconds() < PAYPEAR_RECONCILE_INTERVAL_SECONDS
    ):
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}
    _last_paypear_reconcile_at = now

    paypear_shop_id = (database.get_setting("paypear_shop_id") or "").strip()
    paypear_secret_key = (
        os.environ.get("PAYPEAR_SECRET_KEY")
        or os.environ.get("PAYPEAR_API_KEY")
        or database.get_setting("paypear_secret_key")
        or database.get_setting("paypear_api_key")
        or ""
    ).strip()
    if not paypear_shop_id:
        paypear_shop_id = (os.environ.get("PAYPEAR_SHOP_ID") or "").strip()
    if not paypear_shop_id or not paypear_secret_key:
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}

    candidates = database.get_pending_paypear_transactions(
        limit=PAYPEAR_RECONCILE_LIMIT,
        min_age_seconds=PAYPEAR_RECONCILE_MIN_AGE_SECONDS,
    )
    if not candidates:
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}

    checked = 0
    canceled = 0
    recovered = 0
    errors = 0
    skipped_foreign = 0
    last_error_code = ""

    for tx in candidates:
        payment_id = str(tx.get("payment_id") or "").strip()
        if not payment_id:
            continue

        tx_metadata = _normalize_transaction_metadata(tx.get("metadata"))
        if not _scheduler_metadata_bot_source_matches_runtime(tx_metadata):
            skipped_foreign += 1
            logger.warning(
                "PayPear reconcile skipped before provider call: payment_id=%s belongs to bot_source=%s, runtime BOT_SOURCE=%s.",
                payment_id,
                normalize_bot_source(tx_metadata.get("bot_source")) or "unknown",
                normalize_bot_source(BOT_SOURCE) or "main",
            )
            continue

        checked += 1
        try:
            provider_state = await paypear_api.check_status(payment_id)
            status = str(provider_state.get("status") or "").strip().lower()
            provider_status = str(provider_state.get("provider_status") or "").strip().lower()
        except Exception as e:
            last_error_code = paypear_api.get_exception_code(e)
            logger.exception(
                "PayPear reconcile: failed to fetch payment_id=%s (%s)",
                payment_id,
                last_error_code,
            )
            errors += 1
            continue

        if status == "canceled":
            if database.mark_transaction_canceled(payment_id, payment_method="PayPear"):
                canceled += 1
            continue

        if status != "succeeded":
            continue

        if database.is_transaction_fully_issued(payment_id):
            continue

        pending_metadata = database.get_pending_transaction_metadata(payment_id)
        metadata = merge_payment_metadata(pending_metadata, tx_metadata)
        missing_fields = missing_required_payment_metadata_fields(metadata)
        if missing_fields:
            logger.error(
                "PayPear reconcile: payment_id=%s is succeeded but metadata is incomplete. fields=%s",
                payment_id,
                ",".join(missing_fields),
            )
            errors += 1
            continue

        if not _scheduler_metadata_bot_source_matches_runtime(metadata):
            logger.warning(
                "PayPear reconcile skipped: payment_id=%s belongs to bot_source=%s, runtime BOT_SOURCE=%s.",
                payment_id,
                normalize_bot_source(metadata.get("bot_source")) or "unknown",
                normalize_bot_source(BOT_SOURCE) or "main",
            )
            continue

        metadata["external_payment_id"] = payment_id
        metadata["payment_confirmed"] = True
        metadata["retry_source"] = "paypear_reconcile"
        metadata["payment_method"] = "PayPear"
        if provider_status:
            metadata["provider_status"] = provider_status
        if not metadata.get("paid_at"):
            metadata["paid_at"] = datetime.now(_MOSCOW_TZ).isoformat()

        database.mark_transaction_confirmed(
            payment_id,
            metadata=metadata,
            payment_method="PayPear",
            confirmed_at=metadata.get("paid_at"),
        )
        database.update_transaction_metadata(payment_id, metadata, statuses=("pending",))

        if not database.start_transaction_processing(payment_id):
            continue
        database.update_transaction_metadata(payment_id, metadata, statuses=("processing",))

        bot = bot_controller.get_bot_instance()
        if not bot:
            logger.warning(
                "PayPear reconcile: payment_id=%s succeeded but bot instance is unavailable. Returning to pending.",
                payment_id,
            )
            database.mark_transaction_retryable_failure(
                payment_id,
                reason="bot_unavailable",
                metadata=metadata,
                payment_method="PayPear",
                statuses=("processing", "pending"),
            )
            continue

        try:
            await process_confirmed_payment(bot, metadata)
            if database.is_transaction_fully_issued(payment_id):
                recovered += 1
            elif terminal_state := _get_terminal_issuance_state(payment_id):
                logger.info(
                    "PayPear reconcile: payment_id=%s finished with terminal issuance_state=%s.",
                    payment_id,
                    terminal_state,
                )
            else:
                logger.warning(
                    "PayPear reconcile: payment_id=%s processing finished but transaction is not paid.",
                    payment_id,
                )
                database.mark_transaction_retryable_failure(
                    payment_id,
                    reason="not_marked_paid_after_processing",
                    metadata=metadata,
                    payment_method="PayPear",
                    statuses=("processing", "pending"),
                )
        except Exception as e:
            logger.exception(
                "PayPear reconcile: failed to finalize payment_id=%s",
                payment_id,
            )
            database.mark_transaction_retryable_failure(
                payment_id,
                reason=f"paypear_reconcile_exception:{type(e).__name__}",
                metadata=metadata,
                payment_method="PayPear",
                statuses=("processing", "pending"),
            )
            errors += 1

    if checked:
        logger.info(
            "PayPear reconcile: checked=%s canceled=%s recovered=%s errors=%s skipped_foreign=%s",
            checked,
            canceled,
            recovered,
            errors,
            skipped_foreign,
        )

    if errors > 0:
        details = (
            f"source={BOT_SOURCE} provider=PayPear checked={checked} errors={errors} recovered={recovered} "
            "reason=paypear_reconcile_error"
        )
        if last_error_code:
            details += f" error={last_error_code}"
        send_critical_alert_nonblocking(
            BotError.PAYMENT_RECONCILE_FAILED,
            details=details,
            respect_cooldown=True,
        )

    return {"checked": checked, "canceled": canceled, "recovered": recovered, "errors": errors}


async def reconcile_pending_yookassa_transactions(bot_controller: BotController) -> dict[str, int]:
    """
    Reconcile stale local pending YooKassa transactions with provider state.
    - canceled on provider -> mark local transaction canceled
    - succeeded on provider -> finalize key issuance from pending metadata
    """
    global _last_yookassa_reconcile_at  # noqa: PLW0603 - scheduler throttle is module-level runtime state.

    now = _naive_now()
    if (
        _last_yookassa_reconcile_at is not None
        and (now - _last_yookassa_reconcile_at).total_seconds() < YOOKASSA_RECONCILE_INTERVAL_SECONDS
    ):
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}

    _last_yookassa_reconcile_at = now

    yk_shop_id = (database.get_setting("yookassa_shop_id") or "").strip()
    yk_secret_key = (database.get_setting("yookassa_secret_key") or "").strip()
    if not yk_shop_id or not yk_secret_key:
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}

    try:
        from yookassa import Configuration, Payment  # local import to keep scheduler import-light
    except Exception:
        logger.exception("YooKassa reconcile skipped: failed to import SDK.")
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 1}

    Configuration.account_id = yk_shop_id
    Configuration.secret_key = yk_secret_key

    candidates = database.get_pending_yookassa_transactions(
        limit=YOOKASSA_RECONCILE_LIMIT,
        min_age_seconds=YOOKASSA_RECONCILE_MIN_AGE_SECONDS,
    )
    if not candidates:
        return {"checked": 0, "canceled": 0, "recovered": 0, "errors": 0}

    checked = 0
    canceled = 0
    recovered = 0
    errors = 0

    for tx in candidates:
        payment_id = str(tx.get("payment_id") or "").strip()
        if not payment_id:
            continue

        checked += 1
        try:
            payment_obj = await asyncio.to_thread(Payment.find_one, payment_id)
            status = str(getattr(payment_obj, "status", "") or "").strip().lower()
        except Exception:
            logger.exception(
                "YooKassa reconcile: failed to fetch payment_id=%s",
                payment_id,
            )
            errors += 1
            continue

        if status == "canceled":
            if database.mark_transaction_canceled(payment_id, payment_method="YooKassa"):
                canceled += 1
            continue

        if status != "succeeded":
            continue

        if database.is_transaction_fully_issued(payment_id):
            continue

        provider_metadata = normalize_payment_metadata(getattr(payment_obj, "metadata", None))
        pending_metadata = database.get_pending_transaction_metadata(payment_id)
        metadata = merge_payment_metadata(pending_metadata, provider_metadata)
        missing_fields = missing_required_payment_metadata_fields(metadata)
        if missing_fields:
            logger.error(
                "YooKassa reconcile: payment_id=%s is succeeded but metadata is incomplete after merge. fields=%s",
                payment_id,
                ",".join(missing_fields),
            )
            errors += 1
            continue
        if not _scheduler_metadata_bot_source_matches_runtime(metadata):
            logger.warning(
                "YooKassa reconcile skipped: payment_id=%s belongs to bot_source=%s, runtime BOT_SOURCE=%s.",
                payment_id,
                normalize_bot_source(metadata.get("bot_source")) or "unknown",
                normalize_bot_source(BOT_SOURCE) or "main",
            )
            continue

        if not metadata.get("external_payment_id"):
            metadata["external_payment_id"] = payment_id
        metadata["payment_confirmed"] = True
        metadata["retry_source"] = "yookassa_reconcile"
        if not metadata.get("payment_method"):
            metadata["payment_method"] = "YooKassa"
        if not metadata.get("paid_at"):
            paid_at = _extract_provider_paid_at(
                getattr(payment_obj, "captured_at", None),
                getattr(payment_obj, "paid_at", None),
                getattr(payment_obj, "created_at", None),
            )
            if paid_at:
                metadata["paid_at"] = paid_at
        database.mark_transaction_confirmed(
            payment_id,
            metadata=metadata,
            payment_method="YooKassa",
            confirmed_at=metadata.get("paid_at"),
        )
        database.update_transaction_metadata(payment_id, metadata, statuses=("pending",))

        if not database.start_transaction_processing(payment_id):
            continue
        database.update_transaction_metadata(payment_id, metadata, statuses=("processing",))

        bot = bot_controller.get_bot_instance()
        if not bot:
            logger.warning(
                "YooKassa reconcile: payment_id=%s succeeded but bot instance is unavailable. Returning to pending.",
                payment_id,
            )
            database.mark_transaction_retryable_failure(
                payment_id,
                reason="bot_unavailable",
                metadata=metadata,
                payment_method="YooKassa",
                statuses=("processing", "pending"),
            )
            continue

        try:
            await process_confirmed_payment(bot, metadata)
            if database.is_transaction_fully_issued(payment_id):
                recovered += 1
            elif terminal_state := _get_terminal_issuance_state(payment_id):
                logger.info(
                    "YooKassa reconcile: payment_id=%s finished with terminal issuance_state=%s.",
                    payment_id,
                    terminal_state,
                )
            else:
                logger.warning(
                    "YooKassa reconcile: payment_id=%s processing finished but transaction is not paid.",
                    payment_id,
                )
                database.mark_transaction_retryable_failure(
                    payment_id,
                    reason="not_marked_paid_after_processing",
                    metadata=metadata,
                    payment_method="YooKassa",
                    statuses=("processing", "pending"),
                )
        except Exception as e:
            logger.exception(
                "YooKassa reconcile: failed to finalize payment_id=%s",
                payment_id,
            )
            database.mark_transaction_retryable_failure(
                payment_id,
                reason=f"reconcile_exception:{type(e).__name__}",
                metadata=metadata,
                payment_method="YooKassa",
                statuses=("processing", "pending"),
            )
            errors += 1

    if checked:
        logger.info(
            "YooKassa reconcile: checked=%s canceled=%s recovered=%s errors=%s",
            checked,
            canceled,
            recovered,
            errors,
        )

    if errors > 0:
        send_critical_alert_nonblocking(
            BotError.PAYMENT_RECONCILE_FAILED,
            details=(
                f"provider=YooKassa checked={checked} errors={errors} recovered={recovered} "
                "reason=yookassa_reconcile_error"
            ),
            respect_cooldown=True,
        )

    _emit_issuance_sla_alert_if_needed()

    return {"checked": checked, "canceled": canceled, "recovered": recovered, "errors": errors}


_processing_payment_ids: set[str] = set()
_TERMINAL_ISSUANCE_STATES = frozenset(
    {
        database.ISSUANCE_STATE_ISSUED,
        database.ISSUANCE_STATE_FATAL_FAILED,
        database.ISSUANCE_STATE_EXHAUSTED,
        database.ISSUANCE_STATE_CANCELED,
    }
)


def _get_terminal_issuance_state(payment_id: str | None) -> str | None:
    if not payment_id:
        return None
    tx = database.get_transaction_by_payment_id(payment_id)
    if not isinstance(tx, dict):
        return None
    state = str(tx.get("issuance_state") or "").strip().lower()
    return state if state in _TERMINAL_ISSUANCE_STATES else None


def _retry_host_pause_reason(host_name: str | None) -> str | None:
    """Return a pause reason when autoretry must not touch this host now."""
    normalized_host = str(host_name or "").strip()
    if not normalized_host:
        return None

    if normalized_host == blitz_api.BLITZ_HOST_NAME and not blitz_api.is_blitz_enabled():
        return "blitz_disabled"

    host = database.get_host(normalized_host)
    if not host:
        return None
    if host.get("is_maintenance"):
        return "maintenance"

    host_status = str(host.get("status") or "").strip().lower()
    if host_status in {"unavailable", "maintenance"}:
        return host_status
    return None


async def retry_pending_confirmed_transactions(
    bot_controller: BotController,
    *,
    target_host: str | None = None,
    retry_source: str = "pending_confirmed_retry",
    due_only: bool = True,
    only_retryable_failed: bool = False,
    min_age_seconds: int | None = None,
    limit: int | None = None,
    emit_sla_alert: bool = True,
) -> dict[str, object]:
    """
    Retry key issuance for payments that are already confirmed by provider
    and intentionally kept in local pending state after issuance failure.
    """
    safe_limit = max(1, int(limit or YOOKASSA_RECONCILE_LIMIT))
    safe_min_age = YOOKASSA_RECONCILE_MIN_AGE_SECONDS if min_age_seconds is None else max(0, int(min_age_seconds))
    normalized_target_host = str(target_host or "").strip()

    candidates, scan_stats = _scan_pending_confirmed_retry_candidates(
        limit=safe_limit,
        min_age_seconds=safe_min_age,
        host_name=normalized_target_host or None,
        due_only=due_only,
        retryable_only=only_retryable_failed,
        retry_source=str(retry_source or "pending_confirmed_retry"),
    )
    if not candidates:
        return {
            "checked": 0,
            "recovered": 0,
            "errors": 0,
            "payment_ids": [],
            "recovered_payment_ids": [],
            "scan_scanned": int(scan_stats.get("scanned") or 0),
            "scan_accepted": int(scan_stats.get("accepted") or 0),
            "scan_skipped_foreign": int(scan_stats.get("skipped_foreign") or 0),
            "scan_skipped_incomplete": int(scan_stats.get("skipped_incomplete") or 0),
        }

    bot = bot_controller.get_bot_instance()
    if not bot:
        logger.warning("Pending-confirmed retry skipped: bot instance is unavailable.")
        return {"checked": 0, "recovered": 0, "errors": 0, "payment_ids": [], "recovered_payment_ids": []}

    checked = 0
    recovered = 0
    errors = 0
    host_skipped = 0
    checked_payment_ids: list[str] = []
    recovered_payment_ids: list[str] = []

    by_host: dict[str, list[dict]] = {}
    host_names_by_key: dict[str, str] = {}
    for c in candidates:
        raw_host = str((c.get("metadata") or {}).get("host_name") or "unknown").strip()
        host = raw_host.lower()
        by_host.setdefault(host, []).append(c)
        host_names_by_key.setdefault(host, raw_host)

    _unavailable_hosts_this_cycle: set[str] = set()

    for host_name_key, host_candidates in by_host.items():
        host_display_name = host_names_by_key.get(host_name_key) or host_name_key
        pause_reason = await asyncio.to_thread(_retry_host_pause_reason, host_display_name)
        if pause_reason:
            skipped_count = len(host_candidates)
            host_skipped += skipped_count
            logger.info(
                "Host-aware skip: host=%s pause_reason=%s — skipping %d txns (retry_count unchanged)",
                host_display_name,
                pause_reason,
                skipped_count,
            )
            continue

        snapshot = xui_api.get_host_health_snapshot(host_display_name) or xui_api.get_host_health_snapshot(
            host_name_key
        )
        if snapshot and snapshot.status in ("unavailable", "maintenance"):
            skipped_count = len(host_candidates)
            host_skipped += skipped_count
            logger.info(
                "Host-aware skip: host=%s status=%s — skipping %d txns (retry_count unchanged)",
                host_name_key,
                snapshot.status,
                skipped_count,
            )
            continue

        for candidate in host_candidates:
            payment_id = str(candidate.get("payment_id") or "").strip()
            if not payment_id:
                continue

            if payment_id in _processing_payment_ids:
                logger.debug("Skipping payment_id=%s — already being processed by another source", payment_id)
                continue

            if host_name_key in _unavailable_hosts_this_cycle:
                logger.debug(
                    "Skipping payment_id=%s — host %s marked unavailable this cycle", payment_id, host_name_key
                )
                host_skipped += 1
                continue

            checked += 1
            checked_payment_ids.append(payment_id)
            tx = dict(candidate.get("tx") or {})
            metadata = normalize_payment_metadata(candidate.get("metadata"))
            metadata["trace_id"] = uuid.uuid4().hex[:12]
            if tx.get("retry_count") is not None:
                with contextlib.suppress(TypeError, ValueError):
                    metadata["retry_count"] = int(tx.get("retry_count") or 0)
            if tx.get("next_retry_at"):
                metadata["next_retry_at"] = str(tx.get("next_retry_at"))
            if tx.get("last_error"):
                metadata["last_error"] = str(tx.get("last_error"))
            # Claim the processing state and persist retry metadata together.
            # A fresh lease acquired after the candidate scan must remain intact.
            if not await asyncio.to_thread(
                database.start_transaction_processing, payment_id, metadata=metadata,
            ):
                continue

            _processing_payment_ids.add(payment_id)
            try:
                await process_confirmed_payment(bot, metadata)
                if database.is_transaction_fully_issued(payment_id):
                    recovered += 1
                    recovered_payment_ids.append(payment_id)
                    send_critical_alert_nonblocking(
                        BotError.PAYMENT_ISSUE_RECOVERED,
                        details=(
                            f"source={BOT_SOURCE} "
                            f"reason=issuance_autoretry_success "
                            f"payment_id={payment_id} "
                            f"host={host_name_key} "
                        ),
                        respect_cooldown=False,
                    )
                elif terminal_state := _get_terminal_issuance_state(payment_id):
                    logger.info(
                        "Pending-confirmed retry: payment_id=%s finished with terminal issuance_state=%s.",
                        payment_id,
                        terminal_state,
                    )
                elif (
                    (latest_tx := await asyncio.to_thread(database.get_transaction_by_payment_id, payment_id))
                    and latest_tx.get("status") == "pending"
                    and latest_tx.get("issuance_state") == database.ISSUANCE_STATE_RETRYABLE_FAILED
                    and latest_tx.get("last_error") == "runtime_shutdown_deferred"
                ):
                    logger.info(
                        "Pending-confirmed retry deferred during shutdown: payment_id=%s (retry_count unchanged)",
                        payment_id,
                    )
                else:
                    logger.warning(
                        "Pending-confirmed retry: payment_id=%s processing finished but transaction is not paid.",
                        payment_id,
                    )
                    database.mark_transaction_retryable_failure(
                        payment_id,
                        reason="retry_not_marked_paid",
                        payment_method=str(metadata.get("payment_method") or ""),
                        statuses=("processing", "pending"),
                        respect_active_lock=True,
                    )
            except Exception as e:
                logger.exception(
                    "Pending-confirmed retry failed for payment_id=%s",
                    payment_id,
                )
                err_name = type(e).__name__
                database.mark_transaction_retryable_failure(
                    payment_id,
                    reason=f"retry_exception:{err_name}",
                    payment_method=str(metadata.get("payment_method") or ""),
                    statuses=("processing", "pending"),
                    respect_active_lock=True,
                )
                errors += 1
                if "timeout" in err_name.lower() or "connection" in err_name.lower():
                    _unavailable_hosts_this_cycle.add(host_name_key)
                    logger.info("Marking host %s as unavailable for remainder of cycle", host_name_key)
            finally:
                _processing_payment_ids.discard(payment_id)

    if checked:
        logger.info(
            "Pending-confirmed retry: scanned=%s accepted=%s checked=%s recovered=%s errors=%s skipped_foreign=%s skipped_incomplete=%s source=%s host=%s due_only=%s retryable_only=%s",
            scan_stats.get("scanned", 0),
            scan_stats.get("accepted", 0),
            checked,
            recovered,
            errors,
            scan_stats.get("skipped_foreign", 0),
            scan_stats.get("skipped_incomplete", 0),
            retry_source,
            normalized_target_host or "all",
            due_only,
            only_retryable_failed,
        )

    if errors > 0:
        send_critical_alert_nonblocking(
            BotError.PAYMENT_RECONCILE_FAILED,
            details=(
                f"source={BOT_SOURCE} reason=pending_confirmed_retry_error "
                f"retry_source={retry_source} host={normalized_target_host or 'all'} "
                f"checked={checked} recovered={recovered} errors={errors} "
                f"scan_scanned={scan_stats.get('scanned', 0)} "
                f"scan_skipped_foreign={scan_stats.get('skipped_foreign', 0)} "
                f"scan_skipped_incomplete={scan_stats.get('skipped_incomplete', 0)}"
            ),
            respect_cooldown=True,
        )

    if emit_sla_alert:
        _emit_issuance_sla_alert_if_needed()

    return {
        "checked": checked,
        "recovered": recovered,
        "errors": errors,
        "host_skipped": host_skipped,
        "payment_ids": checked_payment_ids,
        "recovered_payment_ids": recovered_payment_ids,
        "scan_scanned": int(scan_stats.get("scanned") or 0),
        "scan_accepted": int(scan_stats.get("accepted") or 0),
        "scan_skipped_foreign": int(scan_stats.get("skipped_foreign") or 0),
        "scan_skipped_incomplete": int(scan_stats.get("skipped_incomplete") or 0),
    }


def _host_is_unhealthy_for_autoretry(host_name: str | None) -> tuple[bool, str]:
    from shop_bot.domain.host_health import check_host_for_autoretry

    return check_host_for_autoretry(host_name, get_host_fn=database.get_host)


def _build_autoretry_tail(metadata: dict, *, retry_count: int, retry_limit: int, host_status: str) -> str:
    user_id = str(metadata.get("user_id") or "unknown")
    user = database.get_user(int(user_id)) if user_id.isdigit() else None
    username = str((user or {}).get("username") or "N/A")
    payment_id = str(metadata.get("external_payment_id") or metadata.get("payment_id") or "unknown")
    plan_name = str(metadata.get("plan_name") or "—")
    months = str(metadata.get("months") or "—")
    action = str(metadata.get("action") or "—")
    amount = metadata.get("price")
    payment_method = str(metadata.get("payment_method") or "—")
    paid_at = str(metadata.get("paid_at") or metadata.get("confirmed_at") or metadata.get("created_date") or "—")
    failure_reason = str(metadata.get("issuance_failure_reason") or "unknown")
    next_retry_at = str(metadata.get("next_retry_at") or "n/a")
    fields = [
        f"source={BOT_SOURCE}",
        "reason=autoretry_exhausted_host_dead",
        f"payment_id={payment_id}",
        f"user_id={user_id}",
        f"host={metadata.get('host_name', 'unknown')}",
        f"retry_count={retry_count}",
        f"retry_limit={retry_limit}",
        f"status={host_status}",
        "",
        f"Username = @{username}",
        f"Тариф = {plan_name}",
        f"Срок (мес) = {months}",
        f"Действие = {action}",
        f"Сумма = {amount}",
        f"Метод оплаты = {payment_method}",
        f"Дата оплаты = {paid_at}",
        f"Код ошибки = {failure_reason}",
        f"Следующая попытка = {next_retry_at}",
    ]
    if "yookassa" in payment_method.lower():
        fields.append(f"YooKassa payment_id = {payment_id}")
    return "\n".join(fields)


def _emit_autoretry_exhausted_alerts(*, max_attempts: int, limit: int) -> int:
    exhausted = 0
    candidates = database.get_pending_confirmed_transactions(
        limit=limit,
        min_age_seconds=0,
        due_only=False,
        retryable_only=True,
    )
    for tx in candidates:
        payment_id = str(tx.get("payment_id") or "").strip()
        if not payment_id:
            continue
        retry_count = int(tx.get("retry_count") or 0)
        if retry_count < max_attempts:
            continue

        metadata = normalize_payment_metadata(tx.get("metadata"))
        if not isinstance(metadata, dict):
            continue
        if metadata.get("autoretry_exhausted_alert_sent"):
            continue

        host_name = str(metadata.get("host_name") or "").strip()
        host_unhealthy, host_status = _host_is_unhealthy_for_autoretry(host_name)
        exhaustion_reason = (
            "autoretry_exhausted_host_dead"
            if host_unhealthy
            else "autoretry_exhausted_persistent_failure"
        )

        tail = _build_autoretry_tail(
            metadata,
            retry_count=retry_count,
            retry_limit=max_attempts,
            host_status=host_status,
        )
        tail = tail.replace("reason=autoretry_exhausted_host_dead", f"reason={exhaustion_reason}", 1)
        send_critical_alert_nonblocking(
            BotError.PAYMENT_KEY_ISSUE_FAILED,
            details=tail,
            respect_cooldown=False,
        )

        metadata["autoretry_exhausted_alert_sent"] = True
        metadata["autoretry_exhausted_alert_at"] = _naive_now().isoformat(timespec="seconds")
        metadata["retry_count"] = retry_count
        metadata["next_retry_at"] = tx.get("next_retry_at")
        database.update_transaction_metadata(
            payment_id,
            metadata,
            statuses=("pending", "processing"),
        )
        database.mark_transaction_exhausted(
            payment_id,
            reason=(
                f"retry_exhausted:attempts={retry_count},host_status={host_status},"
                f"reason={exhaustion_reason}"
            ),
        )
        exhausted += 1
    return exhausted


async def _retry_host_transactions_after_recovery(
    bot_controller: BotController,
    *,
    host_name: str,
) -> dict[str, object]:
    normalized_host = str(host_name or "").strip()
    if not normalized_host:
        return {"checked": 0, "recovered": 0, "errors": 0}
    result = await retry_pending_confirmed_transactions(
        bot_controller,
        target_host=normalized_host,
        retry_source="host_recovered_auto_retry",
        due_only=False,
        only_retryable_failed=True,
        min_age_seconds=0,
        limit=PAYMENT_ISSUANCE_AUTORETRY_LIMIT,
        emit_sla_alert=False,
    )
    checked = int(result.get("checked") or 0)
    recovered = int(result.get("recovered") or 0)
    errors = int(result.get("errors") or 0)
    if checked > 0 and errors > 0:
        logger.warning(
            "Auto-retry after host recovery finished with errors: source=%s host=%s checked=%s recovered=%s errors=%s",
            BOT_SOURCE,
            normalized_host,
            checked,
            recovered,
            errors,
        )
    return result


def _detect_and_fix_stuck_unretried(
    *,
    min_age_seconds: int = 1800,
) -> int:
    """Find confirmed transactions that the retry system has never picked up.

    For each stuck transaction, diagnose *why* it was skipped (bot_source
    mismatch, incomplete metadata) and either auto-fix or emit a targeted alert
    so operators know exactly what to do.

    Returns the number of transactions that were auto-fixed.
    """
    stuck = database.get_stuck_unretried_transactions(
        min_age_seconds=min_age_seconds,
        limit=20,
    )
    if not stuck:
        return 0

    fixed = 0
    for tx in stuck:
        payment_id = str(tx.get("payment_id") or "").strip()
        if not payment_id:
            continue

        raw_meta = tx.get("metadata")
        if isinstance(raw_meta, str):
            try:
                parsed = json.loads(raw_meta)
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            metadata = parsed if isinstance(parsed, dict) else {}
        elif isinstance(raw_meta, dict):
            metadata = raw_meta
        else:
            metadata = {}

        skip_reasons: list[str] = []

        if not _scheduler_metadata_bot_source_matches_runtime(metadata):
            meta_src = normalize_bot_source(metadata.get("bot_source")) or "unknown"
            runtime_src = normalize_bot_source(BOT_SOURCE) or "main"
            skip_reasons.append(f"bot_source_mismatch(meta={meta_src},runtime={runtime_src})")

        hydrated = _hydrate_retry_metadata_from_transaction(
            tx=tx,
            metadata=metadata,
            payment_id=payment_id,
            retry_source="stuck_detector",
        )
        hydrated = _normalize_retry_host_alias(hydrated)
        missing = missing_required_payment_metadata_fields(hydrated)
        if missing:
            skip_reasons.append(f"missing_fields({','.join(missing)})")

        if not skip_reasons:
            database.mark_transaction_retryable_failure(
                payment_id,
                reason="stuck_unretried_auto_fix",
                metadata=hydrated,
                payment_method=str(hydrated.get("payment_method") or ""),
                statuses=("pending",),
            )
            fixed += 1
            logger.warning(
                "Stuck-detector auto-fixed payment_id=%s (no skip reasons found, re-enqueued for retry)",
                payment_id,
            )
            continue

        logger.error(
            "Stuck-detector: payment_id=%s stuck for >%ds with retry_count=0. reasons=%s",
            payment_id,
            min_age_seconds,
            "; ".join(skip_reasons),
        )

    unfixable = len(stuck) - fixed
    if unfixable > 0:
        pids = [str(t.get("payment_id") or "?") for t in stuck][:5]
        send_critical_alert_nonblocking(
            BotError.PAYMENT_KEY_ISSUE_FAILED,
            details=(
                f"source={BOT_SOURCE} reason=stuck_unretried_detected "
                f"count={len(stuck)} auto_fixed={fixed} unfixable={unfixable} "
                f"min_age={min_age_seconds}s "
                f"payment_ids={', '.join(pids)}"
            ),
            respect_cooldown=True,
        )

    if fixed > 0:
        logger.info(
            "Stuck-detector: auto-fixed %d of %d stuck transactions",
            fixed,
            len(stuck),
        )

    return fixed


_autoretry_stale_counter = 0
_autoretry_stuck_counter = 0
_autoretry_consecutive_all_failed = 0
_AUTORETRY_STALE_CHECK_EVERY_N = 4  # ~60s at 15s interval
_AUTORETRY_STUCK_CHECK_EVERY_N = 40  # ~10min at 15s interval
_AUTORETRY_STUCK_MIN_AGE_SECONDS = 1800  # 30 min without retry
_AUTORETRY_CASCADE_THRESHOLD = 3
_AUTORETRY_CASCADE_BACKOFF_SECONDS = 60


async def _autoretry_startup(bot_controller: BotController) -> None:
    """One-time recovery on process start (stale processing + catch-up)."""
    try:
        stale_recovered = database.recover_stale_processing_transactions(
            stale_seconds=120,
            limit=PAYMENT_ISSUANCE_AUTORETRY_LIMIT,
        )
        if stale_recovered:
            stale_pids = [str(r.get("payment_id") or "?") for r in stale_recovered]
            logger.warning(
                "Recovered %d stale processing transactions at startup: %s",
                len(stale_recovered),
                stale_pids,
            )
    except Exception:
        logger.exception("Stale processing recovery crashed.")

    try:
        await reconcile_pending_platega_transactions(bot_controller)
    except Exception:
        logger.exception("Startup Platega reconcile crashed.")

    try:
        await reconcile_pending_lava_transactions(bot_controller)
    except Exception:
        logger.exception("Startup LAVA reconcile crashed.")

    try:
        await reconcile_pending_paypear_transactions(bot_controller)
    except Exception:
        logger.exception("Startup PayPear reconcile crashed.")

    try:
        startup_stats = await retry_pending_confirmed_transactions(
            bot_controller,
            retry_source="startup_catchup",
            due_only=False,
            only_retryable_failed=False,
            min_age_seconds=0,
            limit=PAYMENT_ISSUANCE_AUTORETRY_LIMIT * 2,
            emit_sla_alert=True,
        )
        startup_checked = int(startup_stats.get("checked") or 0)
        startup_recovered = int(startup_stats.get("recovered") or 0)
        startup_errors = int(startup_stats.get("errors") or 0)
        if startup_checked > 0:
            logger.info(
                "Startup catch-up completed: checked=%s recovered=%s errors=%s",
                startup_checked,
                startup_recovered,
                startup_errors,
            )
    except Exception:
        logger.exception("Startup catch-up crashed.")


@indestructible_loop(
    name="issuance_autoretry",
    interval_fn=lambda: _sleep_seconds_with_jitter(
        PAYMENT_ISSUANCE_AUTORETRY_INTERVAL_SECONDS,
        PAYMENT_ISSUANCE_AUTORETRY_JITTER_SECONDS,
    ),
)
async def _autoretry_loop_cycle(bot_controller: BotController) -> None:
    """Single cycle of the issuance autoretry worker."""
    global _autoretry_stale_counter, _autoretry_stuck_counter, _autoretry_consecutive_all_failed  # noqa: PLW0603

    if _autoretry_consecutive_all_failed >= _AUTORETRY_CASCADE_THRESHOLD:
        logger.warning(
            "Cascade backoff: %d consecutive all-failed cycles, sleeping %ds before next attempt",
            _autoretry_consecutive_all_failed,
            _AUTORETRY_CASCADE_BACKOFF_SECONDS,
        )
        await asyncio.sleep(_AUTORETRY_CASCADE_BACKOFF_SECONDS)

    _autoretry_stale_counter += 1
    if _autoretry_stale_counter >= _AUTORETRY_STALE_CHECK_EVERY_N:
        _autoretry_stale_counter = 0
        try:
            stale = database.recover_stale_processing_transactions(stale_seconds=120, limit=20)
            if stale:
                logger.warning("Recovered %d stale processing transactions in autoretry cycle", len(stale))
        except Exception:
            logger.exception("Stale processing recovery error.")

    try:
        await reconcile_pending_platega_transactions(bot_controller)
    except Exception:
        logger.exception("Platega reconcile cycle crashed.")

    try:
        await reconcile_pending_lava_transactions(bot_controller)
    except Exception:
        logger.exception("LAVA reconcile cycle crashed.")

    try:
        await reconcile_pending_paypear_transactions(bot_controller)
    except Exception:
        logger.exception("PayPear reconcile cycle crashed.")

    stats = await retry_pending_confirmed_transactions(
        bot_controller,
        retry_source="issuance_autoretry_worker",
        due_only=True,
        only_retryable_failed=True,
        min_age_seconds=0,
        limit=PAYMENT_ISSUANCE_AUTORETRY_LIMIT,
        emit_sla_alert=False,
    )
    exhausted = _emit_autoretry_exhausted_alerts(
        max_attempts=PAYMENT_ISSUANCE_AUTORETRY_MAX_ATTEMPTS,
        limit=PAYMENT_ISSUANCE_AUTORETRY_LIMIT,
    )
    cycle_checked = int(stats.get("checked") or 0)
    cycle_errors = int(stats.get("errors") or 0)
    cycle_recovered = int(stats.get("recovered") or 0)

    if cycle_checked > 0 or exhausted > 0:
        logger.info(
            "Issuance auto-retry cycle: checked=%s recovered=%s errors=%s exhausted=%s",
            cycle_checked,
            cycle_recovered,
            cycle_errors,
            exhausted,
        )

    if cycle_checked > 0 and cycle_errors > 0 and cycle_recovered == 0:
        _autoretry_consecutive_all_failed += 1
    else:
        _autoretry_consecutive_all_failed = 0

    _autoretry_stuck_counter += 1
    if _autoretry_stuck_counter >= _AUTORETRY_STUCK_CHECK_EVERY_N:
        _autoretry_stuck_counter = 0
        try:
            _detect_and_fix_stuck_unretried(min_age_seconds=_AUTORETRY_STUCK_MIN_AGE_SECONDS)
        except Exception:
            logger.exception("Stuck-transaction detector crashed.")


async def periodic_payment_issuance_autoretry(bot_controller: BotController) -> None:
    """Entry point: startup recovery then crash-proof autoretry loop."""
    logger.info(
        "Payment issuance auto-retry started. interval=%s sec, max_attempts=%s, limit=%s",
        PAYMENT_ISSUANCE_AUTORETRY_INTERVAL_SECONDS,
        PAYMENT_ISSUANCE_AUTORETRY_MAX_ATTEMPTS,
        PAYMENT_ISSUANCE_AUTORETRY_LIMIT,
    )
    await asyncio.sleep(
        _sleep_seconds_with_jitter(
            PAYMENT_ISSUANCE_AUTORETRY_INITIAL_DELAY_SECONDS,
            PAYMENT_ISSUANCE_AUTORETRY_JITTER_SECONDS,
        )
    )
    await _autoretry_startup(bot_controller)
    await _autoretry_loop_cycle(bot_controller)


@indestructible_loop(
    name="daily_catchup",
    interval_fn=lambda: float(PAYMENT_ISSUANCE_DAILY_CATCHUP_INTERVAL_SECONDS),
    initial_delay_fn=lambda: float(PAYMENT_ISSUANCE_DAILY_CATCHUP_INITIAL_DELAY_SECONDS),
)
async def periodic_payment_issuance_daily_catchup(bot_controller: BotController) -> None:
    """Single cycle of daily catch-up scan."""
    stats = await retry_pending_confirmed_transactions(
        bot_controller,
        retry_source="daily_catchup_scan",
        due_only=False,
        only_retryable_failed=False,
        min_age_seconds=0,
        limit=PAYMENT_ISSUANCE_DAILY_CATCHUP_LIMIT,
        emit_sla_alert=False,
    )
    checked = int(stats.get("checked") or 0)
    recovered = int(stats.get("recovered") or 0)
    errors = int(stats.get("errors") or 0)
    if checked > 0:
        logger.info(
            "Daily catch-up scan completed: checked=%s recovered=%s errors=%s",
            checked,
            recovered,
            errors,
        )

    await escalate_stale_pending_transactions()


STALE_PENDING_ESCALATION_MAX_AGE_HOURS = _get_env_positive_int(
    "STALE_PENDING_ESCALATION_MAX_AGE_HOURS",
    2,
    minimum=1,
)
STALE_PENDING_ESCALATION_EXHAUST_HOURS = _get_env_positive_int(
    "STALE_PENDING_ESCALATION_EXHAUST_HOURS",
    24,
    minimum=4,
)


async def escalate_stale_pending_transactions() -> dict[str, int]:
    """Alert on non-YooKassa pending transactions older than threshold.

    Transactions older than *exhaust_hours* are moved to exhausted state.
    """
    stale = database.get_stale_pending_by_provider(
        max_age_hours=STALE_PENDING_ESCALATION_MAX_AGE_HOURS,
        exclude_providers={"yookassa"},
        limit=30,
    )
    alerted = 0
    exhausted_count = 0
    for tx in stale:
        pid = tx["payment_id"]
        provider = tx["payment_method"]
        meta = tx.get("metadata") or {}
        user_id = meta.get("user_id") or meta.get("telegram_id") or "?"
        created = tx.get("created_date") or "?"

        age_str = f"created={created}"
        detail = (
            f"source={BOT_SOURCE} reason=stale_pending_escalation "
            f"provider={provider} payment_id={pid} user_id={user_id} {age_str}"
        )

        created_str = str(created).strip()
        try:
            created_dt = datetime.fromisoformat(created_str)
            age_hours = (_naive_utcnow() - created_dt).total_seconds() / 3600
        except (ValueError, TypeError):
            age_hours = 0

        if age_hours >= STALE_PENDING_ESCALATION_EXHAUST_HOURS and database.mark_transaction_exhausted(
            pid, reason="stale_pending_timeout"
        ):
            exhausted_count += 1
            _safe_alert(
                BotError.PAYMENT_KEY_ISSUE_FAILED,
                details=f"{detail} action=moved_to_exhausted age_hours={age_hours:.1f}",
            )
        alerted += 1

    if alerted:
        logger.info(
            "Stale pending escalation: alerted=%d exhausted=%d",
            alerted,
            exhausted_count,
        )
    return {"alerted": alerted, "exhausted": exhausted_count}


_TASK_TOKEN_RE = re.compile(r"#?(?P<id>\d{1,8})")


def _clean_deploy_context_value(raw: str | None, *, max_len: int = 240) -> str:
    value = " ".join(str(raw or "").strip().split())
    if not value:
        return ""
    return value[:max_len]


def _extract_task_token(raw_task: str) -> str:
    match = _TASK_TOKEN_RE.search(raw_task)
    if not match:
        return ""
    return match.group("id")


def _build_deploy_restart_alert_details() -> str:
    task = _clean_deploy_context_value(os.environ.get("DEPLOY_LAST_TASK"))
    commit_short = _clean_deploy_context_value(os.environ.get("DEPLOY_LAST_COMMIT_SHORT_SHA"), max_len=64)
    commit_title = _clean_deploy_context_value(os.environ.get("DEPLOY_LAST_COMMIT_TITLE"))
    ref_name = _clean_deploy_context_value(os.environ.get("DEPLOY_LAST_REF_NAME"), max_len=128)
    pipeline_url = _clean_deploy_context_value(os.environ.get("DEPLOY_LAST_PIPELINE_URL"))

    details = f"source={BOT_SOURCE} reason=deploy_restart_ok"
    task_token = _extract_task_token(task)
    if task_token:
        details = f"{details} task={task_token}"
    if commit_short:
        details = f"{details} commit={commit_short}"
    if ref_name:
        details = f"{details} ref={ref_name}"
    if pipeline_url:
        details = f"{details} pipeline={pipeline_url}"

    lines: list[str] = []
    if task:
        lines.append(f"Задача: {task}")
    if commit_short:
        lines.append(f"Коммит: {commit_short}")
    if commit_title:
        lines.append(f"Изменение: {commit_title}")
    if ref_name:
        lines.append(f"Ветка: {ref_name}")
    if pipeline_url:
        lines.append(f"Pipeline: {pipeline_url}")

    if lines:
        details = f"{details}\n" + "\n".join(lines)
    return details


def process_shop_watchdog_cycle(
    bot_controller: BotController,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """
    Run one watchdog cycle.

    Kept as a standalone function for deterministic unit testing.
    """
    global _shop_watchdog_transient_unhealthy_cycles  # noqa: PLW0603 - watchdog cycle state is module-local.
    global _shop_watchdog_transient_unhealthy_since  # noqa: PLW0603 - watchdog cycle state is module-local.

    cycle_now = now or _naive_now()

    status = bot_controller.get_status(include_health=True) or {}
    shop_running = bool(status.get("shop_bot_running"))
    shop_starting = bool(status.get("shop_bot_starting"))
    shop_healthy = bool(status.get("shop_bot_healthy", shop_running))
    shop_health_message = str(status.get("shop_bot_health_message") or "")
    desired_running = bool(status.get("shop_bot_desired_running"))

    # Process is shutting down (SIGTERM/deploy): watchdog should not intervene.
    if getattr(bot_controller, "_shutting_down", False):
        logger.debug("Shop watchdog: process is shutting down; skip.")
        _shop_watchdog_transient_unhealthy_cycles = 0
        _shop_watchdog_transient_unhealthy_since = None
        return {"attempted_restart": False, "strict_reason": False, "alert_sent": False}

    if not desired_running:
        logger.debug("Shop watchdog: desired state is stopped; skip auto-restart.")
        _shop_watchdog_transient_unhealthy_cycles = 0
        _shop_watchdog_transient_unhealthy_since = None
        return {"attempted_restart": False, "strict_reason": False, "alert_sent": False}

    if shop_starting:
        logger.debug("Shop watchdog: Shop bot is already starting; skip duplicate start.")
        _shop_watchdog_transient_unhealthy_cycles = 0
        _shop_watchdog_transient_unhealthy_since = None
        return {"attempted_restart": False, "strict_reason": False, "alert_sent": False}

    if shop_running and shop_healthy:
        logger.debug("Shop watchdog: Shop bot is healthy.")
        _shop_watchdog_transient_unhealthy_cycles = 0
        _shop_watchdog_transient_unhealthy_since = None
        return {"attempted_restart": False, "strict_reason": False, "alert_sent": False}

    if shop_running and not shop_healthy:
        normalized_health_message = shop_health_message.strip().lower()
        transient_unhealthy = normalized_health_message == "probe_timeout" or normalized_health_message.startswith(
            "probe_error:"
        )
        if transient_unhealthy:
            _shop_watchdog_transient_unhealthy_cycles = min(
                _shop_watchdog_transient_unhealthy_cycles + 1,
                10000,
            )
            if _shop_watchdog_transient_unhealthy_since is None:
                _shop_watchdog_transient_unhealthy_since = cycle_now
            elapsed_seconds = max(
                0.0,
                (cycle_now - _shop_watchdog_transient_unhealthy_since).total_seconds(),
            )
            if elapsed_seconds < float(SHOP_WATCHDOG_TRANSIENT_UNHEALTHY_MAX_SECONDS):
                logger.warning(
                    "Shop watchdog: transient unhealthy probe (%s), cycle=%s, elapsed=%.1fs. Keep runtime running.",
                    shop_health_message or "unknown",
                    _shop_watchdog_transient_unhealthy_cycles,
                    elapsed_seconds,
                )
                return {"attempted_restart": False, "strict_reason": False, "alert_sent": False}
            # SAFETY: Never stop a running bot due to probe timeouts.
            # External hosts being down causes probe contention — the bot
            # polling is still alive.  Just warn and keep going.
            logger.warning(
                "Shop watchdog: transient unhealthy probe persisted %.1fs (>= %ss). "
                "Keeping runtime alive — probe issues do not justify stopping a running bot.",
                elapsed_seconds,
                SHOP_WATCHDOG_TRANSIENT_UNHEALTHY_MAX_SECONDS,
            )
            send_critical_alert_nonblocking(
                BotError.TELEGRAM_API_ERROR,
                details=(
                    f"source={BOT_SOURCE} "
                    "reason=watchdog_probe_timeout_persistent "
                    f"\nHealth: {shop_health_message or 'unknown'}"
                    f"\nПроблема длится {int(elapsed_seconds)} сек. Бот продолжает работу."
                ),
                respect_cooldown=True,
            )
            return {"attempted_restart": False, "strict_reason": False, "alert_sent": True}
        _shop_watchdog_transient_unhealthy_cycles = 0
        _shop_watchdog_transient_unhealthy_since = None

        # SAFETY: Never stop a running bot due to health probe issues.
        # Probe timeouts happen when external hosts are down — the bot itself
        # (polling + message handling) is still alive.  Stopping it makes things
        # worse because restart may fail, leaving the bot completely dead.
        # Only restart bots that have actually crashed (shop_running == False).
        logger.warning(
            "Shop watchdog: Bot is running but health probe reports unhealthy (%s). "
            "Keeping runtime alive — only crashed bots get restarted.",
            shop_health_message or "unknown",
        )
        send_critical_alert_nonblocking(
            BotError.BOT_CRASH,
            details=(
                f"source={BOT_SOURCE} "
                "reason=bot_unhealthy_but_running "
                f"\nHealth: {shop_health_message or 'unknown'}"
                "\nBot is kept running. Only truly crashed bots get restarted."
            ),
            respect_cooldown=True,
        )
        return {"attempted_restart": False, "strict_reason": False, "alert_sent": True}

    logger.warning("Shop watchdog: Shop bot is down unexpectedly. Starting recovery...")
    _shop_watchdog_transient_unhealthy_cycles = 0
    _shop_watchdog_transient_unhealthy_since = None

    raw_failure = getattr(bot_controller, "shop_last_failure", None)
    is_deploy_restart = raw_failure is None
    crash_reason = raw_failure or "Обновление кода (деплой)"

    if not is_deploy_restart:
        send_critical_alert_nonblocking(
            BotError.BOT_CRASH,
            details=(f"source={BOT_SOURCE} reason=bot_down_detected \nПадение: {crash_reason}"),
            respect_cooldown=True,
        )

    # Try auto-restart first, then report the outcome.
    start_result = bot_controller.start_shop_bot(initiated_by="watchdog") or {}

    if start_result.get("status") == "success":
        logger.info("Shop watchdog: Auto-restart command sent successfully.")
        if is_deploy_restart:
            send_critical_alert_nonblocking(
                BotError.BOT_DEPLOY_RESTARTED,
                details=_build_deploy_restart_alert_details(),
                respect_cooldown=False,
            )
        else:
            send_critical_alert_nonblocking(
                BotError.BOT_AUTO_RESTART,
                details=(f"source={BOT_SOURCE} reason=bot_recovered_after_crash \nПадение: {crash_reason}"),
                respect_cooldown=False,
            )
        return {"attempted_restart": True, "strict_reason": False, "alert_sent": True}

    message = start_result.get("message", "Unknown start failure")
    strict_reason = bool(start_result.get("strict_reason"))
    if strict_reason and BotController._is_retryable_shop_start_error(message):
        logger.warning(
            "Shop watchdog: Startup failure is retryable (%s). Keeping auto-restart enabled.",
            message,
        )
        strict_reason = False

    if not strict_reason:
        logger.warning("Shop watchdog: Startup failed, will retry next cycle. Reason: %s", message)
        classified = classify_error(crash_reason)
        send_critical_alert_nonblocking(
            classified if classified != BotError.UNKNOWN else BotError.BOT_CRASH,
            details=(
                f"source={BOT_SOURCE} "
                "reason=watchdog_retry "
                f"\nПадение: {crash_reason}"
                f"\nАвторестарт не удался: {message}"
                "\nСледующая попытка через 60 сек."
            ),
            respect_cooldown=False,
        )
        return {"attempted_restart": True, "strict_reason": False, "alert_sent": True}

    # Strict failure — keep desired state running.
    # Always-on policy: watchdog must keep retrying on next cycles,
    # while surfacing an explicit critical alert for operators.
    logger.error(
        "Shop watchdog: Strict startup failure. Keep auto-restart active. Reason: %s",
        message,
    )
    bot_controller.shop_desired_running = True
    bot_controller.shop_autorestart_paused = False

    classified = classify_error(message)
    send_critical_alert_nonblocking(
        classified if classified != BotError.UNKNOWN else BotError.BOT_START_FAILED,
        details=(
            f"source={BOT_SOURCE} "
            "reason=watchdog_strict_failure "
            f"\nПричина: {message}"
            "\nАвтозапуск продолжает попытки в следующих циклах."
        ),
        respect_cooldown=True,
    )

    return {"attempted_restart": True, "strict_reason": True, "alert_sent": True}


def format_time_left(hours: int) -> str:
    if hours >= 24:
        days = hours // 24
        if days % 10 == 1 and days % 100 != 11:
            return f"{days} день"
        if 2 <= days % 10 <= 4 and (days % 100 < 10 or days % 100 >= 20):
            return f"{days} дня"
        return f"{days} дней"
    if hours % 10 == 1 and hours % 100 != 11:
        return f"{hours} час"
    if 2 <= hours % 10 <= 4 and (hours % 100 < 10 or hours % 100 >= 20):
        return f"{hours} часа"
    return f"{hours} часов"


async def send_subscription_notification(
    bot: Bot,
    user_id: int,
    key_id: int,
    time_left_hours: int,
    expiry_date: datetime,
) -> bool:
    try:
        time_text = format_time_left(time_left_hours)
        expiry_str = expiry_date.strftime("%d.%m.%Y в %H:%M")

        message = (
            f"⚠️ **Внимание!** ⚠️\n\n"
            f"Срок действия вашей подписки истекает через **{time_text}**.\n"
            f"Дата окончания: **{expiry_str}**\n\n"
            f"Продлите подписку, чтобы не остаться без доступа к VPN!"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="🔑 Мои ключи", callback_data="manage_keys")
        builder.button(text="🔁 Продлить ключ", callback_data=f"extend_key_{key_id}")
        builder.adjust(2)

        await bot.send_message(chat_id=user_id, text=message, reply_markup=builder.as_markup(), parse_mode="Markdown")
        logger.info(
            "Sent subscription notification to user %s for key %s (%s hours left).",
            user_id,
            key_id,
            time_left_hours,
        )
    except Exception:
        logger.exception("Error sending subscription notification to user %s.", user_id)
        return False
    return True


async def check_expiring_subscriptions(bot: Bot):
    logger.info("Scheduler: Checking for expiring subscriptions...")
    current_time = _naive_now()
    all_keys = database.get_all_keys(bot_source=normalize_bot_source(BOT_SOURCE) or "main")
    pruned = database.prune_expiry_notifications(NOTIFICATION_HISTORY_RETENTION_DAYS)
    if pruned > 0:
        logger.info("Scheduler: Pruned %s old expiry notification records.", pruned)

    for key in all_keys:
        try:
            expiry_raw = key.get("expiry_date")
            if not expiry_raw:
                continue
            expiry_date = datetime.fromisoformat(expiry_raw)
            time_left = expiry_date - current_time

            time_left_seconds = time_left.total_seconds()
            if time_left_seconds <= 0:
                continue
            if time_left_seconds > SUBSCRIPTION_NOTIFY_WINDOW_HOURS * 3600:
                continue

            user_id = key["user_id"]
            key_id = key["key_id"]
            total_hours_left = max(1, int((time_left_seconds + 3599) // 3600))

            if database.was_expiry_notification_sent(key_id, expiry_raw):
                continue
            sent = await send_subscription_notification(
                bot,
                user_id,
                key_id,
                total_hours_left,
                expiry_date,
            )
            if sent:
                database.mark_expiry_notification_sent(key_id, expiry_raw)

        except Exception:
            logger.exception("Error processing expiry for key %s.", key.get("key_id"))


def _format_whitelist_reset_date(reset_timestamp_ms: int | None) -> str:
    try:
        value = int(reset_timestamp_ms or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return "не запланирована"
    return datetime.fromtimestamp(value / 1000, tz=_MOSCOW_TZ).strftime("%d.%m.%Y в %H:%M МСК")


def build_whitelist_limit_message(reset_timestamp_ms: int | None) -> str:
    reset_text = _format_whitelist_reset_date(reset_timestamp_ms)
    return (
        "Здравствуйте! У вас закончился лимит 40 ГБ по серверу WHITELIST.\n\n"
        "Остальные серверы продолжат работу, они безлимитные.\n\n"
        f"Следующая дата сброса лимита WHITELIST: <b>{reset_text}</b>.\n"
        "После сброса снова появится 40 ГБ.\n\n"
        "Можете подождать либо докупить еще одну супер-подписку в нашем боте @probelvpnbot"
    )


def _normalize_email_for_whitelist_snapshot(value: object) -> str:
    return str(value or "").strip().lower()


def _is_expected_secondary_whitelist_snapshot(
    base_email: str,
    stat: Mapping[str, Any],
    *,
    host_name: str = "",
) -> bool:
    normalized_base = _normalize_email_for_whitelist_snapshot(base_email)
    normalized_candidate = _normalize_email_for_whitelist_snapshot(stat.get("email"))
    if (
        not normalized_base
        or not normalized_candidate
        or normalized_candidate == normalized_base
        or bool(stat.get("identity_collision"))
    ):
        return False
    inbound_id = int(stat.get("inbound_id") or 0)
    inbound_label = str(stat.get("inbound_label") or "").strip()
    if inbound_id <= 0 or not inbound_label:
        return False
    expected = xui_api.build_host_specific_email(
        normalized_base,
        xui_api.subscription_policy.client_identity_suffix(host_name, inbound_id, inbound_label),
    )
    return normalized_candidate == _normalize_email_for_whitelist_snapshot(expected)


def _refresh_whitelist_limit_statuses() -> dict[str, Any]:
    host_name = (database.get_setting("super_key_host_name") or "MainPanel").strip() or "MainPanel"
    runtime_source = normalize_bot_source(BOT_SOURCE) or "main"
    snapshot = xui_api.get_whitelist_traffic_snapshot(host_name)
    cycle_guard = {
        "checked": 0,
        "already_correct": 0,
        "updated": 0,
        "needs_review": 0,
        "failed": 0,
    }
    if runtime_source != "reserve":
        try:
            cycle_guard = xui_api.reconcile_active_whitelist_reset_cycles(
                host_name,
                snapshot=snapshot,
            )
            if cycle_guard.get("updated"):
                # Notifications and admin cache must use the verified post-write
                # state, never the stale snapshot that triggered reconciliation.
                snapshot = xui_api.get_whitelist_traffic_snapshot(host_name)
        except Exception:
            logger.exception(
                "WHITELIST cycle guard failed for host=%s; keeping the read-only cache refresh.",
                host_name,
            )
            cycle_guard["failed"] = max(1, int(cycle_guard.get("failed") or 0))

    keys = database.get_active_whitelist_limit_keys(
        host_name=host_name,
        bot_source=runtime_source,
    )
    if not keys:
        return {
            "host_name": host_name,
            "keys": 0,
            "updated": 0,
            "notify": [],
            "cycle_guard": cycle_guard,
        }

    previous_statuses = database.get_whitelist_traffic_statuses_by_key_ids(
        [int(key.get("key_id") or 0) for key in keys]
    )
    by_email = {
        _normalize_email_for_whitelist_snapshot(key.get("key_email")): key
        for key in keys
        if _normalize_email_for_whitelist_snapshot(key.get("key_email"))
    }
    snapshot_by_sub_id = {
        str(stat.get("sub_id") or "").strip(): stat
        for stat in snapshot.values()
        if str(stat.get("sub_id") or "").strip()
    }
    snapshot_by_client_uuid: dict[str, list[dict[str, Any]]] = {}
    for stat in snapshot.values():
        client_uuid = str(stat.get("client_uuid") or "").strip().lower()
        if client_uuid:
            snapshot_by_client_uuid.setdefault(client_uuid, []).append(stat)
    host_data = database.get_host(host_name) or {}
    try:
        primary_inbound_id = int(host_data.get("host_inbound_id") or 0)
    except (TypeError, ValueError):
        primary_inbound_id = 0
    whitelist_inbound_ids = {
        int(stat.get("inbound_id") or 0)
        for stat in snapshot.values()
        if int(stat.get("inbound_id") or 0) > 0
    }
    primary_is_whitelist = (
        primary_inbound_id > 0 and primary_inbound_id in whitelist_inbound_ids
    )
    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    rows: list[dict] = []
    notify_rows: list[dict] = []

    for email, key in by_email.items():
        sub_id = str(key.get("panel_sub_id") or "").strip()
        client_uuid = str(key.get("xui_client_uuid") or "").strip().lower()
        uuid_stats = snapshot_by_client_uuid.get(client_uuid, [])
        preferred_secondary = None
        if not primary_is_whitelist:
            secondary_candidates = [
                candidate
                for candidate in uuid_stats
                if _is_expected_secondary_whitelist_snapshot(email, candidate, host_name=host_name)
            ]
            if secondary_candidates:
                preferred_secondary = sorted(
                    secondary_candidates,
                    key=lambda candidate: (
                        bool(candidate.get("identity_collision")),
                        int(candidate.get("inbound_id") or 0),
                        _normalize_email_for_whitelist_snapshot(candidate.get("email")),
                    ),
                )[0]
        if primary_is_whitelist:
            stat = (
                snapshot.get(email)
                or snapshot_by_sub_id.get(sub_id)
                or (uuid_stats[0] if uuid_stats else None)
            )
        else:
            # A base-email row on a secondary WHITELIST inbound is an unsafe
            # replica, not this subscription's traffic counter.  Never fall
            # back to it when the exact suffix is absent or ambiguous.
            stat = preferred_secondary
        if not stat or bool(stat.get("identity_collision")):
            logger.warning(
                "WHITELIST cache skipped unsafe identity: key_id=%s email=%s primary_inbound_id=%s",
                key.get("key_id"),
                email,
                primary_inbound_id,
            )
            continue
        try:
            key_id = int(key.get("key_id") or 0)
            user_id = int(key.get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        total_bytes = max(0, int(stat.get("total_bytes") or 0))
        used_bytes = max(0, int(stat.get("used_bytes") or 0))
        reset_timestamp_ms = max(0, int(stat.get("reset_timestamp_ms") or 0))
        exhausted = bool(total_bytes > 0 and used_bytes >= total_bytes)
        row = {
            "key_id": key_id,
            "user_id": user_id,
            "key_email": key.get("key_email"),
            "host_name": key.get("host_name") or host_name,
            "bot_source": key.get("bot_source") or runtime_source,
            "used_bytes": used_bytes,
            "total_bytes": total_bytes,
            "reset_days": int(stat.get("reset_days") or 0),
            "reset_timestamp_ms": reset_timestamp_ms,
            "panel_enable": bool(stat.get("panel_enable", True)),
            "exhausted": exhausted,
        }
        rows.append(row)

        previous = previous_statuses.get(key_id)
        was_exhausted = bool(previous and previous.get("exhausted"))
        is_paid_super_limit = total_bytes >= xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES
        if (
            previous
            and exhausted
            and not was_exhausted
            and is_paid_super_limit
            and reset_timestamp_ms > now_ms
            and not database.has_whitelist_limit_notification(key_id, reset_timestamp_ms)
        ):
            notify_rows.append(row)

    updated = database.upsert_whitelist_traffic_status(rows)
    return {
        "host_name": host_name,
        "keys": len(keys),
        "updated": updated,
        "notify": notify_rows,
        "cycle_guard": cycle_guard,
    }


async def check_whitelist_limit_notifications(bot: Bot) -> dict[str, Any]:
    stats = await asyncio.to_thread(_refresh_whitelist_limit_statuses)
    notify_rows = list(stats.get("notify") or [])
    sent = 0
    for row in notify_rows:
        key_id = int(row.get("key_id") or 0)
        user_id = int(row.get("user_id") or 0)
        reset_timestamp_ms = int(row.get("reset_timestamp_ms") or 0)
        if key_id <= 0 or user_id <= 0 or reset_timestamp_ms <= 0:
            continue
        message = build_whitelist_limit_message(reset_timestamp_ms)
        try:
            await bot.send_message(chat_id=user_id, text=message, parse_mode="HTML")
        except Exception:
            logger.exception(
                "Failed to send WHITELIST limit notification: user_id=%s key_id=%s",
                user_id,
                key_id,
            )
            continue
        if await asyncio.to_thread(database.mark_whitelist_limit_notification_sent, key_id, reset_timestamp_ms):
            sent += 1
            logger.info(
                "Sent WHITELIST limit notification: user_id=%s key_id=%s reset=%s.",
                user_id,
                key_id,
                reset_timestamp_ms,
            )

    pruned = await asyncio.to_thread(database.prune_whitelist_limit_notifications, NOTIFICATION_HISTORY_RETENTION_DAYS)
    stats["sent"] = sent
    stats["pruned"] = pruned
    return stats


def _parse_expiry_date(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _parse_blitz_expiry(
    user_data: dict,
    now: datetime,
    *,
    local_created_at: datetime | None = None,
) -> tuple[datetime | None, str | None]:
    """
    Best-effort parser for Blitz user expiry fields.
    Supports timestamp fields (sec/ms), ISO date strings, or remaining day counters.
    """
    if not isinstance(user_data, dict):
        return None, None

    timestamp_fields = (
        "expiry_time",
        "expires_at",
        "expire_at",
        "expiration_timestamp",
        "expiration_time",
        "expiry_timestamp",
    )
    for field in timestamp_fields:
        value = user_data.get(field)
        if value is None:
            continue
        try:
            numeric = int(float(value))
            # Heuristic: >= 10^12 means milliseconds.
            if numeric >= 1_000_000_000_000:
                numeric = numeric / 1000
            return _naive_fromtimestamp(numeric), "absolute"
        except (TypeError, ValueError, OSError):
            continue

    string_fields = ("expiry_date", "expires_at", "expire_at", "expiration_date")
    for field in string_fields:
        value = user_data.get(field)
        if not value or not isinstance(value, str):
            continue
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized).replace(tzinfo=None), "absolute"
        except ValueError:
            continue

    # Some Blitz versions expose "expiration_days" as plan duration from account
    # creation (static), not "remaining days from now". Treat it as absolute only
    # when account_creation_date is available, otherwise ignore to avoid drift.
    expiration_days = user_data.get("expiration_days")
    creation_date_raw = user_data.get("account_creation_date")
    if expiration_days is not None and creation_date_raw:
        try:
            base_date = datetime.fromisoformat(str(creation_date_raw))
            if local_created_at is not None:
                base_date = base_date.replace(
                    hour=local_created_at.hour,
                    minute=local_created_at.minute,
                    second=local_created_at.second,
                    microsecond=local_created_at.microsecond,
                )
            return base_date + timedelta(days=max(int(expiration_days), 0)), "absolute_from_creation"
        except (TypeError, ValueError):
            pass

    days_fields = ("remaining_days", "days_left")
    for field in days_fields:
        value = user_data.get(field)
        if value is None:
            continue
        try:
            days = int(value)
            return now + timedelta(days=max(days, 0)), "relative_days"
        except (TypeError, ValueError):
            continue

    return None, None


async def _sync_blitz_keys() -> int:
    logger.info("Scheduler: Starting Blitz keys sync...")
    affected_records = 0
    now = _naive_now()
    blitz_keys = database.get_keys_for_host(blitz_api.BLITZ_HOST_NAME)

    if not blitz_keys:
        logger.info("Scheduler: No Blitz keys found in database.")
        return 0

    blitz_enabled = blitz_api.is_blitz_enabled()
    if not blitz_enabled:
        logger.warning("Scheduler: Blitz integration is disabled. Running local cleanup for expired Blitz keys only.")

    for db_key in blitz_keys:
        key_id = db_key.get("key_id")
        key_email = db_key.get("key_email")
        blitz_username = db_key.get("xui_client_uuid") or key_email

        try:
            expiry_date = _parse_expiry_date(db_key.get("expiry_date"))
            if not expiry_date:
                logger.error(
                    "Scheduler: Blitz key %s has invalid expiry_date in DB (key_email='%s'). Skipping.",
                    key_id,
                    key_email,
                )
                continue

            if not blitz_enabled:
                if expiry_date < now - timedelta(days=EXPIRY_DELETE_GRACE_DAYS):
                    database.delete_key_by_email(key_email)
                    affected_records += 1
                    logger.info(
                        "Scheduler: Deleted expired Blitz key '%s' from DB (Blitz disabled).",
                        key_email,
                    )
                continue

            if expiry_date < now - timedelta(days=EXPIRY_DELETE_GRACE_DAYS):
                deleted_on_blitz = await blitz_api.delete_user(blitz_username)
                if deleted_on_blitz:
                    database.delete_key_by_email(key_email)
                    affected_records += 1
                    logger.info("Scheduler: Deleted expired Blitz key '%s' from panel and DB.", key_email)
                else:
                    logger.error(
                        "Scheduler: Failed to delete expired Blitz user '%s' for key '%s'.",
                        blitz_username,
                        key_email,
                    )
                continue

            user_data = await blitz_api.get_user(blitz_username)
            if user_data is None:
                logger.warning(
                    "Scheduler: Blitz user '%s' not available for sync. Keeping local key '%s'.",
                    blitz_username,
                    key_email,
                )
                continue

            local_created_at = _parse_expiry_date(db_key.get("created_date"))
            server_expiry, expiry_source = _parse_blitz_expiry(
                user_data,
                now,
                local_created_at=local_created_at,
            )
            if server_expiry is not None:
                server_expiry_ms = int(server_expiry.timestamp() * 1000)
                local_expiry_ms = int(expiry_date.timestamp() * 1000)
                local_ahead_by_ms = local_expiry_ms - server_expiry_ms

                # If paid local expiry is at least 1 day ahead of panel, heal panel state.
                # This prevents accidental "month cut" from shrinking entitlement.
                if local_ahead_by_ms >= 24 * 3600 * 1000:
                    desired_days = subscription_dates.panel_days_until(expiry_date, now=now)
                    updated = await blitz_api.update_user(
                        blitz_username,
                        new_expiration_days=desired_days,
                    )
                    if updated:
                        affected_records += 1
                        logger.info(
                            "Scheduler: Extended Blitz user '%s' to match local expiry for key '%s' (%s days).",
                            blitz_username,
                            key_email,
                            desired_days,
                        )
                    else:
                        logger.error(
                            "Scheduler: Failed to extend Blitz user '%s' for key '%s' (desired_days=%s).",
                            blitz_username,
                            key_email,
                            desired_days,
                        )
                    # Keep local paid expiry; do not overwrite with shorter panel value.
                    continue

                should_update = abs(server_expiry_ms - local_expiry_ms) > 1000
                if expiry_source == "relative_days":
                    # remaining_days-like values can drift every sync cycle;
                    # update only when difference reaches at least one day.
                    should_update = abs(server_expiry_ms - local_expiry_ms) >= 24 * 3600 * 1000

                if should_update:
                    database.update_key_info(key_id, db_key["xui_client_uuid"], server_expiry_ms)
                    affected_records += 1
                    logger.debug("Scheduler: Updated expiry for Blitz key '%s'.", key_email)

        except Exception:
            logger.exception(
                "Scheduler: Error syncing Blitz key '%s' (user='%s')",
                key_email,
                blitz_username,
            )

    logger.info("Scheduler: Blitz sync finished. Total records affected: %s.", affected_records)
    return affected_records


async def sync_keys_with_panels():
    """
    Sync local database with XUI panels.

    This function runs periodically and:
    - Updates expiry dates from panels
    - Deletes expired keys (>5 days old)
    - Detects orphaned clients
    - Syncs Blitz (Hysteria2) keys via Blitz API

    Errors on individual hosts do not stop the sync for other hosts.
    """
    logger.info("Scheduler: Starting sync with XUI panels...")
    sync_start_memory = runtime_guard.get_memory_snapshot()
    memory_pressure_level = runtime_guard.memory_pressure_level(sync_start_memory)
    skip_xui_sync = memory_pressure_level is not None
    if skip_xui_sync:
        logger.warning(
            "Scheduler: Skipping XUI panel sync due to %s memory pressure (%s).",
            memory_pressure_level,
            runtime_guard.format_memory_snapshot(sync_start_memory),
        )
        runtime_guard.emit_memory_pressure_alert_if_needed(
            reason="scheduler_sync_memory_guard",
            snapshot=sync_start_memory,
        )
    else:
        logger.info(
            "Scheduler: XUI sync memory before cycle: %s",
            runtime_guard.format_memory_snapshot(sync_start_memory),
        )
    total_affected_records = 0
    successful_hosts = 0
    failed_hosts = 0

    all_hosts = [] if skip_xui_sync else database.get_all_hosts()
    if skip_xui_sync:
        logger.warning("Scheduler: XUI hosts skipped by memory guard; continuing with non-XUI sync tasks.")
    elif not all_hosts:
        logger.info("Scheduler: No XUI hosts configured in the database. Continuing with Blitz sync.")
    else:
        loop = asyncio.get_running_loop()

        for host in all_hosts:
            host_name = host["host_name"]
            host_url = host["host_url"]
            host_was_unhealthy = _is_unhealthy_host_status(host.get("status"))

            if host.get("is_maintenance"):
                logger.info("Scheduler: Host '%s' is in maintenance mode. Skipping.", host_name)
                continue

            host_memory_before = runtime_guard.get_memory_snapshot()
            host_memory_pressure_level = runtime_guard.memory_pressure_level(host_memory_before)
            if host_memory_pressure_level is not None:
                logger.warning(
                    "Scheduler: Skipping remaining XUI panel sync due to %s memory pressure before host '%s' (%s).",
                    host_memory_pressure_level,
                    host_name,
                    runtime_guard.format_memory_snapshot(host_memory_before),
                )
                runtime_guard.emit_memory_pressure_alert_if_needed(
                    reason="scheduler_sync_memory_guard",
                    snapshot=host_memory_before,
                )
                break

            logger.info("Scheduler: Processing host: '%s' (%s)", host_name, host_url)

            try:
                # Run blocking XUI API calls in executor to avoid freezing the event loop.
                # Without this, a slow/hanging host blocks ALL bot message processing.
                login_result, current_status, _ = await asyncio.wait_for(
                    loop.run_in_executor(None, _probe_host_health, host),
                    timeout=SYNC_PER_HOST_TIMEOUT_SECONDS,
                )
                api, inbound = login_result.api, login_result.inbound

                if not api or not inbound:
                    logger.error(
                        "Scheduler: Could not access host '%s' (%s). code=%s status=%s error=%s",
                        host_name,
                        host_url,
                        login_result.error_code,
                        current_status,
                        login_result.error_message,
                    )
                    failed_hosts += 1
                    continue

                if login_result.recovered_on_retry:
                    logger.warning(
                        "Scheduler: Host '%s' recovered after transient panel failure during this cycle.",
                        host_name,
                    )

                panel_scan: xui_api.PanelClientScan | None = None
                clients_on_server: dict[str, xui_api.PanelClientSummary] = {}
                panel_scan_started = False
                panel_scan_loaded = False
                try:
                    keys_in_db = database.get_keys_for_host(host_name)
                    local_key_emails = {
                        str(db_key.get("key_email") or "").strip()
                        for db_key in keys_in_db
                        if str(db_key.get("key_email") or "").strip()
                    }
                    panel_scan_started = True
                    scan_callable = xui_api.scan_inbound_clients_minimal
                    scan_args: tuple[Any, ...] = (
                        api,
                        inbound.id,
                        local_key_emails,
                        PANEL_SYNC_ORPHAN_SAMPLE_LIMIT,
                    )
                    if str(host_name or "").strip() == "MainPanel":
                        scan_callable = xui_api.scan_subscription_inbounds_clients_minimal
                        scan_args = (
                            api,
                            inbound,
                            host_name,
                            local_key_emails,
                            PANEL_SYNC_ORPHAN_SAMPLE_LIMIT,
                        )
                    panel_scan = await asyncio.wait_for(
                        loop.run_in_executor(
                            None,
                            scan_callable,
                            *scan_args,
                        ),
                        timeout=SYNC_PER_HOST_TIMEOUT_SECONDS,
                    )
                    panel_scan_loaded = True
                    clients_on_server = panel_scan.clients_by_email
                    logger.info(
                        "Scheduler: Found %s local clients and %s orphan clients on the '%s' panel.",
                        len(clients_on_server),
                        panel_scan.orphan_total,
                        host_name,
                    )

                    for key_index, db_key in enumerate(keys_in_db):
                        if key_index > 0 and key_index % SYNC_KEYS_DB_YIELD_EVERY == 0:
                            await asyncio.sleep(0)
                        key_email = db_key["key_email"]
                        expiry_date = datetime.fromisoformat(db_key["expiry_date"])
                        now = datetime.now(UTC).replace(tzinfo=None)
                        expiry_delete_grace_days = _expiry_delete_grace_days(db_key)
                        if expiry_date <= now - timedelta(days=expiry_delete_grace_days):
                            logger.info(
                                "Scheduler: Key '%s' expired more than %s days ago. Deleting from panel and DB.",
                                key_email,
                                expiry_delete_grace_days,
                            )
                            deleted_on_panel = False
                            try:
                                deleted_on_panel = bool(await xui_api.delete_client_on_host(host_name, key_email))
                            except (
                                OSError,
                                RuntimeError,
                                ValueError,
                                TypeError,
                                KeyError,
                                AttributeError,
                            ):
                                logger.exception("Scheduler: Failed to delete client '%s' from panel.", key_email)
                            if not deleted_on_panel:
                                logger.error(
                                    "Scheduler: Keeping expired key '%s' in DB because panel deletion was not confirmed.",
                                    key_email,
                                )
                                continue
                            database.delete_key_by_email(key_email)
                            total_affected_records += 1
                            continue

                        server_client = clients_on_server.pop(key_email, None)

                        if server_client:
                            if _is_whitelist_subscription_key(db_key):
                                # For both paid tiers the panel expiry of a WHITELIST
                                # component is the next 30-day traffic reset.
                                # The paid entitlement in vpn_keys is authoritative.
                                continue
                            # NOTE: server_client.reset is a traffic-counter reset interval (days),
                            # NOT an expiry extension. Do NOT add it to expiry_time.
                            server_expiry_ms = server_client.expiry_time
                            if server_expiry_ms is None:
                                logger.warning(
                                    "Scheduler: Key '%s' on host '%s' has no panel expiry_time. Skipping update.",
                                    key_email,
                                    host_name,
                                )
                                continue
                            local_expiry_ms = int(expiry_date.timestamp() * 1000)

                            if abs(server_expiry_ms - local_expiry_ms) > 1000:
                                # Safety: only sync if the difference is less than 365 days
                                # to prevent corrupted panel data from overwriting good local dates.
                                diff_days = abs(server_expiry_ms - local_expiry_ms) / (24 * 3600 * 1000)
                                if diff_days > 365:
                                    logger.warning(
                                        "Scheduler: Key '%s' has suspicious expiry diff (%.0f days) "
                                        "between panel and DB. Skipping update.",
                                        key_email,
                                        diff_days,
                                    )
                                else:
                                    database.update_key_status_from_server(key_email, server_client)
                                    total_affected_records += 1
                                    logger.info(
                                        "Scheduler: Synced (updated) key '%s' for host '%s'.",
                                        key_email,
                                        host_name,
                                    )
                        else:
                            if _is_whitelist_subscription_key(db_key):
                                logger.error(
                                    "Scheduler: Active paid WHITELIST key '%s' was not found in the aggregate panel scan. "
                                    "Keeping the paid DB record for reconciliation.",
                                    key_email,
                                )
                                continue
                            logger.warning(
                                "Scheduler: Key '%s' for host '%s' not found on server. Deleting from local DB.",
                                key_email,
                                host_name,
                            )
                            database.update_key_status_from_server(key_email, None)
                            total_affected_records += 1

                    if panel_scan.orphan_total:
                        logger.debug(
                            "Scheduler: Found %s orphan clients on host '%s'. sample=%s",
                            panel_scan.orphan_total,
                            host_name,
                            panel_scan.orphan_sample,
                        )
                except (
                    OSError,
                    TimeoutError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                ) as exc:
                    if not panel_scan_started or panel_scan_loaded:
                        raise
                    reason = (
                        "host_inbound_missing"
                        if xui_api.is_panel_inbound_not_found_error(exc)
                        else "panel_raw_lookup_failed"
                    )
                    logger.warning(
                        "Scheduler: Minimal panel scan failed for host '%s'; skipping DB mutation for this host: %s",
                        host_name,
                        exc,
                        exc_info=True,
                    )
                    if _host_sync_alert_allowed(host_name, reason):
                        send_critical_alert_nonblocking(
                            BotError.HOST_SYNC_FAILED,
                            details=(
                                f"source={BOT_SOURCE} host={host_name} status=degraded reason={reason} \nОшибка: {exc}"
                            ),
                            respect_cooldown=True,
                        )
                    else:
                        logger.info(
                            "Scheduler: Host sync alert suppressed by shared cooldown host='%s' reason=%s.",
                            host_name,
                            reason,
                        )
                    failed_hosts += 1
                    continue
                finally:
                    clients_on_server.clear()
                    if panel_scan is not None:
                        panel_scan.clients_by_email.clear()
                        panel_scan.orphan_sample.clear()
                    panel_scan = None
                    gc.collect()

                successful_hosts += 1
                logger.info(
                    "Scheduler: Successfully synced host '%s'. memory before=%s after=%s",
                    host_name,
                    runtime_guard.format_memory_snapshot(host_memory_before),
                    runtime_guard.format_memory_snapshot(),
                )

            except TimeoutError:
                logger.warning(
                    "Scheduler: Host '%s' timed out after %s seconds. Skipping.",
                    host_name,
                    SYNC_PER_HOST_TIMEOUT_SECONDS,
                )
                database.update_host_status(host_name, "degraded", f"Sync timeout ({SYNC_PER_HOST_TIMEOUT_SECONDS}s)")
                failed_hosts += 1
            except Exception as e:
                logger.exception(
                    "Scheduler: An unexpected error occurred while processing host '%s'.",
                    host_name,
                )
                database.update_host_status(host_name, "degraded", str(e))
                failed_hosts += 1
                if not host_was_unhealthy:
                    logger.warning(
                        "Scheduler: host '%s' switched to degraded during sync (alert delegated to host-health monitor).",
                        host_name,
                    )
            finally:
                gc.collect()

    # Host down alerts are emitted by host-health monitor with debounce (>60s),
    # so sync cycle keeps only logs to avoid duplicate critical notifications.
    if all_hosts and failed_hosts > 0 and successful_hosts == 0:
        logger.warning(
            "Scheduler: all hosts failed in sync cycle (failed_hosts=%s), waiting for host-health monitor alert window.",
            failed_hosts,
        )

    try:
        total_affected_records += await _sync_blitz_keys()
    except Exception as e:
        logger.exception("Scheduler: Blitz sync failed with unexpected error.")
        send_critical_alert_nonblocking(
            BotError.BLITZ_SYNC_FAILED,
            details=str(e),
            respect_cooldown=True,
        )

    logger.info(
        "Scheduler: Sync with XUI panels and Blitz finished. "
        "Hosts: %s successful, %s failed. "
        "Total records affected: %s. "
        "Memory: %s.",
        successful_hosts,
        failed_hosts,
        total_affected_records,
        runtime_guard.format_memory_snapshot(),
    )


@indestructible_loop(
    name="subscription_check",
    interval_fn=lambda: _sleep_seconds_with_jitter(CHECK_INTERVAL_SECONDS, SCHEDULER_JITTER_SECONDS),
    initial_delay_fn=lambda: _sleep_seconds_with_jitter(SCHEDULER_INITIAL_DELAY_SECONDS, SCHEDULER_JITTER_SECONDS),
)
async def periodic_subscription_check(bot_controller: BotController):
    """Single cycle: provider reconcile, retry, panel sync, subscription notifications."""
    await reconcile_pending_platega_transactions(bot_controller)
    await reconcile_pending_lava_transactions(bot_controller)
    await reconcile_pending_paypear_transactions(bot_controller)
    await reconcile_pending_yookassa_transactions(bot_controller)
    await retry_pending_confirmed_transactions(bot_controller)
    if normalize_bot_source(BOT_SOURCE) == "reserve" and not ENABLE_RESERVE_PANEL_SYNC:
        logger.info("Scheduler: Skipping panel sync in reserve runtime.")
    else:
        await sync_keys_with_panels()

    if bot_controller.get_status().get("shop_bot_running"):
        bot = bot_controller.get_bot_instance()
        if bot:
            await check_expiring_subscriptions(bot)
        else:
            logger.warning("Scheduler: Bot is marked as running, but instance is not available.")
    else:
        logger.info("Scheduler: Bot is stopped, skipping user notifications.")
    logger.info("Scheduler: Cycle finished. Next check in %s seconds.", CHECK_INTERVAL_SECONDS)


def _mainpanel_inbound_discovery_host_names() -> list[str]:
    configured_names = [
        str(database.get_setting("standard_key_host_name") or "").strip(),
        str(database.get_setting("super_key_host_name") or "").strip(),
    ]
    seen: set[str] = set()
    result: list[str] = []
    for host_name in configured_names:
        if not host_name or host_name in seen:
            continue
        if not database.get_host(host_name):
            logger.warning("MainPanel inbound discovery: configured host '%s' is missing.", host_name)
            continue
        seen.add(host_name)
        result.append(host_name)
    return result


@indestructible_loop(
    name="mainpanel_inbound_discovery",
    interval_fn=lambda: _sleep_seconds_with_jitter(
        MAINPANEL_INBOUND_DISCOVERY_INTERVAL_SECONDS,
        SCHEDULER_JITTER_SECONDS,
    ),
    initial_delay_fn=lambda: _sleep_seconds_with_jitter(
        MAINPANEL_INBOUND_DISCOVERY_INITIAL_DELAY_SECONDS,
        SCHEDULER_JITTER_SECONDS,
    ),
)
async def periodic_mainpanel_inbound_discovery(bot_controller: BotController | None = None):
    del bot_controller
    if not MAINPANEL_INBOUND_DISCOVERY_ENABLED:
        logger.info("MainPanel inbound discovery disabled by MAINPANEL_INBOUND_DISCOVERY_ENABLED=0.")
        return
    host_names = _mainpanel_inbound_discovery_host_names()
    if not host_names:
        logger.info("MainPanel inbound discovery: no configured MainPanel hosts.")
        return
    for host_name in host_names:
        result = await asyncio.to_thread(
            xui_api.sync_existing_mainpanel_clients_to_inbounds,
            host_name,
            dry_run=False,
            batch_limit=MAINPANEL_INBOUND_BACKFILL_BATCH_SIZE,
            mode="scheduler",
            persist_snapshot=True,
        )
        logger.info(
            "MainPanel inbound discovery: host=%s status=%s processed=%s updated=%s skipped=%s failed=%s review=%s",
            host_name,
            result.get("status"),
            result.get("processed"),
            result.get("updated"),
            result.get("skipped"),
            result.get("failed"),
            result.get("needs_review"),
        )


_health_probe_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="health-probe",
)


@indestructible_loop(
    name="host_health_check",
    interval_fn=lambda: _sleep_seconds_with_jitter(
        HOST_HEALTH_CHECK_INTERVAL_SECONDS, HOST_HEALTH_CHECK_JITTER_SECONDS
    ),
    initial_delay_fn=lambda: _sleep_seconds_with_jitter(
        HOST_HEALTH_CHECK_INITIAL_DELAY_SECONDS, HOST_HEALTH_CHECK_JITTER_SECONDS
    ),
)
async def periodic_host_health_check(bot_controller: BotController | None = None):
    """Single cycle: probe all hosts, trigger recovery retries for restored hosts."""
    all_hosts = database.get_all_hosts()
    loop = asyncio.get_running_loop()
    probe_semaphore = asyncio.Semaphore(max(1, HOST_HEALTH_CHECK_CONCURRENCY))
    probe_tasks: list[asyncio.Task] = []

    async def _probe_one_host(
        host: dict,
        *,
        _loop: asyncio.AbstractEventLoop = loop,
        _probe_semaphore: asyncio.Semaphore = probe_semaphore,
    ) -> dict[str, object]:
        host_name = host["host_name"]
        host_previous_status = str(host.get("raw_status") or host.get("status") or "").strip().lower()
        host_was_unhealthy = _is_unhealthy_host_status(host_previous_status)
        if host.get("is_maintenance"):
            logger.info("Host health monitor: Host '%s' is in maintenance mode. Skipping.", host_name)
            _clear_host_unhealthy_tracking(host_name)
            xui_api.update_host_health_snapshot(
                host_name=host_name,
                status="maintenance",
                clients_count=None,
            )
            return {"host_name": host_name, "recovered": False}

        try:
            async with _probe_semaphore:
                login_result, current_status, clients_count = await asyncio.wait_for(
                    _loop.run_in_executor(
                        _health_probe_executor,
                        partial(
                            _probe_host_health,
                            host,
                            login_retries=HOST_HEALTH_PROBE_LOGIN_RETRIES,
                        ),
                    ),
                    timeout=HOST_HEALTH_PROBE_TIMEOUT_SECONDS,
                )
        except Exception as probe_err:  # noqa: BLE001 - host probe must record any SDK/network failure.
            logger.warning(
                "Host health monitor: probe for '%s' failed (%s). Marking unavailable.",
                host_name,
                type(probe_err).__name__,
            )
            xui_api.update_host_health_snapshot(
                host_name=host_name,
                status="unavailable",
                clients_count=None,
                error_code="probe_timeout" if isinstance(probe_err, asyncio.TimeoutError) else "probe_error",
                error_message=str(probe_err) or type(probe_err).__name__,
            )
            return {"host_name": host_name, "recovered": False}

        xui_api.update_host_health_snapshot(
            host_name=host_name,
            status=current_status,
            clients_count=clients_count,
            error_code=login_result.error_code,
            error_message=login_result.error_message,
        )
        if login_result.api and login_result.inbound and current_status == "healthy":
            logger.debug("Host health monitor: Host '%s' is healthy.", host_name)
        else:
            logger.warning(
                "Host health monitor: Host '%s' is %s. code=%s error=%s",
                host_name,
                current_status,
                login_result.error_code,
                login_result.error_message,
            )
        return {
            "host_name": host_name,
            "recovered": bool(
                host_was_unhealthy and login_result.api and login_result.inbound and current_status == "healthy"
            ),
        }

    probe_tasks = [asyncio.create_task(_probe_one_host(host)) for host in all_hosts]

    if probe_tasks:
        probe_results = await asyncio.gather(*probe_tasks, return_exceptions=True)
        recovered_hosts: list[str] = []
        for result in probe_results:
            if isinstance(result, Exception):
                logger.error("Host health monitor: per-host probe task failed: %s", result)
                continue
            if isinstance(result, dict) and result.get("recovered"):
                host_name = str(result.get("host_name") or "").strip()
                if host_name:
                    recovered_hosts.append(host_name)
        if PAYMENT_ISSUANCE_AUTORETRY_ENABLED and bot_controller is not None and recovered_hosts:
            for recovered_host in recovered_hosts:
                try:
                    await _retry_host_transactions_after_recovery(
                        bot_controller,
                        host_name=recovered_host,
                    )
                except Exception:
                    logger.exception(
                        "Host recovery auto-retry failed for host=%s.",
                        recovered_host,
                    )


@indestructible_loop(
    name="shop_watchdog",
    interval_fn=lambda: _sleep_seconds_with_jitter(SHOP_WATCHDOG_INTERVAL_SECONDS, SHOP_WATCHDOG_JITTER_SECONDS),
    initial_delay_fn=lambda: _sleep_seconds_with_jitter(
        SHOP_WATCHDOG_INITIAL_DELAY_SECONDS, SHOP_WATCHDOG_JITTER_SECONDS
    ),
)
async def periodic_shop_watchdog(bot_controller: BotController):
    """Single watchdog cycle — keeps Shop bot running if desired."""
    process_shop_watchdog_cycle(bot_controller)


@indestructible_loop(
    name="whitelist_limit_check",
    interval_fn=lambda: _sleep_seconds_with_jitter(
        WHITELIST_LIMIT_CHECK_INTERVAL_SECONDS,
        WHITELIST_LIMIT_CHECK_JITTER_SECONDS,
    ),
    initial_delay_fn=lambda: _sleep_seconds_with_jitter(
        WHITELIST_LIMIT_CHECK_INITIAL_DELAY_SECONDS,
        WHITELIST_LIMIT_CHECK_JITTER_SECONDS,
    ),
)
async def periodic_whitelist_limit_check(bot_controller: BotController):
    """Check WHITELIST traffic caps and notify users on newly exhausted paid super caps."""
    shop_bot_running = bool(bot_controller.get_status().get("shop_bot_running"))
    bot = bot_controller.get_bot_instance() if shop_bot_running else None
    if bot:
        stats = await check_whitelist_limit_notifications(bot)
    else:
        # Cycle reconciliation and admin-cache refresh are operational duties,
        # not Telegram duties.  They must continue while the shop bot is
        # stopped; only outbound notifications are suppressed.
        if shop_bot_running:
            logger.warning(
                "WHITELIST limit monitor: Bot is marked as running, but instance is not available; "
                "running cycle guard without notifications."
            )
        else:
            logger.info(
                "WHITELIST limit monitor: Shop bot is stopped; running cycle guard without notifications."
            )
        stats = await asyncio.to_thread(_refresh_whitelist_limit_statuses)
        stats["sent"] = 0
        stats["pruned"] = await asyncio.to_thread(
            database.prune_whitelist_limit_notifications,
            NOTIFICATION_HISTORY_RETENTION_DAYS,
        )
    logger.info(
        "WHITELIST limit monitor: host=%s keys=%s updated=%s sent=%s pruned=%s "
        "cycle_checked=%s cycle_updated=%s cycle_review=%s cycle_failed=%s.",
        stats.get("host_name"),
        stats.get("keys"),
        stats.get("updated"),
        stats.get("sent"),
        stats.get("pruned"),
        (stats.get("cycle_guard") or {}).get("checked"),
        (stats.get("cycle_guard") or {}).get("updated"),
        (stats.get("cycle_guard") or {}).get("needs_review"),
        (stats.get("cycle_guard") or {}).get("failed"),
    )


@indestructible_loop(
    name="delivery_sla_check",
    interval_fn=lambda: float(DELIVERY_SLA_CHECK_INTERVAL_SECONDS),
    initial_delay_fn=lambda: _sleep_seconds_with_jitter(300, 30),
)
async def periodic_delivery_sla_check():
    """Hourly check: alert if p95 delivery time exceeds threshold."""
    stats = database.get_delivery_time_percentiles(period_hours=1)
    p95 = stats.get("p95")
    count = stats.get("count", 0)
    if p95 is None or count < 3:
        return
    threshold = float(DELIVERY_SLA_P95_THRESHOLD_SECONDS)
    if p95 > threshold:
        _safe_alert(
            BotError.PAYMENT_KEY_ISSUE_FAILED,
            details=(
                f"source={BOT_SOURCE} reason=delivery_sla_degraded "
                f"p95={p95:.1f}s threshold={threshold:.0f}s "
                f"p50={stats.get('p50', 'n/a')}s p99={stats.get('p99', 'n/a')}s "
                f"count={count}"
            ),
        )
