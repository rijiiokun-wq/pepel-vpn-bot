import asyncio
import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps
from html import escape as html_escape
from io import BytesIO
from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo

import aiohttp
import qrcode
from aiogram import Bot, F, Router, html, types
from aiogram.enums import ChatAction, ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiosend import CryptoPay
from pytonconnect import TonConnect
from pytonconnect.exceptions import UserRejectsError
from yookassa import Payment

from shop_bot.bot import keyboards
from shop_bot.bot.message_effects import (
    ONBOARDING_SUCCESS_MESSAGE_EFFECT_ID_SETTING,
    PURCHASE_SUCCESS_MESSAGE_EFFECT_ID_SETTING,
    TRIAL_SUCCESS_MESSAGE_EFFECT_ID_SETTING,
    WELCOME_MESSAGE_EFFECT_ID_SETTING,
    send_effect_message,
)
from shop_bot.bot.middlewares import (
    _DEFAULT_MAIN_SERVICE_MESSAGE,
    _DEFAULT_MAIN_SERVICE_RESERVE_BOT_URL,
)
from shop_bot.config import (
    BOT_SOURCE as RUNTIME_BOT_SOURCE,
)
from shop_bot.config import (
    CHOOSE_PAYMENT_METHOD_MESSAGE,
    VPN_INACTIVE_TEXT,
    VPN_NO_DATA_TEXT,
    get_key_info_text,
    get_profile_text,
    get_purchase_success_text,
    get_trial_success_text,
    get_vpn_active_text,
)
from shop_bot.data_manager import referral_withdrawal_actions, referrals
from shop_bot.data_manager.database import (
    add_new_key,
    apply_payment_accounting_once,
    archive_key,
    create_pending_transaction,
    finalize_transaction_paid,
    get_all_users,
    get_host,
    get_key_by_email,
    get_key_by_id,
    get_latest_migrated_key_id_for_user_incident,
    get_next_key_number,
    get_plan_by_id,
    get_plans_for_host,
    get_setting,
    get_transaction_by_payment_id,
    get_unseen_incidents_for_user,
    get_user,
    get_user_keys,
    get_user_whitelist_traffic_statuses,
    is_payment_accounting_applied,
    is_transaction_fully_issued,
    log_transaction,
    mark_incident_seen,
    mark_transaction_fatal_failure,
    mark_transaction_paid,
    mark_transaction_retryable_failure,
    promote_lava_pending_transaction,
    refresh_transaction_lock,
    register_user_if_not_exists,
    release_transaction_lock,
    reset_transaction_to_pending,
    set_blitz_refresh_used,
    set_incident_opened_new_key_at,
    set_terms_agreed,
    set_trial_super_used,
    set_trial_used,
    try_acquire_transaction_lock,
    update_key_host_and_uuid,
    update_key_info,
    update_transaction_metadata,
    update_transaction_metadata_field,
)
from shop_bot.data_manager.host_visibility import (
    filter_customer_visible_keys,
    is_customer_host_visible,
)
from shop_bot.domain.payments import is_fatal_precheck_reason
from shop_bot.errors import BotError, humanize_alert_reason, send_critical_alert_nonblocking
from shop_bot.modules import blitz_api, lava_api, paypear_api, standard_birth_recovery, subscription_dates, xui_api
from shop_bot.modules.purchase_precheck import (
    PreCheckResult,
    check_purchase,
    check_super_purchase,
)
from shop_bot.super_promotion import (
    PROMO_EXPIRED_TEXT,
    PROMO_SUPER_PLAN,
    is_promotional_plan,
    is_super_offer_available,
    promotion_metadata,
    promotion_price,
)

TELEGRAM_BOT_USERNAME = None
PAYMENT_METHODS = None
ADMIN_ID = None
CRYPTO_BOT_TOKEN = get_setting("cryptobot_token")

logger = logging.getLogger(__name__)
BOT_SOURCE = str(RUNTIME_BOT_SOURCE or "main").strip().lower() or "main"
if BOT_SOURCE not in {"main", "reserve"}:
    BOT_SOURCE = "main"
admin_router = Router()
user_router = Router()
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
_TRIAL_SUPER_TIMEOUT_COUNT_TTL_SECONDS = 3600
_TRIAL_SUPER_TIMEOUT_COUNT_MAX_USERS = 2048
_trial_super_timeout_counts: dict[int, tuple[int, float]] = {}
_trial_super_issue_locks: dict[int, asyncio.Lock] = {}
_trial_standard_issue_locks: dict[int, asyncio.Lock] = {}
_payment_shutdown_started = False
_active_payment_completions: set[asyncio.Future] = set()


def begin_payment_shutdown() -> None:
    """Stop accepting new issuance work before draining the current payments."""
    global _payment_shutdown_started
    _payment_shutdown_started = True


async def drain_active_payments(timeout_seconds: float) -> int:
    """Wait for payment completion without cancelling owners on timeout."""
    pending = {future for future in _active_payment_completions if not future.done()}
    if not pending:
        return 0
    logger.info("Waiting for %s active payment(s) before shutdown.", len(pending))
    _, pending = await asyncio.wait(pending, timeout=max(0.0, float(timeout_seconds)))
    return len(pending)


async def _refresh_payment_lock_until_done(payment_id: str, lock_token: str, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30.0)
            break
        except TimeoutError:
            pass
        refreshed = await asyncio.to_thread(refresh_transaction_lock, payment_id, lock_token)
        if not refreshed:
            logger.warning("Payment lock heartbeat stopped: payment_id=%s lock is no longer owned.", payment_id)
            return


def _naive_utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _naive_utc_from_timestamp_ms(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).replace(tzinfo=None)


def _format_gib(value_bytes: int | None) -> str:
    try:
        value = max(0, int(value_bytes or 0)) / (1024 ** 3)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 10 or float(value).is_integer():
        return f"{value:.0f}"
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _format_moscow_reset_date(reset_timestamp_ms: int | None) -> str:
    try:
        value = int(reset_timestamp_ms or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        return "не запланирован"
    return datetime.fromtimestamp(value / 1000, tz=MOSCOW_TZ).strftime("%d.%m.%Y в %H:%M МСК")


def _build_profile_whitelist_status_text(status: dict | None) -> str | None:
    if not status:
        return None
    total_bytes = int(status.get("total_bytes") or 0)
    if total_bytes <= 0:
        return None
    used_bytes = int(status.get("used_bytes") or 0)
    reset_text = _format_moscow_reset_date(status.get("reset_timestamp_ms"))
    if reset_text == "не запланирован":
        expiry_at = _parse_key_expiry_date(status.get("expiry_date"))
        if expiry_at:
            expiry_moscow = expiry_at.replace(tzinfo=UTC).astimezone(MOSCOW_TZ)
            reset_text = f"{expiry_moscow.strftime('%d.%m.%Y в %H:%M МСК')} (окончание подписки)"
    if bool(status.get("exhausted")):
        return (
            "🔒 <b>WHITELIST:</b> лимит израсходован\n"
            f"📊 <b>Использовано:</b> {_format_gib(used_bytes)} из {_format_gib(total_bytes)} ГБ\n"
            f"🔄 <b>Следующий сброс:</b> {reset_text}"
        )
    return (
        "🔒 <b>WHITELIST:</b> активен\n"
        f"📊 <b>Использовано:</b> {_format_gib(used_bytes)} из {_format_gib(total_bytes)} ГБ\n"
        f"🔄 <b>Следующий сброс:</b> {reset_text}"
    )


def _build_profile_whitelist_statuses_text(statuses: list[dict] | None) -> str | None:
    blocks: list[str] = []
    for status in statuses or []:
        try:
            key_id = int(status.get("key_id") or 0)
            total_bytes = max(0, int(status.get("total_bytes") or 0))
            used_bytes = max(0, int(status.get("used_bytes") or 0))
        except (TypeError, ValueError):
            continue
        key_label = f"Ключ #{key_id}" if key_id > 0 else "Ключ"
        if total_bytes <= 0:
            body = (
                "🔒 <b>WHITELIST:</b> безлимитный\n"
                f"📊 <b>Использовано:</b> {_format_gib(used_bytes)} ГБ"
            )
        else:
            body = _build_profile_whitelist_status_text(status)
        if body:
            blocks.append(f"🔑 <b>{key_label}</b>\n{body}")
    return "\n\n".join(blocks) or None


def _parse_key_expiry_date(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    for candidate in (raw_value, raw_value.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed
    return None


def _trial_super_key_number(email: str | None, user_id: int, fallback: int) -> int:
    match = re.match(rf"^user{re.escape(str(user_id))}-key(\d+)-trial-super@", str(email or ""), re.IGNORECASE)
    if not match:
        return fallback
    try:
        return max(1, int(match.group(1)))
    except ValueError:
        return fallback


def _trial_super_email(user_id: int, key_number: int) -> str:
    return f"user{user_id}-key{key_number}-trial-super@telegram.bot"


def _trial_standard_email(user_id: int, key_number: int) -> str:
    return f"user{user_id}-key{key_number}-trial-standard@vpn.bot"


def _trial_super_target_expiry_ms(trial_days: int) -> int:
    return int((_naive_utc_now() + timedelta(days=trial_days)).timestamp() * 1000)


def _trial_standard_target_expiry_ms(trial_days: int) -> int:
    return int((_naive_utc_now() + timedelta(days=trial_days)).timestamp() * 1000)


async def _try_acquire_trial_super_issue_lock(user_id: int) -> asyncio.Lock | None:
    lock = _trial_super_issue_locks.get(user_id)
    if lock and lock.locked():
        return None
    if lock is None:
        lock = asyncio.Lock()
        _trial_super_issue_locks[user_id] = lock
    await lock.acquire()
    return lock


def _release_trial_super_issue_lock(user_id: int, lock: asyncio.Lock | None) -> None:
    if lock is None:
        return
    if lock.locked():
        lock.release()
    if not lock.locked() and _trial_super_issue_locks.get(user_id) is lock:
        _trial_super_issue_locks.pop(user_id, None)


async def _try_acquire_trial_standard_issue_lock(user_id: int) -> asyncio.Lock | None:
    lock = _trial_standard_issue_locks.get(user_id)
    if lock and lock.locked():
        return None
    if lock is None:
        lock = asyncio.Lock()
        _trial_standard_issue_locks[user_id] = lock
    await lock.acquire()
    return lock


def _release_trial_standard_issue_lock(user_id: int, lock: asyncio.Lock | None) -> None:
    if lock is None:
        return
    if lock.locked():
        lock.release()
    if not lock.locked() and _trial_standard_issue_locks.get(user_id) is lock:
        _trial_standard_issue_locks.pop(user_id, None)


def _existing_active_trial_standard_key(user_id: int, host_name: str) -> dict | None:
    now = _naive_utc_now()
    for key in get_user_keys(user_id):
        if str(key.get("host_name") or "") != str(host_name or ""):
            continue
        if "-trial-standard@" not in str(key.get("key_email") or ""):
            continue
        if int(key.get("is_archived") or 0):
            continue
        if _key_subscription_tier(key) != xui_api.SUBSCRIPTION_TIER_STANDARD:
            continue
        expiry_date = _parse_key_expiry_date(key.get("expiry_date"))
        if expiry_date and expiry_date > now:
            return key
    return None


def _existing_active_trial_super_key(user_id: int, host_name: str) -> dict | None:
    now = _naive_utc_now()
    for key in get_user_keys(user_id):
        if str(key.get("host_name") or "") != str(host_name or ""):
            continue
        if "-trial-super@" not in str(key.get("key_email") or ""):
            continue
        if int(key.get("is_archived") or 0):
            continue
        expiry_date = _parse_key_expiry_date(key.get("expiry_date"))
        if expiry_date and expiry_date > now:
            return key
    return None


_KEY_ACCESS_URL_PREFIXES = ("http://", "https://")


def _split_key_access_value(raw_value: object) -> tuple[str | None, str | None]:
    value = str(raw_value or "").strip()
    if not value:
        return None, None
    if value.lower().startswith(_KEY_ACCESS_URL_PREFIXES):
        return None, value
    return value, None


def _stored_key_access_values(key: dict) -> tuple[str | None, str | None]:
    connection_string, subscription_link = _split_key_access_value(key.get("issued_key_value"))
    if connection_string or subscription_link:
        return connection_string, subscription_link

    subscription_payload = xui_api.normalize_subscription_payload(key.get("subscription_payload"))
    if not subscription_payload:
        return None, None
    return _split_key_access_value(subscription_payload)


def _key_subscription_tier(key: dict | None) -> str | None:
    return xui_api.normalize_subscription_tier((key or {}).get("subscription_tier"))


PROFILE_BS_BYTES_PER_GB = 1024 * 1024 * 1024
PROFILE_BS_QUOTA_BYTES = 40 * PROFILE_BS_BYTES_PER_GB
PROFILE_BS_RESET_DAYS = 30
PROFILE_BS_RESET_SECONDS = PROFILE_BS_RESET_DAYS * 24 * 60 * 60


def _profile_traffic_state_db_path() -> str:
    configured = str(os.environ.get("TRAFFIC_MONITOR_STATE_DB") or "").strip()
    if configured:
        return configured
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
    return os.path.join(project_root, "runtime", "traffic_monitor.sqlite")


def _datetime_to_utc_ts(value: datetime) -> int:
    if value.tzinfo is not None:
        return int(value.timestamp())
    return int(value.replace(tzinfo=UTC).timestamp())


def _msk_date_from_ts(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), UTC).astimezone(MOSCOW_TZ).strftime("%d.%m.%Y")


def _format_profile_gb(bytes_value: int) -> str:
    gb_value = max(0.0, float(bytes_value or 0) / PROFILE_BS_BYTES_PER_GB)
    return f"{gb_value:.2f}".rstrip("0").rstrip(".")


def _profile_canonical_clients_for_key(key: dict) -> list[str]:
    values: list[str] = []
    email = str(key.get("key_email") or "").strip()
    sub_id = str(key.get("panel_sub_id") or "").strip()
    if email:
        values.append(f"email:{email}")
    if sub_id:
        values.append(f"sub:{sub_id}")
    return values


def _profile_int(value, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _profile_panel_client_matches_key(key: dict, client: dict) -> bool:
    raw_email = str(client.get("email") or "").strip()
    raw_uuid = str(client.get("id") or client.get("uuid") or "").strip()
    raw_sub_id = str(client.get("subId") or client.get("sub_id") or "").strip()
    key_email = str(key.get("key_email") or "").strip()
    key_uuid = str(key.get("xui_client_uuid") or "").strip()
    key_sub_id = str(key.get("panel_sub_id") or "").strip()

    if key_uuid and raw_uuid and raw_uuid == key_uuid:
        return True
    if key_sub_id and raw_sub_id and raw_sub_id == key_sub_id:
        return True
    if key_email and raw_email == key_email:
        return True

    if key_email and "@" in key_email and raw_email:
        local, domain = key_email.split("@", 1)
        return raw_email.startswith(f"{local}-whitelist") and raw_email.endswith(f"@{domain}")
    return False


def _profile_raw_inbound_is_whitelist(raw_inbound: dict) -> bool:
    label = str(raw_inbound.get("remark") or raw_inbound.get("tag") or "").lower()
    return "whitelist" in label or "white" in label or "бел" in label


def _profile_live_bs_usage_for_key(key: dict) -> dict | None:
    host_name = str(key.get("host_name") or "").strip()
    if not host_name:
        return None
    host_data = get_host(host_name)
    if not host_data:
        return None

    api = None
    try:
        token_resolver = getattr(xui_api, "_resolve_host_api_token", None)
        api_token = token_resolver(host_data) if callable(token_resolver) else None
        api, _primary_inbound = xui_api.login_to_host(
            host_url=host_data["host_url"],
            username=host_data["host_username"],
            password=host_data["host_pass"],
            inbound_id=host_data["host_inbound_id"],
            api_token=api_token,
            prefer_raw_inbound_lookup=True,
            fallback_to_full_inbound_lookup=True,
        )
        if not api:
            return None

        raw_inbounds = xui_api._list_inbounds_raw(api)
        for raw_inbound in raw_inbounds:
            if not _profile_raw_inbound_is_whitelist(raw_inbound):
                continue
            if "settings" not in raw_inbound:
                raw_inbound = xui_api._get_inbound_raw_by_id(
                    api,
                    raw_inbound.get("id"),
                    allow_list_fallback=True,
                    allow_lightweight_fallback=False,
                )

            matched_client = None
            for raw_client in xui_api._iter_client_dicts_from_settings(raw_inbound.get("settings")):
                if _profile_panel_client_matches_key(key, raw_client):
                    matched_client = raw_client
                    break
            if not matched_client:
                continue

            matched_email = str(matched_client.get("email") or "").strip()
            stats = (
                raw_inbound.get("clientStats")
                or raw_inbound.get("clientTraffics")
                or raw_inbound.get("client_traffics")
                or []
            )
            matched_stats = None
            for item in stats:
                if str(item.get("email") or "").strip() == matched_email:
                    matched_stats = item
                    break

            source = matched_stats or {}
            up = _profile_int(source.get("up"))
            down = _profile_int(source.get("down"))
            total = _profile_int(
                source.get("total"),
                _profile_int(matched_client.get("totalGB"), PROFILE_BS_QUOTA_BYTES),
            )
            if total <= 0:
                total = PROFILE_BS_QUOTA_BYTES
            enabled = bool(matched_client.get("enable", True))
            if matched_stats is not None and "enable" in matched_stats:
                enabled = bool(matched_stats.get("enable"))

            used = max(0, up + down)
            if not enabled and used >= total:
                used = total
            return {
                "used_bytes": used,
                "total_bytes": total,
                "enabled": enabled,
            }
    except Exception as exc:
        logger.warning("Failed to read live BS quota from panel for profile: %s", exc)
        return None
    finally:
        close = getattr(api, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    return None


def _profile_latest_active_super_key(user_keys: list[dict], now: datetime) -> dict | None:
    candidates: list[tuple[datetime, dict]] = []
    for key in user_keys:
        if int(key.get("is_archived") or 0):
            continue
        if _key_subscription_tier(key) != xui_api.SUBSCRIPTION_TIER_SUPER:
            continue
        expiry_date = _parse_key_expiry_date(key.get("expiry_date"))
        if not expiry_date or expiry_date <= now:
            continue
        candidates.append((expiry_date, key))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _traffic_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _traffic_sum_for_clients(conn: sqlite3.Connection, clients: list[str], cycle_start_ts: int) -> int:
    if not clients or not _traffic_table_exists(conn, "traffic_deltas"):
        return 0
    placeholders = ",".join("?" for _ in clients)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(total_delta), 0)
        FROM traffic_deltas
        WHERE classification = 'whitelist'
          AND observed_at >= ?
          AND canonical_client IN ({placeholders})
        """,
        (cycle_start_ts, *clients),
    ).fetchone()
    return max(0, int(row[0] if row else 0))


def _traffic_latest_reset_for_clients(conn: sqlite3.Connection, clients: list[str], created_ts: int) -> int | None:
    if not clients or not _traffic_table_exists(conn, "client_reset_events"):
        return None
    placeholders = ",".join("?" for _ in clients)
    row = conn.execute(
        f"""
        SELECT MAX(reset_at)
        FROM client_reset_events
        WHERE classification = 'whitelist'
          AND reset_at >= ?
          AND canonical_client IN ({placeholders})
        """,
        (created_ts, *clients),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return max(0, int(row[0]))


def _profile_bs_cycle_bounds(created_ts: int, now_ts: int, last_reset_ts: int | None) -> tuple[int, int]:
    cycle_start = max(0, int(last_reset_ts or created_ts))
    if last_reset_ts is None:
        while cycle_start + PROFILE_BS_RESET_SECONDS <= now_ts:
            cycle_start += PROFILE_BS_RESET_SECONDS
    return cycle_start, cycle_start + PROFILE_BS_RESET_SECONDS


def _render_profile_bs_quota_text(user_keys: list[dict], *, now: datetime | None = None) -> str:
    current_time = now or _naive_utc_now()
    key = _profile_latest_active_super_key(user_keys, current_time)
    if not key:
        return ""

    created_at = _parse_key_expiry_date(key.get("created_date")) or current_time
    created_ts = _datetime_to_utc_ts(created_at)
    now_ts = _datetime_to_utc_ts(current_time)
    clients = _profile_canonical_clients_for_key(key)
    last_reset_ts: int | None = None
    used_bytes = 0

    db_path = _profile_traffic_state_db_path()
    if clients and os.path.exists(db_path):
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0) as conn:
                last_reset_ts = _traffic_latest_reset_for_clients(conn, clients, created_ts)
                cycle_start_ts, next_reset_ts = _profile_bs_cycle_bounds(created_ts, now_ts, last_reset_ts)
                used_bytes = _traffic_sum_for_clients(conn, clients, cycle_start_ts)
        except sqlite3.Error as exc:
            logger.warning("Failed to read BS traffic quota for profile: %s", exc)
            cycle_start_ts, next_reset_ts = _profile_bs_cycle_bounds(created_ts, now_ts, None)
    else:
        cycle_start_ts, next_reset_ts = _profile_bs_cycle_bounds(created_ts, now_ts, None)

    _ = cycle_start_ts
    quota_bytes = PROFILE_BS_QUOTA_BYTES
    live_usage = _profile_live_bs_usage_for_key(key)
    if live_usage:
        cycle_start_ts, next_reset_ts = _profile_bs_cycle_bounds(created_ts, now_ts, None)
        _ = cycle_start_ts
        quota_bytes = max(1, int(live_usage.get("total_bytes") or PROFILE_BS_QUOTA_BYTES))
        used_bytes = max(0, int(live_usage.get("used_bytes") or 0))
    elif str(key.get("host_name") or "").strip() == "MainPanel":
        cycle_start_ts, next_reset_ts = _profile_bs_cycle_bounds(created_ts, now_ts, None)
        _ = cycle_start_ts
        return (
            "\n\n"
            "📊 <b>Остаток БС:</b> данные обновляются\n"
            f"🔄 <b>Дата сброса:</b> {_msk_date_from_ts(next_reset_ts)}"
        )

    remaining_bytes = max(0, quota_bytes - used_bytes)
    return (
        "\n\n"
        f"📊 <b>Остаток БС:</b> {_format_profile_gb(remaining_bytes)} ГБ из {_format_profile_gb(quota_bytes)} ГБ\n"
        f"🔄 <b>Дата сброса:</b> {_msk_date_from_ts(next_reset_ts)}"
    )


def _is_extendable_managed_key(
    key: dict | None,
    *,
    expiry_date: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    if not key or blitz_api.is_blitz_key(key):
        return False
    if int(key.get("is_archived") or 0):
        return False
    if not str(key.get("panel_sub_id") or "").strip():
        return False
    if _key_subscription_tier(key) not in {
        xui_api.SUBSCRIPTION_TIER_STANDARD,
        xui_api.SUBSCRIPTION_TIER_SUPER,
    }:
        return False

    resolved_expiry = expiry_date or _parse_key_expiry_date(key.get("expiry_date"))
    if not resolved_expiry:
        return False
    return resolved_expiry > (now or datetime.now())


def _trial_result_from_existing_key(key: dict) -> dict:
    expiry_date = _parse_key_expiry_date(key.get("expiry_date")) or _naive_utc_now()
    connection_string, subscription_link = _stored_key_access_values(key)
    return {
        "client_uuid": str(key.get("xui_client_uuid") or "").strip(),
        "email": str(key.get("key_email") or "").strip(),
        "expiry_timestamp_ms": int(expiry_date.timestamp() * 1000),
        "connection_string": connection_string,
        "subscription_link": subscription_link,
        "host_name": str(key.get("host_name") or "").strip(),
        "sub_id": str(key.get("panel_sub_id") or "").strip() or None,
    }


def _main_payments_disabled() -> bool:
    return BOT_SOURCE == "main" and str(get_setting("yookassa_payments_enabled") or "").strip().lower() != "true"


def _build_main_purchase_redirect_text() -> str:
    message = str(os.environ.get("MAIN_SERVICE_MODE_MESSAGE") or "").strip()
    if not message:
        message = str(get_setting("main_service_mode_message") or "").strip()
    if not message:
        message = str(get_setting("maintenance_message") or "").strip()
    return message or _DEFAULT_MAIN_SERVICE_MESSAGE


def _build_main_purchase_redirect_keyboard():
    reserve_bot_url = str(os.environ.get("MAIN_SERVICE_MODE_RESERVE_BOT_URL") or "").strip()
    if not reserve_bot_url:
        reserve_bot_url = str(get_setting("main_service_mode_reserve_bot_url") or "").strip()
    if not reserve_bot_url:
        reserve_bot_username = str(get_setting("reserve_bot_username") or "").strip().lstrip("@")
        if reserve_bot_username:
            reserve_bot_url = f"https://t.me/{reserve_bot_username}"
    if not reserve_bot_url:
        reserve_bot_url = _DEFAULT_MAIN_SERVICE_RESERVE_BOT_URL

    builder = InlineKeyboardBuilder()
    builder.button(text="🔁 Перейти в бота", url=reserve_bot_url)
    builder.adjust(1)
    return builder.as_markup()


async def _show_main_purchase_redirect_for_callback(
    callback: types.CallbackQuery,
    state: FSMContext | None = None,
) -> None:
    await _safe_callback_answer(callback)
    if callback.message:
        await _replace_message(
            callback.message,
            text=_build_main_purchase_redirect_text(),
            reply_markup=_build_main_purchase_redirect_keyboard(),
            disable_web_page_preview=True,
        )
    if state is not None:
        await state.clear()


async def _show_main_purchase_redirect_for_message(message: types.Message, state: FSMContext) -> None:
    await message.answer(
        _build_main_purchase_redirect_text(),
        reply_markup=_build_main_purchase_redirect_keyboard(),
        disable_web_page_preview=True,
    )
    await state.clear()


def _build_main_runtime_service_text(configured_message: str | None) -> str:
    message = str(configured_message or "").strip() or _DEFAULT_MAIN_SERVICE_MESSAGE
    return f"⚙️ Основной сервер временно недоступен.\n\n{message}"


def _main_runtime_status_is_healthy(status: dict | None) -> bool:
    return bool(isinstance(status, dict) and status.get("shop_bot_running") and status.get("all_settings_ok"))


TRIAL_SUPER_ISSUE_TIMEOUT_SECONDS = max(
    8.0,
    float(str(os.environ.get("TRIAL_SUPER_ISSUE_TIMEOUT_SECONDS", "40")).strip() or "40"),
)
TRIAL_ISSUE_QUICK_RETRY_ATTEMPTS = max(
    1,
    int(str(os.environ.get("TRIAL_ISSUE_QUICK_RETRY_ATTEMPTS", "3")).strip() or "3"),
)
TRIAL_ISSUE_QUICK_RETRY_ATTEMPT_TIMEOUT_SECONDS = max(
    4.0,
    float(str(os.environ.get("TRIAL_ISSUE_QUICK_RETRY_ATTEMPT_TIMEOUT_SECONDS", "16")).strip() or "16"),
)
TRIAL_ISSUE_QUICK_RETRY_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 3.0)
TRIAL_SUPER_VERIFY_ATTEMPTS = max(
    1,
    int(str(os.environ.get("TRIAL_SUPER_VERIFY_ATTEMPTS", "3")).strip() or "3"),
)
TRIAL_SUPER_VERIFY_DELAY_SECONDS = max(
    0.1,
    float(str(os.environ.get("TRIAL_SUPER_VERIFY_DELAY_SECONDS", "2")).strip() or "2"),
)
PURCHASE_PRECHECK_TIMEOUT_SECONDS = max(
    1.0,
    float(str(os.environ.get("PURCHASE_PRECHECK_TIMEOUT_SECONDS", "4")).strip() or "4"),
)
PURCHASE_ISSUE_PANEL_CALL_TIMEOUT_SECONDS = max(
    float(xui_api.PANEL_CALL_TIMEOUT_SECONDS),
    float(
        str(
            os.environ.get(
                "PURCHASE_ISSUE_PANEL_CALL_TIMEOUT_SECONDS",
                str(math.ceil(float(xui_api.PANEL_CALL_TIMEOUT_SECONDS) * 1.5)),
            )
        ).strip()
        or str(math.ceil(float(xui_api.PANEL_CALL_TIMEOUT_SECONDS) * 1.5))
    ),
)
PURCHASE_ISSUE_MAX_ATTEMPTS = max(
    1,
    int(str(os.environ.get("PURCHASE_ISSUE_MAX_ATTEMPTS", "5")).strip() or "5"),
)
STANDARD_KEY_NETWORK_WARNING = (
    "<b>Стандарт:</b> WHITELIST — 5 ГБ в месяц.\n"
    "Остальные серверы — безлимитные."
)
DEFAULT_STANDARD_KEY_HOST_NAME = "StandartVLESS"
_INSTRUCTION_URLS = {
    "happ_ios": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215?l=ru",
    "happ_android": "https://play.google.com/store/apps/details?id=com.happproxy&pcampaignid=web_share",
    "happ_windows": "https://github.com/Happ-proxy/happ-desktop/releases/download/2.18.3/setup-Happ.x64.exe",
    "happ_macos": "https://github.com/Happ-proxy/happ-desktop/releases/download/2.18.3/Happ.macOS.universal.dmg",
    "karing_ios": "https://apps.apple.com/ru/app/karing/id6472431552",
    "karing_android": "https://github.com/KaringX/karing/releases/download/v1.2.21.2406/karing_1.2.21.2406_android_arm64-v8a.apk",
    "karing_windows": "https://github.com/KaringX/karing/releases/download/v1.2.21.2406/karing_1.2.21.2406_windows_x64.exe",
    "karing_macos": "https://github.com/KaringX/karing/releases/download/v1.2.21.2406/karing_1.2.21.2406_macos_universal.dmg",
    "hiddify_ios": "https://apps.apple.com/us/app/hiddify-proxy-vpn/id6596777532",
    "hiddify_android": "https://play.google.com/store/apps/details?id=app.hiddify.com",
    "hiddify_windows": "https://github.com/hiddify/hiddify-app/releases/latest/download/Hiddify-Windows-Setup-x64.exe",
    "hiddify_macos": "https://github.com/hiddify/hiddify-next/releases/latest/download/Hiddify-MacOS.dmg",
    "v2raytun_ios": "https://apps.apple.com/us/app/v2raytun/id6476628951?l=ru",
    "v2raytun_android": "https://play.google.com/store/apps/details?id=com.v2raytun.android",
    "v2raytun_windows": "https://github.com/mdf45/v2raytun/releases/download/v3.8.12/v2RayTun_Setup.exe",
    "incy_ios": "https://apps.apple.com/ru/app/incy/id6756943388",
    "incy_android": "https://play.google.com/store/apps/details?id=llc.itdev.incy&hl=ru",
    "incy_windows": "https://github.com/INCY-DEV/incy-platforms/releases/download/desktop-v3.2.6/incy-windows-portable.zip",
    "incy_macos": "https://github.com/INCY-DEV/incy-platforms/releases/download/desktop-v3.2.6/incy-macos-arm64.dmg",
    "karing_instruction": "https://t.me/Instructionspepel/11",
}
_INSTRUCTION_MESSAGE = (
    "Тариф «Стандарт»: WHITELIST — 5 ГБ в месяц, остальные серверы — безлимитные.\n\n"
    "Тариф «Суперключ (обход белых списков)» можно использовать в любом приложении из списка ниже.\n\n"
    "Полная инструкция по установке VPN для каждого приложения — "
    f'<a href="{_INSTRUCTION_URLS["karing_instruction"]}">@instructionspepel</a>.'
)
SUPER_KEY_PLANS: tuple[dict, ...] = (
    {
        "code": "1m",
        "months": 1,
        "price": 600,
        "old_price": None,
        "discount_text": "",
        "name": "Суперключ (обход белых списков) 1 месяц",
        "button_text": "1 месяц — 600 ₽",
    },
    PROMO_SUPER_PLAN,
    {
        "code": "3m",
        "months": 3,
        "price": 1620,
        "old_price": 1800,
        "discount_text": "−10%",
        "name": "Суперключ (обход белых списков) 3 месяца",
        "button_text": "3 месяца — 1620 ₽ −10% (вместо 1800 ₽)",
    },
    {
        "code": "6m",
        "months": 6,
        "price": 3096,
        "old_price": 3600,
        "discount_text": "−14%",
        "name": "Суперключ (обход белых списков) 6 месяцев",
        "button_text": "6 месяцев — 3096 ₽ −14% (вместо 3600 ₽)",
    },
    {
        "code": "12m",
        "months": 12,
        "price": 6120,
        "old_price": 7200,
        "discount_text": "−15%",
        "name": "Суперключ (обход белых списков) 1 год",
        "button_text": "1 год — 6120 ₽ −15% (вместо 7200 ₽)",
    },
    {
        "code": "24m",
        "months": 24,
        "price": 11520,
        "old_price": 14400,
        "discount_text": "−20%",
        "name": "Суперключ (обход белых списков) 2 года",
        "button_text": "2 года — 11520 ₽ −20% (вместо 14400 ₽)",
    },
)
SUPER_KEY_PLAN_BY_CODE = {plan["code"]: plan for plan in SUPER_KEY_PLANS}


def _generate_blitz_username() -> str:
    """Generate non-predictable Blitz usernames without exposing Telegram IDs."""
    return f"bk_{secrets.token_hex(10)}"


def _remaining_days_until(expiry_dt: datetime, *, now: datetime | None = None) -> int:
    """
    Return remaining whole days until expiry, rounded up.

    This avoids the common "-1 day" issue from timedelta.days flooring.
    Example: 12.1 days remaining -> 13.
    """
    now_dt = now or datetime.now()
    seconds = (expiry_dt - now_dt).total_seconds()
    if seconds <= 0:
        return 0
    return int(math.ceil(seconds / 86400.0))


def _get_super_key_host_name() -> str | None:
    value = (get_setting("super_key_host_name") or "").strip()
    return value or None


def _get_super_key_host() -> dict | None:
    host_name = _get_super_key_host_name()
    if not host_name:
        return None
    return get_host(host_name)


def _get_standard_key_host_name() -> str:
    configured = (get_setting("standard_key_host_name") or "").strip()
    return configured or DEFAULT_STANDARD_KEY_HOST_NAME


def _normalize_host_url(host_url: str | None) -> str:
    return str(host_url or "").strip().rstrip("/").lower()


def _is_super_key_host_name(host_name: str | None) -> bool:
    candidate_host_name = str(host_name or "").strip()
    super_host_name = _get_super_key_host_name()
    if not super_host_name or not candidate_host_name:
        return False
    if candidate_host_name == super_host_name:
        return True

    super_host = get_host(super_host_name)
    candidate_host = get_host(candidate_host_name)
    if not super_host or not candidate_host:
        return False

    return _normalize_host_url(super_host.get("host_url")) == _normalize_host_url(candidate_host.get("host_url"))


def _render_super_key_plans_text() -> str:
    return "🔥 <b>Суперключ (обход белых списков)</b>"


PURCHASE_SOURCE_MAIN = "main"
PURCHASE_SOURCE_KEYS = "keys"


def _resolve_purchase_back_callback(user_id: int) -> str:
    # Fallback for legacy flows without explicit source.
    return "manage_keys" if _get_customer_visible_user_keys(user_id) else "back_to_main_menu"


def _get_customer_visible_user_keys(user_id: int) -> list[dict]:
    """Read keys for customer presentation only.

    Billing, renewals, retries, and admin operations continue to use the raw
    ``get_user_keys`` result so hiding a legacy host never mutates ownership or
    payment state.
    """

    return [
        key for key in filter_customer_visible_keys(get_user_keys(user_id))
        if not key.get("is_archived")
    ]


def _get_customer_visible_key_for_user(key_id: int, user_id: int) -> dict | None:
    key_data = get_key_by_id(key_id)
    if not key_data or key_data.get("is_archived") or int(key_data.get("user_id") or 0) != int(user_id):
        return None
    if not is_customer_host_visible(key_data.get("host_name")):
        return None
    return key_data


def _filter_whitelist_statuses_for_customer_keys(
    statuses: list[dict] | None,
    customer_keys: list[dict],
) -> list[dict]:
    visible_key_ids: set[int] = set()
    for key in customer_keys:
        try:
            key_id = int(key.get("key_id") or 0)
        except (TypeError, ValueError):
            continue
        if key_id > 0:
            visible_key_ids.add(key_id)

    visible_statuses: list[dict] = []
    for status in statuses or []:
        try:
            key_id = int(status.get("key_id") or 0)
        except (TypeError, ValueError):
            continue
        if key_id in visible_key_ids:
            visible_statuses.append(status)
    return visible_statuses


def _resolve_purchase_source(callback_data: str | None, message_text: str | None = None) -> str | None:
    if callback_data in {"buy_new_key_main", "buy_super_key_main"}:
        return PURCHASE_SOURCE_MAIN
    if callback_data in {"buy_new_key_keys", "buy_super_key_keys"}:
        return PURCHASE_SOURCE_KEYS

    # Legacy callbacks from old messages.
    if callback_data in {"buy_new_key", "buy_super_key"}:
        normalized_text = (message_text or "").lower()
        if "ваши ключи" in normalized_text:
            return PURCHASE_SOURCE_KEYS
        if "главное меню" in normalized_text:
            return PURCHASE_SOURCE_MAIN
    return None


def _resolve_purchase_back_callback_by_source(source: str | None, user_id: int) -> str:
    if source == PURCHASE_SOURCE_MAIN:
        return "back_to_main_menu"
    if source == PURCHASE_SOURCE_KEYS:
        return "manage_keys"
    return _resolve_purchase_back_callback(user_id)


def _pick_issued_key_value(
    connection_string: str | None = None,
    subscription_link: str | None = None,
    sub_id: str | None = None,
    subscription_tier: str | None = None,
) -> str | None:
    value = (
        _managed_subscription_url(sub_id, subscription_tier=subscription_tier)
        or subscription_link
        or connection_string
        or ""
    ).strip()
    return value or None


def _managed_subscription_url(sub_id: str | None, *, subscription_tier: str | None = None) -> str | None:
    normalized_tier = xui_api.normalize_subscription_tier(subscription_tier)
    if not normalized_tier:
        return None

    normalized_sub_id = str(sub_id or "").strip().strip("/")
    if not normalized_sub_id:
        return None

    base_url = str(os.environ.get("MANAGED_SUBSCRIPTION_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        return None

    parsed = urlparse(base_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        logger.warning("Ignoring invalid MANAGED_SUBSCRIPTION_BASE_URL for issued key value.")
        return None

    return f"{base_url}/{quote(normalized_sub_id, safe='')}"


def _trial_super_access_values_from_panel_result(
    panel_result: dict,
) -> tuple[str | None, str | None, str | None]:
    issued_key_value = _pick_issued_key_value(
        connection_string=panel_result.get("connection_string"),
        subscription_link=panel_result.get("subscription_link"),
        sub_id=panel_result.get("sub_id"),
        subscription_tier=xui_api.SUBSCRIPTION_TIER_SUPER,
    )
    connection_string, subscription_link = _split_key_access_value(issued_key_value)
    if connection_string or subscription_link:
        return issued_key_value, connection_string, subscription_link

    fallback_connection_string, fallback_subscription_link = _split_key_access_value(
        panel_result.get("subscription_link") or panel_result.get("connection_string")
    )
    return issued_key_value, fallback_connection_string, fallback_subscription_link


def _build_support_text(
    support_text: str | None,
    *,
    support_contact_visible: bool,
) -> str:
    if support_text is not None:
        normalized = support_text.strip()
        if normalized:
            return normalized
    _ = support_contact_visible
    return "Для связи с поддержкой используйте кнопку ниже."  # noqa: RUF001


def _resolve_purchase_availability_sync() -> dict[str, object]:
    """Resolve purchase entry availability without network I/O.

    Uses DB/settings/cached host state only, so it is safe to call in a worker
    before rendering menu keyboards.
    """
    maintenance_message = (
        str(get_setting("maintenance_message") or "").strip()
        or "Покупка временно недоступна, ведутся технические работы. Попробуйте позже."
    )
    maintenance_mode = str(get_setting("maintenance_mode") or "").strip().lower() == "true"
    if maintenance_mode:
        return {
            "standard_allowed": False,
            "super_allowed": False,
            "standard_message": maintenance_message,
            "super_message": maintenance_message,
            "common_message": maintenance_message,
            "reason": "global_maintenance",
        }

    standard_allowed = False
    standard_host_name = _get_standard_key_host_name()
    standard_message = "В данный момент нет доступных тарифов. Пожалуйста, обратитесь в поддержку."
    standard_host_visible = is_customer_host_visible(standard_host_name)
    standard_host = get_host(standard_host_name) if standard_host_visible else None
    if not standard_host_visible:
        standard_message = "Тариф «Стандарт» временно недоступен. Пожалуйста, обратитесь в поддержку."
    elif not standard_host:
        standard_message = f"Тариф «Стандарт» временно недоступен: хост «{standard_host_name}» не настроен."
    else:
        standard_precheck = _run_purchase_precheck(standard_host_name, is_super_key_flow=False)
        if not standard_precheck.allowed:
            standard_message = (
                str(standard_precheck.user_message or "").strip()
                or f"Сервер «{standard_host_name}» временно недоступен. Попробуйте позже."
            )
        else:
            standard_plans = get_plans_for_host(standard_host_name)
            standard_allowed = bool(standard_plans)
            if not standard_allowed:
                standard_message = (
                    f"Тариф «Стандарт» временно недоступен: для хоста «{standard_host_name}» не настроены тарифы."
                )

    super_allowed = False
    super_message = "Суперключ (обход белых списков) временно недоступен. Пожалуйста, обратитесь в поддержку."
    super_host_name = _get_super_key_host_name()
    super_host_visible = is_customer_host_visible(super_host_name) if super_host_name else False
    super_host = _get_super_key_host() if super_host_visible else None
    if super_host_name and super_host:
        super_status = str(super_host.get("status") or "").strip().lower()
        super_maintenance = bool(super_host.get("is_maintenance"))
        if not super_maintenance and super_status not in {"unavailable", "maintenance"}:
            super_allowed = True
        else:
            super_message = f"Сервер «{super_host_name}» временно недоступен. Попробуйте позже."

    reason = ""
    if not standard_allowed and not super_allowed:
        reason = "no_purchase_options"
    elif not standard_host_visible:
        reason = "standard_host_hidden"
    elif super_host_name and not super_host_visible:
        reason = "super_host_hidden"
    elif not standard_allowed:
        reason = "standard_unavailable"
    elif not super_allowed:
        reason = "super_unavailable"

    common_message = standard_message if not standard_allowed else super_message
    return {
        "standard_allowed": standard_allowed,
        "super_allowed": super_allowed,
        "standard_message": standard_message,
        "super_message": super_message,
        "common_message": common_message,
        "reason": reason,
    }


async def _resolve_purchase_availability() -> dict[str, object]:
    try:
        return await asyncio.to_thread(_resolve_purchase_availability_sync)
    except Exception as exc:
        logger.warning("Purchase availability resolver failed, fallback to allow mode: %s", exc, exc_info=True)
        return {
            "standard_allowed": True,
            "super_allowed": True,
            "standard_message": "",
            "super_message": "",
            "common_message": "",
            "reason": "resolver_failed",
        }


def _extract_panel_snapshot_from_metadata(metadata: dict | None) -> dict | None:
    from shop_bot.domain.issuance import extract_panel_snapshot as _domain_extract

    base = _domain_extract(metadata)
    if base is None:
        return None

    snapshot: dict[str, object] = dict(base)
    try:
        if isinstance(metadata, dict) and metadata.get("issued_panel_expiry_ms") is not None:
            snapshot["expiry_timestamp_ms"] = int(metadata["issued_panel_expiry_ms"])
    except (TypeError, ValueError):
        pass

    if isinstance(metadata, dict):
        issued_key_value = str(metadata.get("issued_panel_key_value") or "").strip()
        if issued_key_value:
            snapshot["issued_key_value"] = issued_key_value
    return snapshot


def _persist_panel_snapshot_for_payment(
    payment_id: str | None,
    metadata: dict,
    panel_result: dict,
    *,
    issued_key_value: str | None = None,
) -> None:
    if not payment_id:
        return
    try:
        snapshot = dict(metadata or {})
        snapshot["issued_panel_host"] = str(panel_result.get("host_name") or "").strip()
        snapshot["issued_panel_email"] = str(panel_result.get("email") or "").strip()
        snapshot["issued_panel_uuid"] = str(panel_result.get("client_uuid") or "").strip()
        try:
            snapshot["issued_panel_expiry_ms"] = int(panel_result.get("expiry_timestamp_ms") or 0)
        except (TypeError, ValueError):
            pass
        if issued_key_value:
            snapshot["issued_panel_key_value"] = issued_key_value

        update_transaction_metadata(
            payment_id,
            snapshot,
            statuses=("processing", "pending"),
        )
    except Exception as e:
        logger.warning("Failed to persist panel snapshot for payment_id=%s: %s", payment_id, e)


def _coerce_positive_int(value: object) -> int | None:
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _datetime_from_timestamp_ms(timestamp_ms: int) -> datetime:
    return datetime.fromtimestamp(int(timestamp_ms) / 1000)


def _payload_has_expected_subscription_shape(
    key_data: dict,
    *,
    subscription_tier: str | None,
    allow_native_vision: bool = False,
) -> bool:
    payload = xui_api.normalize_subscription_payload(
        key_data.get("subscription_payload") or key_data.get("issued_key_value")
    )
    if not payload:
        return False
    if "flow=xtls-rprx-vision" in payload and not allow_native_vision:
        return False
    normalized_tier = xui_api.normalize_subscription_tier(subscription_tier)
    host_name = str(key_data.get("host_name") or "").strip()
    expected_labels = xui_api.mainpanel_expected_subscription_labels(host_name, normalized_tier)
    if expected_labels:
        fragments = {
            re.sub(r"[^a-z0-9]+", "", (
                unquote(urlparse(line).fragment) if allow_native_vision else urlparse(line).fragment
            ).strip().lower())
            for line in payload.splitlines()
            if line.strip()
        }
        expected = {
            re.sub(r"[^a-z0-9]+", "", str(label or "").strip().lower())
            for label in expected_labels
            if str(label or "").strip()
        }
        if not expected.issubset(fragments):
            return False
    return True


def _result_from_db_key(key_data: dict, *, target_expiry_ms: int | None = None) -> dict:
    connection_string, subscription_link = _split_key_access_value(key_data.get("issued_key_value"))
    expiry_ms = target_expiry_ms
    if expiry_ms is None:
        expiry_dt = _parse_key_expiry_date(key_data.get("expiry_date"))
        if expiry_dt:
            expiry_ms = int(expiry_dt.timestamp() * 1000)
    return {
        "client_uuid": str(key_data.get("xui_client_uuid") or ""),
        "email": str(key_data.get("key_email") or ""),
        "expiry_timestamp_ms": int(expiry_ms or 0),
        "connection_string": connection_string,
        "subscription_link": subscription_link,
        "host_name": str(key_data.get("host_name") or ""),
        "sub_id": str(key_data.get("panel_sub_id") or ""),
        "subscription_payload": xui_api.normalize_subscription_payload(key_data.get("subscription_payload")),
    }


def _persist_payment_target_for_retry(
    payment_id: str | None,
    metadata: dict,
    *,
    action: str,
    key_id: int,
    target_expiry_ms: int,
    target_expiry_at: datetime,
    email: str | None,
) -> None:
    if not payment_id:
        return
    if not metadata.get("issuance_target_expiry_ms"):
        metadata["issuance_target_expiry_ms"] = int(target_expiry_ms)
        metadata["issuance_target_expiry_at"] = target_expiry_at.isoformat()
    metadata["issuance_target_action"] = str(action or "")
    metadata["issuance_target_key_id"] = int(key_id or 0)
    if email:
        metadata["issuance_target_email"] = str(email)
    update_transaction_metadata(payment_id, metadata, statuses=("processing", "pending", "paid"))


def _claim_paid_standard_birth(
    payment_id: str | None,
    metadata: dict,
    *,
    user_id: int,
    email: str,
    target_expiry_ms: int,
    whitelist_cycle_anchor_ms: int,
) -> bool:
    """Persist the one paid MainPanel Standard write intent before creating it."""
    if not payment_id or not re.fullmatch(rf"user{user_id}-key[1-9]\d*@vpn\.bot", email):
        return False
    if metadata.get("standard_birth_attempted"):
        return False
    if target_expiry_ms <= 0 or whitelist_cycle_anchor_ms <= 0:
        return False
    claimed = dict(metadata, issuance_target_email=email, standard_birth_attempted=True,
                   issuance_whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms)
    if not update_transaction_metadata(payment_id, claimed, statuses=("processing", "pending", "paid")):
        return False
    metadata.clear()
    metadata.update(claimed)
    return True


def _paid_standard_birth_probe_result_is_safe(
    result: dict | None, *, email: str, target_expiry_ms: int,
) -> bool:
    return bool(
        isinstance(result, dict)
        and result.get("standard_readonly_recovery_verified") is True
        and result.get("host_name") == "MainPanel"
        and result.get("email") == email
        and result.get("expiry_timestamp_ms") == target_expiry_ms
        and str(result.get("client_uuid") or "").strip()
        and str(result.get("sub_id") or "").strip()
    )


def _persist_payment_key_applied_for_retry(
    payment_id: str | None,
    metadata: dict,
    *,
    key_id: int,
    result: dict,
    action: str,
    target_expiry_ms: int,
    issued_key_value: str | None,
) -> None:
    if not payment_id or not key_id:
        return
    metadata["payment_applied_key_id"] = int(key_id)
    metadata["payment_applied_action"] = str(action or "")
    metadata["payment_applied_at"] = datetime.utcnow().isoformat(timespec="seconds")
    metadata["payment_applied_expiry_ms"] = int(target_expiry_ms)
    metadata["issued_panel_host"] = str(result.get("host_name") or "")
    metadata["issued_panel_email"] = str(result.get("email") or "")
    metadata["issued_panel_uuid"] = str(result.get("client_uuid") or "")
    metadata["issued_panel_expiry_ms"] = int(result.get("expiry_timestamp_ms") or target_expiry_ms)
    if issued_key_value:
        metadata["issued_panel_key_value"] = issued_key_value
    update_transaction_metadata(payment_id, metadata, statuses=("processing", "pending", "paid"))


def _recover_payment_applied_key_from_metadata(
    metadata: dict,
    *,
    user_id: int,
    target_expiry_ms: int,
    subscription_tier: str | None,
    readonly_standard_birth: bool = False,
) -> dict | None:
    applied_key_id = _coerce_positive_int(metadata.get("payment_applied_key_id"))
    if not applied_key_id:
        return None
    key_data = get_key_by_id(applied_key_id)
    if not key_data:
        return None
    if int(key_data.get("user_id") or 0) != int(user_id):
        return None
    if int(key_data.get("is_archived") or 0):
        return None
    expiry_dt = _parse_key_expiry_date(key_data.get("expiry_date"))
    if not expiry_dt:
        return None
    expiry_ms = int(expiry_dt.timestamp() * 1000)
    if readonly_standard_birth and (
        expiry_ms != int(target_expiry_ms) or key_data.get("host_name") != "MainPanel"
        or key_data.get("key_email") != metadata.get("issuance_target_email")
        or xui_api.normalize_subscription_tier(key_data.get("subscription_tier")) != xui_api.SUBSCRIPTION_TIER_STANDARD
    ):
        return None
    if expiry_ms + 1000 < int(target_expiry_ms):
        return None
    if not _payload_has_expected_subscription_shape(
        key_data, subscription_tier=subscription_tier, allow_native_vision=readonly_standard_birth,
    ):
        return None
    return key_data


def _extension_reconciliation_result_is_safe(
    result: dict | None,
    *,
    expected_client_uuid: str | None,
    target_expiry_ms: int,
    subscription_tier: str | None,
) -> bool:
    if not isinstance(result, dict):
        return False
    observed_uuid = str(result.get("client_uuid") or "").strip()
    expected_uuid = str(expected_client_uuid or "").strip()
    if not observed_uuid or (expected_uuid and observed_uuid != expected_uuid):
        return False
    if int(result.get("expiry_timestamp_ms") or 0) + 1000 < int(target_expiry_ms):
        return False

    payload = xui_api.subscription_payload_from_result(
        result,
        subscription_tier=subscription_tier,
    )
    return _payload_has_expected_subscription_shape(
        {
            "host_name": result.get("host_name"),
            "subscription_payload": payload,
        },
        subscription_tier=subscription_tier,
    )


async def _reconcile_extension_after_panel_error(
    *,
    host_name: str,
    email: str,
    client_uuid: str | None,
    target_expiry_ms: int,
    subscription_tier: str | None,
) -> dict | None:
    """Read after an ambiguous panel error and accept only the exact completed renewal."""
    expected_uuid = str(client_uuid or "").strip() or None
    candidates: list[dict | None] = []
    if expected_uuid:
        uuid_candidate = await xui_api.get_existing_key_by_uuid_on_host(
            host_name=host_name,
            client_uuid=expected_uuid,
            target_expiry_ms=target_expiry_ms,
            subscription_tier=subscription_tier,
        )
        if _extension_reconciliation_result_is_safe(
            uuid_candidate,
            expected_client_uuid=expected_uuid,
            target_expiry_ms=target_expiry_ms,
            subscription_tier=subscription_tier,
        ):
            logger.warning(
                "Recovered ambiguous extension after UUID read-back: host=%s email=%s uuid=%s",
                host_name,
                email,
                expected_uuid,
            )
            return uuid_candidate
        candidates.append(uuid_candidate)

    email_candidate = await xui_api.get_existing_key_by_email_on_host(
        host_name=host_name,
        email=email,
        target_expiry_ms=target_expiry_ms,
        subscription_tier=subscription_tier,
    )
    candidates.append(email_candidate)

    for candidate in candidates:
        if _extension_reconciliation_result_is_safe(
            candidate,
            expected_client_uuid=expected_uuid,
            target_expiry_ms=target_expiry_ms,
            subscription_tier=subscription_tier,
        ):
            logger.warning(
                "Recovered ambiguous extension after read-back: host=%s email=%s uuid=%s",
                host_name,
                email,
                expected_uuid or str((candidate or {}).get("client_uuid") or ""),
            )
            return candidate

    logger.error(
        "Extension read-back did not prove a complete renewal: host=%s email=%s expected_uuid=%s",
        host_name,
        email,
        expected_uuid or "missing",
    )
    return None


def _coerce_bool(value: object) -> bool:
    from shop_bot.domain.payments import _coerce_bool as _domain_coerce

    return _domain_coerce(value)


def _is_super_purchase_flow(
    *,
    host_name: str | None = None,
    is_super_key_flow: object = None,
    plan_data: dict | None = None,
) -> bool:
    if is_super_key_flow is not None:
        return _coerce_bool(is_super_key_flow)
    if plan_data:
        if _coerce_bool(plan_data.get("is_super")):
            return True
        plan_id = plan_data.get("plan_id")
        if plan_id not in (None, ""):
            try:
                return int(plan_id) == 0
            except (TypeError, ValueError):
                pass
    if host_name and _is_super_key_host_name(host_name):
        return True
    return False


def _purchase_email_candidates_after_panel_preflight(
    primary_email: str,
    candidate_emails: list[str],
    available_candidate_emails: list[str],
    *,
    panel_preflight_completed: bool,
) -> list[str]:
    if panel_preflight_completed:
        return list(available_candidate_emails)
    return list(available_candidate_emails or candidate_emails or [primary_email])


def _run_purchase_precheck(
    host_name: str | None,
    *,
    is_super_key_flow: object = None,
    plan_data: dict | None = None,
) -> PreCheckResult:
    """
    Run pre-check consistently for all purchase flows.

    Blitz standard flow does not have an entry in xui_hosts, so for it we
    allow host_not_found while still honoring global maintenance mode.
    """
    normalized_host = (host_name or "").strip()
    try:
        if normalized_host == blitz_api.BLITZ_HOST_NAME and not blitz_api.is_blitz_enabled():
            maintenance_message = (
                str(get_setting("maintenance_message") or "").strip()
                or "Покупка временно недоступна, ведутся технические работы. Попробуйте позже."
            )
            return PreCheckResult(
                allowed=False,
                reason="blitz_disabled",
                user_message=maintenance_message,
            )

        if _is_super_purchase_flow(
            host_name=normalized_host,
            is_super_key_flow=is_super_key_flow,
            plan_data=plan_data,
        ):
            if normalized_host:
                return check_purchase(normalized_host)
            return check_super_purchase()

        result = check_purchase(normalized_host)
        if normalized_host == blitz_api.BLITZ_HOST_NAME and not result.allowed and result.reason == "host_not_found":
            return PreCheckResult(allowed=True)
        return result
    except Exception as e:
        logger.error(
            "Purchase pre-check failed unexpectedly for host='%s' super_flow=%s: %s",
            normalized_host,
            _coerce_bool(is_super_key_flow),
            e,
            exc_info=True,
        )
        maintenance_message = (
            str(get_setting("maintenance_message") or "").strip()
            or "Покупка временно недоступна, ведутся технические работы. Попробуйте позже."
        )
        return PreCheckResult(
            allowed=False,
            reason="precheck_exception",
            user_message=maintenance_message,
        )


KEY_DETAILS_TIMEOUT_SECONDS = max(
    5.0,
    float(str(os.environ.get("KEY_DETAILS_TIMEOUT_SECONDS", "10")).strip() or "10"),
)


def _is_host_reachable(host_name: str | None) -> tuple[bool, str]:
    """Fast in-memory check using the health cache.  No I/O, no blocking.

    Returns ``(True, "")`` when the host appears reachable, or
    ``(False, user_message)`` when it is known to be down/degraded.
    """
    if not host_name:
        return True, ""
    snapshot = xui_api.get_host_health_snapshot(host_name)
    if snapshot and snapshot.status in {"unavailable", "maintenance"}:
        return False, f"Сервер «{host_name}» временно недоступен. Попробуйте позже."
    return True, ""


async def _run_purchase_precheck_async(
    host_name: str | None,
    *,
    is_super_key_flow: object = None,
    plan_data: dict | None = None,
) -> PreCheckResult:
    """
    Run pre-check in a worker thread to avoid blocking the asyncio loop.

    Pre-check may perform panel revalidation on unavailable hosts, and that network
    call must never freeze bot responsiveness for all users.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _run_purchase_precheck,
                host_name,
                is_super_key_flow=is_super_key_flow,
                plan_data=plan_data,
            ),
            timeout=PURCHASE_PRECHECK_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Purchase pre-check timed out for host='%s' super_flow=%s (timeout=%ss).",
            str(host_name or "").strip(),
            _coerce_bool(is_super_key_flow),
            PURCHASE_PRECHECK_TIMEOUT_SECONDS,
        )
        maintenance_message = (
            str(await asyncio.to_thread(get_setting, "maintenance_message") or "").strip()
            or "Покупка временно недоступна, ведутся технические работы. Попробуйте позже."
        )
        return PreCheckResult(
            allowed=False,
            reason="precheck_timeout",
            user_message=maintenance_message,
        )


def _resolve_selected_plan(data: dict) -> dict | None:
    super_plan_code = data.get("super_plan_code")
    if super_plan_code:
        super_plan = SUPER_KEY_PLAN_BY_CODE.get(super_plan_code)
        if not super_plan or not is_super_offer_available(super_plan):
            return None
        return {
            "plan_id": 0,
            "plan_name": super_plan["name"],
            "months": int(super_plan["months"]),
            "price": float(super_plan["price"]),
            "is_super": True,
            **promotion_metadata(super_plan),
        }

    plan_id = data.get("plan_id")
    plan = get_plan_by_id(plan_id) if plan_id is not None else None
    if not plan:
        return None
    return {
        "plan_id": int(plan["plan_id"]),
        "plan_name": plan.get("plan_name") or f"{plan['months']} мес.",
        "months": int(plan["months"]),
        "price": float(plan["price"]),
        "is_super": False,
        **promotion_metadata(plan),
    }


async def _reject_expired_promotion(callback, state, plan_data: dict) -> bool:
    """Recheck immediately before provider I/O, including slow prechecks."""
    if is_super_offer_available(plan_data):
        return False
    await state.clear()
    await callback.message.edit_text(
        PROMO_EXPIRED_TEXT,
        reply_markup=keyboards.create_back_to_menu_keyboard(),
    )
    return True


def _encode_cryptobot_payload_value(value: str | None) -> str:
    """Encode optional payload field safely for ':'-separated payloads."""
    if value is None:
        return ""
    raw = str(value)
    if not raw:
        return ""
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _resolve_transaction_plan_name(
    *,
    explicit_plan_name: str | None,
    plan_id: int,
    months: int,
    is_super_flow: bool,
) -> str:
    if explicit_plan_name:
        return explicit_plan_name

    plan = get_plan_by_id(plan_id) if plan_id else None
    if plan and plan.get("plan_name"):
        return str(plan["plan_name"])

    if is_super_flow:
        for plan_data in SUPER_KEY_PLANS:
            if not is_promotional_plan(plan_data) and int(plan_data["months"]) == months:
                return str(plan_data["name"])

    return f"{months} мес."


def _is_trial_key_email(email: str | None) -> bool:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return False
    local_part = normalized.split("@", 1)[0]
    return "trial" in local_part


def _is_trial_super_key_email(email: str | None) -> bool:
    return str(email or "").strip().lower().endswith("-trial-super@telegram.bot")


def _render_manual_migration_key_text(
    *,
    host_name: str,
    expiry_date: datetime,
    key_value: str | None,
    key_email: str | None,
    key_id: int | None,
) -> str:
    return get_purchase_success_text(
        action="выдан",
        key_number=int(key_id or 0),
        expiry_date=expiry_date,
        connection_string=key_value or "—",
        host_name=host_name,
        key_email=key_email,
    )


def _build_paid_extend_email(user_id: int, key_id: int, *, suffix: int | None = None) -> str:
    base = f"user{int(user_id)}-key{int(key_id)}"
    if suffix is not None and suffix > 0:
        base = f"{base}-r{suffix}"
    return f"{base}@vpn.bot"


def _resolve_extend_key_display_name(*, original_email: str | None) -> str:
    return "Выданный после триала" if _is_trial_key_email(original_email) else "Выданный после продления"


def _resolve_extend_email_target(
    *,
    user_id: int,
    key_id: int,
    original_email: str | None,
) -> str | None:
    del user_id, key_id, original_email
    # Keep panel email stable on renewal. Renaming only primary inbound breaks
    # multi-inbound subscriptions on 3x-ui 3.x.
    return None


def _resolve_available_extend_email(
    *,
    user_id: int,
    key_id: int,
    current_key_id: int,
) -> str | None:
    # Keep pool small: once there is a collision, reuse deterministic reserve suffixes.
    for suffix in [None, 1, 2, 3, 4, 5]:
        candidate = _build_paid_extend_email(user_id, key_id, suffix=suffix)
        existing = get_key_by_email(candidate)
        if not existing:
            return candidate
        try:
            existing_key_id = int(existing.get("key_id") or 0)
        except (TypeError, ValueError):
            existing_key_id = 0
        if existing_key_id == int(current_key_id):
            return candidate
    return None


def _resolve_key_display_name_from_metadata(metadata: dict) -> str:
    explicit = str(metadata.get("key_display_name") or "").strip()
    if explicit:
        return explicit
    action = str(metadata.get("action") or "").strip().lower()
    key_email = str(metadata.get("customer_email") or "").strip()
    if action == "extend":
        return _resolve_extend_key_display_name(original_email=key_email)
    if action == "new":
        return "Новый ключ"
    return "Ключ"


def _resolve_paid_at_from_metadata(metadata: dict) -> datetime:
    paid_at = subscription_dates.parse_datetime(metadata.get("paid_at"))
    return paid_at or datetime.now()


def _parse_referral_id(value: object) -> int | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,18}", text):
        return None
    result = int(text)
    return result if result <= 9223372036854775807 else None


def _apply_first_purchase_discount(
    base_price: Decimal, user_data: dict | None, *, plan_data: dict | None = None
) -> Decimal:
    if is_promotional_plan(plan_data):
        return Decimal(str(promotion_price(plan_data)))
    if not user_data:
        return base_price
    policy = referrals.get_policy()
    if not policy["enabled"]:
        return base_price
    user_id = _parse_referral_id(user_data.get("telegram_id"))
    referrer_id = _parse_referral_id(user_data.get("referred_by"))
    if not user_id or not referrer_id or referrer_id == user_id:
        return base_price
    if not get_user(referrer_id):
        return base_price
    return referrals.preview_discount_price(user_id, base_price)


async def _prepare_referral_invoice(user_id: int, plan_data: dict, provider: str) -> dict:
    """Reserve once in the shared database before any external invoice can exist."""
    base_price = Decimal(str(plan_data["price"]))
    if is_promotional_plan(plan_data):
        base_price = Decimal(str(promotion_price(plan_data)))
    return await asyncio.to_thread(
        referrals.prepare_discount_invoice, user_id, base_price,
        str(uuid.uuid4()), provider, eligible=not is_promotional_plan(plan_data),
        minimum_price=lava_api.MIN_INVOICE_AMOUNT if provider == "LAVA" else Decimal("0.01"),
    )


async def _mark_referral_invoice(discount: dict, payment_id: str | None = None, *, status: str = "issued", note: str = ""):
    claim_id = discount.get("claim_id")
    if claim_id:
        await asyncio.to_thread(referrals.mark_discount_invoice, claim_id, payment_id=payment_id, status=status, note=note)


def _referral_invoice_text(discount: dict) -> str:
    text = f"Сумма к оплате: <b>{discount['price_rub']:.2f} RUB</b>.\n"
    if discount.get("discount_cents", 0) > 0:
        text += "Скидка на первую покупку закреплена за этим счётом.\n"
    elif discount.get("reason") in {"reserved", "active_claim", "discount_reserved", "already_reserved", "already_claimed"}:
        text += "Скидка уже закреплена за ранее созданным счётом. Этот счёт выставлен по полной цене.\n"
    return text + "Нажмите на кнопку ниже для оплаты:"


class KeyPurchase(StatesGroup):
    waiting_for_plan_selection = State()


class Onboarding(StatesGroup):
    waiting_for_subscription_and_agreement = State()


class PaymentProcess(StatesGroup):
    waiting_for_email = State()
    waiting_for_payment_method = State()


class Broadcast(StatesGroup):
    waiting_for_message = State()
    waiting_for_button_option = State()
    waiting_for_button_text = State()
    waiting_for_button_url = State()
    waiting_for_confirmation = State()


class WithdrawStates(StatesGroup):
    waiting_for_details = State()


BROADCAST_SOURCE_PROMPT_TEXT = (
    "Пришлите сообщение, которое вы хотите разослать всем пользователям.\n"
    "Вы можете использовать форматирование (<b>жирный</b>, <i>курсив</i>).\n"
    "Также поддерживаются фото, видео и документы.\n\n"
    "Быстрый режим: отправьте нужный текст в чат, затем ответьте на него командой /broadcast."
)
BROADCAST_OPTIONS_TEXT = "Сообщение получено. Хотите добавить к нему кнопку со ссылкой?"  # noqa: RUF001
BROADCAST_REPLY_USAGE_TEXT = "Ответьте командой /broadcast на сообщение, которое нужно разослать."


def _build_broadcast_source_data(source_message: types.Message) -> dict[str, object]:
    message_json = _safe_broadcast_message_json(source_message)

    return {
        "message_to_send": message_json,
        "broadcast_source_chat_id": source_message.chat.id,
        "broadcast_source_message_id": source_message.message_id,
        "broadcast_text_fallback": _safe_broadcast_text(source_message),
        "broadcast_caption_fallback": _safe_broadcast_caption(source_message),
    }


def _safe_broadcast_message_json(source_message: types.Message) -> str:
    try:
        return source_message.model_dump_json()
    except Exception:  # noqa: BLE001
        logger.warning(
            "Broadcast source message JSON serialization failed; stable message ids will be used.",
            exc_info=True,
        )
        return ""


def _safe_broadcast_text(source_message: types.Message) -> str:
    return _safe_broadcast_message_attr(source_message, "html_text", "text")


def _safe_broadcast_caption(source_message: types.Message) -> str:
    return _safe_broadcast_message_attr(source_message, "html_caption", "caption")


def _safe_broadcast_message_attr(source_message: types.Message, rich_attr: str, plain_attr: str) -> str:
    for attr_name in (rich_attr, plain_attr):
        try:
            value = getattr(source_message, attr_name, None)
        except Exception:  # noqa: BLE001
            logger.warning("Broadcast source message %s extraction failed.", attr_name, exc_info=True)
            continue
        if value:
            return str(value)
    return ""


def _resolve_broadcast_source_ref(data: dict) -> tuple[int, int]:
    source_chat_id = data.get("broadcast_source_chat_id")
    source_message_id = data.get("broadcast_source_message_id")
    if source_chat_id is not None and source_message_id is not None:
        return int(source_chat_id), int(source_message_id)

    message_json = data.get("message_to_send")
    if not message_json:
        raise ValueError("missing")
    original_message = types.Message.model_validate_json(message_json)
    return int(original_message.chat.id), int(original_message.message_id)


async def _store_broadcast_source_message(state: FSMContext, source_message: types.Message) -> None:
    await state.update_data(**_build_broadcast_source_data(source_message))


async def _prompt_broadcast_button_options(message: types.Message, state: FSMContext) -> None:
    await message.answer(BROADCAST_OPTIONS_TEXT, reply_markup=keyboards.create_broadcast_options_keyboard())
    await state.set_state(Broadcast.waiting_for_button_option)


def is_valid_email(email: str) -> bool:
    """Validate email format and length for security"""
    if not email or len(email) > 254:  # RFC 5321 максимальная длина email
        return False
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    return re.match(pattern, email) is not None


async def _replace_message(
    message: types.Message,
    *,
    text: str,
    **kwargs,
) -> None:
    """Prefer editing the same message; send a new one only when edit is impossible."""
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        normalized = str(exc).lower()
        if "message is not modified" in normalized:
            return
        logger.debug("Replace-message edit failed, falling back to delete+send: %s", exc)
    else:
        return

    try:
        await message.delete()
    except TelegramBadRequest:
        pass

    try:
        await message.bot.send_message(
            chat_id=message.chat.id,
            text=text,
            **kwargs,
        )
        return
    except TelegramBadRequest as exc:
        logger.debug("Replace-message send failed after edit fallback: %s", exc)


def _is_stale_callback_error(exc: TelegramBadRequest) -> bool:
    normalized = str(exc).lower()
    return (
        "query is too old" in normalized
        or "query id is invalid" in normalized
        or "response timeout expired" in normalized
    )


async def _safe_callback_answer(
    callback: types.CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    """Answer callback and ignore stale callback-query errors."""
    try:
        if text is None:
            await callback.answer()
        else:
            await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as exc:
        if _is_stale_callback_error(exc):
            logger.debug("Ignoring stale callback answer error: %s", exc)
            return
        raise


async def _send_callback_chat_action(
    callback: types.CallbackQuery,
    action: ChatAction = ChatAction.TYPING,
) -> None:
    """Show Telegram-side activity for callback flows with noticeable latency."""
    message = callback.message
    if not isinstance(message, types.Message):
        return
    try:
        await message.bot.send_chat_action(chat_id=message.chat.id, action=action)
    except TelegramAPIError:
        logger.debug("Callback chat action failed: action=%s", action, exc_info=True)


async def show_main_menu(message: types.Message, edit_message: bool = False):
    try:
        await _show_main_menu_impl(message, edit_message)
    except Exception as e:
        logger.error("show_main_menu crashed for chat=%s: %s", message.chat.id, e, exc_info=True)
        try:
            await message.answer("Произошла ошибка. Попробуйте ещё раз или отправьте /start.")
        except Exception:
            logger.debug("Failed to send main-menu fallback error message", exc_info=True)


def _is_main_menu_request_text(text: str | None) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if not normalized:
        return False

    normalized = re.sub(r"^[^\w/]+", "", normalized).strip()
    return normalized in {"главное меню", "в меню", "меню", "/menu"} or "главное меню" in normalized


async def _resolve_incident_id_from_callback(
    callback: types.CallbackQuery,
    legacy_prefix: str,
) -> str | None:
    data = str(callback.data or "")
    if data.startswith(legacy_prefix):
        incident_id = data.removeprefix(legacy_prefix)
        if incident_id:
            return incident_id

    unseen = await asyncio.to_thread(get_unseen_incidents_for_user, callback.from_user.id)
    if not unseen:
        return None
    return str(unseen[0].get("incident_id") or "") or None


def _sync_load_main_menu_state(user_id: int):
    """SQLite reads for main menu; must run in a worker thread (asyncio.to_thread)."""
    user_db_data = get_user(user_id)
    user_keys = _get_customer_visible_user_keys(user_id)
    unseen = get_unseen_incidents_for_user(user_id)
    migrated_key_id = None
    if unseen:
        migrated_key_id = get_latest_migrated_key_id_for_user_incident(user_id, unseen[0]["incident_id"])
    return user_db_data, user_keys, unseen, migrated_key_id


async def _show_main_menu_impl(message: types.Message, edit_message: bool = False):
    user_id = message.chat.id
    user_db_data, user_keys, unseen, migrated_key_id = await asyncio.to_thread(_sync_load_main_menu_state, user_id)
    purchase_availability = await _resolve_purchase_availability()

    if unseen:
        incident = unseen[0]
        incident_text = f"⚠️ <b>Уведомление</b>\n\n{incident['message']}"
        builder = InlineKeyboardBuilder()
        if migrated_key_id:
            builder.button(
                text="🔑 Открыть новый ключ",
                callback_data="incident_open_key",
            )
        builder.button(
            text="✅ Понятно",
            callback_data="ack_incident",
        )
        builder.adjust(1)
        if edit_message:
            await _replace_message(
                message,
                text=incident_text,
                reply_markup=builder.as_markup(),
            )
        else:
            await message.answer(incident_text, reply_markup=builder.as_markup())
        return

    trial_standard_available = not bool(user_db_data and user_db_data.get("trial_used")) and bool(
        purchase_availability.get("standard_allowed")
    )
    trial_super_available = not bool(user_db_data and user_db_data.get("trial_super_used")) and bool(
        purchase_availability.get("super_allowed")
    )
    is_admin = str(user_id) == ADMIN_ID

    text = "🏠 <b>Главное меню</b>\n\nВыберите действие:"
    if not purchase_availability.get("standard_allowed") and not purchase_availability.get("super_allowed"):
        service_hint = str(purchase_availability.get("common_message") or "").strip()
        if service_hint:
            text = f"{text}\n\n🔧 {service_hint}"
    referral_policy = await asyncio.to_thread(referrals.get_policy)
    keyboard = keyboards.create_main_menu_keyboard(
        user_keys,
        trial_standard_available,
        trial_super_available,
        is_admin,
        show_standard_purchase=bool(purchase_availability.get("standard_allowed")),
        show_super_purchase=bool(purchase_availability.get("super_allowed")),
        show_referral_program=referral_policy["enabled"],
    )

    if edit_message:
        await _replace_message(
            message,
            text=text,
            reply_markup=keyboard,
        )
    else:
        await message.answer(text, reply_markup=keyboard)


def registration_required(f):
    @wraps(f)
    async def decorated_function(event: types.Update, *args, **kwargs):
        user_id = event.from_user.id
        user_data = await asyncio.to_thread(get_user, user_id)
        if user_data:
            return await f(event, *args, **kwargs)
        else:
            message_text = "Пожалуйста, для начала работы со мной, отправьте команду /start"
            if isinstance(event, types.CallbackQuery):
                await event.answer(message_text, show_alert=True)
            else:
                await event.answer(message_text)

    return decorated_function


def get_user_router() -> Router:
    user_router = Router()
    from shop_bot import mobile_app
    mobile_app.register_bot_handlers(user_router, get_user)

    @user_router.message(CommandStart())
    async def start_handler(message: types.Message, state: FSMContext, command: CommandObject):
        if await mobile_app.handle_start(message, command.args, get_user):
            return
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        referrer_id = None

        referral_policy = await asyncio.to_thread(referrals.get_policy)
        if referral_policy["enabled"] and command.args and command.args.startswith("ref_"):
            potential_referrer_id = _parse_referral_id(command.args[4:])
            if potential_referrer_id and potential_referrer_id != user_id and await asyncio.to_thread(get_user, potential_referrer_id):
                referrer_id = potential_referrer_id

        await asyncio.to_thread(register_user_if_not_exists, user_id, username, referrer_id)
        user_data = await asyncio.to_thread(get_user, user_id)

        if user_data and user_data.get("agreed_to_terms"):
            await message.answer(
                f"👋 Снова здравствуйте, {html.bold(message.from_user.full_name)}!",
                reply_markup=keyboards.main_reply_keyboard,
            )
            await show_main_menu(message)
            return

        terms_url = get_setting("terms_url")
        privacy_url = get_setting("privacy_url")
        channel_url = get_setting("channel_url")

        if not channel_url or not terms_url or not privacy_url:
            set_terms_agreed(user_id)
            await show_main_menu(message)
            return

        is_subscription_forced = get_setting("force_subscription") == "true"

        show_welcome_screen = (is_subscription_forced and channel_url) or (terms_url and privacy_url)

        if not show_welcome_screen:
            set_terms_agreed(user_id)
            await show_main_menu(message)
            return

        welcome_parts = ["<b>Добро пожаловать!</b>\n"]

        if is_subscription_forced and channel_url:
            welcome_parts.append("Для доступа ко всем функциям, пожалуйста, подпишитесь на наш канал.\n")

        if terms_url and privacy_url:
            welcome_parts.append(
                "Также необходимо ознакомиться с нашими Условиями использования и Политикой конфиденциальности."
            )
        elif terms_url:
            welcome_parts.append("Также необходимо ознакомиться и принять наши Условия использования.")
        elif privacy_url:
            welcome_parts.append("Также необходимо ознакомиться с нашей Политикой конфиденциальности.")

        welcome_parts.append("\nПосле этого нажмите кнопку ниже.")
        final_text = "\n".join(welcome_parts)

        await send_effect_message(
            message.bot,
            chat_id=message.chat.id,
            text=final_text,
            effect_setting_key=WELCOME_MESSAGE_EFFECT_ID_SETTING,
            reply_markup=keyboards.create_welcome_keyboard(
                channel_url=channel_url,
                is_subscription_forced=is_subscription_forced,
                terms_url=terms_url,
                privacy_url=privacy_url,
            ),
            disable_web_page_preview=True,
        )
        await state.set_state(Onboarding.waiting_for_subscription_and_agreement)

    @user_router.callback_query(
        Onboarding.waiting_for_subscription_and_agreement, F.data == "check_subscription_and_agree"
    )
    async def check_subscription_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        user_id = callback.from_user.id
        channel_url = get_setting("channel_url")
        is_subscription_forced = get_setting("force_subscription") == "true"

        if not is_subscription_forced or not channel_url:
            await process_successful_onboarding(callback, state)
            return

        try:
            if "@" not in channel_url and "t.me/" not in channel_url:
                logger.error(f"Неверный формат URL канала: {channel_url}. Пропускаем проверку подписки.")
                await process_successful_onboarding(callback, state)
                return

            channel_id = "@" + channel_url.split("/")[-1] if "t.me/" in channel_url else channel_url
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)

            if member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]:
                await process_successful_onboarding(callback, state)
            else:
                await _safe_callback_answer(
                    callback,
                    "Вы еще не подписались на канал. Пожалуйста, подпишитесь и попробуйте снова.",
                    show_alert=True,
                )

        except Exception as e:
            logger.error(f"Ошибка при проверке подписки для user_id {user_id} на канал {channel_url}: {e}")
            await _safe_callback_answer(
                callback,
                "Не удалось проверить подписку. Убедитесь, что бот является администратором канала. Попробуйте позже.",
                show_alert=True,
            )

    @user_router.message(StateFilter("*"), F.text.func(_is_main_menu_request_text))
    @registration_required
    async def main_menu_handler(message: types.Message, state: FSMContext):
        # Home button must work from any flow/state (onboarding, purchase, support forms).
        await state.clear()
        await show_main_menu(message)

    @user_router.callback_query(StateFilter("*"), F.data == "channel_gate_check")
    async def channel_gate_confirmed_handler(callback: types.CallbackQuery, state: FSMContext):
        username = callback.from_user.username or callback.from_user.full_name
        register_user_if_not_exists(callback.from_user.id, username, None)
        await process_successful_onboarding(callback, state)

    @user_router.message(Onboarding.waiting_for_subscription_and_agreement)
    async def onboarding_fallback_handler(message: types.Message):
        await message.answer("Пожалуйста, выполните требуемые действия и нажмите на кнопку в сообщении выше.")

    @user_router.callback_query(F.data.in_({"show_main_menu", "back_to_main_menu"}))
    @registration_required
    async def back_to_main_menu_handler(callback: types.CallbackQuery):
        await _safe_callback_answer(callback)
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "ack_incident")
    @user_router.callback_query(F.data.startswith("ack_incident_"))
    @registration_required
    async def acknowledge_incident_handler(callback: types.CallbackQuery):
        await _safe_callback_answer(callback)
        incident_id = await _resolve_incident_id_from_callback(callback, "ack_incident_")
        if not incident_id:
            await show_main_menu(callback.message, edit_message=True)
            return
        await asyncio.to_thread(mark_incident_seen, callback.from_user.id, incident_id)
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "incident_open_key")
    @user_router.callback_query(F.data.startswith("incident_open_key_"))
    @registration_required
    async def incident_open_key_handler(callback: types.CallbackQuery):
        await _safe_callback_answer(callback)
        incident_id = await _resolve_incident_id_from_callback(callback, "incident_open_key_")
        if not incident_id:
            await show_main_menu(callback.message, edit_message=True)
            return
        await asyncio.to_thread(mark_incident_seen, callback.from_user.id, incident_id)
        await asyncio.to_thread(set_incident_opened_new_key_at, callback.from_user.id, incident_id)
        key_id_to_show = await asyncio.to_thread(
            get_latest_migrated_key_id_for_user_incident,
            callback.from_user.id,
            incident_id,
        )
        if not key_id_to_show:
            await callback.message.edit_text(
                "✅ Уведомление прочитано.\n\nОбновленный ключ находится в разделе «Мои ключи».",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return
        await _show_key_details(
            callback,
            key_id_to_show,
            loading_text="Открываю ваш обновленный ключ...",
        )

    @user_router.callback_query(F.data == "show_profile")
    @registration_required
    async def profile_handler_callback(callback: types.CallbackQuery):
        await _safe_callback_answer(callback)
        user_id = callback.from_user.id
        user_db_data = await asyncio.to_thread(get_user, user_id)
        user_keys = await asyncio.to_thread(_get_customer_visible_user_keys, user_id)
        if not user_db_data:
            await _safe_callback_answer(callback, "Не удалось получить данные профиля.", show_alert=True)
            return
        username = html.bold(user_db_data.get("username", "Пользователь"))
        total_spent, total_months = user_db_data.get("total_spent", 0), user_db_data.get("total_months", 0)
        now = datetime.now()
        active_keys = [key for key in user_keys if datetime.fromisoformat(key["expiry_date"]) > now]
        if active_keys:
            latest_key = max(active_keys, key=lambda k: datetime.fromisoformat(k["expiry_date"]))
            latest_expiry_date = datetime.fromisoformat(latest_key["expiry_date"])
            time_left = latest_expiry_date - now
            vpn_status_text = get_vpn_active_text(time_left.days, time_left.seconds // 3600)
        elif user_keys:
            vpn_status_text = VPN_INACTIVE_TEXT
        else:
            vpn_status_text = VPN_NO_DATA_TEXT
        whitelist_status_text = _build_profile_whitelist_statuses_text(
            _filter_whitelist_statuses_for_customer_keys(
                await asyncio.to_thread(get_user_whitelist_traffic_statuses, user_id, bot_source=BOT_SOURCE),
                user_keys,
            )
        )
        final_text = get_profile_text(
            username,
            total_spent,
            total_months,
            vpn_status_text,
            whitelist_status_text=whitelist_status_text,
        )
        await _replace_message(
            callback.message,
            text=final_text,
            reply_markup=keyboards.create_back_to_menu_keyboard(),
        )

    @user_router.callback_query(F.data == "start_broadcast")
    @registration_required
    async def start_broadcast_handler(callback: types.CallbackQuery, state: FSMContext):
        if str(callback.from_user.id) != ADMIN_ID:
            await _safe_callback_answer(callback, "У вас нет прав.", show_alert=True)
            return

        await _safe_callback_answer(callback)
        await callback.message.edit_text(
            BROADCAST_SOURCE_PROMPT_TEXT, reply_markup=keyboards.create_broadcast_cancel_keyboard()
        )
        await state.set_state(Broadcast.waiting_for_message)

    @user_router.message(Command("broadcast"))
    @registration_required
    async def broadcast_from_reply_handler(message: types.Message, state: FSMContext):
        if str(message.from_user.id) != ADMIN_ID:
            await message.answer("У вас нет прав.")  # noqa: RUF001
            return

        source_message = message.reply_to_message
        if not source_message:
            await message.answer(
                BROADCAST_REPLY_USAGE_TEXT,
                reply_markup=keyboards.create_broadcast_cancel_keyboard(),
            )
            await state.set_state(Broadcast.waiting_for_message)
            return

        await _store_broadcast_source_message(state, source_message)
        await _prompt_broadcast_button_options(message, state)

    @user_router.message(Broadcast.waiting_for_message)
    async def broadcast_message_received_handler(message: types.Message, state: FSMContext):
        await _store_broadcast_source_message(state, message)
        await _prompt_broadcast_button_options(message, state)

    @user_router.callback_query(Broadcast.waiting_for_button_option, F.data == "broadcast_add_button")
    async def add_button_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        await _safe_callback_answer(callback)
        await callback.message.edit_text(
            "Хорошо. Теперь отправьте мне текст для кнопки.", reply_markup=keyboards.create_broadcast_cancel_keyboard()
        )
        await state.set_state(Broadcast.waiting_for_button_text)

    @user_router.message(Broadcast.waiting_for_button_text)
    async def button_text_received_handler(message: types.Message, state: FSMContext):
        await state.update_data(button_text=message.text)
        await message.answer(
            "Текст кнопки получен. Теперь отправьте ссылку (URL), куда она будет вести.",
            reply_markup=keyboards.create_broadcast_cancel_keyboard(),
        )
        await state.set_state(Broadcast.waiting_for_button_url)

    @user_router.message(Broadcast.waiting_for_button_url)
    async def button_url_received_handler(message: types.Message, state: FSMContext, bot: Bot):
        url_to_check = message.text

        is_valid = await is_url_reachable(url_to_check)

        if not is_valid:
            await message.answer(
                "❌ **Ссылка не прошла проверку.**\n\n"
                "Пожалуйста, убедитесь, что:\n"
                "1. Ссылка начинается с `http://` или `https://`.\n"
                "2. Доменное имя корректно (например, `example.com`).\n"
                "3. Сайт доступен в данный момент.\n\n"
                "Попробуйте еще раз."
            )
            return

        await state.update_data(button_url=url_to_check)
        await show_broadcast_preview(message, state, bot)

    @user_router.callback_query(Broadcast.waiting_for_button_option, F.data == "broadcast_skip_button")
    async def skip_button_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await _safe_callback_answer(callback)
        await state.update_data(button_text=None, button_url=None)
        await show_broadcast_preview(callback.message, state, bot)

    async def show_broadcast_preview(message: types.Message, state: FSMContext, bot: Bot):
        data = await state.get_data()
        try:
            source_chat_id, source_message_id = _resolve_broadcast_source_ref(data)
        except (TypeError, ValueError) as exc:
            logger.warning("Broadcast preview failed: source message is missing: %s", exc)
            await state.clear()
            await message.answer(
                "Не удалось найти исходное сообщение для рассылки. Запустите рассылку заново.",  # noqa: RUF001
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        button_text = data.get("button_text")
        button_url = data.get("button_url")

        preview_keyboard = None
        if button_text and button_url:
            builder = InlineKeyboardBuilder()
            builder.button(text=button_text, url=button_url)
            preview_keyboard = builder.as_markup()

        await message.answer(
            "Вот так будет выглядеть ваше сообщение. Отправляем?",
            reply_markup=keyboards.create_broadcast_confirmation_keyboard(),
        )

        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=source_chat_id,
            message_id=source_message_id,
            reply_markup=preview_keyboard,
        )

        await state.set_state(Broadcast.waiting_for_confirmation)

    @user_router.callback_query(Broadcast.waiting_for_confirmation, F.data == "confirm_broadcast")
    async def confirm_broadcast_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await _safe_callback_answer(callback, "Начинаю рассылку...")
        await callback.message.edit_text("⏳ Начинаю рассылку... Это может занять некоторое время.")

        data = await state.get_data()
        try:
            source_chat_id, source_message_id = _resolve_broadcast_source_ref(data)
        except (TypeError, ValueError) as exc:
            logger.warning("Broadcast confirm failed: source message is missing: %s", exc)
            await state.clear()
            await callback.message.answer(
                "Не удалось найти исходное сообщение для рассылки. Запустите рассылку заново.",  # noqa: RUF001
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        button_text = data.get("button_text")
        button_url = data.get("button_url")

        final_keyboard = None
        if button_text and button_url:
            builder = InlineKeyboardBuilder()
            builder.button(text=button_text, url=button_url)
            final_keyboard = builder.as_markup()

        await state.clear()

        users = get_all_users()
        logger.info(f"Broadcast: Starting to iterate over {len(users)} users.")

        sent_count = 0
        failed_count = 0
        banned_count = 0

        for user in users:
            user_id = user["telegram_id"]
            if user.get("is_banned"):
                banned_count += 1
                continue

            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                    reply_markup=final_keyboard,
                )

                sent_count += 1
                await asyncio.sleep(0.1)
            except Exception as e:
                failed_count += 1
                logger.warning(f"Failed to send broadcast message to user {user_id}: {e}")

        await callback.message.answer(
            f"✅ Рассылка завершена!\n\n"
            f"👍 Отправлено: {sent_count}\n"
            f"👎 Не удалось отправить: {failed_count}\n"
            f"🚫 Пропущено (забанены): {banned_count}"
        )
        await show_main_menu(callback.message)

    @user_router.callback_query(StateFilter(Broadcast), F.data == "cancel_broadcast")
    async def cancel_broadcast_handler(callback: types.CallbackQuery, state: FSMContext):
        await _safe_callback_answer(callback, "Рассылка отменена.")
        await state.clear()
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "show_referral_program")
    @registration_required
    async def referral_program_handler(callback: types.CallbackQuery):
        policy = await asyncio.to_thread(referrals.get_policy)
        if not policy["enabled"]:
            await _safe_callback_answer(callback, "Реферальная программа сейчас отключена.", show_alert=True)
            return
        await _safe_callback_answer(callback)
        user_id = callback.from_user.id
        summary = await asyncio.to_thread(referrals.get_user_summary, user_id)
        bot_username = (await callback.bot.get_me()).username
        referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        text = (
            "🤝 <b>Реферальная программа</b>\n\n"
            f"Получайте <b>{policy['percentage']}%</b> с каждой покупки приглашённых друзей.\n"
            f"Друг получает <b>{policy['discount']}%</b> скидки на первую покупку, кроме промотарифа.\n\n"
            f"<b>Ваша реферальная ссылка:</b>\n<code>{html_escape(referral_link)}</code>\n\n"
            f"<b>Приглашено пользователей:</b> {summary['count']}\n"
            f"<b>Доступно к выводу:</b> {summary['balance']:.2f} RUB\n"
            f"<b>В заявках на вывод:</b> {summary['reserved']:.2f} RUB\n"
            f"<b>Минимум для вывода:</b> {policy['minimum']:.2f} RUB"
        )
        if summary.get("pending"):
            text += f"\n\nЗаявка №{summary['pending']['id']} ожидает обработки."
        builder = InlineKeyboardBuilder()
        if not summary.get("pending") and Decimal(str(summary["balance"])) >= policy["minimum"]:
            builder.button(text="💸 Оставить заявку на вывод", callback_data="withdraw_request")
        builder.button(text="⬅️ Назад", callback_data="back_to_main_menu")
        builder.adjust(1)
        await callback.message.edit_text(text, reply_markup=builder.as_markup())

    @user_router.callback_query(F.data == "withdraw_request")
    @registration_required
    async def withdraw_request_handler(callback: types.CallbackQuery, state: FSMContext):
        policy = await asyncio.to_thread(referrals.get_policy)
        if not policy["enabled"]:
            await _safe_callback_answer(callback, "Реферальная программа сейчас отключена.", show_alert=True)
            return
        summary = await asyncio.to_thread(referrals.get_user_summary, callback.from_user.id)
        if summary.get("pending"):
            await _safe_callback_answer(callback, f"Заявка №{summary['pending']['id']} уже ожидает обработки.", show_alert=True)
            return
        if Decimal(str(summary["balance"])) < policy["minimum"]:
            await _safe_callback_answer(callback, f"Минимум для вывода: {policy['minimum']:.2f} RUB.", show_alert=True)
            return
        await _safe_callback_answer(callback)
        builder = InlineKeyboardBuilder()
        builder.button(text="Отменить", callback_data="cancel_referral_withdrawal")
        await callback.message.edit_text(
            f"Доступно к выводу: {summary['balance']:.2f} RUB.\n"
            "Отправьте реквизиты: номер карты или номер телефона и банк.\n"
            "Не отправляйте PIN, CVV/CVC или код из SMS.",
            reply_markup=builder.as_markup(),
        )
        await state.set_state(WithdrawStates.waiting_for_details)

    @user_router.callback_query(F.data == "cancel_referral_withdrawal")
    async def cancel_referral_withdrawal(callback: types.CallbackQuery, state: FSMContext):
        await state.clear()
        await _safe_callback_answer(callback, "Заявка отменена.")
        await show_main_menu(callback.message, edit_message=True)

    async def is_referral_admin(user_id: int) -> bool:
        configured = await asyncio.to_thread(get_setting, "admin_telegram_id")
        return bool(configured and str(user_id) == str(configured).strip())

    async def withdrawal_admin_notice(message: types.Message, request: dict, text: str):
        try:
            await message.answer(text)
        except TelegramAPIError:
            logger.warning("Withdrawal %s resolved, administrator notification delivery failed", request["id"])

    async def notify_withdrawal_resolution(message: types.Message, request: dict, *, paid: bool):
        from shop_bot.data_manager.referral_notifications import deliver_pending_withdrawal_notifications

        # The durable outbox owns delivery and retries; this is only a prompt drain.
        del paid
        try:
            await deliver_pending_withdrawal_notifications(message.bot, source=BOT_SOURCE)
        except Exception:
            logger.exception("Withdrawal %s resolved; queued notice will be retried", request["id"])

    @user_router.message(Command(commands=["approve_withdraw"]))
    async def approve_withdraw_handler(message: types.Message, command: CommandObject):
        if not await is_referral_admin(message.from_user.id):
            return
        request_id = _parse_referral_id(command.args)
        if request_id is None:
            await message.answer("Используйте /approve_withdraw НОМЕР_ЗАЯВКИ. Деньги переводятся вручную.")
            return
        request = await asyncio.to_thread(referrals.get_withdrawal, request_id)
        if not request or request["status"] != "pending":
            await message.answer("Заявка не найдена или уже обработана.")
            return
        builder = InlineKeyboardBuilder()
        builder.button(
            text="✅ Деньги переведены — отметить выплату",
            callback_data=f"referral_withdraw_paid:{request['id']}",
        )
        await message.answer(
            f"Заявка №{request['id']}, пользователь {request['user_id']}, сумма <b>{request['amount']:.2f} RUB</b>.\n"
            f"Реквизиты: <code>{html_escape(str(request['details']))}</code>\n\n"
            "Переведите деньги вручную, проверьте успешный перевод и только затем подтвердите выплату кнопкой.\n"
            "Нажатие кнопки сохраняет отметку о выплате и не переводит деньги.",
            reply_markup=builder.as_markup(),
        )

    async def withdrawal_callback_answer(callback: types.CallbackQuery, text: str, *, show_alert: bool = False):
        try:
            await _safe_callback_answer(callback, text, show_alert=show_alert)
        except (TelegramAPIError, TimeoutError):
            logger.warning("Withdrawal action callback acknowledgement failed; stored decision is unchanged.")

    async def withdrawal_callback_status(callback: types.CallbackQuery, request: dict):
        if request["status"] == "paid":
            text = f"✅ Выплата по заявке №{request['id']} на {request['amount']:.2f} RUB отмечена как выполненная."
        else:
            text = (
                f"❌ Вывод по заявке №{request['id']} на {request['amount']:.2f} RUB отменён.\n"
                "Деньги не отправлены. Сумма возвращена на реферальный баланс клиента."
            )
        username = str(request.get("username") or "").strip().lstrip("@")
        if username == f"user_{request['user_id']}" or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", username):
            identity = "username не указан"
        else:
            identity = "@" + username
        text += f"\nКлиент: {identity}\nID: <code>{request['user_id']}</code>"
        try:
            await callback.message.edit_text(text, reply_markup=None)
        except (TelegramAPIError, TimeoutError):
            logger.warning("Withdrawal %s resolved, administrator message edit failed", request["id"])

    @user_router.callback_query(
        F.data.startswith("referral_withdraw_paid:") | F.data.startswith("referral_withdraw_rejected:")
    )
    async def handle_referral_withdrawal_decision(callback: types.CallbackQuery):
        message = callback.message
        if (
            not isinstance(message, types.Message) or message.chat.type != "private"
            or message.chat.id != callback.from_user.id or callback.from_user.is_bot
            or message.date.timestamp() <= 0
        ):
            await withdrawal_callback_answer(callback, "Решение доступно только в личном чате с ботом.", show_alert=True)
            return
        if not await asyncio.to_thread(referral_withdrawal_actions.is_reviewer, callback.from_user.id):
            await withdrawal_callback_answer(callback, "Недостаточно прав.", show_alert=True)
            return
        parsed = referral_withdrawal_actions.parse_callback(callback.data)
        if parsed is None:
            await withdrawal_callback_answer(callback, "Некорректный номер заявки.", show_alert=True)
            return
        action, request_id = parsed
        note = (
            "Администратор подтвердил выполненный ручной перевод через Telegram."
            if action == "paid" else "Администратор отменил вывод: деньги не отправлялись. Сумма возвращена на баланс."
        )
        try:
            request = await asyncio.to_thread(
                referrals.resolve_withdrawal, request_id, action=action, admin_id=callback.from_user.id, note=note,
            )
        except Exception:  # noqa: BLE001 - read the authoritative outcome after a possible commit; never repeat a decision.
            try:
                existing = await asyncio.to_thread(referrals.get_withdrawal, request_id)
            except Exception:  # noqa: BLE001 - fail closed when storage is unavailable.
                existing = None
            if existing and existing["status"] in {"paid", "rejected"}:
                await withdrawal_callback_answer(callback, "Заявка уже обработана. Сохранённое решение не изменено.", show_alert=True)
                await withdrawal_callback_status(callback, existing)
            else:
                logger.warning("Withdrawal %s decision could not be confirmed; no automatic repeat.", request_id)
                await withdrawal_callback_answer(callback, "Не удалось подтвердить решение: заявка не найдена или недоступна. Проверьте её в админке.", show_alert=True)
            return
        if request.get("is_new"):
            # The outbox retains the originating bot; a decision in another bot
            # must leave delivery to that source's worker.
            if request.get("source") == BOT_SOURCE:
                await notify_withdrawal_resolution(message, request, paid=action == "paid")
            answer = "Выплата отмечена." if action == "paid" else "Вывод отменён, сумма возвращена клиенту."
        else:
            answer = "Это решение уже сохранено. Повторная операция не выполнялась."
        await withdrawal_callback_answer(callback, answer)
        await withdrawal_callback_status(callback, request)

    @user_router.message(Command(commands=["decline_withdraw"]))
    async def decline_withdraw_handler(message: types.Message, command: CommandObject):
        if not await is_referral_admin(message.from_user.id):
            return
        args = str(command.args or "").strip().split(maxsplit=1)
        request_id = _parse_referral_id(args[0]) if args else None
        if request_id is None:
            await message.answer("Используйте /decline_withdraw НОМЕР_ЗАЯВКИ [причина].")
            return
        existing = await asyncio.to_thread(referrals.get_withdrawal, request_id)
        if not existing or existing["status"] != "pending":
            await message.answer("Заявка не найдена или уже обработана.")
            return
        try:
            request = await asyncio.to_thread(
                referrals.resolve_withdrawal, request_id, action="rejected", admin_id=message.from_user.id,
                note=args[1] if len(args) > 1 else "Отклонено администратором через Telegram.",
            )
        except ValueError as exc:
            await message.answer(html_escape(str(exc)))
            return
        await notify_withdrawal_resolution(message, request, paid=False)
        await withdrawal_admin_notice(
            message, request, f"Заявка №{request['id']} отклонена. Сумма возвращена на доступный баланс.",
        )

    @user_router.message(WithdrawStates.waiting_for_details)
    @registration_required
    async def process_withdraw_details(message: types.Message, state: FSMContext):
        policy = await asyncio.to_thread(referrals.get_policy)
        if not policy["enabled"]:
            await state.clear()
            await message.answer("Реферальная программа сейчас отключена. Новая заявка не создана.")
            return
        if not message.text or not 5 <= len(message.text.strip()) <= 1000:
            await message.answer("Отправьте реквизиты текстом: от 5 до 1000 символов.")
            return
        try:
            request = await asyncio.to_thread(
                referrals.create_withdrawal, message.from_user.id, message.text.strip(), source=BOT_SOURCE,
            )
        except ValueError as exc:
            await message.answer(html_escape(str(exc)))
            await state.clear()
            return
        await state.clear()
        if not request.get("is_new", True):
            await message.answer(f"Заявка №{request['id']} уже ожидает обработки. Повторная заявка не создана.")
            return
        from shop_bot.data_manager.referral_admin_notifications import deliver

        try:
            delivery = await deliver(message.bot, source=BOT_SOURCE, withdrawal_id=request["id"])
            notification_sent = delivery["sent"] > 0 and delivery["failed"] == 0
        except Exception:
            logger.warning("Withdrawal %s saved; reminders will be retried by worker", request["id"])
            notification_sent = False
        if notification_sent:
            await message.answer(
                f"Заявка №{request['id']} на {request['amount']:.2f} RUB сохранена и отправлена администратору. "
                "Сумма зарезервирована до обработки заявки."
            )
        else:
            await message.answer(
                f"Заявка №{request['id']} на {request['amount']:.2f} RUB сохранена в кабинете администратора, "
                "но уведомление в Telegram отправить не удалось всем получателям. Отправка будет повторена автоматически. Повторять заявку не нужно."
            )

    @user_router.callback_query(F.data == "show_help")
    @registration_required
    async def about_handler(callback: types.CallbackQuery):
        await _safe_callback_answer(callback)

        support_user = get_setting("support_user")
        support_text = get_setting("support_text")
        support_contact_visible = keyboards.is_support_contact_visible()

        if support_user is None and support_text is None:
            next_text = "Информация о поддержке не установлена. Установите её в админ-панели."
            next_markup = keyboards.create_back_to_menu_keyboard()
        else:
            next_text = _build_support_text(
                support_text,
                support_contact_visible=support_contact_visible,
            )
            next_markup = keyboards.create_support_keyboard(
                support_user,
                contact_visible=support_contact_visible,
            )

        await _replace_message(
            callback.message,
            text=next_text,
            reply_markup=next_markup,
        )

    @user_router.callback_query(F.data == "manage_keys")
    @registration_required
    async def manage_keys_handler(callback: types.CallbackQuery):
        await _safe_callback_answer(callback)
        user_id = callback.from_user.id
        user_keys = await asyncio.to_thread(_get_customer_visible_user_keys, user_id)
        purchase_availability = await _resolve_purchase_availability()
        await _replace_message(
            callback.message,
            text="Ваши ключи:" if user_keys else "У вас пока нет ключей.",
            reply_markup=keyboards.create_keys_management_keyboard(
                user_keys,
                show_standard_purchase=bool(purchase_availability.get("standard_allowed")),
                show_super_key=bool(purchase_availability.get("super_allowed")),
            ),
        )

    def _read_trial_days(
        setting_key: str,
        *,
        default: int = 2,
        min_days: int = 1,
        max_days: int | None = None,
    ) -> int:
        raw_value = (get_setting(setting_key) or "").strip()
        try:
            days = max(int(raw_value), min_days)
            if max_days is not None and days > max_days:
                logger.warning(
                    "Trial duration setting '%s'=%s exceeds max %s, capping.",
                    setting_key,
                    days,
                    max_days,
                )
                days = max_days
            return days
        except (TypeError, ValueError):
            return default

    @user_router.callback_query(F.data == "get_trial")
    @user_router.callback_query(F.data == "get_trial_standard")
    @registration_required
    async def trial_standard_handler(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        user_db_data = get_user(user_id) or {}

        if user_db_data.get("trial_used"):
            await _safe_callback_answer(callback, "Пробный доступ «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)» уже использован.", show_alert=True)
            return

        standard_host_name = _get_standard_key_host_name()
        if not await asyncio.to_thread(is_customer_host_visible, standard_host_name):
            await _safe_callback_answer(callback, "Пробный доступ «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)» временно недоступен.", show_alert=True)
            return
        standard_host = get_host(standard_host_name)
        if not standard_host:
            await _safe_callback_answer(callback, "Пробный доступ «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)» временно недоступен.", show_alert=True)
            return

        # ---- host availability pre-check (same as purchase flow) ----
        precheck = await _run_purchase_precheck_async(
            standard_host_name,
            is_super_key_flow=False,
        )
        if not precheck.allowed:
            await _safe_callback_answer(callback)
            await _replace_message(
                callback.message,
                text=f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        # Standard trial is capped to 2 days to keep free access bounded.
        trial_days = _read_trial_days("trial_duration_days", default=2, max_days=2)
        await _safe_callback_answer(callback)
        await callback.message.edit_text(f"⏳ Создаю пробный доступ «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)» на {trial_days} дн...")

        lock = await _try_acquire_trial_standard_issue_lock(user_id)
        if lock is None:
            await _replace_message(
                callback.message,
                text=("⏳ Пробный доступ «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)» уже создаётся.\n\nПодождите несколько секунд, я проверяю результат."),
            )
            return

        try:
            reachable, unavail_msg = _is_host_reachable(standard_host_name)
            if not reachable:
                await _replace_message(
                    callback.message,
                    text=f"🔧 {unavail_msg}",
                    reply_markup=keyboards.create_back_to_menu_keyboard(),
                )
                return

            existing_trial_key = _existing_active_trial_standard_key(user_id, standard_host_name)
            if existing_trial_key:
                result = _trial_result_from_existing_key(existing_trial_key)
                set_trial_used(user_id)
                final_text = get_trial_success_text(
                    expiry_date=_naive_utc_from_timestamp_ms(result["expiry_timestamp_ms"]),
                    connection_string=result.get("connection_string"),
                    subscription_link=result.get("subscription_link"),
                    host_name=standard_host_name,
                    key_email=result.get("email"),
                    whitelist_limit_gb=5,
                )
                await callback.message.edit_text(
                    text=final_text,
                    reply_markup=keyboards.create_key_info_keyboard(
                        int(existing_trial_key["key_id"]),
                        show_refresh=False,
                        show_qr=not bool(result.get("subscription_link")),
                    ),
                )
                return

            key_number = get_next_key_number(user_id)
            target_expiry_ms = _trial_standard_target_expiry_ms(trial_days)
            candidate_emails: list[str] = []
            seen_candidate_emails: set[str] = set()
            for offset in range(0, 8):
                candidate_email = _trial_standard_email(user_id, key_number + offset)
                normalized_candidate_email = candidate_email.strip().lower()
                if normalized_candidate_email in seen_candidate_emails:
                    continue
                seen_candidate_emails.add(normalized_candidate_email)
                if get_key_by_email(candidate_email):
                    logger.warning(
                        "Standard trial email DB preflight skipped occupied candidate user=%s host=%s email=%s",
                        user_id,
                        standard_host_name,
                        candidate_email,
                    )
                    continue
                candidate_emails.append(candidate_email)

            available_emails = candidate_emails
            if candidate_emails:
                try:
                    available_emails, conflicts_by_email = await asyncio.to_thread(
                        xui_api.filter_available_subscription_base_emails_on_host,
                        standard_host_name,
                        candidate_emails,
                        include_white_inbounds=True,
                        require_white_inbound=True,
                    )
                    if conflicts_by_email:
                        logger.warning(
                            "Standard trial email panel preflight skipped occupied candidates user=%s host=%s conflicts=%s",
                            user_id,
                            standard_host_name,
                            ",".join(list(conflicts_by_email)[:5]),
                        )
                except Exception:
                    logger.exception(
                        "Standard trial email panel preflight failed user=%s host=%s; falling back to DB-only candidates",
                        user_id,
                        standard_host_name,
                    )
                    available_emails = candidate_emails

            result = None
            attempted_emails: list[str] = []
            for candidate_email in (available_emails or candidate_emails)[:5]:
                attempted_emails.append(candidate_email)
                result = await xui_api.create_or_update_key_on_host(
                    host_name=standard_host_name,
                    email=candidate_email,
                    days_to_add=trial_days,
                    months=1,
                    white_traffic_limit_bytes=xui_api.STANDARD_WHITELIST_TRAFFIC_BYTES,
                    target_expiry_ms=target_expiry_ms,
                    include_white_inbounds=True,
                    require_white_inbound=True,
                    panel_call_timeout_seconds=PURCHASE_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
                    max_attempts=PURCHASE_ISSUE_MAX_ATTEMPTS,
                )
                if result:
                    break

            if not result:
                logger.error(
                    "Standard trial issuance failed user=%s host=%s attempted_emails=%s",
                    user_id,
                    standard_host_name,
                    ",".join(attempted_emails),
                )
                send_critical_alert_nonblocking(
                    BotError.TRIAL_ISSUE_FAILED,
                    details=(
                        f"source={BOT_SOURCE} "
                        f"flow=trial_standard "
                        f"user_id={user_id} "
                        f"host={standard_host_name} "
                        f"attempted_emails={','.join(attempted_emails) or 'none'} "
                        "reason=trial_issue_failed"
                    ),
                    respect_cooldown=False,
                )
                await _replace_message(
                    callback.message,
                    text=("❌ Сервер временно недоступен.\n\nПробный доступ «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)» не списан. Нажмите «Попробовать ещё раз»."),
                    reply_markup=keyboards.create_trial_retry_keyboard("get_trial_standard"),
                )
                return

            issued_key_value = _pick_issued_key_value(
                connection_string=result.get("connection_string"),
                subscription_link=result.get("subscription_link"),
                sub_id=result.get("sub_id"),
                subscription_tier=xui_api.SUBSCRIPTION_TIER_STANDARD,
            )
            if not issued_key_value:
                logger.error(
                    "Standard trial panel returned empty access value user=%s host=%s email=%s",
                    user_id,
                    standard_host_name,
                    result.get("email"),
                )
                send_critical_alert_nonblocking(
                    BotError.TRIAL_ISSUE_FAILED,
                    details=(
                        f"source={BOT_SOURCE} "
                        "flow=trial_standard "
                        f"user_id={user_id} "
                        f"host={standard_host_name} "
                        f"key_email={result.get('email') or 'unknown'} "
                        "reason=empty_connection_string"
                    ),
                    respect_cooldown=False,
                )
                await _replace_message(
                    callback.message,
                    text=("❌ Панель вернула пустую ссылку.\n\nПробный доступ «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)» не списан. Нажмите «Попробовать ещё раз»."),
                    reply_markup=keyboards.create_trial_retry_keyboard("get_trial_standard"),
                )
                return
            trial_connection_string, trial_subscription_link = _split_key_access_value(issued_key_value)
            new_key_id = add_new_key(
                user_id=user_id,
                host_name=standard_host_name,
                xui_client_uuid=result["client_uuid"],
                key_email=result["email"],
                expiry_timestamp_ms=result["expiry_timestamp_ms"],
                issued_key_value=issued_key_value,
                panel_sub_id=result.get("sub_id"),
                subscription_payload=xui_api.subscription_payload_from_result(
                    result,
                    subscription_tier=xui_api.SUBSCRIPTION_TIER_STANDARD,
                ),
                subscription_tier=xui_api.SUBSCRIPTION_TIER_STANDARD,
            )
            if not new_key_id:
                logger.error(
                    "Standard trial panel key created but DB persistence failed user=%s host=%s email=%s",
                    user_id,
                    standard_host_name,
                    result.get("email"),
                )
                send_critical_alert_nonblocking(
                    BotError.TRIAL_ISSUE_FAILED,
                    details=(
                        f"source={BOT_SOURCE} "
                        "flow=trial_standard "
                        f"user_id={user_id} "
                        f"host={standard_host_name} "
                        f"key_email={result.get('email') or 'unknown'} "
                        "reason=trial_db_persist_failed"
                    ),
                    respect_cooldown=False,
                )
                await callback.message.edit_text(
                    "❌ Не удалось сохранить пробный доступ «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)».\n\n"
                    "Пробный доступ «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)» не списан. Нажмите «Попробовать ещё раз».",
                    reply_markup=keyboards.create_trial_retry_keyboard("get_trial_standard"),
                )
                return

            set_trial_used(user_id)

            final_text = get_trial_success_text(
                expiry_date=_naive_utc_from_timestamp_ms(result["expiry_timestamp_ms"]),
                connection_string=trial_connection_string,
                subscription_link=trial_subscription_link,
                host_name=standard_host_name,
                key_email=result.get("email"),
                whitelist_limit_gb=5,
            )
            await callback.message.edit_text(
                text=final_text,
                reply_markup=keyboards.create_key_info_keyboard(
                    new_key_id,
                    show_refresh=False,
                    show_qr=not bool(trial_subscription_link),
                ),
            )
            await send_effect_message(
                callback.message.bot,
                chat_id=user_id,
                text="🎁 Пробный доступ «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)» активирован.",
                effect_setting_key=TRIAL_SUCCESS_MESSAGE_EFFECT_ID_SETTING,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Standard trial issuance timed out for user=%s timeout=%ss",
                user_id,
                TRIAL_SUPER_ISSUE_TIMEOUT_SECONDS,
            )
            send_critical_alert_nonblocking(
                BotError.TRIAL_ISSUE_FAILED,
                details=(
                    f"source={BOT_SOURCE} "
                    f"flow=trial_standard "
                    f"user_id={user_id} "
                    f"host={standard_host_name} "
                    "reason=trial_issue_failed"
                ),
                respect_cooldown=False,
            )
            await _replace_message(
                callback.message,
                text=("❌ Сервер временно недоступен.\n\nПробный доступ «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)» не списан. Нажмите «Попробовать ещё раз»."),
                reply_markup=keyboards.create_trial_retry_keyboard("get_trial_standard"),
            )
        except Exception as e:
            logger.error("Error creating standard trial key for user %s: %s", user_id, e, exc_info=True)
            send_critical_alert_nonblocking(
                BotError.TRIAL_ISSUE_FAILED,
                details=(
                    f"source={BOT_SOURCE} "
                    "flow=trial_standard "
                    f"user_id={user_id} "
                    f"host={standard_host_name} "
                    "reason=trial_issue_failed"
                ),
                respect_cooldown=False,
            )
            await _replace_message(
                callback.message,
                text="❌ Произошла ошибка при создании пробного доступа «Стандартный ключ (Wi-Fi + WHITELIST 5 ГБ)».",
                reply_markup=keyboards.create_support_error_keyboard(),
            )
        finally:
            _release_trial_standard_issue_lock(user_id, lock)

    @user_router.callback_query(F.data == "get_trial_super")
    @registration_required
    async def trial_super_handler(callback: types.CallbackQuery):
        user_id = callback.from_user.id
        user_db_data = get_user(user_id) or {}

        if user_db_data.get("trial_super_used"):
            await _safe_callback_answer(callback, "Пробный доступ «Суперключ (обход белых списков)» уже использован.", show_alert=True)
            return

        super_host_name = _get_super_key_host_name()
        if not super_host_name or not await asyncio.to_thread(is_customer_host_visible, super_host_name):
            await _safe_callback_answer(callback, "Пробный SUPER временно недоступен.", show_alert=True)
            return
        super_host = _get_super_key_host()
        if not super_host_name or not super_host:
            await _safe_callback_answer(callback, "Пробный доступ «Суперключ (обход белых списков)» временно недоступен.", show_alert=True)
            return

        # ---- host availability pre-check (same as purchase flow) ----
        precheck = await _run_purchase_precheck_async(super_host_name, is_super_key_flow=True)
        if not precheck.allowed:
            await _safe_callback_answer(callback)
            await _replace_message(
                callback.message,
                text=f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        # Free super trial is hard-capped to 2 days to prevent free month issuance.
        trial_days = _read_trial_days("trial_duration_days_super", default=2, max_days=2)
        await _safe_callback_answer(callback)
        await callback.message.edit_text(f"⏳ Создаю пробный доступ «Суперключ (обход белых списков)» на {trial_days} дн...")

        try:
            # Double-check host health right before the XUI call.
            # Precheck might have passed from stale cache; this is instant (in-memory).
            reachable, unavail_msg = _is_host_reachable(super_host_name)
            if not reachable:
                await _replace_message(
                    callback.message,
                    text=f"🔧 {unavail_msg}",
                    reply_markup=keyboards.create_back_to_menu_keyboard(),
                )
                return

            existing_trial_key = _existing_active_trial_super_key(user_id, super_host_name)
            if existing_trial_key:
                key_number = _trial_super_key_number(
                    existing_trial_key.get("key_email"),
                    user_id,
                    fallback=get_next_key_number(user_id),
                )
                result = _trial_result_from_existing_key(existing_trial_key)
                set_trial_super_used(user_id)
                _consume_trial_super_timeout_count(user_id)
                final_text = get_trial_success_text(
                    expiry_date=_naive_utc_from_timestamp_ms(result["expiry_timestamp_ms"]),
                    connection_string=result.get("connection_string"),
                    subscription_link=result.get("subscription_link"),
                    host_name=super_host_name,
                    key_email=result.get("email"),
                    whitelist_limit_gb=2,
                )
                await callback.message.edit_text(
                    text=final_text,
                    reply_markup=keyboards.create_key_info_keyboard(
                        int(existing_trial_key["key_id"]),
                        show_refresh=False,
                        show_qr=not bool(result.get("subscription_link")),
                    ),
                )
                return

            key_number = get_next_key_number(user_id)
            target_expiry_ms = _trial_super_target_expiry_ms(trial_days)
            timeout_issue_count = 0
            result, timed_out, in_progress = await _issue_trial_super_with_quick_retries(
                host_name=super_host_name,
                user_id=user_id,
                key_number=key_number,
                trial_days=trial_days,
                target_expiry_ms=target_expiry_ms,
            )
            if in_progress:
                await _replace_message(
                    callback.message,
                    text=("⏳ Пробный SUPER уже создаётся.\n\nПодождите несколько секунд, я проверяю результат."),
                )
                return
            if not result:
                timeout_issue_count = _increment_trial_super_timeout_count(user_id)
                send_critical_alert_nonblocking(
                    BotError.TRIAL_ISSUE_PENDING_VERIFICATION,
                    details=(
                        f"source={BOT_SOURCE} "
                        f"flow=trial_super "
                        f"user_id={user_id} "
                        f"host={super_host_name} "
                        f"timed_out={str(bool(timed_out)).lower()} "
                        f"verification_attempts={TRIAL_SUPER_VERIFY_ATTEMPTS} "
                        "reason=trial_issue_pending_verification"
                    ),
                    respect_cooldown=True,
                )
                await _replace_message(
                    callback.message,
                    text=(
                        "⏳ Сервер отвечает медленно.\n\n"
                        "Проверяю, создался ли пробный SUPER. Не нажимайте кнопку повторно."
                    ),
                )
                result = await _verify_trial_super_after_uncertain_issue(
                    host_name=super_host_name,
                    user_id=user_id,
                    key_number=key_number,
                    target_expiry_ms=target_expiry_ms,
                )

            if not result:
                if timeout_issue_count <= 0:
                    timeout_issue_count = _increment_trial_super_timeout_count(user_id)
                send_critical_alert_nonblocking(
                    BotError.TRIAL_ISSUE_FAILED,
                    details=(
                        f"source={BOT_SOURCE} "
                        f"flow=trial_super "
                        f"user_id={user_id} "
                        f"host={super_host_name} "
                        f"timed_out={str(bool(timed_out)).lower()} "
                        f"verification_attempts={TRIAL_SUPER_VERIFY_ATTEMPTS} "
                        "reason=trial_issue_failed"
                    ),
                    respect_cooldown=False,
                )
                if timeout_issue_count > 1:
                    send_critical_alert_nonblocking(
                        BotError.TRIAL_RETRY_PROMPT_SHOWN_AGAIN,
                        details=(
                            f"source={BOT_SOURCE} "
                            "flow=trial_super "
                            f"user_id={user_id} "
                            f"host={super_host_name} "
                            "reason=trial_retry_prompt_shown_again"
                        ),
                        respect_cooldown=False,
                    )
                await _replace_message(
                    callback.message,
                    text=("❌ Сервер временно недоступен.\n\nПробный SUPER не списан. Нажмите «Попробовать ещё раз»."),
                    reply_markup=keyboards.create_trial_retry_keyboard("get_trial_super"),
                )
                return

            issued_key_value, trial_connection_string, trial_subscription_link = (
                _trial_super_access_values_from_panel_result(result)
            )
            new_key_id = add_new_key(
                user_id=user_id,
                host_name=super_host_name,
                xui_client_uuid=result["client_uuid"],
                key_email=result["email"],
                expiry_timestamp_ms=result["expiry_timestamp_ms"],
                issued_key_value=issued_key_value,
                panel_sub_id=result.get("sub_id"),
                subscription_payload=xui_api.subscription_payload_from_result(
                    result,
                    subscription_tier=xui_api.SUBSCRIPTION_TIER_SUPER,
                ),
                subscription_tier=xui_api.SUBSCRIPTION_TIER_SUPER,
            )
            if not new_key_id:
                logger.error(
                    "Super trial panel key created but DB persistence failed user=%s host=%s email=%s",
                    user_id,
                    super_host_name,
                    result.get("email"),
                )
                send_critical_alert_nonblocking(
                    BotError.TRIAL_ISSUE_FAILED,
                    details=(
                        f"source={BOT_SOURCE} "
                        "flow=trial_super "
                        f"user_id={user_id} "
                        f"host={super_host_name} "
                        f"key_email={result.get('email') or 'unknown'} "
                        "reason=trial_db_persist_failed"
                    ),
                    respect_cooldown=False,
                )
                await _replace_message(
                    callback.message,
                    text=(
                        "❌ \u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c "
                        "\u0441\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c "
                        "\u043f\u0440\u043e\u0431\u043d\u044b\u0439 SUPER.\n\n"
                        "\u041f\u0440\u043e\u0431\u043d\u044b\u0439 SUPER \u043d\u0435 "
                        "\u0441\u043f\u0438\u0441\u0430\u043d. "
                        "\u041d\u0430\u0436\u043c\u0438\u0442\u0435 "
                        "\u00ab\u041f\u043e\u043f\u0440\u043e\u0431\u043e\u0432\u0430\u0442\u044c "
                        "\u0435\u0449\u0451 \u0440\u0430\u0437\u00bb."
                    ),
                    reply_markup=keyboards.create_trial_retry_keyboard("get_trial_super"),
                )
                return
            set_trial_super_used(user_id)
            timeout_issue_count = _consume_trial_super_timeout_count(user_id)
            if timeout_issue_count > 0:
                send_critical_alert_nonblocking(
                    BotError.TRIAL_ISSUE_RECOVERED,
                    details=(
                        f"source={BOT_SOURCE} "
                        f"flow=trial_super "
                        f"user_id={user_id} "
                        f"host={super_host_name} "
                        "reason=trial_issue_recovered "
                        f"key_id={new_key_id if new_key_id is not None else 'unknown'} "
                        f"key_email={result.get('email') or 'unknown'}"
                    ),
                    respect_cooldown=False,
                )

            final_text = get_trial_success_text(
                expiry_date=_naive_utc_from_timestamp_ms(result["expiry_timestamp_ms"]),
                connection_string=trial_connection_string,
                subscription_link=trial_subscription_link,
                host_name=super_host_name,
                key_email=result.get("email"),
                whitelist_limit_gb=2,
            )
            await callback.message.edit_text(
                text=final_text,
                reply_markup=keyboards.create_key_info_keyboard(
                    new_key_id,
                    show_refresh=False,
                    show_qr=not bool(trial_subscription_link),
                ),
            )
            await send_effect_message(
                callback.message.bot,
                chat_id=user_id,
                text="🎁 Пробный доступ «Суперключ (обход белых списков)» активирован.",
                effect_setting_key=TRIAL_SUCCESS_MESSAGE_EFFECT_ID_SETTING,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Super trial issuance timed out for user=%s host=%s timeout=%ss",
                user_id,
                super_host_name,
                TRIAL_SUPER_ISSUE_TIMEOUT_SECONDS,
            )
            timeout_issue_count = _increment_trial_super_timeout_count(user_id)
            send_critical_alert_nonblocking(
                BotError.TRIAL_ISSUE_FAILED,
                details=(
                    f"source={BOT_SOURCE} "
                    f"flow=trial_super "
                    f"user_id={user_id} "
                    f"host={super_host_name} "
                    "reason=trial_issue_failed"
                ),
                respect_cooldown=False,
            )
            if timeout_issue_count > 1:
                send_critical_alert_nonblocking(
                    BotError.TRIAL_RETRY_PROMPT_SHOWN_AGAIN,
                    details=(
                        f"source={BOT_SOURCE} "
                        "flow=trial_super "
                        f"user_id={user_id} "
                        f"host={super_host_name} "
                        "reason=trial_retry_prompt_shown_again"
                    ),
                    respect_cooldown=False,
                )
            await _replace_message(
                callback.message,
                text=("❌ Сервер временно недоступен.\n\nПробный SUPER не списан. Нажмите «Попробовать ещё раз»."),
                reply_markup=keyboards.create_trial_retry_keyboard("get_trial_super"),
            )
        except Exception as e:
            logger.error("Error creating super trial key for user %s: %s", user_id, e, exc_info=True)
            send_critical_alert_nonblocking(
                BotError.TRIAL_ISSUE_FAILED,
                details=(
                    f"source={BOT_SOURCE} "
                    "flow=trial_super "
                    f"user_id={user_id} "
                    f"host={super_host_name} "
                    "reason=trial_issue_failed"
                ),
                respect_cooldown=False,
            )
            await _replace_message(
                callback.message,
                text=("❌ Не удалось выдать пробный SUPER.\n\nПробный SUPER не списан. Нажмите «Попробовать ещё раз»."),
                reply_markup=keyboards.create_trial_retry_keyboard("get_trial_super"),
            )

    async def _show_key_details(
        callback: types.CallbackQuery,
        key_id_to_show: int,
        *,
        loading_text: str = "Загружаю информацию о ключе...",
    ) -> None:
        user_id = callback.from_user.id
        key_data = await asyncio.to_thread(
            _get_customer_visible_key_for_user,
            key_id_to_show,
            user_id,
        )

        if not key_data:
            await callback.message.edit_text(
                "❌ Ошибка: ключ не найден.",
                reply_markup=keyboards.create_back_to_keys_keyboard(),
            )
            return

        try:
            # Determine if this is a Blitz key or a 3X-UI key.
            is_blitz = blitz_api.is_blitz_key(key_data)
            connection_string, subscription_link = _stored_key_access_values(key_data)

            if is_blitz:
                # For Blitz keys, xui_client_uuid stores the Blitz username.
                if not connection_string and not subscription_link:
                    await callback.message.edit_text(loading_text)
                    blitz_username = key_data["xui_client_uuid"]
                    try:
                        connection_string = await asyncio.wait_for(
                            blitz_api.get_connection_uri(blitz_username),
                            timeout=KEY_DETAILS_TIMEOUT_SECONDS,
                        )
                    except TimeoutError:
                        logger.warning("Blitz get_connection_uri timed out for key %s", key_id_to_show)
                        connection_string = None
                    if not connection_string:
                        await callback.message.edit_text(
                            "❌ Ошибка на сервере Blitz. Не удалось получить данные ключа.",
                            reply_markup=keyboards.create_back_to_keys_keyboard(),
                        )
                        return
            else:
                if not connection_string and not subscription_link:
                    await callback.message.edit_text(loading_text)
                    # Fast health check — no I/O, prevents 25s hang on downed host.
                    reachable, unavail_msg = _is_host_reachable(key_data.get("host_name"))
                    if not reachable:
                        await callback.message.edit_text(
                            f"🔧 {unavail_msg}",
                            reply_markup=keyboards.create_back_to_menu_keyboard(),
                        )
                        return
                    try:
                        details = await asyncio.wait_for(
                            xui_api.get_key_details_from_host(key_data),
                            timeout=KEY_DETAILS_TIMEOUT_SECONDS,
                        )
                    except TimeoutError:
                        logger.warning(
                            "get_key_details_from_host timed out for key %s host=%s",
                            key_id_to_show,
                            key_data.get("host_name"),
                        )
                        await callback.message.edit_text(
                            "🔧 Сервер временно не отвечает. Попробуйте позже.",
                            reply_markup=keyboards.create_back_to_menu_keyboard(),
                        )
                        return
                    if not details:
                        await callback.message.edit_text(
                            "❌ Ошибка на сервере. Не удалось получить данные ключа.",
                            reply_markup=keyboards.create_back_to_keys_keyboard(),
                        )
                        return
                    connection_string = details.get("connection_string")
                    subscription_link = details.get("subscription_link")
                    if not connection_string and not subscription_link:
                        await callback.message.edit_text(
                            "❌ Ошибка на сервере. Не удалось получить данные ключа.",
                            reply_markup=keyboards.create_back_to_keys_keyboard(),
                        )
                        return

            expiry_date = datetime.fromisoformat(key_data["expiry_date"])
            created_date = datetime.fromisoformat(key_data["created_date"])

            all_user_keys = await asyncio.to_thread(_get_customer_visible_user_keys, user_id)
            key_number = next((i + 1 for i, key in enumerate(all_user_keys) if key["key_id"] == key_id_to_show), 0)

            final_text = get_key_info_text(
                key_number,
                expiry_date,
                created_date,
                connection_string=connection_string,
                subscription_link=subscription_link,
                gift_days=int(key_data.get("gift_days") or 0),
            )

            is_active = expiry_date > datetime.now()

            # Migration is handled by "Обновить ключ", keep extra migration button hidden.
            show_migration = False

            # Paid extension is available for active managed subscriptions.
            # Standard/super are distinguished by subscription_tier because both
            # products can live on the same MainPanel host.
            show_refresh = is_active and _is_extendable_managed_key(key_data, expiry_date=expiry_date)
            show_qr = bool(connection_string and "\n" not in connection_string and not subscription_link)

            await callback.message.edit_text(
                text=final_text,
                reply_markup=keyboards.create_key_info_keyboard(
                    key_id_to_show,
                    show_blitz_migration=show_migration,
                    show_refresh=show_refresh,
                    show_qr=show_qr,
                ),
            )
        except Exception as e:
            logger.error(f"Error showing key {key_id_to_show}: {e}")
            await callback.message.edit_text("❌ Произошла ошибка при получении данных ключа.")

    @user_router.callback_query(F.data.startswith("show_key_"))
    @registration_required
    async def show_key_handler(callback: types.CallbackQuery):
        await _safe_callback_answer(callback)
        key_id_to_show = int(callback.data.split("_")[2])
        await _show_key_details(callback, key_id_to_show)

    @user_router.callback_query(F.data.startswith("migrate_blitz_"))
    @registration_required
    async def migrate_to_blitz_handler(callback: types.CallbackQuery):
        """Migrate an existing 3X-UI key to Blitz (Hysteria 2)."""
        key_id = int(callback.data.split("_")[2])
        user_id = callback.from_user.id
        key_data = await asyncio.to_thread(_get_customer_visible_key_for_user, key_id, user_id)

        if not key_data:
            await _safe_callback_answer(callback, "❌ Ключ не найден.", show_alert=True)
            return

        if blitz_api.is_blitz_key(key_data):
            await _safe_callback_answer(callback, "Этот ключ уже на Hysteria 2.", show_alert=True)
            return

        expiry_date = datetime.fromisoformat(key_data["expiry_date"])
        remaining_days = _remaining_days_until(expiry_date)

        if remaining_days <= 0:
            await _safe_callback_answer(callback, "❌ Ключ истёк. Миграция невозможна.", show_alert=True)
            return

        # Show confirmation
        await _safe_callback_answer(callback)
        confirm_text = (
            f"⚡ <b>Переход на Hysteria 2 (Blitz)</b>\n\n"
            f"Ваш текущий VLESS-ключ будет заменён на новый высокоскоростной ключ Hysteria 2.\n\n"
            f"📅 <b>Оставшееся время:</b> {remaining_days} дн.\n"
            f"♾ <b>Трафик:</b> безлимитный\n\n"
            f"Старый ключ перестанет работать. Продолжить?"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="✅ Да, перейти", callback_data=f"confirm_migrate_blitz_{key_id}")
        builder.button(text="⬅️ Отмена", callback_data=f"show_key_{key_id}")
        builder.adjust(1)
        await callback.message.edit_text(confirm_text, reply_markup=builder.as_markup())

    @user_router.callback_query(F.data.startswith("confirm_migrate_blitz_"))
    @registration_required
    async def confirm_migrate_to_blitz_handler(callback: types.CallbackQuery):
        """Execute the migration from 3X-UI to Blitz after user confirmation."""
        key_id = int(callback.data.split("_")[3])
        user_id = callback.from_user.id
        key_data = await asyncio.to_thread(_get_customer_visible_key_for_user, key_id, user_id)

        if not key_data:
            await _safe_callback_answer(callback, "❌ Ключ не найден.", show_alert=True)
            return

        expiry_date = datetime.fromisoformat(key_data["expiry_date"])
        remaining_days = _remaining_days_until(expiry_date)

        if remaining_days <= 0:
            await _safe_callback_answer(callback, "❌ Ключ истёк.", show_alert=True)
            return

        await _safe_callback_answer(callback)
        await callback.message.edit_text("⏳ Создаю новый ключ Hysteria 2...")

        try:
            # Generate a unique Blitz username
            blitz_username = _generate_blitz_username()

            result = await blitz_api.create_user(username=blitz_username, expiry_days=remaining_days, unlimited=True)

            if not result:
                await callback.message.edit_text(
                    "❌ Не удалось создать ключ на сервере Blitz.\n"
                    "Пожалуйста, попробуйте позже или обратитесь в поддержку.",
                    reply_markup=keyboards.create_support_error_keyboard(),
                )
                return

            # Get the connection URI
            connection_uri = await blitz_api.get_connection_uri(blitz_username)
            if not connection_uri:
                await callback.message.edit_text(
                    "❌ Ключ создан, но не удалось получить ссылку.\nПожалуйста, обратитесь в поддержку.",
                    reply_markup=keyboards.create_support_error_keyboard(),
                )
                return

            # Update the local DB record to point to Blitz
            new_expiry_ms = int((datetime.now() + timedelta(days=remaining_days)).timestamp() * 1000)
            update_key_host_and_uuid(
                key_id=key_id,
                new_host_name=blitz_api.BLITZ_HOST_NAME,
                new_uuid=blitz_username,
                new_expiry_ms=new_expiry_ms,
                issued_key_value=connection_uri,
            )
            set_blitz_refresh_used(user_id, True)

            # Note: We intentionally do NOT delete the old X-UI key.
            # It will expire naturally. The DB record is updated to point to Blitz.
            logger.info(
                "Migration complete for key %s. Old X-UI key on '%s' left intact.",
                key_id,
                key_data.get("host_name"),
            )

            # Show success
            all_user_keys = await asyncio.to_thread(_get_customer_visible_user_keys, user_id)
            key_number = next((i + 1 for i, k in enumerate(all_user_keys) if k["key_id"] == key_id), 0)
            new_expiry = datetime.now() + timedelta(days=remaining_days)

            success_text = (
                f"⚡ <b>Ключ #{key_number} успешно переведён на Hysteria 2!</b>\n\n"
                f"⏳ <b>Действителен до:</b> {new_expiry.strftime('%d.%m.%Y в %H:%M')}\n"
                f"♾ <b>Трафик:</b> безлимитный\n\n"
                f"<code>{connection_uri}</code>"
            )
            await callback.message.edit_text(
                text=success_text,
                reply_markup=keyboards.create_key_info_keyboard(
                    key_id,
                    show_blitz_migration=False,
                    show_refresh=False,
                ),
            )

        except Exception as e:
            logger.error(f"Error migrating key {key_id} to Blitz for user {user_id}: {e}", exc_info=True)
            await callback.message.edit_text(
                "❌ Произошла ошибка при миграции. Обратитесь в поддержку.",
                reply_markup=keyboards.create_support_error_keyboard(),
            )

    @user_router.callback_query(F.data.startswith("show_qr_"))
    @registration_required
    async def show_qr_handler(callback: types.CallbackQuery):
        key_id = int(callback.data.split("_")[2])
        key_data = await asyncio.to_thread(
            _get_customer_visible_key_for_user,
            key_id,
            callback.from_user.id,
        )
        if not key_data:
            await _safe_callback_answer(callback, "Ошибка: ключ не найден.", show_alert=True)
            return

        try:
            connection_string, subscription_link = _stored_key_access_values(key_data)
            if subscription_link or (connection_string and "\n" in connection_string):
                await _safe_callback_answer(
                    callback,
                    "Для этого ключа используется ссылка подписки. QR-код не требуется.",
                    show_alert=True,
                )
                return

            await _safe_callback_answer(callback, "Генерирую QR-код...")

            if blitz_api.is_blitz_key(key_data):
                try:
                    if not connection_string:
                        connection_string = await asyncio.wait_for(
                            blitz_api.get_connection_uri(key_data["xui_client_uuid"]),
                            timeout=KEY_DETAILS_TIMEOUT_SECONDS,
                        )
                except asyncio.TimeoutError:
                    connection_string = None
            else:
                if not connection_string:
                    # Fast health check — prevents 25s hang on downed host.
                    reachable, unavail_msg = _is_host_reachable(key_data.get("host_name"))
                    if not reachable:
                        await _safe_callback_answer(callback, f"🔧 {unavail_msg}", show_alert=True)
                        return
                    try:
                        details = await asyncio.wait_for(
                            xui_api.get_key_details_from_host(key_data),
                            timeout=KEY_DETAILS_TIMEOUT_SECONDS,
                        )
                    except TimeoutError:
                        logger.warning("get_key_details_from_host timed out in QR handler for key %s", key_id)
                        await _safe_callback_answer(callback, "🔧 Сервер временно не отвечает.", show_alert=True)
                        return
                    if details and details.get("subscription_link"):
                        await _safe_callback_answer(
                            callback,
                            "Для этого ключа используется ссылка подписки. QR-код не требуется.",
                            show_alert=True,
                        )
                        return
                    connection_string = details["connection_string"] if details else None

            if not connection_string:
                await _safe_callback_answer(callback, "Ошибка: Не удалось сгенерировать QR-код.", show_alert=True)
                return

            qr_img = qrcode.make(connection_string)
            bio = BytesIO()
            qr_img.save(bio, "PNG")
            bio.seek(0)
            qr_code_file = BufferedInputFile(bio.read(), filename="vpn_qr.png")
            await callback.message.answer_photo(photo=qr_code_file)
        except Exception as e:
            logger.error(f"Error showing QR for key {key_id}: {e}")

    @user_router.callback_query(F.data.startswith("howto_vless_"))
    @registration_required
    async def show_instruction_for_key_handler(callback: types.CallbackQuery):
        await _safe_callback_answer(callback)
        key_id = int(callback.data.split("_")[2])
        u = _INSTRUCTION_URLS
        await callback.message.edit_text(
            _INSTRUCTION_MESSAGE,
            reply_markup=keyboards.create_howto_vless_keyboard_key(
                happ_android_url=u["happ_android"],
                happ_ios_url=u["happ_ios"],
                happ_windows_url=u["happ_windows"],
                happ_macos_url=u["happ_macos"],
                karing_android_url=u["karing_android"],
                karing_ios_url=u["karing_ios"],
                karing_windows_url=u["karing_windows"],
                karing_macos_url=u["karing_macos"],
                hiddify_android_url=u["hiddify_android"],
                hiddify_ios_url=u["hiddify_ios"],
                hiddify_windows_url=u["hiddify_windows"],
                hiddify_macos_url=u["hiddify_macos"],
                v2raytun_android_url=u["v2raytun_android"],
                v2raytun_ios_url=u["v2raytun_ios"],
                v2raytun_windows_url=u["v2raytun_windows"],
                incy_android_url=u["incy_android"],
                incy_ios_url=u["incy_ios"],
                incy_windows_url=u["incy_windows"],
                incy_macos_url=u["incy_macos"],
                key_id=key_id,
            ),
            disable_web_page_preview=True,
        )

    @user_router.callback_query(F.data.startswith("howto_vless"))
    @registration_required
    async def show_instruction_menu_handler(callback: types.CallbackQuery):
        await _safe_callback_answer(callback)
        u = _INSTRUCTION_URLS
        await callback.message.edit_text(
            _INSTRUCTION_MESSAGE,
            reply_markup=keyboards.create_howto_vless_keyboard(
                happ_android_url=u["happ_android"],
                happ_ios_url=u["happ_ios"],
                happ_windows_url=u["happ_windows"],
                happ_macos_url=u["happ_macos"],
                karing_android_url=u["karing_android"],
                karing_ios_url=u["karing_ios"],
                karing_windows_url=u["karing_windows"],
                karing_macos_url=u["karing_macos"],
                hiddify_android_url=u["hiddify_android"],
                hiddify_ios_url=u["hiddify_ios"],
                hiddify_windows_url=u["hiddify_windows"],
                hiddify_macos_url=u["hiddify_macos"],
                v2raytun_android_url=u["v2raytun_android"],
                v2raytun_ios_url=u["v2raytun_ios"],
                v2raytun_windows_url=u["v2raytun_windows"],
                incy_android_url=u["incy_android"],
                incy_ios_url=u["incy_ios"],
                incy_windows_url=u["incy_windows"],
                incy_macos_url=u["incy_macos"],
            ),
            disable_web_page_preview=True,
        )

    async def _show_new_key_plans(
        callback: types.CallbackQuery,
        *,
        state: FSMContext | None = None,
        purchase_source: str | None = None,
        answer_callback: bool = True,
        action: str = "new",
        key_id: int = 0,
        back_callback: str | None = None,
    ):
        """
        Handler for new key purchase.
        Standard keys are issued on configured primary standard host.
        """
        if answer_callback:
            await _safe_callback_answer(callback)

        requested_action = str(action or "new").strip().lower()
        if requested_action not in {"new", "extend"}:
            requested_action = "new"
        requested_key_id = int(key_id or 0)

        host_name = _get_standard_key_host_name()
        if not await asyncio.to_thread(is_customer_host_visible, host_name):
            await _replace_message(
                callback.message,
                text="🔧 Тариф «Стандарт» временно недоступен. Пожалуйста, обратитесь в поддержку.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return
        logger.info(
            "Routing new standard key purchase to host '%s' for user %s",
            host_name,
            callback.from_user.id,
        )

        precheck = await _run_purchase_precheck_async(host_name, is_super_key_flow=False)
        if not precheck.allowed:
            user_message = str(precheck.user_message or "").strip()
            if precheck.reason == "host_not_found":
                user_message = f"Тариф «Стандарт» временно недоступен: хост «{host_name}» не настроен."
            await _replace_message(
                callback.message,
                text=f"🔧 {user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        plans = get_plans_for_host(host_name)
        if not plans:
            await _replace_message(
                callback.message,
                text=(f"❌ Для хоста «{host_name}» пока не настроены тарифы.\nПожалуйста, обратитесь в поддержку."),
                reply_markup=keyboards.create_support_error_keyboard(),
            )
            logger.error("No plans available for standard host '%s'", host_name)
            return

        if state is not None:
            await state.update_data(purchase_source=purchase_source)

        resolved_back_callback = back_callback or _resolve_purchase_back_callback_by_source(
            purchase_source,
            callback.from_user.id,
        )
        prompt_text = (
            "Выберите подходящий тариф:\n\n" if requested_action == "new" else "Выберите тариф для продления:\n\n"
        )

        # Don't show technical server name to user for security
        await _replace_message(
            callback.message,
            text=(prompt_text + STANDARD_KEY_NETWORK_WARNING),
            parse_mode="HTML",
            reply_markup=keyboards.create_plans_keyboard(
                plans=plans,
                action=requested_action,
                host_name=host_name,
                key_id=max(0, requested_key_id),
                back_callback=resolved_back_callback,
            ),
        )

    @user_router.callback_query(F.data.in_({"buy_new_key", "buy_new_key_main", "buy_new_key_keys"}))
    @registration_required
    async def buy_new_key_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        purchase_availability = await _resolve_purchase_availability()
        if not purchase_availability.get("standard_allowed"):
            await _safe_callback_answer(callback)
            await _replace_message(
                callback.message,
                text=f"🔧 {purchase_availability.get('standard_message') or purchase_availability.get('common_message')}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return
        purchase_source = _resolve_purchase_source(
            callback.data,
            callback.message.text if callback.message else None,
        )
        await _show_new_key_plans(
            callback,
            state=state,
            purchase_source=purchase_source,
        )

    async def show_super_key_plans(
        message: types.Message,
        *,
        user_id: int,
        purchase_source: str | None = None,
    ):
        super_host_name = _get_super_key_host_name()
        if not super_host_name or not await asyncio.to_thread(is_customer_host_visible, super_host_name):
            await _replace_message(
                message,
                text="🔧 Суперключ (обход белых списков) временно недоступен. Пожалуйста, обратитесь в поддержку.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return
        precheck = await _run_purchase_precheck_async(
            super_host_name,
            is_super_key_flow=True,
        )
        if not precheck.allowed:
            await _replace_message(
                message,
                text=f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        super_host = _get_super_key_host()
        back_callback = _resolve_purchase_back_callback_by_source(purchase_source, user_id)

        if not super_host_name or not super_host:
            await _replace_message(
                message,
                text=("❌ Суперключ (обход белых списков) временно недоступен.\nПожалуйста, обратитесь в поддержку."),
                reply_markup=keyboards.create_support_error_keyboard(),
            )
            logger.warning(
                "Super key requested but configuration is invalid. super_key_host_name=%s",
                super_host_name,
            )
            return

        await _replace_message(
            message,
            text=_render_super_key_plans_text(),
            reply_markup=keyboards.create_superkey_plans_keyboard(
                list(SUPER_KEY_PLANS),
                back_callback=back_callback,
            ),
        )

    @user_router.callback_query(F.data.in_({"buy_super_key", "buy_super_key_main", "buy_super_key_keys"}))
    @registration_required
    async def buy_super_key_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        purchase_availability = await _resolve_purchase_availability()
        if not purchase_availability.get("super_allowed"):
            await _safe_callback_answer(callback)
            await _replace_message(
                callback.message,
                text=f"🔧 {purchase_availability.get('super_message') or purchase_availability.get('common_message')}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return
        await _safe_callback_answer(callback)
        purchase_source = _resolve_purchase_source(
            callback.data,
            callback.message.text if callback.message else None,
        )
        await state.update_data(
            purchase_source=purchase_source,
            action="new",
            key_id=0,
            is_super_key_flow=True,
            extend_host_name=None,
        )
        await show_super_key_plans(
            callback.message,
            user_id=callback.from_user.id,
            purchase_source=purchase_source,
        )

    @user_router.callback_query(F.data.startswith("superbuy_"))
    @registration_required
    async def super_plan_selection_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        await _safe_callback_answer(callback)
        current_data = await state.get_data()

        plan_code = callback.data.removeprefix("superbuy_")
        super_plan = SUPER_KEY_PLAN_BY_CODE.get(plan_code)
        if super_plan and not is_super_offer_available(super_plan):
            await state.clear()
            await _replace_message(
                callback.message,
                text=PROMO_EXPIRED_TEXT,
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return
        super_host_name = _get_super_key_host_name()
        selected_action = str(current_data.get("action") or "new").strip().lower()
        if selected_action not in {"new", "extend"}:
            selected_action = "new"
        try:
            selected_key_id = int(current_data.get("key_id") or 0)
        except (TypeError, ValueError):
            selected_key_id = 0
        if selected_action != "extend" or selected_key_id <= 0:
            selected_action = "new"
            selected_key_id = 0

        selected_key_data = None
        if selected_action == "extend":
            selected_key_data = await asyncio.to_thread(
                _get_customer_visible_key_for_user,
                selected_key_id,
                callback.from_user.id,
            )
            if not selected_key_data:
                await _replace_message(
                    callback.message,
                    text="❌ Ошибка: ключ для продления не найден.",
                    reply_markup=keyboards.create_back_to_menu_keyboard(),
                )
                return

        requested_host_name = (
            str(selected_key_data.get("host_name") or "").strip() if selected_key_data else ""
        )
        target_host_name = requested_host_name or str(super_host_name or "").strip()
        target_host = get_host(target_host_name) if target_host_name else None
        if not target_host and super_host_name:
            target_host_name = super_host_name
            target_host = _get_super_key_host()

        if target_host_name and not await asyncio.to_thread(is_customer_host_visible, target_host_name):
            target_host = None

        if not super_plan or not target_host_name or not target_host:
            await _replace_message(
                callback.message,
                text=(
                    "❌ Не удалось выбрать тариф «Суперключ (обход белых списков)».\nПожалуйста, попробуйте снова или обратитесь в поддержку."
                ),
                reply_markup=keyboards.create_support_error_keyboard(),
            )
            logger.warning(
                "Invalid super plan selection. plan_code=%s target_host=%s host_exists=%s",
                plan_code,
                target_host_name,
                bool(target_host),
            )
            return

        precheck = await _run_purchase_precheck_async(target_host_name, is_super_key_flow=True)
        if not precheck.allowed:
            await _replace_message(
                callback.message,
                text=f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        await state.update_data(
            action=selected_action,
            key_id=selected_key_id,
            plan_id=0,
            host_name=target_host_name,
            is_super_key_flow=True,
            super_plan_code=super_plan["code"],
            super_plan_name=super_plan["name"],
            super_plan_months=super_plan["months"],
            super_plan_price=super_plan["price"],
            purchase_source=current_data.get("purchase_source"),
        )

        await _replace_message(
            callback.message,
            text=(
                "📧 Пожалуйста, введите ваш email для отправки чека об оплате.\n\n"
                "Если вы не хотите указывать почту, нажмите кнопку ниже."
            ),
            reply_markup=keyboards.create_skip_email_keyboard(),
        )
        await state.set_state(PaymentProcess.waiting_for_email)

    @user_router.callback_query(F.data.startswith("promo:"))
    @registration_required
    async def promo_campaign_handler(callback: types.CallbackQuery, state: FSMContext):
        """Stateless campaign choices; key ownership and tier are checked on every click."""
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return
        await _safe_callback_answer(callback)
        parts = str(callback.data or "").split(":")
        if parts == ["promo", "offers"]:
            await state.clear()
            await _replace_message(callback.message, text="🎁 Выберите акцию:",
                                   reply_markup=keyboards.create_promo_campaign_keyboard())
            return
        if len(parts) < 2 or parts[1] not in {"super", "standard"}:
            return
        tier = parts[1]
        title = "Супер 2+1 — 1100 ₽" if tier == "super" else "Стандарт 6+1 — 500 ₽"
        if len(parts) == 2:
            await state.clear()
            await _replace_message(callback.message, text=f"🎁 {title}\n\nЧто хотите сделать?",
                                   reply_markup=keyboards.create_promo_action_keyboard(tier))
            return
        operation = parts[2]
        if operation == "keys" and len(parts) == 4:
            try:
                page = max(0, int(parts[3]))
            except ValueError:
                return
            await state.clear()
            all_keys = await asyncio.to_thread(_get_customer_visible_user_keys, callback.from_user.id)
            matching = [dict(key, promo_display_number=index) for index, key in enumerate(all_keys, 1)
                        if _key_subscription_tier(key) == tier and not key.get("is_archived")
                        and int(key.get("user_id") or 0) == callback.from_user.id]
            text = (f"🔄 {title}\n\nВыберите ключ для продления. Оставшийся оплаченный срок сохранится."
                    if matching else f"У вас пока нет ключей тарифа «{'Супер' if tier == 'super' else 'Стандарт'}» для продления.\nМожно купить новый ключ по акции.")
            await _replace_message(callback.message, text=text,
                                   reply_markup=keyboards.create_promo_keys_keyboard(matching, tier, page))
            return
        key_id = 0
        key = None
        if operation == "renew" and len(parts) == 4:
            try:
                key_id = int(parts[3])
            except ValueError:
                return
            key = await asyncio.to_thread(_get_customer_visible_key_for_user, key_id, callback.from_user.id)
            if (not key or key_id <= 0 or _key_subscription_tier(key) != tier
                    or key.get("is_archived") or int(key.get("user_id") or 0) != callback.from_user.id):
                await state.clear()
                await _replace_message(callback.message,
                                       text="Этот ключ недоступен для выбранной акции. Выберите другой ключ.",
                                       reply_markup=keyboards.create_promo_action_keyboard(tier))
                return
            action = "extend"
        elif operation == "new" and len(parts) == 3:
            action = "new"
        else:
            return
        availability = await _resolve_purchase_availability()
        if not availability.get(f"{tier}_allowed"):
            await state.clear()
            await _replace_message(callback.message,
                                   text=f"🔧 {availability.get(f'{tier}_message') or availability.get('common_message') or 'Покупка временно недоступна.'}",
                                   reply_markup=keyboards.create_back_to_menu_keyboard())
            return
        # No previous checkout state can select a different key or offer.
        await state.clear()
        await state.update_data(action=action, key_id=key_id, purchase_source=PURCHASE_SOURCE_MAIN,
                                is_super_key_flow=tier == "super",
                                extend_host_name=key.get("host_name") if key else None)
        if tier == "super":
            selected = callback.model_copy(update={"data": f"superbuy_{PROMO_SUPER_PLAN['code']}"})
            await super_plan_selection_handler(selected, state)
            return
        plans = await asyncio.to_thread(get_plans_for_host, _get_standard_key_host_name())
        offers = [plan for plan in plans if promotion_metadata(plan).get("promo_code") == "promo_standard_6plus1"]
        if len(offers) != 1:
            await state.clear()
            await _replace_message(callback.message, text="Акция временно недоступна. Попробуйте позже.",
                                   reply_markup=keyboards.create_back_to_menu_keyboard())
            return
        host_name = key.get("host_name") if key else _get_standard_key_host_name()
        selected = callback.model_copy(update={"data": f"buy_{host_name}_{offers[0]['plan_id']}_{action}_{key_id}"})
        await plan_selection_handler(selected, state)

    @user_router.callback_query(F.data.startswith("extend_key_"))
    @registration_required
    async def extend_key_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        await _safe_callback_answer(callback)

        try:
            key_id = int(callback.data.split("_")[2])
        except (IndexError, ValueError):
            await callback.message.edit_text("❌ Произошла ошибка. Неверный формат ключа.")
            return

        key_data = await asyncio.to_thread(
            _get_customer_visible_key_for_user,
            key_id,
            callback.from_user.id,
        )

        if not key_data:
            await callback.message.edit_text("❌ Ошибка: Ключ не найден или не принадлежит вам.")
            return

        key_host_name = str(key_data.get("host_name") or "").strip()
        purchase_source = PURCHASE_SOURCE_MAIN
        key_subscription_tier = _key_subscription_tier(key_data)
        if key_subscription_tier == xui_api.SUBSCRIPTION_TIER_SUPER:
            is_super_extend = True
        elif key_subscription_tier == xui_api.SUBSCRIPTION_TIER_STANDARD:
            is_super_extend = False
        else:
            # Legacy keys created before subscription_tier keep the old host-based behavior.
            is_super_extend = _is_super_key_host_name(key_host_name)

        if is_super_extend:
            await state.update_data(
                action="extend",
                key_id=key_id,
                purchase_source=purchase_source,
                is_super_key_flow=True,
                extend_host_name=key_host_name,
            )
            await show_super_key_plans(
                callback.message,
                user_id=callback.from_user.id,
                purchase_source=purchase_source,
            )
            return

        await state.update_data(
            action="extend",
            key_id=key_id,
            purchase_source=purchase_source,
            is_super_key_flow=False,
            extend_host_name=None,
        )
        await _show_new_key_plans(
            callback,
            state=state,
            purchase_source=purchase_source,
            answer_callback=False,
            action="extend",
            key_id=key_id,
            back_callback="back_to_main_menu",
        )

    @user_router.callback_query(F.data.startswith("buy_"))
    @registration_required
    async def plan_selection_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        await _safe_callback_answer(callback)
        current_data = await state.get_data()

        parts = callback.data.split("_")[1:]
        if len(parts) < 4:
            logger.warning("Unexpected buy callback format: %s", callback.data)
            return
        try:
            action = parts[-2]
            key_id = int(parts[-1])
            plan_id = int(parts[-3])
        except (TypeError, ValueError):
            logger.warning("Invalid buy callback payload: %s", callback.data)
            return
        host_name = "_".join(parts[:-3])

        if action == "extend":
            extend_key_data = await asyncio.to_thread(
                _get_customer_visible_key_for_user,
                key_id,
                callback.from_user.id,
            )
            if not extend_key_data:
                await _replace_message(
                    callback.message,
                    text="❌ Ошибка: ключ для продления не найден.",
                    reply_markup=keyboards.create_back_to_menu_keyboard(),
                )
                return
            host_name = str(extend_key_data.get("host_name") or "").strip() or host_name
        else:
            # Legacy plan messages may contain old X-UI host callback payload.
            # Standard flow must always be routed to configured primary standard host.
            standard_host_name = _get_standard_key_host_name()
            if host_name != standard_host_name:
                host_name = standard_host_name

        precheck = await _run_purchase_precheck_async(host_name, is_super_key_flow=False)
        if not precheck.allowed:
            await callback.message.edit_text(
                f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return

        await state.update_data(
            action=action,
            key_id=key_id,
            plan_id=plan_id,
            host_name=host_name,
            is_super_key_flow=False,
            super_plan_code=None,
            super_plan_name=None,
            super_plan_months=None,
            super_plan_price=None,
            purchase_source=current_data.get("purchase_source"),
        )

        await callback.message.edit_text(
            "📧 Пожалуйста, введите ваш email для отправки чека об оплате.\n\n"
            "Если вы не хотите указывать почту, нажмите кнопку ниже.",
            reply_markup=keyboards.create_skip_email_keyboard(),
        )
        await state.set_state(PaymentProcess.waiting_for_email)

    @user_router.callback_query(PaymentProcess.waiting_for_email, F.data == "back_to_plans")
    async def back_to_plans_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        await _safe_callback_answer(callback)
        data = await state.get_data()
        purchase_source = data.get("purchase_source")
        action = str(data.get("action") or "").strip().lower()
        try:
            key_id = int(data.get("key_id") or 0)
        except (TypeError, ValueError):
            key_id = 0
        extend_host_name = str(data.get("extend_host_name") or "").strip() or None
        await state.clear()

        if data.get("is_super_key_flow"):
            if action == "extend":
                purchase_source = PURCHASE_SOURCE_MAIN
            await state.update_data(purchase_source=purchase_source)
            if action == "extend":
                await state.update_data(
                    action="extend",
                    key_id=key_id,
                    is_super_key_flow=True,
                    extend_host_name=extend_host_name,
                )
            await show_super_key_plans(
                callback.message,
                user_id=callback.from_user.id,
                purchase_source=purchase_source,
            )
            return

        if action == "new":
            await _show_new_key_plans(
                callback,
                state=state,
                purchase_source=purchase_source,
                answer_callback=False,
            )
        elif action == "extend" and key_id > 0:
            await state.update_data(
                action="extend",
                key_id=key_id,
                purchase_source=PURCHASE_SOURCE_MAIN,
                is_super_key_flow=False,
            )
            await _show_new_key_plans(
                callback,
                state=state,
                purchase_source=PURCHASE_SOURCE_MAIN,
                answer_callback=False,
                action="extend",
                key_id=key_id,
                back_callback="back_to_main_menu",
            )
        else:
            await back_to_main_menu_handler(callback)

    @user_router.message(PaymentProcess.waiting_for_email)
    async def process_email_handler(message: types.Message, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_message(message, state)
            return

        email_input = message.text.strip() if message.text else ""

        if is_valid_email(email_input):
            # Sanitize email before saving
            sanitized_email = email_input.lower()
            await state.update_data(customer_email=sanitized_email)
            await message.answer("✅ Email принят")
            logger.info(f"User {message.chat.id} provided email for receipt")

            await message.answer(
                CHOOSE_PAYMENT_METHOD_MESSAGE,
                reply_markup=keyboards.create_payment_method_keyboard(payment_methods=PAYMENT_METHODS),
            )
            await state.set_state(PaymentProcess.waiting_for_payment_method)
        else:
            await message.answer("❌ Неверный формат email. Попробуйте еще раз.")

    @user_router.callback_query(PaymentProcess.waiting_for_email, F.data == "skip_email")
    async def skip_email_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        await _safe_callback_answer(callback)
        await state.update_data(customer_email=None)

        await callback.message.edit_text(
            CHOOSE_PAYMENT_METHOD_MESSAGE,
            reply_markup=keyboards.create_payment_method_keyboard(payment_methods=PAYMENT_METHODS),
        )
        await state.set_state(PaymentProcess.waiting_for_payment_method)
        logger.info(f"User {callback.from_user.id}: State set to waiting_for_payment_method")

    async def show_payment_options(message: types.Message, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_message(message, state)
            return

        data = await state.get_data()
        user_data = get_user(message.chat.id)
        plan_data = _resolve_selected_plan(data)

        if not plan_data:
            await message.edit_text("❌ Ошибка: Тариф не найден.")
            await state.clear()
            return

        price = Decimal(str(plan_data["price"]))
        final_price = await asyncio.to_thread(_apply_first_purchase_discount, price, user_data, plan_data=plan_data)
        message_text = CHOOSE_PAYMENT_METHOD_MESSAGE
        if final_price < price and not is_promotional_plan(plan_data):
            policy = await asyncio.to_thread(referrals.get_policy)
            message_text = (
                f"🎉 Скидка на первую покупку для приглашённого пользователя: {policy['discount']}%!\n"
                f"Старая цена: <s>{price:.2f} RUB</s>\n"
                f"<b>Новая цена: {final_price:.2f} RUB</b>\n\n"
            ) + CHOOSE_PAYMENT_METHOD_MESSAGE

        await state.update_data(final_price=float(final_price))

        await message.edit_text(
            message_text, reply_markup=keyboards.create_payment_method_keyboard(payment_methods=PAYMENT_METHODS)
        )
        await state.set_state(PaymentProcess.waiting_for_payment_method)

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "back_to_email_prompt")
    async def back_to_email_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        await _safe_callback_answer(callback)
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        await callback.message.edit_text(
            "📧 Пожалуйста, введите ваш email для отправки чека об оплате.\n\n"
            "Если вы не хотите указывать почту, нажмите кнопку ниже.",
            reply_markup=keyboards.create_skip_email_keyboard(),
        )
        await state.set_state(PaymentProcess.waiting_for_email)

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_yookassa")
    async def create_yookassa_payment_handler(callback: types.CallbackQuery, state: FSMContext):
        if BOT_SOURCE == "reserve":
            await _safe_callback_answer(callback, "Оплата через YooKassa недоступна в резервном боте.", show_alert=True)
            return
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        await _safe_callback_answer(callback, "Создаю ссылку на оплату...")
        await _send_callback_chat_action(callback)

        data = await state.get_data()

        plan_data = _resolve_selected_plan(data)
        if not plan_data:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return


        plan_id = plan_data["plan_id"]
        plan_name = plan_data["plan_name"]
        customer_email = data.get("customer_email")
        host_name = data.get("host_name")
        action = data.get("action")
        key_id = data.get("key_id")

        if not customer_email:
            customer_email = get_setting("receipt_email")

        months = plan_data["months"]
        user_id = callback.from_user.id

        precheck = await _run_purchase_precheck_async(
            host_name,
            is_super_key_flow=data.get("is_super_key_flow"),
            plan_data=plan_data,
        )
        if not precheck.allowed:
            await callback.message.edit_text(
                f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        discount = await _prepare_referral_invoice(user_id, plan_data, "YooKassa")
        price_rub = discount["price_rub"]
        try:
            price_str_for_api = f"{price_rub:.2f}"
            price_float_for_metadata = float(price_rub)

            receipt = None
            if customer_email and is_valid_email(customer_email):
                receipt = {
                    "customer": {"email": customer_email},
                    "items": [
                        {
                            "description": plan_name,
                            "quantity": "1.00",
                            "amount": {"value": price_str_for_api, "currency": "RUB"},
                            "vat_code": "1",
                        }
                    ],
                }
            payment_payload = {
                "amount": {"value": price_str_for_api, "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": f"https://t.me/{TELEGRAM_BOT_USERNAME}"},
                "capture": True,
                "description": plan_name,
                "metadata": {
                    "user_id": user_id,
                    "months": months,
                    "price": price_float_for_metadata,
                    "referral_discount_claim_id": discount.get("claim_id"),
                    "action": action,
                    "key_id": key_id,
                    "host_name": host_name,
                    "plan_id": plan_id,
                    "plan_name": plan_name,
                    **promotion_metadata(plan_data),
                    "customer_email": customer_email,
                    "payment_method": "YooKassa",
                    "bot_source": BOT_SOURCE,
                    "is_super_key_flow": _is_super_purchase_flow(
                        host_name=host_name,
                        is_super_key_flow=data.get("is_super_key_flow"),
                        plan_data=plan_data,
                    ),
                },
            }
            if receipt:
                payment_payload["receipt"] = receipt

            if await _reject_expired_promotion(callback, state, plan_data):
                return
            payment = await asyncio.to_thread(Payment.create, payment_payload, str(uuid.uuid4()))

            # Persist pending transaction for webhook metadata recovery/idempotency.
            external_payment_id = str(payment.id)
            await _mark_referral_invoice(discount, external_payment_id)
            pending_metadata = dict(payment_payload["metadata"])
            pending_metadata["external_payment_id"] = external_payment_id
            pending_id = await asyncio.to_thread(create_pending_transaction,
                payment_id=external_payment_id, user_id=user_id, amount_rub=float(price_rub), metadata=pending_metadata
            )
            if not pending_id:
                logger.error(
                    "YooKassa pending transaction was not created for payment_id=%s user_id=%s",
                    external_payment_id,
                    user_id,
                )
                await state.clear()
                await callback.message.edit_text(
                    "⚠️ Не удалось подготовить платеж. Попробуйте еще раз через 10-20 секунд.",
                    reply_markup=keyboards.create_back_to_menu_keyboard(),
                )
                return

            await state.clear()

            await callback.message.edit_text(
                _referral_invoice_text(discount),
                reply_markup=keyboards.create_payment_keyboard(payment.confirmation.confirmation_url),
            )
        except Exception as e:
            await _mark_referral_invoice(discount, status="uncertain")
            logger.error(f"Failed to create YooKassa payment: {e}", exc_info=True)
            await callback.message.answer("Не удалось создать ссылку на оплату.")
            await state.clear()

    @user_router.callback_query(
        PaymentProcess.waiting_for_payment_method,
        F.data.in_({"pay_lava_sbp", "pay_lava_card"}),
    )
    async def create_lava_payment_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return
        if BOT_SOURCE != "reserve" or not (PAYMENT_METHODS or {}).get("lava"):
            await _safe_callback_answer(callback, "Оплата через LAVA временно недоступна.", show_alert=True)
            return

        await _safe_callback_answer(callback, "Создаю ссылку на оплату через LAVA...")
        await _send_callback_chat_action(callback)

        data = await state.get_data()
        plan_data = _resolve_selected_plan(data)
        if not plan_data:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        plan_id = plan_data["plan_id"]
        plan_name = plan_data["plan_name"]
        customer_email = data.get("customer_email") or get_setting("receipt_email")
        host_name = data.get("host_name")
        action = data.get("action")
        key_id = data.get("key_id")
        months = plan_data["months"]
        user_id = callback.from_user.id

        precheck = await _run_purchase_precheck_async(
            host_name,
            is_super_key_flow=data.get("is_super_key_flow"),
            plan_data=plan_data,
        )
        if not precheck.allowed:
            await callback.message.edit_text(
                f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        discount = await _prepare_referral_invoice(user_id, plan_data, "LAVA")
        price_rub = discount["price_rub"]
        order_id = str(uuid.uuid4())
        await state.update_data(lava_order_id=order_id)
        callback_name = str(callback.data or "").strip()
        payment_variant = "sbp" if callback_name == "pay_lava_sbp" else "card"
        payment_service = (
            lava_api.PAYMENT_METHOD_SBP if payment_variant == "sbp" else lava_api.PAYMENT_METHOD_CARD
        )

        metadata = {
            "user_id": user_id,
            "months": months,
            "price": float(price_rub),
            "referral_discount_claim_id": discount.get("claim_id"),
            "action": action,
            "key_id": key_id,
            "host_name": host_name,
            "plan_id": plan_id,
            "plan_name": plan_name,
            "customer_email": customer_email,
            "payment_method": "LAVA",
            **promotion_metadata(plan_data),
            "lava_order_id": order_id,
            "lava_payment_service": payment_service,
            "lava_payment_variant": payment_variant,
            "bot_source": BOT_SOURCE,
            "is_super_key_flow": _is_super_purchase_flow(
                host_name=host_name,
                is_super_key_flow=data.get("is_super_key_flow"),
                plan_data=plan_data,
            ),
        }

        if await _reject_expired_promotion(callback, state, plan_data):
            return
        metadata["lava_invoice_pending"] = True
        pending_id = await asyncio.to_thread(
            create_pending_transaction, payment_id=f"lava-order:{order_id}", user_id=user_id,
            amount_rub=float(price_rub), metadata=metadata,
        )
        if not pending_id:
            await _mark_referral_invoice(
                discount, status="cancelled", note="Local pending invoice was not stored; LAVA request not sent.",
            )
            await callback.message.edit_text(
                "Не удалось подготовить счёт. Попробуйте позже.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return
        try:
            payment_data = await lava_api.create_payment(
                amount_rub=price_rub,
                order_id=order_id,
                description="Пепел ВПН",
                payment_service=payment_service,
            )
        except Exception as exc:
            await _mark_referral_invoice(discount, status="uncertain")
            logger.exception("Failed to create LAVA payment.")
            error_code = lava_api.get_exception_code(exc)
            try:
                send_critical_alert_nonblocking(
                    BotError.PAYMENT_LAVA_CREATE_FAILED,
                    details=(
                        f"source={BOT_SOURCE} payment_variant={payment_variant} "
                        f"reason=lava_create_payment_exception error={error_code}"
                    ),
                    respect_cooldown=True,
                )
            except Exception:
                logger.warning("Failed to emit LAVA create_payment alert.", exc_info=True)
            await _replace_message(
                callback.message,
                text=(
                    "❌ Не удалось создать ссылку LAVA.\n\n"
                    "Попробуйте ещё раз через 1-2 минуты или выберите другой способ оплаты."
                ),
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        external_payment_id = str(payment_data.get("payment_id") or "").strip()
        payment_url = str(payment_data.get("payment_url") or "").strip()
        if not external_payment_id or not payment_url:
            await _mark_referral_invoice(discount, external_payment_id or None, status="uncertain")
            logger.error(
                "LAVA create_payment returned invalid payload: payment_id=%r payment_url=%r",
                external_payment_id,
                payment_url,
            )
            send_critical_alert_nonblocking(
                BotError.PAYMENT_LAVA_CREATE_FAILED,
                details=(
                    f"source={BOT_SOURCE} payment_variant={payment_variant} "
                    "reason=lava_invalid_create_payment_payload"
                ),
                respect_cooldown=False,
            )
            await _replace_message(
                callback.message,
                text="❌ Платёжный провайдер вернул некорректный ответ. Попробуйте позже.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        try:
            pending_transaction = await asyncio.to_thread(
                promote_lava_pending_transaction, order_id, external_payment_id,
            )
        except (ValueError, sqlite3.Error):
            logger.exception("LAVA invoice correlation failed for order_id=%s", order_id)
            pending_transaction = None
        if not pending_transaction:
            await _mark_referral_invoice(discount, status="uncertain")
            logger.error(
                "LAVA pending transaction was not created for payment_id=%s user_id=%s",
                external_payment_id,
                user_id,
            )
            send_critical_alert_nonblocking(
                BotError.PAYMENT_LAVA_FAILED,
                details=(
                    f"source={BOT_SOURCE} user_id={user_id} payment_id={external_payment_id} "
                    "reason=lava_pending_transaction_not_created"
                ),
                respect_cooldown=False,
            )
            await _replace_message(
                callback.message,
                text="⚠️ Не удалось подготовить платёж. Попробуйте ещё раз через 10-20 секунд.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        await state.clear()
        await callback.message.edit_text(
            _referral_invoice_text(discount),
            reply_markup=keyboards.create_payment_keyboard(payment_url),
        )

    @user_router.callback_query(
        PaymentProcess.waiting_for_payment_method,
        F.data.in_({"pay_paypear_sbp", "pay_paypear_card"}),
    )
    async def create_paypear_payment_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return
        if (PAYMENT_METHODS or {}).get("lava"):
            await _safe_callback_answer(callback, "Способ оплаты обновлён. Выберите LAVA.", show_alert=True)
            await callback.message.edit_text(
                CHOOSE_PAYMENT_METHOD_MESSAGE,
                reply_markup=keyboards.create_payment_method_keyboard(payment_methods=PAYMENT_METHODS),
            )
            return

        await _safe_callback_answer(callback, "Создаю ссылку на оплату через PayPear...")
        await _send_callback_chat_action(callback)

        data = await state.get_data()

        plan_data = _resolve_selected_plan(data)
        if not plan_data:
            await callback.message.answer("Произошла ошибка при выборе тарифа.")
            await state.clear()
            return


        plan_id = plan_data["plan_id"]
        plan_name = plan_data["plan_name"]
        customer_email = data.get("customer_email")
        host_name = data.get("host_name")
        action = data.get("action")
        key_id = data.get("key_id")
        months = plan_data["months"]
        user_id = callback.from_user.id

        if not customer_email:
            customer_email = get_setting("receipt_email")

        precheck = await _run_purchase_precheck_async(
            host_name,
            is_super_key_flow=data.get("is_super_key_flow"),
            plan_data=plan_data,
        )
        if not precheck.allowed:
            await callback.message.edit_text(
                f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        discount = await _prepare_referral_invoice(user_id, plan_data, "PayPear")
        price_rub = discount["price_rub"]
        order_id = str(uuid.uuid4())
        await state.update_data(paypear_order_id=order_id)

        callback_name = str(callback.data or "").strip()
        payment_variant = "sbp" if callback_name == "pay_paypear_sbp" else "card"
        payment_method_type = (
            paypear_api.PAYMENT_METHOD_SBP if payment_variant == "sbp" else paypear_api.PAYMENT_METHOD_CARD
        )

        metadata = {
            "user_id": user_id,
            "months": months,
            "price": float(price_rub),
            "referral_discount_claim_id": discount.get("claim_id"),
            "action": action,
            "key_id": key_id,
            "host_name": host_name,
            "plan_id": plan_id,
            "plan_name": plan_name,
            "customer_email": customer_email,
            "payment_method": "PayPear",
            **promotion_metadata(plan_data),
            "paypear_order_id": order_id,
            "paypear_payment_method": payment_method_type,
            "paypear_payment_variant": payment_variant,
            "bot_source": BOT_SOURCE,
            "is_super_key_flow": _is_super_purchase_flow(
                host_name=host_name,
                is_super_key_flow=data.get("is_super_key_flow"),
                plan_data=plan_data,
            ),
        }

        try:
            if await _reject_expired_promotion(callback, state, plan_data):
                return
            payment_data = await paypear_api.create_payment(
                amount_rub=float(price_rub),
                order_id=order_id,
                description=plan_name,
                metadata=metadata,
                payment_method_type=payment_method_type,
            )
        except Exception as exc:
            await _mark_referral_invoice(discount, status="uncertain")
            logger.exception("Failed to create PayPear payment.")
            error_code = paypear_api.get_exception_code(exc)
            try:
                send_critical_alert_nonblocking(
                    BotError.PAYMENT_PAYPEAR_CREATE_FAILED,
                    details=(
                        f"source={BOT_SOURCE} "
                        f"payment_variant={payment_variant} "
                        "reason=create_payment_exception "
                        f"error={error_code}"
                    ),
                    respect_cooldown=True,
                )
            except Exception:
                logger.warning("Failed to emit PayPear create_payment alert.", exc_info=True)
            await _replace_message(
                callback.message,
                text=(
                    "❌ Не удалось создать ссылку PayPear.\n\n"  # noqa: RUF001
                    "Ответ платёжного сервиса не получен. Если счёт уже оплачен, дождитесь подтверждения. "
                    "или выберите другой способ оплаты."
                ),
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        external_payment_id = str(payment_data.get("payment_id") or "").strip()
        payment_url = str(payment_data.get("payment_url") or "").strip()
        if not external_payment_id or not payment_url:
            await _mark_referral_invoice(discount, external_payment_id or None, status="uncertain")
            logger.error(
                "PayPear create_payment returned invalid payload: payment_id=%r payment_url=%r",
                external_payment_id,
                payment_url,
            )
            try:
                send_critical_alert_nonblocking(
                    BotError.PAYMENT_PAYPEAR_CREATE_FAILED,
                    details=(
                        f"source={BOT_SOURCE} payment_variant={payment_variant} reason=invalid_create_payment_payload"
                    ),
                    respect_cooldown=False,
                )
            except Exception:
                logger.warning("Failed to emit PayPear invalid_payload alert.", exc_info=True)
            await _replace_message(
                callback.message,
                text="❌ Платежный провайдер вернул некорректный ответ. Попробуйте позже.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        await _mark_referral_invoice(discount, external_payment_id)
        pending_metadata = dict(metadata)
        pending_metadata["external_payment_id"] = external_payment_id
        pending_id = await asyncio.to_thread(create_pending_transaction,
            payment_id=external_payment_id,
            user_id=user_id,
            amount_rub=float(price_rub),
            metadata=pending_metadata,
        )
        if not pending_id:
            logger.error(
                "PayPear pending transaction was not created for payment_id=%s user_id=%s",
                external_payment_id,
                user_id,
            )
            try:
                send_critical_alert_nonblocking(
                    BotError.PAYMENT_PAYPEAR_FAILED,
                    details=(
                        f"source={BOT_SOURCE} "
                        f"user_id={user_id} "
                        f"payment_id={external_payment_id} "
                        "reason=pending_transaction_not_created"
                    ),
                    respect_cooldown=False,
                )
            except Exception:
                logger.warning("Failed to emit PayPear pending transaction alert.", exc_info=True)
            await _replace_message(
                callback.message,
                text="⚠️ Не удалось подготовить платеж. Попробуйте еще раз через 10-20 секунд.",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        await state.clear()
        await callback.message.edit_text(
            _referral_invoice_text(discount),
            reply_markup=keyboards.create_payment_keyboard(payment_url),
        )

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_cryptobot")
    async def create_cryptobot_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        await _safe_callback_answer(callback, "Создаю счет в Crypto Pay...")
        await _send_callback_chat_action(callback)

        data = await state.get_data()

        plan_data = _resolve_selected_plan(data)
        if not plan_data:
            await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        plan_id = plan_data["plan_id"]
        plan_name = plan_data["plan_name"]
        user_id = callback.from_user.id
        customer_email = data.get("customer_email")
        host_name = data.get("host_name")
        action = data.get("action")
        key_id = data.get("key_id")
        is_super_flow = _is_super_purchase_flow(
            host_name=host_name,
            is_super_key_flow=data.get("is_super_key_flow"),
            plan_data=plan_data,
        )

        precheck = await _run_purchase_precheck_async(
            host_name,
            is_super_key_flow=is_super_flow,
            plan_data=plan_data,
        )
        if not precheck.allowed:
            await callback.message.edit_text(
                f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        cryptobot_token = get_setting("cryptobot_token")
        if not cryptobot_token:
            logger.error(
                "Attempt to create Crypto Pay invoice failed for user %s: cryptobot_token is not set.", user_id
            )
            await callback.message.edit_text(
                "❌ Оплата криптовалютой временно недоступна. (Администратор не указал токен)."
            )
            await state.clear()
            return

        months = plan_data["months"]

        discount = await _prepare_referral_invoice(user_id, plan_data, "CryptoBot")
        price_rub = discount["price_rub"]
        try:
            crypto = CryptoPay(cryptobot_token)

            payload_data = (
                f"{user_id}:{months}:{float(price_rub)}:{action}:{key_id}:"
                f"{host_name}:{plan_id}:{customer_email}:CryptoBot:{int(is_super_flow)}:"
                f"{_encode_cryptobot_payload_value(plan_name)}:{BOT_SOURCE}:{discount.get('claim_id') or ''}"
            )

            if await _reject_expired_promotion(callback, state, plan_data):
                return
            invoice = await crypto.create_invoice(
                currency_type="fiat",
                fiat="RUB",
                amount=float(price_rub),
                description=f"Подписка на {months} мес.",
                payload=payload_data,
                expires_in=3600,
            )

            if not invoice or not invoice.pay_url:
                raise Exception("Failed to create invoice or pay_url is missing.")

            external_payment_id = str(invoice.invoice_id)
            await _mark_referral_invoice(discount, external_payment_id)
            await asyncio.to_thread(
                create_pending_transaction, payment_id=external_payment_id, user_id=user_id,
                amount_rub=float(price_rub), metadata={
                    "user_id": user_id, "months": months, "price": float(price_rub),
                    "action": action, "key_id": key_id, "host_name": host_name, "plan_id": plan_id,
                    "plan_name": plan_name, "customer_email": customer_email, "payment_method": "CryptoBot",
                    "bot_source": BOT_SOURCE, "is_super_key_flow": is_super_flow,
                    "external_payment_id": external_payment_id,
                    "referral_discount_claim_id": discount.get("claim_id"),
                    **promotion_metadata(plan_data),
                },
            )
            await callback.message.edit_text(
                _referral_invoice_text(discount), reply_markup=keyboards.create_payment_keyboard(invoice.pay_url)
            )
            await state.clear()

        except Exception as e:
            await _mark_referral_invoice(discount, status="uncertain")
            logger.error(f"Failed to create Crypto Pay invoice for user {user_id}: {e}", exc_info=True)
            await callback.message.edit_text(
                "❌ Не удалось получить ссылку на оплату криптовалютой.\n\n"
                "Если вы уже оплатили счёт, дождитесь подтверждения и не оплачивайте его повторно. "
                "При повторении ошибки обратитесь в поддержку."
            )
            await state.clear()

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_heleket")
    async def create_heleket_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        await _safe_callback_answer(callback, "Создаю счет Heleket...")
        await _send_callback_chat_action(callback)

        data = await state.get_data()

        plan_data = _resolve_selected_plan(data)
        if not plan_data:
            await callback.message.edit_text("❌ Произошла ошибка при выборе тарифа.")
            await state.clear()
            return

        precheck = await _run_purchase_precheck_async(
            data.get("host_name"),
            is_super_key_flow=data.get("is_super_key_flow"),
            plan_data=plan_data,
        )
        if not precheck.allowed:
            await callback.message.edit_text(
                f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        discount = await _prepare_referral_invoice(callback.from_user.id, plan_data, "Heleket")
        final_price_float = float(discount["price_rub"])
        payment_state_data = dict(data)
        payment_state_data["referral_discount_claim_id"] = discount.get("claim_id")
        payment_state_data["plan_id"] = plan_data["plan_id"]
        payment_state_data["plan_name"] = plan_data["plan_name"]
        payment_state_data.update(promotion_metadata(plan_data))

        if await _reject_expired_promotion(callback, state, plan_data):
            return
        pay_url = await _create_heleket_payment_request(
            user_id=callback.from_user.id,
            price=final_price_float,
            months=plan_data["months"],
            host_name=data.get("host_name"),
            state_data=payment_state_data,
        )

        if pay_url:
            await callback.message.edit_text(
                _referral_invoice_text(discount), reply_markup=keyboards.create_payment_keyboard(pay_url)
            )
            await state.clear()
        else:
            await callback.message.edit_text("❌ Не удалось создать счет Heleket. Попробуйте другой способ оплаты.")

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_tonconnect")
    async def create_ton_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        if _main_payments_disabled():
            await _show_main_purchase_redirect_for_callback(callback, state)
            return

        logger.info(f"User {callback.from_user.id}: Entered create_ton_invoice_handler.")
        data = await state.get_data()
        user_id = callback.from_user.id
        wallet_address = get_setting("ton_wallet_address")
        plan_data = _resolve_selected_plan(data)

        if not wallet_address or not plan_data:
            await callback.message.edit_text("❌ Оплата через TON временно недоступна.")
            await state.clear()
            return

        precheck = await _run_purchase_precheck_async(
            data.get("host_name"),
            is_super_key_flow=data.get("is_super_key_flow"),
            plan_data=plan_data,
        )
        if not precheck.allowed:
            await callback.message.edit_text(
                f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            await state.clear()
            return

        await _safe_callback_answer(callback, "Создаю ссылку и QR-код для TON Connect...")
        await _send_callback_chat_action(callback, ChatAction.UPLOAD_PHOTO)

        usdt_rub_rate = await get_usdt_rub_rate()
        ton_usdt_rate = await get_ton_usdt_rate()

        if not usdt_rub_rate or not ton_usdt_rate:
            await callback.message.edit_text("❌ Не удалось получить курс TON. Попробуйте позже.")
            await state.clear()
            return

        discount = await _prepare_referral_invoice(user_id, plan_data, "TON Connect")
        price_rub = discount["price_rub"]
        price_ton = max(Decimal("0.001"), (price_rub / usdt_rub_rate / ton_usdt_rate).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))
        amount_nanoton = int(price_ton * 1_000_000_000)

        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id,
            "months": plan_data["months"],
            "price": float(price_rub),
            "referral_discount_claim_id": discount.get("claim_id"),
            "action": data.get("action"),
            "key_id": data.get("key_id"),
            "host_name": data.get("host_name"),
            "plan_id": plan_data["plan_id"],
            "plan_name": plan_data["plan_name"],
            "customer_email": data.get("customer_email"),
            "payment_method": "TON Connect",
            **promotion_metadata(plan_data),
            "bot_source": BOT_SOURCE,
            "is_super_key_flow": _is_super_purchase_flow(
                host_name=data.get("host_name"),
                is_super_key_flow=data.get("is_super_key_flow"),
                plan_data=plan_data,
            ),
        }
        if await _reject_expired_promotion(callback, state, plan_data):
            return
        pending_id = await asyncio.to_thread(create_pending_transaction, payment_id, user_id, float(price_rub), metadata)
        if not pending_id:
            await _mark_referral_invoice(discount, status="cancelled", note="Pending invoice was not stored; provider request not sent.")
            await callback.message.edit_text("Не удалось подготовить счёт. Попробуйте позже.")
            await state.clear()
            return
        await _mark_referral_invoice(discount, payment_id)

        transaction_payload = {
            "messages": [{"address": wallet_address, "amount": str(amount_nanoton), "payload": payment_id}],
            "valid_until": int(datetime.now(UTC).timestamp()) + 600,
        }

        try:
            connect_url = await _start_ton_connect_process(user_id, transaction_payload)

            qr_img = qrcode.make(connect_url)
            bio = BytesIO()
            qr_img.save(bio, "PNG")
            qr_file = BufferedInputFile(bio.getvalue(), "ton_qr.png")

            await callback.message.delete()
            await callback.message.answer_photo(
                photo=qr_file,
                caption=(
                    f"💎 **Оплата через TON Connect**\n\n"
                    f"Сумма к оплате: `{price_ton}` **TON** ({price_rub:.2f} RUB)\n\n"
                    + _referral_invoice_text(discount).replace("<b>", "").replace("</b>", "") + "\n\n"
                    +
                    "✅ **Способ 1 (на телефоне):** Нажмите кнопку **'Открыть кошелек'** ниже.\n"
                    "✅ **Способ 2 (на компьютере):** Отсканируйте QR-код кошельком.\n\n"
                    "После подключения кошелька подтвердите транзакцию."
                ),
                parse_mode="Markdown",
                reply_markup=keyboards.create_ton_connect_keyboard(connect_url),
            )
            await state.clear()

        except Exception:
            await _mark_referral_invoice(discount, payment_id, status="uncertain")
            logger.exception("Failed to generate TON Connect link for user %s", user_id)
            await callback.message.answer("❌ Не удалось создать ссылку для TON Connect. Попробуйте позже.")
            await state.clear()

    @user_router.message(F.text)
    @registration_required
    async def unknown_message_handler(message: types.Message):
        if message.text.startswith("/"):
            await message.answer("Такой команды не существует. Попробуйте /start.")
        else:
            await message.answer("Я не понимаю эту команду. Пожалуйста, используйте кнопки меню.")

    return user_router


_user_connectors: dict[int, TonConnect] = {}
_listener_tasks: dict[int, asyncio.Task] = {}
_listener_tokens: dict[int, object] = {}


async def _get_ton_connect_instance(user_id: int) -> TonConnect:
    if user_id not in _user_connectors:
        manifest_url = "https://raw.githubusercontent.com/ton-blockchain/ton-connect/main/requests-responses.json"
        _user_connectors[user_id] = TonConnect(manifest_url=manifest_url)
    return _user_connectors[user_id]


async def _listener_task(connector: TonConnect, user_id: int, transaction_payload: dict, listener_token: object):
    try:
        wallet_connected = False
        for _ in range(120):
            if connector.connected:
                wallet_connected = True
                break
            await asyncio.sleep(1)

        if not wallet_connected:
            logger.warning("TON Connect: Timeout waiting for wallet connection from user %s.", user_id)
            return

        logger.info("TON Connect: Wallet connected for user %s. Address: %s", user_id, connector.account.address)

        logger.info(
            "TON Connect: Sending transaction request to user %s with payload: %s", user_id, transaction_payload
        )
        await connector.send_transaction(transaction_payload)

        logger.info("TON Connect: Transaction request sent successfully for user %s.", user_id)

    except UserRejectsError:
        logger.warning("TON Connect: User %s rejected the transaction.", user_id)
    except Exception:
        logger.exception("TON Connect: An error occurred in the listener task for user %s", user_id)
    finally:
        if _listener_tokens.get(user_id) is listener_token:
            _listener_tokens.pop(user_id, None)
            _user_connectors.pop(user_id, None)
            _listener_tasks.pop(user_id, None)


async def _start_ton_connect_process(user_id: int, transaction_payload: dict) -> str:
    if user_id in _listener_tasks and not _listener_tasks[user_id].done():
        _listener_tasks[user_id].cancel()

    connector = await _get_ton_connect_instance(user_id)
    listener_token = object()
    _listener_tokens[user_id] = listener_token

    task = asyncio.create_task(_listener_task(connector, user_id, transaction_payload, listener_token))
    _listener_tasks[user_id] = task

    wallets = connector.get_wallets()
    return await connector.connect(wallets[0])


async def process_successful_onboarding(callback: types.CallbackQuery, state: FSMContext):
    await _safe_callback_answer(callback, "✅ Спасибо! Доступ предоставлен.")
    set_terms_agreed(callback.from_user.id)
    await state.clear()
    await callback.message.delete()
    await send_effect_message(
        callback.message.bot,
        chat_id=callback.from_user.id,
        text="Приятного использования!",
        effect_setting_key=ONBOARDING_SUCCESS_MESSAGE_EFFECT_ID_SETTING,
        reply_markup=keyboards.main_reply_keyboard,
    )
    await show_main_menu(callback.message)


async def is_url_reachable(url: str) -> bool:
    """Validate URL format, length, and reachability with security checks"""
    # Limit URL length
    if not url or len(url) > 2048:
        return False

    # Block local and internal network addresses
    blocked_patterns = [
        r"localhost",
        r"127\.0\.0\.",
        r"0\.0\.0\.0",
        r"10\.",
        r"172\.(1[6-9]|2[0-9]|3[01])\.",
        r"192\.168\.",
        r"\[::1\]",
        r"\[::ffff:127\.0\.0\.1\]",
    ]

    for pattern in blocked_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            logger.warning("Blocked local/internal URL attempt: %s", url)
            return False

    # Validate URL format
    pattern = re.compile(
        r"^(https?://)"
        r"(([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,})"
        r"(/.*)?$"
    )
    if not re.match(pattern, url):
        return False

    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session,
            session.head(url, allow_redirects=True) as response,
        ):
            return response.status < 400
    except (aiohttp.ClientError, TimeoutError) as e:
        logger.warning("URL validation failed for %s... Error: %s", url[:50], type(e).__name__)
        return False


async def notify_admin_of_purchase(bot: Bot, metadata: dict):
    chat_ids = _get_purchase_notification_chat_ids()
    if not chat_ids:
        logger.warning("Success notification skipped: no purchase chat ids configured.")
        return

    try:
        user_id = metadata.get("user_id")
        months = metadata.get("months")
        price_raw = metadata.get("price")
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            price = 0.0
        host_name = metadata.get("host_name")
        plan_id = metadata.get("plan_id")
        plan_name_from_metadata = metadata.get("plan_name")
        payment_method = str(metadata.get("payment_method", "Unknown") or "Unknown")

        plan_info = get_plan_by_id(plan_id)
        plan_name = plan_name_from_metadata or (plan_info.get("plan_name") if plan_info else None) or f"{months} мес."
        key_display_name = _resolve_key_display_name_from_metadata(metadata)
        user_db = get_user(user_id) if user_id else None
        username_str = ""
        if user_db and user_db.get("username"):
            username_str = f"@{user_db.get('username')} "

        message_text = (
            f"🎉 Новая покупка! 🎉\n\n"
            f"👤 Пользователь: {username_str}(ID: {user_id or '—'})\n"
            f"🌍 Сервер: {host_name or '—'}\n"
            f"🔑 Название ключа: {key_display_name or '—'}\n"
            f"📄 Тариф: {plan_name or '—'}\n"
            f"💰 Сумма: {price:.2f} RUB\n"
            f"💳 Способ оплаты: {payment_method}"
        )

        sent_count = 0
        bot_token = _get_purchase_notification_bot_token()
        for chat_id in chat_ids:
            try:
                await _send_alert_routed_message(
                    bot,
                    chat_id=chat_id,
                    text=message_text,
                    bot_token=bot_token,
                    parse_mode="HTML",
                )
                sent_count += 1
            except Exception as send_error:
                logger.error(
                    "Failed to send success payment notification to chat_id=%s: %s",
                    chat_id,
                    send_error,
                    exc_info=True,
                )
        if sent_count == 0:
            logger.error("Success payment notification was not delivered to any chat. user_id=%s", user_id)

    except Exception as e:
        logger.error(f"Failed to send success payment notification: {e}", exc_info=True)


def _parse_alert_chat_ids(raw_chat_ids: str | None) -> list[str]:
    normalized = str(raw_chat_ids or "").replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _dedupe_chat_ids(chat_ids: list[str]) -> list[str]:
    result: list[str] = []
    for chat_id in chat_ids:
        if chat_id and chat_id not in result:
            result.append(chat_id)
    return result


def _get_ops_alert_chat_ids() -> list[str]:
    alert_chat_ids = _parse_alert_chat_ids(get_setting("alert_telegram_chat_id"))
    if alert_chat_ids:
        return _dedupe_chat_ids(alert_chat_ids)

    admin_chat_id = str(ADMIN_ID or "").strip()
    return [admin_chat_id] if admin_chat_id else []


def _get_purchase_notification_chat_ids() -> list[str]:
    configured = (
        get_setting("purchase_reporter_chat_id")
        or os.getenv("PURCHASE_REPORTER_CHAT_ID")
        or "-5082385899"
    )
    return _dedupe_chat_ids(_parse_alert_chat_ids(configured))


def _get_ops_alert_bot_token() -> str:
    return str(
        get_setting("alert_telegram_bot_token")
        or os.getenv("ALERT_TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()


def _get_purchase_notification_bot_token() -> str:
    return str(
        get_setting("reserve_bot_token")
        or os.getenv("RESERVE_BOT_TOKEN")
        or get_setting("telegram_bot_token")
        or os.getenv("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()


async def _send_alert_routed_message(
    bot: Bot,
    *,
    chat_id: str,
    text: str,
    bot_token: str,
    parse_mode: str | None = None,
    disable_web_page_preview: bool | None = None,
) -> None:
    sender = Bot(token=bot_token) if bot_token else bot
    try:
        kwargs = {
            "chat_id": chat_id,
            "text": text,
        }
        if parse_mode is not None:
            kwargs["parse_mode"] = parse_mode
        if disable_web_page_preview is not None:
            kwargs["disable_web_page_preview"] = disable_web_page_preview
        await sender.send_message(**kwargs)
    finally:
        if sender is not bot:
            await sender.session.close()


def _sanitize_failure_logs(failure_logs: dict | None) -> dict[str, str]:
    if not isinstance(failure_logs, dict):
        return {}
    cleaned: dict[str, str] = {}
    for raw_key, raw_value in failure_logs.items():
        key = str(raw_key or "").strip()
        if not key:
            continue
        value = str(raw_value or "").strip()
        if not value:
            continue
        if len(value) > 500:
            value = value[:497] + "..."
        cleaned[key] = value
    return cleaned


try:
    _payment_issuance_sla_raw = int(str(os.getenv("PAYMENT_ISSUANCE_SLA_SECONDS", "900")).strip() or "900")
except (TypeError, ValueError):
    _payment_issuance_sla_raw = 900
PAYMENT_ISSUANCE_ALERT_SLA_SECONDS = max(60, _payment_issuance_sla_raw)


def _parse_iso_datetime_loose(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    return parsed


def _resolve_issuance_alert_error(
    *,
    reason: str,
    retry_count: int,
    first_failure_at: datetime | None,
) -> BotError:
    _ = reason
    _ = retry_count
    _ = first_failure_at
    return BotError.PAYMENT_KEY_ISSUE_FAILED


async def _issue_trial_super_with_quick_retries(
    *,
    host_name: str,
    user_id: int,
    key_number: int,
    trial_days: int,
    target_expiry_ms: int | None = None,
) -> tuple[dict | None, bool, bool]:
    issue_lock = await _try_acquire_trial_super_issue_lock(user_id)
    if issue_lock is None:
        return None, False, True

    attempts = max(1, TRIAL_ISSUE_QUICK_RETRY_ATTEMPTS)
    email = _trial_super_email(user_id, key_number)
    resolved_target_expiry_ms = int(target_expiry_ms or _trial_super_target_expiry_ms(trial_days))
    timeout_seconds = max(TRIAL_SUPER_ISSUE_TIMEOUT_SECONDS, TRIAL_ISSUE_QUICK_RETRY_ATTEMPT_TIMEOUT_SECONDS * attempts)

    try:
        result = await asyncio.wait_for(
            xui_api.create_or_update_key_on_host(
                host_name=host_name,
                email=email,
                days_to_add=trial_days,
                target_expiry_ms=resolved_target_expiry_ms,
                white_traffic_limit_bytes=xui_api.TRIAL_SUPER_WHITELIST_TRAFFIC_BYTES,
                include_white_inbounds=True,
                max_attempts=attempts,
                retry_backoff_seconds=TRIAL_ISSUE_QUICK_RETRY_BACKOFF_SECONDS,
            ),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        logger.warning(
            "Trial super issuance timeout user=%s host=%s attempts=%s timeout=%ss",
            user_id,
            host_name,
            attempts,
            timeout_seconds,
        )
        return None, True, False
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning(
            "Trial super issuance failed user=%s host=%s attempts=%s error=%s",
            user_id,
            host_name,
            attempts,
            exc,
        )
        return None, False, False
    finally:
        _release_trial_super_issue_lock(user_id, issue_lock)

    return result, False, False


async def _verify_trial_super_after_uncertain_issue(
    *,
    host_name: str,
    user_id: int,
    key_number: int,
    target_expiry_ms: int,
) -> dict | None:
    email = _trial_super_email(user_id, key_number)
    attempts = max(1, int(TRIAL_SUPER_VERIFY_ATTEMPTS))
    delay_seconds = max(0.0, float(TRIAL_SUPER_VERIFY_DELAY_SECONDS))

    for attempt in range(1, attempts + 1):
        if attempt > 1 and delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        existing_key = await asyncio.to_thread(_existing_active_trial_super_key, user_id, host_name)
        if existing_key:
            return _trial_result_from_existing_key(existing_key)

        try:
            result = await xui_api.get_existing_key_by_email_on_host(
                host_name,
                email,
                target_expiry_ms=target_expiry_ms,
                subscription_tier=xui_api.SUBSCRIPTION_TIER_SUPER,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "Trial super verification failed user=%s host=%s email=%s attempt=%s/%s error=%s",
                user_id,
                host_name,
                email,
                attempt,
                attempts,
                exc,
            )
            result = None
        if result:
            logger.warning(
                "Trial super recovered by verification user=%s host=%s email=%s attempt=%s/%s",
                user_id,
                host_name,
                email,
                attempt,
                attempts,
            )
            return result

    return None


def _increment_trial_super_timeout_count(user_id: int) -> int:
    now = time.monotonic()
    _prune_trial_super_timeout_counts(now)
    previous_entry = _trial_super_timeout_counts.get(user_id)
    previous = int(previous_entry[0] if previous_entry else 0)
    current = previous + 1
    _trial_super_timeout_counts[user_id] = (current, now)
    _prune_trial_super_timeout_counts(now)
    return current


def _consume_trial_super_timeout_count(user_id: int) -> int:
    _prune_trial_super_timeout_counts()
    previous_entry = _trial_super_timeout_counts.pop(user_id, None)
    return int(previous_entry[0] if previous_entry else 0)


def _prune_trial_super_timeout_counts(now: float | None = None) -> None:
    current_time = time.monotonic() if now is None else now
    expired_user_ids = [
        user_id
        for user_id, (_count, updated_at) in _trial_super_timeout_counts.items()
        if current_time - updated_at > _TRIAL_SUPER_TIMEOUT_COUNT_TTL_SECONDS
    ]
    for user_id in expired_user_ids:
        _trial_super_timeout_counts.pop(user_id, None)

    overflow = len(_trial_super_timeout_counts) - _TRIAL_SUPER_TIMEOUT_COUNT_MAX_USERS
    if overflow <= 0:
        return

    oldest_user_ids = sorted(
        _trial_super_timeout_counts,
        key=lambda user_id: _trial_super_timeout_counts[user_id][1],
    )[:overflow]
    for user_id in oldest_user_ids:
        _trial_super_timeout_counts.pop(user_id, None)


def _mark_transaction_issuance_failure(
    *,
    payment_id: str | None,
    metadata: dict | None,
    reason: str,
    failure_logs: dict | None = None,
    fatal: bool = False,
) -> None:
    if not payment_id:
        return
    now_iso = datetime.now().isoformat()
    updated_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    previous_count = int(updated_metadata.get("issuance_failure_count") or 0)

    updated_metadata["issuance_failed"] = True
    updated_metadata["issuance_failure_reason"] = str(reason or "unknown")
    updated_metadata["issuance_failure_last_at"] = now_iso
    if not updated_metadata.get("issuance_failure_first_at"):
        updated_metadata["issuance_failure_first_at"] = now_iso
    updated_metadata["issuance_failure_count"] = previous_count + 1
    updated_metadata["issuance_failure_alert_sent"] = True
    if fatal:
        updated_metadata["issuance_fatal"] = True
    updated_metadata.pop("autoretry_exhausted_alert_sent", None)
    updated_metadata.pop("autoretry_exhausted_alert_at", None)
    if not updated_metadata.get("retry_source"):
        updated_metadata["retry_source"] = "issuance_autoretry"

    logs_payload = _sanitize_failure_logs(failure_logs)
    if logs_payload:
        updated_metadata["issuance_failure_logs"] = logs_payload

    try:
        if fatal:
            updated = mark_transaction_fatal_failure(
                payment_id,
                reason=str(reason or "unknown"),
                metadata=updated_metadata,
                payment_method=str(updated_metadata.get("payment_method") or ""),
                statuses=("processing", "pending", "paid"),
            )
            if not updated:
                logger.warning("Failed to persist fatal issuance failure for payment_id=%s", payment_id)
                return
            if isinstance(metadata, dict):
                metadata.clear()
                metadata.update(updated_metadata)
            return

        immediate_retry_at = datetime.now() if previous_count == 0 else None
        retry_state = mark_transaction_retryable_failure(
            payment_id,
            reason=str(reason or "unknown"),
            metadata=updated_metadata,
            payment_method=str(updated_metadata.get("payment_method") or ""),
            next_retry_at=immediate_retry_at,
            statuses=("processing", "pending", "paid"),
        )
    except Exception as update_error:
        logger.warning(
            "Failed to persist issuance failure metadata for payment_id=%s: %s",
            payment_id,
            update_error,
        )
        return
    if not retry_state.get("updated"):
        logger.warning("Failed to persist issuance failure metadata for payment_id=%s", payment_id)
        return

    updated_metadata["retry_count"] = int(retry_state.get("retry_count") or (previous_count + 1))
    updated_metadata["next_retry_at"] = retry_state.get("next_retry_at")
    if isinstance(metadata, dict):
        metadata.clear()
        metadata.update(updated_metadata)


def _format_failure_logs_for_message(failure_logs: dict | None) -> str:
    logs_payload = _sanitize_failure_logs(failure_logs)
    if not logs_payload:
        return ""
    fields = [(f"Диагностика / {key}", value) for key, value in logs_payload.items()]
    return "\n\n<b>Диагностика ошибки</b>\n\n" + _format_copyable_fields(fields)


def _runtime_source_label() -> str:
    return "Резервный бот" if str(BOT_SOURCE).strip().lower() == "reserve" else "Основной бот"


def _format_copyable_fields(fields: list[tuple[str, object]]) -> str:
    lines: list[str] = []
    for label, raw_value in fields:
        value = str(raw_value if raw_value is not None else "—")
        normalized = value.strip() or "—"
        normalized_label = str(label).strip() or "Поле"
        lines.append(f"<b>{html_escape(normalized_label)}</b>\n<pre>{html_escape(normalized)}</pre>")
    return "\n\n".join(lines)


_SUPPRESSED_PAYMENT_ISSUE_ALERT_REASONS = frozenset(
    {
        "precheck_blocked:host_maintenance",
    }
)


def _should_suppress_payment_issue_alert(*, alert_error: BotError, reason: str | None) -> bool:
    if alert_error != BotError.PAYMENT_KEY_ISSUE_FAILED:
        return False
    normalized_reason = str(reason or "").strip().lower()
    return normalized_reason in _SUPPRESSED_PAYMENT_ISSUE_ALERT_REASONS


async def notify_admin_of_issuance_failure(
    bot: Bot,
    *,
    user_id: int,
    payment_id: str | None,
    payment_method: str | None,
    host_name: str,
    plan_id: int | None,
    plan_name: str | None,
    months: int | None,
    amount_rub: float | None,
    action: str | None,
    key_email: str | None,
    reason: str,
    failure_logs: dict | None = None,
    alert_error: BotError | None = None,
) -> None:
    # Keep the alert API stable while the current message omits plan details.
    _ = plan_id, plan_name, months, amount_rub
    chat_ids = _get_ops_alert_chat_ids()
    if not chat_ids:
        return

    readable_reason = humanize_alert_reason(reason)
    resolved_host_name = str(host_name or "").strip()
    retry_count = 0
    first_failure_at = None
    tx_meta_dict: dict | None = None
    if payment_id:
        tx = get_transaction_by_payment_id(payment_id)
        if isinstance(tx, dict):
            retry_count = int(tx.get("retry_count") or 0)
            tx_metadata = tx.get("metadata")
            if isinstance(tx_metadata, str):
                try:
                    tx_meta_dict = json.loads(tx_metadata)
                except json.JSONDecodeError:
                    tx_meta_dict = None
            if not resolved_host_name and isinstance(tx_meta_dict, dict):
                resolved_host_name = str(tx_meta_dict.get("host_name") or "").strip()
            first_failure_at = _parse_iso_datetime_loose(
                (tx_meta_dict or {}).get("issuance_failure_first_at")
                if isinstance(tx_meta_dict, dict)
                else tx.get("confirmed_at")
            )
    if alert_error is None:
        alert_error = _resolve_issuance_alert_error(
            reason=reason,
            retry_count=retry_count,
            first_failure_at=first_failure_at,
        )
    if _should_suppress_payment_issue_alert(alert_error=alert_error, reason=reason):
        normalized_reason = str(reason or "").strip().lower()
        logger.info(
            "Payment issue alert suppressed for payment_id=%s reason=%s host=%s",
            payment_id,
            normalized_reason,
            resolved_host_name or "unknown",
        )
        if payment_id:
            suppressed_at = _naive_utc_now().isoformat(timespec="seconds")
            for field_name, field_value in (
                ("payment_issue_critical_suppressed", True),
                ("payment_issue_critical_suppressed_reason", normalized_reason),
                ("payment_issue_critical_suppressed_at", suppressed_at),
            ):
                update_transaction_metadata_field(
                    payment_id,
                    field_name,
                    field_value,
                    statuses=("pending", "processing", "paid", "exhausted"),
                )
        return
    if (
        payment_id
        and alert_error == BotError.PAYMENT_KEY_ISSUE_FAILED
        and isinstance(tx_meta_dict, dict)
        and bool(tx_meta_dict.get("payment_issue_critical_sent"))
    ):
        logger.info(
            "Issuance failure alert deduped for payment_id=%s reason=%s",
            payment_id,
            reason,
        )
        return

    trace = ""
    if isinstance(tx_meta_dict, dict):
        trace = str(tx_meta_dict.get("trace_id") or "")
    detail = (
        f"source={BOT_SOURCE} " + (f"trace={trace} " if trace else "") + f"payment_id={payment_id or 'unknown'} "
        f"user_id={user_id} "
        f"method={payment_method or 'unknown'} "
        f"host={resolved_host_name or 'unknown'} "
        f"reason={reason}"
    )
    try:
        send_critical_alert_nonblocking(
            alert_error,
            details=detail,
            respect_cooldown=True,
        )
        if payment_id and alert_error == BotError.PAYMENT_KEY_ISSUE_FAILED:
            update_transaction_metadata_field(
                payment_id,
                "payment_issue_critical_sent",
                True,
                statuses=("pending", "processing", "paid", "exhausted"),
            )
    except Exception:
        logger.warning("Failed to emit critical issuance failure alert.", exc_info=True)
        return

    try:
        key_display_name = (
            _resolve_extend_key_display_name(original_email=key_email)
            if str(action or "").strip().lower() == "extend"
            else "Новый ключ"
        )
        normalized_method = str(payment_method or "").strip().lower()
        title_text = "🔴 Оплата есть, ключ не выдан"
        try:
            retry_limit = max(
                1,
                int(str(os.environ.get("PAYMENT_ISSUANCE_AUTORETRY_MAX_ATTEMPTS", "8")).strip() or "8"),
            )
        except (TypeError, ValueError):
            retry_limit = 8
        fields: list[tuple[str, object]] = [
            ("ID платежа", payment_id or "—"),
            ("ID Telegram", user_id),
        ]
        if "yookassa" in normalized_method:
            fields.append(("ID YooKassa", payment_id or "—"))

        host_fields: list[tuple[str, object]] = [
            ("Хост", resolved_host_name or "—"),
            ("Название ключа", key_display_name),
        ]

        severity_label = "Критично"
        time_text = datetime.now().strftime("%d.%m.%Y %H:%M")

        message_text = (
            f"<b>{html_escape(title_text)}</b>\n"
            f"{html_escape(severity_label)} · {html_escape(_runtime_source_label())} · {html_escape(time_text)}\n\n"
            f"<b>Причина:</b> {html_escape(readable_reason)}\n"
            f"<b>Попытка:</b> {retry_count}/{retry_limit}\n\n"
            f"{'━' * 20}\n"
            f"<b>Клиент и платёж</b>\n\n"
            f"{_format_copyable_fields(fields)}\n\n"
            f"{'━' * 20}\n"
            f"<b>Сервер и ключ</b>\n\n"
            f"{_format_copyable_fields(host_fields)}\n\n"
            f"{'━' * 20}\n"
            f"<b>Что сделать:</b> Проверьте статус хоста и повторите выдачу.\n"
        )
        message_text += _format_failure_logs_for_message(failure_logs)
        sent_count = 0
        bot_token = _get_ops_alert_bot_token()
        for chat_id in chat_ids:
            try:
                await _send_alert_routed_message(
                    bot,
                    chat_id=chat_id,
                    text=message_text,
                    bot_token=bot_token,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                sent_count += 1
            except Exception as send_error:
                logger.error(
                    "Failed to send issuance failure notification to chat_id=%s: %s",
                    chat_id,
                    send_error,
                    exc_info=True,
                )
        if sent_count == 0:
            logger.error("Issuance failure notification was not delivered to any chat. user_id=%s", user_id)
    except Exception as e:
        logger.error("Failed to send admin issuance failure notification: %s", e, exc_info=True)


async def notify_admin_of_issuance_recovered(
    bot: Bot,
    *,
    metadata: dict,
    retry_source: str | None = None,
) -> None:
    # Keep the retry callback contract stable for scheduler call sites.
    _ = retry_source
    chat_ids = _get_ops_alert_chat_ids()
    if not chat_ids:
        return

    try:
        user_id = int(metadata.get("user_id"))
    except (TypeError, ValueError):
        return

    payment_id = str(metadata.get("external_payment_id") or metadata.get("payment_id") or "—")
    payment_method = str(metadata.get("payment_method") or "—")
    host_name = str(metadata.get("host_name") or "—")
    key_display_name = _resolve_key_display_name_from_metadata(metadata)
    failure_reason = str(metadata.get("issuance_failure_reason") or "—")
    key_id = metadata.get("key_id")
    try:
        retry_count = int(metadata.get("retry_count") or 0)
    except (TypeError, ValueError):
        retry_count = 0
    try:
        retry_limit = max(
            1,
            int(str(os.environ.get("PAYMENT_ISSUANCE_AUTORETRY_MAX_ATTEMPTS", "8")).strip() or "8"),
        )
    except (TypeError, ValueError):
        retry_limit = 8
    delay_text = "—"
    first_failure_at = metadata.get("issuance_failure_first_at")
    if first_failure_at:
        try:
            started = datetime.fromisoformat(str(first_failure_at).replace("Z", "+00:00"))
            delay_minutes = max(0, int((datetime.now() - started.replace(tzinfo=None)).total_seconds() // 60))
            delay_text = f"{delay_minutes} мин"
        except Exception:
            delay_text = str(first_failure_at)

    client_fields: list[tuple[str, object]] = [
        ("ID платежа", payment_id),
        ("ID Telegram", user_id),
    ]
    if "yookassa" in payment_method.lower():
        client_fields.append(("ID YooKassa", payment_id))

    key_fields: list[tuple[str, object]] = [
        ("Хост", host_name),
        ("Название ключа", key_display_name),
        ("ID ключа", str(key_id) if key_id is not None else "—"),
    ]

    readable_failure_reason = humanize_alert_reason(failure_reason)
    time_text = datetime.now().strftime("%d.%m.%Y %H:%M")

    message_text = (
        "<b>✅ Ключ довыдан автоматически</b>\n"
        f"Успешно · {html_escape(_runtime_source_label())} · {html_escape(time_text)}\n\n"
        f"<b>Причина задержки:</b> {html_escape(readable_failure_reason)}\n"
        f"<b>Попытка довыдачи:</b> {retry_count}/{retry_limit}\n"
        f"<b>Задержка:</b> {html_escape(delay_text)}\n\n"
        f"{'━' * 20}\n"
        f"<b>Клиент и платёж</b>\n\n"
        f"{_format_copyable_fields(client_fields)}\n\n"
        f"{'━' * 20}\n"
        f"<b>Сервер и ключ</b>\n\n"
        f"{_format_copyable_fields(key_fields)}"
    )

    sent_count = 0
    bot_token = _get_ops_alert_bot_token()
    for chat_id in chat_ids:
        try:
            await _send_alert_routed_message(
                bot,
                chat_id=chat_id,
                text=message_text,
                bot_token=bot_token,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            sent_count += 1
        except Exception as send_error:
            logger.error(
                "Failed to send issuance recovered notification to chat_id=%s: %s",
                chat_id,
                send_error,
                exc_info=True,
            )
    if sent_count == 0:
        logger.error("Issuance recovered notification was not delivered to any chat. user_id=%s", user_id)


async def notify_admin_of_post_issue_delivery_failure(
    bot: Bot,
    *,
    user_id: int,
    payment_id: str | None,
    host_name: str | None,
    key_id: int | None,
    key_email: str | None,
    error_text: str,
) -> None:
    chat_ids = _get_ops_alert_chat_ids()
    if not chat_ids:
        return

    time_text = datetime.now().strftime("%d.%m.%Y %H:%M")
    fields = [
        ("ID платежа", payment_id or "—"),
        ("ID Telegram", user_id),
        ("Хост", host_name or "—"),
        ("ID ключа", str(key_id) if key_id is not None else "—"),
        ("Email ключа", key_email or "—"),
        ("Ошибка Telegram", error_text),
    ]
    message_text = (
        "<b>🟡 Ключ выдан, но сообщение пользователю не доставлено</b>\n"
        f"Внимание · {html_escape(_runtime_source_label())} · {html_escape(time_text)}\n\n"
        f"{_format_copyable_fields(fields)}\n\n"
        "<b>Что сделать:</b> Свяжитесь с клиентом вручную или дождитесь, пока он разблокирует бота."
    )

    bot_token = _get_ops_alert_bot_token()
    for chat_id in chat_ids:
        try:
            await _send_alert_routed_message(
                bot,
                chat_id=chat_id,
                text=message_text,
                bot_token=bot_token,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as send_error:
            logger.error(
                "Failed to send post-issue delivery failure notification to chat_id=%s: %s",
                chat_id,
                send_error,
                exc_info=True,
            )


async def _show_payment_status_to_user(
    bot: Bot,
    *,
    user_id: int,
    processing_message,
    text: str,
    reply_markup=None,
) -> None:
    status_delivered = False
    if processing_message:
        try:
            await processing_message.edit_text(text, reply_markup=reply_markup)
            status_delivered = True
        except (TelegramBadRequest, TelegramForbiddenError) as edit_error:
            logger.warning(
                "Could not edit payment status message for user_id=%s: %s",
                user_id,
                edit_error,
            )
        except Exception as edit_error:  # noqa: BLE001
            logger.warning(
                "Unexpected error while editing payment status message for user_id=%s: %s",
                user_id,
                edit_error,
                exc_info=True,
            )

    if status_delivered:
        return

    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=reply_markup,
        )
    except (TelegramBadRequest, TelegramForbiddenError) as send_error:
        logger.warning(
            "Could not send payment status message to user_id=%s: %s",
            user_id,
            send_error,
        )
    except Exception as send_error:  # noqa: BLE001
        logger.warning(
            "Unexpected error while sending payment status message to user_id=%s: %s",
            user_id,
            send_error,
            exc_info=True,
        )


async def _create_heleket_payment_request(
    user_id: int, price: float, months: int, host_name: str, state_data: dict
) -> str | None:
    merchant_id = get_setting("heleket_merchant_id")
    api_key = get_setting("heleket_api_key")
    bot_username = get_setting("telegram_bot_username")
    domain = get_setting("domain")

    claim = {"claim_id": state_data.get("referral_discount_claim_id")}
    if not all([merchant_id, api_key, bot_username, domain]):
        await _mark_referral_invoice(claim, status="cancelled", note="Provider request not sent: missing configuration or pending invoice persistence failed.")
        logger.error("Heleket Error: Not all required settings are configured.")
        return None

    redirect_url = f"https://t.me/{bot_username}"
    order_id = str(uuid.uuid4())

    metadata = {
        "user_id": user_id,
        "months": months,
        "price": float(price),
        "referral_discount_claim_id": state_data.get("referral_discount_claim_id"),
        "action": state_data.get("action"),
        "key_id": state_data.get("key_id"),
        "host_name": host_name,
        "plan_id": state_data.get("plan_id"),
        "plan_name": state_data.get("plan_name"),
        "customer_email": state_data.get("customer_email"),
        "payment_method": "Heleket",
        **promotion_metadata(state_data),
        "bot_source": BOT_SOURCE,
        "is_super_key_flow": _is_super_purchase_flow(
            host_name=host_name,
            is_super_key_flow=state_data.get("is_super_key_flow"),
            plan_data={"plan_id": state_data.get("plan_id")},
        ),
    }

    payload = {
        "amount": f"{price:.2f}",
        "currency": "RUB",
        "order_id": order_id,
        "description": json.dumps(metadata),
        "url_return": redirect_url,
        "url_success": redirect_url,
        "url_callback": f"https://{domain}/heleket-webhook",
        "lifetime": 1800,
        "is_payment_multiple": False,
    }

    headers = {
        "merchant": merchant_id,
        "sign": _generate_heleket_signature(json.dumps(payload), api_key),
        "Content-Type": "application/json",
    }

    pending_id = await asyncio.to_thread(
        create_pending_transaction, payment_id=order_id, user_id=user_id, amount_rub=price, metadata=metadata,
    )
    if not pending_id:
        await _mark_referral_invoice(claim, status="cancelled", note="Provider request not sent: missing configuration or pending invoice persistence failed.")
        return None
    await _mark_referral_invoice(claim, order_id, status="uncertain")
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.heleket.com/v1/payment"
            async with session.post(url, json=payload, headers=headers) as response:
                result = await response.json()
                if response.status == 200 and result.get("result", {}).get("url"):
                    await _mark_referral_invoice(claim, order_id)
                    return result["result"]["url"]
                logger.error("Heleket API Error: Status %s, Result: %s", response.status, result)
                return None
    except (aiohttp.ClientError, TimeoutError, ValueError):
        logger.exception("Heleket request failed")
        return None


def _generate_heleket_signature(data, api_key: str) -> str:
    if isinstance(data, dict):
        data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    else:
        data_str = str(data)
    base64_encoded = base64.b64encode(data_str.encode()).decode()
    raw_string = f"{base64_encoded}{api_key}"
    return hashlib.md5(raw_string.encode(), usedforsecurity=False).hexdigest()


async def get_usdt_rub_rate() -> Decimal | None:
    url = "https://api.binance.com/api/v3/ticker/price"
    params = {"symbol": "USDTRUB"}

    try:
        async with aiohttp.ClientSession() as session, session.get(url, params=params) as response:
            response.raise_for_status()
            data = await response.json()
            price_str = data.get("price")
            if price_str:
                logger.info(f"Got USDT RUB: {price_str}")
                return Decimal(price_str)
            logger.error("Can't find 'price' in Binance response.")
            return None
    except Exception as e:
        logger.error(f"Error getting USDT RUB Binance rate: {e}", exc_info=True)
        return None


async def get_ton_usdt_rate() -> Decimal | None:
    url = "https://api.binance.com/api/v3/ticker/price"
    params = {"symbol": "TONUSDT"}

    try:
        async with aiohttp.ClientSession() as session, session.get(url, params=params) as response:
            response.raise_for_status()
            data = await response.json()
            price_str = data.get("price")
            if price_str:
                logger.info(f"Got TON USDT: {price_str}")
                return Decimal(price_str)
            logger.error("Can't find 'price' in Binance response.")
            return None
    except Exception as e:
        logger.error(f"Error getting TON USDT Binance rate: {e}", exc_info=True)
        return None


async def process_successful_payment(bot: Bot, metadata: dict):
    """Run payment issuance once per transaction, even across concurrent dispatchers."""
    payment_id = str(metadata.get("external_payment_id") or metadata.get("payment_id") or "").strip()
    if _payment_shutdown_started:
        if payment_id:
            reset_transaction_to_pending(
                payment_id,
                reason="runtime_shutdown_deferred",
                respect_active_lock=True,
            )
        logger.info("Payment issuance deferred during shutdown: payment_id=%s", payment_id or "unknown")
        return
    if not payment_id:
        await _process_successful_payment_locked(bot, metadata)
        return

    if is_transaction_fully_issued(payment_id):
        logger.info("Idempotency guard: payment_id=%s already issued, skipping", payment_id)
        return

    lock_token = try_acquire_transaction_lock(payment_id)
    if not lock_token:
        logger.info("Idempotency guard: payment_id=%s is already locked or terminal, skipping", payment_id)
        return

    completion = asyncio.get_running_loop().create_future()
    _active_payment_completions.add(completion)
    stop_lock_heartbeat = asyncio.Event()
    heartbeat_task = asyncio.create_task(
        _refresh_payment_lock_until_done(payment_id, lock_token, stop_lock_heartbeat)
    )
    cancelled = False
    try:
        await _process_successful_payment_locked(bot, metadata, issuance_lock_token=lock_token)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        stop_lock_heartbeat.set()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            # Global shutdown cancels the heartbeat as well as its owner.
            # A separately cancelled heartbeat must not hide a completed payment.
            if asyncio.current_task().cancelling():
                cancelled = True
                raise
        except Exception:
            logger.warning("Payment lock heartbeat failed for payment_id=%s", payment_id, exc_info=True)
        finally:
            try:
                if cancelled and not is_transaction_fully_issued(payment_id):
                    # Cancelling an await does not stop the panel's worker thread.
                    # Keep the lease until its TTL so retries cannot overlap it.
                    reset_transaction_to_pending(payment_id, reason="issuance_cancelled_waiting_for_lease")
                    logger.warning("Cancelled payment retains its lease: payment_id=%s", payment_id)
                else:
                    release_transaction_lock(payment_id, lock_token)
            finally:
                if not completion.done():
                    completion.set_result(None)
                _active_payment_completions.discard(completion)


async def _process_successful_payment_locked(
    bot: Bot, metadata: dict, *, issuance_lock_token: str | None = None,
):
    """
    Process successful payment and create/extend VPN key.

    Uses fallback mechanism for new non-super keys:
    - New standard keys: tries fallback hosts if primary host fails (up to 3 attempts)
    - New super keys: use designated host only (no fallback)
    - Key extension: uses original host only (no fallback)

    Security:
    - Validates all metadata fields
    - Logs technical details but shows user-friendly messages
    - Notifies admin on critical failures

    Args:
        bot: Bot instance for sending messages
        metadata: Payment metadata with user_id, plan_id, host_name, etc.
    """
    external_payment_id = metadata.get("external_payment_id")
    if external_payment_id:
        external_payment_id = str(external_payment_id)

    if external_payment_id and is_transaction_fully_issued(external_payment_id):
        logger.info("Idempotency guard: payment_id=%s already issued, skipping", external_payment_id)
        return

    trace_id = metadata.get("trace_id") or "no-trace"
    logger.info(
        "[PAYMENT trace=%s pid=%s stage=start] Processing payment",
        trace_id,
        external_payment_id or "unknown",
    )

    try:
        user_id = int(metadata["user_id"])
        months = int(metadata["months"])
        price = float(metadata["price"])
        action = metadata["action"]
        key_id = int(metadata["key_id"])
        host_name = metadata["host_name"]
        plan_id = int(metadata["plan_id"])
        plan_name = metadata.get("plan_name")
        customer_email = metadata.get("customer_email")
        payment_method = metadata.get("payment_method")
        is_super_flow = _is_super_purchase_flow(
            host_name=host_name,
            is_super_key_flow=metadata.get("is_super_key_flow"),
            plan_data={"plan_id": plan_id},
        )
        subscription_tier = xui_api.SUBSCRIPTION_TIER_SUPER if is_super_flow else xui_api.SUBSCRIPTION_TIER_STANDARD

        chat_id_to_delete = metadata.get("chat_id")
        message_id_to_delete = metadata.get("message_id")

    except (ValueError, TypeError, KeyError) as e:
        # Don't log full metadata - may contain sensitive data
        logger.exception(
            "FATAL: Could not parse payment metadata. User ID: %s",
            metadata.get("user_id", "unknown"),
        )
        parse_reason = f"metadata_parse_failed:{type(e).__name__}"
        _mark_transaction_issuance_failure(
            payment_id=external_payment_id,
            metadata=metadata,
            reason=parse_reason,
            failure_logs={
                "stage": "metadata_parse",
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )
        try:
            notify_user_id = int(metadata.get("user_id") or 0)
        except (TypeError, ValueError):
            notify_user_id = 0
        if notify_user_id > 0:
            await notify_admin_of_issuance_failure(
                bot,
                user_id=notify_user_id,
                payment_id=external_payment_id,
                payment_method=metadata.get("payment_method"),
                host_name=str(metadata.get("host_name") or "—"),
                plan_id=metadata.get("plan_id"),
                plan_name=metadata.get("plan_name"),
                months=metadata.get("months"),
                amount_rub=metadata.get("price"),
                action=metadata.get("action"),
                key_email=metadata.get("customer_email"),
                reason=parse_reason,
                failure_logs={
                    "stage": "metadata_parse",
                    "error_type": type(e).__name__,
                    "error": str(e),
                },
            )
            await _show_payment_status_to_user(
                bot,
                user_id=notify_user_id,
                processing_message=None,
                text=(
                    "⚠️ Оплата получена, но данные заказа временно не разобраны.\n\n"
                    "Платёж сохранён, автовыдача поставлена в повтор. Поддержка уже уведомлена."
                ),
                reply_markup=keyboards.create_support_error_keyboard(),
            )
        if external_payment_id:
            reset_transaction_to_pending(external_payment_id)
        return

    payment_confirmed = bool(metadata.get("payment_confirmed"))
    tx_status = ""
    if external_payment_id:
        tx = get_transaction_by_payment_id(external_payment_id)
        if isinstance(tx, dict):
            tx_status = str(tx.get("status") or "").strip().lower()
        if tx_status != "paid" and not payment_confirmed:
            reason = "payment_not_confirmed"
            logger.error(
                "Payment processing blocked by strict gate: payment_id=%s tx_status=%s payment_confirmed=%s",
                external_payment_id,
                tx_status or "unknown",
                payment_confirmed,
            )
            _mark_transaction_issuance_failure(
                payment_id=external_payment_id,
                metadata=metadata,
                reason=reason,
                failure_logs={
                    "stage": "payment_confirmation_gate",
                    "tx_status": tx_status or "unknown",
                    "payment_confirmed": str(payment_confirmed),
                },
            )
            await notify_admin_of_issuance_failure(
                bot,
                user_id=user_id,
                payment_id=external_payment_id,
                payment_method=payment_method,
                host_name=host_name,
                plan_id=plan_id,
                plan_name=plan_name,
                months=months,
                amount_rub=price,
                action=action,
                key_email=customer_email,
                reason=reason,
                failure_logs={
                    "stage": "payment_confirmation_gate",
                    "tx_status": tx_status or "unknown",
                    "payment_confirmed": str(payment_confirmed),
                },
            )
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "⚠️ Не удалось подтвердить платёж для выдачи ключа.\n\nОплата сохранена, поддержка уже уведомлена."
                ),
                reply_markup=keyboards.create_support_error_keyboard(),
            )
            if external_payment_id:
                reset_transaction_to_pending(external_payment_id)
            return

    user_data = get_user(user_id)
    if not user_data:
        # Defensive recovery: payment metadata can exist even if users row is missing
        # (manual migration, DB inconsistency, legacy import). Create minimal user row.
        logger.warning(
            "Payment processing: user %s not found in users table. Creating fallback record.",
            user_id,
        )
        register_user_if_not_exists(user_id, f"user_{user_id}", None)
        user_data = get_user(user_id)
        if not user_data:
            logger.error("Payment processing aborted: failed to create fallback user record for user_id=%s", user_id)
            await _show_payment_status_to_user(
                bot,
                user_id=user_id,
                processing_message=None,
                text=(
                    "⚠️ Оплата получена, но профиль пользователя временно недоступен.\n\n"
                    "Платёж сохранён, автовыдача поставлена в повтор. Поддержка уже уведомлена."
                ),
                reply_markup=keyboards.create_support_error_keyboard(),
            )
            if external_payment_id:
                reset_transaction_to_pending(external_payment_id)
            return

    if chat_id_to_delete and message_id_to_delete:
        try:
            await bot.delete_message(chat_id=chat_id_to_delete, message_id=message_id_to_delete)
        except TelegramBadRequest as e:
            logger.warning(f"Could not delete payment message: {e}")

    processing_message = None
    try:
        processing_message = await bot.send_message(
            chat_id=user_id, text=("✅ Оплата получена.\nАвтовыдача ключа запущена, обычно это занимает до пары минут.")
        )
    except Exception as e:
        logger.warning(
            "Could not send processing message to user %s. Continue payment flow anyway: %s",
            user_id,
            e,
        )

    try:
        precheck = await _run_purchase_precheck_async(
            host_name,
            is_super_key_flow=is_super_flow,
            plan_data={"plan_id": plan_id, "plan_name": plan_name},
        )
        if not precheck.allowed:
            precheck_reason = f"precheck_blocked:{precheck.reason}"
            is_fatal_precheck = is_fatal_precheck_reason(precheck.reason)
            logger.warning(
                "Post-payment pre-check blocked key issuance for user=%s host=%s reason=%s fatal=%s",
                user_id,
                host_name,
                precheck.reason,
                is_fatal_precheck,
            )
            _mark_transaction_issuance_failure(
                payment_id=external_payment_id,
                metadata=metadata,
                reason=precheck_reason,
                failure_logs={
                    "stage": "precheck",
                    "precheck_reason": str(precheck.reason or "unknown"),
                    "user_message": str(precheck.user_message or ""),
                },
                fatal=is_fatal_precheck,
            )
            await notify_admin_of_issuance_failure(
                bot,
                user_id=user_id,
                payment_id=external_payment_id,
                payment_method=payment_method,
                host_name=host_name,
                plan_id=plan_id,
                plan_name=plan_name,
                months=months,
                amount_rub=price,
                action=action,
                key_email=customer_email,
                reason=precheck_reason,
                failure_logs={
                    "stage": "precheck",
                    "precheck_reason": str(precheck.reason or "unknown"),
                    "user_message": str(precheck.user_message or ""),
                },
            )
            await _show_payment_status_to_user(
                bot,
                user_id=user_id,
                processing_message=processing_message,
                text=f"🔧 {precheck.user_message}",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            if external_payment_id and not is_fatal_precheck:
                reset_transaction_to_pending(external_payment_id)
            return

        email = ""
        key_display_name = "Новый ключ" if str(action).strip().lower() == "new" else "Выданный после продления"
        attempted_new_emails: list[str] = []
        existing_expiry: datetime | None = None
        original_extend_email: str | None = None
        extend_client_uuid: str | None = None
        extend_panel_sub_id: str | None = None
        missing_extend_key_fallback = False
        missing_extend_key_id: int | None = None
        trial_super_replacement = False
        trial_super_replaced_key_id: int | None = None
        trial_super_replaced_email: str | None = None
        paid_at = _resolve_paid_at_from_metadata(metadata)
        # Route to Blitz only when the selected host is explicitly the Blitz sentinel host.
        # This keeps super-key (fixed X-UI host) and regular X-UI purchases on their chosen host.
        use_blitz = host_name == blitz_api.BLITZ_HOST_NAME
        standard_birth_scope = (
            bool(external_payment_id) and action == "new" and not is_super_flow and host_name == "MainPanel"
            and _get_standard_key_host_name() == host_name
            and metadata.get("original_action") != "extend"
        )
        standard_birth_saved_intent = standard_birth_scope and (
            "issuance_target_email" in metadata or "standard_birth_attempted" in metadata
        )
        standard_birth_namespace_verified = False

        if action == "new":
            key_number = get_next_key_number(user_id)
            if standard_birth_saved_intent:
                email = str(metadata.get("issuance_target_email") or "").strip()
                attempted_new_emails.append(email)
            elif standard_birth_scope:
                for offset in range(5):
                    candidate_email = f"user{user_id}-key{key_number + offset}@vpn.bot"
                    attempted_new_emails.append(candidate_email)
                    if get_key_by_email(candidate_email):
                        continue
                    if await xui_api.standard_birth_namespace_is_empty(host_name, candidate_email) is True:
                        email = candidate_email
                        standard_birth_namespace_verified = True
                        break
            else:
                email = f"user{user_id}-key{key_number}@vpn.bot"
                attempted_new_emails.append(email)
        elif action == "extend":
            key_data = get_key_by_id(key_id)
            if metadata.get("mobile_strict_renewal") is True:
                from shop_bot.mobile_renewal import strict_target_matches
                if not strict_target_matches(metadata, key_data, user_id):
                    reason = "mobile_renewal_target_changed"
                    _mark_transaction_issuance_failure(
                        payment_id=external_payment_id, metadata=metadata, reason=reason,
                        failure_logs={"stage": "mobile_renewal_target_guard"}, fatal=True,
                    )
                    await notify_admin_of_issuance_failure(
                        bot, user_id=user_id, payment_id=external_payment_id,
                        payment_method=payment_method, host_name=host_name, plan_id=plan_id,
                        plan_name=plan_name, months=months, amount_rub=price, action=action,
                        key_email=None, reason=reason,
                        failure_logs={"stage": "mobile_renewal_target_guard"},
                    )
                    await _show_payment_status_to_user(
                        bot, user_id=user_id, processing_message=processing_message,
                        text="Оплата получена, но выбранный ключ изменился. Поддержка проверит продление.",
                        reply_markup=keyboards.create_support_error_keyboard(),
                    )
                    return
            if not key_data or key_data["user_id"] != user_id:
                missing_extend_key_fallback = True
                missing_extend_key_id = key_id
                metadata["original_action"] = "extend"
                metadata["missing_extend_key_id"] = missing_extend_key_id
                metadata["action"] = "new"
                action = "new"
                key_number = get_next_key_number(user_id)
                email = f"user{user_id}-key{key_number}@vpn.bot"
                attempted_new_emails.append(email)
                key_display_name = "Новый ключ"
                logger.warning(
                    "Extend target key not found; issuing replacement key. payment_id=%s user_id=%s missing_key_id=%s host=%s",
                    external_payment_id,
                    user_id,
                    missing_extend_key_id,
                    host_name,
                )
            else:
                email = key_data["key_email"]
                original_extend_email = str(email or "").strip()
                extend_client_uuid = str(key_data.get("xui_client_uuid") or "").strip() or None
                extend_panel_sub_id = str(key_data.get("panel_sub_id") or "").strip() or None
                key_display_name = _resolve_extend_key_display_name(original_email=original_extend_email)
                use_blitz = blitz_api.is_blitz_key(key_data)
                if not use_blitz and _is_trial_super_key_email(original_extend_email):
                    if not is_super_flow:
                        logger.warning(
                            "Trial-super paid extend forced into super replacement flow. "
                            "payment_id=%s user_id=%s old_key_id=%s old_email=%s host=%s plan_id=%s plan_name=%s",
                            external_payment_id,
                            user_id,
                            key_id,
                            original_extend_email,
                            host_name,
                            plan_id,
                            plan_name,
                        )
                    is_super_flow = True
                    subscription_tier = xui_api.SUBSCRIPTION_TIER_SUPER
                    metadata["is_super_key_flow"] = True
                    trial_super_replacement = True
                    trial_super_replaced_key_id = key_id
                    trial_super_replaced_email = original_extend_email
                    metadata["original_action"] = "extend"
                    metadata["trial_super_replaced_key_id"] = trial_super_replaced_key_id
                    metadata["trial_super_replaced_email"] = trial_super_replaced_email
                    metadata["action"] = "new"
                    action = "new"
                    key_number = get_next_key_number(user_id)
                    email = f"user{user_id}-key{key_number}@vpn.bot"
                    attempted_new_emails.append(email)
                    extend_client_uuid = None
                    extend_panel_sub_id = None
                    key_display_name = "Выданный после триала"
                    logger.warning(
                        "Trial-super paid extend routed to replacement key. payment_id=%s user_id=%s old_key_id=%s old_email=%s new_email=%s host=%s",
                        external_payment_id,
                        user_id,
                        trial_super_replaced_key_id,
                        trial_super_replaced_email,
                        email,
                        host_name,
                    )
                else:
                    existing_expiry = subscription_dates.parse_datetime(key_data.get("expiry_date"))

        purchase_window = subscription_dates.build_purchase_window(
            months=months,
            paid_at=paid_at,
            current_expiry=existing_expiry if action == "extend" else None,
        )
        stored_target_expiry_ms = _coerce_positive_int(metadata.get("issuance_target_expiry_ms"))
        if stored_target_expiry_ms:
            stored_expiry_at = _datetime_from_timestamp_ms(stored_target_expiry_ms)
            purchase_window = subscription_dates.PurchaseWindow(
                paid_at=purchase_window.paid_at,
                anchor_at=purchase_window.anchor_at,
                expiry_at=stored_expiry_at,
                panel_days=subscription_dates.full_days_between(purchase_window.anchor_at, stored_expiry_at),
            )
        target_expiry_ms = int(purchase_window.expiry_at.timestamp() * 1000)
        whitelist_cycle_anchor_at = subscription_dates.resolve_recurring_cycle_anchor(
            paid_at=purchase_window.paid_at,
            current_expiry=(
                subscription_dates.parse_datetime(key_data.get("expiry_date"))
                if action == "extend" and key_data
                else None
            ),
            created_at=(
                subscription_dates.parse_datetime(
                    key_data.get("whitelist_cycle_anchor_at") or key_data.get("created_date")
                )
                if action == "extend" and key_data
                else None
            ),
        )
        whitelist_cycle_anchor_ms = int(whitelist_cycle_anchor_at.timestamp() * 1000)
        if standard_birth_scope:
            stored_cycle_anchor_ms = _coerce_positive_int(metadata.get("issuance_whitelist_cycle_anchor_ms"))
            if stored_cycle_anchor_ms:
                whitelist_cycle_anchor_ms = stored_cycle_anchor_ms
                whitelist_cycle_anchor_at = _datetime_from_timestamp_ms(stored_cycle_anchor_ms)
        white_traffic_limit_bytes = xui_api.whitelist_traffic_limit_bytes_for_key({
            **(key_data if action == "extend" and key_data else {}),
            "subscription_tier": subscription_tier,
            "key_email": email,
        })
        days_to_add = purchase_window.panel_days
        _persist_payment_target_for_retry(
            external_payment_id,
            metadata,
            action=str(action),
            key_id=key_id,
            target_expiry_ms=target_expiry_ms,
            target_expiry_at=purchase_window.expiry_at,
            email=email,
        )

        if use_blitz:
            # Blitz API accepts only expiration_days (TTL from now), not absolute timestamp.
            # MUST use total days from now to expiry_at, NOT panel_days (anchor→expiry).
            # Otherwise extend cuts the month: user with 10d left + 1mo pays → gets 30d instead of 40d.
            days_to_add = subscription_dates.panel_days_until(purchase_window.expiry_at)
            logger.info(
                "Blitz payment: action=%s anchor=%s expiry_at=%s panel_days=%s → expiry_days=%s",
                action,
                purchase_window.anchor_at.isoformat() if purchase_window.anchor_at else None,
                purchase_window.expiry_at.isoformat(),
                purchase_window.panel_days,
                days_to_add,
            )

        standard_primary_host = _get_standard_key_host_name()
        strict_standard_host = (
            action == "new" and not is_super_flow and str(host_name or "").strip() == standard_primary_host
        )

        result: dict | None = None
        recovered_from_existing = False
        recovered_applied_key = _recover_payment_applied_key_from_metadata(
            metadata,
            user_id=user_id,
            target_expiry_ms=target_expiry_ms,
            subscription_tier=subscription_tier,
            readonly_standard_birth=standard_birth_scope,
        )
        if trial_super_replacement and recovered_applied_key and _is_trial_super_key_email(
            recovered_applied_key.get("key_email")
        ):
            logger.warning(
                "Ignoring old trial-super applied key during paid replacement retry. payment_id=%s user_id=%s key_id=%s",
                external_payment_id,
                user_id,
                recovered_applied_key.get("key_id"),
            )
            recovered_applied_key = None
        if recovered_applied_key:
            result = _result_from_db_key(recovered_applied_key, target_expiry_ms=target_expiry_ms)
            key_id = int(recovered_applied_key["key_id"])
            email = str(recovered_applied_key.get("key_email") or email)
            recovered_from_existing = True
            logger.warning(
                "Recovered payment_id=%s from already-applied DB key_id=%s action=%s",
                external_payment_id,
                key_id,
                action,
            )

        if result:
            pass
        elif use_blitz:
            # ---- Blitz (Hysteria 2) path ----
            blitz_username = _generate_blitz_username()
            blitz_result = await blitz_api.create_user(username=blitz_username, expiry_days=days_to_add, unlimited=True)
            if not blitz_result:
                result = None
            else:
                connection_uri = await blitz_api.get_connection_uri(blitz_username)
                if not connection_uri:
                    result = None
                else:
                    result = {
                        "client_uuid": blitz_username,
                        "email": blitz_username,
                        "expiry_timestamp_ms": target_expiry_ms,
                        "connection_string": connection_uri,
                        "host_name": blitz_api.BLITZ_HOST_NAME,
                    }
        elif action == "new":
            # ---- Fast host health gate — skip 60s+ of dead XUI calls ----
            reachable, _unavail_msg = _is_host_reachable(host_name)
            if not reachable:
                logger.warning(
                    "Host %s unavailable at payment processing, deferring txn %s for user %s",
                    host_name,
                    external_payment_id,
                    user_id,
                )
                _mark_transaction_issuance_failure(
                    payment_id=external_payment_id,
                    metadata=metadata,
                    reason="host_unavailable_at_issuance",
                    failure_logs={"stage": "host_health_gate", "host": str(host_name)},
                )
                error_msg = (
                    "⏳ Сервер для вашего ключа временно на обслуживании.\n\n"
                    "Ваш платёж сохранён. Ключ будет выдан автоматически, как только сервер восстановится.\n\n"
                    "Если ключ не появится в течение часа — обратитесь в поддержку."
                )
                await _show_payment_status_to_user(
                    bot,
                    user_id=user_id,
                    processing_message=processing_message,
                    text=error_msg,
                    reply_markup=keyboards.create_support_error_keyboard(),
                )
                await notify_admin_of_issuance_failure(
                    bot,
                    user_id=user_id,
                    payment_id=external_payment_id,
                    payment_method=payment_method,
                    host_name=host_name,
                    plan_id=plan_id,
                    plan_name=plan_name,
                    months=months,
                    amount_rub=price,
                    action=action,
                    key_email=email,
                    reason="host_unavailable_at_issuance",
                    failure_logs={"stage": "host_health_gate", "host": str(host_name)},
                )
                if external_payment_id:
                    reset_transaction_to_pending(external_payment_id)
                return

            # 1) Payment-id snapshot reconciliation (idempotent recovery).
            #    Prefer UUID-based lookup (stable across email renames),
            #    fallback to email-based lookup.
            panel_snapshot = (
                None if trial_super_replacement or standard_birth_scope
                else _extract_panel_snapshot_from_metadata(metadata)
            )
            if panel_snapshot:
                snapshot_host = str(panel_snapshot.get("host_name") or "").strip()
                snapshot_email = str(panel_snapshot.get("email") or "").strip()
                snapshot_uuid = str(panel_snapshot.get("client_uuid") or "").strip()
                if snapshot_host and snapshot_uuid:
                    result = await xui_api.get_existing_key_by_uuid_on_host(
                        host_name=snapshot_host,
                        client_uuid=snapshot_uuid,
                        target_expiry_ms=target_expiry_ms,
                        subscription_tier=subscription_tier,
                    )
                    if result:
                        recovered_from_existing = True
                        logger.warning(
                            "Recovered payment_id=%s via panel UUID host=%s uuid=%s",
                            external_payment_id,
                            snapshot_host,
                            snapshot_uuid,
                        )
                if not result and snapshot_host and snapshot_email:
                    result = await xui_api.get_existing_key_by_email_on_host(
                        host_name=snapshot_host,
                        email=snapshot_email,
                        target_expiry_ms=target_expiry_ms,
                        subscription_tier=subscription_tier,
                    )
                    if result:
                        recovered_from_existing = True
                        logger.warning(
                            "Recovered payment_id=%s using stored panel snapshot host=%s email=%s",
                            external_payment_id,
                            snapshot_host,
                            snapshot_email,
                        )

            # 2) Existing panel client reconciliation by deterministic email.
            if not result and not standard_birth_scope:
                if is_super_flow:
                    result = await xui_api.get_existing_key_by_email_on_host(
                        host_name=host_name,
                        email=email,
                        target_expiry_ms=target_expiry_ms,
                        subscription_tier=subscription_tier,
                    )
                elif strict_standard_host:
                    result = await xui_api.get_existing_key_by_email_on_host(
                        host_name=host_name,
                        email=email,
                        target_expiry_ms=target_expiry_ms,
                        subscription_tier=subscription_tier,
                    )
                else:
                    result = await xui_api.find_existing_key_by_email_with_fallback(
                        host_name=host_name,
                        email=email,
                        target_expiry_ms=target_expiry_ms,
                        subscription_tier=subscription_tier,
                        max_hosts=3,
                    )
                if result:
                    recovered_from_existing = True
                    logger.warning(
                        "Reconciled payment_id=%s with existing panel client email=%s host=%s",
                        external_payment_id,
                        result.get("email"),
                        result.get("host_name"),
                    )

            standard_multi_host_attempted = False
            if result and strict_standard_host and not standard_birth_scope:
                standard_multi_host_attempted = True
                result = await xui_api.create_or_update_standard_subscription_on_hosts(
                    host_name=host_name,
                    email=email,
                    days_to_add=days_to_add,
                    months=months,
                    target_expiry_ms=target_expiry_ms,
                    whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
                    max_attempts_per_host=PURCHASE_ISSUE_MAX_ATTEMPTS,
                    panel_call_timeout_seconds=PURCHASE_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
                )

            # 3) Standard creation path.
            async def _issue_new_key_with_email(target_email: str) -> dict | None:
                nonlocal standard_multi_host_attempted
                if is_super_flow:
                    # Super-key must be issued only on its designated host (no fallback).
                    return await xui_api.create_or_update_key_on_host(
                        host_name=host_name,
                        email=target_email,
                        days_to_add=days_to_add,
                        months=months,
                        target_expiry_ms=target_expiry_ms,
                        whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
                        include_white_inbounds=True,
                        require_white_inbound=True,
                        panel_call_timeout_seconds=PURCHASE_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
                        max_attempts=PURCHASE_ISSUE_MAX_ATTEMPTS,
                    )
                if strict_standard_host:
                    standard_multi_host_attempted = True
                    return await xui_api.create_or_update_standard_subscription_on_hosts(
                        host_name=host_name,
                        email=target_email,
                        days_to_add=days_to_add,
                        months=months,
                        target_expiry_ms=target_expiry_ms,
                        whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
                        max_attempts_per_host=PURCHASE_ISSUE_MAX_ATTEMPTS,
                        panel_call_timeout_seconds=PURCHASE_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
                    )
                # ---- X-UI path: new key with fallback ----
                return await xui_api.create_or_update_key_with_fallback(
                    host_name=host_name,
                    email=target_email,
                    days_to_add=days_to_add,
                    max_retries=3,
                    months=months,
                    target_expiry_ms=target_expiry_ms,
                    include_white_inbounds=True,
                    white_traffic_limit_bytes=white_traffic_limit_bytes,
                    whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
                    retry_attempts_per_host=PURCHASE_ISSUE_MAX_ATTEMPTS,
                    panel_call_timeout_seconds=PURCHASE_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
                )

            async def _build_available_purchase_emails(primary_email: str) -> list[str]:
                candidate_emails: list[str] = []
                seen_candidate_emails: set[str] = set()
                for offset in range(0, 32):
                    candidate_email = (
                        primary_email if offset == 0 else f"user{user_id}-key{key_number + offset}@vpn.bot"
                    )
                    normalized_candidate_email = str(candidate_email or "").strip().lower()
                    if not normalized_candidate_email or normalized_candidate_email in seen_candidate_emails:
                        continue
                    seen_candidate_emails.add(normalized_candidate_email)
                    if get_key_by_email(candidate_email):
                        logger.warning(
                            "Purchase email DB preflight skipped occupied candidate user=%s host=%s email=%s",
                            user_id,
                            host_name,
                            candidate_email,
                        )
                        continue
                    candidate_emails.append(candidate_email)
                    if len(candidate_emails) >= 10:
                        break

                available_candidate_emails = candidate_emails
                panel_preflight_completed = False
                if candidate_emails and not use_blitz:
                    try:
                        available_candidate_emails, conflicts_by_email = await asyncio.to_thread(
                            xui_api.filter_available_subscription_base_emails_on_host,
                            host_name,
                            candidate_emails,
                            include_white_inbounds=True,
                            require_white_inbound=True,
                        )
                        panel_preflight_completed = True
                        if conflicts_by_email:
                            logger.warning(
                                "Purchase email panel preflight skipped occupied candidates user=%s host=%s conflicts=%s",
                                user_id,
                                host_name,
                                ",".join(list(conflicts_by_email)[:5]),
                            )
                    except Exception:
                        logger.exception(
                            "Purchase email panel preflight failed user=%s host=%s; falling back to DB-only candidates",
                            user_id,
                            host_name,
                        )
                        available_candidate_emails = candidate_emails
                if available_candidate_emails and available_candidate_emails[0] != primary_email:
                    logger.warning(
                        "Purchase email preflight selected alternate first email user=%s host=%s skipped_email=%s selected_email=%s",
                        user_id,
                        host_name,
                        primary_email,
                        available_candidate_emails[0],
                    )
                return _purchase_email_candidates_after_panel_preflight(
                    primary_email,
                    candidate_emails,
                    available_candidate_emails,
                    panel_preflight_completed=panel_preflight_completed,
                )

            # 4) Issue only after panel email preflight. The local DB can lag behind
            # MainPanel multi-inbound state, so the deterministic email may already
            # be occupied even when get_next_key_number() says it is free.
            if not result and standard_birth_scope:
                async def _probe_claimed_standard_birth() -> dict | None:
                    nonlocal whitelist_cycle_anchor_ms, whitelist_cycle_anchor_at
                    if (not re.fullmatch(rf"user{user_id}-key[1-9]\d*@vpn\.bot", email)
                            or not _coerce_positive_int(metadata.get("issuance_whitelist_cycle_anchor_ms"))):
                        return None
                    observed = await xui_api.probe_pending_standard_subscription(
                        host_name, email, target_expiry_ms=target_expiry_ms,
                        whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
                    )
                    if not _paid_standard_birth_probe_result_is_safe(
                        observed, email=email, target_expiry_ms=target_expiry_ms,
                    ):
                        def persist_recovery(journal: dict) -> bool:
                            if not issuance_lock_token or not refresh_transaction_lock(external_payment_id, issuance_lock_token):
                                return False
                            updated = dict(metadata, standard_birth_recovery=journal,
                                issuance_whitelist_cycle_anchor_ms=journal["identity"]["anchor_ms"])
                            if not update_transaction_metadata(
                                external_payment_id, updated, statuses=("processing", "pending", "paid"),
                            ):
                                return False
                            metadata.clear()
                            metadata.update(updated)
                            return True

                        observed = await standard_birth_recovery.resume_pending_standard_birth(
                            host_name, email, target_expiry_ms=target_expiry_ms,
                            whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
                            journal=metadata.get("standard_birth_recovery"), persist=persist_recovery,
                        )
                    if not _paid_standard_birth_probe_result_is_safe(
                        observed, email=email, target_expiry_ms=target_expiry_ms,
                    ):
                        return None
                    whitelist_cycle_anchor_ms = int(metadata["issuance_whitelist_cycle_anchor_ms"])
                    whitelist_cycle_anchor_at = _datetime_from_timestamp_ms(whitelist_cycle_anchor_ms)
                    return observed

                if metadata.get("standard_birth_attempted") is True:
                    result = await _probe_claimed_standard_birth()
                    recovered_from_existing = bool(result)
                elif not metadata.get("standard_birth_attempted"):
                    # Only a fresh, unoccupied namespace may receive its first
                    # write. An uncertain write keeps this alias for GET recovery.
                    namespace_empty = (
                        standard_birth_namespace_verified or await xui_api.standard_birth_namespace_is_empty(host_name, email)
                        if re.fullmatch(rf"user{user_id}-key[1-9]\d*@vpn\.bot", email) else False
                    )
                    if namespace_empty is True and _claim_paid_standard_birth(
                        external_payment_id, metadata, user_id=user_id, email=email,
                        target_expiry_ms=target_expiry_ms, whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
                    ):
                        result = await _issue_new_key_with_email(email)
                        if not result:
                            result = await _probe_claimed_standard_birth()
                            recovered_from_existing = bool(result)
            elif not result:
                available_purchase_emails = await _build_available_purchase_emails(email)
                for candidate_email in available_purchase_emails[:5]:
                    if candidate_email not in attempted_new_emails:
                        attempted_new_emails.append(candidate_email)
                    logger.warning(
                        "Trying new-key issuance for user=%s host=%s email=%s selected_email=%s",
                        user_id,
                        host_name,
                        email,
                        candidate_email,
                    )
                    email = candidate_email
                    result = await _issue_new_key_with_email(candidate_email)
                    if result:
                        logger.warning(
                            "Recovered new-key issuance for user=%s via alternate email=%s",
                            user_id,
                            candidate_email,
                        )
                        break
        else:
            # ---- X-UI path: extension, no fallback ----
            # Fast host health gate — skip dead XUI calls for extensions too.
            reachable, _unavail_msg = _is_host_reachable(host_name)
            if not reachable:
                logger.warning(
                    "Host %s unavailable at extend processing, deferring txn %s for user %s",
                    host_name,
                    external_payment_id,
                    user_id,
                )
                _mark_transaction_issuance_failure(
                    payment_id=external_payment_id,
                    metadata=metadata,
                    reason="host_unavailable_at_extension",
                    failure_logs={"stage": "host_health_gate_extend", "host": str(host_name)},
                )
                error_msg = (
                    "⏳ Сервер для вашего ключа временно на обслуживании.\n\n"
                    "Ваш платёж сохранён. Продление будет выполнено автоматически, как только сервер восстановится.\n\n"
                    "Если ключ не обновится в течение часа — обратитесь в поддержку."
                )
                await _show_payment_status_to_user(
                    bot,
                    user_id=user_id,
                    processing_message=processing_message,
                    text=error_msg,
                    reply_markup=keyboards.create_support_error_keyboard(),
                )
                await notify_admin_of_issuance_failure(
                    bot,
                    user_id=user_id,
                    payment_id=external_payment_id,
                    payment_method=payment_method,
                    host_name=host_name,
                    plan_id=plan_id,
                    plan_name=plan_name,
                    months=months,
                    amount_rub=price,
                    action=action,
                    key_email=email,
                    reason="host_unavailable_at_extension",
                    failure_logs={"stage": "host_health_gate_extend", "host": str(host_name)},
                )
                if external_payment_id:
                    reset_transaction_to_pending(external_payment_id)
                return
            result = await xui_api.create_or_update_key_on_host(
                host_name=host_name,
                email=email,
                days_to_add=days_to_add,
                months=months,
                target_expiry_ms=target_expiry_ms,
                target_sub_id=extend_panel_sub_id,
                target_client_uuid=extend_client_uuid,
                white_traffic_limit_bytes=white_traffic_limit_bytes,
                reset_exhausted_limited_traffic=True,
                include_white_inbounds=True,
                require_white_inbound=True,
                whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
                panel_call_timeout_seconds=PURCHASE_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
                max_attempts=min(2, PURCHASE_ISSUE_MAX_ATTEMPTS),
            )
            if not result:
                result = await _reconcile_extension_after_panel_error(
                    host_name=host_name,
                    email=email,
                    client_uuid=extend_client_uuid,
                    target_expiry_ms=target_expiry_ms,
                    subscription_tier=subscription_tier,
                )
                if result:
                    recovered_from_existing = True

        if not result:
            logger.error(
                "Failed to create key after all retries. User: %s, Host: %s, Action: %s",
                user_id,
                host_name,
                action,
            )
            failure_reason = "key_creation_failed"
            if action == "new" and attempted_new_emails:
                failure_reason = f"key_creation_failed:attempted_emails={','.join(attempted_new_emails)}"
            _mark_transaction_issuance_failure(
                payment_id=external_payment_id,
                metadata=metadata,
                reason=failure_reason,
                failure_logs={
                    "stage": "key_issue",
                    "host": str(host_name),
                    "action": str(action),
                    "is_super_flow": str(is_super_flow),
                    "use_blitz": str(use_blitz),
                    "attempted_emails": ",".join(attempted_new_emails),
                    "missing_extend_key_fallback": str(missing_extend_key_fallback),
                    "missing_extend_key_id": str(missing_extend_key_id or ""),
                },
            )
            error_msg = (
                "❌ К сожалению, сейчас мы не можем выдать ваш ключ из-за технических проблем.\n\n"
                "Ваш платёж сохранён в системе. Мы автоматически выдадим ключ, как только проблема будет решена.\n\n"
                "Пожалуйста, обратитесь в поддержку для уточнения деталей."
            )
            await _show_payment_status_to_user(
                bot,
                user_id=user_id,
                processing_message=processing_message,
                text=error_msg,
                reply_markup=keyboards.create_support_error_keyboard(),
            )
            await notify_admin_of_issuance_failure(
                bot,
                user_id=user_id,
                payment_id=external_payment_id,
                payment_method=payment_method,
                host_name=host_name,
                plan_id=plan_id,
                plan_name=plan_name,
                months=months,
                amount_rub=price,
                action=action,
                key_email=email,
                reason=failure_reason,
                failure_logs={
                    "stage": "key_issue",
                    "host": str(host_name),
                    "action": str(action),
                    "is_super_flow": str(is_super_flow),
                    "use_blitz": str(use_blitz),
                    "attempted_emails": ",".join(attempted_new_emails),
                    "missing_extend_key_fallback": str(missing_extend_key_fallback),
                    "missing_extend_key_id": str(missing_extend_key_id or ""),
                },
            )
            if external_payment_id:
                reset_transaction_to_pending(external_payment_id)
            return

        actual_host_name = result.get("host_name", host_name)

        if external_payment_id and result.get("client_uuid"):
            update_transaction_metadata_field(
                external_payment_id,
                "panel_client_uuid",
                result["client_uuid"],
                statuses=("processing", "pending"),
            )
            update_transaction_metadata_field(
                external_payment_id,
                "panel_email",
                result.get("email", ""),
                statuses=("processing", "pending"),
            )
            update_transaction_metadata_field(
                external_payment_id,
                "panel_host_name",
                actual_host_name,
                statuses=("processing", "pending"),
            )

        issued_key_value = _pick_issued_key_value(
            connection_string=result.get("connection_string"),
            subscription_link=result.get("subscription_link"),
            sub_id=result.get("sub_id"),
            subscription_tier=subscription_tier,
        )
        if not issued_key_value:
            logger.error(
                "Empty connection string/subscription link for payment_id=%s host=%s email=%s",
                external_payment_id,
                actual_host_name,
                email,
            )
            _mark_transaction_issuance_failure(
                payment_id=external_payment_id,
                metadata=metadata,
                reason="empty_connection_string",
                failure_logs={"stage": "connection_string_validation", "host": str(actual_host_name)},
            )
            await notify_admin_of_issuance_failure(
                bot,
                user_id=user_id,
                payment_id=external_payment_id,
                payment_method=payment_method,
                host_name=actual_host_name,
                plan_id=plan_id,
                plan_name=plan_name,
                months=months,
                amount_rub=price,
                action=action,
                key_email=email,
                reason="empty_connection_string",
                failure_logs={"stage": "connection_string_validation", "host": str(actual_host_name)},
            )
            await _show_payment_status_to_user(
                bot,
                user_id=user_id,
                processing_message=processing_message,
                text=(
                    "⚠️ Оплата получена, но панель вернула пустую ссылку ключа.\n\n"
                    "Платёж сохранён, автовыдача поставлена в повтор. Поддержка уже уведомлена."
                ),
                reply_markup=keyboards.create_support_error_keyboard(),
            )
            if external_payment_id:
                reset_transaction_to_pending(external_payment_id, reason="empty_connection_string")
            return

        _persist_panel_snapshot_for_payment(
            external_payment_id,
            metadata,
            result,
            issued_key_value=issued_key_value,
        )

        if action == "extend" and not use_blitz:
            desired_email = _resolve_extend_email_target(
                user_id=user_id,
                key_id=key_id,
                original_email=original_extend_email or email,
            )
            if desired_email:
                available_email = _resolve_available_extend_email(
                    user_id=user_id,
                    key_id=key_id,
                    current_key_id=key_id,
                )
                if not available_email:
                    logger.warning(
                        "Extend key rename skipped: no free email candidate user_id=%s key_id=%s",
                        user_id,
                        key_id,
                    )
                elif str(available_email).strip().lower() != str(email or "").strip().lower():
                    renamed = await xui_api.rename_client_email_on_host(
                        host_name=actual_host_name,
                        old_email=str(email or ""),
                        new_email=available_email,
                        client_uuid=str(result.get("client_uuid") or ""),
                        max_attempts=2,
                    )
                    if renamed:
                        logger.info(
                            "Extend key rename applied user_id=%s key_id=%s host=%s old_email=%s new_email=%s",
                            user_id,
                            key_id,
                            actual_host_name,
                            email,
                            available_email,
                        )
                        email = available_email
                        result["email"] = available_email
                    else:
                        logger.warning(
                            "Extend key rename failed user_id=%s key_id=%s host=%s old_email=%s desired_email=%s",
                            user_id,
                            key_id,
                            actual_host_name,
                            email,
                            available_email,
                        )

        if action == "new":
            existing_key = get_key_by_email(str(result.get("email") or ""))
            if existing_key and int(existing_key.get("user_id") or 0) == user_id:
                key_id = int(existing_key["key_id"])
                update_key_host_and_uuid(
                    key_id,
                    actual_host_name,
                    str(result.get("client_uuid") or ""),
                    target_expiry_ms,
                    issued_key_value=issued_key_value,
                    panel_sub_id=result.get("sub_id"),
                    subscription_payload=xui_api.subscription_payload_from_result(
                        result,
                        subscription_tier=subscription_tier,
                    ),
                    subscription_tier=subscription_tier,
                )
                logger.info(
                    "Reused existing DB key for payment_id=%s user=%s key_id=%s email=%s recovered=%s",
                    external_payment_id,
                    user_id,
                    key_id,
                    result.get("email"),
                    recovered_from_existing,
                )
            else:
                key_id = add_new_key(
                    user_id,
                    actual_host_name,  # Use actual host (may differ from requested after fallback)
                    result["client_uuid"],
                    result["email"],
                    target_expiry_ms,
                    issued_key_value=issued_key_value,
                    created_date=purchase_window.paid_at,
                    whitelist_cycle_anchor_at=whitelist_cycle_anchor_at,
                    panel_sub_id=result.get("sub_id"),
                    subscription_payload=xui_api.subscription_payload_from_result(
                        result,
                        subscription_tier=subscription_tier,
                    ),
                    subscription_tier=subscription_tier,
                )
            if not key_id:
                watchdog_reason = (
                    "DB_SAVE_FAIL:"
                    f" host={actual_host_name}"
                    f" uuid={result.get('client_uuid')}"
                    f" email={result.get('email')}"
                )
                _mark_transaction_issuance_failure(
                    payment_id=external_payment_id,
                    metadata=metadata,
                    reason=watchdog_reason,
                    failure_logs={
                        "stage": "db_save",
                        "host": str(actual_host_name),
                        "uuid": str(result.get("client_uuid") or ""),
                        "email": str(result.get("email") or ""),
                    },
                )
                logger.error(
                    "PAYMENT DB_SAVE_FAIL: payment_id=%s user_id=%s %s",
                    external_payment_id,
                    user_id,
                    watchdog_reason,
                )
                await notify_admin_of_issuance_failure(
                    bot,
                    user_id=user_id,
                    payment_id=external_payment_id,
                    payment_method=payment_method,
                    host_name=actual_host_name,
                    plan_id=plan_id,
                    plan_name=plan_name,
                    months=months,
                    amount_rub=price,
                    action=action,
                    key_email=result.get("email"),
                    reason=watchdog_reason,
                    failure_logs={
                        "stage": "db_save",
                        "host": str(actual_host_name),
                        "uuid": str(result.get("client_uuid") or ""),
                        "email": str(result.get("email") or ""),
                    },
                )
                await _show_payment_status_to_user(
                    bot,
                    user_id=user_id,
                    processing_message=processing_message,
                    text=(
                        "❌ Платёж получен, но ключ не сохранился в БД.\n\n"
                        "Платёж сохранён, автовыдача поставлена в повтор. Поддержка уже уведомлена."
                    ),
                    reply_markup=keyboards.create_support_error_keyboard(),
                )
                if external_payment_id:
                    reset_transaction_to_pending(external_payment_id)
                return
            _persist_payment_key_applied_for_retry(
                external_payment_id,
                metadata,
                key_id=int(key_id),
                result=result,
                action=str(action),
                target_expiry_ms=target_expiry_ms,
                issued_key_value=issued_key_value,
            )
            if (
                trial_super_replacement
                and trial_super_replaced_key_id
                and int(trial_super_replaced_key_id) != int(key_id)
            ):
                archive_key(
                    int(trial_super_replaced_key_id),
                    replaced_due_to_incident=False,
                    migration_reason="trial_super_paid_replacement",
                )
                logger.info(
                    "Archived trial-super key after paid replacement. payment_id=%s user_id=%s old_key_id=%s new_key_id=%s",
                    external_payment_id,
                    user_id,
                    trial_super_replaced_key_id,
                    key_id,
                )
        elif action == "extend":
            key_updated = update_key_info(
                key_id,
                result["client_uuid"],
                target_expiry_ms,
                issued_key_value=issued_key_value,
                new_key_email=str(result.get("email") or email or ""),
                panel_sub_id=result.get("sub_id"),
                subscription_payload=xui_api.subscription_payload_from_result(
                    result,
                    subscription_tier=subscription_tier,
                ),
                subscription_tier=subscription_tier,
                new_whitelist_cycle_anchor_at=whitelist_cycle_anchor_at,
            )
            if not key_updated:
                watchdog_reason = (
                    f"DB_UPDATE_FAIL: key_id={key_id} host={actual_host_name} uuid={result.get('client_uuid')}"
                )
                _mark_transaction_issuance_failure(
                    payment_id=external_payment_id,
                    metadata=metadata,
                    reason=watchdog_reason,
                    failure_logs={
                        "stage": "db_update",
                        "key_id": str(key_id),
                        "host": str(actual_host_name),
                        "uuid": str(result.get("client_uuid") or ""),
                    },
                )
                logger.error(
                    "PAYMENT DB_UPDATE_FAIL: payment_id=%s user_id=%s %s",
                    external_payment_id,
                    user_id,
                    watchdog_reason,
                )
                await notify_admin_of_issuance_failure(
                    bot,
                    user_id=user_id,
                    payment_id=external_payment_id,
                    payment_method=payment_method,
                    host_name=actual_host_name,
                    plan_id=plan_id,
                    plan_name=plan_name,
                    months=months,
                    amount_rub=price,
                    action=action,
                    key_email=str(result.get("email") or email or ""),
                    reason=watchdog_reason,
                    failure_logs={
                        "stage": "db_update",
                        "key_id": str(key_id),
                        "host": str(actual_host_name),
                        "uuid": str(result.get("client_uuid") or ""),
                    },
                )
                await _show_payment_status_to_user(
                    bot,
                    user_id=user_id,
                    processing_message=processing_message,
                    text=(
                        "❌ Платёж получен, но продление не сохранилось в БД.\n\n"
                        "Платёж сохранён, автопродление поставлено в повтор. Поддержка уже уведомлена."
                    ),
                    reply_markup=keyboards.create_support_error_keyboard(),
                )
                if external_payment_id:
                    reset_transaction_to_pending(external_payment_id)
                return
            _persist_payment_key_applied_for_retry(
                external_payment_id,
                metadata,
                key_id=int(key_id),
                result=result,
                action=str(action),
                target_expiry_ms=target_expiry_ms,
                issued_key_value=issued_key_value,
            )

        metadata["host_name"] = actual_host_name
        metadata["customer_email"] = str(result.get("email") or email or metadata.get("customer_email") or "")
        metadata["key_id"] = key_id
        metadata["key_display_name"] = key_display_name

        price = float(metadata.get("price"))

        user_data = await asyncio.to_thread(get_user, user_id) or user_data or {}
        referrer_id = user_data.get("referred_by")
        policy = await asyncio.to_thread(referrals.get_policy)
        percentage = policy["percentage"] if policy["enabled"] and referrer_id else Decimal("0")
        reward = (Decimal(str(price)) * percentage / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        accounting_payment_id = str(
            external_payment_id
            or metadata.get("payment_id")
            or metadata.get("provider_payment_id")
            or f"legacy:{user_id}:{key_id}:{purchase_window.paid_at.isoformat()}:{action}"
        ).strip()
        accounting_applied_now = await asyncio.to_thread(
            apply_payment_accounting_once,
            accounting_payment_id,
            user_id=user_id,
            amount_rub=price,
            months=months,
            referrer_id=int(referrer_id) if referrer_id else None,
            referral_reward=float(reward),
            activate_super=is_super_flow,
            discount_claim_id=metadata.get("referral_discount_claim_id"),
        )
        if not accounting_applied_now and not await asyncio.to_thread(is_payment_accounting_applied, accounting_payment_id):
            raise RuntimeError("payment_accounting_not_applied")
        metadata["accounting_applied"] = True

        applied_reward = await asyncio.to_thread(referrals.get_payment_reward, accounting_payment_id)
        referrer_id = applied_reward.get("referrer_id")
        reward = Decimal(str(applied_reward.get("reward", 0)))
        if accounting_applied_now and referrer_id and reward > 0:
            try:
                referrer_username = html_escape(str(user_data.get("username") or "пользователь"))
                await bot.send_message(
                    referrer_id,
                    f"🎉 Ваш реферал @{referrer_username} совершил покупку на сумму {price:.2f} RUB!\n"
                    f"💰 На ваш баланс начислено вознаграждение: {reward:.2f} RUB.",
                )
            except TelegramAPIError as e:
                logger.warning("Could not send referral reward notification to %s: %s", referrer_id, e)

        user_info = get_user(user_id)

        log_username = user_info.get("username", "N/A") if user_info else "N/A"
        log_status = "paid"
        log_amount_rub = float(price)
        log_method = metadata.get("payment_method", "Unknown")
        resolved_plan_name = _resolve_transaction_plan_name(
            explicit_plan_name=plan_name,
            plan_id=int(metadata.get("plan_id") or 0),
            months=months,
            is_super_flow=is_super_flow,
        )

        log_metadata_payload = {
            "user_id": user_id,
            "plan_id": metadata.get("plan_id"),
            "plan_name": resolved_plan_name,
            "key_display_name": key_display_name,
            "host_name": metadata.get("host_name"),
            "customer_email": metadata.get("customer_email"),
            "months": months,
            "action": action,
            "key_id": key_id,
            "paid_at": purchase_window.paid_at.isoformat(),
            "accounting_applied": True,
            "payment_applied_key_id": int(key_id),
            "payment_applied_expiry_ms": int(target_expiry_ms),
        }
        if metadata.get("mobile_strict_renewal") is True:
            for field in (
                "mobile_payment_id", "mobile_strict_renewal", "mobile_target_fingerprint",
                "mobile_renewal_mode", "lava_order_id", "lava_invoice_id", "bot_source",
                "promo_code", "paid_months", "bonus_months",
            ):
                if field in metadata:
                    log_metadata_payload[field] = metadata[field]
        if missing_extend_key_fallback:
            log_metadata_payload["original_action"] = "extend"
            log_metadata_payload["missing_extend_key_id"] = missing_extend_key_id
        if trial_super_replacement:
            log_metadata_payload["original_action"] = "extend"
            log_metadata_payload["trial_super_replaced_key_id"] = trial_super_replaced_key_id
            log_metadata_payload["trial_super_replaced_email"] = trial_super_replaced_email
        log_metadata = json.dumps(log_metadata_payload)

        if external_payment_id:
            finalized = finalize_transaction_paid(
                external_payment_id,
                username=log_username,
                payment_method=log_method,
                amount_rub=log_amount_rub,
                metadata=log_metadata,
                created_date_override=purchase_window.paid_at,
            )
            if not finalized:
                # Fallback keeps payment from being stuck in processing/pending
                # even if enrichment (username/metadata update) fails.
                mark_only = mark_transaction_paid(external_payment_id, payment_method=log_method)
                if not mark_only:
                    logger.warning(
                        "Could not finalize/mark transaction paid for external payment_id=%s",
                        external_payment_id,
                    )
                    # If pending row does not exist at all (legacy/manual loss), preserve accounting by direct insert.
                    log_transaction(
                        username=log_username,
                        transaction_id=None,
                        payment_id=external_payment_id,
                        user_id=user_id,
                        status=log_status,
                        amount_rub=log_amount_rub,
                        amount_currency=None,
                        currency_name=None,
                        payment_method=log_method,
                        metadata=log_metadata,
                        created_date_override=purchase_window.paid_at,
                    )
                else:
                    logger.warning(
                        "Transaction paid with fallback (without full enrichment) for external payment_id=%s",
                        external_payment_id,
                    )
            else:
                logger.info("Finalized pending transaction for external payment_id=%s", external_payment_id)

            if not is_transaction_fully_issued(external_payment_id):
                logger.error(
                    "Payment side effects are applied but transaction finalization is not durable: payment_id=%s",
                    external_payment_id,
                )
                _mark_transaction_issuance_failure(
                    payment_id=external_payment_id,
                    metadata=metadata,
                    reason="transaction_finalize_not_durable",
                    failure_logs={"stage": "transaction_finalize", "key_id": str(key_id)},
                )
                return
        else:
            # No pending transaction exists (legacy/manual/alt payment flow): keep historical logging behavior.
            provider_payment_id = str(
                metadata.get("external_payment_id")
                or metadata.get("payment_id")
                or metadata.get("provider_payment_id")
                or ""
            ).strip()
            log_transaction(
                username=log_username,
                transaction_id=None,
                payment_id=provider_payment_id or None,
                user_id=user_id,
                status=log_status,
                amount_rub=log_amount_rub,
                amount_currency=None,
                currency_name=None,
                payment_method=log_method,
                metadata=log_metadata,
                created_date_override=purchase_window.paid_at,
            )

        if processing_message:
            try:
                await processing_message.delete()
            except TelegramBadRequest as processing_delete_error:
                logger.warning(
                    "Could not delete payment processing message for user_id=%s: %s",
                    user_id,
                    processing_delete_error,
                )

        connection_string, subscription_link = _split_key_access_value(issued_key_value)
        new_expiry_date = purchase_window.expiry_at

        all_user_keys = get_user_keys(user_id)
        delivered_key_data = next((key for key in all_user_keys if key["key_id"] == key_id), None)
        key_number = next((i + 1 for i, key in enumerate(all_user_keys) if key["key_id"] == key_id), len(all_user_keys))

        if trial_super_replacement:
            final_text = _render_manual_migration_key_text(
                host_name=actual_host_name,
                expiry_date=new_expiry_date,
                key_value=issued_key_value,
                key_email=str(result.get("email") or email or ""),
                key_id=key_id,
            )
        else:
            final_text = get_purchase_success_text(
                action="создан" if action == "new" else "продлен",
                key_number=key_number,
                expiry_date=new_expiry_date,
                connection_string=connection_string,
                subscription_link=subscription_link,
                host_name=actual_host_name,
                key_email=str(result.get("email") or email or ""),
                whitelist_limit_gb=white_traffic_limit_bytes // (1024 ** 3),
            )
        if missing_extend_key_fallback:
            final_text = (
                "Ключ для продления не найден, поэтому мы выдали новый ключ на оплаченный срок.\n\n" + final_text
            )

        # Log technical details, do not show to user
        logger.info(
            "Key created successfully. Original host: %s, Actual host: %s, User: %s",
            host_name,
            actual_host_name,
            user_id,
        )

        # Show fallback notice to user only when fallback occurred
        if actual_host_name != host_name:
            logger.warning("Fallback occurred: %s -> %s for user %s", host_name, actual_host_name, user_id)
            final_text = "ℹ️ Ваш ключ успешно создан на резервном сервере.\n\n" + final_text

        try:
            await send_effect_message(
                bot,
                chat_id=user_id,
                text=final_text,
                effect_setting_key=PURCHASE_SUCCESS_MESSAGE_EFFECT_ID_SETTING,
                reply_markup=keyboards.create_key_info_keyboard(
                    key_id,
                    show_refresh=_is_extendable_managed_key(delivered_key_data, expiry_date=new_expiry_date),
                    show_qr=not bool(subscription_link),
                ),
            )
            if external_payment_id:
                update_transaction_metadata_field(
                    external_payment_id,
                    "key_delivered_at",
                    datetime.utcnow().isoformat(timespec="seconds"),
                    statuses=("processing", "pending", "paid"),
                )
        except (TelegramBadRequest, TelegramForbiddenError) as delivery_error:
            logger.warning(
                "Key issued but could not deliver success message to user_id=%s payment_id=%s: %s",
                user_id,
                external_payment_id,
                delivery_error,
            )
            await notify_admin_of_post_issue_delivery_failure(
                bot,
                user_id=user_id,
                payment_id=external_payment_id,
                host_name=actual_host_name,
                key_id=key_id,
                key_email=str(result.get("email") or email or ""),
                error_text=str(delivery_error),
            )

        await notify_admin_of_purchase(bot, metadata)

    except (TelegramBadRequest, TelegramForbiddenError) as tg_err:
        _tg_err_text = str(tg_err).lower()
        _is_permanent_tg = (
            "chat not found" in _tg_err_text
            or "bot was blocked" in _tg_err_text
            or "user is deactivated" in _tg_err_text
        )
        if not _is_permanent_tg:
            raise
        fatal_reason = f"fatal_delivery:{type(tg_err).__name__}:{str(tg_err)[:200]}"
        logger.error(
            "PERMANENT: Cannot deliver key to user %s (%s). payment_id=%s — will NOT retry.",
            user_id,
            _tg_err_text,
            external_payment_id,
        )
        _mark_transaction_issuance_failure(
            payment_id=external_payment_id,
            metadata=metadata,
            reason=fatal_reason,
            failure_logs={
                "stage": "fatal_delivery",
                "error_type": type(tg_err).__name__,
                "error": str(tg_err),
            },
            fatal=True,
        )
        await notify_admin_of_issuance_failure(
            bot,
            user_id=user_id,
            payment_id=external_payment_id,
            payment_method=payment_method,
            host_name=host_name,
            plan_id=plan_id if "plan_id" in locals() else None,
            plan_name=plan_name if "plan_name" in locals() else None,
            months=months if "months" in locals() else None,
            amount_rub=price if "price" in locals() else None,
            action=action if "action" in locals() else None,
            key_email=customer_email if "customer_email" in locals() else None,
            reason=fatal_reason,
            failure_logs={
                "stage": "fatal_delivery",
                "error_type": type(tg_err).__name__,
                "error": str(tg_err),
            },
        )
    except Exception as e:
        logger.error(f"Error processing payment for user {user_id} on host {host_name}: {e}", exc_info=True)
        exception_reason = f"exception:{type(e).__name__}:{str(e)[:300]}"
        _mark_transaction_issuance_failure(
            payment_id=external_payment_id,
            metadata=metadata,
            reason=exception_reason,
            failure_logs={
                "stage": "exception",
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )
        await notify_admin_of_issuance_failure(
            bot,
            user_id=user_id,
            payment_id=external_payment_id,
            payment_method=payment_method,
            host_name=host_name,
            plan_id=plan_id if "plan_id" in locals() else None,
            plan_name=plan_name if "plan_name" in locals() else None,
            months=months if "months" in locals() else None,
            amount_rub=price if "price" in locals() else None,
            action=action if "action" in locals() else None,
            key_email=customer_email if "customer_email" in locals() else None,
            reason=exception_reason,
            failure_logs={
                "stage": "exception",
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )
        if external_payment_id:
            reset_transaction_to_pending(external_payment_id)
        await _show_payment_status_to_user(
            bot,
            user_id=user_id,
            processing_message=processing_message,
            text=(
                "❌ Произошла ошибка при обработке платежа.\n\n"
                "Платёж сохранён, автовыдача поставлена в повтор. Поддержка уже уведомлена."
            ),
            reply_markup=keyboards.create_support_error_keyboard(),
        )
