import asyncio
import base64
import csv
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps
from hmac import compare_digest
from math import ceil
from urllib.parse import quote, unquote, urlparse
from zoneinfo import ZoneInfo

from flask import Flask, Response, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for

from shop_bot.webhook_server.panel_access import (
    authenticate_scoped_account,
    register_panel_access,
    start_scoped_session,
)

logger = logging.getLogger(__name__)
_ENV_VAR_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_INVALID_XUI_ENV_NAME_MESSAGE = "Имя переменной XUI API Token должно быть в формате XUI_API_TOKEN_NAME."
_DASHBOARD_MOSCOW_TZ = ZoneInfo("Europe/Moscow")

from shop_bot.bot import handlers
from shop_bot.happ_routing import register_subscription_routing
from shop_bot.clearway_ddg import append_clearway_ddg_hosts
from shop_bot.config import BOT_SOURCE, get_subscription_ready_notification_template, get_whitelist_policy_text
from shop_bot.config import is_truthy as _is_truthy
from shop_bot.data_manager import scheduler as scheduler_module
from shop_bot.data_manager import referrals, subscription_policy
from shop_bot.data_manager.dashboard_analytics import payment_time_moscow
from shop_bot.data_manager.database import (
    BOT_SOURCE_ALL,
    BOT_SOURCE_MAIN,
    BOT_SOURCE_RESERVE,
    TX_TYPE_ALL,
    TX_TYPE_HYSTERIA,
    TX_TYPE_OLD,
    TX_TYPE_SUPER,
    TX_TYPE_VLESS,
    add_migrated_key,
    add_new_key,
    archive_key,
    ban_user,
    claim_mainpanel_migration_item,
    classify_product_type,
    close_db,
    create_host,
    create_incident,
    create_pending_transaction,
    create_plan,
    delete_host,
    delete_key_by_email,
    delete_plan,
    delete_user_keys,
    find_and_complete_ton_transaction,
    finish_mainpanel_migration_item,
    get_active_key_by_panel_sub_id,
    get_all_hosts,
    get_all_keys_for_host_migration,
    get_all_settings,
    get_all_settings_cached,
    get_dashboard_snapshot,
    get_delivery_time_percentiles,
    get_distinct_vpn_key_hosts,
    get_host,
    get_hosts_with_plans,
    get_hosts_with_status,
    get_key_by_email,
    get_key_by_id,
    get_key_by_panel_sub_id,
    get_mainpanel_inbound_sync_run,
    get_mainpanel_migration_source_keys,
    get_migration_for_key,
    get_next_key_number,
    get_pending_lava_transaction_by_order_id,
    promote_lava_pending_transaction,
    get_pending_paypear_transaction_by_order_id,
    get_pending_transaction_metadata,
    get_replacement_key_for_incident,
    get_setting,
    get_successful_transactions_for_migration_keys,
    get_transaction_by_payment_id,
    get_user,
    get_user_count,
    get_user_id_by_username,
    get_user_keys,
    get_user_transactions,
    get_user_whitelist_traffic_statuses,
    get_users_filtered,
    get_users_paginated,
    is_transaction_fully_issued,
    is_transaction_processing,
    log_migration,
    mark_transaction_canceled,
    mark_transaction_retryable_failure,
    register_user_if_not_exists,
    reset_transaction_to_pending,
    set_host_delivery_state,
    set_host_maintenance,
    start_transaction_processing,
    unban_user,
    update_host_api_token_ref,
    update_host_config,
    update_host_node_sync_state,
    update_host_urls,
    update_key_info,
    update_key_subscription_payload,
    update_setting,
    update_transaction_metadata,
)
from shop_bot.data_manager.host_visibility import filter_customer_visible_hosts
from shop_bot.domain import mainpanel_migration, trial_reset
from shop_bot.errors import BotError, classify_error, send_critical_alert
from shop_bot.modules import blitz_api, lava_api, xui_api
from shop_bot.modules.paypear_api import (
    map_paypear_status,
)
from shop_bot.modules.paypear_api import (
    verify_webhook_signature as verify_paypear_webhook_signature,
)
from shop_bot.modules.subscription_dates import (
    add_calendar_months,
    panel_days_until,
    resolve_recurring_cycle_anchor,
)
from shop_bot.payment_metadata import (
    merge_payment_metadata,
    missing_required_payment_metadata_fields,
    normalize_bot_source,
    normalize_payment_metadata,
)
from shop_bot.payment_metadata import (
    metadata_bot_source_matches_runtime as _payment_metadata_bot_source_matches_runtime,
)

_bot_controller = None
_RUNTIME_CONTROL_HEADER = "X-Runtime-Control-Token"
_SUBSCRIPTION_CONNECTION_PREFIXES = ("vless://", "vmess://", "trojan://", "hy2://", "hysteria2://")
_DEFAULT_SUBSCRIPTION_UPSTREAM_TIMEOUT_SECONDS = 8
_DEFAULT_SUBSCRIPTION_UPSTREAM_MAX_BYTES = 512 * 1024
_DEFAULT_MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS = 60
_DEFAULT_MANUAL_ISSUE_PANEL_JOB_TIMEOUT_SECONDS = 75
_ISSUE_IDEMPOTENCY_TTL_SECONDS = 10 * 60
_ISSUE_IDEMPOTENCY_WAIT_SECONDS = 5 * 60
_ISSUE_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._:-]{16,128}")


def _read_positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(str(os.environ.get(name, str(default))).strip() or str(default)))
    except (TypeError, ValueError):
        return default


MANAGED_SUBSCRIPTION_UPSTREAM_TIMEOUT_SECONDS = _read_positive_int_env(
    "MANAGED_SUBSCRIPTION_UPSTREAM_TIMEOUT_SECONDS",
    _DEFAULT_SUBSCRIPTION_UPSTREAM_TIMEOUT_SECONDS,
)
MANAGED_SUBSCRIPTION_UPSTREAM_MAX_BYTES = _read_positive_int_env(
    "MANAGED_SUBSCRIPTION_UPSTREAM_MAX_BYTES",
    _DEFAULT_SUBSCRIPTION_UPSTREAM_MAX_BYTES,
)
MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS = _read_positive_int_env(
    "MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS",
    _DEFAULT_MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
)
MANUAL_ISSUE_PANEL_JOB_TIMEOUT_SECONDS = max(
    MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS + 5,
    _read_positive_int_env(
        "MANUAL_ISSUE_PANEL_JOB_TIMEOUT_SECONDS",
        _DEFAULT_MANUAL_ISSUE_PANEL_JOB_TIMEOUT_SECONDS,
    ),
)

ALL_SETTINGS_KEYS = [
    "panel_login",
    "panel_password",
    "about_text",
    "terms_url",
    "privacy_url",
    "android_url",
    "ios_url",
    "windows_url",
    "linux_url",
    "support_user",
    "support_text",
    "channel_url",
    "telegram_bot_token",
    "telegram_bot_username",
    "admin_telegram_id",
    "yookassa_shop_id",
    "yookassa_secret_key",
    "yookassa_payments_enabled",
    "sbp_enabled",
    "receipt_email",
    "cryptobot_token",
    "heleket_merchant_id",
    "heleket_api_key",
    "domain",
    "referral_percentage",
    "referral_discount",
    "ton_wallet_address",
    "tonapi_key",
    "force_subscription",
    "trial_enabled",
    "trial_duration_days",
    "trial_duration_days_super",
    "enable_referrals",
    "minimum_withdrawal",
    "support_group_id",
    "support_bot_token",
    "blitz_url",
    "blitz_api_token",
    "blitz_username",
    "blitz_password",
    "enable_blitz",
    "blitz_insecure_tls",
    "super_key_host_name",
    "standard_key_host_name",
    "chatwoot_enabled",
    "chatwoot_api_url",
    "chatwoot_account_id",
    "chatwoot_api_token",
    "chatwoot_inbox_id",
    "alert_telegram_bot_token",
    "alert_telegram_chat_id",
    "alert_telegram_thread_id",
    "welcome_message_effect_id",
    "onboarding_success_message_effect_id",
    "trial_success_message_effect_id",
    "purchase_success_message_effect_id",
    "maintenance_message",
    "main_service_mode_message",
    "main_service_mode_reserve_bot_url",
    "reserve_bot_token",
    "reserve_bot_username",
    "paypear_shop_id",
    "paypear_secret_key",
    "paypear_api_key",
    "paypear_webhook_secret",
    "paypear_webhook_url",
    "paypear_api_base_url",
    "paypear_payment_lifetime_minutes",
    # Legacy Platega settings kept for backward compatibility of old pending transactions.
    "platega_api_key",
    "platega_shop_id",
    "platega_webhook_secret",
    "platega_sbp_method_id",
    "platega_card_method_id",
]

CHECKBOX_SETTINGS_KEYS = [
    "force_subscription",
    "sbp_enabled",
    "trial_enabled",
    "enable_referrals",
    "enable_blitz",
    "blitz_insecure_tls",
    "chatwoot_enabled",
    "maintenance_mode",
    "main_service_mode_enabled",
    "yookassa_payments_enabled",
]


def _is_checked(form_data, key: str) -> bool:
    values = form_data.getlist(key)
    return bool(values and values[-1] == "true")


def _is_fresh_chatwoot_timestamp(timestamp: str, max_age_seconds: int = 300) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = int(time.time())
    return abs(now - ts) <= max_age_seconds


def _is_valid_chatwoot_signature(raw_body: bytes, signature: str, timestamp: str, secret: str) -> bool:
    if not signature or not secret or not timestamp:
        return False
    signed_payload = f"{timestamp}.".encode("utf-8") + raw_body
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return compare_digest(expected, signature.strip())


def _truncate_text(value, max_length: int = 1024) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3]}..."


def _is_env_var_name(value: str | None) -> bool:
    candidate = str(value or "").strip()
    return bool(candidate and _ENV_VAR_NAME_RE.fullmatch(candidate))


def _resolve_host_token_mode(raw_value: str | None, host: dict | None = None) -> str:
    candidate = str(raw_value or "").strip()
    if not candidate:
        auto_env_name = xui_api.get_auto_host_api_token_env_name(host)
        if auto_env_name and os.environ.get(auto_env_name):
            return "env_auto"
        return "none"

    lowered = candidate.lower()
    if lowered.startswith("env:"):
        env_name = candidate[len("env:") :].strip()
        return "env" if _is_env_var_name(env_name) else "direct"

    if _is_env_var_name(candidate) and os.environ.get(candidate):
        return "env"

    return "direct"


def _describe_host_api_token_config(raw_value: str | None, host: dict | None = None) -> str:
    candidate = str(raw_value or "").strip()
    mode = _resolve_host_token_mode(candidate, host)
    if mode == "none":
        return "—"
    if mode == "env_auto":
        return f"env:{xui_api.get_auto_host_api_token_env_name(host)} (auto)"
    if mode == "env":
        lowered = candidate.lower()
        if lowered.startswith("env:"):
            env_name = candidate[len("env:") :].strip()
            return f"env:{env_name}"
        return f"env:{candidate}"
    return "direct token (hidden)"


def _collect_xui_api_token_env_candidates(hosts: list[dict] | None = None) -> list[str]:
    candidates: set[str] = {key for key in os.environ if key.startswith("XUI_API_TOKEN_")}
    for host in hosts or []:
        token_ref = str(host.get("host_api_token_env") or "").strip()
        if token_ref.lower().startswith("env:"):
            token_ref = token_ref[len("env:") :].strip()
        if _is_env_var_name(token_ref):
            candidates.add(token_ref)
        auto_env_name = xui_api.get_auto_host_api_token_env_name(host)
        if auto_env_name:
            candidates.add(auto_env_name)
    return sorted(candidates)


def _read_host_api_token_ref_from_form(form) -> str | None:
    source_mode = str(form.get("host_api_token_mode") or "").strip().lower()
    if source_mode == "direct":
        return str(form.get("host_api_token_direct") or "").strip() or None
    if source_mode == "env":
        env_name = str(form.get("host_api_token_env_name") or "").strip()
        env_choice = str(form.get("host_api_token_env_choice") or "").strip()
        env_ref = env_name or env_choice
        if not env_ref:
            return None
        if env_ref.lower().startswith("env:"):
            env_ref = env_ref[len("env:") :].strip()
        if not _is_env_var_name(env_ref):
            raise ValueError(_INVALID_XUI_ENV_NAME_MESSAGE)
        return f"env:{env_ref}"

    legacy_ref = str(form.get("host_api_token_env") or "").strip()
    return legacy_ref or None


def _validate_host_url(raw_url: str, *, field_label: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        raise ValueError(f"{field_label} обязателен.")  # noqa: TRY003
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_label} должен быть полным HTTP/HTTPS URL.")  # noqa: TRY003
    return value


def _validate_optional_host_url(raw_url: str | None, *, field_label: str) -> str | None:
    value = str(raw_url or "").strip()
    if not value:
        return None
    return _validate_host_url(value, field_label=field_label)


def _read_positive_int(raw_value: str | None, *, field_label: str) -> int:
    try:
        value = int(str(raw_value or "").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_label} должен быть положительным числом.") from exc  # noqa: TRY003
    if value <= 0:
        raise ValueError(f"{field_label} должен быть положительным числом.")  # noqa: TRY003
    return value


def _read_non_negative_int(raw_value: str | None, *, field_label: str) -> int:
    try:
        value = int(str(raw_value or "0").strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_label} должен быть числом.") from exc  # noqa: TRY003
    if value < 0:
        raise ValueError(f"{field_label} не может быть отрицательным.")  # noqa: TRY003
    return value


def _read_delivery_state(raw_value: str | None) -> str:
    state = str(raw_value or "draft").strip().lower()
    if state not in {"draft", "active", "disabled"}:
        raise ValueError("Состояние хоста должно быть draft, active или disabled.")  # noqa: TRY003
    return state


def _read_host_config_from_form(
    form,
    *,
    current_host_pass: str | None = None,
) -> dict[str, object]:
    host_url = _validate_host_url(form.get("host_url"), field_label="URL панели")
    subscription_base_url = _validate_optional_host_url(
        form.get("host_subscription_base_url"),
        field_label="URL подписки",
    )
    host_username = str(form.get("host_username") or "").strip()
    host_pass = str(form.get("host_pass") or "").strip() or str(current_host_pass or "").strip()
    if not host_username:
        raise ValueError("Логин XUI обязателен.")  # noqa: TRY003
    if not host_pass:
        raise ValueError("Пароль XUI обязателен.")  # noqa: TRY003

    return {
        "host_url": host_url,
        "host_username": host_username,
        "host_pass": host_pass,
        "host_inbound_id": _read_positive_int(
            form.get("host_inbound_id"),
            field_label="ID входящего подключения",
        ),
        "subscription_base_url": subscription_base_url,
        "is_white_host": _is_checked(form, "is_white_host"),
        "delivery_state": _read_delivery_state(form.get("delivery_state")),
    }


def _to_iso_datetime(value) -> str | None:
    date_value = _parse_datetime(value)
    if date_value is None:
        raw = str(value).strip() if value is not None else ""
        return raw or None
    return date_value.isoformat(timespec="seconds")


def _parse_transaction_metadata(raw_metadata) -> dict:
    if not raw_metadata:
        return {}
    if isinstance(raw_metadata, dict):
        return raw_metadata
    try:
        parsed = json.loads(raw_metadata)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _normalize_manual_bot_source(raw_value) -> str:
    normalized = str(raw_value or "").strip().lower()
    if normalized in {BOT_SOURCE_MAIN, BOT_SOURCE_RESERVE}:
        return normalized
    runtime_source = normalize_bot_source(BOT_SOURCE) or BOT_SOURCE_MAIN
    return runtime_source if runtime_source in {BOT_SOURCE_MAIN, BOT_SOURCE_RESERVE} else BOT_SOURCE_MAIN


def _resolve_runtime_bot_token(bot_source: str | None = None) -> str:
    """Resolve the Telegram token for the current bot source.

    Admin tools must use the same token source as the running shop bot.
    Production stores shop tokens in DB settings, while BOT_TOKEN env may be unset.
    """
    selected_source = _normalize_manual_bot_source(bot_source)
    token_key = "reserve_bot_token" if selected_source == BOT_SOURCE_RESERVE else "telegram_bot_token"
    return str(get_setting(token_key) or "").strip() or str(os.environ.get("BOT_TOKEN", "")).strip()


def _resolve_runtime_control_token() -> str:
    """Token for trusted runtime-to-runtime control requests."""
    return (
        str(os.environ.get("RUNTIME_CONTROL_TOKEN", "")).strip()
        or str(os.environ.get("FLASK_SECRET_KEY", "")).strip()
        or str(get_setting("flask_secret_key") or "").strip()
    )


def _runtime_stop_allowed() -> bool:
    """Operational kill-switch for stop/restart endpoints.

    Always-on policy keeps runtime stop disabled by default.
    """
    return _is_truthy(os.environ.get("ALLOW_RUNTIME_STOP", "false"))


def _resolve_runtime_control_base_url(bot_source: str) -> str:
    selected = _normalize_manual_bot_source(bot_source)
    if selected == BOT_SOURCE_RESERVE:
        default_url = "http://bot-reserve:8080"
        return str(os.environ.get("RESERVE_RUNTIME_CONTROL_URL") or default_url).rstrip("/")
    default_url = "http://bot-main:8080"
    return str(os.environ.get("MAIN_RUNTIME_CONTROL_URL") or default_url).rstrip("/")


def _is_runtime_control_request_authorized(req) -> bool:
    expected = _resolve_runtime_control_token()
    provided = str(req.headers.get(_RUNTIME_CONTROL_HEADER, "")).strip()
    if not expected or not provided:
        return False
    return compare_digest(provided, expected)


def _runtime_status_unavailable(message: str) -> dict[str, object]:
    return {
        "available": False,
        "shop_bot_running": False,
        "shop_bot_healthy": False,
        "shop_bot_health_message": message,
        "all_settings_ok": False,
        "message": message,
    }


def _load_runtime_status(bot_source: str, *, timeout: float = 1.5) -> dict[str, object]:
    token = _resolve_runtime_control_token()
    if not token:
        return _runtime_status_unavailable("Нет внутреннего токена управления runtime.")

    status_url = f"{_resolve_runtime_control_base_url(bot_source)}/internal/runtime/status"
    req = urllib.request.Request(
        status_url,
        headers={_RUNTIME_CONTROL_HEADER: token},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload_raw = resp.read().decode("utf-8", errors="ignore")
        payload = json.loads(payload_raw) if payload_raw else {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            "available": True,
            "shop_bot_running": bool(payload.get("shop_bot_running")),
            "shop_bot_healthy": bool(payload.get("shop_bot_healthy", payload.get("shop_bot_running"))),
            "shop_bot_health_message": str(payload.get("shop_bot_health_message") or ""),
            "all_settings_ok": bool(payload.get("all_settings_ok")),
            "message": str(payload.get("message") or ""),
        }
    except urllib.error.HTTPError as exc:
        details = f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        details = f"network: {exc.reason}"
    except Exception as exc:
        details = str(exc)

    logger.warning(
        "Runtime status probe failed for bot_source=%s url=%s: %s",
        bot_source,
        status_url,
        details,
    )
    return _runtime_status_unavailable(f"Reserve runtime недоступен ({details})")


def _dispatch_runtime_shop_action(
    bot_source: str,
    action: str,
    *,
    timeout: float = 4.0,
) -> dict[str, str]:
    normalized_action = str(action or "").strip().lower()
    if normalized_action not in {"start", "stop"}:
        return {"status": "error", "message": "Неизвестная команда runtime-control."}
    if normalized_action == "stop" and not _runtime_stop_allowed():
        return {
            "status": "error",
            "message": "Остановка runtime отключена политикой always-on.",
        }

    token = _resolve_runtime_control_token()
    if not token:
        return {
            "status": "error",
            "message": "Внутренний токен runtime-control не задан.",
        }

    action_url = f"{_resolve_runtime_control_base_url(bot_source)}/internal/runtime/shop/{normalized_action}"
    req = urllib.request.Request(
        action_url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            _RUNTIME_CONTROL_HEADER: token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload_raw = resp.read().decode("utf-8", errors="ignore")
        payload = json.loads(payload_raw) if payload_raw else {}
        if not isinstance(payload, dict):
            payload = {}
        status = str(payload.get("status") or "success").strip().lower()
        message = str(payload.get("message") or "Команда отправлена.")
        return {
            "status": "success" if status == "success" else "error",
            "message": message,
        }
    except urllib.error.HTTPError as exc:
        response_message = ""
        try:
            payload_raw = exc.read().decode("utf-8", errors="ignore")
            payload = json.loads(payload_raw) if payload_raw else {}
            if isinstance(payload, dict):
                response_message = str(payload.get("message") or "")
        except Exception:
            response_message = ""
        details = response_message or f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        details = f"network: {exc.reason}"
    except Exception as exc:
        details = str(exc)

    logger.error(
        "Runtime action failed bot_source=%s action=%s url=%s error=%s",
        bot_source,
        normalized_action,
        action_url,
        details,
    )
    return {
        "status": "error",
        "message": f"Не удалось выполнить команду {normalized_action} для reserve-runtime: {details}",
    }


def _coerce_checkbox_value(raw_value) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    if isinstance(raw_value, str):
        return raw_value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw_value)


def _parse_issue_duration_value(raw_value, *, field_label: str, max_value: int) -> int:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return 0
    try:
        parsed = int(raw_text)
    except (TypeError, ValueError):
        raise ValueError(f"{field_label} должны быть целым числом")
    if parsed < 0 or parsed > max_value:
        raise ValueError(f"{field_label} должны быть в диапазоне 0..{max_value}")
    return parsed


def _resolve_issue_expiry_options(
    *,
    raw_days,
    raw_months,
    raw_years,
    raw_unlimited,
    now: datetime | None = None,
) -> dict[str, object]:
    safe_now = now or datetime.now()
    unlimited = _coerce_checkbox_value(raw_unlimited)
    days = _parse_issue_duration_value(raw_days, field_label="Дни", max_value=3650)
    months = _parse_issue_duration_value(raw_months, field_label="Месяцы", max_value=99)
    years = _parse_issue_duration_value(raw_years, field_label="Годы", max_value=99)

    if unlimited:
        expiry_dt = datetime(2099, 12, 31, 23, 59, 59)
        return {
            "days": days,
            "months": months,
            "years": years,
            "unlimited": True,
            "panel_days": panel_days_until(expiry_dt, now=safe_now),
            "target_expiry_ms": int(expiry_dt.timestamp() * 1000),
            "expiry_dt": expiry_dt,
            "expiry_label": "Безлимит",
        }

    if not any((days, months, years)):
        raise ValueError("Укажите срок: дни, месяцы, годы или включите безлимит")

    expiry_dt = add_calendar_months(safe_now, months + (years * 12))
    if days:
        expiry_dt += timedelta(days=days)

    return {
        "days": days,
        "months": months,
        "years": years,
        "unlimited": False,
        "panel_days": panel_days_until(expiry_dt, now=safe_now),
        "target_expiry_ms": int(expiry_dt.timestamp() * 1000),
        "expiry_dt": expiry_dt,
        "expiry_label": expiry_dt.strftime("%d.%m.%Y %H:%M"),
    }


def _decode_cryptobot_payload_field(raw_value: str | None) -> str | None:
    """Decode optional base64url payload fields from CryptoBot."""
    if not raw_value:
        return None
    try:
        normalized = str(raw_value).strip()
        if not normalized:
            return None
        padded = normalized + "=" * (-len(normalized) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        return decoded or None
    except Exception:
        logger.warning("CryptoBot webhook: failed to decode optional payload field.")
        return None


def _extract_provider_paid_at(*candidates) -> str | None:
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, (int, float)):
            # Accept both seconds and milliseconds epoch.
            raw = float(candidate)
            epoch_seconds = raw / 1000.0 if raw > 10_000_000_000 else raw
            try:
                return datetime.fromtimestamp(epoch_seconds).isoformat()
            except Exception:
                continue
        raw_text = str(candidate).strip()
        if not raw_text:
            continue
        parsed = _parse_datetime(raw_text)
        if parsed:
            return parsed.isoformat()
    return None


def _lava_paid_at_utc(pay_time, confirmed_at: datetime) -> str:
    """Use a zoned provider instant, or the first confirmed webhook instant.

    LAVA also sends an offset-free wall clock. Its zone is ambiguous, so it
    must not become a future cycle anchor in the UTC bot runtime.
    """
    confirmed_utc = confirmed_at.astimezone(UTC)
    paid_utc = confirmed_utc
    raw = str(pay_time or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None and parsed.tzinfo is not None:
        provider_utc = parsed.astimezone(UTC)
        if provider_utc <= confirmed_utc:
            paid_utc = provider_utc
    return paid_utc.replace(tzinfo=None).isoformat(timespec="seconds")


def _chatwoot_nested_value(mapping: dict, *keys: str):
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _normalize_chatwoot_telegram_id(value) -> str | None:
    if value is None:
        return None
    raw_value = str(value).strip()
    if not raw_value:
        return None
    try:
        user_id = int(raw_value)
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None
    return str(user_id)


def _normalize_chatwoot_username(value) -> str | None:
    if value is None:
        return None
    raw_value = str(value).strip()
    if not raw_value:
        return None

    normalized = raw_value.lstrip("@").strip()
    return normalized or None


def _extract_chatwoot_contact_context(data: dict) -> tuple[str | None, int | None, str | None]:
    conversation = data.get("conversation") or {}
    contact = data.get("contact") or {}
    message = data.get("message") or {}
    message_sender = message.get("sender") or {}
    meta = conversation.get("meta") or {}
    contact_inbox = conversation.get("contact_inbox") or {}

    sender = meta.get("sender") or message.get("sender") or contact or {}
    source_candidates = (
        sender.get("identifier"),
        sender.get("identifier_value"),
        contact.get("identifier"),
        contact.get("identifier_value"),
        contact_inbox.get("source_id"),
        _chatwoot_nested_value(sender, "additional_attributes", "social_telegram_user_id"),
        _chatwoot_nested_value(message_sender, "additional_attributes", "social_telegram_user_id"),
        _chatwoot_nested_value(contact, "additional_attributes", "social_telegram_user_id"),
        _chatwoot_nested_value(sender, "custom_attributes", "pb_telegram_id"),
        _chatwoot_nested_value(message_sender, "custom_attributes", "pb_telegram_id"),
        _chatwoot_nested_value(contact, "custom_attributes", "pb_telegram_id"),
        sender.get("name"),
        message_sender.get("name"),
        contact.get("name"),
        sender.get("username"),
        message_sender.get("username"),
        contact.get("username"),
    )
    source_id = next(
        (
            normalized
            for normalized in (_normalize_chatwoot_telegram_id(candidate) for candidate in source_candidates)
            if normalized
        ),
        None,
    )
    username = next(
        (
            normalized
            for normalized in (_normalize_chatwoot_username(candidate) for candidate in source_candidates)
            if normalized and not normalized.isdigit()
        ),
        None,
    )

    contact_id_raw = sender.get("id") or contact.get("id") or contact_inbox.get("contact_id")
    try:
        contact_id = int(contact_id_raw) if contact_id_raw is not None else None
    except (TypeError, ValueError):
        contact_id = None

    return source_id, contact_id, username


def _chatwoot_mainpanel_host_name() -> str | None:
    standard_host_name = str(get_setting("standard_key_host_name") or "").strip()
    super_host_name = str(get_setting("super_key_host_name") or "").strip()
    if standard_host_name and super_host_name and standard_host_name == super_host_name:
        return standard_host_name
    return None


def _is_chatwoot_mainpanel_key(key: dict, mainpanel_host_name: str | None) -> bool:
    if not mainpanel_host_name:
        return False
    return str(key.get("host_name") or "").strip() == mainpanel_host_name


def _build_chatwoot_additional_attributes(user_id: int) -> dict:
    max_keys = 10
    max_paid_transactions = 10
    all_keys = get_user_keys(user_id) or []
    user_data = get_user(user_id) or {}
    paid_transactions = get_user_transactions(user_id, limit=max_paid_transactions, statuses=("paid",))
    latest_paid_transaction = paid_transactions[0] if paid_transactions else None
    mainpanel_host_name = _chatwoot_mainpanel_host_name()

    active_keys = []
    for key in all_keys:
        if not _is_chatwoot_mainpanel_key(key, mainpanel_host_name):
            continue
        is_archived = bool(key.get("is_archived"))
        if _is_key_expired(key.get("expiry_date"), is_archived=is_archived):
            continue
        expiry_date = _to_iso_datetime(key.get("expiry_date"))
        if not expiry_date:
            continue
        active_keys.append((expiry_date, key))

    active_keys.sort(
        key=lambda item: (
            item[0],
            str(item[1].get("host_name") or ""),
            str(item[1].get("key_email") or ""),
        )
    )

    keys_payload = []
    for expiry_date, key in active_keys[:max_keys]:
        tariff = key.get("subscription_tier") or classify_product_type(key.get("host_name"))
        key_payload = {
            "key_id": key.get("key_id"),
            "host_name": key.get("host_name"),
            "key_email": key.get("key_email"),
            "tariff": tariff,
            "xui_client_uuid": key.get("xui_client_uuid"),
            "expiry_date": expiry_date,
            "created_date": _to_iso_datetime(key.get("created_date")),
        }

        issued_key_value = key.get("issued_key_value")
        if issued_key_value:
            key_payload["issued_key_value"] = _truncate_text(issued_key_value, max_length=2048)
            key_payload["issued_key_value_truncated"] = len(str(issued_key_value)) > 2048

        keys_payload.append(key_payload)

    payments_payload = []
    for transaction in paid_transactions:
        tx_meta = _parse_transaction_metadata(transaction.get("metadata"))
        payments_payload.append(
            {
                "payment_id": transaction.get("payment_id"),
                "status": transaction.get("status"),
                "payment_method": transaction.get("payment_method"),
                "amount_rub": transaction.get("amount_rub"),
                "currency_name": transaction.get("currency_name"),
                "amount_currency": transaction.get("amount_currency"),
                "created_date": _to_iso_datetime(transaction.get("created_date")),
                "plan_name": tx_meta.get("plan_name"),
                "host_name": tx_meta.get("host_name"),
                "action": tx_meta.get("action"),
            }
        )

    additional_attrs = {
        "pb_user": {
            "telegram_id": user_data.get("telegram_id", user_id),
            "username": user_data.get("username"),
            "registration_date": _to_iso_datetime(user_data.get("registration_date")),
            "agreed_to_terms": bool(user_data.get("agreed_to_terms")),
            "trial_used": bool(user_data.get("trial_used")),
            "trial_super_used": bool(user_data.get("trial_super_used")),
            "blitz_refresh_used": bool(user_data.get("blitz_refresh_used")),
            "total_months": int(user_data.get("total_months") or 0),
            "referral_balance": float(user_data.get("referral_balance") or 0.0),
            "referral_balance_all": float(user_data.get("referral_balance_all") or 0.0),
            "referred_by": user_data.get("referred_by"),
        },
        "pb_keys": keys_payload,
        "pb_payments": payments_payload,
        "pb_summary": {
            "keys_total": len(keys_payload),
            "keys_attached": len(keys_payload),
            "paid_payments_attached": len(payments_payload),
            "updated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        },
    }

    if latest_paid_transaction:
        latest_tx_meta = _parse_transaction_metadata(latest_paid_transaction.get("metadata"))
        additional_attrs["last_payment"] = {
            "host": latest_tx_meta.get("host_name"),
            "method": latest_paid_transaction.get("payment_method"),
            "date": _to_iso_datetime(latest_paid_transaction.get("created_date")),
        }

    return additional_attrs


def _format_chatwoot_tariff(value) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"super", "супер"}:
        return "Супер"
    return "Стандарт"


def _build_chatwoot_custom_attributes(additional_attrs: dict) -> dict:
    pb_user = additional_attrs.get("pb_user") or {}
    pb_keys = additional_attrs.get("pb_keys") or []
    pb_payments = additional_attrs.get("pb_payments") or []
    last_payment = additional_attrs.get("last_payment") or {}

    key_hosts = []
    key_emails = []
    for key in pb_keys:
        host_name = str(key.get("host_name") or "").strip()
        key_email = str(key.get("key_email") or "").strip()
        if host_name:
            key_hosts.append(host_name)
        if key_email:
            key_emails.append(key_email)

    custom_attrs = {
        "pb_telegram_id": pb_user.get("telegram_id"),
        "pb_username": pb_user.get("username"),
        "pb_registration_date": pb_user.get("registration_date"),
        "pb_agreed_to_terms": bool(pb_user.get("agreed_to_terms")),
        "pb_total_months": pb_user.get("total_months"),
        "pb_referral_balance": pb_user.get("referral_balance"),
        "pb_referral_balance_all": pb_user.get("referral_balance_all"),
        "pb_key_hosts": ", ".join(key_hosts[:10]) or None,
        "pb_key_emails": ", ".join(key_emails[:10]) or None,
        "pb_last_payment_host": last_payment.get("host"),
        "pb_last_payment_method": last_payment.get("method"),
        "pb_last_payment_date": last_payment.get("date"),
        "pb_paid_payments_count": len(pb_payments),
        "pb_updated_at": (additional_attrs.get("pb_summary") or {}).get("updated_at"),
    }

    for index, key in enumerate(pb_keys[:10], start=1):
        key_email = str(key.get("key_email") or "").strip()
        key_value = str(key.get("issued_key_value") or key_email or f"key_id:{key.get('key_id')}").strip()
        tariff = _format_chatwoot_tariff(key.get("tariff"))
        expiry_date = str(key.get("expiry_date") or "").strip()
        expiry_display = expiry_date.split("T", 1)[0] if expiry_date else ""
        custom_attrs[f"pb_slot_tariff_{index}"] = tariff
        custom_attrs[f"pb_slot_key_{index}"] = _truncate_text(key_value, max_length=2048)
        custom_attrs[f"pb_slot_date_{index}"] = expiry_display

    return {key: value for key, value in custom_attrs.items() if value is not None and value != ""}


def _active_word(count: int) -> str:
    return "активный" if count % 10 == 1 and count % 100 != 11 else "активных"


def _build_vpn_status_from_count(key_count: int, expired_key_count: int = 0) -> dict:
    if key_count <= 0:
        if expired_key_count > 0:
            return {
                "vpn_status_label": "Есть просроченные",
                "vpn_status_class": "status-vpn-expired",
            }
        return {
            "vpn_status_label": "Нет ключа",
            "vpn_status_class": "status-vpn-none",
        }

    return {
        "vpn_status_label": f"{key_count} {_active_word(key_count)}",
        "vpn_status_class": "status-vpn-active",
    }


def _build_users_live_signature(
    users: list[dict],
    *,
    total_users: int,
    current_page: int,
    total_pages: int,
) -> str:
    normalized_rows: list[dict[str, object]] = []
    for user in users:
        try:
            key_count = int(user.get("key_count") or 0)
        except (TypeError, ValueError):
            key_count = 0
        try:
            expired_key_count = int(user.get("expired_key_count") or 0)
        except (TypeError, ValueError):
            expired_key_count = 0
        normalized_rows.append(
            {
                "telegram_id": int(user.get("telegram_id") or 0),
                "is_banned": int(user.get("is_banned") or 0),
                "key_count": key_count,
                "expired_key_count": expired_key_count,
                "registration_date": str(user.get("registration_date") or ""),
            }
        )
    payload = {
        "total_users": int(total_users or 0),
        "current_page": int(current_page or 1),
        "total_pages": int(total_pages or 1),
        "rows": normalized_rows,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_dashboard_live_signature(**snapshot) -> str:
    """Cover every displayed value, including date boundaries and chart series."""
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_pagination_items(current_page: int, total_pages: int) -> list[int | None]:
    if total_pages <= 1:
        return [1]

    pages: set[int] = {1, total_pages}
    for candidate in range(current_page - 2, current_page + 3):
        if 1 <= candidate <= total_pages:
            pages.add(candidate)

    sorted_pages = sorted(pages)
    items: list[int | None] = []
    prev_page = 0
    for page in sorted_pages:
        if prev_page and page - prev_page > 1:
            items.append(None)
        items.append(page)
        prev_page = page
    return items


def _parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone().replace(tzinfo=None) if value.tzinfo else value

    raw = str(value).strip()
    if not raw:
        return None

    normalized = raw.replace("T", " ")
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        try:
            parsed = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                parsed = datetime.strptime(normalized, "%Y-%m-%d")
            except ValueError:
                return None

    return parsed.astimezone().replace(tzinfo=None) if parsed.tzinfo else parsed


def _ru_date(value) -> str:
    date_value = _parse_datetime(value)
    if date_value is None:
        return "—"

    has_time = any([date_value.hour, date_value.minute, date_value.second])
    return date_value.strftime("%d.%m.%Y %H:%M" if has_time else "%d.%m.%Y")


def _referral_date_moscow(value) -> str:
    date_value = _parse_datetime(value)
    if date_value is None:
        return "—"
    if date_value.tzinfo is None:
        date_value = date_value.replace(tzinfo=UTC)
    return date_value.astimezone(_DASHBOARD_MOSCOW_TZ).strftime("%d.%m.%Y %H:%M МСК")


def _payment_date_moscow(transaction: dict) -> str:
    """Use the same payment instant and timezone policy as the dashboard ledger."""
    metadata = _parse_transaction_metadata(transaction.get("metadata"))
    row = {
        **transaction,
        "paid_at": metadata.get("paid_at"),
        "payment_confirmed_at": metadata.get("payment_confirmed_at"),
    }
    instant, _ = payment_time_moscow(row)
    return instant.strftime("%d.%m.%Y %H:%M:%S МСК") if instant else "Дата неизвестна"


def _is_key_expired(expiry_value, *, is_archived: bool = False) -> bool:
    if is_archived:
        return True

    expiry_date = _parse_datetime(expiry_value)
    if expiry_date is None:
        return False

    now = datetime.now(expiry_date.tzinfo) if expiry_date.tzinfo else datetime.now()
    return expiry_date <= now


def _normalize_date_query(value: str | None) -> str | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    return parsed.strftime("%Y-%m-%d")


def _parse_dashboard_period_arg(value: str | None) -> int | str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"today", "yesterday", "month", "previous_month"}:
        return normalized
    try:
        return int(normalized)
    except (TypeError, ValueError):
        return 30


def _resolve_dashboard_period(
    *,
    period_value: int | str | None,
    raw_date_from: str | None,
    raw_date_to: str | None,
) -> dict:
    today = datetime.now(_DASHBOARD_MOSCOW_TZ).date()
    date_from = _normalize_date_query(raw_date_from)
    date_to = _normalize_date_query(raw_date_to)
    custom = bool(date_from or date_to)
    selected = period_value if period_value in {7, 30, 90, "today", "yesterday", "month", "previous_month"} else "month"
    if custom:
        date_from = date_from or date_to
        date_to = date_to or date_from
        start = datetime.strptime(min(date_from, date_to), "%Y-%m-%d").date()
        end = datetime.strptime(max(date_from, date_to), "%Y-%m-%d").date()
        start = max(start, end - timedelta(days=365))
        selected = "range"
    elif selected == "today":
        start = end = today
    elif selected == "yesterday":
        start = end = today - timedelta(days=1)
    elif selected == "previous_month":
        end = today.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif selected == "month":
        start, end = today.replace(day=1), today
    else:
        start, end = today - timedelta(days=int(selected) - 1), today
    date_from, date_to = start.isoformat(), end.isoformat()
    return {
        "custom_range_active": custom,
        "default_range_active": period_value is None and not custom,
        "selected_period": selected,
        "preset_period": selected,
        "period_days": (end - start).days + 1,
        "date_from": date_from,
        "date_to": date_to,
        "range_start": date_from,
        "range_end": date_to,
        "caption": f"с {_ru_date(date_from)} по {_ru_date(date_to)} · МСК",
    }


def _number_format(value) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return "0"

    if number.is_integer():
        return f"{int(number):,}".replace(",", " ")
    return f"{number:,.2f}".replace(",", " ")


_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_ATTEMPTS_MAX_IPS = 2048

# YooKassa official IP ranges — https://yookassa.ru/developers/using-api/webhooks
_YOOKASSA_IP_PREFIXES = (
    "185.71.76.",
    "185.71.77.",
    "77.75.153.",
    "77.75.154.",
    "77.75.156.",
)
_YOOKASSA_IPV6_NETWORK = ipaddress.ip_network("2a02:5180::/32")
_DEFAULT_TRUSTED_PROXY_CIDRS = (
    "127.0.0.1/32",
    "::1/128",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
)


def _is_yookassa_ip(ip: str | None) -> bool:
    if not ip:
        return False
    if any(ip.startswith(prefix) for prefix in _YOOKASSA_IP_PREFIXES):
        return True
    try:
        return ipaddress.ip_address(ip) in _YOOKASSA_IPV6_NETWORK
    except ValueError:
        return False


def _normalize_forwarded_ip(raw_value: str | None) -> str | None:
    value = str(raw_value or "").strip().strip('"').strip("'")
    if not value:
        return None
    if value.lower().startswith("for="):
        value = value.split("=", 1)[1].strip().strip('"').strip("'")
    if value.startswith("[") and "]" in value:
        value = value[1 : value.index("]")]
    elif value.count(":") == 1 and "." in value:
        value = value.split(":", 1)[0].strip()
    return value or None


def _load_trusted_proxy_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    raw_cidrs = str(os.environ.get("TRUSTED_PROXY_CIDRS", "")).strip()
    cidrs = [part.strip() for part in raw_cidrs.split(",") if part.strip()]
    if not cidrs:
        cidrs = list(_DEFAULT_TRUSTED_PROXY_CIDRS)

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for cidr in cidrs:
        try:
            networks.append(ipaddress.ip_network(cidr, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid TRUSTED_PROXY_CIDRS entry: %s", cidr)
    return networks


def _is_trusted_proxy_ip(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(ip_obj in network for network in _load_trusted_proxy_networks())


def _extract_yookassa_candidate_ips() -> list[str]:
    candidates: list[str] = []
    remote_addr = _normalize_forwarded_ip(request.remote_addr)

    # Trust forwarded headers only from known reverse proxies.
    # This blocks direct X-Forwarded-For spoofing by external clients.
    if _is_trusted_proxy_ip(remote_addr):
        x_real_ip = _normalize_forwarded_ip(request.headers.get("X-Real-IP"))
        if x_real_ip:
            candidates.append(x_real_ip)

        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            # Nginx appends remote_addr to XFF, so right-most entry is the
            # closest trusted hop and safer than left-most client-controlled value.
            for part in reversed([piece.strip() for piece in xff.split(",") if piece.strip()]):
                normalized = _normalize_forwarded_ip(part)
                if normalized:
                    candidates.append(normalized)
                    break

        forwarded = request.headers.get("Forwarded", "")
        if forwarded:
            for hop in reversed([item.strip() for item in forwarded.split(",") if item.strip()]):
                for segment in reversed([item.strip() for item in hop.split(";") if item.strip()]):
                    if segment.lower().startswith("for="):
                        normalized = _normalize_forwarded_ip(segment)
                        if normalized:
                            candidates.append(normalized)
                            break
                else:
                    continue
                break

        for route_ip in reversed(request.access_route or []):
            normalized = _normalize_forwarded_ip(route_ip)
            if normalized:
                candidates.append(normalized)
                break

    if remote_addr:
        candidates.append(remote_addr)

    unique_ips: list[str] = []
    seen: set[str] = set()
    for raw_ip in candidates:
        normalized = _normalize_forwarded_ip(raw_ip)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_ips.append(normalized)
    return unique_ips


def _resolve_trusted_yookassa_ip() -> tuple[str | None, list[str]]:
    candidate_ips = _extract_yookassa_candidate_ips()
    for candidate in candidate_ips:
        if _is_yookassa_ip(candidate):
            return candidate, candidate_ips
    return None, candidate_ips


def _log_payment_lost(provider: str, payment_id: str | None, reason: str, metadata: dict | None = None) -> None:
    provider_name = str(provider or "unknown").strip()
    pid = str(payment_id or "unknown").strip() or "unknown"
    detail = f"source={BOT_SOURCE} provider={provider_name} payment_id={pid} reason={reason}"
    if isinstance(metadata, dict):
        runtime_bot_source = normalize_bot_source(BOT_SOURCE) or "main"
        metadata_source = normalize_bot_source(metadata.get("bot_source"))
        if metadata_source:
            detail += f" payment_source={metadata_source}"
        detail += f" runtime_source={runtime_bot_source}"
        detail += f" metadata_keys={','.join(sorted(metadata.keys()))}"
    logger.error("PAYMENT LOST: %s", detail)
    try:
        send_critical_alert(
            BotError.PAYMENT_KEY_ISSUE_FAILED,
            details=detail,
            respect_cooldown=False,
        )
    except Exception as alert_error:
        logger.warning("PAYMENT LOST alert failed: %s", alert_error)


def _mark_retryable_payment_dispatch_failure(payment_id: str | None, reason: str, metadata: dict | None) -> None:
    """
    Persist metadata markers so scheduler can auto-retry issuance and later send recovered alert.

    This is used for transient dispatch failures where payment is already confirmed
    but immediate processing could not be started (e.g. bot/event loop unavailable).
    """
    pid = str(payment_id or "").strip()
    if not pid or not isinstance(metadata, dict):
        return

    updated_metadata = normalize_payment_metadata(metadata) or {}
    previous_count = int(updated_metadata.get("issuance_failure_count") or 0)
    updated_metadata["external_payment_id"] = str(updated_metadata.get("external_payment_id") or pid)
    updated_metadata["payment_confirmed"] = True
    updated_metadata["issuance_failed"] = True
    updated_metadata["issuance_failure_alert_sent"] = True
    updated_metadata["issuance_failure_reason"] = str(
        updated_metadata.get("issuance_failure_reason") or reason or "dispatch_unavailable"
    )
    updated_metadata.pop("autoretry_exhausted_alert_sent", None)
    updated_metadata.pop("autoretry_exhausted_alert_at", None)
    if not updated_metadata.get("issuance_failure_first_at"):
        updated_metadata["issuance_failure_first_at"] = datetime.now().isoformat(timespec="seconds")
    if not updated_metadata.get("retry_source"):
        updated_metadata["retry_source"] = "pending_confirmed_retry"

    failure_logs = updated_metadata.get("issuance_failure_logs")
    if not isinstance(failure_logs, dict):
        failure_logs = {}
    failure_logs["stage"] = "webhook_dispatch"
    failure_logs["dispatch_reason"] = str(reason or "unknown")
    updated_metadata["issuance_failure_logs"] = failure_logs

    metadata_updated = update_transaction_metadata(
        pid,
        updated_metadata,
        statuses=("pending", "processing"),
    )

    retry_state = mark_transaction_retryable_failure(
        pid,
        reason=str(reason or "dispatch_unavailable"),
        metadata=updated_metadata,
        payment_method=str(updated_metadata.get("payment_method") or ""),
        next_retry_at=(datetime.now() if previous_count == 0 else None),
        statuses=("pending", "processing"),
    )
    retry_count = int(retry_state.get("retry_count") or previous_count + 1)
    next_retry_at = retry_state.get("next_retry_at") or "auto"

    if retry_state.get("updated") or metadata_updated:
        send_critical_alert(
            BotError.PAYMENT_KEY_ISSUE_RETRYING,
            details=(
                f"source={BOT_SOURCE} provider=webhook payment_id={pid} "
                f"reason={updated_metadata.get('issuance_failure_reason')} "
                f"retry_count={retry_count} next_retry_at={next_retry_at}"
            ),
            respect_cooldown=True,
        )
        return

    # Defensive fallback: if row is missing, recreate pending row so scheduler can pick it up.
    try:
        user_id = int(updated_metadata.get("user_id"))
    except (TypeError, ValueError):
        return
    try:
        amount_rub = float(updated_metadata.get("price") or 0.0)
    except (TypeError, ValueError):
        amount_rub = 0.0

    created_id = create_pending_transaction(
        payment_id=pid,
        user_id=user_id,
        amount_rub=amount_rub,
        metadata=updated_metadata,
    )
    if created_id:
        retry_state = mark_transaction_retryable_failure(
            pid,
            reason=str(reason or "dispatch_unavailable"),
            metadata=updated_metadata,
            payment_method=str(updated_metadata.get("payment_method") or ""),
            statuses=("pending", "processing"),
        )
        send_critical_alert(
            BotError.PAYMENT_KEY_ISSUE_RETRYING,
            details=(
                f"source={BOT_SOURCE} provider=webhook payment_id={pid} "
                f"reason={updated_metadata.get('issuance_failure_reason')} "
                f"retry_count={int(retry_state.get('retry_count') or previous_count + 1)} "
                f"next_retry_at={retry_state.get('next_retry_at') or 'auto'}"
            ),
            respect_cooldown=True,
        )


def _verify_cryptobot_signature(raw_body: bytes, header_signature: str | None) -> bool:
    if not header_signature:
        return False
    token = (get_setting("cryptobot_token") or "").strip()
    if not token:
        return False
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return compare_digest(expected, header_signature.strip())


def _metadata_bot_source_matches_runtime(metadata: dict | None) -> bool:
    return _payment_metadata_bot_source_matches_runtime(
        metadata or {},
        normalize_bot_source(BOT_SOURCE) or "main",
    )


def _warn_wrong_bot_instance(
    provider: str, payment_id: str, metadata: dict | None, *, processing_started: bool = False
) -> None:
    normalized_metadata = normalize_payment_metadata(metadata)
    payment_source = normalize_bot_source(normalized_metadata.get("bot_source")) or "unknown"
    runtime_bot_source = normalize_bot_source(BOT_SOURCE) or "main"
    logger.warning(
        "%s webhook ignored: payment_id=%s belongs to bot_source=%s, runtime BOT_SOURCE=%s.",
        provider,
        payment_id,
        payment_source,
        runtime_bot_source,
    )
    _log_payment_lost(provider, payment_id, "wrong_bot_instance", normalized_metadata)
    if processing_started:
        reset_transaction_to_pending(payment_id)


def _schedule_payment_processing(
    provider: str,
    payment_id: str,
    payment_processor,
    bot,
    metadata: dict,
    loop,
    *,
    processing_started: bool = False,
):
    future = asyncio.run_coroutine_threadsafe(payment_processor(bot, metadata), loop)
    if not hasattr(future, "add_done_callback"):
        return future

    def _on_done(done_future):
        try:
            done_future.result()
        except Exception as exc:
            logger.error(
                "%s webhook async payment processor crashed for payment_id=%s: %s",
                provider,
                payment_id,
                exc,
                exc_info=True,
            )
            _mark_retryable_payment_dispatch_failure(
                payment_id,
                f"async_processor_crashed:{type(exc).__name__}",
                metadata,
            )
            if processing_started and is_transaction_processing(payment_id):
                reset_transaction_to_pending(payment_id)

    future.add_done_callback(_on_done)
    return future


def _dispatch_confirmed_payment(
    provider: str,
    payment_id: str,
    metadata: dict,
    *,
    processing_started: bool = False,
) -> tuple[str, int]:
    """Validate final metadata, check bot_source match, acquire bot/loop and dispatch.

    Returns an HTTP response tuple so callers can ``return _dispatch_confirmed_payment(...)``.
    """
    import uuid as _uuid
    from datetime import datetime as _dt

    metadata["trace_id"] = _uuid.uuid4().hex[:12]
    metadata.setdefault("payment_confirmed_at", _dt.utcnow().isoformat(timespec="seconds"))

    missing_fields = missing_required_payment_metadata_fields(metadata)
    if missing_fields:
        logger.error(
            "%s webhook ignored: payment_id=%s metadata is incomplete. fields=%s",
            provider,
            payment_id,
            ",".join(missing_fields),
        )
        _log_payment_lost(
            provider,
            payment_id,
            f"metadata_incomplete_after_merge:{','.join(missing_fields)}",
            metadata,
        )
        if processing_started:
            reset_transaction_to_pending(payment_id)
        return "OK", 200

    if not _metadata_bot_source_matches_runtime(metadata):
        _warn_wrong_bot_instance(provider, payment_id, metadata, processing_started=processing_started)
        return "OK", 200

    bot = _bot_controller.get_bot_instance() if _bot_controller else None
    loop = current_app.config.get("EVENT_LOOP")
    payment_processor = getattr(handlers, "process_successful_payment", None)

    if not bot or not payment_processor or not loop or not loop.is_running():
        reason = (
            "bot_unavailable"
            if not bot
            else "payment_processor_unavailable"
            if not payment_processor
            else "event_loop_unavailable"
        )
        logger.error(
            "%s webhook: %s for payment_id=%s. Returning to pending.",
            provider,
            reason,
            payment_id,
        )
        _mark_retryable_payment_dispatch_failure(payment_id, reason, metadata)
        if processing_started:
            reset_transaction_to_pending(payment_id)
        return "OK", 200

    _schedule_payment_processing(
        provider,
        payment_id,
        payment_processor,
        bot,
        metadata,
        loop,
        processing_started=processing_started,
    )
    return "OK", 200


def _hash_password(plain: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        import secrets as _secrets

        salt = _secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt.encode("utf-8"), 100_000)
    return digest.hex(), salt


def _verify_password(plain: str, stored_hash: str, stored_salt: str) -> bool:
    digest, _ = _hash_password(plain, stored_salt)
    return compare_digest(digest, stored_hash)


def _is_password_hashed(value: str | None) -> bool:
    if not value:
        return False
    return value.startswith("pbkdf2:")


def _parse_hashed_password(value: str) -> tuple[str, str] | None:
    if not value or not value.startswith("pbkdf2:"):
        return None
    parts = value.split(":", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]


def _format_hashed_password(digest: str, salt: str) -> str:
    return f"pbkdf2:{salt}:{digest}"


def _prune_login_attempts(now: float | None = None) -> None:
    current_time = time.time() if now is None else now
    for key, attempts in list(_LOGIN_ATTEMPTS.items()):
        fresh_attempts = [t for t in attempts if current_time - t < _LOGIN_WINDOW_SECONDS]
        if fresh_attempts:
            _LOGIN_ATTEMPTS[key] = fresh_attempts
        else:
            _LOGIN_ATTEMPTS.pop(key, None)

    overflow = len(_LOGIN_ATTEMPTS) - _LOGIN_ATTEMPTS_MAX_IPS
    if overflow <= 0:
        return

    oldest_ips = sorted(
        _LOGIN_ATTEMPTS,
        key=lambda key: max(_LOGIN_ATTEMPTS.get(key) or [0.0]),
    )[:overflow]
    for key in oldest_ips:
        _LOGIN_ATTEMPTS.pop(key, None)


def _check_login_rate(ip: str) -> bool:
    now = time.time()
    _prune_login_attempts(now)
    attempts = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS]
    if attempts:
        _LOGIN_ATTEMPTS[ip] = attempts
    else:
        _LOGIN_ATTEMPTS.pop(ip, None)
    return len(attempts) < _LOGIN_MAX_ATTEMPTS


def _record_login_attempt(ip: str) -> None:
    now = time.time()
    _prune_login_attempts(now)
    attempts = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS]
    attempts.append(now)
    _LOGIN_ATTEMPTS[ip] = attempts
    _prune_login_attempts(now)


def _clear_login_attempts(ip: str) -> None:
    _LOGIN_ATTEMPTS.pop(ip, None)


def create_webhook_app(bot_controller_instance):
    global _bot_controller
    _bot_controller = bot_controller_instance

    # Ensure logging is configured when Flask app is created (e.g. when run without __main__)
    root = logging.getLogger()
    if not root.handlers:
        from shop_bot.logging_config import setup_logging

        setup_logging()

    app_file_path = os.path.abspath(__file__)
    app_dir = os.path.dirname(app_file_path)
    template_dir = os.path.join(app_dir, "templates")

    logger.debug(
        "Diagnostic: cwd=%s app_dir=%s template_dir=%s login_exists=%s",
        os.getcwd(),
        app_dir,
        template_dir,
        os.path.isfile(os.path.join(template_dir, "login.html")),
    )

    flask_app = Flask(__name__, template_folder="templates", static_folder="static")
    register_subscription_routing(flask_app, get_setting)

    configured_secret = (os.environ.get("FLASK_SECRET_KEY") or get_setting("flask_secret_key") or "").strip()
    if not configured_secret:
        configured_secret = os.urandom(32).hex()
        logger.error(
            "Flask SECRET_KEY is not configured (FLASK_SECRET_KEY or bot_settings.flask_secret_key). "
            "Using ephemeral key; sessions will reset on restart."
        )

    flask_app.config["SECRET_KEY"] = configured_secret
    flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
    flask_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    flask_app.config["SESSION_COOKIE_SECURE"] = _is_truthy(os.environ.get("FLASK_SESSION_COOKIE_SECURE", "true"))
    flask_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
    flask_app.config["APP_VERSION"] = os.environ.get("APP_VERSION", "1.0.0")
    flask_app.jinja_env.filters["ru_date"] = _ru_date
    flask_app.jinja_env.filters["referral_date"] = _referral_date_moscow
    flask_app.jinja_env.filters["number_format"] = _number_format
    flask_app.teardown_appcontext(close_db)
    register_panel_access(flask_app, get_setting=get_setting)

    @flask_app.context_processor
    def inject_current_year():
        return {"current_year": datetime.now(UTC).year}

    @flask_app.after_request
    def apply_no_store_headers(response):
        endpoint = str(request.endpoint or "")
        if request.method == "GET" and endpoint and endpoint != "static":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    def login_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if session.get("logged_in") is not True:
                return redirect(url_for("login_page"))
            return f(*args, **kwargs)

        return decorated_function

    referral_setting_keys = (
        "enable_referrals", "referral_percentage", "referral_discount", "minimum_withdrawal",
    )

    def referral_csrf_token():
        if not session.get("referral_csrf_token"):
            session["referral_csrf_token"] = secrets.token_urlsafe(32)
        return session["referral_csrf_token"]

    def referral_csrf_valid():
        expected = session.get("referral_csrf_token")
        submitted = request.form.get("csrf_token", "")
        return bool(expected and submitted and compare_digest(expected.encode("utf-8"), submitted.encode("utf-8")))

    def referral_form_settings(*, preserve_missing=False):
        values = get_all_settings() if preserve_missing else {}
        return referrals.validate_settings({
            key: request.form.getlist(key)[-1] if key in request.form else values.get(key)
            for key in referral_setting_keys
        })

    def _resolve_runtime_health_payload() -> dict[str, object]:
        try:
            if _bot_controller is None:
                raw_status = {}
            else:
                try:
                    raw_status = _bot_controller.get_status(include_health=True)
                except TypeError:
                    raw_status = _bot_controller.get_status()
        except Exception:
            logger.exception("Failed to resolve runtime health status")
            raw_status = {}

        if not isinstance(raw_status, dict):
            raw_status = {}
        running = bool(raw_status.get("shop_bot_running", False))
        healthy = bool(raw_status.get("shop_bot_healthy", running))
        health_message = str(raw_status.get("shop_bot_health_message") or ("ok" if healthy else "unknown"))
        return {
            "shop_bot_running": running,
            "shop_bot_healthy": healthy,
            "shop_bot_health_message": health_message,
        }

    @flask_app.route("/health/live")
    def health_live():
        """Liveness probe: process is alive and can serve HTTP."""
        return {"status": "ok", "probe": "live"}, 200

    @flask_app.route("/health/ready")
    def health_ready():
        """Readiness probe: runtime bot is started and healthy."""
        payload = _resolve_runtime_health_payload()
        ready = bool(payload["shop_bot_running"]) and bool(payload["shop_bot_healthy"])
        return {
            "status": "ok" if ready else "not_ready",
            "probe": "ready",
            **payload,
        }, (200 if ready else 503)

    @flask_app.route("/health")
    def health_check():
        """Backward-compatible health endpoint (alias to liveness)."""
        return health_live()

    def _happ_profile_title_header(subscription_tier: str | None) -> str | None:
        display_name = xui_api.subscription_display_name_for_tier(subscription_tier)
        if not display_name:
            return None
        encoded = base64.b64encode(display_name.encode("utf-8")).decode("ascii")
        return f"base64:{encoded}"

    def _happ_profile_title_name(subscription_tier: str | None) -> str | None:
        return xui_api.subscription_display_name_for_tier(subscription_tier)

    def _subscription_plain_text_response(payload: str, *, subscription_tier: str | None = None):
        policy_host = getattr(g, "subscription_host_name", "")
        expected_labels = (
            _expected_subscription_labels(policy_host, subscription_tier or "super")
            if subscription_policy.get_policy(policy_host) else None
        )
        payload = subscription_policy.filter_subscription_payload(
            policy_host, payload, subscription_tier, expected_labels=expected_labels
        )
        if not payload:
            return "Subscription unavailable", 502
        payload = append_clearway_ddg_hosts(payload, policy_host, subscription_tier)
        response = current_app.response_class(
            str(payload or "").strip() + "\n",
            status=200,
            mimetype="text/plain",
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        profile_title = _happ_profile_title_header(subscription_tier)
        profile_name = _happ_profile_title_name(subscription_tier)
        if profile_title:
            response.headers["Profile-Title"] = profile_title
        if profile_name:
            response.headers["Content-Disposition"] = f'attachment; filename="{profile_name}.txt"'
        return response

    def _managed_subscription_url(sub_id: object, *, subscription_tier: str | None = None) -> str | None:
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

    def _issued_key_value_from_panel_result(
        panel_result: dict,
        *,
        subscription_tier: str | None = None,
        fallback_value: str | None = None,
    ) -> str:
        managed_url = None
        if xui_api.normalize_subscription_tier(subscription_tier):
            managed_url = _managed_subscription_url(panel_result.get("sub_id"), subscription_tier=subscription_tier)
        return (
            managed_url
            or str(panel_result.get("subscription_link") or "").strip()
            or str(panel_result.get("connection_string") or "").strip()
            or str(fallback_value or "").strip()
        )

    def _safe_fallback_subscription_payload(raw_payload, *, subscription_tier: str | None = None) -> str | None:
        return xui_api.normalize_subscription_payload(raw_payload)

    def _is_managed_subscription_url(value: str) -> bool:
        parsed = urlparse(str(value or "").strip())
        normalized_path = parsed.path.rstrip("/")
        return normalized_path.endswith("/podpiska") or "/podpiska/" in normalized_path

    def _valid_direct_subscription_url(value: str) -> str | None:
        url = str(value or "").strip()
        if not url:
            return None

        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            return None
        if _is_managed_subscription_url(url):
            return None
        return url

    def _build_direct_subscription_url_from_base(base_url: str, sub_id: str) -> str | None:
        normalized_base = str(base_url or "").strip().rstrip("/")
        if not _valid_direct_subscription_url(normalized_base):
            return None

        return f"{normalized_base}/{quote(str(sub_id).strip().strip('/'), safe='')}"

    def _direct_subscription_urls(host_data: dict, key_data: dict, sub_id: str) -> list[str]:
        candidates = [
            _build_direct_subscription_url_from_base(
                str(host_data.get("host_subscription_base_url") or ""),
                sub_id,
            ),
            _valid_direct_subscription_url(str(key_data.get("issued_key_value") or "")),
        ]
        urls: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if candidate and candidate not in seen:
                seen.add(candidate)
                urls.append(candidate)
        return urls

    def _fetch_direct_subscription_payload(
        host_data: dict,
        key_data: dict,
        sub_id: str,
        *,
        subscription_tier: str | None = None,
    ) -> str | None:
        upstream_urls = _direct_subscription_urls(host_data, key_data, sub_id)
        if not upstream_urls:
            return None

        host_name = str(host_data.get("host_name") or "").strip()
        for upstream_url in upstream_urls:
            # upstream_url comes only from _valid_direct_subscription_url(): http(s), netloc, non-managed path.
            request_obj = urllib.request.Request(  # noqa: S310, RUF100
                upstream_url,
                headers={
                    "Accept": "text/plain, application/octet-stream, */*",
                    "User-Agent": "managed-subscription-client/1.0",
                },
            )
            try:
                with urllib.request.urlopen(  # noqa: S310, RUF100
                    request_obj,
                    timeout=MANAGED_SUBSCRIPTION_UPSTREAM_TIMEOUT_SECONDS,
                ) as response:
                    status = int(response.getcode() or 0)
                    if status < 200 or status >= 300:
                        logger.warning(
                            "Direct subscription upstream returned HTTP %s for host=%s.",
                            status,
                            host_name,
                        )
                        continue
                    raw_payload = response.read(MANAGED_SUBSCRIPTION_UPSTREAM_MAX_BYTES + 1)
            except urllib.error.HTTPError as exc:
                logger.warning(
                    "Direct subscription upstream returned HTTP %s for host=%s.",
                    exc.code,
                    host_name,
                )
                continue
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.warning("Direct subscription upstream failed for host=%s: %s", host_name, exc)
                continue

            if len(raw_payload) > MANAGED_SUBSCRIPTION_UPSTREAM_MAX_BYTES:
                logger.warning("Direct subscription upstream payload is too large for host=%s.", host_name)
                continue

            decoded_payload = raw_payload.decode("utf-8", errors="replace").strip()
            normalized_payload = _safe_fallback_subscription_payload(
                decoded_payload,
                subscription_tier=subscription_tier,
            )
            if normalized_payload:
                return normalized_payload

        logger.warning("Direct subscription upstream payload is not a recognized subscription for host=%s.", host_name)
        return None

    def _stored_connection_payload(key_data: dict, *, subscription_tier: str | None = None) -> str | None:
        issued_value = str(key_data.get("issued_key_value") or "").strip()
        if not issued_value.lower().startswith(_SUBSCRIPTION_CONNECTION_PREFIXES):
            return None
        return _safe_fallback_subscription_payload(issued_value, subscription_tier=subscription_tier)

    def _key_subscription_tier(key_data: dict) -> str | None:
        return xui_api.normalize_subscription_tier(key_data.get("subscription_tier"))

    def _key_includes_white_subscription(key_data: dict) -> bool:
        # Standard trials include the same White routes with their own 5 GiB cap.
        return not xui_api.standard_trial_is_ordinary_only(key_data)

    def _expected_subscription_labels(host_name: str, subscription_tier: str | None) -> list[str]:
        labels = xui_api.mainpanel_expected_subscription_labels(host_name, subscription_tier)
        if getattr(g, "subscription_include_white", True):
            return labels
        return [label for label in labels if not _payload_has_white_marker(label)]

    def _payload_has_white_marker(payload: str | None) -> bool:
        return bool(re.search(r"white|whitelist|обход\s+бел", str(payload or ""), flags=re.IGNORECASE))

    def _payload_uses_only_display_name(payload: str | None, display_name: str | None) -> bool:
        expected_fragment = str(display_name or "").strip()
        normalized_payload = xui_api.normalize_subscription_payload(payload)
        if not expected_fragment or not normalized_payload:
            return False

        fragments: list[str] = []
        for line in normalized_payload.splitlines():
            parsed = urlparse(line)
            if not parsed.fragment:
                return False
            fragments.append(unquote(parsed.fragment).strip())
        return bool(fragments) and all(fragment == expected_fragment for fragment in fragments)

    def _fallback_subscription_payload_for_tier(
        key_data: dict,
        cached_payload: str | None,
        *,
        subscription_tier: str | None,
    ) -> str | None:
        fallback_payload = cached_payload or _stored_connection_payload(key_data, subscription_tier=subscription_tier)
        if not fallback_payload:
            return None
        return fallback_payload

    def _cache_subscription_payload_if_possible(key_data: dict, payload: str) -> None:
        key_id = int(key_data.get("key_id") or 0)
        if key_id and payload:
            update_key_subscription_payload(key_id, payload)

    def _cached_subscription_payload_is_usable(
        key_data: dict,
        host_data: dict | None,
        payload: str | None,
    ) -> bool:
        normalized_payload = xui_api.normalize_subscription_payload(payload)
        if not normalized_payload:
            return False
        if not host_data:
            return True
        host_name = str(key_data.get("host_name") or "").strip()
        if not xui_api.should_include_white_subscription_inbounds(host_data, host_name=host_name):
            return True
        return len(normalized_payload.splitlines()) > 1

    def _cached_tier_subscription_payload_is_usable(
        key_data: dict,
        host_data: dict | None,
        payload: str | None,
        *,
        subscription_tier: str | None,
    ) -> bool:
        normalized_payload = _fallback_subscription_payload_for_tier(
            key_data,
            payload,
            subscription_tier=subscription_tier,
        )
        if not normalized_payload:
            return False
        display_name = xui_api.subscription_display_name_for_tier(subscription_tier)
        if _payload_uses_only_display_name(normalized_payload, display_name):
            return False
        host_name = str(key_data.get("host_name") or "").strip()
        expected_labels = _expected_subscription_labels(host_name, subscription_tier)
        if expected_labels:
            return _cached_mainpanel_payload_for_tier(host_name, subscription_tier, normalized_payload) is not None
        return _cached_subscription_payload_is_usable(key_data, host_data, normalized_payload)

    def _cached_mainpanel_payload_for_tier(host_name: str, key_tier: str | None, payload: str | None) -> str | None:
        if host_name != "MainPanel" or not key_tier or not payload:
            return None

        expected_labels = _expected_subscription_labels(host_name, key_tier)
        payload = subscription_policy.filter_subscription_payload(
            host_name, payload, key_tier, expected_labels=expected_labels
        )
        if not payload:
            return None
        expected_keys = {
            re.sub(r"[^a-z0-9]+", "", str(label or "").lower()) for label in expected_labels if str(label or "").strip()
        }
        links = [line.strip() for line in payload.splitlines() if line.strip()]
        fragment_keys = {
            re.sub(r"[^a-z0-9]+", "", unquote(urlparse(link).fragment).strip().lower())
            for link in links
            if urlparse(link).scheme.lower() in {"vless", "hysteria2", "hy2"}
        }
        if (
            expected_keys
            and fragment_keys == expected_keys
            and len(links) == len(expected_keys)
        ):
            return payload

        return None

    def _mainpanel_panel_payload_by_sub_id(sub_id: str) -> tuple[str | None, str | None]:
        normalized_sub_id = str(sub_id or "").strip().strip("/")
        if not normalized_sub_id:
            return None, None

        host_data = get_host("MainPanel")
        if not host_data:
            return None, None

        try:
            api, primary_inbound = xui_api.login_to_host(
                host_url=host_data["host_url"],
                username=host_data["host_username"],
                password=host_data["host_pass"],
                inbound_id=host_data["host_inbound_id"],
                api_token=xui_api._resolve_host_api_token(host_data),
                prefer_raw_inbound_lookup=True,
            )
            if not api or not primary_inbound:
                return None, None

            raw_inbounds = xui_api._list_inbounds_raw(api)
            has_legacy_sub_id = any(
                normalized_sub_id
                in str(inbound.get("settings") if isinstance(inbound, dict) else getattr(inbound, "settings", ""))
                for inbound in raw_inbounds or []
            )
            if not has_legacy_sub_id:
                return None, None

            node_address_by_id, node_address_by_name = xui_api._node_public_address_maps(api)
            white_node_inbound_ids = xui_api._white_node_inbound_ids_for_panel("MainPanel")
            primary_id = xui_api._safe_inbound_id(primary_inbound)
            if xui_api._host_is_white(host_data) and primary_id > 0:
                white_node_inbound_ids.add(primary_id)
            active_inbounds = xui_api._subscription_inbounds(
                api,
                primary_inbound,
                include_white_inbounds=True,
                white_node_inbound_ids=white_node_inbound_ids,
                raw_inbounds=raw_inbounds or None,
                host_name="MainPanel",
            )
            links = xui_api._build_subscription_links_from_panel_clients(
                api,
                host_data,
                normalized_sub_id,
                active_inbounds,
                raw_inbounds=raw_inbounds or None,
                node_address_by_id=node_address_by_id,
                node_address_by_name=node_address_by_name,
                remark_mode="inbound_label",
            )
        except Exception:
            logger.exception(
                "MainPanel legacy subscription lookup failed for sub_id=%s",
                normalized_sub_id,
            )
            return None, None

        payload = xui_api.normalize_subscription_payload(links)
        if not payload:
            return None, None
        inferred_tier = (
            xui_api.SUBSCRIPTION_TIER_SUPER
            if _payload_has_white_marker(payload)
            else xui_api.SUBSCRIPTION_TIER_STANDARD
        )
        return payload, inferred_tier

    @flask_app.route("/podpiska/<path:sub_id>")
    def managed_subscription_route(sub_id: str):
        g.subscription_host_name = "MainPanel"
        normalized_sub_id = str(sub_id or "").strip().strip("/")
        if not normalized_sub_id:
            return "Not found", 404

        key_data = get_active_key_by_panel_sub_id(normalized_sub_id)
        if not key_data:
            # Do not confuse a known inactive subscription with a legacy key
            # that was never persisted locally.  Otherwise an expired or
            # archived key can be served again from a stale MainPanel client.
            if get_key_by_panel_sub_id(normalized_sub_id):
                return "Not found", 404
            legacy_payload, legacy_tier = _mainpanel_panel_payload_by_sub_id(normalized_sub_id)
            if legacy_payload:
                return _subscription_plain_text_response(legacy_payload, subscription_tier=legacy_tier)
            return "Not found", 404

        g.subscription_host_name = str(key_data.get("host_name") or "").strip()
        g.subscription_include_white = _key_includes_white_subscription(key_data)
        key_tier = _key_subscription_tier(key_data)
        if not key_tier:
            logger.warning(
                "Managed subscription has missing or unknown tier for key_id=%s host=%s",
                key_data.get("key_id"),
                key_data.get("host_name"),
            )
        cached_payload = xui_api.normalize_subscription_payload(key_data.get("subscription_payload"))
        cached_payload = subscription_policy.filter_subscription_payload(
            g.subscription_host_name, cached_payload, key_tier,
            expected_labels=(
                _expected_subscription_labels(g.subscription_host_name, key_tier or "super")
                if subscription_policy.get_policy(g.subscription_host_name) else None
            ),
        )
        if cached_payload and key_tier:
            display_name = xui_api.subscription_display_name_for_tier(key_tier)
            if _payload_uses_only_display_name(cached_payload, display_name) and len(cached_payload.splitlines()) <= 1:
                cached_payload = None
        if not key_tier and cached_payload and len(cached_payload.splitlines()) > 1:
            return _subscription_plain_text_response(cached_payload)

        host_name = str(key_data.get("host_name") or "").strip()
        host_data = get_host(host_name)
        if not host_data:
            stored_payload = _fallback_subscription_payload_for_tier(
                key_data,
                cached_payload,
                subscription_tier=key_tier,
            )
            if stored_payload:
                return _subscription_plain_text_response(stored_payload, subscription_tier=key_tier)
            logger.warning(
                "Managed subscription host not found for key_id=%s host=%s",
                key_data.get("key_id"),
                host_name,
            )
            return "Subscription unavailable", 502

        if not key_tier and _cached_subscription_payload_is_usable(key_data, host_data, cached_payload):
            return _subscription_plain_text_response(cached_payload)
        if key_tier and _cached_tier_subscription_payload_is_usable(
            key_data,
            host_data,
            cached_payload,
            subscription_tier=key_tier,
        ):
            return _subscription_plain_text_response(cached_payload, subscription_tier=key_tier)
        mainpanel_cached_payload = _cached_mainpanel_payload_for_tier(host_name, key_tier, cached_payload)
        if mainpanel_cached_payload:
            return _subscription_plain_text_response(mainpanel_cached_payload, subscription_tier=key_tier)

        if not key_tier:
            direct_payload = _fetch_direct_subscription_payload(host_data, key_data, normalized_sub_id)
            if direct_payload:
                _cache_subscription_payload_if_possible(key_data, direct_payload)
                return _subscription_plain_text_response(direct_payload)
        else:
            expected_labels = _expected_subscription_labels(host_name, key_tier)
            direct_payload = _fetch_direct_subscription_payload(
                host_data,
                key_data,
                normalized_sub_id,
                subscription_tier=key_tier,
            )
            direct_links = xui_api.managed_subscription_links_from_direct_payload(
                direct_payload,
                subscription_tier=key_tier,
                expected_labels=expected_labels,
            )
            direct_managed_payload = xui_api.normalize_subscription_payload(direct_links)
            if direct_managed_payload and host_name != "MainPanel":
                _cache_subscription_payload_if_possible(key_data, direct_managed_payload)
                return _subscription_plain_text_response(direct_managed_payload, subscription_tier=key_tier)

            logger.warning(
                "Managed subscription direct payload is unavailable or incomplete for key_id=%s host=%s tier=%s",
                key_data.get("key_id"),
                host_name,
                key_tier,
            )

        try:
            include_white_inbounds = (
                g.subscription_include_white
                and key_tier in {xui_api.SUBSCRIPTION_TIER_STANDARD, xui_api.SUBSCRIPTION_TIER_SUPER}
                if key_tier
                else xui_api.should_include_white_subscription_inbounds(
                    host_data,
                    host_name=host_name,
                )
            )
            links = xui_api.get_managed_subscription_links(
                host_data,
                normalized_sub_id,
                include_white_inbounds=include_white_inbounds,
                subscription_tier=key_tier,
                remark_mode="inbound_label",
                client_uuid=key_data.get("xui_client_uuid"),
                base_email=key_data.get("key_email"),
            )
        except (xui_api.ManagedSubscriptionError, RuntimeError) as exc:
            logger.warning(
                "Managed subscription failed for key_id=%s host=%s: %s",
                key_data.get("key_id"),
                host_name,
                exc,
            )
            stored_payload = _fallback_subscription_payload_for_tier(
                key_data,
                cached_payload,
                subscription_tier=key_tier,
            )
            if stored_payload:
                return _subscription_plain_text_response(stored_payload, subscription_tier=key_tier)
            return "Subscription unavailable", 502
        except Exception:
            logger.exception(
                "Managed subscription crashed for key_id=%s host=%s",
                key_data.get("key_id"),
                host_name,
            )
            stored_payload = _fallback_subscription_payload_for_tier(
                key_data,
                cached_payload,
                subscription_tier=key_tier,
            )
            if stored_payload:
                return _subscription_plain_text_response(stored_payload, subscription_tier=key_tier)
            return "Subscription unavailable", 502

        if not links:
            stored_payload = _fallback_subscription_payload_for_tier(
                key_data,
                cached_payload,
                subscription_tier=key_tier,
            )
            if stored_payload:
                return _subscription_plain_text_response(stored_payload, subscription_tier=key_tier)
            return "Subscription unavailable", 502

        live_payload = xui_api.normalize_subscription_payload(links)
        if not live_payload:
            stored_payload = _fallback_subscription_payload_for_tier(
                key_data,
                cached_payload,
                subscription_tier=key_tier,
            )
            if stored_payload:
                return _subscription_plain_text_response(stored_payload, subscription_tier=key_tier)
            return "Subscription unavailable", 502
        _cache_subscription_payload_if_possible(key_data, live_payload)

        return _subscription_plain_text_response(live_payload, subscription_tier=key_tier)

    @flask_app.route("/login", methods=["GET", "POST"])
    def login_page():
        if request.method == "POST":
            client_ip = request.remote_addr or "unknown"
            if not _check_login_rate(client_ip):
                flash("Слишком много попыток входа. Подождите 5 минут.", "danger")
                logger.warning("Login rate limit hit for IP %s", client_ip)
                return render_template("login.html")

            submitted_user = (request.form.get("username", "") or "").strip()
            submitted_pass = request.form.get("password", "") or ""
            scoped_account = authenticate_scoped_account(submitted_user, submitted_pass, get_setting)
            if scoped_account is not None:
                stored_login = scoped_account["username"]
                login_ok = password_ok = True
            else:
                settings = get_all_settings()
                stored_login = str(settings.get("panel_login", "") or "").strip()
                stored_password = str(settings.get("panel_password", "") or "")
                login_ok = bool(stored_login) and compare_digest(submitted_user.casefold(), stored_login.casefold())
                password_ok = False
                if login_ok and stored_password:
                    if _is_password_hashed(stored_password):
                        parsed = _parse_hashed_password(stored_password)
                        if parsed:
                            password_ok = _verify_password(submitted_pass, parsed[1], parsed[0])
                    else:
                        password_ok = compare_digest(submitted_pass, stored_password)
                        if password_ok:
                            digest, salt = _hash_password(submitted_pass)
                            update_setting("panel_password", _format_hashed_password(digest, salt))
                            logger.info("Panel password migrated to hashed format.")

            if login_ok and password_ok:
                _clear_login_attempts(client_ip)
                session.clear()
                session["logged_in"] = True
                session["admin_login"] = stored_login
                if scoped_account is not None:
                    start_scoped_session(scoped_account)
                session["referral_csrf_token"] = secrets.token_urlsafe(32)
                session.permanent = True
                return redirect(url_for("dashboard_page"))
            else:
                _record_login_attempt(client_ip)
                logger.warning("Failed login attempt from IP %s, user='%s'", client_ip, submitted_user)
                flash("Неверный логин или пароль", "danger")
        return render_template("login.html")

    @flask_app.route("/logout", methods=["POST"])
    @login_required
    def logout_page():
        session.clear()
        flash("Вы успешно вышли.", "success")
        return redirect(url_for("login_page"))

    def get_common_template_data():
        default_bot_status = {
            "shop_bot_running": False,
            "shop_bot_healthy": False,
            "shop_bot_health_message": "stopped",
            "shop_bot_health_checked_at": None,
            "shop_bot_desired_running": False,
            "shop_autorestart_paused": False,
            "shop_last_failure": None,
            "shop_last_failure_at": None,
            "support_bot_running": False,
            "chatwoot_enabled": False,
        }
        try:
            raw_bot_status = _bot_controller.get_status(include_health=True) if _bot_controller else {}
        except Exception:
            logger.exception("Failed to get bot status for template context")
            raw_bot_status = {}

        if not isinstance(raw_bot_status, dict):
            raw_bot_status = {}
        bot_status = {**default_bot_status, **raw_bot_status}

        try:
            settings = get_all_settings_cached() or {}
        except Exception:
            logger.exception("Failed to get settings for template context")
            settings = {}

        runtime_bot_source = _normalize_manual_bot_source(BOT_SOURCE)
        if runtime_bot_source == BOT_SOURCE_RESERVE:
            required_for_start = ["reserve_bot_token", "reserve_bot_username", "admin_telegram_id"]
        else:
            required_for_start = ["telegram_bot_token", "telegram_bot_username", "admin_telegram_id"]
        all_settings_ok = all(settings.get(key) for key in required_for_start)

        reserve_runtime_status = None
        if runtime_bot_source != BOT_SOURCE_RESERVE and settings.get("reserve_bot_token"):
            reserve_runtime_status = _load_runtime_status(BOT_SOURCE_RESERVE)

        return {
            "bot_status": bot_status,
            "all_settings_ok": all_settings_ok,
            "allow_runtime_stop": _runtime_stop_allowed(),
            "global_settings": settings,
            "runtime_bot_source": runtime_bot_source,
            "reserve_runtime_status": reserve_runtime_status,
        }

    @flask_app.context_processor
    def inject_common_template_data():
        return get_common_template_data()

    @flask_app.route("/")
    @login_required
    def index():
        return redirect(url_for("dashboard_page"))

    def _dashboard_context(*, include_all: bool = False) -> dict:
        tx_type = str(request.args.get("tx_type") or TX_TYPE_ALL).strip().lower()
        if tx_type not in {TX_TYPE_ALL, TX_TYPE_HYSTERIA, TX_TYPE_SUPER, TX_TYPE_OLD, TX_TYPE_VLESS}:
            tx_type = TX_TYPE_ALL
        bot_source = str(request.args.get("bot_source") or BOT_SOURCE_ALL).strip().lower()
        if bot_source not in {BOT_SOURCE_ALL, BOT_SOURCE_MAIN, BOT_SOURCE_RESERVE}:
            bot_source = BOT_SOURCE_ALL
        search = str(request.args.get("q") or "").strip()[:128]
        period = _resolve_dashboard_period(
            period_value=_parse_dashboard_period_arg(request.args.get("period")),
            raw_date_from=request.args.get("date_from"),
            raw_date_to=request.args.get("date_to"),
        )
        snapshot = get_dashboard_snapshot(
            date_from=period["date_from"],
            date_to=period["date_to"],
            tx_type=tx_type,
            bot_source=bot_source,
            search=search,
            page=max(1, request.args.get("page", 1, type=int)),
            per_page=15,
            include_all=include_all,
        )
        analytics = snapshot["analytics"]
        summary = snapshot["tx_type_summary"]
        total = sum(summary.values())
        metrics = analytics["totals"]
        context = {
            **period,
            "period_caption": period["caption"],
            "stats": snapshot["stats"],
            "selected_metrics": metrics,
            "transactions": snapshot["transactions"],
            "total_transactions": snapshot["total_transactions"],
            "current_page": snapshot["current_page"],
            "total_pages": snapshot["total_pages"],
            "pagination_items": _build_pagination_items(snapshot["current_page"], snapshot["total_pages"]),
            "tx_summary": summary,
            "tx_summary_total": total,
            "trial_conversion": snapshot["trial_analytics"],
            "comparison": snapshot["comparison"],
            "selected_tx_type": tx_type,
            "selected_bot_source": bot_source,
            "search": search,
            "selected_share_percent": round(100 * metrics["transactions"] / total, 1) if total else 0,
            "tx_type_options": [
                {"value": TX_TYPE_ALL, "label": "Все"},
                {"value": TX_TYPE_VLESS, "label": "Стандарт"},
                {"value": TX_TYPE_SUPER, "label": "Супер"},
                {"value": TX_TYPE_HYSTERIA, "label": "Hysteria · старые"},
                {"value": TX_TYPE_OLD, "label": "Архивные тарифы"},
            ],
            "bot_source_options": [
                {"value": BOT_SOURCE_ALL, "label": "Оба бота"},
                {"value": BOT_SOURCE_MAIN, "label": "Основной"},
                {"value": BOT_SOURCE_RESERVE, "label": "Резервный"},
            ],
            "period_options": [
                ("today", "Сегодня"), ("yesterday", "Вчера"),
                ("month", "Этот месяц"), ("previous_month", "Прошлый месяц"),
                (7, "7 дней"), (30, "30 дней"), (90, "90 дней"),
            ],
            "chart_data": {
                "transactions": analytics["transactions_by_day"],
                "revenue": analytics["revenue_by_day"],
            },
            "chart_meta": {
                "period_days": period["period_days"],
                "range_start": period["range_start"],
                "range_end": period["range_end"],
                "tx_type": tx_type,
                "bot_source": bot_source,
            },
        }
        context["dashboard_live_signature"] = _build_dashboard_live_signature(**context)

        def dashboard_url(endpoint="dashboard_page", **changes):
            params = {"tx_type": tx_type, "bot_source": bot_source, "q": search, "page": 1}
            if period["selected_period"] == "range":
                params.update(date_from=period["date_from"], date_to=period["date_to"])
            else:
                params["period"] = period["selected_period"]
            if "period" in changes:
                params.pop("date_from", None)
                params.pop("date_to", None)
            params.update(changes)
            return url_for(endpoint, **params)

        context["dashboard_url"] = dashboard_url
        return context

    from shop_bot.webhook_server.finance_admin import register_finance_routes

    def _finance_period(args):
        return _resolve_dashboard_period(
            period_value=_parse_dashboard_period_arg(args.get("period") or "month"),
            raw_date_from=args.get("date_from"),
            raw_date_to=args.get("date_to"),
        )

    def _finance_db_path():
        from shop_bot.data_manager.database import DB_FILE
        return DB_FILE

    register_finance_routes(
        flask_app, login_required=login_required,
        resolve_period=_finance_period, db_path=_finance_db_path,
    )

    @flask_app.route("/dashboard")
    @login_required
    def dashboard_page():
        return render_template("dashboard.html", **_dashboard_context())

    @flask_app.route("/api/dashboard/live-signature")
    @login_required
    def dashboard_live_signature_api():
        context = _dashboard_context()
        return jsonify({
            "signature": context["dashboard_live_signature"],
            "checked_at": datetime.now(_DASHBOARD_MOSCOW_TZ).strftime("%d.%m.%Y %H:%M:%S МСК"),
            "current_page": context["current_page"],
            "total_pages": context["total_pages"],
            "total_transactions": context["total_transactions"],
            "html": render_template("dashboard.html", **context),
            "chart_data": context["chart_data"],
            "chart_meta": context["chart_meta"],
        })

    @flask_app.route("/dashboard/export.csv")
    @login_required
    def dashboard_export_csv():
        context = _dashboard_context(include_all=True)
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Дата оплаты (МСК)", "Telegram ID", "Username", "Тариф", "Действие",
                         "Сумма RUB", "Способ оплаты", "Payment ID", "Бот", "Выдача", "Telegram"])
        for tx in context["transactions"]:
            values = [
                tx.get("paid_at_display"), tx.get("user_id"), tx.get("username"),
                tx.get("plan_name"), tx.get("action_label"), tx.get("amount_rub"),
                tx.get("payment_method"), tx.get("payment_id"), tx.get("bot_source"),
                tx.get("issuance_label"), tx.get("delivery_label"),
            ]
            writer.writerow([
                "'" + str(value) if str(value or "").lstrip().startswith(("=", "+", "-", "@"))
                else ("" if value is None else value)
                for value in values
            ])
        filename = f"payments-{context['date_from']}-{context['date_to']}.csv"
        return Response(
            "\ufeff" + output.getvalue(),
            content_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"',
                     "Cache-Control": "no-store"},
        )

    def _format_admin_gib(bytes_value: int | float | None) -> str:
        try:
            value = max(0, float(bytes_value or 0))
        except (TypeError, ValueError):
            value = 0.0
        gib = value / (1024 ** 3)
        if gib >= 10:
            return f"{gib:.1f} GiB"
        return f"{gib:.2f} GiB"

    def _format_admin_reset_ms(reset_timestamp_ms: int | str | None) -> str:
        try:
            timestamp_ms = int(reset_timestamp_ms or 0)
        except (TypeError, ValueError):
            timestamp_ms = 0
        if timestamp_ms <= 0:
            return "не запланирован"
        reset_dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=_DASHBOARD_MOSCOW_TZ)
        return reset_dt.strftime("%d.%m.%Y %H:%M МСК")

    def _format_admin_status_updated_at(raw_value: str | None) -> str:
        value = str(raw_value or "").strip()
        if not value:
            return ""
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                parsed = datetime.strptime(value.split("+")[0], fmt)
                return parsed.strftime("%d.%m.%Y %H:%M")
            except ValueError:
                continue
        return value

    def _infer_whitelist_server_label(row: dict) -> str:
        email = str(row.get("key_email") or "")
        match = re.search(r"(whitelist\d*)", email, flags=re.IGNORECASE)
        server = match.group(1).upper() if match else "WHITELIST"
        host = str(row.get("host_name") or "").strip()
        return f"{server} · {host}" if host else server

    def _resolve_user_for_whitelist_balance(raw_query: str) -> tuple[dict | None, str | None, int]:
        query = str(raw_query or "").strip()
        if not query:
            return None, "Введите Telegram ID или username в поиск.", 400
        normalized = query.lstrip("@").strip()
        if normalized.isdigit():
            user = get_user(int(normalized))
            if user:
                return user, None, 200
            return None, "Пользователь с таким Telegram ID не найден.", 404

        user_id = get_user_id_by_username(normalized)
        if user_id:
            user = get_user(user_id)
            if user:
                return user, None, 200

        matches = get_users_filtered(normalized)
        exact_matches = [
            item for item in matches
            if str(item.get("username") or "").strip().lstrip("@").lower() == normalized.lower()
        ]
        if len(exact_matches) == 1:
            return exact_matches[0], None, 200
        if len(matches) == 1:
            return matches[0], None, 200
        if len(matches) > 1:
            return None, f"Найдено несколько пользователей: {len(matches)}. Уточните Telegram ID или полный username.", 400
        return None, "Пользователь не найден.", 404

    def _build_whitelist_balance_item(row: dict) -> dict:
        total_bytes = max(0, int(row.get("total_bytes") or 0))
        used_bytes = max(0, int(row.get("used_bytes") or 0))
        remaining_bytes = max(0, total_bytes - used_bytes)
        unlimited = total_bytes == 0
        exhausted = bool(row.get("exhausted")) or (total_bytes > 0 and used_bytes >= total_bytes)
        panel_enabled = bool(row.get("panel_enable", True))
        subscription_url = str(row.get("issued_key_value") or "").strip()
        if not subscription_url:
            subscription_url = _managed_subscription_url(
                row.get("panel_sub_id"),
                subscription_tier=row.get("subscription_tier"),
            ) or ""
        status_parts = []
        if unlimited:
            status_parts.append("безлимит")
        elif exhausted:
            status_parts.append("лимит исчерпан")
        else:
            status_parts.append("доступно")
        if not panel_enabled:
            status_parts.append("клиент отключен на панели")
        reset_label = _format_admin_reset_ms(row.get("reset_timestamp_ms"))
        if unlimited:
            reset_label = "не требуется"
        elif reset_label == "не запланирован":
            expiry_at = _parse_datetime(row.get("expiry_date"))
            if expiry_at:
                expiry_moscow = expiry_at.replace(tzinfo=UTC).astimezone(_DASHBOARD_MOSCOW_TZ)
                reset_label = f"{expiry_moscow.strftime('%d.%m.%Y %H:%M МСК')} (окончание подписки)"
        return {
            "server": _infer_whitelist_server_label(row),
            "host_name": row.get("host_name") or "",
            "key_id": row.get("key_id"),
            "key_email": row.get("key_email") or "",
            "subscription_url": subscription_url,
            "remaining_label": "Безлимит" if unlimited else _format_admin_gib(remaining_bytes),
            "used_label": _format_admin_gib(used_bytes),
            "total_label": "Безлимит" if unlimited else _format_admin_gib(total_bytes),
            "reset_label": reset_label,
            "updated_label": _format_admin_status_updated_at(row.get("updated_at")),
            "status": ", ".join(status_parts),
            "exhausted": exhausted,
            "panel_enabled": panel_enabled,
        }

    @flask_app.route("/users")
    @login_required
    def users_page():
        page = request.args.get("page", 1, type=int)
        per_page = 50
        search_query = (request.args.get("q") or request.args.get("search") or "").strip()
        bot_status_filter = (request.args.get("bot_status") or "all").strip().lower()
        if bot_status_filter not in {"all", "allowed", "banned"}:
            bot_status_filter = "all"

        vpn_status_filter = (request.args.get("vpn_status") or "all").strip().lower()
        if vpn_status_filter not in {"all", "with_keys", "without_keys", "expired"}:
            vpn_status_filter = "all"

        registration_from = _normalize_date_query(request.args.get("reg_from"))
        registration_to = _normalize_date_query(request.args.get("reg_to"))
        selected_host = (request.args.get("host") or "").strip()

        sort_by = (request.args.get("sort") or "registration_desc").strip().lower()
        if sort_by not in {
            "registration_desc",
            "registration_asc",
            "spent_desc",
            "spent_asc",
            "key_count_desc",
            "key_count_asc",
        }:
            sort_by = "registration_desc"

        bot_status_labels = {
            "allowed": "Разрешен",
            "banned": "Заблокирован",
        }
        vpn_status_labels = {
            "with_keys": "Есть ключи",
            "without_keys": "Нет ключей",
            "expired": "Есть просроченные",
        }
        sort_labels = {
            "registration_desc": "Регистрация: новые",
            "registration_asc": "Регистрация: старые",
            "spent_desc": "Потрачено: больше",
            "spent_asc": "Потрачено: меньше",
            "key_count_desc": "Ключи: больше",
            "key_count_asc": "Ключи: меньше",
        }

        try:
            users, total_users = get_users_paginated(
                page=page,
                per_page=per_page,
                search_query=search_query or None,
                bot_status=bot_status_filter,
                vpn_status=vpn_status_filter,
                registration_from=registration_from,
                registration_to=registration_to,
                host_name=selected_host or None,
                sort_by=sort_by,
            )
            total_users_all = get_user_count()
            hosts = get_distinct_vpn_key_hosts()
        except Exception:
            logger.exception("Failed to build users page context")
            users, total_users = [], 0
            total_users_all = 0
            hosts = []
            bot_status_filter = "all"
            vpn_status_filter = "all"
            registration_from = None
            registration_to = None
            selected_host = ""
            sort_by = "registration_desc"
            flash("Ошибка загрузки пользователей. Попробуйте обновить страницу.", "danger")

        total_pages = max(1, ceil(total_users / per_page)) if total_users else 1
        pagination_items = _build_pagination_items(page, total_pages)

        for user in users:
            raw_key_count = user.get("key_count")
            raw_expired_key_count = user.get("expired_key_count")
            try:
                key_count = int(raw_key_count or 0)
            except (TypeError, ValueError):
                try:
                    key_count = int(float(raw_key_count))
                except (TypeError, ValueError):
                    key_count = 0
            try:
                expired_key_count = int(raw_expired_key_count or 0)
            except (TypeError, ValueError):
                try:
                    expired_key_count = int(float(raw_expired_key_count))
                except (TypeError, ValueError):
                    expired_key_count = 0
            user["key_count"] = key_count
            user["expired_key_count"] = expired_key_count
            user.update(_build_vpn_status_from_count(key_count, expired_key_count))
        users_live_signature = _build_users_live_signature(
            users,
            total_users=total_users,
            current_page=page,
            total_pages=total_pages,
        )

        active_filters: list[dict[str, str]] = []
        if search_query:
            active_filters.append({"label": "Поиск", "value": search_query})
        if bot_status_filter in bot_status_labels:
            active_filters.append({"label": "Статус в боте", "value": bot_status_labels[bot_status_filter]})
        if vpn_status_filter in vpn_status_labels:
            active_filters.append({"label": "VPN", "value": vpn_status_labels[vpn_status_filter]})
        if registration_from:
            active_filters.append({"label": "С даты", "value": registration_from})
        if registration_to:
            active_filters.append({"label": "По дату", "value": registration_to})
        if selected_host:
            active_filters.append({"label": "Хост", "value": selected_host})
        if sort_by != "registration_desc":
            active_filters.append(
                {"label": "Сортировка", "value": sort_labels.get(sort_by, sort_labels["registration_desc"])}
            )

        has_filters = bool(active_filters)
        common_data = get_common_template_data()
        return render_template(
            "users.html",
            users=users,
            search_query=search_query,
            bot_status_filter=bot_status_filter,
            vpn_status_filter=vpn_status_filter,
            reg_from=registration_from or "",
            reg_to=registration_to or "",
            selected_host=selected_host,
            sort_by=sort_by,
            hosts=hosts,
            active_filters=active_filters,
            has_filters=has_filters,
            shown_count=len(users),
            total_users=total_users,
            total_users_all=total_users_all,
            current_page=page,
            total_pages=total_pages,
            pagination_items=pagination_items,
            users_live_signature=users_live_signature,
            **common_data,
        )

    @flask_app.route("/api/users/whitelist-balance")
    @login_required
    def users_whitelist_balance_api():
        user, error, status_code = _resolve_user_for_whitelist_balance(request.args.get("q", ""))
        if error or not user:
            return jsonify({"ok": False, "error": error or "Пользователь не найден."}), status_code

        try:
            telegram_id = int(user.get("telegram_id") or 0)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "У пользователя некорректный Telegram ID."}), 400

        rows = get_user_whitelist_traffic_statuses(telegram_id, bot_source=BOT_SOURCE_ALL)
        items = [_build_whitelist_balance_item(row) for row in rows]
        return jsonify(
            {
                "ok": True,
                "user": {
                    "telegram_id": telegram_id,
                    "username": user.get("username") or "",
                },
                "items": items,
            }
        )

    @flask_app.route("/api/users/live-signature")
    @login_required
    def users_live_signature_api():
        page = request.args.get("page", 1, type=int)
        per_page = 50
        search_query = (request.args.get("q") or request.args.get("search") or "").strip()
        bot_status_filter = (request.args.get("bot_status") or "all").strip().lower()
        if bot_status_filter not in {"all", "allowed", "banned"}:
            bot_status_filter = "all"

        vpn_status_filter = (request.args.get("vpn_status") or "all").strip().lower()
        if vpn_status_filter not in {"all", "with_keys", "without_keys", "expired"}:
            vpn_status_filter = "all"

        registration_from = _normalize_date_query(request.args.get("reg_from"))
        registration_to = _normalize_date_query(request.args.get("reg_to"))
        selected_host = (request.args.get("host") or "").strip()
        sort_by = (request.args.get("sort") or "registration_desc").strip().lower()
        if sort_by not in {
            "registration_desc",
            "registration_asc",
            "spent_desc",
            "spent_asc",
            "key_count_desc",
            "key_count_asc",
        }:
            sort_by = "registration_desc"

        users, total_users = get_users_paginated(
            page=page,
            per_page=per_page,
            search_query=search_query or None,
            bot_status=bot_status_filter,
            vpn_status=vpn_status_filter,
            registration_from=registration_from,
            registration_to=registration_to,
            host_name=selected_host or None,
            sort_by=sort_by,
        )
        total_pages = max(1, ceil(total_users / per_page)) if total_users else 1
        for user in users:
            try:
                key_count = int(user.get("key_count") or 0)
            except (TypeError, ValueError):
                key_count = 0
            try:
                expired_key_count = int(user.get("expired_key_count") or 0)
            except (TypeError, ValueError):
                expired_key_count = 0
            user["key_count"] = key_count
            user["expired_key_count"] = expired_key_count

        signature = _build_users_live_signature(
            users,
            total_users=total_users,
            current_page=page,
            total_pages=total_pages,
        )
        return jsonify(
            {
                "signature": signature,
                "checked_at": datetime.utcnow().strftime("%H:%M:%S"),
                "shown_count": len(users),
                "total_users": total_users,
                "current_page": page,
                "total_pages": total_pages,
            }
        )

    @flask_app.route("/api/users/<int:user_id>/detail")
    @login_required
    def user_detail_api(user_id: int):
        user = get_user(user_id)
        if not user:
            return jsonify({"error": "Пользователь не найден"}), 404

        raw_keys = get_user_keys(user_id)
        paid_transactions = get_user_transactions(user_id, limit=20, statuses=("paid",))
        keys = []
        for key in raw_keys:
            expiry_value = key.get("expiry_date")
            is_archived = bool(key.get("is_archived"))
            key_source = str(key.get("bot_source") or "main").strip().lower()
            if key_source not in {"main", "reserve"}:
                key_source = "main"
            keys.append(
                {
                    "key_id": key.get("key_id"),
                    "key_email": key.get("key_email"),
                    "host_name": key.get("host_name"),
                    "bot_source": key_source,
                    "purchase_date": _ru_date(key.get("created_date")),
                    "expiry_date": _ru_date(expiry_value),
                    "is_expired": _is_key_expired(expiry_value, is_archived=is_archived),
                    "is_archived": is_archived,
                    "issued_key_value": key.get("issued_key_value"),
                }
            )

        transactions: list[dict] = []
        for transaction in paid_transactions:
            tx_metadata = _parse_transaction_metadata(transaction.get("metadata"))
            tx_source = str(transaction.get("bot_source") or tx_metadata.get("bot_source") or "main").strip().lower()
            if tx_source not in {"main", "reserve"}:
                tx_source = "main"
            transactions.append(
                {
                    "payment_id": transaction.get("payment_id"),
                    "payment_method": transaction.get("payment_method"),
                    "amount_rub": float(transaction.get("amount_rub") or 0),
                    "host_name": tx_metadata.get("host_name"),
                    "plan_name": tx_metadata.get("plan_name"),
                    "created_date": _payment_date_moscow(transaction),
                    "bot_source": tx_source,
                }
            )

        user_payload = {
            "telegram_id": user.get("telegram_id"),
            "username": user.get("username"),
            "is_banned": int(user.get("is_banned") or 0) == 1,
            "total_spent": float(user.get("total_spent") or 0),
            "registration_date": _ru_date(user.get("registration_date")),
        }
        return jsonify({"user": user_payload, "keys": keys, "transactions": transactions})

    @flask_app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings_page():
        if request.method == "POST":
            referral_settings = None
            if any(key in request.form for key in referral_setting_keys):
                if not referral_csrf_valid():
                    return "Форма устарела. Обновите страницу и повторите сохранение.", 403
                try:
                    referral_settings = referral_form_settings(preserve_missing=True)
                except ValueError as exc:
                    flash(str(exc), "danger")
                    return redirect(url_for("settings_page", group="sales"))

            new_password = (request.form.get("panel_password") or "").strip()
            if new_password:
                digest, salt = _hash_password(new_password)
                update_setting("panel_password", _format_hashed_password(digest, salt))

            chatwoot_enabled_requested = _is_checked(request.form, "chatwoot_enabled")
            try:
                runtime_status = _bot_controller.get_status() if _bot_controller else {}
            except Exception:
                logger.exception("Failed to get runtime status while saving settings")
                runtime_status = {}
            if not isinstance(runtime_status, dict):
                runtime_status = {}
            support_running = bool(runtime_status.get("support_bot_running"))
            chatwoot_toggle_blocked = False
            if chatwoot_enabled_requested and support_running:
                chatwoot_toggle_blocked = True
                chatwoot_enabled_requested = False

            for checkbox_key in CHECKBOX_SETTINGS_KEYS:
                if checkbox_key in referral_setting_keys:
                    continue
                if checkbox_key == "chatwoot_enabled":
                    value = "true" if chatwoot_enabled_requested else "false"
                else:
                    values = request.form.getlist(checkbox_key)
                    value = values[-1] if values else "false"
                update_setting(checkbox_key, "true" if value == "true" else "false")

            for key in ALL_SETTINGS_KEYS:
                if key == "panel_password" or key in CHECKBOX_SETTINGS_KEYS or key in referral_setting_keys:
                    continue
                update_setting(key, request.form.get(key, ""))

            if referral_settings is not None:
                referrals.save_settings(referral_settings)

            if chatwoot_toggle_blocked:
                flash(
                    "Остальные настройки сохранены. Chatwoot не включен: сначала остановите legacy Support-бот.",
                    "warning",
                )
            else:
                flash("Настройки успешно сохранены!", "success")
            return redirect(url_for("settings_page"))

        current_settings = get_all_settings()
        hosts = filter_customer_visible_hosts(get_hosts_with_plans())
        for host in hosts:
            raw_token_ref = host.get("host_api_token_env")
            host["host_api_token_mode"] = _resolve_host_token_mode(raw_token_ref, host)
            host["host_api_token_label"] = _describe_host_api_token_config(raw_token_ref, host)
        xui_api_token_env_candidates = _collect_xui_api_token_env_candidates(hosts)
        blocked_statuses = {"unavailable", "maintenance"}
        disabled_purchases = []
        for host in hosts:
            status = str(host.get("status", "unknown") or "unknown").lower()
            is_maintenance = bool(host.get("is_maintenance"))
            if not (is_maintenance or status in blocked_statuses):
                continue
            disabled_purchases.append(
                {
                    "host_name": host.get("host_name"),
                    "status": "maintenance" if is_maintenance else status,
                    "last_error": host.get("last_error"),
                    "plans_count": len(host.get("plans") or []),
                }
            )

        common_data = get_common_template_data()
        delivery_sla_24h = get_delivery_time_percentiles(period_hours=24)
        delivery_sla_7d = get_delivery_time_percentiles(period_hours=168)
        return render_template(
            "settings.html",
            settings=current_settings,
            hosts=hosts,
            xui_api_token_env_candidates=xui_api_token_env_candidates,
            disabled_purchases=disabled_purchases,
            delivery_sla_24h=delivery_sla_24h,
            delivery_sla_7d=delivery_sla_7d,
            referral_csrf_token=referral_csrf_token(),
            **common_data,
        )

    @flask_app.route("/referrals", methods=["GET"])
    @login_required
    def referrals_page():
        query = (request.args.get("q") or "").strip()[:100]
        page = max(1, min(request.args.get("page", 1, type=int) or 1, 100000))
        withdrawal_page = max(1, min(request.args.get("withdrawal_page", 1, type=int) or 1, 100000))
        reward_page = max(1, min(request.args.get("reward_page", 1, type=int) or 1, 100000))
        notification_page = max(1, min(request.args.get("notification_page", 1, type=int) or 1, 100000))
        status = request.args.get("status", "pending")
        if status not in {"all", "pending", "paid", "rejected"}:
            status = "pending"
        limit = 50
        dashboard = referrals.get_dashboard(
            query=query, limit=limit, offset=(page - 1) * limit,
            reward_limit=limit, reward_offset=(reward_page - 1) * limit,
        )
        withdrawals = referrals.list_withdrawals(
            status=None if status == "all" else status,
            limit=limit + 1,
            offset=(withdrawal_page - 1) * limit,
        )
        notifications = referrals.list_withdrawal_notifications(limit=limit + 1, offset=(notification_page - 1) * limit)
        selected_user = None
        user_id = request.args.get("user_id", type=int)
        if user_id is not None:
            try:
                selected_user = referrals.get_user_summary(user_id)
            except ValueError as exc:
                flash(str(exc), "warning")
        return render_template(
            "referrals.html", policy=referrals.get_policy(), dashboard=dashboard,
            withdrawals=withdrawals[:limit], has_more_withdrawals=len(withdrawals) > limit,
            withdrawal_notifications=notifications[:limit], notification_page=notification_page,
            has_more_notifications=len(notifications) > limit,
            query=query, page=page, withdrawal_page=withdrawal_page, withdrawal_status=status,
            reward_page=reward_page, has_more_rewards=reward_page * limit < dashboard.get("rewards_total", 0),
            has_more_users=page * limit < dashboard.get("users_total", 0),
            selected_user=selected_user, csrf_token=referral_csrf_token(),
        )

    @flask_app.route("/referrals/notifications/status", methods=["GET"])
    @login_required
    def referral_notification_status_route():
        notification_page = max(1, min(request.args.get("notification_page", 1, type=int) or 1, 100000))
        withdrawal_page = max(1, min(request.args.get("withdrawal_page", 1, type=int) or 1, 100000))
        status = request.args.get("status", "pending")
        if status not in {"all", "pending", "paid", "rejected"}:
            status = "pending"
        limit = 50
        notifications = referrals.list_withdrawal_notifications(limit=limit + 1, offset=(notification_page - 1) * limit)
        withdrawals = referrals.list_withdrawals(
            status=None if status == "all" else status, limit=limit, offset=(withdrawal_page - 1) * limit,
        )
        response = jsonify(
            html=render_template(
                "_referral_notifications.html", withdrawal_notifications=notifications[:limit],
                notification_page=notification_page, has_more_notifications=len(notifications) > limit,
                query=(request.args.get("q") or "").strip()[:100], withdrawal_status=status,
                csrf_token=referral_csrf_token(),
            ),
            withdrawal_statuses={str(row["id"]): row.get("notification_status") for row in withdrawals},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @flask_app.route("/referrals/settings", methods=["POST"])
    @login_required
    def referral_settings_route():
        if not referral_csrf_valid():
            return "Форма устарела. Обновите страницу и повторите сохранение.", 403
        try:
            referrals.save_settings(referral_form_settings())
        except ValueError as exc:
            flash(str(exc), "danger")
        else:
            flash("Настройки реферальной программы сохранены.", "success")
        return redirect(url_for("referrals_page"))

    @flask_app.route("/referrals/withdrawals/<int:withdrawal_id>/<action>", methods=["POST"])
    @login_required
    def referral_withdrawal_route(withdrawal_id, action):
        if not referral_csrf_valid():
            return "Форма устарела. Обновите страницу и повторите действие.", 403
        if action not in {"paid", "rejected"}:
            return "Неизвестное действие.", 400
        note = (request.form.get("note") or "").strip()
        if len(note) > 1000:
            flash("Комментарий не должен превышать 1000 символов.", "danger")
        elif action == "paid" and request.form.get("manual_payment_confirmed") != "true":
            flash("Подтвердите, что деньги уже переведены вручную.", "danger")
        elif action == "rejected" and not note:
            flash("Укажите причину отклонения заявки.", "danger")
        else:
            admin_login = session.get("admin_login") or get_setting("panel_login") or "admin"
            try:
                result = referrals.resolve_withdrawal(withdrawal_id, action, f"panel:{admin_login}", note=note)
            except ValueError as exc:
                flash(str(exc), "danger")
            else:
                flash("Заявка отмечена как выплаченная." if action == "paid" else "Заявка отклонена; сумма возвращена в доступный баланс.", "success")
                if result.get("is_new"):
                    flash("Уведомление клиенту сохранено в очереди. Фактический результат отправки отображается ниже.", "info")
                else:
                    flash("Это решение уже было сохранено. Повторная выплата и новое уведомление не создаются.", "info")
        return redirect(url_for("referrals_page", _anchor="withdrawals"))

    @flask_app.route("/referrals/notifications/<int:notification_id>/retry", methods=["POST"])
    @login_required
    def referral_notification_retry_route(notification_id):
        if not referral_csrf_valid():
            return "Форма устарела. Обновите страницу и повторите действие.", 403
        try:
            referrals.retry_withdrawal_notification(notification_id)
        except ValueError as exc:
            flash(str(exc), "danger")
        else:
            flash("Повторная отправка уведомления поставлена в очередь. Решение и баланс не изменены.", "success")
        return redirect(url_for("referrals_page", _anchor="notifications"))

    def _emit_shop_manual_action_alert(action: str, result: dict, *, details: str | None = None) -> None:
        try:
            normalized_action = str(action or "").strip().lower()
            if result.get("status") == "success":
                alert_type = BotError.BOT_MANUAL_START if normalized_action == "start" else BotError.BOT_MANUAL_STOP
                send_critical_alert(
                    alert_type,
                    details=details,
                    respect_cooldown=False,
                )
                return

            if normalized_action == "start":
                error = classify_error(result.get("message", ""))
                if error == BotError.UNKNOWN:
                    error = BotError.BOT_START_FAILED
                send_critical_alert(
                    error,
                    details=result.get("message", "Unknown start failure"),
                    respect_cooldown=False,
                )
        except Exception as exc:
            logger.error("Failed to emit shop manual action alert: %s", exc, exc_info=True)

    @flask_app.route("/internal/runtime/status", methods=["GET"])
    def internal_runtime_status_route():
        if not _is_runtime_control_request_authorized(request):
            return jsonify({"error": "forbidden"}), 403

        try:
            raw_bot_status = _bot_controller.get_status(include_health=True) if _bot_controller else {}
        except Exception:
            logger.exception("Failed to read bot status for internal runtime status")
            raw_bot_status = {}

        if not isinstance(raw_bot_status, dict):
            raw_bot_status = {}
        bot_status = {
            "shop_bot_running": False,
            "shop_bot_healthy": False,
            "shop_bot_health_message": "stopped",
            **raw_bot_status,
        }

        runtime_bot_source = _normalize_manual_bot_source(BOT_SOURCE)
        if runtime_bot_source == BOT_SOURCE_RESERVE:
            required_for_start = ["reserve_bot_token", "reserve_bot_username", "admin_telegram_id"]
        else:
            required_for_start = ["telegram_bot_token", "telegram_bot_username", "admin_telegram_id"]

        try:
            settings = get_all_settings_cached() or {}
        except Exception:
            logger.exception("Failed to read settings for internal runtime status")
            settings = {}

        all_settings_ok = all(settings.get(key) for key in required_for_start)
        message = "OK"
        if not all_settings_ok:
            message = "Заполните обязательные настройки Telegram."

        try:
            from shop_bot.domain.metrics import runtime_status as _issuance_runtime_status

            issuance_backend_status = _issuance_runtime_status()
        except (ImportError, ValueError):
            logger.exception("Failed to read issuance backend status for internal runtime status")
            issuance_backend_status = {
                "available": False,
                "message": "issuance backend status unavailable",
            }

        return jsonify(
            {
                "bot_source": runtime_bot_source,
                "shop_bot_running": bool(bot_status.get("shop_bot_running")),
                "shop_bot_healthy": bool(bot_status.get("shop_bot_healthy", bot_status.get("shop_bot_running"))),
                "shop_bot_health_message": str(bot_status.get("shop_bot_health_message") or ""),
                "scheduler_loops": scheduler_module.get_loop_health(),
                "issuance_backend": issuance_backend_status,
                "all_settings_ok": bool(all_settings_ok),
                "message": message,
            }
        )

    @flask_app.route("/internal/runtime/shop/<action>", methods=["POST"])
    def internal_runtime_shop_action_route(action: str):
        if not _is_runtime_control_request_authorized(request):
            return jsonify({"status": "error", "message": "forbidden"}), 403

        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"start", "stop"}:
            return jsonify({"status": "error", "message": "unknown action"}), 400
        if normalized_action == "stop" and not _runtime_stop_allowed():
            return jsonify(
                {
                    "status": "error",
                    "message": "Остановка runtime отключена политикой always-on.",
                }
            ), 423

        if normalized_action == "start":
            result = _bot_controller.start_shop_bot(initiated_by="runtime-control")
        else:
            result = _bot_controller.stop_shop_bot()

        _emit_shop_manual_action_alert(
            normalized_action,
            result,
            details=f"source={_normalize_manual_bot_source(BOT_SOURCE)} via=runtime-control",
        )
        http_code = 200 if result.get("status") == "success" else 409
        return jsonify(result), http_code

    @flask_app.route("/start-shop-bot", methods=["POST"])
    @login_required
    def start_shop_bot_route():
        result = _bot_controller.start_shop_bot()
        _emit_shop_manual_action_alert(
            "start",
            result,
            details=f"source={_normalize_manual_bot_source(BOT_SOURCE)} via=panel",
        )
        flash(result.get("message", "An error occurred."), "success" if result.get("status") == "success" else "danger")
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/restart-shop-bot", methods=["POST"])
    @login_required
    def restart_shop_bot_route():
        if not _runtime_stop_allowed():
            flash("Restart отключен политикой always-on.", "warning")
            return redirect(request.referrer or url_for("dashboard_page"))
        stop_result = _bot_controller.stop_shop_bot()
        _emit_shop_manual_action_alert(
            "restart-stop",
            stop_result,
            details=f"source={_normalize_manual_bot_source(BOT_SOURCE)} via=panel",
        )
        start_result = _bot_controller.start_shop_bot()
        _emit_shop_manual_action_alert(
            "restart-start",
            start_result,
            details=f"source={_normalize_manual_bot_source(BOT_SOURCE)} via=panel",
        )
        flash(
            start_result.get("message", "An error occurred."),
            "success" if start_result.get("status") == "success" else "danger",
        )
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/stop-shop-bot", methods=["POST"])
    @login_required
    def stop_shop_bot_route():
        if not _runtime_stop_allowed():
            flash("Остановка отключена политикой always-on.", "warning")
            return redirect(request.referrer or url_for("dashboard_page"))
        result = _bot_controller.stop_shop_bot()
        _emit_shop_manual_action_alert(
            "stop",
            result,
            details=f"source={_normalize_manual_bot_source(BOT_SOURCE)} via=panel",
        )
        flash(result.get("message", "An error occurred."), "success" if result.get("status") == "success" else "danger")
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/start-reserve-bot", methods=["POST"])
    @login_required
    def start_reserve_bot_route():
        runtime_bot_source = _normalize_manual_bot_source(BOT_SOURCE)
        if runtime_bot_source == BOT_SOURCE_RESERVE:
            result = _bot_controller.start_shop_bot()
            _emit_shop_manual_action_alert(
                "start",
                result,
                details="source=reserve via=panel",
            )
        else:
            result = _dispatch_runtime_shop_action(BOT_SOURCE_RESERVE, "start")
            if result.get("status") != "success":
                send_critical_alert(
                    BotError.BOT_START_FAILED,
                    details=f"reserve_runtime_start_failed: {result.get('message')}",
                    respect_cooldown=False,
                )
        flash(result.get("message", "An error occurred."), "success" if result.get("status") == "success" else "danger")
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/stop-reserve-bot", methods=["POST"])
    @login_required
    def stop_reserve_bot_route():
        if not _runtime_stop_allowed():
            flash("Остановка отключена политикой always-on.", "warning")
            return redirect(request.referrer or url_for("dashboard_page"))
        runtime_bot_source = _normalize_manual_bot_source(BOT_SOURCE)
        if runtime_bot_source == BOT_SOURCE_RESERVE:
            result = _bot_controller.stop_shop_bot()
            _emit_shop_manual_action_alert(
                "stop",
                result,
                details="source=reserve via=panel",
            )
        else:
            result = _dispatch_runtime_shop_action(BOT_SOURCE_RESERVE, "stop")
        flash(result.get("message", "An error occurred."), "success" if result.get("status") == "success" else "danger")
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/start-support-bot", methods=["POST"])
    @login_required
    def start_support_bot_route():
        result = _bot_controller.start_support_bot()
        flash(result.get("message", "An error occurred."), "success" if result.get("status") == "success" else "danger")
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/stop-support-bot", methods=["POST"])
    @login_required
    def stop_support_bot_route():
        result = _bot_controller.stop_support_bot()
        flash(result.get("message", "An error occurred."), "success" if result.get("status") == "success" else "danger")
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/users/ban/<int:user_id>", methods=["POST"])
    @login_required
    def ban_user_route(user_id):
        ban_user(user_id)
        flash(f"Пользователь {user_id} был заблокирован.", "success")
        return redirect(request.referrer or url_for("users_page"))

    @flask_app.route("/users/unban/<int:user_id>", methods=["POST"])
    @login_required
    def unban_user_route(user_id):
        unban_user(user_id)
        flash(f"Пользователь {user_id} был разблокирован.", "success")
        return redirect(request.referrer or url_for("users_page"))

    @flask_app.route("/users/revoke/<int:user_id>", methods=["POST"])
    @login_required
    def revoke_keys_route(user_id):
        keys_to_revoke = get_user_keys(user_id)
        success_count = 0
        failed_count = 0

        loop = current_app.config.get("EVENT_LOOP")

        def _run_async(coro):
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                return future.result(timeout=30)
            raise RuntimeError("Event loop is not available for async revoke")

        for key in keys_to_revoke:
            host_name = key.get("host_name")
            key_email = key.get("key_email")
            blitz_username = key.get("xui_client_uuid") or key_email

            try:
                if host_name == blitz_api.BLITZ_HOST_NAME:
                    result = _run_async(blitz_api.delete_user(blitz_username))
                    if not result:
                        logger.error(
                            "Failed to revoke Blitz key for user %s: username='%s', key_email='%s'",
                            user_id,
                            blitz_username,
                            key_email,
                        )
                else:
                    result = _run_async(xui_api.delete_client_on_host(host_name, key_email))
            except Exception as e:
                logger.error(
                    "Error while revoking key for user %s (host='%s', key_email='%s'): %s",
                    user_id,
                    host_name,
                    key_email,
                    e,
                    exc_info=True,
                )
                result = False

            if result:
                success_count += 1
                delete_key_by_email(key_email)
            else:
                failed_count += 1

        if keys_to_revoke and failed_count == 0:
            delete_user_keys(user_id)

        if success_count == len(keys_to_revoke):
            flash(f"Все {len(keys_to_revoke)} ключей для пользователя {user_id} были успешно отозваны.", "success")
        else:
            flash(
                f"Удалось отозвать {success_count} из {len(keys_to_revoke)} ключей для пользователя {user_id}. Проверьте логи.",
                "warning",
            )

        return redirect(request.referrer or url_for("users_page"))

    _host_node_sync_lock = threading.Lock()
    _host_node_sync_running: set[str] = set()

    def _start_host_node_sync_job(
        host_name: str,
        *,
        client_limit: int | None,
        dry_run: bool,
    ) -> bool:
        normalized_host_name = str(host_name or "").strip()
        if not normalized_host_name:
            return False

        with _host_node_sync_lock:
            if normalized_host_name in _host_node_sync_running:
                return False
            _host_node_sync_running.add(normalized_host_name)

        def _run_sync_job() -> None:
            try:
                update_host_node_sync_state(
                    normalized_host_name,
                    status="running",
                    error=None,
                )
                sync_result = xui_api.sync_new_host_into_main_panel(
                    normalized_host_name,
                    client_limit=client_limit,
                    dry_run=dry_run,
                )
                update_host_node_sync_state(
                    normalized_host_name,
                    status="completed",
                    error=None,
                    node_id=int(sync_result.get("node_id") or 0),
                    node_inbound_id=int(sync_result.get("node_inbound_id") or 0),
                )
                logger.info(
                    "Host node sync completed for '%s': copied=%s skipped=%s node_id=%s inbound_id=%s dry_run=%s limit=%s",
                    normalized_host_name,
                    sync_result.get("copied_clients"),
                    sync_result.get("skipped_clients"),
                    sync_result.get("node_id"),
                    sync_result.get("node_inbound_id"),
                    sync_result.get("dry_run"),
                    sync_result.get("client_limit"),
                )
            except Exception as exc:
                logger.exception("Host node sync failed for '%s'", normalized_host_name)
                update_host_node_sync_state(
                    normalized_host_name,
                    status="failed",
                    error=str(exc),
                )
            finally:
                with _host_node_sync_lock:
                    _host_node_sync_running.discard(normalized_host_name)

        threading.Thread(
            target=_run_sync_job,
            name=f"host-node-sync-{normalized_host_name}",
            daemon=True,
        ).start()
        return True

    @flask_app.route("/add-host", methods=["POST"])
    @login_required
    def add_host_route():
        host_name = str(request.form.get("host_name") or "").strip()
        try:
            host_config = _read_host_config_from_form(request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings_page", group="delivery"))
        if not host_name:
            flash("Название хоста обязательно.", "error")
            return redirect(url_for("settings_page", group="delivery"))

        try:
            api_token_env = _read_host_api_token_ref_from_form(request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings_page", group="delivery"))
        create_host(
            name=host_name,
            url=str(host_config["host_url"]),
            user=str(host_config["host_username"]),
            passwd=str(host_config["host_pass"]),
            inbound=int(host_config["host_inbound_id"]),
            subscription_base_url=host_config["subscription_base_url"],
            api_token_env=api_token_env,
            is_white_host=bool(host_config["is_white_host"]),
            delivery_state="draft",
        )
        if not get_host(host_name):
            flash(
                f"Не удалось добавить хост '{host_name}'. Проверьте уникальность и корректность данных.",  # noqa: RUF001
                "error",
            )
            return redirect(url_for("settings_page", group="delivery"))

        update_host_node_sync_state(host_name, status="pending", error=None)
        flash(
            f"Хост '{host_name}' добавлен как draft. Сначала проверьте и активируйте его.",  # noqa: RUF001
            "success",
        )
        return redirect(url_for("settings_page", group="delivery"))

    @flask_app.route("/validate-host/<host_name>", methods=["POST"])
    @login_required
    def validate_host_route(host_name):
        host_record = get_host(host_name)
        if not host_record:
            flash(f"Хост '{host_name}' не найден.", "error")
            return redirect(url_for("settings_page", group="delivery"))
        try:
            validation = xui_api.validate_host_catalog_entry(host_name)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            update_host_node_sync_state(host_name, status="failed", error=str(exc))
            flash(f"Проверка хоста '{host_name}' не прошла: {exc}", "error")
            return redirect(url_for("settings_page", group="delivery", host=host_name))

        update_host_node_sync_state(host_name, status="pending", error=None)
        flash(
            f"Проверка хоста прошла: inbound {validation['inbound_id']} / {validation['inbound_remark']}.",
            "success",
        )
        return redirect(url_for("settings_page", group="delivery", host=host_name))

    @flask_app.route("/activate-host/<host_name>", methods=["POST"])
    @login_required
    def activate_host_route(host_name):
        host_record = get_host(host_name)
        if not host_record:
            flash(f"Хост '{host_name}' не найден.", "error")
            return redirect(url_for("settings_page", group="delivery"))
        try:
            xui_api.validate_host_catalog_entry(host_name)
            if host_record.get("is_white_host"):
                sync_result = xui_api.sync_new_host_into_main_panel(
                    host_name,
                    client_limit=0,
                    subscription_verify_limit=0,
                )
                update_host_node_sync_state(
                    host_name,
                    status="completed",
                    error=None,
                    node_id=int(sync_result.get("node_id") or 0),
                    node_inbound_id=int(sync_result.get("node_inbound_id") or 0),
                )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            update_host_node_sync_state(host_name, status="failed", error=str(exc))
            flash(f"Хост '{host_name}' не активирован: {exc}", "error")
            return redirect(url_for("settings_page", group="delivery", host=host_name))

        if not set_host_delivery_state(host_name, "active"):
            flash(f"Не удалось активировать хост '{host_name}'.", "error")
            return redirect(url_for("settings_page", group="delivery", host=host_name))

        flash(f"Хост '{host_name}' активирован для новых выдач.", "success")
        return redirect(url_for("settings_page", group="delivery", host=host_name))

    @flask_app.route("/disable-host/<host_name>", methods=["POST"])
    @login_required
    def disable_host_route(host_name):
        if not get_host(host_name):
            flash(f"Хост '{host_name}' не найден.", "error")
            return redirect(url_for("settings_page", group="delivery"))
        if set_host_delivery_state(host_name, "disabled"):
            flash(f"Хост '{host_name}' отключен от новых выдач и подписок.", "success")
        else:
            flash(f"Не удалось отключить хост '{host_name}'.", "error")
        return redirect(url_for("settings_page", group="delivery", host=host_name))

    @flask_app.route("/sync-host-node/<host_name>", methods=["POST"])
    @login_required
    def sync_host_node_route(host_name):
        host_record = get_host(host_name)
        if not host_record:
            flash(f"Хост '{host_name}' не найден.", "error")
            return redirect(url_for("settings_page", group="delivery"))

        try:
            client_limit = _read_non_negative_int(
                request.form.get("client_limit"),
                field_label="Лимит клиентов",
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings_page", group="delivery", host=host_name))
        dry_run = _is_checked(request.form, "dry_run")

        update_host_node_sync_state(host_name, status="pending", error=None)
        started = _start_host_node_sync_job(
            host_name,
            client_limit=client_limit,
            dry_run=dry_run,
        )
        if started:
            mode_text = "dry-run" if dry_run else f"батч {client_limit}"
            flash(f"Синхронизация для хоста '{host_name}' запущена: {mode_text}.", "success")
        else:
            flash(f"Синхронизация для хоста '{host_name}' уже выполняется.", "warning")
        return redirect(url_for("settings_page", group="delivery", host=host_name))

    @flask_app.route("/delete-host/<host_name>", methods=["POST"])
    @login_required
    def delete_host_route(host_name):
        delete_host(host_name)
        flash(f"Хост '{host_name}' и все его тарифы были удалены.", "success")
        return redirect(url_for("settings_page", group="delivery"))

    @flask_app.route("/update-host-api-token/<host_name>", methods=["POST"])
    @login_required
    def update_host_api_token_route(host_name):
        try:
            api_token_ref = _read_host_api_token_ref_from_form(request.form)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings_page", group="delivery", host=host_name))
        if update_host_api_token_ref(host_name, api_token_ref):
            flash(f"API Token XUI для хоста '{host_name}' обновлен.", "success")
        else:
            flash(f"Не удалось обновить API Token XUI для хоста '{host_name}'.", "error")
        return redirect(url_for("settings_page", group="delivery", host=host_name))

    @flask_app.route("/update-host-config/<host_name>", methods=["POST"])
    @login_required
    def update_host_config_route(host_name):
        current_host = get_host(host_name)
        if not current_host:
            flash(f"Хост '{host_name}' не найден.", "error")
            return redirect(url_for("settings_page", group="delivery"))
        try:
            host_config = _read_host_config_from_form(
                request.form,
                current_host_pass=current_host.get("host_pass"),
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings_page", group="delivery", host=host_name))

        current_delivery_state = str(current_host.get("delivery_state") or "active").strip().lower()
        requested_delivery_state = str(host_config.get("delivery_state") or "draft").strip().lower()
        if requested_delivery_state == "active" and current_delivery_state != "active":
            flash(
                "Нельзя активировать хост через обычное сохранение. Сначала используйте проверку и кнопку активации.",
                "error",
            )
            return redirect(url_for("settings_page", group="delivery", host=host_name))

        if update_host_config(host_name, **host_config):
            update_host_node_sync_state(host_name, status="pending", error=None)
            flash(
                f"Настройки хоста '{host_name}' обновлены. При необходимости запустите повторный node sync.",
                "success",
            )
        else:
            flash(f"Не удалось обновить настройки хоста '{host_name}'.", "error")
        return redirect(url_for("settings_page", group="delivery", host=host_name))

    @flask_app.route("/update-host-urls/<host_name>", methods=["POST"])
    @login_required
    def update_host_urls_route(host_name):
        try:
            host_url = _validate_host_url(request.form.get("host_url"), field_label="URL панели")
            subscription_base_url = _validate_optional_host_url(
                request.form.get("host_subscription_base_url"),
                field_label="URL подписки",
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("settings_page", group="delivery", host=host_name))
        if update_host_urls(host_name, host_url, subscription_base_url):
            flash(f"Адреса XUI для хоста '{host_name}' обновлены.", "success")
        else:
            flash(f"Не удалось обновить адреса XUI для хоста '{host_name}'.", "error")
        return redirect(url_for("settings_page", group="delivery", host=host_name))

    @flask_app.route("/add-plan", methods=["POST"])
    @login_required
    def add_plan_route():
        create_plan(
            host_name=request.form["host_name"],
            plan_name=request.form["plan_name"],
            months=int(request.form["months"]),
            price=float(request.form["price"]),
        )
        flash(f"Новый тариф для хоста '{request.form['host_name']}' добавлен.", "success")
        return redirect(url_for("settings_page", group="delivery", host=request.form["host_name"]))

    @flask_app.route("/delete-plan/<int:plan_id>", methods=["POST"])
    @login_required
    def delete_plan_route(plan_id):
        delete_plan(plan_id)
        flash("Тариф успешно удален.", "success")
        return redirect(url_for("settings_page", group="delivery"))

    @flask_app.route("/api/host/<host_name>/maintenance", methods=["POST"])
    @login_required
    def toggle_host_maintenance(host_name):
        data = request.get_json(silent=True) or {}
        enabled = bool(data.get("enabled", False))
        set_host_maintenance(host_name, enabled)
        state = "включен" if enabled else "выключен"
        return jsonify({"status": "ok", "message": f"Режим обслуживания {state} для {host_name}"})

    @flask_app.route("/api/hosts/status")
    @login_required
    def hosts_status_api():
        hosts = filter_customer_visible_hosts(get_hosts_with_status())
        return jsonify({"hosts": hosts})

    # ── Migration UI ─────────────────────────────────────────────
    # In-memory store for migration progress (single-process Flask).
    import threading as _threading

    _migration_jobs: dict[str, dict] = {}
    _migration_jobs_lock = _threading.Lock()
    _MIGRATION_JOBS_MAX = 50  # evict oldest completed jobs when exceeded

    def _parse_migration_gift_days(raw_value, *, default: int = 0) -> int:
        try:
            parsed = int(raw_value if raw_value is not None else default)
        except (TypeError, ValueError) as exc:
            raise ValueError("Подарочные дни должны быть целым числом") from exc
        if parsed < 0 or parsed > 365:
            raise ValueError("Подарочные дни должны быть в диапазоне 0..365")
        return parsed

    def _parse_marketing_keep_old_keys(data: dict) -> bool:
        if not isinstance(data, dict) or "keep_old_keys" not in data:
            return True
        return _coerce_checkbox_value(data.get("keep_old_keys"))

    def _normalize_marketing_segment(raw_value) -> str:
        normalized = str(raw_value or "").strip().lower()
        if normalized in {"standard", "super", "all"}:
            return normalized
        return "all"

    def _normalize_host_url(raw_value) -> str:
        return str(raw_value or "").strip().rstrip("/").lower()

    def _resolve_super_host_aliases(hosts: list[dict]) -> set[str]:
        aliases: set[str] = set()
        configured = (get_setting("super_key_host_name") or "").strip()
        if not configured:
            return aliases

        aliases.add(configured)
        configured_host = next(
            (host for host in hosts if str(host.get("host_name") or "").strip() == configured),
            None,
        )
        if not configured_host:
            configured_host = get_host(configured)
        configured_url = _normalize_host_url((configured_host or {}).get("host_url"))
        if not configured_url:
            return aliases

        for host in hosts:
            host_name = str(host.get("host_name") or "").strip()
            if not host_name:
                continue
            if _normalize_host_url(host.get("host_url")) == configured_url:
                aliases.add(host_name)
        return aliases

    def _collect_marketing_candidates(segment: str) -> list[dict]:
        normalized_segment = _normalize_marketing_segment(segment)
        hosts = get_all_hosts()
        if not hosts:
            return []

        now = datetime.now()
        super_aliases = _resolve_super_host_aliases(hosts)
        candidates_by_user: dict[int, dict] = {}

        for host in hosts:
            host_name = str(host.get("host_name") or "").strip()
            if not host_name:
                continue

            tx_type = classify_product_type(host_name)
            is_super = host_name in super_aliases or tx_type == TX_TYPE_SUPER
            is_standard = tx_type == TX_TYPE_VLESS and not is_super

            include_host = (
                (normalized_segment == "standard" and is_standard)
                or (normalized_segment == "super" and is_super)
                or (normalized_segment == "all" and (is_standard or is_super))
            )
            if not include_host:
                continue

            keys = get_all_keys_for_host_migration(host_name, include_trial_super=False)
            for key in keys:
                try:
                    user_id = int(key.get("user_id") or 0)
                except (TypeError, ValueError):
                    continue
                if user_id <= 0:
                    continue

                key_id = int(key.get("key_id") or 0)
                expiry_dt = _parse_datetime(key.get("expiry_date")) or now
                if expiry_dt <= now:
                    continue

                candidate = {
                    "user_id": user_id,
                    "source_key_id": key_id,
                    "source_host": host_name,
                    "source_key_email": str(key.get("key_email") or "").strip(),
                    "source_segment": "super" if is_super else "standard",
                    "expiry_date": expiry_dt.isoformat(),
                    "_expiry_dt": expiry_dt,
                }

                previous = candidates_by_user.get(user_id)
                if not previous:
                    candidates_by_user[user_id] = candidate
                    continue
                prev_expiry = previous.get("_expiry_dt") or now
                if expiry_dt > prev_expiry:
                    candidates_by_user[user_id] = candidate
                    continue
                if expiry_dt == prev_expiry and key_id > int(previous.get("source_key_id") or 0):
                    candidates_by_user[user_id] = candidate

        result: list[dict] = []
        for candidate in candidates_by_user.values():
            item = dict(candidate)
            item.pop("_expiry_dt", None)
            result.append(item)
        result.sort(key=lambda item: (int(item.get("user_id") or 0), int(item.get("source_key_id") or 0)))
        return result

    @flask_app.route("/migration")
    @login_required
    def migration_page():
        hosts = filter_customer_visible_hosts(get_all_hosts())
        issue_hosts = []
        for host in hosts:
            issue_host = dict(host)
            issue_host["has_direct_subscription_url"] = bool(xui_api._resolve_subscription_base_url(issue_host))
            issue_hosts.append(issue_host)
        if blitz_api.is_blitz_enabled():
            has_blitz_option = any(
                str(host.get("host_name") or "").strip() == blitz_api.BLITZ_HOST_NAME for host in issue_hosts
            )
            if not has_blitz_option:
                issue_hosts.append(
                    {
                        "host_name": blitz_api.BLITZ_HOST_NAME,
                        "host_url": "Blitz (Hysteria 2)",
                        "has_direct_subscription_url": False,
                    }
                )
        return render_template(
            "migration.html",
            hosts=hosts,
            issue_hosts=filter_customer_visible_hosts(issue_hosts),
            runtime_bot_source=_normalize_manual_bot_source(BOT_SOURCE),
            issue_notification_template=get_subscription_ready_notification_template(dynamic_quota=True),
        )

    @flask_app.route("/issue-by-id")
    @login_required
    def issue_by_id_page():
        return redirect(url_for("migration_page", tab="issue"))

    issue_idempotency_lock = threading.Lock()
    issue_idempotency_entries: dict[str, dict] = {}

    def _issue_request_fingerprint(data: dict) -> str:
        payload = {key: value for key, value in data.items() if key != "request_id"}
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _issue_idempotent_execute(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            data = request.get_json(silent=True) or {}
            request_id = str(data.get("request_id") or "").strip()
            if not request_id:
                return handler(*args, **kwargs)
            if not _ISSUE_REQUEST_ID_RE.fullmatch(request_id):
                return jsonify({"error": "Некорректный идентификатор операции выдачи"}), 400

            fingerprint = _issue_request_fingerprint(data)
            now = time.monotonic()
            owner = False
            with issue_idempotency_lock:
                expired_ids = [
                    entry_id
                    for entry_id, entry in issue_idempotency_entries.items()
                    if entry.get("completed_at") is not None
                    and now - float(entry["completed_at"]) > _ISSUE_IDEMPOTENCY_TTL_SECONDS
                ]
                for expired_id in expired_ids:
                    issue_idempotency_entries.pop(expired_id, None)

                entry = issue_idempotency_entries.get(request_id)
                if entry and entry.get("fingerprint") != fingerprint:
                    return jsonify({"error": "Идентификатор операции уже использован с другими параметрами"}), 409
                if entry is None:
                    entry = {
                        "fingerprint": fingerprint,
                        "event": threading.Event(),
                        "completed_at": None,
                        "payload": None,
                        "status_code": None,
                    }
                    issue_idempotency_entries[request_id] = entry
                    owner = True

            if not owner:
                if not entry["event"].wait(timeout=_ISSUE_IDEMPOTENCY_WAIT_SECONDS):
                    pending_response = jsonify(
                        {
                            "error": "Выдача с этим идентификатором ещё выполняется. Повторите проверку позже.",
                            "request_id": request_id,
                        }
                    )
                    pending_response.status_code = 409
                    pending_response.headers["X-Issue-Request-In-Progress"] = "true"
                    return pending_response
                with issue_idempotency_lock:
                    completed_entry = issue_idempotency_entries.get(request_id) or {}
                    payload = completed_entry.get("payload")
                    status_code = completed_entry.get("status_code")
                if payload is None or status_code is None:
                    return jsonify({"error": "Не удалось получить результат предыдущей выдачи"}), 503
                replay = jsonify(payload)
                replay.status_code = int(status_code)
                replay.headers["X-Issue-Idempotent-Replay"] = "true"
                return replay

            try:
                response = current_app.make_response(handler(*args, **kwargs))
                payload = response.get_json(silent=True)
                with issue_idempotency_lock:
                    entry["payload"] = payload
                    entry["status_code"] = response.status_code
                    entry["completed_at"] = time.monotonic()
                    entry["event"].set()
                response.headers["X-Issue-Request-Id"] = request_id
                return response
            except BaseException:
                with issue_idempotency_lock:
                    failed_entry = issue_idempotency_entries.pop(request_id, None)
                    if failed_entry:
                        failed_entry["event"].set()
                raise

        return wrapped

    @flask_app.route("/api/issue-by-id/status/<request_id>", methods=["GET"])
    @login_required
    def issue_by_id_status_api(request_id: str):
        request_id = str(request_id or "").strip()
        if not _ISSUE_REQUEST_ID_RE.fullmatch(request_id):
            return jsonify({"error": "Некорректный идентификатор операции выдачи"}), 400
        with issue_idempotency_lock:
            entry = issue_idempotency_entries.get(request_id)
            if entry is None:
                return jsonify({"error": "Операция выдачи не найдена", "request_id": request_id}), 404
            completed = entry.get("completed_at") is not None
            payload = entry.get("payload")
            status_code = entry.get("status_code")
        if not completed:
            response = jsonify({"status": "pending", "request_id": request_id})
            response.status_code = 202
            response.headers["X-Issue-Request-In-Progress"] = "true"
            return response
        if payload is None or status_code is None:
            return jsonify({"error": "Результат операции выдачи недоступен", "request_id": request_id}), 503
        response = jsonify(payload)
        response.status_code = int(status_code)
        response.headers["X-Issue-Idempotent-Replay"] = "true"
        return response

    def _issue_parse_telegram_id(raw_value) -> int:
        try:
            telegram_id = int(str(raw_value).strip())
        except (TypeError, ValueError):
            raise ValueError("Telegram ID должен быть числом") from None
        if telegram_id <= 0:
            raise ValueError("Telegram ID должен быть положительным")
        return telegram_id

    def _issue_parse_key_id(raw_value) -> int:
        try:
            key_id = int(str(raw_value).strip())
        except (TypeError, ValueError):
            raise ValueError("ID ключа должен быть числом") from None
        if key_id <= 0:
            raise ValueError("ID ключа должен быть положительным")
        return key_id

    def _issue_to_bool(raw_value) -> bool:
        if isinstance(raw_value, bool):
            return raw_value
        if isinstance(raw_value, (int, float)):
            return bool(raw_value)
        if isinstance(raw_value, str):
            return raw_value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw_value)

    def _issue_parse_subscription_tier(raw_value) -> str:
        tier = str(raw_value or "super").strip().lower()
        if tier in {"super", "standard"}:
            return tier
        raise ValueError("Некорректный тариф: выберите standard или super")

    def _issue_include_white_inbounds(subscription_tier: str) -> bool:
        return subscription_tier in {"standard", "super"}

    def _issue_whitelist_traffic_limit_bytes(
        subscription_tier: str, *, unlimited: bool = False, key_data: dict | None = None
    ) -> int:
        if subscription_tier == xui_api.SUBSCRIPTION_TIER_SUPER and unlimited:
            return 0
        return xui_api.whitelist_traffic_limit_bytes_for_key({
            **(key_data or {}),
            "subscription_tier": subscription_tier,
        })

    def _issue_main_panel_host_name() -> str | None:
        standard_host_name = str(get_setting("standard_key_host_name") or "").strip()
        super_host_name = str(get_setting("super_key_host_name") or "").strip()
        if standard_host_name and super_host_name and standard_host_name == super_host_name:
            return standard_host_name
        return None

    def _issue_validate_main_panel_host(host_name: str) -> str | None:
        normalized_host_name = str(host_name or "").strip()
        main_panel_host_name = _issue_main_panel_host_name()
        if not main_panel_host_name:
            return (
                "MainPanel не настроен для #90: standard_key_host_name и super_key_host_name "
                "должны указывать на одну общую панель."
            )
        if normalized_host_name != main_panel_host_name:
            return (
                f'Для #90 выдача Standard/Super разрешена только через "{main_panel_host_name}". '
                f'Выбранный хост "{normalized_host_name or "—"}" не участвует в этой выдаче.'
            )
        return None

    def _mainpanel_migration_host_name() -> str:
        main_panel_host_name = _issue_main_panel_host_name()
        if not main_panel_host_name:
            raise ValueError(
                "MainPanel не настроен для #93: standard_key_host_name и super_key_host_name "
                "должны указывать на одну общую панель."
            )
        if not get_host(main_panel_host_name):
            raise ValueError(f'MainPanel "{main_panel_host_name}" не найден в базе host-ов.')
        return main_panel_host_name

    def _mainpanel_inbound_sync_host_name(data: dict | None = None) -> str:
        requested_host = str((data or {}).get("host_name") or "").strip()
        if requested_host:
            if not get_host(requested_host):
                raise ValueError(f'Host "{requested_host}" не найден в базе host-ов.')
            return requested_host
        return _mainpanel_migration_host_name()

    def _parse_mainpanel_inbound_sync_batch_limit(raw_value, *, default: int = 100) -> int:
        try:
            value = int(raw_value if raw_value is not None else default)
        except (TypeError, ValueError) as exc:
            raise ValueError("Batch limit должен быть целым числом") from exc
        if value < 0 or value > 500:
            raise ValueError("Batch limit должен быть в диапазоне 0..500")
        return value

    def _public_mainpanel_inbound_sync_report(report: dict | None) -> dict:
        result = dict(report or {})
        if "items" in result and "details" not in result:
            result["details"] = result["items"]
        result.setdefault("details", [])
        result.setdefault("inbounds", [])
        result.setdefault("processed", 0)
        result.setdefault("updated", 0)
        result.setdefault("skipped", 0)
        result.setdefault("failed", 0)
        result.setdefault("needs_review", 0)
        result.setdefault("total", result.get("processed", 0))
        result.setdefault("status", "unknown")
        return result

    def _mainpanel_public_detail(item: dict) -> dict:
        return {key: value for key, value in item.items() if not str(key).startswith("_") and key != "target_expiry_ms"}

    def _build_mainpanel_migration_plan(
        *,
        subscription_tier: str,
        bonus_days: int,
        incident_id: str | None,
    ) -> dict:
        main_panel_host_name = _mainpanel_migration_host_name()
        source_keys = get_mainpanel_migration_source_keys(main_panel_host_name, include_trial_super=False)
        transactions_by_key = get_successful_transactions_for_migration_keys(source_keys)
        now = datetime.now(UTC).replace(tzinfo=None)
        details: list[dict] = []
        for key in source_keys:
            try:
                key_id = int(key.get("key_id") or 0)
            except (TypeError, ValueError):
                key_id = 0
            existing = get_migration_for_key(key_id, incident_id) if key_id and incident_id else None
            replacement = get_replacement_key_for_incident(key_id, incident_id) if key_id and incident_id else None
            details.append(
                mainpanel_migration.build_migration_item(
                    key,
                    transaction_rows=transactions_by_key.get(key_id, []),
                    target_tier=subscription_tier,
                    main_panel_host_name=main_panel_host_name,
                    bonus_days=bonus_days,
                    incident_id=incident_id,
                    existing_migration=existing,
                    replacement_key=replacement,
                    now=now,
                )
            )

        summary = mainpanel_migration.summarize_items(details)
        return {
            **summary,
            "subscription_tier": subscription_tier,
            "bonus_days": bonus_days,
            "new_host": main_panel_host_name,
            "expired_policy": "skip",
            "details": details,
        }

    def _issue_panel_error_payload(base_message: str, failure_context: dict[str, str]) -> dict[str, str]:
        reason = str(failure_context.get("reason") or "panel_call_failed").strip()
        return {"error": f"{base_message} ({reason})", "reason": reason}

    def _issue_format_expiry_label(expiry_dt: datetime, *, unlimited: bool) -> str:
        if unlimited:
            return "Безлимит"
        return expiry_dt.strftime("%d.%m.%Y %H:%M")

    def _issue_default_subscription_notification_template(
        subscription_tier: str = "super", *, unlimited: bool = False, key_data: dict | None = None
    ) -> str:
        quota = _issue_whitelist_traffic_limit_bytes(
            subscription_tier, unlimited=unlimited, key_data=key_data
        )
        return get_subscription_ready_notification_template(quota // (1024 ** 3))

    def _issue_render_notification_text(
        *,
        template: str,
        host_name: str,
        expiry_label: str,
        key_value: str,
        key_email: str,
        key_id: int | None = None,
        whitelist_limit_gb: int = 40,
    ) -> str:
        rendered = (
            template.replace("{host}", host_name or "—")
            .replace("{expiry}", expiry_label or "—")
            .replace("{key}", key_value or "—")
            .replace("{email}", key_email or "—")
            .replace("{whitelist_policy}", get_whitelist_policy_text(whitelist_limit_gb))
        )
        return rendered.replace("{key_id}", str(key_id or "—"))

    def _issue_send_notification(
        *,
        telegram_id: int,
        bot_source: str,
        text: str,
    ) -> tuple[bool, str | None]:
        bot_token = _resolve_runtime_bot_token(bot_source)
        if not bot_token:
            return False, "Токен Telegram-бота не задан"
        try:
            from aiogram import Bot
            from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

            bot = Bot(token=bot_token)
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Мои ключи", callback_data="manage_keys")],
                    [InlineKeyboardButton(text="В меню", callback_data="back_to_main_menu")],
                ]
            )
            notify_loop = asyncio.new_event_loop()
            try:
                notify_loop.run_until_complete(
                    bot.send_message(
                        chat_id=telegram_id,
                        text=text,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                )
            finally:
                try:
                    notify_loop.run_until_complete(bot.session.close())
                except Exception:
                    pass
                notify_loop.close()
            return True, None
        except Exception as exc:
            logger.warning("Manual issue notify failed for user %s: %s", telegram_id, exc)
            return False, str(exc)[:300]

    def _resolve_issue_extension_options(
        *,
        base_expiry_dt: datetime,
        raw_days,
        raw_months,
        raw_years,
        raw_unlimited,
        now: datetime | None = None,
    ) -> dict[str, object]:
        safe_now = now or datetime.now()
        unlimited = _coerce_checkbox_value(raw_unlimited)
        days = _parse_issue_duration_value(raw_days, field_label="Дни", max_value=3650)
        months = _parse_issue_duration_value(raw_months, field_label="Месяцы", max_value=99)
        years = _parse_issue_duration_value(raw_years, field_label="Годы", max_value=99)

        if unlimited:
            expiry_dt = datetime(2099, 12, 31, 23, 59, 59)
            return {
                "days": days,
                "months": months,
                "years": years,
                "unlimited": True,
                "panel_days": panel_days_until(expiry_dt, now=safe_now),
                "target_expiry_ms": int(expiry_dt.timestamp() * 1000),
                "expiry_dt": expiry_dt,
                "expiry_label": "Безлимит",
            }

        if not any((days, months, years)):
            raise ValueError("Укажите срок: дни, месяцы, годы или включите безлимит")

        anchor = base_expiry_dt if base_expiry_dt > safe_now else safe_now
        expiry_dt = add_calendar_months(anchor, months + (years * 12))
        if days:
            expiry_dt += timedelta(days=days)
        return {
            "days": days,
            "months": months,
            "years": years,
            "unlimited": False,
            "panel_days": panel_days_until(expiry_dt, now=safe_now),
            "target_expiry_ms": int(expiry_dt.timestamp() * 1000),
            "expiry_dt": expiry_dt,
            "expiry_label": expiry_dt.strftime("%d.%m.%Y %H:%M"),
        }

    def _issue_build_keys_context(telegram_id: int) -> list[dict]:
        now = datetime.now()
        result: list[dict] = []
        for key in get_user_keys(telegram_id) or []:
            try:
                key_id = int(key.get("key_id") or 0)
            except (TypeError, ValueError):
                continue
            if key_id <= 0:
                continue
            is_archived = bool(int(key.get("is_archived") or 0))
            expiry_dt = _parse_datetime(key.get("expiry_date"))
            is_expired = _is_key_expired(key.get("expiry_date"), is_archived=is_archived)
            is_active = not is_archived and not is_expired
            expiry_label = _ru_date(key.get("expiry_date"))
            result.append(
                {
                    "key_id": key_id,
                    "host_name": str(key.get("host_name") or "").strip(),
                    "key_email": str(key.get("key_email") or "").strip(),
                    "xui_client_uuid": str(key.get("xui_client_uuid") or "").strip(),
                    "expiry_date": expiry_dt.isoformat() if expiry_dt else None,
                    "expiry_label": expiry_label,
                    "is_archived": is_archived,
                    "is_expired": is_expired,
                    "is_active": is_active,
                    "remaining_days": round(max((expiry_dt - now).total_seconds(), 0) / 86400, 1)
                    if expiry_dt and not is_archived
                    else 0,
                }
            )
        result.sort(
            key=lambda item: (
                1 if item.get("is_active") else 0,
                item.get("expiry_date") or "",
                int(item.get("key_id") or 0),
            ),
            reverse=True,
        )
        return result

    @flask_app.route("/api/issue-by-id/context", methods=["POST"])
    @login_required
    def issue_by_id_context_api():
        data = request.get_json(silent=True) or {}
        try:
            telegram_id = _issue_parse_telegram_id(data.get("telegram_id"))
            subscription_tier = _issue_parse_subscription_tier(data.get("subscription_tier"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        user = get_user(telegram_id)
        if not user:
            return jsonify({"error": f"Пользователь {telegram_id} не найден"}), 404

        keys = _issue_build_keys_context(telegram_id)
        return jsonify(
            {
                "telegram_id": telegram_id,
                "subscription_tier": subscription_tier,
                "username": str(user.get("username") or "").strip(),
                "is_banned": bool(user.get("is_banned")),
                "total_spent": float(user.get("total_spent") or 0),
                "keys_total": len(keys),
                "keys_active": sum(1 for item in keys if item.get("is_active")),
                "keys": keys,
            }
        )

    @flask_app.route("/api/issue-by-id/preview", methods=["POST"])
    @login_required
    def issue_by_id_preview_api():
        data = request.get_json(silent=True) or {}
        mode = str(data.get("mode") or "new").strip().lower()
        if mode not in {"new", "replace", "extend"}:
            return jsonify({"error": "Некорректный режим"}), 400

        try:
            telegram_id = _issue_parse_telegram_id(data.get("telegram_id"))
            subscription_tier = _issue_parse_subscription_tier(data.get("subscription_tier"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        selected_key = None
        target_key_raw = data.get("target_key_id")
        target_key_id = None
        if target_key_raw not in (None, ""):
            try:
                target_key_id = _issue_parse_key_id(target_key_raw)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400
            selected_key = get_key_by_id(target_key_id)
            if not selected_key or int(selected_key.get("user_id") or 0) != telegram_id:
                return jsonify({"error": f"Ключ {target_key_id} не принадлежит пользователю {telegram_id}"}), 400

        host_name = str(data.get("host_name") or "").strip()
        if selected_key:
            selected_host = str(selected_key.get("host_name") or "").strip()
            if host_name and host_name != selected_host:
                return jsonify({"error": f"Выбранный ключ принадлежит хосту {selected_host}"}), 400
            host_name = selected_host
        if not host_name:
            return jsonify({"error": "Выберите хост"}), 400
        main_panel_error = _issue_validate_main_panel_host(host_name)
        if main_panel_error:
            return jsonify({"error": main_panel_error}), 400

        now = datetime.now()
        try:
            if mode == "extend":
                if not selected_key:
                    return jsonify({"error": "Для продления укажите target_key_id"}), 400
                base_expiry = _parse_datetime(selected_key.get("expiry_date")) or now
                duration = _resolve_issue_extension_options(
                    base_expiry_dt=base_expiry,
                    raw_days=data.get("duration_days", 0),
                    raw_months=data.get("duration_months", 0),
                    raw_years=data.get("duration_years", 0),
                    raw_unlimited=data.get("unlimited", False),
                    now=now,
                )
                base_label = _ru_date(base_expiry)
            else:
                duration = _resolve_issue_expiry_options(
                    raw_days=data.get("duration_days", 30),
                    raw_months=data.get("duration_months", 0),
                    raw_years=data.get("duration_years", 0),
                    raw_unlimited=data.get("unlimited", False),
                    now=now,
                )
                base_label = _ru_date(now)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify(
            {
                "status": "ok",
                "mode": mode,
                "telegram_id": telegram_id,
                "host_name": host_name,
                "subscription_tier": subscription_tier,
                "target_key_id": target_key_id,
                "base_expiry_label": base_label,
                "new_expiry_label": _issue_format_expiry_label(
                    duration["expiry_dt"],
                    unlimited=bool(duration["unlimited"]),
                ),
                "panel_days": int(duration["panel_days"]),
                "target_expiry_ms": int(duration["target_expiry_ms"]),
                "duration_days": int(duration["days"]),
                "duration_months": int(duration["months"]),
                "duration_years": int(duration["years"]),
                "unlimited": bool(duration["unlimited"]),
            }
        )

    @flask_app.route("/api/issue-by-id/execute", methods=["POST"])
    @login_required
    @_issue_idempotent_execute
    def issue_by_id_execute_api():
        data = request.get_json(silent=True) or {}

        try:
            telegram_id = _issue_parse_telegram_id(data.get("telegram_id"))
            subscription_tier = _issue_parse_subscription_tier(data.get("subscription_tier"))
            issuance_started_at = datetime.now()
            duration_options = _resolve_issue_expiry_options(
                raw_days=data.get("duration_days", 30),
                raw_months=data.get("duration_months", 0),
                raw_years=data.get("duration_years", 0),
                raw_unlimited=data.get("unlimited", False),
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        send_message = _issue_to_bool(data.get("send_message", True))
        replace_existing = _issue_to_bool(data.get("replace_existing", False))
        message_template = (data.get("message_text") or "").strip()
        selected_bot_source = _normalize_manual_bot_source(data.get("bot_source"))
        target_key_id = None
        target_key_raw = data.get("target_key_id")
        if target_key_raw not in (None, ""):
            try:
                target_key_id = _issue_parse_key_id(target_key_raw)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 400

        user = get_user(telegram_id)
        user_created = False
        if not user:
            register_user_if_not_exists(telegram_id, f"user_{telegram_id}", None)
            user = get_user(telegram_id)
            user_created = bool(user)
            if not user:
                return jsonify({"error": "Не удалось создать запись пользователя в БД"}), 500

        host_name = (data.get("host_name") or "").strip()
        replaced_key_id = None
        if replace_existing:
            if target_key_id:
                selected_key = get_key_by_id(target_key_id)
                if not selected_key or int(selected_key.get("user_id") or 0) != telegram_id:
                    return jsonify({"error": f"Ключ {target_key_id} не принадлежит пользователю {telegram_id}"}), 400
                if bool(int(selected_key.get("is_archived") or 0)):
                    return jsonify({"error": f"Ключ {target_key_id} уже архивирован"}), 400
                selected_host_name = str(selected_key.get("host_name") or "").strip()
                if host_name and host_name != selected_host_name:
                    return jsonify({"error": f"Выбранный ключ принадлежит хосту {selected_host_name}"}), 400
                host_name = selected_host_name
                replaced_key_id = target_key_id

            if not host_name:
                return jsonify({"error": "Выберите хост"}), 400

        if not host_name:
            return jsonify({"error": "Выберите хост"}), 400
        is_blitz_host = host_name == blitz_api.BLITZ_HOST_NAME
        # Blitz is an independent provider offered by the issue-by-ID UI.
        # MainPanel restrictions apply to the XUI path; Blitz still requires
        # its explicit availability check below before any provider operation.
        if not is_blitz_host:
            main_panel_error = _issue_validate_main_panel_host(host_name)
            if main_panel_error:
                return jsonify({"error": main_panel_error}), 400

        failure_context: dict[str, str] = {}
        if is_blitz_host:
            if not blitz_api.is_blitz_enabled():
                return jsonify({"error": "Выдача через Blitz отключена или не настроена"}), 400
        elif not get_host(host_name):
            return jsonify({"error": f'Хост "{host_name}" не найден'}), 400

        if replace_existing and not replaced_key_id:
            now = datetime.now()
            user_keys = get_user_keys(telegram_id) or []
            candidates = [
                key
                for key in user_keys
                if str(key.get("host_name") or "") == host_name and int(key.get("is_archived") or 0) == 0
            ]
            if not candidates:
                return jsonify(
                    {
                        "error": f"У пользователя {telegram_id} нет ключа на хосте {host_name} для замены",
                    }
                ), 400

            # Prefer active key with latest expiry; fallback to latest inserted key_id.
            def _candidate_sort_key(item: dict):
                expiry_raw = str(item.get("expiry_date") or "").strip()
                try:
                    expiry_dt = datetime.fromisoformat(expiry_raw) if expiry_raw else now
                except ValueError:
                    expiry_dt = now
                is_active = 1 if expiry_dt > now else 0
                return (is_active, expiry_dt.timestamp(), int(item.get("key_id") or 0))

            candidates.sort(key=_candidate_sort_key, reverse=True)
            replaced_key_id = int(candidates[0].get("key_id") or 0) or None

        key_email = None
        if not is_blitz_host:
            key_number = get_next_key_number(telegram_id)
            key_email = f"user{telegram_id}-key{key_number}@vpn.bot"
            while get_key_by_email(key_email):
                key_number += 1
                key_email = f"user{telegram_id}-key{key_number}@vpn.bot"

        if is_blitz_host:
            blitz_username = f"bk_{secrets.token_hex(10)}"
            loop = asyncio.new_event_loop()
            try:
                created = loop.run_until_complete(
                    blitz_api.create_user(
                        username=blitz_username,
                        expiry_days=int(duration_options["panel_days"]),
                        unlimited=True,
                    )
                )
                if not created:
                    return jsonify({"error": "Blitz не создал пользователя"}), 502

                connection_uri = loop.run_until_complete(blitz_api.get_connection_uri(blitz_username))
            finally:
                loop.close()

            if not connection_uri:
                return jsonify({"error": "Blitz не вернул ссылку подключения"}), 502

            panel_result = {
                "client_uuid": blitz_username,
                "email": blitz_username,
                "expiry_timestamp_ms": int(duration_options["target_expiry_ms"]),
                "connection_string": connection_uri,
                "host_name": blitz_api.BLITZ_HOST_NAME,
            }
        else:
            loop = asyncio.new_event_loop()
            try:
                panel_result = None
                candidate_emails: list[str] = []
                for offset in range(0, 120):
                    candidate_email = f"user{telegram_id}-key{key_number + offset}@vpn.bot"
                    if get_key_by_email(candidate_email):
                        continue
                    candidate_emails.append(candidate_email)
                    if len(candidate_emails) >= 40:
                        break
                if not candidate_emails:
                    failure_context["reason"] = "email_candidates_exhausted"
                    return jsonify(
                        _issue_panel_error_payload("Не удалось подобрать свободный email для нового ключа", failure_context)
                    ), 502

                try:
                    available_emails, conflicts_by_email = xui_api.filter_available_subscription_base_emails_on_host(
                        host_name,
                        candidate_emails,
                        include_white_inbounds=_issue_include_white_inbounds(subscription_tier),
                        require_white_inbound=_issue_include_white_inbounds(subscription_tier),
                    )
                except Exception as exc:
                    logger.warning(
                        "Issue-by-ID panel email preflight failed for user_id=%s host=%s: %s",
                        telegram_id,
                        host_name,
                        exc,
                        exc_info=True,
                    )
                    available_emails = candidate_emails
                    conflicts_by_email = {}

                if conflicts_by_email:
                    conflict_sample = []
                    for base_email, conflicts in conflicts_by_email.items():
                        conflict_sample.append(f"{base_email}=>{','.join(conflicts[:3])}")
                        if len(conflict_sample) >= 5:
                            break
                    failure_context["panel_email_conflicts"] = ";".join(conflict_sample)

                issue_emails = available_emails[:5]
                if not issue_emails:
                    failure_context["reason"] = "panel_email_candidates_exhausted"
                    return jsonify(
                        _issue_panel_error_payload("На панели не найден свободный email для нового ключа", failure_context)
                    ), 502

                for issue_email in issue_emails:
                    if not issue_email:
                        continue
                    key_email = issue_email
                    attempt_context: dict[str, str] = {}
                    panel_result = loop.run_until_complete(
                        xui_api.create_or_update_key_on_host(
                            host_name,
                            issue_email,
                            int(duration_options["panel_days"]),
                            target_expiry_ms=int(duration_options["target_expiry_ms"]),
                            white_traffic_limit_bytes=_issue_whitelist_traffic_limit_bytes(
                                subscription_tier, unlimited=bool(duration_options["unlimited"])
                            ),
                            whitelist_cycle_anchor_ms=int(issuance_started_at.timestamp() * 1000),
                            include_white_inbounds=_issue_include_white_inbounds(subscription_tier),
                            require_white_inbound=_issue_include_white_inbounds(subscription_tier),
                            manual_super_whitelist_postflight=(
                                subscription_tier == xui_api.SUBSCRIPTION_TIER_SUPER
                            ),
                            panel_call_timeout_seconds=MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
                            panel_call_retries=1,
                            max_attempts=1,
                            failure_context=attempt_context,
                        )
                    )
                    if panel_result:
                        break
                    failure_context.update(attempt_context)
            finally:
                loop.close()

        if not panel_result:
            return jsonify(
                _issue_panel_error_payload("Панель не вернула данные для нового ключа", failure_context)
            ), 502

        issued_key_value = _issued_key_value_from_panel_result(
            panel_result,
            subscription_tier=subscription_tier,
        )
        key_id = add_new_key(
            user_id=telegram_id,
            host_name=host_name,
            xui_client_uuid=panel_result["client_uuid"],
            key_email=panel_result["email"],
            expiry_timestamp_ms=panel_result["expiry_timestamp_ms"],
            issued_key_value=issued_key_value,
            created_date=issuance_started_at,
            bot_source=selected_bot_source,
            panel_sub_id=panel_result.get("sub_id"),
            subscription_payload=xui_api.subscription_payload_from_result(
                panel_result,
                subscription_tier=subscription_tier,
            ),
            subscription_tier=subscription_tier,
            whitelist_cycle_anchor_at=issuance_started_at,
        )
        if not key_id:
            return jsonify({"error": "Ключ создан на панели, но не сохранён в БД"}), 500
        if replaced_key_id:
            archive_key(
                replaced_key_id,
                replaced_due_to_incident=False,
                migration_reason="manual_replace_by_id",
            )

        expiry_dt = datetime.fromtimestamp(panel_result["expiry_timestamp_ms"] / 1000, tz=UTC).astimezone()
        expiry_ru = str(duration_options.get("expiry_label") or expiry_dt.strftime("%d.%m.%Y %H:%M"))
        default_text = _issue_default_subscription_notification_template(
            subscription_tier, unlimited=bool(duration_options["unlimited"])
        )
        template = message_template or default_text
        rendered_message = (
            template.replace("{host}", host_name)
            .replace("{expiry}", expiry_ru)
            .replace("{key}", issued_key_value or "—")
            .replace("{email}", panel_result.get("email") or "—")
            .replace("{key_id}", str(key_id))
            .replace("{whitelist_policy}", get_whitelist_policy_text(
                _issue_whitelist_traffic_limit_bytes(
                    subscription_tier, unlimited=bool(duration_options["unlimited"])
                ) // (1024 ** 3)
            ))
        )

        sent, send_error = (False, None)
        if send_message:
            sent, send_error = _issue_send_notification(
                telegram_id=telegram_id,
                bot_source=selected_bot_source,
                text=rendered_message,
            )

        return jsonify(
            {
                "status": "ok",
                "bot_source": selected_bot_source,
                "telegram_id": telegram_id,
                "host_name": host_name,
                "duration_days": int(duration_options["days"]),
                "duration_months": int(duration_options["months"]),
                "duration_years": int(duration_options["years"]),
                "unlimited": bool(duration_options["unlimited"]),
                "replace_existing": replace_existing,
                "subscription_tier": subscription_tier,
                "target_key_id": target_key_id,
                "replaced_key_id": replaced_key_id,
                "key_id": key_id,
                "key_email": panel_result.get("email"),
                "expiry": expiry_dt.isoformat(),
                "expiry_label": expiry_ru,
                "issued_key_value": issued_key_value,
                "sent": sent,
                "send_error": send_error,
                "user_created": user_created,
            }
        )

    @flask_app.route("/api/issue-by-id/extend", methods=["POST"])
    @login_required
    def issue_by_id_extend_api():
        data = request.get_json(silent=True) or {}
        try:
            telegram_id = _issue_parse_telegram_id(data.get("telegram_id"))
            target_key_id = _issue_parse_key_id(data.get("target_key_id"))
            subscription_tier = _issue_parse_subscription_tier(data.get("subscription_tier"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        key_data = get_key_by_id(target_key_id)
        if not key_data or int(key_data.get("user_id") or 0) != telegram_id:
            return jsonify({"error": f"Ключ {target_key_id} не принадлежит пользователю {telegram_id}"}), 400
        if bool(int(key_data.get("is_archived") or 0)):
            return jsonify({"error": f"Ключ {target_key_id} уже архивирован"}), 400

        now = datetime.now()
        base_expiry_dt = _parse_datetime(key_data.get("expiry_date")) or now
        whitelist_cycle_anchor_at = resolve_recurring_cycle_anchor(
            paid_at=now,
            current_expiry=base_expiry_dt,
            created_at=_parse_datetime(
                key_data.get("whitelist_cycle_anchor_at") or key_data.get("created_date")
            ),
        )
        try:
            duration_options = _resolve_issue_extension_options(
                base_expiry_dt=base_expiry_dt,
                raw_days=data.get("duration_days", 0),
                raw_months=data.get("duration_months", 0),
                raw_years=data.get("duration_years", 0),
                raw_unlimited=data.get("unlimited", False),
                now=now,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        host_name = str(key_data.get("host_name") or "").strip()
        if not host_name:
            return jsonify({"error": "У ключа не указан хост"}), 400
        main_panel_error = _issue_validate_main_panel_host(host_name)
        if main_panel_error:
            return jsonify({"error": main_panel_error}), 400

        is_blitz_host = host_name == blitz_api.BLITZ_HOST_NAME
        panel_result = None
        failure_context: dict[str, str] = {}
        if is_blitz_host:
            if not blitz_api.is_blitz_enabled():
                return jsonify({"error": "Продление через Blitz отключено или не настроено"}), 400
            blitz_username = str(key_data.get("key_email") or key_data.get("xui_client_uuid") or "").strip()
            if not blitz_username:
                return jsonify({"error": "У blitz-ключа отсутствует username"}), 400
            loop = asyncio.new_event_loop()
            try:
                updated = loop.run_until_complete(
                    blitz_api.update_user(
                        username=blitz_username,
                        new_expiration_days=int(duration_options["panel_days"]),
                        blocked=False,
                    )
                )
                connection_uri = loop.run_until_complete(blitz_api.get_connection_uri(blitz_username))
            finally:
                loop.close()
            if not updated:
                return jsonify({"error": "Blitz не принял продление пользователя"}), 502
            panel_result = {
                "client_uuid": blitz_username,
                "email": blitz_username,
                "expiry_timestamp_ms": int(duration_options["target_expiry_ms"]),
                "connection_string": connection_uri,
                "host_name": host_name,
            }
        else:
            if not get_host(host_name):
                return jsonify({"error": f'Хост "{host_name}" не найден'}), 400
            key_email = str(key_data.get("key_email") or "").strip()
            if not key_email:
                return jsonify({"error": "У ключа отсутствует key_email"}), 400

            loop = asyncio.new_event_loop()
            try:
                panel_result = loop.run_until_complete(
                    xui_api.create_or_update_key_on_host(
                        host_name,
                        key_email,
                        int(duration_options["panel_days"]),
                        target_expiry_ms=int(duration_options["target_expiry_ms"]),
                        target_client_uuid=str(key_data.get("xui_client_uuid") or "").strip() or None,
                        target_sub_id=str(key_data.get("panel_sub_id") or "").strip() or None,
                        white_traffic_limit_bytes=_issue_whitelist_traffic_limit_bytes(
                            subscription_tier,
                            unlimited=bool(duration_options["unlimited"]),
                            key_data=key_data,
                        ),
                        reset_exhausted_limited_traffic=_issue_include_white_inbounds(subscription_tier),
                        whitelist_cycle_anchor_ms=int(whitelist_cycle_anchor_at.timestamp() * 1000),
                        include_white_inbounds=_issue_include_white_inbounds(subscription_tier),
                        require_white_inbound=_issue_include_white_inbounds(subscription_tier),
                        manual_super_whitelist_postflight=(
                            subscription_tier == xui_api.SUBSCRIPTION_TIER_SUPER
                        ),
                        panel_call_timeout_seconds=MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
                        panel_call_retries=1,
                        max_attempts=1,
                        failure_context=failure_context,
                    )
                )
            finally:
                loop.close()

        if not panel_result:
            return jsonify(_issue_panel_error_payload("Панель не вернула данные для продления", failure_context)), 502

        issued_key_value = _issued_key_value_from_panel_result(
            panel_result,
            subscription_tier=subscription_tier,
            fallback_value=str(key_data.get("issued_key_value") or ""),
        )
        update_ok = update_key_info(
            key_id=target_key_id,
            new_xui_uuid=str(panel_result.get("client_uuid") or key_data.get("xui_client_uuid") or ""),
            new_expiry_ms=int(duration_options["target_expiry_ms"]),
            issued_key_value=issued_key_value,
            new_key_email=str(panel_result.get("email") or key_data.get("key_email") or ""),
            panel_sub_id=panel_result.get("sub_id"),
            subscription_payload=xui_api.subscription_payload_from_result(
                panel_result,
                subscription_tier=subscription_tier,
            ),
            subscription_tier=subscription_tier,
            new_whitelist_cycle_anchor_at=whitelist_cycle_anchor_at,
        )
        if not update_ok:
            return jsonify({"error": "Продление прошло на панели, но не сохранилось в БД"}), 500

        send_message = _issue_to_bool(data.get("send_message", True))
        selected_bot_source = _normalize_manual_bot_source(data.get("bot_source"))
        message_template = (data.get("message_text") or "").strip()
        default_text = _issue_default_subscription_notification_template(
            subscription_tier, unlimited=bool(duration_options["unlimited"]), key_data=key_data
        )
        expiry_label = _issue_format_expiry_label(
            duration_options["expiry_dt"],
            unlimited=bool(duration_options["unlimited"]),
        )
        rendered_message = _issue_render_notification_text(
            template=message_template or default_text,
            host_name=host_name,
            expiry_label=expiry_label,
            key_value=issued_key_value,
            key_email=str(panel_result.get("email") or key_data.get("key_email") or ""),
            key_id=target_key_id,
            whitelist_limit_gb=_issue_whitelist_traffic_limit_bytes(
                subscription_tier, unlimited=bool(duration_options["unlimited"]), key_data=key_data
            ) // (1024 ** 3),
        )
        sent, send_error = (False, None)
        if send_message:
            sent, send_error = _issue_send_notification(
                telegram_id=telegram_id,
                bot_source=selected_bot_source,
                text=rendered_message,
            )

        return jsonify(
            {
                "status": "ok",
                "mode": "extend",
                "bot_source": selected_bot_source,
                "telegram_id": telegram_id,
                "host_name": host_name,
                "subscription_tier": subscription_tier,
                "target_key_id": target_key_id,
                "key_id": target_key_id,
                "key_email": str(panel_result.get("email") or key_data.get("key_email") or ""),
                "expiry": duration_options["expiry_dt"].isoformat(),
                "expiry_label": expiry_label,
                "issued_key_value": issued_key_value,
                "duration_days": int(duration_options["days"]),
                "duration_months": int(duration_options["months"]),
                "duration_years": int(duration_options["years"]),
                "unlimited": bool(duration_options["unlimited"]),
                "sent": sent,
                "send_error": send_error,
            }
        )

    @flask_app.route("/api/migration/marketing/preview", methods=["POST"])
    @login_required
    def migration_marketing_preview_api():
        data = request.get_json(silent=True) or {}
        segment = _normalize_marketing_segment(data.get("segment"))
        target_host = (data.get("target_host") or "").strip()
        incident_id = (data.get("incident_id") or "").strip()
        keep_old_keys = _parse_marketing_keep_old_keys(data)
        try:
            subscription_tier = _issue_parse_subscription_tier(data.get("subscription_tier"))
            gift_days = _parse_migration_gift_days(data.get("gift_days", 0), default=0)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if not target_host:
            return jsonify({"error": "Выберите хост для выдачи"}), 400
        main_panel_error = _issue_validate_main_panel_host(target_host)
        if main_panel_error:
            return jsonify({"error": main_panel_error}), 400
        if not get_host(target_host):
            return jsonify({"error": f'Хост "{target_host}" не найден'}), 400

        candidates = _collect_marketing_candidates(segment)
        now = datetime.now()
        details: list[dict] = []
        skipped = 0

        for candidate in candidates:
            source_key_id = int(candidate.get("source_key_id") or 0)
            source_expiry_dt = _parse_datetime(candidate.get("expiry_date")) or now
            remaining_seconds = max((source_expiry_dt - now).total_seconds(), 0)
            remaining_days = round(remaining_seconds / 86400, 1)
            new_expiry = now + timedelta(seconds=remaining_seconds) + timedelta(days=gift_days)
            already_processed = False

            if incident_id:
                existing = get_migration_for_key(source_key_id, incident_id)
                replacement = get_replacement_key_for_incident(source_key_id, incident_id)
                if (existing and existing.get("status") == "migrated") or replacement:
                    already_processed = True
                    skipped += 1

            details.append(
                {
                    "user_id": candidate.get("user_id"),
                    "source_key_id": source_key_id,
                    "source_key_email": candidate.get("source_key_email"),
                    "source_host": candidate.get("source_host"),
                    "source_segment": candidate.get("source_segment"),
                    "remaining_days": remaining_days,
                    "gift_days": gift_days,
                    "new_total_days": round(remaining_days + gift_days, 1),
                    "new_expiry": new_expiry.isoformat(),
                    "old_key_action": "keep" if keep_old_keys else "delete_after_issue",
                    "already_processed": already_processed,
                }
            )

        return jsonify(
            {
                "segment": segment,
                "target_host": target_host,
                "subscription_tier": subscription_tier,
                "keep_old_keys": keep_old_keys,
                "total": len(details),
                "skipped": skipped,
                "details": details,
            }
        )

    @flask_app.route("/api/migration/marketing/execute", methods=["POST"])
    @login_required
    def migration_marketing_execute_api():
        import threading
        import uuid as _uuid

        data = request.get_json(silent=True) or {}
        segment = _normalize_marketing_segment(data.get("segment"))
        target_host = (data.get("target_host") or "").strip()
        incident_id = (data.get("incident_id") or "").strip()
        notification_text = (data.get("notification_text") or "").strip()
        selected_bot_source = _normalize_manual_bot_source(data.get("bot_source"))
        keep_old_keys = _parse_marketing_keep_old_keys(data)

        try:
            subscription_tier = _issue_parse_subscription_tier(data.get("subscription_tier"))
            gift_days = _parse_migration_gift_days(data.get("gift_days", 0), default=0)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        def _to_bool(raw_value) -> bool:
            if isinstance(raw_value, bool):
                return raw_value
            if isinstance(raw_value, (int, float)):
                return bool(raw_value)
            if isinstance(raw_value, str):
                return raw_value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(raw_value)

        send_notify = _to_bool(data.get("send_notify", True))

        if not target_host:
            return jsonify({"error": "Выберите хост для выдачи"}), 400
        main_panel_error = _issue_validate_main_panel_host(target_host)
        if main_panel_error:
            return jsonify({"error": main_panel_error}), 400
        if not get_host(target_host):
            return jsonify({"error": f'Хост "{target_host}" не найден'}), 400
        if not incident_id:
            return jsonify({"error": "Укажите ID кампании"}), 400

        with _migration_jobs_lock:
            if any(
                job.get("status") == "running"
                and job.get("incident_id") == incident_id
                and job.get("mode") == "marketing"
                for job in _migration_jobs.values()
            ):
                return jsonify({"error": f'Маркетинговая выдача для "{incident_id}" уже выполняется'}), 409

            if len(_migration_jobs) >= _MIGRATION_JOBS_MAX:
                completed = [(k, v) for k, v in _migration_jobs.items() if v.get("status") != "running"]
                completed.sort(key=lambda x: x[1].get("_created", 0))
                for k, _ in completed[: len(_migration_jobs) - _MIGRATION_JOBS_MAX + 1]:
                    del _migration_jobs[k]

            job_id = _uuid.uuid4().hex[:12]
            _migration_jobs[job_id] = {
                "mode": "marketing",
                "status": "running",
                "incident_id": incident_id,
                "segment": segment,
                "target_host": target_host,
                "subscription_tier": subscription_tier,
                "keep_old_keys": keep_old_keys,
                "total": 0,
                "issued": 0,
                "deleted": 0,
                "skipped": 0,
                "errors": 0,
                "delete_errors": 0,
                "sent": 0,
                "send_failed": 0,
                "details": [],
                "phase": "starting",
                "_created": time.time(),
            }

        def _render_marketing_notification(template: str, payload: dict) -> str:
            return (
                template.replace("{host}", str(payload.get("host") or "—"))
                .replace("{expiry}", str(payload.get("expiry") or "—"))
                .replace("{key}", str(payload.get("key") or "—"))
                .replace("{email}", str(payload.get("email") or "—"))
                .replace("{segment}", str(payload.get("segment") or "—"))
            )

        def _run_marketing_issue():
            from shop_bot.modules.xui_api import create_or_update_key_on_host

            job = _migration_jobs[job_id]
            notify_template = notification_text or (
                "🎁 <b>Подарочный ключ готов</b>\n\n"
                "Хост: <b>{host}</b>\n"
                "Сегмент: <b>{segment}</b>\n"
                "Срок до: <b>{expiry}</b>\n"
                "Email: <code>{email}</code>\n\n"
                "<pre>{key}</pre>"
            )

            async def _delete_source_key_from_panel(host_name: str, key_email: str) -> bool:
                return bool(
                    await asyncio.wait_for(
                        xui_api.delete_client_on_host(host_name, key_email),
                        timeout=MANUAL_ISSUE_PANEL_JOB_TIMEOUT_SECONDS,
                    )
                )

            async def _issue_target_key_on_panel(new_email: str, panel_days: int, target_expiry_ms: int) -> dict | None:
                return await asyncio.wait_for(
                    create_or_update_key_on_host(
                        target_host,
                        new_email,
                        panel_days,
                        target_expiry_ms=target_expiry_ms,
                        white_traffic_limit_bytes=_issue_whitelist_traffic_limit_bytes(subscription_tier),
                        include_white_inbounds=_issue_include_white_inbounds(subscription_tier),
                        require_white_inbound=_issue_include_white_inbounds(subscription_tier),
                        panel_call_timeout_seconds=MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
                        max_attempts=2,
                    ),
                    timeout=MANUAL_ISSUE_PANEL_JOB_TIMEOUT_SECONDS,
                )

            try:
                create_incident(incident_id, f"Marketing issuance ({segment}) to {target_host}")
                candidates = _collect_marketing_candidates(segment)
                job["total"] = len(candidates)
                job["phase"] = "issuing"

                now = datetime.now()
                loop = asyncio.new_event_loop()
                source_delete_failed_hosts: set[str] = set()

                for candidate in candidates:
                    source_key_id = int(candidate.get("source_key_id") or 0)
                    user_id = int(candidate.get("user_id") or 0)
                    source_host = str(candidate.get("source_host") or "")
                    source_key_email = str(candidate.get("source_key_email") or "")
                    source_segment = str(candidate.get("source_segment") or "")

                    source_expiry_dt = _parse_datetime(candidate.get("expiry_date")) or now
                    remaining_seconds = max((source_expiry_dt - now).total_seconds(), 0)
                    remaining_days = remaining_seconds / 86400.0
                    new_expiry = now + timedelta(seconds=remaining_seconds) + timedelta(days=gift_days)
                    target_expiry_ms = int(new_expiry.timestamp() * 1000)
                    panel_days = max(1, ceil((new_expiry - now).total_seconds() / 86400))

                    existing = get_migration_for_key(source_key_id, incident_id)
                    replacement = get_replacement_key_for_incident(source_key_id, incident_id)
                    if (existing and existing.get("status") == "migrated") or replacement:
                        job["skipped"] += 1
                        job["details"].append(
                            {
                                "user_id": user_id,
                                "source_key_id": source_key_id,
                                "source_key_email": source_key_email,
                                "source_host": source_host,
                                "source_segment": source_segment,
                                "remaining_days": round(remaining_days, 1),
                                "gift_days": gift_days,
                                "new_total_days": round(remaining_days + gift_days, 1),
                                "new_expiry": (replacement or {}).get("expiry_date") or new_expiry.isoformat(),
                                "old_key_action": "keep" if keep_old_keys else "delete_after_issue",
                                "status": "skipped",
                            }
                        )
                        continue

                    try:
                        key_number = get_next_key_number(user_id)
                        new_email = f"user{user_id}-key{key_number}@vpn.bot"
                        while get_key_by_email(new_email):
                            key_number += 1
                            new_email = f"user{user_id}-key{key_number}@vpn.bot"

                        try:
                            panel_result = loop.run_until_complete(
                                _issue_target_key_on_panel(new_email, panel_days, target_expiry_ms)
                            )
                        except TimeoutError as exc:
                            raise RuntimeError("target_host_timeout") from exc
                        if not panel_result:
                            raise RuntimeError("Panel creation failed") from None  # noqa: TRY301

                        issued_key_value = _issued_key_value_from_panel_result(
                            panel_result,
                            subscription_tier=subscription_tier,
                        )
                        new_key_id = add_migrated_key(
                            user_id=user_id,
                            host_name=target_host,
                            xui_client_uuid=panel_result["client_uuid"],
                            key_email=panel_result["email"],
                            expiry_timestamp_ms=panel_result["expiry_timestamp_ms"],
                            migrated_from_key_id=source_key_id,
                            gift_days=gift_days,
                            incident_id=incident_id,
                            issued_key_value=issued_key_value,
                            panel_sub_id=panel_result.get("sub_id"),
                            subscription_payload=xui_api.subscription_payload_from_result(
                                panel_result,
                                subscription_tier=subscription_tier,
                            ),
                            subscription_tier=subscription_tier,
                            bot_source=selected_bot_source,
                        )
                        if not new_key_id:
                            raise RuntimeError("DB insert failed") from None  # noqa: TRY301

                        log_migration(
                            incident_id=incident_id,
                            user_id=user_id,
                            old_key_id=source_key_id,
                            new_key_id=new_key_id,
                            old_host=source_host,
                            new_host=target_host,
                            gift_days=gift_days,
                            remaining_days=remaining_days,
                            status="migrated",
                        )

                        old_key_action = "keep" if keep_old_keys else "delete_after_issue"
                        detail_status = "issued_keep_old" if keep_old_keys else "issued"
                        old_key_deleted = False
                        delete_error = None

                        if not keep_old_keys:
                            if not source_host or not source_key_email:
                                delete_error = "missing source host or key email"
                            elif source_host in source_delete_failed_hosts:
                                delete_error = f"delete skipped: source host {source_host} already unavailable"
                            else:
                                try:
                                    remote_deleted = loop.run_until_complete(
                                        _delete_source_key_from_panel(source_host, source_key_email)
                                    )
                                except TimeoutError:
                                    remote_deleted = False
                                    delete_error = f"source key delete timeout on {source_host}"

                                if remote_deleted:
                                    delete_key_by_email(source_key_email)
                                    if get_key_by_email(source_key_email):
                                        delete_error = "key deleted from panel but still exists in bot DB"
                                    else:
                                        old_key_deleted = True
                                        detail_status = "issued_deleted"
                                        job["deleted"] += 1
                                else:
                                    source_delete_failed_hosts.add(source_host)
                                    if not delete_error:
                                        delete_error = f"source host {source_host} unavailable or delete refused"

                            if delete_error:
                                detail_status = "issued_delete_failed"
                                job["delete_errors"] += 1
                                logger.warning(
                                    "Marketing source key delete failed: incident=%s user=%s host=%s email=%s error=%s",
                                    incident_id,
                                    user_id,
                                    source_host,
                                    source_key_email,
                                    delete_error,
                                )

                        job["issued"] += 1
                        job["details"].append(
                            {
                                "user_id": user_id,
                                "source_key_id": source_key_id,
                                "source_key_email": source_key_email,
                                "source_host": source_host,
                                "source_segment": source_segment,
                                "remaining_days": round(remaining_days, 1),
                                "gift_days": gift_days,
                                "new_total_days": round(remaining_days + gift_days, 1),
                                "new_expiry": new_expiry.isoformat(),
                                "new_key_id": new_key_id,
                                "new_key_email": panel_result.get("email"),
                                "issued_key_value": issued_key_value,
                                "old_key_action": old_key_action,
                                "old_key_deleted": old_key_deleted,
                                "delete_error": delete_error,
                                "status": detail_status,
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        job["errors"] += 1
                        log_migration(
                            incident_id=incident_id,
                            user_id=user_id,
                            old_key_id=source_key_id,
                            new_key_id=None,
                            old_host=source_host,
                            new_host=target_host,
                            gift_days=gift_days,
                            remaining_days=remaining_days,
                            status="error",
                            error_msg=str(exc)[:250],
                        )
                        job["details"].append(
                            {
                                "user_id": user_id,
                                "source_key_id": source_key_id,
                                "source_key_email": source_key_email,
                                "source_host": source_host,
                                "source_segment": source_segment,
                                "remaining_days": round(remaining_days, 1),
                                "gift_days": gift_days,
                                "new_total_days": round(remaining_days + gift_days, 1),
                                "new_expiry": new_expiry.isoformat(),
                                "old_key_action": "keep" if keep_old_keys else "delete_after_issue",
                                "status": "error",
                                "error": str(exc)[:250],
                            }
                        )

                if send_notify and job["issued"] > 0:
                    job["phase"] = "notifying"
                    bot_token = _resolve_runtime_bot_token(selected_bot_source)
                    if bot_token:
                        from aiogram import Bot
                        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                        bot = Bot(token=bot_token)
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="Мои ключи", callback_data="manage_keys")],
                                [InlineKeyboardButton(text="В меню", callback_data="back_to_main_menu")],
                            ]
                        )
                        for detail in job["details"]:
                            if detail.get("status") not in {
                                "issued",
                                "issued_deleted",
                                "issued_keep_old",
                                "issued_delete_failed",
                            }:
                                continue
                            try:
                                payload = {
                                    "host": target_host,
                                    "segment": "Супер" if detail.get("source_segment") == "super" else "Стандарт",
                                    "expiry": _ru_date(detail.get("new_expiry")),
                                    "email": detail.get("new_key_email") or "—",
                                    "key": detail.get("issued_key_value") or "—",
                                }
                                rendered = _render_marketing_notification(notify_template, payload)
                                loop.run_until_complete(
                                    bot.send_message(
                                        chat_id=int(detail.get("user_id") or 0),
                                        text=rendered,
                                        parse_mode="HTML",
                                        reply_markup=keyboard,
                                    )
                                )
                                job["sent"] += 1
                            except Exception as exc:
                                job["send_failed"] += 1
                                logger.warning(
                                    "Marketing notify failed for user %s incident=%s: %s",
                                    detail.get("user_id"),
                                    incident_id,
                                    exc,
                                )
                        loop.run_until_complete(bot.session.close())
                    else:
                        job["send_failed"] = max(job["send_failed"], int(job.get("issued") or 0))

                loop.close()
                job["phase"] = "done"
                job["status"] = "completed"
            except Exception as exc:
                job["status"] = "failed"
                job["phase"] = "error"
                job["error"] = str(exc)[:500]
                logger.error("Marketing migration job %s failed: %s", job_id, exc, exc_info=True)

        thread = threading.Thread(target=_run_marketing_issue, daemon=True)
        thread.start()
        return jsonify({"job_id": job_id})

    @flask_app.route("/api/migration/marketing/status/<job_id>")
    @login_required
    def migration_marketing_status_api(job_id):
        with _migration_jobs_lock:
            job = _migration_jobs.get(job_id)
            if not job or job.get("mode") != "marketing":
                return jsonify({"error": "Job not found"}), 404
            return jsonify({k: v for k, v in job.items() if not k.startswith("_")})

    @flask_app.route("/api/migration/reset-trials", methods=["POST"])
    @login_required
    def migration_reset_trials_api():
        data = request.get_json(silent=True) or {}
        if data.get("confirm") != "reset-user-trials":
            return jsonify({"error": "confirmation_required"}), 400

        try:
            result = trial_reset.reset_user_trials_by_username(data.get("username", ""))
        except trial_reset.TrialResetError as exc:
            status_code = 404 if exc.code == "user_not_found" else 409 if exc.code == "ambiguous_username" else 400
            payload = {"error": exc.code}
            if exc.details:
                payload["details"] = exc.details
            return jsonify(payload), status_code

        logger.info(
            "Admin reset trial flags for user %s (@%s): trial=%s->0 super=%s->0 changed=%s",
            result["telegram_id"],
            result["username"],
            result["trial_used_before"],
            result["trial_super_used_before"],
            result["changed"],
        )
        return jsonify({"ok": True, **result})

    @flask_app.route("/api/migration/mainpanel-inbounds/preview", methods=["POST"])
    @login_required
    def migration_mainpanel_inbound_sync_preview_api():
        data = request.get_json(silent=True) or {}
        try:
            host_name = _mainpanel_inbound_sync_host_name(data)
            batch_limit = _parse_mainpanel_inbound_sync_batch_limit(data.get("batch_limit"), default=100)
            report = xui_api.sync_existing_mainpanel_clients_to_inbounds(
                host_name,
                dry_run=True,
                batch_limit=batch_limit,
                mode="dry_run",
                persist_snapshot=False,
            )
            return jsonify(_public_mainpanel_inbound_sync_report(report))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            logger.exception("MainPanel inbound sync preview failed")
            return jsonify({"error": str(exc)}), 500

    @flask_app.route("/api/migration/mainpanel-inbounds/execute", methods=["POST"])
    @login_required
    def migration_mainpanel_inbound_sync_execute_api():
        data = request.get_json(silent=True) or {}
        try:
            host_name = _mainpanel_inbound_sync_host_name(data)
            batch_limit = _parse_mainpanel_inbound_sync_batch_limit(data.get("batch_limit"), default=100)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        run_id = secrets.token_hex(16)

        def _run_mainpanel_inbound_sync():
            try:
                xui_api.sync_existing_mainpanel_clients_to_inbounds(
                    host_name,
                    dry_run=False,
                    batch_limit=batch_limit,
                    mode="manual",
                    persist_snapshot=True,
                    run_id=run_id,
                )
            except Exception:
                logger.exception("MainPanel inbound sync job %s failed", run_id)

        thread = threading.Thread(target=_run_mainpanel_inbound_sync, daemon=True)
        thread.start()
        return jsonify({"job_id": run_id, "run_id": run_id})

    @flask_app.route("/api/migration/mainpanel-inbounds/status/<run_id>")
    @login_required
    def migration_mainpanel_inbound_sync_status_api(run_id):
        report = get_mainpanel_inbound_sync_run(run_id, include_items=True)
        if not report:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(_public_mainpanel_inbound_sync_report(report))

    @flask_app.route("/api/migration/mainpanel/preview", methods=["POST"])
    @login_required
    def migration_mainpanel_preview_api():
        data = request.get_json(silent=True) or {}
        try:
            subscription_tier = _issue_parse_subscription_tier(data.get("subscription_tier"))
            bonus_days = mainpanel_migration.parse_bonus_days(data.get("bonus_days", data.get("gift_days", 0)))
            incident_id = str(data.get("incident_id") or "").strip() or None
            plan = _build_mainpanel_migration_plan(
                subscription_tier=subscription_tier,
                bonus_days=bonus_days,
                incident_id=incident_id,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        return jsonify(
            {
                **{key: value for key, value in plan.items() if key != "details"},
                "details": [_mainpanel_public_detail(item) for item in plan["details"]],
            }
        )

    @flask_app.route("/api/migration/mainpanel/execute", methods=["POST"])
    @login_required
    def migration_mainpanel_execute_api():
        import uuid as _uuid

        data = request.get_json(silent=True) or {}
        try:
            subscription_tier = _issue_parse_subscription_tier(data.get("subscription_tier"))
            bonus_days = mainpanel_migration.parse_bonus_days(data.get("bonus_days", data.get("gift_days", 0)))
            main_panel_host_name = _mainpanel_migration_host_name()
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        incident_id = str(data.get("incident_id") or "").strip()
        if not incident_id:
            return jsonify({"error": "Укажите ID миграции"}), 400

        with _migration_jobs_lock:
            if any(
                job.get("status") == "running"
                and job.get("mode") == "mainpanel"
                and job.get("incident_id") == incident_id
                for job in _migration_jobs.values()
            ):
                return jsonify({"error": f'Миграция для incident_id "{incident_id}" уже выполняется'}), 409

            if len(_migration_jobs) >= _MIGRATION_JOBS_MAX:
                completed = [(k, v) for k, v in _migration_jobs.items() if v.get("status") != "running"]
                completed.sort(key=lambda x: x[1].get("_created", 0))
                for k, _ in completed[: len(_migration_jobs) - _MIGRATION_JOBS_MAX + 1]:
                    del _migration_jobs[k]

            job_id = _uuid.uuid4().hex[:12]
            _migration_jobs[job_id] = {
                "mode": "mainpanel",
                "status": "running",
                "phase": "starting",
                "incident_id": incident_id,
                "subscription_tier": subscription_tier,
                "bonus_days": bonus_days,
                "new_host": main_panel_host_name,
                "expired_policy": "skip",
                "found": 0,
                "total": 0,
                "will_migrate": 0,
                "migrated": 0,
                "already_migrated": 0,
                "skipped": 0,
                "skipped_expired": 0,
                "skipped_wrong_tier": 0,
                "needs_review": 0,
                "failed": 0,
                "errors": 0,
                "details": [],
                "_created": time.time(),
            }

        def _append_job_detail(job: dict, detail: dict) -> None:
            public_detail = _mainpanel_public_detail(detail)
            job["details"].append(public_detail)

        def _run_mainpanel_migration():
            job = _migration_jobs[job_id]
            loop = asyncio.new_event_loop()
            try:
                create_incident(incident_id, f"MainPanel tariff migration: {subscription_tier}")
                plan = _build_mainpanel_migration_plan(
                    subscription_tier=subscription_tier,
                    bonus_days=bonus_days,
                    incident_id=incident_id,
                )
                for key in (
                    "found",
                    "total",
                    "will_migrate",
                    "already_migrated",
                    "skipped",
                    "skipped_expired",
                    "skipped_wrong_tier",
                    "needs_review",
                ):
                    job[key] = int(plan.get(key) or 0)
                job["phase"] = "migrating"

                for item in plan["details"]:
                    source_status = str(item.get("status") or "")
                    if source_status != mainpanel_migration.STATUS_WILL_MIGRATE:
                        _append_job_detail(job, item)
                        continue

                    old_key_id = int(item.get("key_id") or 0)
                    user_id = int(item.get("user_id") or 0)
                    old_host = str(item.get("old_host") or "")
                    remaining_days = float(item.get("remaining_days") or 0)
                    target_expiry_ms = int(item.get("target_expiry_ms") or 0)
                    if not old_key_id or not user_id or not target_expiry_ms:
                        item.update({"status": mainpanel_migration.STATUS_NEEDS_REVIEW, "reason": "invalid_plan"})
                        job["needs_review"] += 1
                        job["skipped"] += 1
                        _append_job_detail(job, item)
                        continue

                    claimed = claim_mainpanel_migration_item(
                        incident_id=incident_id,
                        user_id=user_id,
                        old_key_id=old_key_id,
                        old_host=old_host,
                        new_host=main_panel_host_name,
                        gift_days=bonus_days,
                        remaining_days=remaining_days,
                    )
                    if not claimed:
                        item.update({"status": "skipped_claimed", "reason": "already_claimed"})
                        job["skipped"] += 1
                        _append_job_detail(job, item)
                        continue

                    try:
                        target_expiry_dt = mainpanel_migration.parse_key_expiry(item.get("new_expiry"))
                        if target_expiry_dt is None:
                            finish_mainpanel_migration_item(
                                incident_id=incident_id,
                                old_key_id=old_key_id,
                                status="error",
                                error_msg="invalid_target_expiry",
                            )
                            item.update({"status": "error", "reason": "invalid_target_expiry"})
                            job["failed"] += 1
                            job["errors"] += 1
                            _append_job_detail(job, item)
                            continue
                        panel_days = max(
                            1,
                            ceil((target_expiry_dt - datetime.now(UTC).replace(tzinfo=None)).total_seconds() / 86400),
                        )
                        key_number = get_next_key_number(user_id)
                        new_email = f"user{user_id}-key{key_number}@vpn.bot"
                        while get_key_by_email(new_email):
                            key_number += 1
                            new_email = f"user{user_id}-key{key_number}@vpn.bot"

                        target_sub_id = str(item.get("_target_sub_id") or "").strip() or None
                        panel_result = loop.run_until_complete(
                            xui_api.create_or_update_key_on_host(
                                main_panel_host_name,
                                new_email,
                                panel_days,
                                target_expiry_ms=target_expiry_ms,
                                target_sub_id=target_sub_id,
                                white_traffic_limit_bytes=_issue_whitelist_traffic_limit_bytes(subscription_tier),
                                include_white_inbounds=_issue_include_white_inbounds(subscription_tier),
                                require_white_inbound=_issue_include_white_inbounds(subscription_tier),
                                panel_call_timeout_seconds=MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
                                max_attempts=2,
                            )
                        )
                        if not panel_result:
                            finish_mainpanel_migration_item(
                                incident_id=incident_id,
                                old_key_id=old_key_id,
                                status="error",
                                error_msg="panel_creation_failed",
                            )
                            item.update({"status": "error", "reason": "panel_creation_failed"})
                            job["failed"] += 1
                            job["errors"] += 1
                            _append_job_detail(job, item)
                            continue

                        if target_sub_id and str(panel_result.get("sub_id") or "").strip() != target_sub_id:
                            finish_mainpanel_migration_item(
                                incident_id=incident_id,
                                old_key_id=old_key_id,
                                status="error",
                                error_msg="sub_id_reuse_failed",
                            )
                            item.update({"status": "error", "reason": "sub_id_reuse_failed"})
                            job["failed"] += 1
                            job["errors"] += 1
                            _append_job_detail(job, item)
                            continue

                        issued_key_value = _issued_key_value_from_panel_result(
                            panel_result,
                            subscription_tier=subscription_tier,
                        )
                        new_key_id = add_migrated_key(
                            user_id=user_id,
                            host_name=main_panel_host_name,
                            xui_client_uuid=panel_result["client_uuid"],
                            key_email=panel_result["email"],
                            expiry_timestamp_ms=panel_result["expiry_timestamp_ms"],
                            migrated_from_key_id=old_key_id,
                            gift_days=bonus_days,
                            incident_id=incident_id,
                            issued_key_value=issued_key_value,
                            bot_source=str(item.get("_bot_source") or BOT_SOURCE or "main"),
                            panel_sub_id=panel_result.get("sub_id"),
                            subscription_payload=xui_api.subscription_payload_from_result(
                                panel_result,
                                subscription_tier=subscription_tier,
                            ),
                            subscription_tier=subscription_tier,
                        )
                        if not new_key_id:
                            finish_mainpanel_migration_item(
                                incident_id=incident_id,
                                old_key_id=old_key_id,
                                status="error",
                                error_msg="db_insert_failed",
                            )
                            item.update({"status": "error", "reason": "db_insert_failed"})
                            job["failed"] += 1
                            job["errors"] += 1
                            _append_job_detail(job, item)
                            continue

                        archive_key(
                            old_key_id, replaced_due_to_incident=True, migration_reason=f"mainpanel:{incident_id}"
                        )
                        finish_mainpanel_migration_item(
                            incident_id=incident_id,
                            old_key_id=old_key_id,
                            status="migrated",
                            new_key_id=int(new_key_id),
                        )
                        item.update({"status": "migrated", "reason": "migrated", "new_key_id": int(new_key_id)})
                        job["migrated"] += 1
                        _append_job_detail(job, item)
                    except Exception:
                        finish_mainpanel_migration_item(
                            incident_id=incident_id,
                            old_key_id=old_key_id,
                            status="error",
                            error_msg="migration_error",
                        )
                        item.update({"status": "error", "reason": "migration_error"})
                        job["failed"] += 1
                        job["errors"] += 1
                        _append_job_detail(job, item)
                        logger.exception("MainPanel migration error for key %s", old_key_id)

                job["phase"] = "done"
                job["status"] = "completed"
            except Exception as exc:
                job["status"] = "failed"
                job["phase"] = "error"
                job["error"] = str(exc)[:500]
                logger.exception("MainPanel migration job %s failed", job_id)
            finally:
                loop.close()

        thread = threading.Thread(target=_run_mainpanel_migration, daemon=True)
        thread.start()
        return jsonify({"job_id": job_id})

    @flask_app.route("/api/migration/mainpanel/status/<job_id>")
    @login_required
    def migration_mainpanel_status_api(job_id):
        with _migration_jobs_lock:
            job = _migration_jobs.get(job_id)
            if not job or job.get("mode") != "mainpanel":
                return jsonify({"error": "Job not found"}), 404
            return jsonify({k: v for k, v in job.items() if not k.startswith("_")})

    @flask_app.route("/api/migration/preview", methods=["POST"])
    @login_required
    def migration_preview_api():
        data = request.get_json(silent=True) or {}
        old_host = (data.get("old_host") or "").strip()
        new_host = (data.get("new_host") or "").strip()
        try:
            subscription_tier = _issue_parse_subscription_tier(data.get("subscription_tier"))
            gift_days = _parse_migration_gift_days(data.get("gift_days", 7), default=7)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if not old_host or not new_host:
            return jsonify({"error": "Выберите оба хоста"}), 400
        if old_host == new_host:
            return jsonify({"error": "Хосты должны быть разными"}), 400
        main_panel_error = _issue_validate_main_panel_host(new_host)
        if main_panel_error:
            return jsonify({"error": main_panel_error}), 400
        if not get_host(old_host):
            return jsonify({"error": f'Старый хост "{old_host}" не найден'}), 400
        if not get_host(new_host):
            return jsonify({"error": f'Новый хост "{new_host}" не найден'}), 400

        keys = get_all_keys_for_host_migration(old_host, include_trial_super=False)
        now = datetime.now()
        details = []
        for key in keys:
            expiry_str = key.get("expiry_date", "")
            try:
                expiry_date = datetime.fromisoformat(expiry_str) if expiry_str else now
            except ValueError:
                expiry_date = now
            remaining_seconds = max((expiry_date - now).total_seconds(), 0)
            remaining_days = round(remaining_seconds / 86400, 1)
            new_total_days = round(remaining_days + gift_days, 1)
            new_expiry = (now + timedelta(seconds=remaining_seconds) + timedelta(days=gift_days)).isoformat()

            already_migrated = False
            incident_id = data.get("incident_id", "").strip()
            if incident_id:
                existing = get_migration_for_key(key["key_id"], incident_id)
                replacement = get_replacement_key_for_incident(key["key_id"], incident_id)
                if (existing and existing.get("status") == "migrated") or replacement:
                    already_migrated = True

            details.append(
                {
                    "user_id": key["user_id"],
                    "key_id": key["key_id"],
                    "key_email": key["key_email"],
                    "remaining_days": remaining_days,
                    "gift_days": gift_days,
                    "new_total_days": new_total_days,
                    "new_expiry": new_expiry,
                    "already_migrated": already_migrated,
                }
            )

        return jsonify(
            {
                "total": len(details),
                "skipped": sum(1 for d in details if d["already_migrated"]),
                "subscription_tier": subscription_tier,
                "details": details,
            }
        )

    @flask_app.route("/api/migration/execute", methods=["POST"])
    @login_required
    def migration_execute_api():
        import uuid as _uuid

        def _to_bool(raw_value) -> bool:
            if isinstance(raw_value, bool):
                return raw_value
            if isinstance(raw_value, (int, float)):
                return bool(raw_value)
            if isinstance(raw_value, str):
                return raw_value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(raw_value)

        data = request.get_json(silent=True) or {}
        old_host = (data.get("old_host") or "").strip()
        new_host = (data.get("new_host") or "").strip()
        try:
            subscription_tier = _issue_parse_subscription_tier(data.get("subscription_tier"))
            gift_days = _parse_migration_gift_days(data.get("gift_days", 7), default=7)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        incident_id = (data.get("incident_id") or "").strip()
        notification_text = (data.get("notification_text") or "").strip()
        send_notify = _to_bool(data.get("send_notify", True))

        if not old_host or not new_host or old_host == new_host:
            return jsonify({"error": "Неверные параметры хостов"}), 400
        if not incident_id:
            return jsonify({"error": "Укажите ID инцидента"}), 400
        main_panel_error = _issue_validate_main_panel_host(new_host)
        if main_panel_error:
            return jsonify({"error": main_panel_error}), 400
        if not get_host(old_host):
            return jsonify({"error": f'Старый хост "{old_host}" не найден'}), 400
        if not get_host(new_host):
            return jsonify({"error": f'Новый хост "{new_host}" не найден'}), 400
        with _migration_jobs_lock:
            if any(
                job.get("status") == "running" and job.get("incident_id") == incident_id
                for job in _migration_jobs.values()
            ):
                return jsonify({"error": f'Миграция для incident_id "{incident_id}" уже выполняется'}), 409

            # Evict oldest completed jobs if limit exceeded
            if len(_migration_jobs) >= _MIGRATION_JOBS_MAX:
                completed = [(k, v) for k, v in _migration_jobs.items() if v.get("status") != "running"]
                completed.sort(key=lambda x: x[1].get("_created", 0))
                for k, _ in completed[: len(_migration_jobs) - _MIGRATION_JOBS_MAX + 1]:
                    del _migration_jobs[k]

            job_id = _uuid.uuid4().hex[:12]
            _migration_jobs[job_id] = {
                "status": "running",
                "incident_id": incident_id,
                "subscription_tier": subscription_tier,
                "total": 0,
                "migrated": 0,
                "skipped": 0,
                "errors": 0,
                "sent": 0,
                "send_failed": 0,
                "details": [],
                "phase": "starting",
                "_created": time.time(),
            }

        import threading

        def _run_migration():
            from shop_bot.modules.xui_api import create_or_update_key_on_host

            job = _migration_jobs[job_id]
            try:
                incident_msg = notification_text or "Key migration"
                create_incident(incident_id, incident_msg)

                keys = get_all_keys_for_host_migration(old_host, include_trial_super=False)
                job["total"] = len(keys)
                job["phase"] = "migrating"
                now = datetime.now()
                loop = asyncio.new_event_loop()

                for i, key in enumerate(keys):
                    old_key_id = key["key_id"]
                    user_id = key["user_id"]
                    key_email = key["key_email"]
                    expiry_str = key.get("expiry_date", "")
                    expiry_date = datetime.fromisoformat(expiry_str) if expiry_str else now
                    remaining_seconds = max((expiry_date - now).total_seconds(), 0)
                    remaining_days = remaining_seconds / 86400
                    new_expiry = now + timedelta(seconds=remaining_seconds) + timedelta(days=gift_days)
                    panel_days = ceil((new_expiry - now).total_seconds() / 86400)

                    # Skip trial-super
                    if key_email and key_email.endswith("-trial-super@telegram.bot"):
                        job["skipped"] += 1
                        job["details"].append(
                            {
                                "user_id": user_id,
                                "key_id": old_key_id,
                                "key_email": key_email,
                                "remaining_days": round(remaining_days, 1),
                                "gift_days": gift_days,
                                "new_total_days": round(remaining_days + gift_days, 1),
                                "new_expiry": new_expiry.isoformat(),
                                "status": "skipped_trial",
                            }
                        )
                        continue

                    # Skip already migrated
                    existing = get_migration_for_key(old_key_id, incident_id)
                    if existing and existing.get("status") == "migrated":
                        job["skipped"] += 1
                        job["details"].append(
                            {
                                "user_id": user_id,
                                "key_id": old_key_id,
                                "key_email": key_email,
                                "remaining_days": round(remaining_days, 1),
                                "gift_days": gift_days,
                                "new_total_days": round(remaining_days + gift_days, 1),
                                "new_expiry": new_expiry.isoformat(),
                                "status": "skipped_already",
                            }
                        )
                        continue

                    # Recover partial migration: replacement key already exists for this incident.
                    replacement = get_replacement_key_for_incident(old_key_id, incident_id)
                    if replacement and replacement.get("key_id"):
                        new_key_id = int(replacement["key_id"])
                        archive_key(
                            old_key_id, replaced_due_to_incident=True, migration_reason=f"incident:{incident_id}"
                        )
                        log_migration(
                            incident_id,
                            user_id,
                            old_key_id,
                            new_key_id,
                            old_host,
                            new_host,
                            gift_days,
                            remaining_days,
                            "migrated",
                            "reused_existing_replacement",
                        )
                        job["migrated"] += 1
                        job["details"].append(
                            {
                                "user_id": user_id,
                                "key_id": old_key_id,
                                "key_email": key_email,
                                "remaining_days": round(remaining_days, 1),
                                "gift_days": gift_days,
                                "new_total_days": round(remaining_days + gift_days, 1),
                                "new_expiry": replacement.get("expiry_date") or new_expiry.isoformat(),
                                "new_key_id": new_key_id,
                                "status": "migrated",
                            }
                        )
                        continue

                    try:
                        key_number = get_next_key_number(user_id)
                        new_email = f"user{user_id}-key{key_number}@vpn.bot"
                        while get_key_by_email(new_email):
                            key_number += 1
                            new_email = f"user{user_id}-key{key_number}@vpn.bot"

                        panel_result = loop.run_until_complete(
                            create_or_update_key_on_host(
                                new_host,
                                new_email,
                                panel_days,
                                white_traffic_limit_bytes=_issue_whitelist_traffic_limit_bytes(subscription_tier),
                                include_white_inbounds=_issue_include_white_inbounds(subscription_tier),
                                require_white_inbound=_issue_include_white_inbounds(subscription_tier),
                                panel_call_timeout_seconds=MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
                                max_attempts=2,
                            )
                        )

                        if not panel_result:
                            log_migration(
                                incident_id,
                                user_id,
                                old_key_id,
                                None,
                                old_host,
                                new_host,
                                gift_days,
                                remaining_days,
                                "error",
                                "Panel creation failed",
                            )
                            job["errors"] += 1
                            job["details"].append(
                                {
                                    "user_id": user_id,
                                    "key_id": old_key_id,
                                    "key_email": key_email,
                                    "remaining_days": round(remaining_days, 1),
                                    "gift_days": gift_days,
                                    "new_total_days": round(remaining_days + gift_days, 1),
                                    "new_expiry": new_expiry.isoformat(),
                                    "status": "error",
                                    "error": "Panel creation failed",
                                }
                            )
                            continue

                        real_uuid = panel_result["client_uuid"]
                        issued_key_value = _issued_key_value_from_panel_result(
                            panel_result,
                            subscription_tier=subscription_tier,
                        )

                        new_key_id = add_migrated_key(
                            user_id=user_id,
                            host_name=new_host,
                            xui_client_uuid=real_uuid,
                            key_email=panel_result["email"],
                            expiry_timestamp_ms=panel_result["expiry_timestamp_ms"],
                            migrated_from_key_id=old_key_id,
                            gift_days=gift_days,
                            incident_id=incident_id,
                            issued_key_value=issued_key_value,
                            panel_sub_id=panel_result.get("sub_id"),
                            subscription_payload=xui_api.subscription_payload_from_result(
                                panel_result,
                                subscription_tier=subscription_tier,
                            ),
                            subscription_tier=subscription_tier,
                        )

                        if new_key_id:
                            archive_key(
                                old_key_id, replaced_due_to_incident=True, migration_reason=f"incident:{incident_id}"
                            )
                            log_migration(
                                incident_id,
                                user_id,
                                old_key_id,
                                new_key_id,
                                old_host,
                                new_host,
                                gift_days,
                                remaining_days,
                                "migrated",
                            )
                            job["migrated"] += 1
                            job["details"].append(
                                {
                                    "user_id": user_id,
                                    "key_id": old_key_id,
                                    "key_email": key_email,
                                    "remaining_days": round(remaining_days, 1),
                                    "gift_days": gift_days,
                                    "new_total_days": round(remaining_days + gift_days, 1),
                                    "new_expiry": new_expiry.isoformat(),
                                    "new_key_id": new_key_id,
                                    "status": "migrated",
                                }
                            )
                        else:
                            log_migration(
                                incident_id,
                                user_id,
                                old_key_id,
                                None,
                                old_host,
                                new_host,
                                gift_days,
                                remaining_days,
                                "error",
                                "DB insert failed",
                            )
                            job["errors"] += 1
                            job["details"].append(
                                {
                                    "user_id": user_id,
                                    "key_id": old_key_id,
                                    "key_email": key_email,
                                    "remaining_days": round(remaining_days, 1),
                                    "gift_days": gift_days,
                                    "new_total_days": round(remaining_days + gift_days, 1),
                                    "new_expiry": new_expiry.isoformat(),
                                    "status": "error",
                                    "error": "DB insert failed",
                                }
                            )

                    except Exception as exc:
                        job["errors"] += 1
                        job["details"].append(
                            {
                                "user_id": user_id,
                                "key_id": old_key_id,
                                "key_email": key_email,
                                "remaining_days": round(remaining_days, 1),
                                "gift_days": gift_days,
                                "new_total_days": round(remaining_days + gift_days, 1),
                                "new_expiry": new_expiry.isoformat(),
                                "status": "error",
                                "error": str(exc)[:200],
                            }
                        )
                        logger.error("Migration error for key %s: %s", old_key_id, exc, exc_info=True)

                # Phase 2: Send notifications
                if send_notify and notification_text and job["migrated"] > 0:
                    job["phase"] = "notifying"
                    migrated_users = {d["user_id"] for d in job["details"] if d["status"] == "migrated"}
                    bot_token = _resolve_runtime_bot_token()
                    if bot_token:
                        from aiogram import Bot
                        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

                        bot = Bot(token=bot_token)
                        keyboard = InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="Мои ключи", callback_data="manage_keys")],
                                [InlineKeyboardButton(text="В меню", callback_data="back_to_main_menu")],
                            ]
                        )
                        for uid in sorted(migrated_users):
                            try:
                                loop.run_until_complete(
                                    bot.send_message(
                                        chat_id=uid,
                                        text=notification_text,
                                        parse_mode="HTML",
                                        reply_markup=keyboard,
                                    )
                                )
                                job["sent"] += 1
                            except Exception as exc:
                                job["send_failed"] += 1
                                logger.warning("Notify failed for user %s: %s", uid, exc)
                        loop.run_until_complete(bot.session.close())

                loop.close()
                job["phase"] = "done"
                job["status"] = "completed"

            except Exception as exc:
                job["status"] = "failed"
                job["phase"] = "error"
                job["error"] = str(exc)[:500]
                logger.error("Migration job %s failed: %s", job_id, exc, exc_info=True)

        thread = threading.Thread(target=_run_migration, daemon=True)
        thread.start()

        return jsonify({"job_id": job_id})

    @flask_app.route("/api/migration/status/<job_id>")
    @login_required
    def migration_status_api(job_id):
        with _migration_jobs_lock:
            job = _migration_jobs.get(job_id)
            if not job:
                return jsonify({"error": "Job not found"}), 404
            # Return a snapshot to avoid concurrent mutation during serialization
            return jsonify({k: v for k, v in job.items() if not k.startswith("_")})

    # ── End Migration UI ─────────────────────────────────────────

    @flask_app.route("/yookassa-webhook", methods=["POST"])
    def yookassa_webhook_handler():
        try:
            trusted_ip, candidate_ips = _resolve_trusted_yookassa_ip()
            forwarded_for = (request.headers.get("X-Forwarded-For") or "").strip()
            if not trusted_ip:
                logger.warning(
                    "YooKassa webhook rejected: untrusted IP remote=%s xff=%s candidates=%s",
                    request.remote_addr,
                    forwarded_for or "none",
                    ",".join(candidate_ips) or "none",
                )
                return "Forbidden", 403
            logger.info(
                "YooKassa webhook accepted: trusted_ip=%s remote=%s xff=%s",
                trusted_ip,
                request.remote_addr,
                forwarded_for or "none",
            )

            event_json = request.json or {}
            event_type = str(event_json.get("event") or "").strip()
            payment_obj = event_json.get("object", {}) or {}
            payment_id = str(payment_obj.get("id") or "").strip()
            webhook_metadata = payment_obj.get("metadata", {}) or {}

            if not payment_id:
                logger.warning("YooKassa webhook ignored: missing payment_id.")
                _log_payment_lost(
                    "YooKassa",
                    None,
                    "missing_payment_id",
                    webhook_metadata if isinstance(webhook_metadata, dict) else None,
                )
                return "OK", 200

            if event_type == "payment.canceled":
                canceled = mark_transaction_canceled(payment_id, payment_method="YooKassa")
                if canceled:
                    logger.info("YooKassa webhook: payment_id=%s marked as canceled.", payment_id)
                else:
                    logger.info(
                        "YooKassa webhook: payment_id=%s cancel event received but no pending/processing row to update.",
                        payment_id,
                    )
                return "OK", 200

            if event_type == "payment.succeeded":
                if payment_id and is_transaction_fully_issued(payment_id):
                    logger.info(
                        "YooKassa webhook: payment_id=%s already processed (issued).",
                        payment_id,
                    )
                    return "OK", 200

                if payment_id and is_transaction_processing(payment_id):
                    logger.info(f"YooKassa webhook: payment_id={payment_id} is already processing.")
                    return "OK", 200

                pending_metadata = get_pending_transaction_metadata(payment_id)
                processing_started = False
                metadata: dict | None = None
                provider_metadata = normalize_payment_metadata(webhook_metadata)

                if pending_metadata:
                    processing_started = start_transaction_processing(payment_id)
                    if not processing_started:
                        logger.info(
                            "YooKassa webhook: payment_id=%s could not switch to processing (already handled).",
                            payment_id,
                        )
                        return "OK", 200

                    metadata = merge_payment_metadata(pending_metadata, provider_metadata)
                    if provider_metadata and provider_metadata != pending_metadata:
                        logger.warning(
                            "YooKassa webhook metadata mismatch for payment_id=%s. Merging provider metadata into pending transaction metadata.",
                            payment_id,
                        )
                else:
                    fallback_metadata = provider_metadata
                    missing_fields = missing_required_payment_metadata_fields(fallback_metadata)
                    if missing_fields:
                        logger.warning(
                            "YooKassa webhook ignored: payment_id=%s has no pending transaction and missing metadata fields=%s.",
                            payment_id,
                            ",".join(missing_fields),
                        )
                        _log_payment_lost(
                            "YooKassa",
                            payment_id,
                            f"missing_metadata_fields:{','.join(missing_fields)}",
                            fallback_metadata,
                        )
                        return "OK", 200

                    metadata = dict(fallback_metadata)
                    metadata["external_payment_id"] = payment_id
                    metadata["payment_method"] = metadata.get("payment_method") or "YooKassa"
                    metadata["payment_confirmed"] = True

                    amount_value = (payment_obj.get("amount") or {}).get("value")
                    raw_amount = metadata.get("price", amount_value)
                    try:
                        amount_rub = float(raw_amount)
                    except (TypeError, ValueError):
                        amount_rub = 0.0

                    try:
                        pending_id = create_pending_transaction(
                            payment_id=payment_id,
                            user_id=int(metadata.get("user_id")),
                            amount_rub=amount_rub,
                            metadata=metadata,
                        )
                    except (TypeError, ValueError):
                        pending_id = 0

                    if pending_id:
                        processing_started = start_transaction_processing(payment_id)
                        if processing_started:
                            update_transaction_metadata(payment_id, metadata, statuses=("processing",))
                    logger.warning(
                        "YooKassa webhook: payment_id=%s has no pending transaction. Fallback to provider metadata (trusted_ip=%s, pending_created=%s).",
                        payment_id,
                        trusted_ip,
                        bool(pending_id),
                    )

                if not metadata:
                    logger.error("YooKassa webhook: metadata is empty after resolution. payment_id=%s", payment_id)
                    return "OK", 200
                if not metadata.get("external_payment_id"):
                    metadata["external_payment_id"] = payment_id
                metadata["payment_confirmed"] = True
                if not metadata.get("paid_at"):
                    paid_at = _extract_provider_paid_at(
                        payment_obj.get("captured_at"),
                        payment_obj.get("paid_at"),
                        payment_obj.get("created_at"),
                    )
                    if paid_at:
                        metadata["paid_at"] = paid_at
                if processing_started:
                    update_transaction_metadata(payment_id, metadata, statuses=("processing",))

                return _dispatch_confirmed_payment(
                    "YooKassa",
                    payment_id,
                    metadata,
                    processing_started=processing_started,
                )
            else:
                logger.debug("YooKassa webhook ignored event=%s payment_id=%s", event_type, payment_id)
            return "OK", 200  # noqa: TRY300
        except Exception:
            logger.exception("Error in yookassa webhook handler")
            return "Error", 500

    @flask_app.route("/cryptobot-webhook", methods=["POST"])
    def cryptobot_webhook_handler():
        try:
            raw_body = request.get_data(cache=True) or b""
            crypto_signature = request.headers.get("crypto-pay-api-signature", "")
            if not _verify_cryptobot_signature(raw_body, crypto_signature):
                logger.warning("CryptoBot webhook rejected: invalid or missing signature.")
                return "Forbidden", 403

            request_data = request.json

            if request_data and request_data.get("update_type") == "invoice_paid":
                payload_data = request_data.get("payload", {})

                payload_string = payload_data.get("payload")

                if not payload_string:
                    logger.warning("CryptoBot Webhook: Received paid invoice but payload was empty.")
                    _log_payment_lost(
                        "CryptoBot", payload_data.get("invoice_id") or payload_data.get("id"), "empty_payload"
                    )
                    return "OK", 200

                parts = payload_string.split(":")
                if len(parts) < 9:
                    logger.error(f"cryptobot Webhook: Invalid payload format received: {payload_string}")
                    return "Error", 400

                is_super_key_flow = False
                if len(parts) >= 10:
                    is_super_key_flow = parts[9].strip().lower() in {"1", "true", "yes", "on"}
                plan_name = None
                if len(parts) >= 11:
                    plan_name = _decode_cryptobot_payload_field(parts[10])
                bot_source = "main"
                if len(parts) >= 12:
                    parsed_bot_source = str(parts[11] or "").strip().lower()
                    if parsed_bot_source in {"main", "reserve"}:
                        bot_source = parsed_bot_source

                metadata = {
                    "user_id": parts[0],
                    "months": parts[1],
                    "price": parts[2],
                    "action": parts[3],
                    "key_id": parts[4],
                    "host_name": parts[5],
                    "plan_id": parts[6],
                    "plan_name": plan_name,
                    "customer_email": parts[7] if parts[7] != "None" else None,
                    "payment_method": parts[8],
                    "bot_source": bot_source,
                    "is_super_key_flow": is_super_key_flow,
                    "referral_discount_claim_id": parts[12] if len(parts) >= 13 else None,
                }
                external_payment_id = str(
                    payload_data.get("invoice_id") or payload_data.get("invoiceId") or payload_data.get("id") or ""
                ).strip()
                if external_payment_id:
                    metadata["external_payment_id"] = external_payment_id
                    metadata["payment_confirmed"] = True
                paid_at = _extract_provider_paid_at(
                    payload_data.get("paid_at"),
                    payload_data.get("paidAt"),
                    payload_data.get("date"),
                    request_data.get("date"),
                )
                if paid_at:
                    metadata["paid_at"] = paid_at

                processing_started = False
                if external_payment_id:
                    if is_transaction_fully_issued(external_payment_id):
                        return "OK", 200
                    if is_transaction_processing(external_payment_id):
                        return "OK", 200

                    pending_metadata = get_pending_transaction_metadata(external_payment_id)
                    if not pending_metadata:
                        try:
                            create_pending_transaction(
                                payment_id=external_payment_id,
                                user_id=int(metadata["user_id"]),
                                amount_rub=float(metadata["price"]),
                                metadata=metadata,
                            )
                        except Exception:
                            logger.exception(
                                "CryptoBot webhook: failed to persist pending transaction for payment_id=%s",
                                external_payment_id,
                            )
                        pending_metadata = get_pending_transaction_metadata(external_payment_id)

                    if pending_metadata:
                        metadata = merge_payment_metadata(pending_metadata, metadata)
                        metadata["external_payment_id"] = external_payment_id
                        metadata["payment_confirmed"] = True
                        update_transaction_metadata(external_payment_id, metadata, statuses=("pending",))
                        processing_started = start_transaction_processing(external_payment_id)
                        if processing_started:
                            update_transaction_metadata(external_payment_id, metadata, statuses=("processing",))
                    else:
                        logger.warning(
                            "CryptoBot webhook ignored: payment_id=%s has no pending metadata.",
                            external_payment_id,
                        )
                        _log_payment_lost("CryptoBot", external_payment_id, "no_pending_metadata", metadata)
                        return "OK", 200

                    if not processing_started:
                        _log_payment_lost("CryptoBot", external_payment_id, "could_not_start_processing", metadata)
                        return "OK", 200

                return _dispatch_confirmed_payment(
                    "CryptoBot",
                    external_payment_id,
                    metadata,
                    processing_started=processing_started,
                )

            return "OK", 200  # noqa: TRY300

        except Exception:
            logger.exception("Error in cryptobot webhook handler")
            return "Error", 500

    @flask_app.route("/heleket-webhook", methods=["POST"])
    def heleket_webhook_handler():
        try:
            data = request.json
            logger.info(
                "Received Heleket webhook: status=%s order_id=%s",
                data.get("status") if isinstance(data, dict) else None,
                data.get("order_id") if isinstance(data, dict) else None,
            )

            api_key = get_setting("heleket_api_key")
            if not api_key:
                return "Error", 500

            sign = data.pop("sign", None)
            if not sign:
                return "Error", 400

            sorted_data_str = json.dumps(data, sort_keys=True, separators=(",", ":"))

            base64_encoded = base64.b64encode(sorted_data_str.encode()).decode()
            raw_string = f"{base64_encoded}{api_key}"
            expected_sign = hashlib.md5(raw_string.encode()).hexdigest()

            if not compare_digest(expected_sign, sign):
                logger.warning("Heleket webhook: Invalid signature.")
                return "Forbidden", 403

            if data.get("status") in ["paid", "paid_over"]:
                metadata_str = data.get("description")
                if not metadata_str:
                    return "Error", 400

                metadata = json.loads(metadata_str)
                if isinstance(metadata, dict) and not metadata.get("paid_at"):
                    paid_at = _extract_provider_paid_at(
                        data.get("paid_at"),
                        data.get("updated_at"),
                        data.get("created_at"),
                    )
                    if paid_at:
                        metadata["paid_at"] = paid_at
                external_payment_id = str(data.get("order_id") or "").strip()
                processing_started = False
                if external_payment_id and isinstance(metadata, dict):
                    metadata["external_payment_id"] = external_payment_id
                    metadata["payment_confirmed"] = True
                    if is_transaction_fully_issued(external_payment_id):
                        return "OK", 200
                    if is_transaction_processing(external_payment_id):
                        return "OK", 200

                    pending_metadata = get_pending_transaction_metadata(external_payment_id)
                    if not pending_metadata:
                        create_pending_transaction(
                            payment_id=external_payment_id,
                            user_id=int(metadata.get("user_id") or 0),
                            amount_rub=float(metadata.get("price") or 0),
                            metadata=metadata,
                        )
                        pending_metadata = get_pending_transaction_metadata(external_payment_id)

                    if pending_metadata:
                        metadata = merge_payment_metadata(pending_metadata, metadata)
                        metadata["external_payment_id"] = external_payment_id
                        metadata["payment_confirmed"] = True
                        update_transaction_metadata(external_payment_id, metadata, statuses=("pending",))
                        processing_started = start_transaction_processing(external_payment_id)
                        if processing_started:
                            update_transaction_metadata(external_payment_id, metadata, statuses=("processing",))
                    else:
                        logger.warning(
                            "Heleket webhook ignored: payment_id=%s has no pending metadata.",
                            external_payment_id,
                        )
                        _log_payment_lost(
                            "Heleket",
                            external_payment_id,
                            "no_pending_metadata",
                            metadata if isinstance(metadata, dict) else None,
                        )
                        return "OK", 200

                    if not processing_started:
                        _log_payment_lost(
                            "Heleket",
                            external_payment_id,
                            "could_not_start_processing",
                            metadata if isinstance(metadata, dict) else None,
                        )
                        return "OK", 200

                return _dispatch_confirmed_payment(
                    "Heleket",
                    external_payment_id,
                    metadata,
                    processing_started=processing_started,
                )

            return "OK", 200  # noqa: TRY300
        except Exception:
            logger.exception("Error in heleket webhook handler")
            return "Error", 500

    @flask_app.route("/ton-webhook", methods=["POST"])
    def ton_webhook_handler():
        try:
            tonapi_key = (get_setting("tonapi_key") or "").strip()
            auth_header = (request.headers.get("Authorization") or "").strip()
            if tonapi_key:
                if not auth_header:
                    logger.warning("TON webhook rejected: missing Authorization header.")
                    return "Forbidden", 403
                expected = f"Bearer {tonapi_key}"
                if not compare_digest(auth_header, expected):
                    logger.warning("TON webhook rejected: invalid Authorization header.")
                    return "Forbidden", 403

            data = request.json
            tx_count = 0
            if isinstance(data, dict):
                tx_count = len(data.get("in_progress_txs", []) or []) + len(data.get("txs", []) or [])
            logger.info(
                "Received TonAPI webhook: account_id=%s tx_count=%s",
                data.get("account_id") if isinstance(data, dict) else None,
                tx_count,
            )

            if "tx_id" in data:
                for tx in data.get("in_progress_txs", []) + data.get("txs", []):
                    in_msg = tx.get("in_msg")
                    if in_msg and in_msg.get("decoded_comment"):
                        payment_id = in_msg["decoded_comment"]
                        amount_nano = int(in_msg.get("value", 0))
                        amount_ton = float(amount_nano / 1_000_000_000)
                        pending_metadata = get_pending_transaction_metadata(payment_id)
                        if pending_metadata and not _metadata_bot_source_matches_runtime(pending_metadata):
                            _warn_wrong_bot_instance("TON", payment_id, pending_metadata)
                            continue

                        metadata = find_and_complete_ton_transaction(payment_id, amount_ton)

                        if metadata:
                            logger.info(f"TON Payment successful for payment_id: {payment_id}")
                            _dispatch_confirmed_payment(
                                "TON",
                                payment_id,
                                metadata,
                                processing_started=True,
                            )
                        else:
                            _log_payment_lost("TON", payment_id, "metadata_not_found_for_pending_transaction")

            return "OK", 200  # noqa: TRY300
        except Exception:
            logger.exception("Error in ton webhook handler")
            return "Error", 500

    def _provider_signature_from_headers() -> str:
        return (
            request.headers.get("Signature")
            or request.headers.get("signature")
            or request.headers.get("X-Platega-Signature")
            or request.headers.get("Platega-Signature")
            or request.headers.get("X-Signature")
            or request.headers.get("X-PayPear-Signature")
            or ""
        )

    def _handle_provider_webhook(
        *,
        provider_label: str,
        payment_method_name: str,
        signature_setting_key: str,
        verify_signature_fn,
        map_status_fn,
        error_type: BotError,
        alert_path: str,
        read_signature_from_body: bool = False,
    ):
        processing_started = False
        payment_id = ""
        try:
            raw_body = request.get_data(cache=True) or b""
            data = request.get_json(silent=True) or request.form.to_dict() or {}
            if not isinstance(data, dict):
                data = {}

            signature_key = str(get_setting(signature_setting_key) or "").strip()
            if not signature_key and signature_setting_key == "paypear_webhook_secret":
                signature_key = str(os.environ.get("PAYPEAR_WEBHOOK_SECRET") or "").strip()
            if not signature_key:
                logger.error("%s webhook rejected: %s is not configured.", provider_label, signature_setting_key)
                try:
                    send_critical_alert(
                        error_type,
                        details=(f"source={BOT_SOURCE} reason=webhook_secret_missing path={alert_path}"),
                        respect_cooldown=False,
                    )
                except Exception:
                    logger.warning("Failed to emit %s webhook secret alert.", provider_label, exc_info=True)
                return "Error", 500

            signature = str(data.get("signature") or "").strip() if read_signature_from_body else ""
            if not signature:
                signature = _provider_signature_from_headers()
            if not verify_signature_fn(raw_body, signature, signature_key):
                logger.warning("%s webhook rejected: invalid signature.", provider_label)
                return "Forbidden", 403

            object_payload = data.get("object") if isinstance(data.get("object"), dict) else {}
            status_raw = str(data.get("status") or object_payload.get("status") or "").strip().lower()
            mapped_status = map_status_fn(status_raw)

            event = str(data.get("event") or "").strip().lower()
            if event == "payment.confirmed":
                mapped_status = "succeeded"
            elif event in {"payment.canceled", "payment.cancelled", "payment.expired"}:
                mapped_status = "canceled"

            reference_id = str(
                object_payload.get("referenceId")
                or object_payload.get("reference_id")
                or object_payload.get("orderId")
                or object_payload.get("order_id")
                or data.get("referenceId")
                or data.get("reference_id")
                or data.get("orderId")
                or data.get("order_id")
                or ""
            ).strip()
            provider_payment_id = str(
                object_payload.get("id") or data.get("transaction_id") or data.get("payment_id") or data.get("id") or ""
            ).strip()
            payment_id = provider_payment_id or reference_id

            def _resolve_paypear_reference() -> tuple[str, dict | None]:
                if provider_label.lower() != "paypear" or not reference_id:
                    return payment_id, None
                resolved = get_pending_paypear_transaction_by_order_id(reference_id)
                if not resolved:
                    return payment_id, None
                resolved_payment_id = str(resolved.get("payment_id") or "").strip()
                resolved_metadata = resolved.get("metadata")
                if not resolved_payment_id or not isinstance(resolved_metadata, dict):
                    return payment_id, None
                if payment_id != resolved_payment_id:
                    logger.info(
                        "%s webhook resolved reference_id=%s to payment_id=%s (provider_payment_id=%s).",
                        provider_label,
                        reference_id,
                        resolved_payment_id,
                        provider_payment_id or "-",
                    )
                return resolved_payment_id, dict(resolved_metadata)

            if not provider_payment_id and reference_id:
                payment_id, _ = _resolve_paypear_reference()

            logger.info(
                "Received %s webhook: payment_id=%s reference_id=%s status=%s mapped=%s",
                provider_label,
                payment_id or None,
                reference_id or None,
                status_raw or None,
                mapped_status,
            )

            if not payment_id:
                _log_payment_lost(provider_label, None, "missing_payment_id", data if isinstance(data, dict) else None)
                return "OK", 200

            if mapped_status == "canceled":
                mark_transaction_canceled(payment_id, payment_method=payment_method_name)
                return "OK", 200

            if mapped_status != "succeeded":
                return "OK", 200

            if is_transaction_fully_issued(payment_id):
                return "OK", 200
            if is_transaction_processing(payment_id):
                return "OK", 200

            pending_metadata = get_pending_transaction_metadata(payment_id)
            if not pending_metadata and reference_id:
                resolved_payment_id, resolved_metadata = _resolve_paypear_reference()
                if resolved_metadata:
                    payment_id = resolved_payment_id
                    if is_transaction_fully_issued(payment_id):
                        return "OK", 200
                    if is_transaction_processing(payment_id):
                        return "OK", 200
                    pending_metadata = resolved_metadata
            if not pending_metadata:
                logger.warning("%s webhook ignored: payment_id=%s has no pending metadata.", provider_label, payment_id)
                _log_payment_lost(
                    provider_label, payment_id, "no_pending_metadata", data if isinstance(data, dict) else None
                )
                return "OK", 200

            metadata = dict(pending_metadata)
            metadata["external_payment_id"] = payment_id
            if provider_payment_id and provider_payment_id != payment_id:
                metadata["paypear_provider_payment_id"] = provider_payment_id
            metadata["payment_confirmed"] = True
            metadata["payment_method"] = payment_method_name
            metadata["bot_source"] = normalize_bot_source(metadata.get("bot_source")) or BOT_SOURCE

            update_transaction_metadata(payment_id, metadata, statuses=("pending",))
            processing_started = start_transaction_processing(payment_id)
            if not processing_started:
                _log_payment_lost(provider_label, payment_id, "could_not_start_processing", metadata)
                return "OK", 200

            update_transaction_metadata(payment_id, metadata, statuses=("processing",))

            return _dispatch_confirmed_payment(
                provider_label,
                payment_id,
                metadata,
                processing_started=processing_started,
            )
        except Exception:
            logger.exception("Error in %s webhook handler.", provider_label.lower())
            if processing_started and payment_id:
                reset_transaction_to_pending(payment_id)
            return "Error", 500

    @flask_app.route("/lava-webhook", methods=["POST"])
    def lava_webhook_handler():
        processing_started = False
        payment_id = ""
        try:
            if BOT_SOURCE != "reserve":
                logger.error("LAVA webhook reached the wrong runtime: BOT_SOURCE=%s", BOT_SOURCE)
                return "Wrong runtime", 503

            raw_body = request.get_data(cache=True) or b""
            data = request.get_json(silent=True)
            if not raw_body or not isinstance(data, dict):
                return "Bad request", 400

            webhook_secret = str(
                os.environ.get("LAVA_WEBHOOK_SECRET")
                or os.environ.get("LAVA_ADDITIONAL_KEY")
                or ""
            ).strip()
            if not webhook_secret:
                logger.error("LAVA webhook rejected: webhook secret is not configured.")
                send_critical_alert(
                    BotError.PAYMENT_LAVA_FAILED,
                    details="source=reserve reason=webhook_secret_missing path=/lava-webhook",
                    respect_cooldown=False,
                )
                return "Not configured", 503

            if not lava_api.verify_webhook_request(
                raw_body,
                request.headers,
                allow_legacy=True,
                secret=webhook_secret,
            ):
                logger.warning("LAVA webhook rejected: invalid signature.")
                return "Forbidden", 403

            webhook_invoice_id = str(data.get("invoice_id") or data.get("invoiceId") or "").strip()
            order_id = str(data.get("order_id") or data.get("orderId") or "").strip()
            status_raw = str(data.get("status") or "").strip().lower()
            mapped_status = lava_api.map_lava_status(status_raw)
            payment_id = webhook_invoice_id

            resolved_metadata = None
            if payment_id:
                resolved_metadata = get_pending_transaction_metadata(payment_id)
            if not resolved_metadata and order_id:
                resolved = get_pending_lava_transaction_by_order_id(order_id)
                if resolved:
                    payment_id = str(resolved.get("payment_id") or "").strip()
                    candidate_metadata = resolved.get("metadata")
                    if isinstance(candidate_metadata, dict):
                        resolved_metadata = dict(candidate_metadata)

            logger.info(
                "Received LAVA webhook: payment_id=%s order_id=%s status=%s mapped=%s",
                payment_id or None,
                order_id or None,
                status_raw or None,
                mapped_status,
            )
            if not payment_id:
                _log_payment_lost("LAVA", None, "missing_payment_id")
                return "OK", 200

            # Signature was checked above. Persist accounting independently of delivery,
            # including redeliveries after the key has already been issued.
            try:
                from shop_bot.data_manager.finance_live import capture_webhook
                capture_webhook(data, _finance_db_path(),
                                os.environ.get('LAVA_FINANCE_HISTORY_FILE', '').strip(),
                                shop_id=os.environ.get('LAVA_SHOP_ID', '').strip())
            except Exception as finance_exc:
                logger.error('LAVA finance capture failed: %s', type(finance_exc).__name__)

            if is_transaction_fully_issued(payment_id):
                return "OK", 200
            if mapped_status not in {"succeeded", "canceled"}:
                return "OK", 200
            if is_transaction_processing(payment_id):
                return "OK", 200
            if not isinstance(resolved_metadata, dict):
                logger.warning("LAVA webhook ignored: payment_id=%s has no pending metadata.", payment_id)
                _log_payment_lost("LAVA", payment_id, "no_pending_metadata")
                return "OK", 200

            metadata = dict(resolved_metadata)
            metadata_payment_method = str(metadata.get("payment_method") or "").strip().lower()
            metadata_bot_source = normalize_bot_source(metadata.get("bot_source"))
            if metadata_payment_method != "lava" or metadata_bot_source != "reserve":
                logger.error("LAVA webhook rejected: local transaction ownership mismatch for payment_id=%s", payment_id)
                return "Conflict", 409
            expected_order_id = str(metadata.get("lava_order_id") or "").strip()
            is_placeholder = payment_id == f"lava-order:{expected_order_id}" and bool(metadata.get("lava_invoice_pending"))
            expected_invoice_id = webhook_invoice_id if is_placeholder else str(metadata.get("lava_invoice_id") or payment_id).strip()
            if not expected_order_id or order_id != expected_order_id:
                logger.error("LAVA webhook rejected: order id mismatch for payment_id=%s", payment_id)
                return "Conflict", 409
            if not webhook_invoice_id or webhook_invoice_id != expected_invoice_id or (not is_placeholder and payment_id != expected_invoice_id):
                logger.error("LAVA webhook rejected: invoice id mismatch for payment_id=%s", payment_id)
                return "Conflict", 409

            transaction = get_transaction_by_payment_id(payment_id) or {}
            expected_amount_raw = transaction.get("amount_rub", metadata.get("price"))
            try:
                expected_amount = Decimal(str(expected_amount_raw)).quantize(Decimal("0.01"))
                webhook_amount = Decimal(str(data.get("amount"))).quantize(Decimal("0.01"))
            except (InvalidOperation, TypeError, ValueError):
                logger.error("LAVA webhook rejected: invalid amount for payment_id=%s", payment_id)
                return "Conflict", 409
            if expected_amount <= 0 or webhook_amount != expected_amount:
                logger.error("LAVA webhook rejected: amount mismatch for payment_id=%s", payment_id)
                return "Conflict", 409

            provider_state = asyncio.run(lava_api.check_status(invoice_id=expected_invoice_id))
            provider_status = str(provider_state.get("provider_status") or "").strip().lower()
            provider_mapped_status = str(provider_state.get("status") or "").strip().lower()

            configured_shop_id = str(os.environ.get("LAVA_SHOP_ID") or "").strip()
            provider_shop_id = str(provider_state.get("shop_id") or "").strip()
            provider_invoice_id = str(provider_state.get("invoice_id") or "").strip()
            provider_order_id = str(provider_state.get("order_id") or "").strip()
            provider_currency = str(provider_state.get("currency") or "").strip().upper()
            try:
                provider_amount = Decimal(str(provider_state.get("amount"))).quantize(Decimal("0.01"))
            except (InvalidOperation, TypeError, ValueError):
                logger.error("LAVA status rejected: invalid amount for payment_id=%s", payment_id)
                return "Conflict", 409

            if (
                not configured_shop_id
                or provider_shop_id != configured_shop_id
                or provider_invoice_id != expected_invoice_id
                or provider_order_id != expected_order_id
                or provider_amount != expected_amount
                or (provider_currency and provider_currency != "RUB")
            ):
                logger.error("LAVA status rejected: invoice integrity mismatch for payment_id=%s", payment_id)
                send_critical_alert(
                    BotError.PAYMENT_LAVA_FAILED,
                    details=(
                        f"source=reserve payment_id={payment_id} "
                        "reason=provider_invoice_integrity_mismatch"
                    ),
                    respect_cooldown=False,
                )
                return "Conflict", 409

            if mapped_status == "canceled":
                if provider_mapped_status != "canceled":
                    logger.warning(
                        "LAVA cancellation deferred: provider status mismatch for payment_id=%s status=%s",
                        payment_id,
                        provider_status or "unknown",
                    )
                    return "Accepted", 202
            elif provider_mapped_status != "succeeded":
                logger.warning(
                    "LAVA webhook deferred: provider status is not successful for payment_id=%s status=%s",
                    payment_id,
                    provider_status or "unknown",
                )
                return "Accepted", 202

            if is_placeholder:
                try:
                    promoted = promote_lava_pending_transaction(expected_order_id, expected_invoice_id)
                except ValueError:
                    logger.warning("LAVA webhook rejected: placeholder ownership conflict for order_id=%s", expected_order_id)
                    return "Conflict", 409
                if not promoted:
                    return "Conflict", 409
                payment_id = expected_invoice_id
                metadata = dict(promoted.get("metadata") or metadata)
                # A competing webhook or create-payment response may already have
                # promoted and completed the row. Never reset its terminal state.
                if is_transaction_fully_issued(payment_id) or is_transaction_processing(payment_id):
                    return "OK", 200

            if mapped_status == "canceled":
                mark_transaction_canceled(payment_id, payment_method="LAVA")
                return "OK", 200

            metadata["external_payment_id"] = payment_id
            metadata["lava_invoice_id"] = expected_invoice_id
            metadata["lava_order_id"] = expected_order_id
            metadata["payment_confirmed"] = True
            metadata["payment_method"] = "LAVA"
            metadata["provider_status"] = provider_status or "success"
            metadata["bot_source"] = normalize_bot_source(metadata.get("bot_source")) or "reserve"
            pay_time = str(data.get("pay_time") or data.get("payTime") or "").strip()
            if not metadata.get("payment_confirmed_at") and not metadata.get("issuance_target_expiry_ms"):
                confirmed_at = datetime.now(UTC)
                metadata["payment_confirmed_at"] = confirmed_at.replace(tzinfo=None).isoformat(timespec="seconds")
                metadata["paid_at"] = _lava_paid_at_utc(pay_time, confirmed_at)
            pay_service = str(data.get("pay_service") or data.get("payService") or "").strip()
            if pay_service:
                metadata["lava_pay_service"] = pay_service

            update_transaction_metadata(payment_id, metadata, statuses=("pending",))
            processing_started = start_transaction_processing(payment_id)
            if not processing_started:
                # A concurrent duplicate webhook may lose the pending -> processing
                # transition after the first request has already claimed or issued it.
                if is_transaction_fully_issued(payment_id) or is_transaction_processing(payment_id):
                    return "OK", 200
                _log_payment_lost("LAVA", payment_id, "could_not_start_processing", metadata)
                return "OK", 200
            update_transaction_metadata(payment_id, metadata, statuses=("processing",))
            return _dispatch_confirmed_payment(
                "LAVA",
                payment_id,
                metadata,
                processing_started=processing_started,
            )
        except Exception as exc:
            logger.exception(
                "Error in LAVA webhook handler: %s",
                lava_api.get_exception_code(exc),
            )
            if processing_started and payment_id:
                reset_transaction_to_pending(payment_id)
            return "Error", 500

    @flask_app.route("/paypear-webhook", methods=["POST"])
    def paypear_webhook_handler():
        return _handle_provider_webhook(
            provider_label="PayPear",
            payment_method_name="PayPear",
            signature_setting_key="paypear_webhook_secret",
            verify_signature_fn=verify_paypear_webhook_signature,
            map_status_fn=map_paypear_status,
            error_type=BotError.PAYMENT_PAYPEAR_FAILED,
            alert_path="/paypear-webhook",
            read_signature_from_body=True,
        )

    @flask_app.route("/chatwoot-webhook", methods=["POST"])
    def chatwoot_webhook_handler():
        try:
            if get_setting("chatwoot_enabled") != "true":
                return "OK", 200

            secret = (os.environ.get("CHATWOOT_WEBHOOK_SECRET") or "").strip()
            if not secret:
                logger.debug("Chatwoot webhook: CHATWOOT_WEBHOOK_SECRET is not configured, skipping.")
                return "OK", 200

            raw_body = request.get_data(cache=True) or b""
            signature = request.headers.get("X-Chatwoot-Signature", "")
            timestamp = request.headers.get("X-Chatwoot-Timestamp", "")
            if not _is_fresh_chatwoot_timestamp(timestamp):
                logger.warning("Chatwoot webhook: stale or invalid timestamp, skipping.")
                return "OK", 200
            if not _is_valid_chatwoot_signature(raw_body, signature, timestamp, secret):
                logger.warning("Chatwoot webhook: invalid signature, skipping.")
                return "OK", 200

            data = request.get_json(silent=True) or {}
            event = data.get("event")

            if event not in {"conversation_created", "message_created"}:
                return "OK", 200

            source_id, contact_id, source_username = _extract_chatwoot_contact_context(data)

            if not source_id and not source_username:
                logger.debug("Chatwoot webhook: missing source_id or username, skipping enrichment.")
                return "OK", 200
            if not contact_id:
                logger.debug("Chatwoot webhook: missing contact_id, skipping enrichment.")
                return "OK", 200

            user_id = None
            if source_id:
                try:
                    user_id = int(source_id)
                except (ValueError, TypeError):
                    logger.warning("Chatwoot webhook: source_id=%s is not a valid user_id.", source_id)
            if user_id is None and source_username:
                user_id = get_user_id_by_username(source_username)

            if not user_id:
                logger.warning(
                    "Chatwoot webhook: source_id=%s and username=%s did not resolve to a known user.",
                    source_id,
                    source_username,
                )
                return "OK", 200

            chatwoot_url = (get_setting("chatwoot_api_url") or "").rstrip("/")
            account_id = get_setting("chatwoot_account_id")
            api_token = get_setting("chatwoot_api_token")

            if not all([chatwoot_url, account_id, api_token]):
                logger.warning("Chatwoot webhook: missing API settings, skipping enrichment.")
                return "OK", 200

            additional_attrs = _build_chatwoot_additional_attributes(user_id)
            custom_attrs = _build_chatwoot_custom_attributes(additional_attrs)

            patch_url = f"{chatwoot_url}/api/v1/accounts/{account_id}/contacts/{contact_id}"
            patch_body = json.dumps(
                {
                    "additional_attributes": additional_attrs,
                    "custom_attributes": custom_attrs,
                }
            ).encode()
            patch_req = urllib.request.Request(
                patch_url,
                data=patch_body,
                headers={
                    "api_access_token": api_token,
                    "Content-Type": "application/json",
                },
                method="PATCH",
            )
            try:
                with urllib.request.urlopen(patch_req, timeout=5):
                    pass
                logger.info("Chatwoot webhook: enriched contact %s for user %s.", contact_id, user_id)
            except Exception as patch_err:
                logger.error("Chatwoot webhook: failed to enrich contact %s: %s", contact_id, patch_err)

            return "OK", 200  # noqa: TRY300
        except Exception:
            logger.exception("Error in chatwoot webhook handler")
            return "OK", 200

    if os.environ.get("MOBILE_APP_ENABLED", "").lower() in {"1", "true", "yes"}:
        from shop_bot import mobile_app

        def _mobile_config_for_key(key_data):
            # API already checked ownership/paid expiry. Keep all delivery policy
            # and XHTTP fields in the existing route; never issue/renew a key.
            sub_id = str(key_data.get("panel_sub_id") or "").strip()
            if sub_id:
                resolved = get_active_key_by_panel_sub_id(sub_id)
                if not resolved or int(resolved.get("user_id") or 0) != int(key_data["user_id"]):
                    return "Subscription unavailable", 404
                return managed_subscription_route(sub_id)

            g.subscription_host_name = str(key_data.get("host_name") or "").strip()
            g.subscription_include_white = _key_includes_white_subscription(key_data)
            tier = _key_subscription_tier(key_data)
            cached = xui_api.normalize_subscription_payload(key_data.get("subscription_payload"))
            payload = _fallback_subscription_payload_for_tier(key_data, cached, subscription_tier=tier)
            if not payload:
                host = get_host(g.subscription_host_name)
                if host:
                    # Existing issued HTTPS legacy URLs are read by the current
                    # delivery helper. The app cannot provide an upstream URL.
                    payload = _fetch_direct_subscription_payload(host, key_data, "", subscription_tier=tier)
            if not payload:
                return "Subscription unavailable", 503
            return _subscription_plain_text_response(payload, subscription_tier=tier)

        mobile_app.register_api(flask_app, get_user, get_user_keys, get_key_by_id, _mobile_config_for_key)

    return flask_app
