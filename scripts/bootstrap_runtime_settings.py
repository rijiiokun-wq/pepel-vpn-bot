from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

ROOT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = str(ROOT_DIR / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

if TYPE_CHECKING:
    from shop_bot.data_manager import database as database_module

logger = logging.getLogger("bootstrap_runtime_settings")
database: database_module | None = None
UsernameResolver = Callable[[str, float], str | None]
_TELEGRAM_TOKEN_RE = re.compile(r"^[0-9]{3,20}:[A-Za-z0-9_-]{20,200}$")

ENV_SETTING_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("panel_login", ("PANEL_LOGIN",)),
    ("panel_password", ("PANEL_PASSWORD_HASH",)),
    ("telegram_bot_token", ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN")),
    ("telegram_bot_username", ("TELEGRAM_BOT_USERNAME", "BOT_USERNAME")),
    ("reserve_bot_token", ("RESERVE_BOT_TOKEN",)),
    ("reserve_bot_username", ("RESERVE_BOT_USERNAME",)),
    ("admin_telegram_id", ("ADMIN_TELEGRAM_ID", "TELEGRAM_ADMIN_ID")),
    ("flask_secret_key", ("FLASK_SECRET_KEY",)),
    ("yookassa_shop_id", ("YOOKASSA_SHOP_ID",)),
    ("yookassa_secret_key", ("YOOKASSA_SECRET_KEY",)),
    ("yookassa_payments_enabled", ("YOOKASSA_PAYMENTS_ENABLED",)),
    ("sbp_enabled", ("SBP_ENABLED",)),
    ("cryptobot_token", ("CRYPTOBOT_TOKEN", "CRYPTO_BOT_TOKEN")),
    ("alert_telegram_bot_token", ("ALERT_TELEGRAM_BOT_TOKEN",)),
    ("alert_telegram_chat_id", ("ALERT_TELEGRAM_CHAT_ID",)),
    ("alert_telegram_thread_id", ("ALERT_TELEGRAM_THREAD_ID",)),
    ("paypear_shop_id", ("PAYPEAR_SHOP_ID",)),
    ("paypear_secret_key", ("PAYPEAR_SECRET_KEY",)),
    ("paypear_api_key", ("PAYPEAR_API_KEY",)),
    ("paypear_webhook_secret", ("PAYPEAR_WEBHOOK_SECRET",)),
    ("paypear_webhook_url", ("PAYPEAR_WEBHOOK_URL",)),
    ("paypear_return_url", ("PAYPEAR_RETURN_URL",)),
    ("paypear_api_base_url", ("PAYPEAR_API_BASE_URL",)),
    ("paypear_payment_lifetime_minutes", ("PAYPEAR_PAYMENT_LIFETIME_MINUTES",)),
    ("domain", ("DOMAIN", "PUBLIC_BASE_URL", "PAYPEAR_PUBLIC_BASE_URL")),
)

USERNAME_BOOTSTRAP_SOURCES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("telegram_bot_username", "telegram_bot_token", ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN")),
    ("reserve_bot_username", "reserve_bot_token", ("RESERVE_BOT_TOKEN",)),
)

REQUIRED_STARTUP_SETTINGS = (
    "telegram_bot_token",
    "telegram_bot_username",
    "reserve_bot_token",
    "reserve_bot_username",
    "admin_telegram_id",
)
AUTHORITATIVE_ENV_SETTINGS = frozenset(
    {
        "panel_login",
        "panel_password",
        "flask_secret_key",
        "yookassa_payments_enabled",
        "sbp_enabled",
        "paypear_webhook_url",
        "paypear_return_url",
        "paypear_api_base_url",
        "paypear_payment_lifetime_minutes",
        "domain",
    }
)
PBKDF2_HASH_SETTINGS = frozenset({"panel_password"})


@dataclass(frozen=True)
class BootstrapReport:
    updated: tuple[str, ...]
    kept_existing: tuple[str, ...]
    missing_required: tuple[str, ...]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Populate missing bot_settings values from runtime environment variables. Secret values are never printed."
        ),
    )
    parser.add_argument(
        "--require-startup-settings",
        action="store_true",
        help="Fail when Telegram runtime settings required for main/reserve startup are still missing.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite DB settings from env. CI deploy should normally omit this.",
    )
    parser.add_argument(
        "--telegram-timeout-seconds",
        type=float,
        default=8.0,
        help="Timeout for Telegram getMe username discovery.",
    )
    return parser


def _non_empty(value: object | None) -> str:
    return str(value or "").strip()


def _first_env_value(env_names: tuple[str, ...]) -> str | None:
    for env_name in env_names:
        value = _non_empty(os.getenv(env_name))
        if value:
            return value
    return None


def _current_setting(setting_key: str) -> str:
    return _non_empty(_database().get_setting(setting_key))


def _persist_setting(setting_key: str, value: str) -> None:
    _database().update_setting(setting_key, value)
    persisted = _current_setting(setting_key)
    if persisted != value:
        raise RuntimeError(f"Failed to persist setting: {setting_key}")


def _is_pbkdf2_hash(value: str) -> bool:
    parts = value.split(":", 2)
    return len(parts) == 3 and parts[0] == "pbkdf2" and bool(parts[1]) and bool(parts[2])


def _database() -> database_module:
    if database is None:
        raise RuntimeError("Database module is not configured.")
    return database


def _set_from_env_if_needed(
    setting_key: str,
    env_names: tuple[str, ...],
    *,
    overwrite: bool,
) -> str | None:
    value = _first_env_value(env_names)
    if not value:
        return None

    if setting_key in PBKDF2_HASH_SETTINGS and not _is_pbkdf2_hash(value):
        raise RuntimeError(f"Invalid PBKDF2 hash for setting: {setting_key}")

    current_value = _current_setting(setting_key)
    if current_value and not overwrite and setting_key not in AUTHORITATIVE_ENV_SETTINGS:
        return "kept"

    _persist_setting(setting_key, value)
    return "updated"


def _fetch_bot_username_from_telegram(token: str, timeout_seconds: float) -> str | None:
    if not token or not _TELEGRAM_TOKEN_RE.fullmatch(token):
        logger.warning("Telegram getMe username discovery skipped: bot token format is invalid.")
        return None

    connection = http.client.HTTPSConnection("api.telegram.org", timeout=timeout_seconds)
    try:
        connection.request(
            "GET",
            f"/bot{token}/getMe",
            headers={"User-Agent": "pepelbot-runtime-bootstrap/1.0"},
        )
        response = connection.getresponse()
        if response.status >= 400:
            logger.warning("Telegram getMe failed with HTTP %s while resolving bot username.", response.status)
            return None
        payload = json.loads(response.read().decode("utf-8"))
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning("Telegram getMe failed while resolving bot username: %s", exc.__class__.__name__)
        return None
    finally:
        connection.close()

    username = _non_empty((payload.get("result") or {}).get("username"))
    return username.lstrip("@") or None


def _bootstrap_usernames_from_tokens(
    *,
    overwrite: bool,
    username_resolver: UsernameResolver,
    telegram_timeout_seconds: float,
) -> tuple[list[str], list[str]]:
    updated: list[str] = []
    kept_existing: list[str] = []

    for username_setting, token_setting, token_env_names in USERNAME_BOOTSTRAP_SOURCES:
        current_username = _current_setting(username_setting)
        if current_username and not overwrite:
            kept_existing.append(username_setting)
            continue

        token = _first_env_value(token_env_names) or _current_setting(token_setting)
        if not token:
            continue

        username = username_resolver(token, telegram_timeout_seconds)
        if not username:
            continue

        _persist_setting(username_setting, username.lstrip("@"))
        updated.append(username_setting)

    return updated, kept_existing


def _missing_required_settings() -> tuple[str, ...]:
    return tuple(setting_key for setting_key in REQUIRED_STARTUP_SETTINGS if not _current_setting(setting_key))


def bootstrap_runtime_settings(
    *,
    require_startup_settings: bool = False,
    overwrite: bool = False,
    telegram_timeout_seconds: float = 8.0,
    username_resolver: UsernameResolver = _fetch_bot_username_from_telegram,
) -> BootstrapReport:
    _database().initialize_db()

    updated: list[str] = []
    kept_existing: list[str] = []
    for setting_key, env_names in ENV_SETTING_MAP:
        result = _set_from_env_if_needed(setting_key, env_names, overwrite=overwrite)
        if result == "updated":
            updated.append(setting_key)
        elif result == "kept":
            kept_existing.append(setting_key)

    username_updates, username_existing = _bootstrap_usernames_from_tokens(
        overwrite=overwrite,
        username_resolver=username_resolver,
        telegram_timeout_seconds=telegram_timeout_seconds,
    )
    updated.extend(username_updates)
    kept_existing.extend(username_existing)

    missing_required = _missing_required_settings()
    report = BootstrapReport(
        updated=tuple(dict.fromkeys(updated)),
        kept_existing=tuple(dict.fromkeys(kept_existing)),
        missing_required=missing_required,
    )

    if require_startup_settings and missing_required:
        missing = ", ".join(missing_required)
        raise RuntimeError(f"Missing required runtime settings after bootstrap: {missing}")

    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = _build_parser().parse_args()

    from shop_bot.data_manager import database as runtime_database

    globals()["database"] = runtime_database

    try:
        report = bootstrap_runtime_settings(
            require_startup_settings=args.require_startup_settings,
            overwrite=args.overwrite,
            telegram_timeout_seconds=args.telegram_timeout_seconds,
        )
    except RuntimeError as exc:
        print(f"runtime settings bootstrap failed: {exc}")
        return 1

    if report.updated:
        print("runtime settings bootstrap updated: " + ", ".join(report.updated))
    else:
        print("runtime settings bootstrap updated: none")

    if report.kept_existing:
        print("runtime settings bootstrap kept existing: " + ", ".join(report.kept_existing))

    if report.missing_required:
        print("runtime settings bootstrap missing required: " + ", ".join(report.missing_required))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
