import json
import logging
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from flask import g, has_app_context

logger = logging.getLogger(__name__)

DEFAULT_DB_FILE = Path("/app/project/users.db")
DATA_VOLUME_DB_FILE = Path("/app/data/shop.db")


def _resolve_db_file_path(
    *,
    env_value: str | None = None,
    data_candidate: Path = DATA_VOLUME_DB_FILE,
    default_candidate: Path = DEFAULT_DB_FILE,
    cwd: Path | None = None,
) -> Path:
    raw_env = str(env_value if env_value is not None else os.getenv("SHOP_DB_FILE", "")).strip()
    if raw_env:
        env_path = Path(raw_env).expanduser()
        if env_path.is_absolute():
            return env_path
        base_dir = cwd or Path.cwd()
        return (base_dir / env_path).resolve()

    if data_candidate.exists():
        return data_candidate

    return default_candidate


def _ensure_db_parent_dir(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.warning("Failed to create DB parent directory for '%s': %s", path, e)


DB_FILE = _resolve_db_file_path()
_ensure_db_parent_dir(DB_FILE)
logger.info("Using SQLite DB file: %s", DB_FILE)

# Timeout (ms) SQLite waits when another connection holds a write lock.
# Keep short to avoid freezing asyncio handlers on DB contention.
SQLITE_BUSY_TIMEOUT_MS = max(
    500,
    int(str(os.environ.get("SQLITE_BUSY_TIMEOUT_MS", "2000")).strip() or "2000"),
)
HOST_STATUS_STALE_SECONDS = max(
    60,
    int(str(os.environ.get("HOST_STATUS_STALE_SECONDS", "240")).strip() or "240"),
)
HOST_DELIVERY_STATES = frozenset({"draft", "active", "disabled"})
HOST_TOPOLOGY_ROLES = frozenset({"ordinary", "whitelist"})
HOST_TOPOLOGY_STATES = frozenset(
    {
        "registered",
        "provisioning",
        "backfilling",
        "verifying",
        "delivery_active",
        "failed",
        "draining",
        "disabled",
    }
)
HOST_TOPOLOGY_TRANSITIONS = {
    "registered": frozenset({"provisioning", "disabled"}),
    "provisioning": frozenset({"backfilling", "failed", "disabled"}),
    "backfilling": frozenset({"verifying", "failed", "disabled"}),
    "verifying": frozenset({"delivery_active", "failed", "disabled"}),
    "delivery_active": frozenset({"draining", "failed", "disabled"}),
    "failed": frozenset({"provisioning", "backfilling", "disabled"}),
    "draining": frozenset({"disabled", "delivery_active", "failed"}),
    "disabled": frozenset({"registered", "provisioning"}),
}
ISSUANCE_RETRY_BACKOFF_BASE_SECONDS = max(
    5,
    int(str(os.environ.get("PAYMENT_ISSUANCE_RETRY_BACKOFF_BASE_SECONDS", "30")).strip() or "30"),
)
ISSUANCE_RETRY_BACKOFF_MAX_SECONDS = max(
    ISSUANCE_RETRY_BACKOFF_BASE_SECONDS,
    int(str(os.environ.get("PAYMENT_ISSUANCE_RETRY_BACKOFF_MAX_SECONDS", "600")).strip() or "600"),
)
_RETRY_MIN_BACKOFF_SECONDS = 30
TRANSACTION_LOCK_TTL_SECONDS = max(
    30,
    int(str(os.environ.get("PAYMENT_TRANSACTION_LOCK_TTL_SECONDS", "900")).strip() or "900"),
)

# A heartbeat owns the transaction until its lease expires. Reuse the same
# predicate in both scans and UPDATE guards so a new owner cannot lose its lock
# between the read and the write.
_TRANSACTION_LOCK_AVAILABLE_SQL = f"""(
    locked_by IS NULL
    OR TRIM(COALESCE(locked_by, '')) = ''
    OR locked_at IS NULL
    OR datetime(locked_at) <= datetime('now', '-{TRANSACTION_LOCK_TTL_SECONDS} seconds')
)"""

TX_TYPE_ALL = "all"
TX_TYPE_HYSTERIA = "hysteria"
TX_TYPE_SUPER = "super"
TX_TYPE_OLD = "old"
TX_TYPE_VLESS = "vless"
TX_TYPE_VALUES = {TX_TYPE_ALL, TX_TYPE_HYSTERIA, TX_TYPE_SUPER, TX_TYPE_OLD, TX_TYPE_VLESS}
BOT_SOURCE_ALL = "all"
BOT_SOURCE_MAIN = "main"
BOT_SOURCE_RESERVE = "reserve"
BOT_SOURCE_VALUES = {BOT_SOURCE_ALL, BOT_SOURCE_MAIN, BOT_SOURCE_RESERVE}
ISSUANCE_STATE_AWAITING_PAYMENT = "awaiting_payment"
ISSUANCE_STATE_CONFIRMED = "confirmed"
ISSUANCE_STATE_ISSUING = "issuing"
ISSUANCE_STATE_ISSUED = "issued"
ISSUANCE_STATE_RETRYABLE_FAILED = "retryable_failed"
ISSUANCE_STATE_FATAL_FAILED = "fatal_failed"
ISSUANCE_STATE_EXHAUSTED = "exhausted"
ISSUANCE_STATE_CANCELED = "canceled"
_TERMINAL_ISSUANCE_STATES = (
    ISSUANCE_STATE_ISSUED,
    ISSUANCE_STATE_FATAL_FAILED,
    ISSUANCE_STATE_EXHAUSTED,
    ISSUANCE_STATE_CANCELED,
)
ISSUANCE_STATE_VALUES = {
    ISSUANCE_STATE_AWAITING_PAYMENT,
    ISSUANCE_STATE_CONFIRMED,
    ISSUANCE_STATE_ISSUING,
    ISSUANCE_STATE_ISSUED,
    ISSUANCE_STATE_RETRYABLE_FAILED,
    ISSUANCE_STATE_FATAL_FAILED,
    ISSUANCE_STATE_EXHAUSTED,
    ISSUANCE_STATE_CANCELED,
}
INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS idx_vpn_keys_user_id ON vpn_keys(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_vpn_keys_host_name ON vpn_keys(host_name)",
    "CREATE INDEX IF NOT EXISTS idx_vpn_keys_key_email ON vpn_keys(key_email)",
    "CREATE INDEX IF NOT EXISTS idx_vpn_keys_bot_source ON vpn_keys(bot_source, created_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_vpn_keys_panel_sub_id ON vpn_keys(panel_sub_id)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_status_created ON transactions(status, created_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_payment_id_status ON transactions(payment_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_bot_source ON transactions(bot_source, created_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_transactions_issuance_retry ON transactions(issuance_state, next_retry_at)",
    "CREATE INDEX IF NOT EXISTS idx_users_registration_date ON users(registration_date DESC)",
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)",
    "CREATE INDEX IF NOT EXISTS idx_plans_host_name ON plans(host_name)",
    "CREATE INDEX IF NOT EXISTS idx_mainpanel_sync_runs_host_started ON mainpanel_inbound_sync_runs(host_name, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mainpanel_sync_items_run ON mainpanel_inbound_sync_items(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_mainpanel_sync_items_key ON mainpanel_inbound_sync_items(key_id)",
    "CREATE INDEX IF NOT EXISTS idx_whitelist_status_user ON whitelist_traffic_status(user_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_whitelist_status_email ON whitelist_traffic_status(key_email)",
)
_WAL_INITIALIZED = False
_SETTINGS_CACHE_TTL_SECONDS = 30
_settings_cache_data: dict | None = None
_settings_cache_expires_at = 0.0

_thread_local = threading.local()


def _parse_db_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        normalized = raw.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                try:
                    parsed = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    parsed = None
            if parsed is None:
                return None

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def normalize_host_delivery_state(value: str | None, *, default: str = "active") -> str:
    normalized_default = str(default or "active").strip().lower()
    if normalized_default not in HOST_DELIVERY_STATES:
        normalized_default = "active"
    normalized = str(value or normalized_default).strip().lower() or normalized_default
    if normalized not in HOST_DELIVERY_STATES:
        return normalized_default
    return normalized


def is_host_delivery_active(host: dict | None) -> bool:
    return normalize_host_delivery_state((host or {}).get("delivery_state")) == "active"


def normalize_host_topology_role(value: str | None, *, is_white_host: bool = False) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in HOST_TOPOLOGY_ROLES:
        return normalized
    return "whitelist" if is_white_host else "ordinary"


def normalize_host_topology_state(
    value: str | None,
    *,
    delivery_state: str | None = None,
) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in HOST_TOPOLOGY_STATES:
        return normalized
    legacy_state = normalize_host_delivery_state(delivery_state)
    return {
        "active": "delivery_active",
        "draft": "registered",
        "disabled": "disabled",
    }[legacy_state]


def _resolve_effective_host_status(
    *,
    status: str | None,
    last_check_at,
    is_maintenance: bool,
) -> tuple[str, bool]:
    if is_maintenance:
        return "maintenance", False

    normalized = str(status or "unknown").strip().lower() or "unknown"
    if normalized not in {"healthy", "degraded", "unavailable", "unknown", "maintenance"}:
        normalized = "unknown"

    parsed_last_check = _parse_db_datetime(last_check_at)
    is_stale = (
        normalized == "healthy"
        and parsed_last_check is not None
        and (datetime.now() - parsed_last_check).total_seconds() > HOST_STATUS_STALE_SECONDS
    )
    if is_stale:
        return "degraded", True
    return normalized, False


def _with_effective_host_status(host: dict) -> dict:
    result = dict(host)
    result["delivery_state"] = normalize_host_delivery_state(
        result.get("delivery_state"),
    )
    result["topology_role"] = normalize_host_topology_role(
        result.get("topology_role"),
        is_white_host=bool(result.get("is_white_host")),
    )
    result["topology_state"] = normalize_host_topology_state(
        result.get("topology_state"),
        delivery_state=result["delivery_state"],
    )
    raw_status = str(result.get("raw_status") or result.get("status") or "unknown").strip().lower() or "unknown"
    effective_status, is_status_stale = _resolve_effective_host_status(
        status=raw_status,
        last_check_at=result.get("last_check_at"),
        is_maintenance=bool(result.get("is_maintenance")),
    )
    result["raw_status"] = raw_status
    result["status"] = effective_status
    result["is_status_stale"] = is_status_stale
    if is_status_stale and not str(result.get("last_error") or "").strip():
        result["last_error"] = f"Нет свежей проверки хоста более {HOST_STATUS_STALE_SECONDS} сек."
    return result


def _resolve_bot_source(value: str | None, metadata: dict | None = None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"main", "reserve"}:
        return normalized

    if isinstance(metadata, dict):
        metadata_source = str(metadata.get("bot_source") or "").strip().lower()
        if metadata_source in {"main", "reserve"}:
            return metadata_source

    env_source = str(os.getenv("BOT_SOURCE", "main") or "main").strip().lower()
    return "reserve" if env_source == "reserve" else "main"


def _normalize_bot_source_filter(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {BOT_SOURCE_MAIN, BOT_SOURCE_RESERVE}:
        return normalized
    return BOT_SOURCE_ALL


def _issuance_state_for_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "paid":
        return ISSUANCE_STATE_ISSUED
    if normalized == "processing":
        return ISSUANCE_STATE_ISSUING
    if normalized == "canceled":
        return ISSUANCE_STATE_CANCELED
    return ISSUANCE_STATE_AWAITING_PAYMENT


def _ensure_indexes(cursor: sqlite3.Cursor) -> None:
    for statement in INDEX_STATEMENTS:
        try:
            cursor.execute(statement)
        except sqlite3.Error as e:
            logging.warning("Failed to create index with '%s': %s", statement, e)


def _ensure_mainpanel_inbound_sync_tables(cursor: sqlite3.Cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mainpanel_inbound_snapshots (
            host_name TEXT NOT NULL,
            inbound_id INTEGER NOT NULL,
            remark TEXT,
            tag TEXT,
            protocol TEXT,
            enable INTEGER,
            classification TEXT,
            settings_hash TEXT,
            first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (host_name, inbound_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mainpanel_inbound_sync_runs (
            run_id TEXT PRIMARY KEY,
            host_name TEXT NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            batch_limit INTEGER DEFAULT 0,
            processed INTEGER DEFAULT 0,
            updated INTEGER DEFAULT 0,
            skipped INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            needs_review INTEGER DEFAULT 0,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            error TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mainpanel_inbound_sync_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            key_id INTEGER,
            user_id INTEGER,
            email TEXT,
            inbound_id INTEGER,
            inbound_label TEXT,
            classification TEXT,
            tier TEXT,
            status TEXT,
            action TEXT,
            error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (run_id) REFERENCES mainpanel_inbound_sync_runs(run_id)
        )
    """)
    cursor.execute("PRAGMA table_info(mainpanel_inbound_sync_items)")
    item_columns = [row[1] for row in cursor.fetchall()]
    if "email" not in item_columns:
        cursor.execute("ALTER TABLE mainpanel_inbound_sync_items ADD COLUMN email TEXT")


def _ensure_wal_mode(connection: sqlite3.Connection) -> None:
    global _WAL_INITIALIZED
    if _WAL_INITIALIZED:
        return
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        _WAL_INITIALIZED = True
    except sqlite3.Error as e:
        logging.warning("Failed to enable WAL mode: %s", e)


def _create_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0)  # noqa: repo-guard
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    # One Unicode-aware classifier is shared by SQL filters and Python summaries.
    conn.create_function("product_type", 2, classify_product_type, deterministic=True)
    _ensure_wal_mode(conn)
    return conn


def get_db() -> sqlite3.Connection:
    if has_app_context():
        connection = g.get("db_connection")
        if connection is None:
            connection = _create_connection()
            g.db_connection = connection
        return connection

    conn = getattr(_thread_local, "connection", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            try:
                conn.close()
            except Exception:
                pass
            conn = None

    conn = _create_connection()
    _thread_local.connection = conn
    return conn


def close_db(_: Exception | None = None) -> None:
    if not has_app_context():
        return

    connection = g.pop("db_connection", None)
    if connection is not None:
        connection.close()


def record_shadow_diff(
    *,
    payment_id: str,
    legacy_status: str | None,
    shadow_status: str | None,
    legacy_reason: str | None,
    shadow_reason: str | None,
    legacy_ms: int,
    shadow_ms: int,
) -> None:
    """Persist one legacy/backend shadow comparison mismatch."""
    conn = _create_connection()
    try:
        conn.execute(
            """
            INSERT INTO shadow_diffs
                (payment_id, legacy_status, shadow_status, legacy_reason, shadow_reason, legacy_ms, shadow_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payment_id,
                legacy_status,
                shadow_status,
                legacy_reason,
                shadow_reason,
                legacy_ms,
                shadow_ms,
            ),
        )
        conn.commit()
    finally:
        conn.close()


_SUPER_MARKERS = ("super", "супер")
_OLD_MARKERS = ("incident", "old", "legacy", "архив", "стар")


def _normalize_product_text(value: str | None) -> str:
    return str(value or "").strip().lower()


def _contains_any_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _is_old_product_text(text: str) -> bool:
    return _contains_any_marker(text, _OLD_MARKERS)


def _is_super_product_text(text: str) -> bool:
    return _contains_any_marker(text, _SUPER_MARKERS)


def classify_product_type(host_name: str | None, plan_name: str | None = None) -> str:
    host = str(host_name or "").strip()
    host_lower = _normalize_product_text(host_name)
    plan_lower = _normalize_product_text(plan_name)

    # "Super Old" (incident / legacy super hosts) must be separated from active SUPER.
    is_super_old = (_is_super_product_text(host_lower) and _is_old_product_text(host_lower)) or (
        _is_super_product_text(plan_lower) and _is_old_product_text(plan_lower)
    )
    if is_super_old:
        return TX_TYPE_OLD

    if _is_super_product_text(host_lower) or _is_super_product_text(plan_lower):
        return TX_TYPE_SUPER

    if host == "blitz_hysteria2" or "hysteria" in host_lower:
        return TX_TYPE_HYSTERIA

    if _is_old_product_text(host_lower) or _is_old_product_text(plan_lower):
        return TX_TYPE_OLD

    return TX_TYPE_VLESS


def _tx_type_sql_condition(tx_type: str, table_alias: str = "t") -> str:
    if tx_type not in TX_TYPE_VALUES or tx_type == TX_TYPE_ALL:
        return "1=1"
    metadata = f"CASE WHEN json_valid({table_alias}.metadata) THEN {table_alias}.metadata ELSE '{{}}' END"
    return (
        f"product_type(json_extract({metadata}, '$.host_name'), "
        f"json_extract({metadata}, '$.plan_name')) = '{tx_type}'"
    )


def get_dashboard_snapshot(
    date_from: str,
    date_to: str,
    tx_type: str = TX_TYPE_ALL,
    bot_source: str = BOT_SOURCE_ALL,
    search: str = "",
    page: int = 1,
    per_page: int = 8,
    *,
    include_all: bool = False,
) -> dict:
    """Read-only, consistent financial snapshot; never changes payment/issuance state."""
    from contextlib import closing

    from shop_bot.data_manager.dashboard_analytics import build_dashboard_snapshot

    with closing(_create_connection()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        return build_dashboard_snapshot(
            conn,
            classify_product_type=classify_product_type,
            date_from=date_from,
            date_to=date_to,
            tx_type=tx_type,
            bot_source=bot_source,
            search=search,
            page=page,
            per_page=per_page,
            include_all=include_all,
        )


def initialize_db():
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY, username TEXT, total_spent REAL DEFAULT 0,
                    total_months INTEGER DEFAULT 0, trial_used BOOLEAN DEFAULT 0,
                    trial_super_used BOOLEAN DEFAULT 0,
                    agreed_to_terms BOOLEAN DEFAULT 0,
                    blitz_refresh_used BOOLEAN DEFAULT 0,
                    registration_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_banned BOOLEAN DEFAULT 0,
                    referred_by INTEGER,
                    referral_balance REAL DEFAULT 0,
                    referral_balance_all REAL DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vpn_keys (
                    key_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    host_name TEXT NOT NULL,
                    xui_client_uuid TEXT NOT NULL,
                    key_email TEXT NOT NULL UNIQUE,
                    issued_key_value TEXT,
                    subscription_payload TEXT,
                    panel_sub_id TEXT,
                    subscription_tier TEXT,
                    expiry_date TIMESTAMP,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    whitelist_cycle_anchor_at TIMESTAMP,
                    whitelist_traffic_unlimited_override INTEGER NOT NULL DEFAULT 0,
                    bot_source TEXT DEFAULT 'main'
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS transactions (
                    username TEXT,
                    transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_id TEXT UNIQUE NOT NULL,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    amount_rub REAL NOT NULL,
                    amount_currency REAL,
                    currency_name TEXT,
                    payment_method TEXT,
                    metadata TEXT,
                    created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    bot_source TEXT DEFAULT 'main',
                    issuance_state TEXT DEFAULT 'awaiting_payment',
                    retry_count INTEGER DEFAULT 0,
                    next_retry_at TIMESTAMP,
                    confirmed_at TIMESTAMP,
                    last_attempt_at TIMESTAMP,
                    last_error TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS payment_accounting_effects (
                    payment_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    amount_rub REAL NOT NULL,
                    months INTEGER NOT NULL,
                    referrer_id INTEGER,
                    referral_reward REAL NOT NULL DEFAULT 0,
                    activated_super INTEGER NOT NULL DEFAULT 0,
                    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS channel_gate_users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    bot_source TEXT,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_event TEXT,
                    last_status TEXT,
                    last_checked_at TIMESTAMP,
                    joined_confirmed_at TIMESTAMP,
                    prompt_count INTEGER DEFAULT 0,
                    check_count INTEGER DEFAULT 0,
                    last_error TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS support_threads (
                    user_id INTEGER PRIMARY KEY,
                    thread_id INTEGER NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS xui_hosts(
                    host_name TEXT NOT NULL,
                    host_url TEXT NOT NULL,
                    host_username TEXT NOT NULL,
                    host_pass TEXT NOT NULL,
                    host_inbound_id INTEGER NOT NULL,
                    host_subscription_base_url TEXT,
                    host_api_token_env TEXT,
                    is_white_host INTEGER DEFAULT 0,
                    node_id INTEGER,
                    node_inbound_id INTEGER,
                    node_sync_status TEXT DEFAULT 'pending',
                    node_sync_error TEXT,
                    node_synced_at TIMESTAMP,
                    delivery_state TEXT DEFAULT 'active',
                    topology_role TEXT,
                    topology_state TEXT,
                    topology_operation_id TEXT,
                    topology_last_error TEXT,
                    topology_updated_at TIMESTAMP,
                    topology_activated_at TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host_name TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    months INTEGER NOT NULL,
                    price REAL NOT NULL,
                    FOREIGN KEY (host_name) REFERENCES xui_hosts (host_name)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS key_expiry_notifications (
                    key_id INTEGER NOT NULL,
                    expiry_date TEXT NOT NULL,
                    notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (key_id, expiry_date)
                )
            """)
            _ensure_mainpanel_inbound_sync_tables(cursor)
            default_settings = {
                "panel_login": "admin",
                "panel_password": "admin",
                "flask_secret_key": secrets.token_urlsafe(48),
                "about_text": None,
                "terms_url": None,
                "privacy_url": None,
                "support_user": None,
                "support_text": None,
                "channel_url": None,
                "force_subscription": "true",
                "proxy_gate_enabled": "false",
                "proxy_gate_bot_token": None,
                "proxy_gate_required_chat": "@pepelproxy",
                "proxy_gate_channel_url": "https://t.me/pepelproxy",
                "proxy_gate_target_bot_source": "reserve",
                "receipt_email": "example@example.com",
                "telegram_bot_token": None,
                "support_bot_token": None,
                "telegram_bot_username": None,
                "trial_enabled": "true",
                "trial_duration_days": "2",
                "trial_duration_days_super": "2",
                "enable_referrals": "false",
                "referral_percentage": "10",
                "referral_discount": "5",
                "minimum_withdrawal": "100",
                "support_group_id": None,
                "admin_telegram_id": None,
                "yookassa_shop_id": None,
                "yookassa_secret_key": None,
                "yookassa_payments_enabled": "false",
                "sbp_enabled": "false",
                "cryptobot_token": None,
                "heleket_merchant_id": None,
                "heleket_api_key": None,
                "domain": None,
                "ton_wallet_address": None,
                "tonapi_key": None,
                "android_url": "https://telegra.ph/Instrukciya-Android-11-09",
                "windows_url": "https://telegra.ph/Instrukciya-Windows-11-09",
                "ios_url": "https://telegra.ph/Instrukcii-ios-11-09",
                "linux_url": "https://telegra.ph/Instrukciya-Linux-11-09",
                "blitz_url": None,
                "blitz_api_token": None,
                "blitz_username": None,
                "blitz_password": None,
                "blitz_insecure_tls": "false",
                "enable_blitz": "false",
                "super_key_host_name": None,
                "standard_key_host_name": "StandartVLESS",
                "maintenance_mode": "false",
                "maintenance_message": "Покупка временно недоступна, ведутся технические работы. Попробуйте позже.",
                "main_service_mode_enabled": "false",
                "main_service_mode_message": (
                    "Уважаемые пользователи, в данный момент покупка ключей доступна только "
                    "в резервном боте — @probelvpnbot"
                ),
                "main_service_mode_reserve_bot_url": "https://t.me/probelvpnbot",
                "chatwoot_enabled": "false",
                "chatwoot_api_url": None,
                "chatwoot_account_id": None,
                "chatwoot_api_token": None,
                "chatwoot_inbox_id": None,
                "alert_telegram_bot_token": None,
                "alert_telegram_chat_id": None,
                "alert_telegram_thread_id": None,
                "welcome_message_effect_id": None,
                "onboarding_success_message_effect_id": None,
                "trial_success_message_effect_id": None,
                "purchase_success_message_effect_id": None,
                "reserve_bot_token": None,
                "reserve_bot_username": None,
                "paypear_shop_id": None,
                "paypear_secret_key": None,
                "paypear_api_key": None,
                "paypear_webhook_secret": None,
                "paypear_webhook_url": None,
                "paypear_api_base_url": "https://engine.paypear.io/api/v1",
                "paypear_payment_lifetime_minutes": "60",
                "platega_api_key": None,
                "platega_shop_id": None,
                "platega_webhook_secret": None,
                "platega_sbp_method_id": "2",
                "platega_card_method_id": "10,12,11",
            }
            run_migration()
            _ensure_indexes(cursor)
            for key, value in default_settings.items():
                cursor.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
            from shop_bot.data_manager.migrations import run_backend_migrations

            applied = run_backend_migrations(conn)
            if applied:
                logging.info("Applied %d backend migration(s).", applied)
            from shop_bot.data_manager.referrals import initialize_schema

            initialize_schema(conn)
            conn.commit()
            logging.info("Database initialized successfully.")
    except sqlite3.Error as e:
        logging.error(f"Database error on initialization: {e}")


def run_migration():
    if not DB_FILE.exists():
        logging.error("Users.db database file was not found. There is nothing to migrate.")
        return

    logging.info(f"Starting the migration of the database: {DB_FILE}")

    conn = None
    try:
        conn = _create_connection()
        cursor = conn.cursor()

        logging.info("The migration of the table 'users' ...")

        cursor.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]

        if "referred_by" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
            logging.info(" -> The column 'referred_by' is successfully added.")
        else:
            logging.info(" -> The column 'referred_by' already exists.")

        if "referral_balance" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN referral_balance REAL DEFAULT 0")
            logging.info(" -> The column 'referral_balance' is successfully added.")
        else:
            logging.info(" -> The column 'referral_balance' already exists.")

        if "referral_balance_all" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN referral_balance_all REAL DEFAULT 0")
            logging.info(" -> The column 'referral_balance_all' is successfully added.")
        else:
            logging.info(" -> The column 'referral_balance_all' already exists.")

        if "blitz_refresh_used" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN blitz_refresh_used BOOLEAN DEFAULT 0")
            logging.info(" -> The column 'blitz_refresh_used' is successfully added.")
        else:
            logging.info(" -> The column 'blitz_refresh_used' already exists.")

        if "trial_super_used" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN trial_super_used BOOLEAN DEFAULT 0")
            logging.info(" -> The column 'trial_super_used' is successfully added.")
        else:
            logging.info(" -> The column 'trial_super_used' already exists.")

        if "has_active_beta" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN has_active_beta INTEGER DEFAULT 0")
            logging.info(" -> The column 'has_active_beta' is successfully added.")
        else:
            logging.info(" -> The column 'has_active_beta' already exists.")

        logging.info("The table 'users' has been successfully updated.")

        logging.info("The migration of the table 'vpn_keys' ...")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='vpn_keys'")
        vpn_keys_exists = cursor.fetchone()

        if vpn_keys_exists:
            cursor.execute("PRAGMA table_info(vpn_keys)")
            vpn_key_columns = [row[1] for row in cursor.fetchall()]

            if "issued_key_value" not in vpn_key_columns:
                cursor.execute("ALTER TABLE vpn_keys ADD COLUMN issued_key_value TEXT")
                logging.info(" -> The column 'issued_key_value' is successfully added.")
            else:
                logging.info(" -> The column 'issued_key_value' already exists.")

            if "subscription_payload" not in vpn_key_columns:
                cursor.execute("ALTER TABLE vpn_keys ADD COLUMN subscription_payload TEXT")
                logger.info(" -> The column 'subscription_payload' is successfully added.")
            else:
                logger.info(" -> The column 'subscription_payload' already exists.")

            if "expiry_date" not in vpn_key_columns:
                cursor.execute("ALTER TABLE vpn_keys ADD COLUMN expiry_date TIMESTAMP")
                logging.info(" -> The column 'expiry_date' is successfully added.")
            else:
                logging.info(" -> The column 'expiry_date' already exists.")

            if "panel_sub_id" not in vpn_key_columns:
                cursor.execute("ALTER TABLE vpn_keys ADD COLUMN panel_sub_id TEXT")
                logger.info(" -> The column 'panel_sub_id' is successfully added.")
            else:
                logger.info(" -> The column 'panel_sub_id' already exists.")

            if "subscription_tier" not in vpn_key_columns:
                cursor.execute("ALTER TABLE vpn_keys ADD COLUMN subscription_tier TEXT")
                logger.info(" -> The column 'subscription_tier' is successfully added.")
            else:
                logger.info(" -> The column 'subscription_tier' already exists.")

            if "whitelist_cycle_anchor_at" not in vpn_key_columns:
                cursor.execute("ALTER TABLE vpn_keys ADD COLUMN whitelist_cycle_anchor_at TIMESTAMP")
                logger.info(" -> The column 'whitelist_cycle_anchor_at' is successfully added.")
            else:
                logger.info(" -> The column 'whitelist_cycle_anchor_at' already exists.")

            if "whitelist_traffic_unlimited_override" not in vpn_key_columns:
                cursor.execute(
                    "ALTER TABLE vpn_keys ADD COLUMN whitelist_traffic_unlimited_override "
                    "INTEGER NOT NULL DEFAULT 0"
                )
                logger.info(" -> The column 'whitelist_traffic_unlimited_override' is successfully added.")
            else:
                logger.info(" -> The column 'whitelist_traffic_unlimited_override' already exists.")
        else:
            logging.info(" -> The table 'vpn_keys' does not exist yet. It will be created on initialization.")

        logging.info("The table 'vpn_keys' has been successfully updated.")

        logging.info("The migration of the table 'xui_hosts' ...")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='xui_hosts'")
        xui_hosts_exists = cursor.fetchone()

        if xui_hosts_exists:
            cursor.execute("PRAGMA table_info(xui_hosts)")
            host_columns = [row[1] for row in cursor.fetchall()]

            if "host_subscription_base_url" not in host_columns:
                cursor.execute("ALTER TABLE xui_hosts ADD COLUMN host_subscription_base_url TEXT")
                logging.info(" -> The column 'host_subscription_base_url' is successfully added.")
            else:
                logging.info(" -> The column 'host_subscription_base_url' already exists.")

            if "host_api_token_env" not in host_columns:
                cursor.execute("ALTER TABLE xui_hosts ADD COLUMN host_api_token_env TEXT")
                logger.info(" -> The column 'host_api_token_env' is successfully added.")
            else:
                logger.info(" -> The column 'host_api_token_env' already exists.")

            for col_name, col_def in [
                ("status", "TEXT DEFAULT 'unknown'"),
                ("last_check_at", "TIMESTAMP"),
                ("last_success_at", "TIMESTAMP"),
                ("last_error", "TEXT"),
                ("is_maintenance", "INTEGER DEFAULT 0"),
                ("is_white_host", "INTEGER DEFAULT 0"),
                ("node_id", "INTEGER"),
                ("node_inbound_id", "INTEGER"),
                ("node_sync_status", "TEXT DEFAULT 'pending'"),
                ("node_sync_error", "TEXT"),
                ("node_synced_at", "TIMESTAMP"),
                ("delivery_state", "TEXT DEFAULT 'active'"),
                ("topology_role", "TEXT"),
                ("topology_state", "TEXT"),
                ("topology_operation_id", "TEXT"),
                ("topology_last_error", "TEXT"),
                ("topology_updated_at", "TIMESTAMP"),
                ("topology_activated_at", "TIMESTAMP"),
            ]:
                if col_name not in host_columns:
                    cursor.execute(f"ALTER TABLE xui_hosts ADD COLUMN {col_name} {col_def}")
                    logging.info(f" -> The column '{col_name}' is successfully added to xui_hosts.")
        else:
            logging.info(" -> The table 'xui_hosts' does not exist yet. It will be created on initialization.")

        logging.info("The table 'xui_hosts' has been successfully updated.")

        cursor.execute(
            """
            UPDATE xui_hosts
            SET topology_role = CASE
                    WHEN COALESCE(is_white_host, 0) = 1 THEN 'whitelist'
                    ELSE 'ordinary'
                END
            WHERE topology_role IS NULL
               OR TRIM(topology_role) = ''
               OR topology_role NOT IN ('ordinary', 'whitelist')
            """
        )
        cursor.execute(
            """
            UPDATE xui_hosts
            SET topology_state = CASE LOWER(COALESCE(delivery_state, 'active'))
                    WHEN 'draft' THEN 'registered'
                    WHEN 'disabled' THEN 'disabled'
                    ELSE 'delivery_active'
                END,
                topology_updated_at = COALESCE(topology_updated_at, CURRENT_TIMESTAMP),
                topology_activated_at = CASE
                    WHEN LOWER(COALESCE(delivery_state, 'active')) = 'active'
                    THEN COALESCE(topology_activated_at, CURRENT_TIMESTAMP)
                    ELSE topology_activated_at
                END
            WHERE topology_state IS NULL
               OR TRIM(topology_state) = ''
               OR topology_state NOT IN (
                    'registered', 'provisioning', 'backfilling', 'verifying',
                    'delivery_active', 'failed', 'draining', 'disabled'
               )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS host_topology_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            INSERT OR IGNORE INTO host_topology_state (id, version)
            VALUES (1, 1)
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS host_topology_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_name TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state TEXT NOT NULL,
                topology_role TEXT NOT NULL,
                operation_id TEXT,
                actor TEXT NOT NULL,
                reason TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_host_topology_events_host_created
            ON host_topology_events(host_name, created_at DESC)
            """
        )

        # --- bot_source column for reserve bot support ---
        logging.info("The migration: adding 'bot_source' to transactions and vpn_keys ...")

        cursor.execute("PRAGMA table_info(transactions)")
        tx_cols = [row[1] for row in cursor.fetchall()]
        if "bot_source" not in tx_cols:
            cursor.execute("ALTER TABLE transactions ADD COLUMN bot_source TEXT DEFAULT 'main'")
            logging.info(" -> The column 'bot_source' is successfully added to transactions.")
        else:
            logging.info(" -> The column 'bot_source' already exists in transactions.")

        issuance_cols: list[tuple[str, str]] = [
            ("issuance_state", f"TEXT DEFAULT '{ISSUANCE_STATE_AWAITING_PAYMENT}'"),
            ("retry_count", "INTEGER DEFAULT 0"),
            ("next_retry_at", "TIMESTAMP"),
            ("confirmed_at", "TIMESTAMP"),
            ("last_attempt_at", "TIMESTAMP"),
            ("last_error", "TEXT"),
        ]
        for col_name, col_def in issuance_cols:
            if col_name not in tx_cols:
                cursor.execute(f"ALTER TABLE transactions ADD COLUMN {col_name} {col_def}")
                logging.info(" -> The column '%s' is successfully added to transactions.", col_name)

        # Backfill issuance lifecycle for existing rows (idempotent and safe for reruns).
        cursor.execute(
            """
            UPDATE transactions
            SET issuance_state = CASE
                WHEN status = 'paid' THEN ?
                WHEN status = 'processing' THEN ?
                WHEN status = 'canceled' THEN ?
                WHEN COALESCE(json_extract(metadata, '$.payment_confirmed'), 0) = 1 THEN ?
                ELSE ?
            END
            WHERE issuance_state IS NULL
               OR TRIM(COALESCE(issuance_state, '')) = ''
               OR issuance_state NOT IN (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ISSUANCE_STATE_ISSUED,
                ISSUANCE_STATE_ISSUING,
                ISSUANCE_STATE_CANCELED,
                ISSUANCE_STATE_CONFIRMED,
                ISSUANCE_STATE_AWAITING_PAYMENT,
                ISSUANCE_STATE_AWAITING_PAYMENT,
                ISSUANCE_STATE_CONFIRMED,
                ISSUANCE_STATE_ISSUING,
                ISSUANCE_STATE_ISSUED,
                ISSUANCE_STATE_RETRYABLE_FAILED,
                ISSUANCE_STATE_FATAL_FAILED,
                ISSUANCE_STATE_CANCELED,
            ),
        )
        cursor.execute(
            """
            UPDATE transactions
            SET retry_count = COALESCE(retry_count, 0)
            WHERE retry_count IS NULL
            """
        )

        cursor.execute("PRAGMA table_info(vpn_keys)")
        vk_cols = [row[1] for row in cursor.fetchall()]
        if "bot_source" not in vk_cols:
            cursor.execute("ALTER TABLE vpn_keys ADD COLUMN bot_source TEXT DEFAULT 'main'")
            logging.info(" -> The column 'bot_source' is successfully added to vpn_keys.")
        else:
            logging.info(" -> The column 'bot_source' already exists in vpn_keys.")
        if "subscription_tier" not in vk_cols:
            cursor.execute("ALTER TABLE vpn_keys ADD COLUMN subscription_tier TEXT")
            logger.info(" -> The column 'subscription_tier' is successfully added to vpn_keys.")

        # --- vpn_keys: migration metadata columns ---
        logging.info("The migration of the table 'vpn_keys' (resilience columns) ...")
        cursor.execute("PRAGMA table_info(vpn_keys)")
        vpn_cols = [row[1] for row in cursor.fetchall()]
        for col_name, col_def in [
            ("migrated_from_key_id", "INTEGER"),
            ("gift_days", "INTEGER DEFAULT 0"),
            ("migration_incident_id", "TEXT"),
            ("is_archived", "INTEGER DEFAULT 0"),
            ("migration_reason", "TEXT"),
            ("replaced_due_to_incident", "INTEGER DEFAULT 0"),
        ]:
            if col_name not in vpn_cols:
                cursor.execute(f"ALTER TABLE vpn_keys ADD COLUMN {col_name} {col_def}")
                logging.info(f" -> The column '{col_name}' is successfully added to vpn_keys.")

        # --- New tables for incident management ---
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS incident_migrations (
                migration_id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                old_key_id INTEGER NOT NULL,
                new_key_id INTEGER,
                old_host TEXT NOT NULL,
                new_host TEXT NOT NULL,
                gift_days INTEGER DEFAULT 0,
                remaining_days_at_migration REAL,
                status TEXT DEFAULT 'pending',
                error_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_incidents_seen (
                user_id INTEGER NOT NULL,
                incident_id TEXT NOT NULL,
                seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                opened_new_key_at TIMESTAMP,
                PRIMARY KEY (user_id, incident_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS key_expiry_notifications (
                key_id INTEGER NOT NULL,
                expiry_date TEXT NOT NULL,
                notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (key_id, expiry_date)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS whitelist_traffic_status (
                key_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                key_email TEXT NOT NULL,
                host_name TEXT NOT NULL,
                bot_source TEXT DEFAULT 'main',
                used_bytes INTEGER DEFAULT 0,
                total_bytes INTEGER DEFAULT 0,
                reset_days INTEGER DEFAULT 0,
                reset_timestamp_ms INTEGER DEFAULT 0,
                panel_enable INTEGER DEFAULT 1,
                exhausted INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS whitelist_limit_notifications (
                key_id INTEGER NOT NULL,
                reset_timestamp_ms INTEGER NOT NULL,
                notified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (key_id, reset_timestamp_ms)
            )
        """)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_incidents_seen'")
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(user_incidents_seen)")
            uis_columns = [row[1] for row in cursor.fetchall()]
            if "opened_new_key_at" not in uis_columns:
                cursor.execute("ALTER TABLE user_incidents_seen ADD COLUMN opened_new_key_at TIMESTAMP")
                logging.info("Added column 'opened_new_key_at' to user_incidents_seen.")
        logging.info("Incident management tables created/verified.")
        _ensure_mainpanel_inbound_sync_tables(cursor)

        logging.info("The migration of the table 'Transactions' ...")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'")
        table_exists = cursor.fetchone()

        if table_exists:
            cursor.execute("PRAGMA table_info(transactions)")
            trans_columns = [row[1] for row in cursor.fetchall()]

            if "payment_id" in trans_columns and "status" in trans_columns and "username" in trans_columns:
                logging.info("The 'Transactions' table already has a new structure. Migration is not required.")
            else:
                backup_name = f"transactions_backup_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                logger.warning(
                    "The old structure of the TRANSACTIONS table was discovered. I rename in '%s' ...",
                    backup_name,
                )
                cursor.execute(f"ALTER TABLE transactions RENAME TO {backup_name}")

                logging.info("I create a new table 'Transactions' with the correct structure ...")
                create_new_transactions_table(cursor)
                logging.info("The new table 'Transactions' has been successfully created. The old data is saved.")
        else:
            logging.info("TRANSACTIONS table was not found. I create a new one ...")
            create_new_transactions_table(cursor)
            logging.info("The new table 'Transactions' has been successfully created.")

        _ensure_indexes(cursor)
        conn.commit()

        logging.info("--- The database is successfully completed! ---")

    except sqlite3.Error as e:
        logging.error(f"An error occurred during migration: {e}")
    finally:
        if conn:
            conn.close()


def create_new_transactions_table(cursor: sqlite3.Cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            username TEXT,
            transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            amount_rub REAL NOT NULL,
            amount_currency REAL,
            currency_name TEXT,
            payment_method TEXT,
            metadata TEXT,
            created_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            bot_source TEXT DEFAULT 'main',
            issuance_state TEXT DEFAULT 'awaiting_payment',
            retry_count INTEGER DEFAULT 0,
            next_retry_at TIMESTAMP,
            confirmed_at TIMESTAMP,
            last_attempt_at TIMESTAMP,
            last_error TEXT
        )
    """)


def create_host(
    name: str,
    url: str,
    user: str,
    passwd: str,
    inbound: int,
    subscription_base_url: str | None = None,
    api_token_env: str | None = None,
    is_white_host: bool = False,
    delivery_state: str = "active",
):
    normalized_delivery_state = normalize_host_delivery_state(delivery_state, default="draft")
    topology_state = normalize_host_topology_state(
        None,
        delivery_state=normalized_delivery_state,
    )
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO xui_hosts
                (
                    host_name, host_url, host_username, host_pass,
                    host_inbound_id, host_subscription_base_url, host_api_token_env,
                    is_white_host, node_sync_status, node_sync_error, delivery_state,
                    topology_role, topology_state, topology_updated_at, topology_activated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP,
                    CASE WHEN ? = 'delivery_active' THEN CURRENT_TIMESTAMP ELSE NULL END)
                """,
                (
                    name,
                    url,
                    user,
                    passwd,
                    inbound,
                    subscription_base_url or None,
                    api_token_env or None,
                    1 if is_white_host else 0,
                    "pending",
                    None,
                    normalized_delivery_state,
                    "whitelist" if is_white_host else "ordinary",
                    topology_state,
                    topology_state,
                ),
            )
            conn.commit()
            logging.info(f"Successfully created a new host: {name}")
    except sqlite3.Error as e:
        logging.error(f"Error creating host '{name}': {e}")


def delete_host(host_name: str):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM plans WHERE host_name = ?", (host_name,))
            cursor.execute("DELETE FROM xui_hosts WHERE host_name = ?", (host_name,))
            conn.commit()
            logging.info(f"Successfully deleted host '{host_name}' and its plans.")
    except sqlite3.Error as e:
        logging.error(f"Error deleting host '{host_name}': {e}")


def update_host_api_token_ref(host_name: str, api_token_ref: str | None) -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE xui_hosts SET host_api_token_env = ? WHERE host_name = ?",
                ((api_token_ref or None), host_name),
            )
            conn.commit()
            updated = cursor.rowcount > 0
            if updated:
                logger.info("Successfully updated XUI API token reference for host: %s", host_name)
            return updated
    except sqlite3.Error:
        logger.exception("Error updating XUI API token reference for host '%s'", host_name)
        return False


def update_host_urls(
    host_name: str,
    host_url: str,
    subscription_base_url: str | None,
) -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE xui_hosts
                SET host_url = ?, host_subscription_base_url = ?
                WHERE host_name = ?
                """,
                (host_url, subscription_base_url or None, host_name),
            )
            conn.commit()
            updated = cursor.rowcount > 0
            if updated:
                logger.info("Successfully updated XUI URLs for host: %s", host_name)
            return updated
    except sqlite3.Error:
        logger.exception("Error updating XUI URLs for host '%s'", host_name)
        return False


def update_host_config(
    host_name: str,
    *,
    host_url: str,
    host_username: str,
    host_pass: str,
    host_inbound_id: int,
    subscription_base_url: str | None,
    is_white_host: bool,
    delivery_state: str = "active",
) -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE xui_hosts
                SET
                    host_url = ?,
                    host_username = ?,
                    host_pass = ?,
                    host_inbound_id = ?,
                    host_subscription_base_url = ?,
                    is_white_host = ?,
                    delivery_state = ?
                WHERE host_name = ?
                """,
                (
                    host_url,
                    host_username,
                    host_pass,
                    int(host_inbound_id),
                    subscription_base_url or None,
                    1 if is_white_host else 0,
                    normalize_host_delivery_state(delivery_state),
                    host_name,
                ),
            )
            conn.commit()
            updated = cursor.rowcount > 0
            if updated:
                logger.info("Successfully updated editable XUI host config: %s", host_name)
            return updated
    except sqlite3.Error:
        logger.exception("Error updating editable XUI host config for '%s'", host_name)
        return False


def update_host_inbound_id(host_name: str, host_inbound_id: int) -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE xui_hosts
                SET host_inbound_id = ?
                WHERE host_name = ?
                """,
                (int(host_inbound_id), host_name),
            )
            conn.commit()
            return cursor.rowcount > 0
    except (sqlite3.Error, TypeError, ValueError):
        logger.exception("Error updating inbound ID for XUI host '%s'", host_name)
        return False


def set_host_delivery_state(host_name: str, delivery_state: str) -> bool:
    normalized_state = normalize_host_delivery_state(delivery_state, default="draft")
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE xui_hosts
                SET delivery_state = ?
                WHERE host_name = ?
                """,
                (normalized_state, host_name),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error:
        logger.exception("Error updating delivery state for XUI host '%s'", host_name)
        return False


def get_host_topology_snapshot(subscription_tier: str | None = None) -> dict:
    """Return the delivery-safe topology view without changing legacy routing."""
    normalized_tier = str(subscription_tier or "standard").strip().lower()
    allowed_roles = (
        ("ordinary", "whitelist")
        if normalized_tier in {"standard", "super"}
        else ("ordinary",)
    )
    placeholders = ", ".join("?" for _ in allowed_roles)

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT version, updated_at FROM host_topology_state WHERE id = 1")
            topology_row = cursor.fetchone()
            cursor.execute(
                f"""
                SELECT *
                FROM xui_hosts
                WHERE delivery_state = 'active'
                  AND topology_state = 'delivery_active'
                  AND topology_role IN ({placeholders})
                ORDER BY host_name
                """,
                allowed_roles,
            )
            return {
                "version": int(topology_row["version"]) if topology_row else 0,
                "updated_at": topology_row["updated_at"] if topology_row else None,
                "hosts": [_with_effective_host_status(dict(row)) for row in cursor.fetchall()],
            }
    except sqlite3.Error:
        logger.exception("Failed to read host topology snapshot")
        return {"version": 0, "updated_at": None, "hosts": []}


def transition_host_topology_state(
    host_name: str,
    target_state: str,
    *,
    actor: str,
    operation_id: str | None = None,
    reason: str | None = None,
    error: str | None = None,
    expected_state: str | None = None,
) -> dict | None:
    """Atomically transition one host, append an audit event, and bump topology version."""
    normalized_target = str(target_state or "").strip().lower()
    if normalized_target not in HOST_TOPOLOGY_STATES:
        raise ValueError(f"Unsupported topology state: {target_state}")  # noqa: TRY003
    normalized_actor = str(actor or "").strip()
    if not normalized_actor:
        raise ValueError("Topology transition actor is required")  # noqa: TRY003
    normalized_expected = None
    if expected_state is not None:
        normalized_expected = str(expected_state).strip().lower()
        if normalized_expected not in HOST_TOPOLOGY_STATES:
            raise ValueError(f"Unsupported expected topology state: {expected_state}")  # noqa: TRY003

    safe_operation_id = str(operation_id or uuid.uuid4()).strip()[:128]
    safe_actor = normalized_actor[:128]
    safe_reason = str(reason).strip()[:1000] if reason else None
    safe_error = str(error).strip()[:1000] if error else None

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("SELECT * FROM xui_hosts WHERE host_name = ?", (host_name,))
            row = cursor.fetchone()
            if row is None:
                conn.rollback()
                return None

            host = _with_effective_host_status(dict(row))
            current_state = host["topology_state"]
            if normalized_expected is not None and current_state != normalized_expected:
                conn.rollback()
                raise RuntimeError(
                    f"Stale topology state for {host_name}: expected {normalized_expected}, got {current_state}"
                )
            if normalized_target not in HOST_TOPOLOGY_TRANSITIONS[current_state]:
                conn.rollback()
                raise ValueError(
                    f"Unsupported topology transition for {host_name}: {current_state} -> {normalized_target}"
                )

            cursor.execute(
                """
                UPDATE xui_hosts
                SET topology_state = ?,
                    topology_operation_id = ?,
                    topology_last_error = ?,
                    topology_updated_at = CURRENT_TIMESTAMP,
                    topology_activated_at = CASE
                        WHEN ? = 'delivery_active' THEN CURRENT_TIMESTAMP
                        ELSE topology_activated_at
                    END
                WHERE host_name = ? AND topology_state = ?
                """,
                (
                    normalized_target,
                    safe_operation_id,
                    safe_error if normalized_target == "failed" else None,
                    normalized_target,
                    host_name,
                    current_state,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                raise RuntimeError(f"Concurrent topology transition detected for {host_name}")  # noqa: TRY003

            cursor.execute(
                """
                INSERT INTO host_topology_events
                    (host_name, from_state, to_state, topology_role, operation_id, actor, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    host_name,
                    current_state,
                    normalized_target,
                    host["topology_role"],
                    safe_operation_id,
                    safe_actor,
                    safe_reason,
                ),
            )
            cursor.execute(
                """
                UPDATE host_topology_state
                SET version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE id = 1
                """
            )
            cursor.execute("SELECT * FROM xui_hosts WHERE host_name = ?", (host_name,))
            updated = cursor.fetchone()
            conn.commit()
            return _with_effective_host_status(dict(updated)) if updated else None
    except sqlite3.Error:
        logger.exception("Failed topology transition for host '%s'", host_name)
        return None


def get_host(host_name: str) -> dict | None:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM xui_hosts WHERE host_name = ?", (host_name,))
            result = cursor.fetchone()
            return _with_effective_host_status(dict(result)) if result else None
    except sqlite3.Error as e:
        logging.error(f"Error getting host '{host_name}': {e}")
        return None


def get_all_hosts() -> list[dict]:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM xui_hosts")
            hosts = cursor.fetchall()
            return [_with_effective_host_status(dict(row)) for row in hosts]
    except sqlite3.Error as e:
        logging.error(f"Error getting list of all hosts: {e}")
        return []


def get_hosts_with_plans() -> list[dict]:
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                h.host_name,
                h.host_url,
                h.host_username,
                h.host_pass,
                h.host_inbound_id,
                h.host_subscription_base_url,
                h.host_api_token_env,
                h.is_white_host,
                h.node_id,
                h.node_inbound_id,
                h.node_sync_status,
                h.node_sync_error,
                h.node_synced_at,
                h.delivery_state,
                h.topology_role,
                h.topology_state,
                h.topology_operation_id,
                h.topology_last_error,
                h.topology_updated_at,
                h.topology_activated_at,
                h.status,
                h.last_check_at,
                h.last_success_at,
                h.last_error,
                h.is_maintenance,
                p.plan_id,
                p.plan_name,
                p.months,
                p.price
            FROM xui_hosts h
            LEFT JOIN plans p ON p.host_name = h.host_name
            ORDER BY h.host_name, p.months
            """
        )
        hosts: dict[str, dict] = {}
        for row in cursor.fetchall():
            host_name = row["host_name"]
            host = hosts.get(host_name)
            if host is None:
                effective_status, is_status_stale = _resolve_effective_host_status(
                    status=row["status"],
                    last_check_at=row["last_check_at"],
                    is_maintenance=bool(row["is_maintenance"]),
                )
                last_error = row["last_error"]
                if is_status_stale and not str(last_error or "").strip():
                    last_error = f"Нет свежей проверки хоста более {HOST_STATUS_STALE_SECONDS} сек."
                host = {
                    "host_name": row["host_name"],
                    "host_url": row["host_url"],
                    "host_username": row["host_username"],
                    "host_pass": row["host_pass"],
                    "host_inbound_id": row["host_inbound_id"],
                    "host_subscription_base_url": row["host_subscription_base_url"],
                    "host_api_token_env": row["host_api_token_env"],
                    "is_white_host": int(row["is_white_host"] or 0),
                    "node_id": row["node_id"],
                    "node_inbound_id": row["node_inbound_id"],
                    "node_sync_status": row["node_sync_status"],
                    "node_sync_error": row["node_sync_error"],
                    "node_synced_at": row["node_synced_at"],
                    "delivery_state": normalize_host_delivery_state(row["delivery_state"]),
                    "topology_role": row["topology_role"],
                    "topology_state": row["topology_state"],
                    "topology_operation_id": row["topology_operation_id"],
                    "topology_last_error": row["topology_last_error"],
                    "topology_updated_at": row["topology_updated_at"],
                    "topology_activated_at": row["topology_activated_at"],
                    "raw_status": row["status"],
                    "status": effective_status,
                    "is_status_stale": is_status_stale,
                    "last_check_at": row["last_check_at"],
                    "last_success_at": row["last_success_at"],
                    "last_error": last_error,
                    "is_maintenance": row["is_maintenance"],
                    "plans": [],
                }
                hosts[host_name] = host

            if row["plan_id"] is not None:
                host["plans"].append(
                    {
                        "plan_id": row["plan_id"],
                        "host_name": host_name,
                        "plan_name": row["plan_name"],
                        "months": row["months"],
                        "price": row["price"],
                    }
                )

        return [_with_effective_host_status(host) for host in hosts.values()]
    except sqlite3.Error as e:
        logging.error("Error getting hosts with plans: %s", e)
        return []


def get_active_keys_count_by_host(host_name: str) -> int:
    """
    Count active (not expired) keys for a specific host.
    Used for load balancing between servers.

    Args:
        host_name: Name of the host to count keys for

    Returns:
        Number of active keys, or 999999 on error (to avoid selecting failed host)
    """
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT COUNT(*) FROM vpn_keys
                WHERE host_name = ? AND COALESCE(is_archived, 0) = 0 AND expiry_date > datetime('now')
            """,
                (host_name,),
            )
            return cursor.fetchone()[0]
    except sqlite3.Error as e:
        logging.error(f"Failed to count keys for host {host_name}: {e}")
        # Return high number on error to avoid selecting this host
        return 999999


def _host_is_white_record(host: dict | None) -> bool:
    try:
        return bool(int((host or {}).get("is_white_host") or 0))
    except (TypeError, ValueError):
        return False


def get_eligible_standard_hosts(
    *,
    exclude_host_names: set[str] | None = None,
    require_plans: bool = False,
) -> list[dict]:
    """
    Return healthy X-UI hosts that are valid for standard purchases.

    Standard purchases must stay isolated from Super and Blitz/Hysteria flows.
    This helper is the single business-rule gate reused by UI selection and
    post-payment fallback issuance.
    """
    hosts = get_all_hosts()
    if not hosts:
        return []

    excluded = {str(name or "").strip() for name in (exclude_host_names or set()) if str(name or "").strip()}
    super_key_host_name = (get_setting("super_key_host_name") or "").strip()
    if super_key_host_name:
        excluded.add(super_key_host_name)

    eligible_hosts: list[dict] = []
    for host in hosts:
        host_name = str(host.get("host_name") or "").strip()
        if not host_name or host_name in excluded:
            continue
        if not is_host_delivery_active(host):
            continue
        if host.get("is_maintenance"):
            continue
        if _host_is_white_record(host):
            logger.info(
                "Skipping host '%s' because white hosts are Super-only.",
                host_name,
            )
            continue
        if str(host.get("status", "")).lower() in {"unavailable", "maintenance"}:
            continue
        if classify_product_type(host_name) != TX_TYPE_VLESS:
            logging.info(
                "Skipping host '%s' because it is not eligible for standard purchase flow.",
                host_name,
            )
            continue
        if require_plans and not get_plans_for_host(host_name):
            logging.info(
                "Skipping host '%s' because it has no standard plans configured.",
                host_name,
            )
            continue
        eligible_hosts.append(host)

    return eligible_hosts


def select_least_loaded_host(exclude_host_names: set[str] | None = None) -> dict | None:
    """
    Select host with minimum total clients (least loaded algorithm).
    Uses in-memory host health/load cache, refreshed by background monitor.
    Falls back to local active keys count when cache is stale/missing.

    Super-key host is excluded by default from regular load balancing to avoid
    mixing premium tariffs into standard purchase flow.
    Hosts in maintenance or with status 'unavailable' are also excluded.

    Args:
        exclude_host_names: Optional set of host names to force-exclude from selection.

    Returns:
        Dict with host data, or None if no hosts available
    """
    try:
        from shop_bot.modules.xui_api import (
            get_cached_host_clients_count,
            get_host_health_snapshot,
        )

        hosts = get_all_hosts()
        if not hosts:
            return None

        excluded = {name for name in (exclude_host_names or set()) if name}
        super_key_host_name = (get_setting("super_key_host_name") or "").strip()
        if super_key_host_name:
            excluded.add(super_key_host_name)

        filtered = []
        for host in hosts:
            name = host.get("host_name")
            name_normalized = str(name or "").strip()
            if name in excluded:
                continue
            if not is_host_delivery_active(host):
                continue
            if host.get("is_maintenance"):
                continue
            if _host_is_white_record(host):
                logger.info(
                    "Skipping host '%s' in select_least_loaded_host because white hosts are Super-only.",
                    name,
                )
                continue
            if str(host.get("status", "")).lower() in {"unavailable", "maintenance"}:
                continue
            if classify_product_type(name_normalized) != TX_TYPE_VLESS:
                logging.info(
                    "Skipping host '%s' in select_least_loaded_host because it is not a standard purchase host.",
                    name,
                )
                continue
            if not get_plans_for_host(name_normalized):
                logging.info(
                    "Skipping host '%s' in select_least_loaded_host because it has no plans.",
                    name,
                )
                continue
            filtered.append(host)

        if excluded:
            logging.info("Load balancing excluded hosts: %s", ", ".join(sorted(excluded)))

        if not filtered:
            logging.warning("No eligible hosts remain after exclusions in select_least_loaded_host.")
            return None

        host_loads = []
        for host in filtered:
            host_name = str(host.get("host_name") or "").strip()
            snapshot = get_host_health_snapshot(host_name)
            if snapshot and snapshot.status in {"unavailable", "maintenance"}:
                logging.info(
                    "Skipping host '%s' in select_least_loaded_host due cached status='%s'.",
                    host_name,
                    snapshot.status,
                )
                continue

            cached_count = get_cached_host_clients_count(host_name)
            if cached_count is not None:
                total_count = cached_count
                source = "cache"
            else:
                total_count = get_active_keys_count_by_host(host_name)
                source = "db_fallback"
            host_loads.append((host, total_count))
            logging.info("Host '%s': load=%s (source=%s)", host_name, total_count, source)

        if not host_loads:
            logging.warning("No hosts with usable cached health/load data. Returning None.")
            return None

        host_loads.sort(key=lambda x: x[1])

        selected_host = host_loads[0][0]
        logging.info(f"Selected host '{selected_host['host_name']}' with {host_loads[0][1]} clients (least loaded)")
        return selected_host

    except Exception as e:
        logging.error(f"Failed to select least loaded host: {e}")
        return None


def get_all_keys(bot_source: str = BOT_SOURCE_ALL) -> list[dict]:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            normalized_source = _normalize_bot_source_filter(bot_source)
            if normalized_source == BOT_SOURCE_ALL:
                cursor.execute("SELECT * FROM vpn_keys")
            else:
                cursor.execute(
                    "SELECT * FROM vpn_keys WHERE COALESCE(bot_source, 'main') = ?",
                    (normalized_source,),
                )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Failed to get all keys: {e}")
        return []


def get_active_whitelist_limit_keys(
    *,
    host_name: str | None = None,
    bot_source: str = BOT_SOURCE_ALL,
) -> list[dict]:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            normalized_source = _normalize_bot_source_filter(bot_source)
            where_parts = [
                "COALESCE(is_archived, 0) = 0",
                "expiry_date > ?",
                """
                (
                    LOWER(TRIM(COALESCE(subscription_tier, ''))) IN ('standard', 'super', '')
                    OR LOWER(COALESCE(key_email, '')) LIKE '%whitelist%'
                )
                """,
            ]
            params: list[object] = [datetime.now().isoformat()]
            if host_name:
                where_parts.append("host_name = ?")
                params.append(str(host_name).strip())
            if normalized_source != BOT_SOURCE_ALL:
                where_parts.append("COALESCE(bot_source, 'main') = ?")
                params.append(normalized_source)
            cursor.execute(
                f"""
                SELECT *
                FROM vpn_keys
                WHERE {' AND '.join(where_parts)}
                ORDER BY key_id
                """,
                tuple(params),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error("Failed to get active WHITELIST limit keys: %s", e)
        return []


def upsert_whitelist_traffic_status(rows: list[dict]) -> int:
    if not rows:
        return 0
    payload: list[tuple] = []
    for row in rows:
        try:
            key_id = int(row.get("key_id") or 0)
            user_id = int(row.get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        key_email = str(row.get("key_email") or "").strip()
        host_name = str(row.get("host_name") or "").strip()
        if key_id <= 0 or user_id <= 0 or not key_email or not host_name:
            continue
        payload.append(
            (
                key_id,
                user_id,
                key_email,
                host_name,
                _resolve_bot_source(str(row.get("bot_source") or "")),
                max(0, int(row.get("used_bytes") or 0)),
                max(0, int(row.get("total_bytes") or 0)),
                max(0, int(row.get("reset_days") or 0)),
                max(0, int(row.get("reset_timestamp_ms") or 0)),
                1 if bool(row.get("panel_enable", True)) else 0,
                1 if bool(row.get("exhausted", False)) else 0,
            )
        )
    if not payload:
        return 0
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                """
                INSERT INTO whitelist_traffic_status (
                    key_id, user_id, key_email, host_name, bot_source,
                    used_bytes, total_bytes, reset_days, reset_timestamp_ms,
                    panel_enable, exhausted, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    key_email = excluded.key_email,
                    host_name = excluded.host_name,
                    bot_source = excluded.bot_source,
                    used_bytes = excluded.used_bytes,
                    total_bytes = excluded.total_bytes,
                    reset_days = excluded.reset_days,
                    reset_timestamp_ms = excluded.reset_timestamp_ms,
                    panel_enable = excluded.panel_enable,
                    exhausted = excluded.exhausted,
                    updated_at = CURRENT_TIMESTAMP
                """,
                payload,
            )
            conn.commit()
            return len(payload)
    except sqlite3.Error as e:
        logging.error("Failed to upsert WHITELIST traffic status: %s", e)
        return 0


def get_user_whitelist_traffic_status(
    user_id: int,
    *,
    bot_source: str = BOT_SOURCE_ALL,
) -> dict | None:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            normalized_source = _normalize_bot_source_filter(bot_source)
            where_parts = [
                "w.user_id = ?",
                "COALESCE(v.is_archived, 0) = 0",
                "v.expiry_date > ?",
                "w.total_bytes > 0",
            ]
            params: list[object] = [int(user_id), datetime.now().isoformat()]
            if normalized_source != BOT_SOURCE_ALL:
                where_parts.append("COALESCE(w.bot_source, 'main') = ?")
                params.append(normalized_source)
            cursor.execute(
                f"""
                SELECT w.*
                FROM whitelist_traffic_status w
                JOIN vpn_keys v ON v.key_id = w.key_id
                WHERE {' AND '.join(where_parts)}
                ORDER BY w.exhausted DESC, w.updated_at DESC, w.reset_timestamp_ms ASC
                LIMIT 1
                """,
                tuple(params),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error("Failed to get user WHITELIST traffic status: %s", e)
        return None


def get_user_whitelist_traffic_statuses(
    user_id: int,
    *,
    bot_source: str = BOT_SOURCE_ALL,
) -> list[dict]:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            normalized_source = _normalize_bot_source_filter(bot_source)
            where_parts = [
                "w.user_id = ?",
                "COALESCE(v.is_archived, 0) = 0",
                "v.expiry_date > ?",
            ]
            params: list[object] = [int(user_id), datetime.now().isoformat()]
            if normalized_source != BOT_SOURCE_ALL:
                where_parts.append("COALESCE(w.bot_source, 'main') = ?")
                params.append(normalized_source)
            cursor.execute(
                f"""
                SELECT
                    w.*,
                    v.panel_sub_id,
                    v.issued_key_value,
                    v.subscription_tier,
                    v.expiry_date
                FROM whitelist_traffic_status w
                JOIN vpn_keys v ON v.key_id = w.key_id
                WHERE {' AND '.join(where_parts)}
                ORDER BY v.expiry_date DESC, w.key_id DESC
                """,
                tuple(params),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error("Failed to get user WHITELIST traffic statuses: %s", e)
        return []


def get_whitelist_traffic_statuses_by_key_ids(key_ids: list[int]) -> dict[int, dict]:
    normalized_ids: list[int] = []
    for key_id in key_ids:
        try:
            value = int(key_id)
        except (TypeError, ValueError):
            continue
        if value > 0:
            normalized_ids.append(value)
    if not normalized_ids:
        return {}
    placeholders = ",".join("?" for _ in normalized_ids)
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT * FROM whitelist_traffic_status WHERE key_id IN ({placeholders})",
                tuple(normalized_ids),
            )
            return {int(row["key_id"]): dict(row) for row in cursor.fetchall()}
    except sqlite3.Error as e:
        logging.error("Failed to get WHITELIST traffic statuses by key ids: %s", e)
        return {}


def has_whitelist_limit_notification(key_id: int, reset_timestamp_ms: int) -> bool:
    if int(key_id or 0) <= 0 or int(reset_timestamp_ms or 0) <= 0:
        return False
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 1
                FROM whitelist_limit_notifications
                WHERE key_id = ? AND reset_timestamp_ms = ?
                LIMIT 1
                """,
                (int(key_id), int(reset_timestamp_ms)),
            )
            return cursor.fetchone() is not None
    except sqlite3.Error as e:
        logging.error("Failed to read WHITELIST limit notification marker: %s", e)
        return False


def mark_whitelist_limit_notification_sent(key_id: int, reset_timestamp_ms: int) -> bool:
    if int(key_id or 0) <= 0 or int(reset_timestamp_ms or 0) <= 0:
        return False
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO whitelist_limit_notifications
                    (key_id, reset_timestamp_ms, notified_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (int(key_id), int(reset_timestamp_ms)),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error("Failed to write WHITELIST limit notification marker: %s", e)
        return False


def prune_whitelist_limit_notifications(retention_days: int = 120) -> int:
    try:
        safe_days = max(1, int(retention_days))
        cutoff = datetime.now() - timedelta(days=safe_days)
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM whitelist_limit_notifications WHERE notified_at < ?",
                (cutoff.isoformat(),),
            )
            conn.commit()
            return cursor.rowcount
    except (sqlite3.Error, TypeError, ValueError) as e:
        logging.error("Failed to prune WHITELIST limit notifications: %s", e)
        return 0


def get_setting(key: str) -> str | None:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
            result = cursor.fetchone()
            return result[0] if result else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get setting '{key}': {e}")
        return None


def get_all_settings() -> dict:
    settings = {}
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM bot_settings")
            rows = cursor.fetchall()
            for row in rows:
                settings[row["key"]] = row["value"]
    except sqlite3.Error as e:
        logging.error(f"Failed to get all settings: {e}")
    return settings


def get_all_settings_cached() -> dict:
    global _settings_cache_data, _settings_cache_expires_at

    now = time.monotonic()
    if _settings_cache_data is not None and now < _settings_cache_expires_at:
        return dict(_settings_cache_data)

    settings = get_all_settings()
    _settings_cache_data = dict(settings)
    _settings_cache_expires_at = now + _SETTINGS_CACHE_TTL_SECONDS
    return settings


def invalidate_settings_cache() -> None:
    global _settings_cache_data, _settings_cache_expires_at
    _settings_cache_data = None
    _settings_cache_expires_at = 0.0


def update_setting(key: str, value: str):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, value))
            conn.commit()
            invalidate_settings_cache()
            logging.info(f"Setting '{key}' updated.")
    except sqlite3.Error as e:
        logging.error(f"Failed to update setting '{key}': {e}")


def record_channel_gate_user(
    *,
    user_id: int,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    bot_source: str | None = None,
    event: str | None = None,
    status: str | None = None,
    allowed: bool = False,
    error: str | None = None,
) -> None:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO channel_gate_users (
                    user_id,
                    username,
                    first_name,
                    last_name,
                    bot_source,
                    first_seen_at,
                    last_seen_at,
                    last_event,
                    last_status,
                    last_checked_at,
                    joined_confirmed_at,
                    prompt_count,
                    check_count,
                    last_error
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?, CURRENT_TIMESTAMP,
                        CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                        CASE WHEN ? THEN 1 ELSE 0 END,
                        1,
                        ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    bot_source = CASE
                        WHEN channel_gate_users.bot_source = 'proxy_gate'
                             AND channel_gate_users.joined_confirmed_at IS NOT NULL
                        THEN channel_gate_users.bot_source
                        ELSE excluded.bot_source
                    END,
                    last_seen_at = CURRENT_TIMESTAMP,
                    last_event = excluded.last_event,
                    last_status = excluded.last_status,
                    last_checked_at = CURRENT_TIMESTAMP,
                    joined_confirmed_at = CASE
                        WHEN ? THEN COALESCE(channel_gate_users.joined_confirmed_at, CURRENT_TIMESTAMP)
                        ELSE channel_gate_users.joined_confirmed_at
                    END,
                    prompt_count = channel_gate_users.prompt_count + CASE WHEN ? THEN 1 ELSE 0 END,
                    check_count = channel_gate_users.check_count + 1,
                    last_error = excluded.last_error
                """,
                (
                    int(user_id),
                    username,
                    first_name,
                    last_name,
                    bot_source,
                    event,
                    status,
                    1 if allowed else 0,
                    0 if allowed else 1,
                    error,
                    1 if allowed else 0,
                    0 if allowed else 1,
                ),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to record channel gate user '{user_id}': {e}")


def has_channel_gate_pass(user_id: int, *, bot_source: str = "proxy_gate") -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 1
                FROM channel_gate_users
                WHERE user_id = ?
                  AND bot_source = ?
                  AND joined_confirmed_at IS NOT NULL
                LIMIT 1
                """,
                (int(user_id), bot_source),
            )
            return cursor.fetchone() is not None
    except sqlite3.Error as e:
        logging.error(f"Failed to check channel gate pass for '{user_id}': {e}")
        return False


def create_plan(host_name: str, plan_name: str, months: int, price: float):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO plans (host_name, plan_name, months, price) VALUES (?, ?, ?, ?)",
                (host_name, plan_name, months, price),
            )
            conn.commit()
            logging.info(f"Created new plan '{plan_name}' for host '{host_name}'.")
    except sqlite3.Error as e:
        logging.error(f"Failed to create plan for host '{host_name}': {e}")


def get_plans_for_host(host_name: str) -> list[dict]:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM plans WHERE host_name = ? ORDER BY months", (host_name,))
            plans = cursor.fetchall()
            return [dict(plan) for plan in plans]
    except sqlite3.Error as e:
        logging.error(f"Failed to get plans for host '{host_name}': {e}")
        return []


def get_plan_by_id(plan_id: int) -> dict | None:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,))
            plan = cursor.fetchone()
            return dict(plan) if plan else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get plan by id '{plan_id}': {e}")
        return None


def delete_plan(plan_id: int):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
            conn.commit()
            logging.info(f"Deleted plan with id {plan_id}.")
    except sqlite3.Error as e:
        logging.error(f"Failed to delete plan with id {plan_id}: {e}")


def _should_update_stored_username(
    telegram_id: int, current_username: str | None, incoming_username: str | None
) -> bool:
    incoming = str(incoming_username or "").strip()
    if not incoming:
        return False

    current = str(current_username or "").strip()
    fallback_username = f"user_{telegram_id}"
    if incoming == fallback_username and current and current != fallback_username:
        return False

    return current != incoming


def register_user_if_not_exists(telegram_id: int, username: str, referrer_id):
    from shop_bot.data_manager import referral_test_sessions, referrals

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            with _create_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("BEGIN IMMEDIATE")
                cursor.execute("SELECT telegram_id, username FROM users WHERE telegram_id = ?", (telegram_id,))
                row = cursor.fetchone()
                safe_referrer = referrals.consume_invite(conn, telegram_id, referrer_id)
                if not row:
                    cursor.execute(
                        "INSERT INTO users (telegram_id, username, registration_date, referred_by) VALUES (?, ?, ?, ?)",
                        (telegram_id, username, datetime.now(), safe_referrer),  # noqa: DTZ005 - legacy DB stores naive timestamps.
                    )
                    referrals._ensure_account(conn, telegram_id)
                else:
                    referral_test_sessions.bind_existing(conn, telegram_id, safe_referrer)
                    if _should_update_stored_username(telegram_id, row[1], username):
                        cursor.execute("UPDATE users SET username = ? WHERE telegram_id = ?", (username, telegram_id))
                conn.commit()
                return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_attempts:
                delay = 0.2 * attempt
                logging.warning(
                    "Register user %s retry %s/%s after lock (%ss).",
                    telegram_id,
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue
            logging.error(f"Failed to register user {telegram_id}: {e}")
            return
        except sqlite3.Error as e:
            logging.error(f"Failed to register user {telegram_id}: {e}")
            return


def set_user_username(telegram_id: int, username: str) -> bool:
    normalized_username = str(username or "").strip()
    if not normalized_username:
        return False
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET username = ? WHERE telegram_id = ?",
                (normalized_username, telegram_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error:
        logger.exception("Failed to set username for user %s", telegram_id)
        return False


def add_to_referral_balance(user_id: int, amount: float):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET referral_balance = referral_balance + ? WHERE telegram_id = ?", (amount, user_id)
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to add to referral balance for user {user_id}: {e}")


def set_referral_balance(user_id: int, value: float):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET referral_balance = ? WHERE telegram_id = ?", (value, user_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to set referral balance for user {user_id}: {e}")


def set_referral_balance_all(user_id: int, value: float):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET referral_balance_all = ? WHERE telegram_id = ?", (value, user_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to set total referral balance for user {user_id}: {e}")


def get_referral_balance(user_id: int) -> float:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT referral_balance FROM users WHERE telegram_id = ?", (user_id,))
            result = cursor.fetchone()
            return result[0] if result else 0.0
    except sqlite3.Error as e:
        logging.error(f"Failed to get referral balance for user {user_id}: {e}")
        return 0.0


def get_referral_count(user_id: int) -> int:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,))
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error(f"Failed to get referral count for user {user_id}: {e}")
        return 0


def get_user(telegram_id: int):
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            user_data = cursor.fetchone()
            return dict(user_data) if user_data else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get user {telegram_id}: {e}")
        return None


def get_user_id_by_username(username: str) -> int | None:
    normalized_username = str(username or "").strip().lstrip("@")
    if not normalized_username:
        return None

    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT telegram_id FROM users WHERE LOWER(TRIM(COALESCE(username, ''))) = LOWER(?)",
                (normalized_username,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            try:
                return int(row[0])
            except (TypeError, ValueError):
                return None
    except sqlite3.Error as e:
        logging.error(f"Failed to get user id by username {username}: {e}")
        return None


def set_terms_agreed(telegram_id: int):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET agreed_to_terms = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            logging.info(f"User {telegram_id} has agreed to terms.")
    except sqlite3.Error as e:
        logging.error(f"Failed to set terms agreed for user {telegram_id}: {e}")


def set_user_active_beta(telegram_id: int, active: bool = True) -> None:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET has_active_beta = ? WHERE telegram_id = ?",
                (1 if active else 0, telegram_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to set has_active_beta for user {telegram_id}: {e}")


def user_has_active_beta(telegram_id: int) -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT has_active_beta FROM users WHERE telegram_id = ?",
                (telegram_id,),
            )
            row = cursor.fetchone()
            return bool(row and row[0])
    except sqlite3.Error as e:
        logging.error(f"Failed to check has_active_beta for user {telegram_id}: {e}")
        return False


def set_blitz_refresh_used(telegram_id: int, used: bool = True) -> None:
    """Mark that the user has used the one-time 'Обновить ключ' feature."""
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET blitz_refresh_used = ? WHERE telegram_id = ?",
                (1 if used else 0, telegram_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to set blitz_refresh_used for user {telegram_id}: {e}")


def update_user_stats(telegram_id: int, amount_spent: float, months_purchased: int):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET total_spent = total_spent + ?, total_months = total_months + ? WHERE telegram_id = ?",
                (amount_spent, months_purchased, telegram_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to update user stats for {telegram_id}: {e}")


def apply_payment_accounting_once(
    payment_id: str,
    *,
    user_id: int,
    amount_rub: float,
    months: int,
    referrer_id: int | None = None,
    referral_reward: float = 0.0,
    activate_super: bool = False,
    discount_claim_id: str | None = None,
) -> bool:
    """Apply payment counters and referral credit exactly once per provider payment."""
    normalized_payment_id = str(payment_id or "").strip()
    if not normalized_payment_id:
        return False

    # Kept in the signature for old callers; persisted relationship and current
    # validated policy are authoritative, never a caller-provided credit amount.
    del referrer_id, referral_reward
    from shop_bot.data_manager import referrals

    try:
        amount_cents = referrals.money_cents(amount_rub)
        if amount_cents < 0 or int(months) < 0:
            return False
        safe_amount = amount_cents / 100
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            safe_referrer_id, reward_cents = referrals.payment_reward(conn, int(user_id), safe_amount)
            safe_reward = reward_cents / 100
            cursor.execute(
                """
                INSERT OR IGNORE INTO payment_accounting_effects
                    (payment_id, user_id, amount_rub, months, referrer_id,
                     referral_reward, activated_super)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_payment_id,
                    int(user_id),
                    safe_amount,
                    int(months),
                    safe_referrer_id,
                    safe_reward,
                    1 if activate_super else 0,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False

            referrals.consume_discount_invoice(
                conn, int(user_id), normalized_payment_id, amount_cents, discount_claim_id,
            )
            cursor.execute(
                """
                UPDATE users
                SET total_spent = total_spent + ?,
                    total_months = total_months + ?,
                    has_active_beta = CASE WHEN ? = 1 THEN 1 ELSE has_active_beta END
                WHERE telegram_id = ?
                """,
                (safe_amount, int(months), 1 if activate_super else 0, int(user_id)),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False

            if safe_referrer_id and safe_reward > 0:
                referrals.credit_reward(
                    conn, safe_referrer_id, int(user_id), reward_cents, normalized_payment_id,
                )
            conn.commit()
            return True
    except (sqlite3.Error, TypeError, ValueError):
        logger.exception("Failed to apply payment accounting for payment_id=%s", normalized_payment_id)
        return False


def is_payment_accounting_applied(payment_id: str) -> bool:
    normalized_payment_id = str(payment_id or "").strip()
    if not normalized_payment_id:
        return False
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM payment_accounting_effects WHERE payment_id = ? LIMIT 1",
                (normalized_payment_id,),
            )
            return cursor.fetchone() is not None
    except sqlite3.Error:
        logger.exception("Failed to read payment accounting state for payment_id=%s", normalized_payment_id)
        return False


def get_user_count() -> int:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error(f"Failed to get user count: {e}")
        return 0


def get_total_keys_count() -> int:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM vpn_keys")
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error(f"Failed to get total keys count: {e}")
        return 0


def get_total_spent_sum() -> float:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(total_spent) FROM users")
            return cursor.fetchone()[0] or 0.0
    except sqlite3.Error as e:
        logging.error(f"Failed to get total spent sum: {e}")
        return 0.0


def get_dashboard_stats() -> dict[str, int | float]:
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS user_count,
                (SELECT COUNT(*) FROM vpn_keys) AS total_keys,
                COALESCE((SELECT SUM(total_spent) FROM users), 0) AS total_spent,
                (SELECT COUNT(*) FROM xui_hosts) AS host_count
            """
        )
        row = cursor.fetchone()
        if not row:
            return {"user_count": 0, "total_keys": 0, "total_spent": 0.0, "host_count": 0}
        return {
            "user_count": int(row[0] or 0),
            "total_keys": int(row[1] or 0),
            "total_spent": float(row[2] or 0.0),
            "host_count": int(row[3] or 0),
        }
    except sqlite3.Error as e:
        logging.error("Failed to get dashboard stats: %s", e)
        return {"user_count": 0, "total_keys": 0, "total_spent": 0.0, "host_count": 0}


def create_pending_transaction(
    payment_id: str,
    user_id: int,
    amount_rub: float,
    metadata: dict,
    bot_source: str | None = None,
) -> int:
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            with _create_connection() as conn:
                cursor = conn.cursor()
                payment_method = None
                resolved_bot_source = _resolve_bot_source(
                    bot_source,
                    metadata if isinstance(metadata, dict) else None,
                )
                if isinstance(metadata, dict):
                    payment_method = metadata.get("payment_method")
                cursor.execute(
                    """
                    INSERT INTO transactions
                        (payment_id, user_id, status, amount_rub, payment_method, metadata, bot_source, issuance_state, retry_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        payment_id,
                        user_id,
                        "pending",
                        amount_rub,
                        payment_method,
                        json.dumps(metadata),
                        resolved_bot_source,
                        ISSUANCE_STATE_AWAITING_PAYMENT,
                    ),
                )
                conn.commit()
                return int(cursor.lastrowid or 0)
        except sqlite3.IntegrityError:
            try:
                with _create_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT transaction_id FROM transactions WHERE payment_id = ? ORDER BY transaction_id DESC LIMIT 1",
                        (payment_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        return int(row[0] or 0)
            except sqlite3.Error:
                pass
            return 0
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_attempts:
                delay = 0.25 * (2 ** (attempt - 1))
                logging.warning(
                    "Retry create pending transaction payment_id=%s attempt %s/%s after SQLITE_BUSY (%ss).",
                    payment_id,
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue
            logging.error(f"Failed to create pending transaction: {e}")
            return 0
        except sqlite3.Error as e:
            logging.error(f"Failed to create pending transaction: {e}")
            return 0
    return 0


def get_pending_transaction_metadata(payment_id: str) -> dict | None:
    """
    Return metadata for a transaction that still needs key issuance.
    Used as fallback when webhook metadata is missing.
    """
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT metadata
                FROM transactions
                WHERE payment_id = ?
                  AND (
                    status = 'pending'
                    OR (
                        status = 'paid'
                        AND (
                            issuance_state IS NULL
                            OR TRIM(COALESCE(issuance_state, '')) = ''
                            OR issuance_state IN (?, ?, ?)
                        )
                    )
                  )
                ORDER BY
                  CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                  created_date DESC
                LIMIT 1
                """,
                (
                    payment_id,
                    ISSUANCE_STATE_AWAITING_PAYMENT,
                    ISSUANCE_STATE_CONFIRMED,
                    ISSUANCE_STATE_RETRYABLE_FAILED,
                ),
            )
            row = cursor.fetchone()
            if not row or not row["metadata"]:
                return None
            try:
                data = json.loads(row["metadata"])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                logging.error(f"Failed to decode pending transaction metadata for payment_id={payment_id}")
                return None
    except sqlite3.Error as e:
        logging.error(f"Failed to get pending transaction metadata for payment_id={payment_id}: {e}")
        return None


def get_pending_paypear_transaction_by_order_id(order_id: str) -> dict | None:
    """
    Return a pending PayPear transaction by merchant reference/order id.

    PayPear sends the merchant `referenceId` unchanged in payment responses and
    webhooks. This fallback keeps instant webhook issuance working even if a
    webhook identifies the payment by reference rather than provider payment id.
    """
    normalized_order_id = str(order_id or "").strip()
    if not normalized_order_id:
        return None

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT payment_id, metadata
                FROM transactions
                WHERE (
                    status = 'pending'
                    OR (
                        status = 'paid'
                        AND (
                            issuance_state IS NULL
                            OR TRIM(COALESCE(issuance_state, '')) = ''
                            OR issuance_state IN (?, ?, ?)
                        )
                    )
                  )
                  AND (
                    LOWER(COALESCE(payment_method, '')) = 'paypear'
                    OR LOWER(COALESCE(json_extract(metadata, '$.payment_method'), '')) = 'paypear'
                  )
                  AND COALESCE(json_extract(metadata, '$.paypear_order_id'), '') = ?
                ORDER BY
                  CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                  created_date DESC
                LIMIT 1
                """,
                (
                    ISSUANCE_STATE_AWAITING_PAYMENT,
                    ISSUANCE_STATE_CONFIRMED,
                    ISSUANCE_STATE_RETRYABLE_FAILED,
                    normalized_order_id,
                ),
            )
            row = cursor.fetchone()
            if not row or not row["metadata"]:
                return None
            try:
                metadata = json.loads(row["metadata"])
            except json.JSONDecodeError:
                logger.exception(
                    "Failed to decode pending PayPear metadata for order_id=%s",
                    normalized_order_id,
                )
                return None
            if not isinstance(metadata, dict):
                return None
            return {
                "payment_id": str(row["payment_id"] or "").strip(),
                "metadata": metadata,
            }
    except sqlite3.Error:
        logger.exception("Failed to get PayPear transaction by order_id=%s", normalized_order_id)
        return None


def get_pending_lava_transaction_by_order_id(order_id: str) -> dict | None:
    """Return an unfinished LAVA transaction by the merchant order id."""
    normalized_order_id = str(order_id or "").strip()
    if not normalized_order_id:
        return None

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT payment_id, metadata
                FROM transactions
                WHERE (
                    status = 'pending'
                    OR (
                        status = 'paid'
                        AND (
                            issuance_state IS NULL
                            OR TRIM(COALESCE(issuance_state, '')) = ''
                            OR issuance_state IN (?, ?, ?)
                        )
                    )
                  )
                  AND (
                    LOWER(COALESCE(payment_method, '')) = 'lava'
                    OR LOWER(COALESCE(json_extract(metadata, '$.payment_method'), '')) = 'lava'
                  )
                  AND COALESCE(json_extract(metadata, '$.lava_order_id'), '') = ?
                ORDER BY
                  CASE WHEN status = 'pending' THEN 0 ELSE 1 END,
                  created_date DESC
                LIMIT 1
                """,
                (
                    ISSUANCE_STATE_AWAITING_PAYMENT,
                    ISSUANCE_STATE_CONFIRMED,
                    ISSUANCE_STATE_RETRYABLE_FAILED,
                    normalized_order_id,
                ),
            )
            row = cursor.fetchone()
            if not row or not row["metadata"]:
                return None
            try:
                metadata = json.loads(row["metadata"])
            except json.JSONDecodeError:
                logger.exception(
                    "Failed to decode pending LAVA metadata for order_id=%s",
                    normalized_order_id,
                )
                return None
            if not isinstance(metadata, dict):
                return None
            return {
                "payment_id": str(row["payment_id"] or "").strip(),
                "metadata": metadata,
            }
    except sqlite3.Error:
        logger.exception("Failed to get LAVA transaction by order_id=%s", normalized_order_id)
        return None


def promote_lava_pending_transaction(order_id: str, invoice_id: str) -> dict | None:
    """Bind a verified LAVA invoice to its durable pre-request transaction.

    Call only after the provider response, or an authenticated callback plus
    provider lookup, confirms invoice/order/amount identity. This operation
    changes identity only and never resets paid/processing/issued state. A fast
    callback and a later invoice-creation response may call it concurrently.
    """
    from shop_bot.data_manager import referrals

    order_id = str(order_id or "").strip()
    invoice_id = str(invoice_id or "").strip()
    if not order_id or len(order_id) > 200 or not invoice_id or len(invoice_id) > 200 or invoice_id.startswith("lava-order:"):
        raise ValueError("Некорректный идентификатор заказа или счёта LAVA.")
    placeholder_id = f"lava-order:{order_id}"
    with _create_connection() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT * FROM transactions WHERE payment_id IN (?,?)", (placeholder_id, invoice_id),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("Заказ и счёт LAVA уже связаны с разными операциями.")
        row = dict(rows[0])
        try:
            metadata = json.loads(row["metadata"] or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Повреждены данные ожидающего платежа LAVA.") from exc
        if not isinstance(metadata, dict):
            raise ValueError("Повреждены данные ожидающего платежа LAVA.")  # noqa: TRY004 - corrupt persisted JSON, not caller type.
        if (
            str(row.get("payment_method") or metadata.get("payment_method") or "").lower() != "lava"
            or str(metadata.get("payment_method") or "").lower() != "lava"
            or row.get("bot_source") != "reserve" or metadata.get("bot_source") != "reserve"
            or str(metadata.get("lava_order_id") or "").strip() != order_id
            or int(metadata.get("user_id") or row["user_id"]) != row["user_id"]
            or referrals.money_cents(metadata.get("price", row["amount_rub"])) != referrals.money_cents(row["amount_rub"])
        ):
            raise ValueError("Принадлежность или сумма ожидающего платежа LAVA не совпадает.")
        for key in ("lava_invoice_id", "external_payment_id"):
            if str(metadata.get(key) or "").strip() not in {"", invoice_id}:
                raise ValueError("Заказ LAVA уже связан с другим счётом.")
        promoted = row["payment_id"] == placeholder_id
        if promoted and (
            row["status"] != "pending" or row.get("confirmed_at") or metadata.get("payment_confirmed")
            or row.get("issuance_state") not in {None, "", ISSUANCE_STATE_AWAITING_PAYMENT}
            or conn.execute("SELECT 1 FROM payment_accounting_effects WHERE payment_id=?", (placeholder_id,)).fetchone()
        ):
            raise ValueError("Нельзя заменить идентификатор уже обрабатываемой операции LAVA.")
        claim_id = str(metadata.get("referral_discount_claim_id") or "").strip()
        if claim_id:
            claim = conn.execute(
                "SELECT * FROM referral_discount_invoices WHERE id=? AND discount_cents>0", (claim_id,),
            ).fetchone()
            if (claim is None or claim["user_id"] != row["user_id"]
                    or claim["price_cents"] != referrals.money_cents(row["amount_rub"])
                    or str(claim["provider"]).lower() != "lava"
                    or claim["payment_id"] not in {None, "", invoice_id}
                    or (claim["status"] == "cancelled" and claim["payment_id"] != invoice_id)):
                raise ValueError("Скидка не соответствует ожидающему платежу LAVA.")
            conn.execute("""
                UPDATE referral_discount_invoices SET payment_id=?,
                    status=CASE WHEN status IN ('reserved','uncertain') THEN 'issued' ELSE status END,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
            """, (invoice_id, claim_id))
        metadata.update(lava_invoice_id=invoice_id, external_payment_id=invoice_id, lava_invoice_pending=False)
        conn.execute(
            "UPDATE transactions SET payment_id=?,metadata=? WHERE transaction_id=?",
            (invoice_id, json.dumps(metadata), row["transaction_id"]),
        )
        conn.commit()
        row.update(payment_id=invoice_id, metadata=metadata, was_promoted=promoted)
        return row


def get_transaction_by_payment_id(payment_id: str) -> dict | None:
    if not payment_id:
        return None
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT *
                FROM transactions
                WHERE payment_id = ?
                ORDER BY transaction_id DESC
                LIMIT 1
                """,
                (payment_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error("Failed to get transaction by payment_id=%s: %s", payment_id, e)
        return None


def update_transaction_metadata(
    payment_id: str,
    metadata: dict,
    *,
    statuses: tuple[str, ...] = ("pending", "processing"),
) -> bool:
    if not payment_id or not isinstance(metadata, dict):
        return False

    safe_statuses = tuple(item for item in statuses if item)
    if not safe_statuses:
        return False

    placeholders = ", ".join(["?"] * len(safe_statuses))
    payment_method = metadata.get("payment_method")

    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                UPDATE transactions
                SET metadata = ?,
                    payment_method = COALESCE(?, payment_method)
                WHERE payment_id = ? AND status IN ({placeholders})
                """,
                (
                    json.dumps(metadata),
                    payment_method,
                    payment_id,
                    *safe_statuses,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Failed to update transaction metadata for payment_id={payment_id}: {e}")
        return False


def update_transaction_metadata_field(
    payment_id: str,
    key: str,
    value: object,
    *,
    statuses: tuple[str, ...] = ("pending", "processing"),
) -> bool:
    """Atomically set a single key in the JSON metadata column."""
    if not payment_id or not key:
        return False
    safe_statuses = tuple(item for item in statuses if item)
    if not safe_statuses:
        return False
    placeholders = ", ".join(["?"] * len(safe_statuses))
    json_value = json.dumps(value)
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                UPDATE transactions
                SET metadata = json_set(COALESCE(metadata, '{{}}'), ?, json(?))
                WHERE payment_id = ? AND status IN ({placeholders})
                """,
                (f"$.{key}", json_value, payment_id, *safe_statuses),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Failed to update metadata field '{key}' for payment_id={payment_id}: {e}")
        return False


def _normalize_db_timestamp(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    text = str(value).strip()
    return text or None


def _retry_backoff_seconds(retry_count: int) -> float:
    """Full jitter exponential backoff (AWS recommended pattern).

    Returns a random value between _RETRY_MIN_BACKOFF_SECONDS and the
    exponential ceiling, preventing thundering herd after host recovery.
    """
    safe_count = max(1, int(retry_count))
    exp_delay = min(
        ISSUANCE_RETRY_BACKOFF_MAX_SECONDS,
        ISSUANCE_RETRY_BACKOFF_BASE_SECONDS * (2 ** (safe_count - 1)),
    )
    return max(_RETRY_MIN_BACKOFF_SECONDS, random.uniform(0, exp_delay))


def mark_transaction_confirmed(
    payment_id: str,
    *,
    metadata: dict | None = None,
    payment_method: str | None = None,
    confirmed_at: datetime | str | None = None,
) -> bool:
    if not payment_id:
        return False
    metadata_payload = json.dumps(metadata) if isinstance(metadata, dict) else None
    confirmed_at_payload = _normalize_db_timestamp(confirmed_at)
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE transactions
                SET issuance_state = CASE
                        WHEN status = 'paid' THEN issuance_state
                        WHEN status = 'canceled' THEN ?
                        WHEN issuance_state IN (?, ?, ?, ?) THEN issuance_state
                        ELSE ?
                    END,
                    confirmed_at = COALESCE(confirmed_at, ?, CURRENT_TIMESTAMP),
                    next_retry_at = COALESCE(next_retry_at, CURRENT_TIMESTAMP),
                    payment_method = COALESCE(?, payment_method),
                    metadata = COALESCE(?, metadata)
                WHERE payment_id = ? AND status IN ('pending', 'processing', 'paid')
                """,
                (
                    ISSUANCE_STATE_CANCELED,
                    *_TERMINAL_ISSUANCE_STATES,
                    ISSUANCE_STATE_CONFIRMED,
                    confirmed_at_payload,
                    payment_method,
                    metadata_payload,
                    payment_id,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Failed to mark transaction confirmed for payment_id={payment_id}: {e}")
        return False


def mark_transaction_retryable_failure(
    payment_id: str,
    *,
    reason: str,
    metadata: dict | None = None,
    payment_method: str | None = None,
    next_retry_at: datetime | str | None = None,
    statuses: tuple[str, ...] = ("processing", "pending"),
    respect_active_lock: bool = False,
) -> dict[str, object]:
    if not payment_id:
        return {"updated": False, "retry_count": 0, "next_retry_at": None}

    safe_statuses = tuple(item for item in statuses if item)
    if not safe_statuses:
        return {"updated": False, "retry_count": 0, "next_retry_at": None}

    lock_guard = f" AND {_TRANSACTION_LOCK_AVAILABLE_SQL}" if respect_active_lock else ""
    placeholders = ", ".join(["?"] * len(safe_statuses))
    metadata_payload = json.dumps(metadata) if isinstance(metadata, dict) else None
    now = datetime.now()
    next_retry_payload = _normalize_db_timestamp(next_retry_at)
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT retry_count
                FROM transactions
                WHERE payment_id = ? AND status IN ({placeholders}){lock_guard}
                  AND (
                    issuance_state IS NULL
                    OR TRIM(COALESCE(issuance_state, '')) = ''
                    OR issuance_state NOT IN (?, ?, ?, ?)
                  )
                ORDER BY transaction_id DESC
                LIMIT 1
                """,
                (payment_id, *safe_statuses, *_TERMINAL_ISSUANCE_STATES),
            )
            row = cursor.fetchone()
            if row is None:
                return {"updated": False, "retry_count": 0, "next_retry_at": None}

            retry_count = int(row["retry_count"] or 0) + 1
            if not next_retry_payload:
                next_retry_payload = _normalize_db_timestamp(
                    now + timedelta(seconds=_retry_backoff_seconds(retry_count))
                )

            cursor.execute(
                f"""
                UPDATE transactions
                SET status = 'pending',
                    issuance_state = ?,
                    retry_count = ?,
                    next_retry_at = ?,
                    confirmed_at = COALESCE(confirmed_at, CURRENT_TIMESTAMP),
                    last_attempt_at = CURRENT_TIMESTAMP,
                    last_error = ?,
                    payment_method = COALESCE(?, payment_method),
                    metadata = COALESCE(?, metadata)
                WHERE payment_id = ? AND status IN ({placeholders}){lock_guard}
                  AND (
                    issuance_state IS NULL
                    OR TRIM(COALESCE(issuance_state, '')) = ''
                    OR issuance_state NOT IN (?, ?, ?, ?)
                  )
                """,
                (
                    ISSUANCE_STATE_RETRYABLE_FAILED,
                    retry_count,
                    next_retry_payload,
                    str(reason or "unknown"),
                    payment_method,
                    metadata_payload,
                    payment_id,
                    *safe_statuses,
                    *_TERMINAL_ISSUANCE_STATES,
                ),
            )
            conn.commit()
            return {
                "updated": cursor.rowcount > 0,
                "retry_count": retry_count,
                "next_retry_at": next_retry_payload,
            }
    except sqlite3.Error as e:
        logging.error(f"Failed to mark retryable failure for payment_id={payment_id}: {e}")
        return {"updated": False, "retry_count": 0, "next_retry_at": None}


def mark_transaction_fatal_failure(
    payment_id: str,
    *,
    reason: str,
    metadata: dict | None = None,
    payment_method: str | None = None,
    statuses: tuple[str, ...] = ("processing", "pending"),
) -> bool:
    """Mark a transaction as permanently failed (no automatic retry).

    Used for errors that will never resolve by retrying, e.g. Telegram
    ``chat not found`` (user never started the bot) or ``host_not_found``
    (configuration error requiring manual intervention).
    """
    if not payment_id:
        return False

    safe_statuses = tuple(item for item in statuses if item)
    if not safe_statuses:
        return False

    placeholders = ", ".join(["?"] * len(safe_statuses))
    metadata_payload = json.dumps(metadata) if isinstance(metadata, dict) else None

    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                UPDATE transactions
                SET status = 'pending',
                    issuance_state = ?,
                    last_attempt_at = CURRENT_TIMESTAMP,
                    last_error = ?,
                    payment_method = COALESCE(?, payment_method),
                    metadata = COALESCE(?, metadata)
                WHERE payment_id = ? AND status IN ({placeholders})
                """,
                (
                    ISSUANCE_STATE_FATAL_FAILED,
                    str(reason or "unknown"),
                    payment_method,
                    metadata_payload,
                    payment_id,
                    *safe_statuses,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Failed to mark fatal failure for payment_id={payment_id}: {e}")
        return False


def mark_transaction_exhausted(payment_id: str, *, reason: str = "retry_exhausted") -> bool:
    """Terminal state: all retry attempts used. Stops autoretry and daily catch-up."""
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE transactions
                SET issuance_state = ?,
                    last_error = ?
                WHERE payment_id = ?
                  AND status = 'pending'
                  AND issuance_state = ?
                """,
                (ISSUANCE_STATE_EXHAUSTED, reason, payment_id, ISSUANCE_STATE_RETRYABLE_FAILED),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Failed to mark exhausted for payment_id={payment_id}: {e}")
        return False


def is_transaction_paid(payment_id: str) -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM transactions WHERE payment_id = ? AND status = 'paid' LIMIT 1",
                (payment_id,),
            )
            return cursor.fetchone() is not None
    except sqlite3.Error as e:
        logging.error(f"Failed to check paid status for payment_id={payment_id}: {e}")
        return False


def is_transaction_fully_issued(payment_id: str) -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 1
                FROM transactions
                WHERE payment_id = ?
                  AND status = 'paid'
                  AND issuance_state = ?
                LIMIT 1
                """,
                (payment_id, ISSUANCE_STATE_ISSUED),
            )
            return cursor.fetchone() is not None
    except sqlite3.Error:
        logger.exception("Failed to check issued status for payment_id=%s", payment_id)
        return False


def is_transaction_processing(payment_id: str) -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM transactions WHERE payment_id = ? AND status = 'processing' LIMIT 1",
                (payment_id,),
            )
            return cursor.fetchone() is not None
    except sqlite3.Error as e:
        logging.error(f"Failed to check processing status for payment_id={payment_id}: {e}")
        return False


def try_acquire_transaction_lock(
    payment_id: str,
    *,
    ttl_seconds: int = TRANSACTION_LOCK_TTL_SECONDS,
) -> str | None:
    """Atomically lock one payment issuance path and return its lock token."""
    normalized_payment_id = str(payment_id or "").strip()
    if not normalized_payment_id:
        return None

    lock_token = f"{os.getpid()}:{threading.get_ident()}:{uuid.uuid4().hex}"
    lock_ttl_modifier = f"-{max(1, int(ttl_seconds))} seconds"
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE transactions
                SET locked_by = ?,
                    locked_at = CURRENT_TIMESTAMP
                WHERE payment_id = ?
                  AND NOT (status = 'paid' AND issuance_state = ?)
                  AND (
                    locked_by IS NULL
                    OR TRIM(COALESCE(locked_by, '')) = ''
                    OR locked_at IS NULL
                    OR datetime(locked_at) <= datetime('now', ?)
                  )
                """,
                (
                    lock_token,
                    normalized_payment_id,
                    ISSUANCE_STATE_ISSUED,
                    lock_ttl_modifier,
                ),
            )
            conn.commit()
            return lock_token if cursor.rowcount > 0 else None
    except sqlite3.Error:
        logger.exception("Failed to lock payment issuance for payment_id=%s", normalized_payment_id)
        return None


def release_transaction_lock(payment_id: str, lock_token: str | None) -> bool:
    """Release a payment lock only when it is still owned by this caller."""
    normalized_payment_id = str(payment_id or "").strip()
    normalized_token = str(lock_token or "").strip()
    if not normalized_payment_id or not normalized_token:
        return False

    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE transactions
                SET locked_by = NULL,
                    locked_at = NULL
                WHERE payment_id = ?
                  AND locked_by = ?
                """,
                (normalized_payment_id, normalized_token),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error:
        logger.exception("Failed to release payment issuance lock for payment_id=%s", normalized_payment_id)
        return False


def refresh_transaction_lock(payment_id: str, lock_token: str | None) -> bool:
    """Extend ownership of a payment issuance lock during long panel calls."""
    normalized_payment_id = str(payment_id or "").strip()
    normalized_token = str(lock_token or "").strip()
    if not normalized_payment_id or not normalized_token:
        return False

    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE transactions
                SET locked_at = CURRENT_TIMESTAMP
                WHERE payment_id = ?
                  AND locked_by = ?
                  AND NOT (status = 'paid' AND issuance_state = ?)
                """,
                (normalized_payment_id, normalized_token, ISSUANCE_STATE_ISSUED),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error:
        logger.exception("Failed to refresh payment issuance lock for payment_id=%s", normalized_payment_id)
        return False


def start_transaction_processing(payment_id: str, *, metadata: dict | None = None) -> bool:
    """
    Atomically move transaction from pending/paid anomaly to processing.
    Returns True only if transition happened. Optional retry metadata is stored
    by the same guarded UPDATE, so a competing owner cannot be overwritten.
    """
    metadata_payload = json.dumps(metadata) if isinstance(metadata, dict) else None
    payment_method = (str(metadata.get("payment_method") or "").strip() or None) if isinstance(metadata, dict) else None
    confirmed_at = _normalize_db_timestamp(metadata.get("paid_at")) if isinstance(metadata, dict) else None
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                UPDATE transactions
                SET status = 'processing',
                    issuance_state = ?,
                    metadata = COALESCE(?, metadata),
                    payment_method = COALESCE(?, payment_method),
                    next_retry_at = NULL,
                    last_attempt_at = CURRENT_TIMESTAMP,
                    confirmed_at = CASE
                        WHEN COALESCE(json_extract(COALESCE(?, metadata), '$.payment_confirmed'), 0) = 1
                            THEN COALESCE(confirmed_at, ?, CURRENT_TIMESTAMP)
                        ELSE confirmed_at
                    END
                WHERE payment_id = ?
                  AND status IN ('pending', 'paid')
                  AND {_TRANSACTION_LOCK_AVAILABLE_SQL}
                  AND (
                    issuance_state IS NULL
                    OR TRIM(COALESCE(issuance_state, '')) = ''
                    OR issuance_state IN (?, ?, ?)
                  )
                """,  # noqa: S608 - only the fixed lease predicate is interpolated.
                (
                    ISSUANCE_STATE_ISSUING,
                    metadata_payload,
                    payment_method,
                    metadata_payload,
                    confirmed_at,
                    payment_id,
                    ISSUANCE_STATE_AWAITING_PAYMENT,
                    ISSUANCE_STATE_CONFIRMED,
                    ISSUANCE_STATE_RETRYABLE_FAILED,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Failed to start processing for payment_id={payment_id}: {e}")
        return False


def reset_transaction_to_pending(
    payment_id: str, *, reason: str = "reset_to_pending", respect_active_lock: bool = False,
) -> bool:
    """Return transaction from processing to pending with minimum retry backoff.

    Sets issuance_state to retryable_failed and ensures next_retry_at is
    at least _RETRY_MIN_BACKOFF_SECONDS in the future so the transaction
    cannot re-enter a tight retry loop.
    """
    lock_guard = f" AND {_TRANSACTION_LOCK_AVAILABLE_SQL}" if respect_active_lock else ""
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                UPDATE transactions
                SET status = 'pending',
                    issuance_state = ?,
                    next_retry_at = CASE
                        WHEN next_retry_at IS NULL
                             OR datetime(next_retry_at) <= datetime('now')
                        THEN datetime('now', ?)
                        ELSE next_retry_at
                    END,
                    last_error = COALESCE(?, last_error)
                WHERE payment_id = ? AND status = 'processing'{lock_guard}
                """,  # noqa: S608 - only the fixed lease predicate is interpolated.
                (
                    ISSUANCE_STATE_RETRYABLE_FAILED,
                    f"+{_RETRY_MIN_BACKOFF_SECONDS} seconds",
                    reason,
                    payment_id,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Failed to reset processing transaction for payment_id={payment_id}: {e}")
        return False


def recover_stale_processing_transactions(
    *,
    stale_seconds: int = 120,
    limit: int = 50,
) -> list[dict]:
    """
    Find and reset transactions stuck in processing/issuing state.

    After a bot crash, transactions may remain in processing/issuing forever
    because the autoretry worker only looks at status='pending'.
    This function resets them to pending/retryable_failed so autoretry picks them up.
    """
    safe_stale = max(30, int(stale_seconds))
    safe_limit = max(1, min(int(limit), 200))
    stale_expr = f"-{safe_stale} seconds"

    recovered: list[dict] = []
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                f"""
                SELECT payment_id, user_id, metadata, issuance_state, last_attempt_at, created_date
                FROM transactions
                WHERE status = 'processing'
                  AND {_TRANSACTION_LOCK_AVAILABLE_SQL}
                  AND payment_id IS NOT NULL
                  AND TRIM(payment_id) <> ''
                  AND datetime(COALESCE(last_attempt_at, created_date)) <= datetime('now', ?)
                ORDER BY transaction_id DESC
                LIMIT ?
                """,  # noqa: S608 - only the fixed lease predicate is interpolated.
                (stale_expr, safe_limit),
            )
            rows = [dict(row) for row in cursor.fetchall()]

            for row in rows:
                pid = str(row.get("payment_id") or "").strip()
                if not pid:
                    continue
                cursor.execute(
                    f"""
                    UPDATE transactions
                    SET status = 'pending',
                        issuance_state = ?,
                        confirmed_at = COALESCE(confirmed_at, CURRENT_TIMESTAMP),
                        next_retry_at = COALESCE(next_retry_at, CURRENT_TIMESTAMP),
                        last_error = 'stale_processing_recovered',
                        locked_by = NULL,
                        locked_at = NULL,
                        metadata = CASE
                            WHEN metadata IS NOT NULL AND json_valid(metadata)
                            THEN json_set(metadata, '$.payment_confirmed', 1)
                            ELSE metadata
                        END
                    WHERE payment_id = ? AND status = 'processing'
                      AND {_TRANSACTION_LOCK_AVAILABLE_SQL}
                      AND datetime(COALESCE(last_attempt_at, created_date)) <= datetime('now', ?)
                    """,  # noqa: S608 - only the fixed lease predicate is interpolated.
                    (ISSUANCE_STATE_RETRYABLE_FAILED, pid, stale_expr),
                )
                if cursor.rowcount > 0:
                    recovered.append(row)

            if recovered:
                conn.commit()
                logging.info(
                    "Recovered %d stale processing transactions: %s",
                    len(recovered),
                    [r.get("payment_id") for r in recovered],
                )
            return recovered
    except sqlite3.Error as e:
        logging.error("Failed to recover stale processing transactions: %s", e)
        return []


def mark_transaction_canceled(payment_id: str, payment_method: str | None = None) -> bool:
    """
    Mark transaction as canceled when provider reports cancellation.
    Works for pending/processing records.
    """
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE transactions
                SET status = 'canceled',
                    issuance_state = ?,
                    next_retry_at = NULL,
                    payment_method = COALESCE(?, payment_method)
                WHERE payment_id = ? AND status IN ('pending', 'processing')
                """,
                (ISSUANCE_STATE_CANCELED, payment_method, payment_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Failed to mark transaction canceled for payment_id={payment_id}: {e}")
        return False


def get_pending_yookassa_transactions(
    *,
    limit: int = 80,
    min_age_seconds: int = 120,
) -> list[dict]:
    """
    Return pending YooKassa-like transactions old enough for reconciliation.
    """
    safe_limit = max(1, min(int(limit or 80), 500))
    safe_age = max(0, int(min_age_seconds or 0))
    age_expr = f"-{safe_age} seconds"

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT transaction_id, payment_id, user_id, amount_rub, created_date, payment_method, metadata
                FROM transactions
                WHERE status = 'pending'
                  AND payment_id IS NOT NULL
                  AND TRIM(payment_id) <> ''
                  AND datetime(created_date) <= datetime('now', ?)
                  AND (
                        issuance_state IS NULL
                        OR TRIM(COALESCE(issuance_state, '')) = ''
                        OR issuance_state = ?
                  )
                  AND (
                        COALESCE(payment_method, '') = 'YooKassa'
                        OR metadata LIKE '%"payment_method": "YooKassa"%'
                        OR metadata LIKE '%"cms_name": "yookassa_sdk_python"%'
                  )
                ORDER BY transaction_id DESC
                LIMIT ?
                """,
                (
                    age_expr,
                    ISSUANCE_STATE_AWAITING_PAYMENT,
                    safe_limit,
                ),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Failed to get pending YooKassa transactions: {e}")
        return []


def get_pending_platega_transactions(
    *,
    limit: int = 80,
    min_age_seconds: int = 120,
) -> list[dict]:
    """
    Return pending Platega transactions old enough for reconciliation.
    """
    safe_limit = max(1, min(int(limit or 80), 500))
    safe_age = max(0, int(min_age_seconds or 0))
    age_expr = f"-{safe_age} seconds"

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT transaction_id, payment_id, user_id, amount_rub, created_date, payment_method, metadata
                FROM transactions
                WHERE status = 'pending'
                  AND payment_id IS NOT NULL
                  AND TRIM(payment_id) <> ''
                  AND datetime(created_date) <= datetime('now', ?)
                  AND (
                        issuance_state IS NULL
                        OR TRIM(COALESCE(issuance_state, '')) = ''
                        OR issuance_state = ?
                  )
                  AND (
                        LOWER(COALESCE(payment_method, '')) = 'platega'
                        OR metadata LIKE '%"payment_method": "Platega"%'
                        OR metadata LIKE '%"payment_method": "platega"%'
                  )
                ORDER BY transaction_id DESC
                LIMIT ?
                """,
                (
                    age_expr,
                    ISSUANCE_STATE_AWAITING_PAYMENT,
                    safe_limit,
                ),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Failed to get pending Platega transactions: {e}")
        return []


def get_pending_paypear_transactions(
    *,
    limit: int = 80,
    min_age_seconds: int = 120,
) -> list[dict]:
    """
    Return pending PayPear transactions old enough for reconciliation.
    """
    safe_limit = max(1, min(int(limit or 80), 500))
    safe_age = max(0, int(min_age_seconds or 0))
    age_expr = f"-{safe_age} seconds"

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT transaction_id, payment_id, user_id, amount_rub, created_date, payment_method, metadata
                FROM transactions
                WHERE status = 'pending'
                  AND payment_id IS NOT NULL
                  AND TRIM(payment_id) <> ''
                  AND datetime(created_date) <= datetime('now', ?)
                  AND (
                        issuance_state IS NULL
                        OR TRIM(COALESCE(issuance_state, '')) = ''
                        OR issuance_state = ?
                  )
                  AND (
                        LOWER(COALESCE(payment_method, '')) = 'paypear'
                        OR metadata LIKE '%"payment_method": "PayPear"%'
                        OR metadata LIKE '%"payment_method": "paypear"%'
                  )
                ORDER BY transaction_id DESC
                LIMIT ?
                """,
                (
                    age_expr,
                    ISSUANCE_STATE_AWAITING_PAYMENT,
                    safe_limit,
                ),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error:
        logger.exception("Failed to get pending PayPear transactions.")
        return []


def get_pending_lava_transactions(
    *,
    limit: int = 80,
    min_age_seconds: int = 120,
) -> list[dict]:
    """Return unfinished LAVA invoices old enough for provider reconciliation."""
    safe_limit = max(1, min(int(limit or 80), 500))
    safe_age = max(0, int(min_age_seconds or 0))
    age_expr = f"-{safe_age} seconds"

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT transaction_id, payment_id, user_id, amount_rub, created_date, payment_method, metadata
                FROM transactions
                WHERE status = 'pending'
                  AND payment_id IS NOT NULL
                  AND TRIM(payment_id) <> ''
                  AND datetime(created_date) <= datetime('now', ?)
                  AND (
                        issuance_state IS NULL
                        OR TRIM(COALESCE(issuance_state, '')) = ''
                        OR issuance_state = ?
                  )
                  AND (
                        LOWER(COALESCE(payment_method, '')) = 'lava'
                        OR metadata LIKE '%"payment_method": "LAVA"%'
                        OR metadata LIKE '%"payment_method": "Lava"%'
                        OR metadata LIKE '%"payment_method": "lava"%'
                  )
                ORDER BY transaction_id DESC
                LIMIT ?
                """,
                (
                    age_expr,
                    ISSUANCE_STATE_AWAITING_PAYMENT,
                    safe_limit,
                ),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error:
        logger.exception("Failed to get pending LAVA transactions.")
        return []


def get_pending_confirmed_transactions(
    *,
    limit: int = 80,
    min_age_seconds: int = 120,
    host_name: str | None = None,
    due_only: bool = True,
    retryable_only: bool = False,
) -> list[dict]:
    """
    Return transactions eligible for issuance retry.

    Normal flow uses ``status='pending'`` with ``metadata.payment_confirmed=1``.
    Legacy anomaly rows with ``status='paid'`` but non-issued ``issuance_state``
    are also included so catch-up can recover them.
    """
    safe_limit = max(1, min(int(limit or 80), 500))
    safe_age = max(0, int(min_age_seconds or 0))
    age_expr = f"-{safe_age} seconds"

    normalized_host = str(host_name or "").strip().lower()

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            where_parts = [
                "status IN ('pending', 'paid')",
                _TRANSACTION_LOCK_AVAILABLE_SQL,
                "payment_id IS NOT NULL",
                "TRIM(payment_id) <> ''",
                "datetime(created_date) <= datetime('now', ?)",
                "(status = 'paid' OR COALESCE(json_extract(metadata, '$.payment_confirmed'), 0) = 1)",
                f"(issuance_state IS NULL OR issuance_state <> '{ISSUANCE_STATE_ISSUING}')",
            ]
            params: list[object] = [age_expr]

            if retryable_only:
                where_parts.append("issuance_state = ?")
                params.append(ISSUANCE_STATE_RETRYABLE_FAILED)
            else:
                where_parts.append("(issuance_state IS NULL OR issuance_state IN (?, ?, ?))")
                params.extend(
                    [
                        ISSUANCE_STATE_CONFIRMED,
                        ISSUANCE_STATE_RETRYABLE_FAILED,
                        ISSUANCE_STATE_AWAITING_PAYMENT,
                    ]
                )

            if due_only:
                where_parts.append("(next_retry_at IS NULL OR datetime(next_retry_at) <= datetime('now'))")

            if normalized_host:
                where_parts.append("LOWER(COALESCE(json_extract(metadata, '$.host_name'), '')) = ?")
                params.append(normalized_host)

            where_sql = " AND ".join(where_parts)
            cursor.execute(
                f"""
                SELECT
                    transaction_id,
                    payment_id,
                    user_id,
                    amount_rub,
                    created_date,
                    payment_method,
                    metadata,
                    issuance_state,
                    retry_count,
                    next_retry_at,
                    confirmed_at,
                    last_error
                FROM transactions
                WHERE {where_sql}
                ORDER BY transaction_id DESC
                LIMIT ?
                """,
                tuple(params + [safe_limit]),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            for row in rows:
                raw_meta = row.get("metadata")
                if not raw_meta:
                    row["metadata"] = None
                    continue
                try:
                    parsed = json.loads(raw_meta)
                    row["metadata"] = parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    row["metadata"] = None
            return rows
    except sqlite3.Error as e:
        logging.error(f"Failed to get pending confirmed transactions: {e}")
        return []


def get_delivery_time_percentiles(
    period_hours: int = 24,
) -> dict[str, float | None]:
    """Return p50, p95, p99 delivery time in seconds for recent paid transactions."""
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    (julianday(json_extract(metadata, '$.key_delivered_at'))
                     - julianday(json_extract(metadata, '$.payment_confirmed_at'))
                    ) * 86400.0 AS delivery_seconds
                FROM transactions
                WHERE status = 'paid'
                  AND json_extract(metadata, '$.payment_confirmed_at') IS NOT NULL
                  AND json_extract(metadata, '$.key_delivered_at') IS NOT NULL
                  AND created_date >= datetime('now', ?)
                ORDER BY delivery_seconds
                """,
                (f"-{int(period_hours)} hours",),
            )
            values = [r[0] for r in cursor.fetchall() if r[0] is not None and r[0] >= 0]
    except sqlite3.Error as e:
        logging.error("get_delivery_time_percentiles failed: %s", e)
        return {"p50": None, "p95": None, "p99": None, "count": 0}

    n = len(values)
    if n == 0:
        return {"p50": None, "p95": None, "p99": None, "count": 0}

    def _percentile(data: list[float], pct: float) -> float:
        idx = (pct / 100.0) * (len(data) - 1)
        lo = int(idx)
        hi = min(lo + 1, len(data) - 1)
        frac = idx - lo
        return round(data[lo] * (1 - frac) + data[hi] * frac, 2)

    return {
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "p99": _percentile(values, 99),
        "count": n,
    }


def get_stale_pending_by_provider(
    *,
    max_age_hours: int = 2,
    exclude_providers: set[str] | None = None,
    limit: int = 50,
) -> list[dict]:
    """Return pending/processing transactions older than *max_age_hours* grouped by provider."""
    exclude = exclude_providers or {"yookassa"}
    safe_limit = max(1, min(int(limit or 50), 200))
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT payment_id, payment_method, metadata, status,
                       created_date, issuance_state, retry_count
                FROM transactions
                WHERE status IN ('pending', 'processing')
                  AND issuance_state NOT IN (?, ?)
                  AND created_date <= datetime('now', ?)
                ORDER BY created_date ASC
                LIMIT ?
                """,
                (
                    ISSUANCE_STATE_EXHAUSTED,
                    ISSUANCE_STATE_FATAL_FAILED,
                    f"-{int(max_age_hours)} hours",
                    safe_limit,
                ),
            )
            rows = cursor.fetchall()
    except sqlite3.Error as e:
        logging.error("get_stale_pending_by_provider failed: %s", e)
        return []

    results: list[dict] = []
    for row in rows:
        provider = str(row[1] or "unknown").strip().lower()
        if provider in exclude:
            continue
        meta = {}
        if row[2]:
            try:
                meta = json.loads(row[2])
            except (json.JSONDecodeError, TypeError):
                pass
        results.append(
            {
                "payment_id": row[0],
                "payment_method": provider,
                "metadata": meta,
                "status": row[3],
                "created_date": row[4],
                "issuance_state": row[5],
                "retry_count": row[6] or 0,
            }
        )
    return results


def get_retryable_failures_over_sla(
    *,
    sla_seconds: int = 900,
    limit: int = 20,
) -> list[dict]:
    safe_limit = max(1, min(int(limit or 20), 200))
    safe_sla = max(60, int(sla_seconds or 900))
    sla_expr = f"-{safe_sla} seconds"
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    payment_id,
                    user_id,
                    payment_method,
                    metadata,
                    retry_count,
                    confirmed_at,
                    created_date,
                    next_retry_at,
                    last_error
                FROM transactions
                WHERE status = 'pending'
                  AND issuance_state = ?
                  AND COALESCE(json_extract(metadata, '$.payment_confirmed'), 0) = 1
                  AND COALESCE(json_extract(metadata, '$.issuance_sla_alert_sent'), 0) != 1
                  AND confirmed_at IS NOT NULL
                  AND datetime(confirmed_at) <= datetime('now', ?)
                ORDER BY datetime(confirmed_at) ASC
                LIMIT ?
                """,
                (
                    ISSUANCE_STATE_RETRYABLE_FAILED,
                    sla_expr,
                    safe_limit,
                ),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error("Failed to get retryable failures over SLA: %s", e)
        return []


def claim_retryable_failure_sla_alert(
    payment_id: str,
    *,
    alert_at: datetime | str | None = None,
) -> bool:
    if not payment_id:
        return False

    alert_at_payload = json.dumps(_normalize_db_timestamp(alert_at) or datetime.now(UTC).isoformat(timespec="seconds"))
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE transactions
                SET metadata = json_set(
                    COALESCE(metadata, '{}'),
                    '$.issuance_sla_alert_sent',
                    json('true'),
                    '$.issuance_sla_alert_at',
                    json(?)
                )
                WHERE payment_id = ?
                  AND status = 'pending'
                  AND issuance_state = ?
                  AND COALESCE(json_extract(metadata, '$.payment_confirmed'), 0) = 1
                  AND COALESCE(json_extract(metadata, '$.issuance_sla_alert_sent'), 0) != 1
                """,
                (
                    alert_at_payload,
                    payment_id,
                    ISSUANCE_STATE_RETRYABLE_FAILED,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error:
        logger.exception("Failed to claim SLA alert for payment_id=%s", payment_id)
        return False


def get_stuck_unretried_transactions(
    *,
    min_age_seconds: int = 1800,
    limit: int = 20,
) -> list[dict]:
    """Return confirmed transactions that autoretry has never picked up.

    These are transactions where ``payment_confirmed = 1`` and
    ``retry_count = 0`` and old enough that they *should* have been retried
    but clearly haven't (indicating a filter silently rejected them).
    """
    safe_limit = max(1, min(int(limit or 20), 200))
    safe_age = max(60, int(min_age_seconds or 1800))
    age_expr = f"-{safe_age} seconds"

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    payment_id, user_id, payment_method, metadata,
                    retry_count, issuance_state, confirmed_at, created_date,
                    next_retry_at, last_error
                FROM transactions
                WHERE status = 'pending'
                  AND payment_id IS NOT NULL
                  AND TRIM(payment_id) <> ''
                  AND COALESCE(json_extract(metadata, '$.payment_confirmed'), 0) = 1
                  AND COALESCE(retry_count, 0) = 0
                  AND issuance_state IN (?, ?, ?)
                  AND datetime(COALESCE(confirmed_at, created_date)) <= datetime('now', ?)
                ORDER BY datetime(COALESCE(confirmed_at, created_date)) ASC
                LIMIT ?
                """,
                (
                    ISSUANCE_STATE_RETRYABLE_FAILED,
                    ISSUANCE_STATE_CONFIRMED,
                    ISSUANCE_STATE_AWAITING_PAYMENT,
                    age_expr,
                    safe_limit,
                ),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error("Failed to get stuck unretried transactions: %s", e)
        return []


def mark_transaction_paid(payment_id: str, payment_method: str | None = None) -> bool:
    """
    Finalize transaction status after successful key issuance.
    """
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE transactions
                SET status = 'paid',
                    issuance_state = ?,
                    next_retry_at = NULL,
                    last_error = NULL,
                    payment_method = COALESCE(?, payment_method)
                WHERE payment_id = ? AND status IN ('pending', 'processing', 'paid')
                """,
                (ISSUANCE_STATE_ISSUED, payment_method, payment_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Failed to mark transaction paid for payment_id={payment_id}: {e}")
        return False


def finalize_transaction_paid(
    payment_id: str,
    *,
    username: str | None = None,
    payment_method: str | None = None,
    amount_rub: float | None = None,
    metadata: str | None = None,
    created_date_override: datetime | str | None = None,
    bot_source: str | None = None,
) -> bool:
    """
    Finalize/refresh a payment transaction row in-place.
    Used to avoid creating duplicate 'paid' rows when the payment already has a pending record.
    """
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            metadata_dict = None
            try:
                parsed_metadata = json.loads(metadata) if isinstance(metadata, str) and metadata else None
                if isinstance(parsed_metadata, dict):
                    metadata_dict = parsed_metadata
            except Exception:
                metadata_dict = None
            resolved_bot_source = _resolve_bot_source(bot_source, metadata_dict)
            cursor.execute(
                """
                UPDATE transactions
                SET status = 'paid',
                    issuance_state = ?,
                    next_retry_at = NULL,
                    last_error = NULL,
                    username = CASE
                        WHEN ? IS NOT NULL AND TRIM(?) != '' THEN ?
                        ELSE username
                    END,
                    payment_method = COALESCE(?, payment_method),
                    amount_rub = COALESCE(?, amount_rub),
                    metadata = COALESCE(?, metadata),
                    created_date = COALESCE(?, created_date),
                    bot_source = COALESCE(?, bot_source)
                WHERE payment_id = ? AND status IN ('pending', 'processing', 'paid')
                """,
                (
                    ISSUANCE_STATE_ISSUED,
                    username,
                    username,
                    username,
                    payment_method,
                    amount_rub,
                    metadata,
                    (
                        created_date_override.isoformat()
                        if isinstance(created_date_override, datetime)
                        else created_date_override
                    ),
                    resolved_bot_source,
                    payment_id,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Failed to finalize transaction for payment_id={payment_id}: {e}")
        return False


def find_and_complete_ton_transaction(payment_id: str, amount_ton: float) -> dict | None:
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            with _create_connection() as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                cursor.execute("SELECT * FROM transactions WHERE payment_id = ? AND status = 'pending'", (payment_id,))
                transaction = cursor.fetchone()
                if not transaction:
                    logger.warning(f"TON Webhook: Received payment for unknown or completed payment_id: {payment_id}")
                    return None

                try:
                    metadata = json.loads(transaction["metadata"]) if transaction["metadata"] else {}
                    if not isinstance(metadata, dict):
                        metadata = {}
                except json.JSONDecodeError:
                    metadata = {}
                metadata["external_payment_id"] = payment_id
                metadata["payment_id"] = payment_id
                metadata["payment_confirmed"] = True
                metadata["payment_method"] = "TON Connect"

                cursor.execute(
                    """
                    UPDATE transactions
                    SET status = 'processing',
                        issuance_state = ?,
                        confirmed_at = COALESCE(confirmed_at, CURRENT_TIMESTAMP),
                        next_retry_at = NULL,
                        last_attempt_at = CURRENT_TIMESTAMP,
                        amount_currency = ?,
                        currency_name = 'TON',
                        payment_method = 'TON Connect',
                        metadata = ?
                    WHERE payment_id = ? AND status = 'pending'
                    """,
                    (
                        ISSUANCE_STATE_ISSUING,
                        amount_ton,
                        json.dumps(metadata),
                        payment_id,
                    ),
                )
                conn.commit()
                if cursor.rowcount == 0:
                    return None

                return metadata
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_attempts:
                delay = 0.25 * (2 ** (attempt - 1))
                logging.warning(
                    "Retry TON completion payment_id=%s attempt %s/%s after SQLITE_BUSY (%ss).",
                    payment_id,
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue
            logging.error(f"Failed to complete TON transaction {payment_id}: {e}")
            return None
        except sqlite3.Error as e:
            logging.error(f"Failed to complete TON transaction {payment_id}: {e}")
            return None
    return None


def log_transaction(
    username: str,
    transaction_id: str | None,
    payment_id: str | None,
    user_id: int,
    status: str,
    amount_rub: float,
    amount_currency: float | None,
    currency_name: str | None,
    payment_method: str,
    metadata: str,
    created_date_override: datetime | str | None = None,
    bot_source: str | None = None,
):
    try:
        resolved_payment_id = str(payment_id or "").strip()
        raw_meta = None
        if not resolved_payment_id:
            try:
                raw_meta = json.loads(metadata) if isinstance(metadata, str) else metadata
            except Exception:
                raw_meta = None
            if isinstance(raw_meta, dict):
                candidate = (
                    raw_meta.get("external_payment_id")
                    or raw_meta.get("payment_id")
                    or raw_meta.get("provider_payment_id")
                    or raw_meta.get("invoice_id")
                )
                if candidate:
                    resolved_payment_id = str(candidate).strip()
        else:
            try:
                raw_meta = json.loads(metadata) if isinstance(metadata, str) else metadata
            except Exception:
                raw_meta = None

        resolved_bot_source = _resolve_bot_source(
            bot_source,
            raw_meta if isinstance(raw_meta, dict) else None,
        )
        resolved_issuance_state = _issuance_state_for_status(status)

        if not resolved_payment_id:
            resolved_payment_id = str(uuid.uuid4())

        resolved_created_date = (
            created_date_override.isoformat()
            if isinstance(created_date_override, datetime)
            else (created_date_override or datetime.now())
        )

        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO transactions
                   (username, transaction_id, payment_id, user_id, status, amount_rub, amount_currency, currency_name, payment_method, metadata, created_date, bot_source, issuance_state, retry_count, next_retry_at, last_error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, NULL)
                   ON CONFLICT(payment_id) DO UPDATE SET
                       username = CASE
                           WHEN excluded.username IS NOT NULL AND TRIM(excluded.username) != '' THEN excluded.username
                           ELSE transactions.username
                       END,
                       user_id = excluded.user_id,
                       status = excluded.status,
                       amount_rub = excluded.amount_rub,
                       amount_currency = COALESCE(excluded.amount_currency, transactions.amount_currency),
                       currency_name = COALESCE(excluded.currency_name, transactions.currency_name),
                       payment_method = COALESCE(excluded.payment_method, transactions.payment_method),
                       metadata = COALESCE(excluded.metadata, transactions.metadata),
                       created_date = COALESCE(excluded.created_date, transactions.created_date),
                       bot_source = COALESCE(excluded.bot_source, transactions.bot_source),
                       issuance_state = COALESCE(excluded.issuance_state, transactions.issuance_state),
                       retry_count = CASE
                           WHEN excluded.issuance_state = ? THEN 0
                           ELSE transactions.retry_count
                       END,
                       next_retry_at = CASE
                           WHEN excluded.issuance_state = ? THEN NULL
                           ELSE transactions.next_retry_at
                       END,
                       last_error = CASE
                           WHEN excluded.issuance_state = ? THEN NULL
                           ELSE transactions.last_error
                       END
                """,
                (
                    username,
                    transaction_id,
                    resolved_payment_id,
                    user_id,
                    status,
                    amount_rub,
                    amount_currency,
                    currency_name,
                    payment_method,
                    metadata,
                    resolved_created_date,
                    resolved_bot_source,
                    resolved_issuance_state,
                    ISSUANCE_STATE_ISSUED,
                    ISSUANCE_STATE_ISSUED,
                    ISSUANCE_STATE_ISSUED,
                ),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to log transaction for user {user_id}: {e}")


def _add_transactions_period_filter(
    where_parts: list[str],
    query_params: list[object],
    *,
    days: int | None,
    date_from: str | None,
    date_to: str | None,
    table_alias: str = "t",
) -> tuple[str | None, str | None]:
    """
    Append transaction date filters.

    Returns:
      (effective_date_from, effective_date_to) as YYYY-MM-DD strings or None.
    """
    from_value = str(date_from).strip() if date_from else ""
    to_value = str(date_to).strip() if date_to else ""
    if from_value or to_value:
        if not from_value:
            from_value = to_value
        if not to_value:
            to_value = from_value
        if from_value > to_value:
            from_value, to_value = to_value, from_value
        where_parts.append(f"DATE({table_alias}.created_date) >= DATE(?)")
        query_params.append(from_value)
        where_parts.append(f"DATE({table_alias}.created_date) <= DATE(?)")
        query_params.append(to_value)
        return from_value, to_value

    if days is None:
        return None, None

    safe_days = max(1, int(days or 30))
    period_modifier = f"-{safe_days - 1} days"
    where_parts.append(f"DATE({table_alias}.created_date) >= DATE('now', ?)")
    query_params.append(period_modifier)
    return None, None


def get_paginated_transactions(
    page: int = 1,
    per_page: int = 15,
    tx_type: str = TX_TYPE_ALL,
    bot_source: str = BOT_SOURCE_ALL,
    status: str | None = None,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[list[dict], int]:
    offset = (page - 1) * per_page
    transactions = []
    total = 0
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            normalized_type = tx_type if tx_type in TX_TYPE_VALUES else TX_TYPE_ALL
            normalized_source = _normalize_bot_source_filter(bot_source)
            where_parts: list[str] = []
            query_params: list[object] = []

            if status:
                where_parts.append("t.status = ?")
                query_params.append(status)

            if normalized_type != TX_TYPE_ALL:
                where_parts.append(_tx_type_sql_condition(normalized_type, table_alias="t"))
            if normalized_source != BOT_SOURCE_ALL:
                where_parts.append("COALESCE(t.bot_source, 'main') = ?")
                query_params.append(normalized_source)

            _add_transactions_period_filter(
                where_parts,
                query_params,
                days=days,
                date_from=date_from,
                date_to=date_to,
                table_alias="t",
            )

            where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

            cursor.execute(
                f"SELECT COUNT(*) FROM transactions t {where_sql}",
                tuple(query_params),
            )
            total = cursor.fetchone()[0]

            query = f"""
                SELECT t.*, u.username AS user_table_username
                FROM transactions t
                LEFT JOIN users u ON u.telegram_id = t.user_id
                {where_sql}
                ORDER BY t.created_date DESC
                LIMIT ? OFFSET ?
            """
            cursor.execute(query, tuple(query_params + [per_page, offset]))

            for row in cursor.fetchall():
                transaction_dict = dict(row)
                transaction_dict["username"] = (
                    transaction_dict.get("username") or transaction_dict.get("user_table_username") or "N/A"
                )

                metadata_str = transaction_dict.get("metadata")
                if metadata_str:
                    try:
                        metadata = json.loads(metadata_str)
                        transaction_dict["host_name"] = metadata.get("host_name", "N/A")
                        transaction_dict["plan_name"] = metadata.get("plan_name", "N/A")
                        transaction_dict["tx_type"] = classify_product_type(
                            transaction_dict["host_name"],
                            transaction_dict["plan_name"],
                        )
                    except json.JSONDecodeError:
                        transaction_dict["host_name"] = "Error"
                        transaction_dict["plan_name"] = "Error"
                        transaction_dict["tx_type"] = TX_TYPE_VLESS
                else:
                    transaction_dict["host_name"] = "N/A"
                    transaction_dict["plan_name"] = "N/A"
                    transaction_dict["tx_type"] = TX_TYPE_VLESS

                transactions.append(transaction_dict)

    except sqlite3.Error as e:
        logging.error(f"Failed to get paginated transactions: {e}")

    return transactions, total


def get_transaction_type_summary(
    status: str | None = "paid",
    *,
    bot_source: str = BOT_SOURCE_ALL,
    days: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, int]:
    summary = {
        TX_TYPE_HYSTERIA: 0,
        TX_TYPE_SUPER: 0,
        TX_TYPE_OLD: 0,
        TX_TYPE_VLESS: 0,
    }
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            where_parts: list[str] = []
            params: list[object] = []
            if status:
                where_parts.append("status = ?")
                params.append(status)
            normalized_source = _normalize_bot_source_filter(bot_source)
            if normalized_source != BOT_SOURCE_ALL:
                where_parts.append("COALESCE(bot_source, 'main') = ?")
                params.append(normalized_source)
            _add_transactions_period_filter(
                where_parts,
                params,
                days=days,
                date_from=date_from,
                date_to=date_to,
                table_alias="transactions",
            )
            where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""
            cursor.execute(
                f"""
                SELECT metadata, COUNT(*) AS cnt
                FROM transactions
                {where_sql}
                GROUP BY metadata
                """,
                tuple(params),
            )
            for row in cursor.fetchall():
                metadata = {}
                raw_metadata = row["metadata"]
                if raw_metadata:
                    try:
                        parsed = json.loads(raw_metadata)
                        if isinstance(parsed, dict):
                            metadata = parsed
                    except json.JSONDecodeError:
                        metadata = {}
                tx_type = classify_product_type(
                    metadata.get("host_name"),
                    metadata.get("plan_name"),
                )
                if tx_type == TX_TYPE_ALL:
                    continue
                summary[tx_type] = summary.get(tx_type, 0) + int(row["cnt"])
    except sqlite3.Error as e:
        logging.error(f"Failed to get transaction type summary: {e}")
    return summary


def set_trial_used(telegram_id: int):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET trial_used = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            logging.info(f"Trial period marked as used for user {telegram_id}.")
    except sqlite3.Error as e:
        logging.error(f"Failed to set trial used for user {telegram_id}: {e}")


def set_trial_super_used(telegram_id: int):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET trial_super_used = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            logging.info(f"Super trial period marked as used for user {telegram_id}.")
    except sqlite3.Error as e:
        logging.error(f"Failed to set super trial used for user {telegram_id}: {e}")


def add_new_key(
    user_id: int,
    host_name: str,
    xui_client_uuid: str,
    key_email: str,
    expiry_timestamp_ms: int,
    issued_key_value: str | None = None,
    created_date: datetime | str | None = None,
    bot_source: str | None = None,
    panel_sub_id: str | None = None,
    subscription_payload: str | None = None,
    subscription_tier: str | None = None,
    whitelist_cycle_anchor_at: datetime | str | None = None,
):
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            with _create_connection() as conn:
                cursor = conn.cursor()
                expiry_date = datetime.fromtimestamp(expiry_timestamp_ms / 1000)
                resolved_bot_source = _resolve_bot_source(bot_source)
                cycle_anchor_value = (
                    whitelist_cycle_anchor_at.isoformat()
                    if isinstance(whitelist_cycle_anchor_at, datetime)
                    else whitelist_cycle_anchor_at
                )
                if created_date is not None:
                    created_value = (
                        created_date.isoformat() if isinstance(created_date, datetime) else str(created_date)
                    )
                    cursor.execute(
                        """
                        INSERT INTO vpn_keys
                        (user_id, host_name, xui_client_uuid, key_email, issued_key_value, subscription_payload, panel_sub_id, subscription_tier, expiry_date, created_date, whitelist_cycle_anchor_at, bot_source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            host_name,
                            xui_client_uuid,
                            key_email,
                            issued_key_value,
                            subscription_payload,
                            panel_sub_id,
                            subscription_tier,
                            expiry_date,
                            created_value,
                            cycle_anchor_value,
                            resolved_bot_source,
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        INSERT INTO vpn_keys
                        (user_id, host_name, xui_client_uuid, key_email, issued_key_value, subscription_payload, panel_sub_id, subscription_tier, expiry_date, whitelist_cycle_anchor_at, bot_source)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            user_id,
                            host_name,
                            xui_client_uuid,
                            key_email,
                            issued_key_value,
                            subscription_payload,
                            panel_sub_id,
                            subscription_tier,
                            expiry_date,
                            cycle_anchor_value,
                            resolved_bot_source,
                        ),
                    )
                new_key_id = int(cursor.lastrowid or 0)
                conn.commit()
                return new_key_id or None
        except sqlite3.IntegrityError as e:
            # Idempotency: if key with same email already exists for this user,
            # treat as successful reuse instead of hard failure.
            try:
                with _create_connection() as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT key_id, user_id FROM vpn_keys WHERE key_email = ? LIMIT 1",
                        (key_email,),
                    )
                    row = cursor.fetchone()
                    if row and int(row["user_id"] or 0) == int(user_id):
                        existing_key_id = int(row["key_id"] or 0) or None
                        if existing_key_id and (panel_sub_id or subscription_payload or subscription_tier):
                            cursor.execute(
                                """
                                UPDATE vpn_keys
                                SET
                                    panel_sub_id = COALESCE(?, panel_sub_id),
                                    subscription_payload = COALESCE(?, subscription_payload),
                                    subscription_tier = COALESCE(?, subscription_tier)
                                WHERE key_id = ?
                                """,
                                (panel_sub_id, subscription_payload, subscription_tier, existing_key_id),
                            )
                            conn.commit()
                        return existing_key_id
            except sqlite3.Error:
                pass
            logging.error(
                "Failed to add new key for user %s (email=%s) due integrity error: %s",
                user_id,
                key_email,
                e,
            )
            return None
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_attempts:
                delay = 0.25 * (2 ** (attempt - 1))
                logging.warning(
                    "Retry add_new_key user_id=%s email=%s attempt %s/%s after SQLITE_BUSY (%ss).",
                    user_id,
                    key_email,
                    attempt,
                    max_attempts,
                    delay,
                )
                time.sleep(delay)
                continue
            logging.error("Failed to add new key for user %s (email=%s): %s", user_id, key_email, e)
            return None
        except sqlite3.Error as e:
            logging.error("Failed to add new key for user %s (email=%s): %s", user_id, key_email, e)
            return None
    return None


def delete_key_by_email(email: str):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM vpn_keys WHERE key_email = ?", (email,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to delete key '{email}': {e}")


def get_user_keys(user_id: int):
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE user_id = ? ORDER BY key_id", (user_id,))
            keys = cursor.fetchall()
            return [dict(key) for key in keys]
    except sqlite3.Error as e:
        logging.error(f"Failed to get keys for user {user_id}: {e}")
        return []


def get_key_by_id(key_id: int):
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE key_id = ?", (key_id,))
            key_data = cursor.fetchone()
            return dict(key_data) if key_data else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get key by ID {key_id}: {e}")
        return None


def get_key_by_email(key_email: str):
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE key_email = ?", (key_email,))
            key_data = cursor.fetchone()
            return dict(key_data) if key_data else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get key by email {key_email}: {e}")
        return None


def get_active_key_by_panel_sub_id(panel_sub_id: str):
    normalized_sub_id = str(panel_sub_id or "").strip()
    if not normalized_sub_id:
        return None
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT *
                FROM vpn_keys
                WHERE panel_sub_id = ?
                  AND COALESCE(is_archived, 0) = 0
                  AND expiry_date > CURRENT_TIMESTAMP
                ORDER BY key_id DESC
                LIMIT 1
                """,
                (normalized_sub_id,),
            )
            key_data = cursor.fetchone()
            if key_data:
                return dict(key_data)

            escaped_sub_id = (
                normalized_sub_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            like_pattern = f"%/podpiska/{escaped_sub_id}%"
            cursor.execute(
                """
                SELECT *
                FROM vpn_keys
                WHERE COALESCE(is_archived, 0) = 0
                  AND expiry_date > CURRENT_TIMESTAMP
                  AND (
                    issued_key_value LIKE ? ESCAPE '\\'
                    OR subscription_payload LIKE ? ESCAPE '\\'
                  )
                ORDER BY key_id DESC
                LIMIT 200
                """,
                (like_pattern, like_pattern),
            )
            sub_id_pattern = re.compile(r"/podpiska/([^\s/?#]+)")
            for row in cursor.fetchall():
                key = dict(row)
                managed_sub_ids: set[str] = set()
                for field_name in ("issued_key_value", "subscription_payload"):
                    raw_value = str(key.get(field_name) or "")
                    managed_sub_ids.update(match.group(1).strip().strip("/") for match in sub_id_pattern.finditer(raw_value))
                if normalized_sub_id not in managed_sub_ids:
                    continue
                if not str(key.get("panel_sub_id") or "").strip():
                    cursor.execute(
                        "UPDATE vpn_keys SET panel_sub_id = ? WHERE key_id = ?",
                        (normalized_sub_id, key["key_id"]),
                    )
                    conn.commit()
                    key["panel_sub_id"] = normalized_sub_id
                return key
            return None
    except sqlite3.Error:
        logger.exception("Failed to get active key by panel_sub_id")
        return None


def get_key_by_panel_sub_id(panel_sub_id: str):
    """Return a managed-subscription key regardless of expiry/archive state.

    This lookup intentionally does not mutate legacy rows.  It is used as a
    deny-list guard before the panel-only subscription fallback: a known
    expired or archived subscription must not become active merely because a
    matching client still exists on the panel.
    """
    normalized_sub_id = str(panel_sub_id or "").strip()
    if not normalized_sub_id:
        return None
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT *
                FROM vpn_keys
                WHERE panel_sub_id = ?
                ORDER BY key_id DESC
                LIMIT 1
                """,
                (normalized_sub_id,),
            )
            key_data = cursor.fetchone()
            if key_data:
                return dict(key_data)

            escaped_sub_id = (
                normalized_sub_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            like_pattern = f"%/podpiska/{escaped_sub_id}%"
            cursor.execute(
                """
                SELECT *
                FROM vpn_keys
                WHERE issued_key_value LIKE ? ESCAPE '\\'
                   OR subscription_payload LIKE ? ESCAPE '\\'
                ORDER BY key_id DESC
                LIMIT 200
                """,
                (like_pattern, like_pattern),
            )
            sub_id_pattern = re.compile(r"/podpiska/([^\s/?#]+)")
            for row in cursor.fetchall():
                key = dict(row)
                for field_name in ("issued_key_value", "subscription_payload"):
                    raw_value = str(key.get(field_name) or "")
                    if any(
                        match.group(1).strip().strip("/") == normalized_sub_id
                        for match in sub_id_pattern.finditer(raw_value)
                    ):
                        return key
            return None
    except sqlite3.Error:
        logger.exception("Failed to get key by panel_sub_id")
        return None


def was_expiry_notification_sent(key_id: int, expiry_date: datetime | str | None) -> bool:
    if expiry_date is None:
        return False
    expiry_text = expiry_date.isoformat() if isinstance(expiry_date, datetime) else str(expiry_date)
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 1
                FROM key_expiry_notifications
                WHERE key_id = ? AND expiry_date = ?
                LIMIT 1
                """,
                (key_id, expiry_text),
            )
            return cursor.fetchone() is not None
    except sqlite3.Error as e:
        logging.error(
            "Failed to check expiry notification for key_id=%s, expiry_date=%s: %s",
            key_id,
            expiry_text,
            e,
        )
        return False


def mark_expiry_notification_sent(key_id: int, expiry_date: datetime | str | None) -> None:
    if expiry_date is None:
        return
    expiry_text = expiry_date.isoformat() if isinstance(expiry_date, datetime) else str(expiry_date)
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO key_expiry_notifications (key_id, expiry_date, notified_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                """,
                (key_id, expiry_text),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(
            "Failed to mark expiry notification for key_id=%s, expiry_date=%s: %s",
            key_id,
            expiry_text,
            e,
        )


def prune_expiry_notifications(retention_days: int = 120) -> int:
    if retention_days <= 0:
        retention_days = 1
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM key_expiry_notifications WHERE notified_at < datetime('now', ?)",
                (f"-{retention_days} days",),
            )
            conn.commit()
            return cursor.rowcount or 0
    except sqlite3.Error as e:
        logging.error("Failed to prune expiry notifications: %s", e)
        return 0


def update_key_info(
    key_id: int,
    new_xui_uuid: str,
    new_expiry_ms: int,
    issued_key_value: str | None = None,
    new_key_email: str | None = None,
    panel_sub_id: str | None = None,
    subscription_payload: str | None = None,
    subscription_tier: str | None = None,
    new_whitelist_cycle_anchor_at: datetime | str | None = None,
) -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            expiry_date = datetime.fromtimestamp(new_expiry_ms / 1000)
            cycle_anchor_value = (
                new_whitelist_cycle_anchor_at.isoformat()
                if isinstance(new_whitelist_cycle_anchor_at, datetime)
                else new_whitelist_cycle_anchor_at
            )
            cursor.execute(
                """
                UPDATE vpn_keys
                SET
                    xui_client_uuid = ?,
                    expiry_date = ?,
                    key_email = COALESCE(?, key_email),
                    issued_key_value = COALESCE(?, issued_key_value),
                    panel_sub_id = COALESCE(?, panel_sub_id),
                    subscription_payload = COALESCE(?, subscription_payload),
                    subscription_tier = COALESCE(?, subscription_tier),
                    whitelist_cycle_anchor_at = COALESCE(?, whitelist_cycle_anchor_at)
                WHERE key_id = ?
                """,
                (
                    new_xui_uuid,
                    expiry_date,
                    new_key_email,
                    issued_key_value,
                    panel_sub_id,
                    subscription_payload,
                    subscription_tier,
                    cycle_anchor_value,
                    key_id,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Failed to update key {key_id}: {e}")
        return False


def update_key_host_and_uuid(
    key_id: int,
    new_host_name: str,
    new_uuid: str,
    new_expiry_ms: int,
    issued_key_value: str | None = None,
    panel_sub_id: str | None = None,
    subscription_payload: str | None = None,
    subscription_tier: str | None = None,
):
    """Update a key's host, UUID, and expiry (used for Blitz migration)."""
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            expiry_date = datetime.fromtimestamp(new_expiry_ms / 1000)
            cursor.execute(
                """
                UPDATE vpn_keys
                SET
                    host_name = ?,
                    xui_client_uuid = ?,
                    expiry_date = ?,
                    issued_key_value = COALESCE(?, issued_key_value),
                    panel_sub_id = COALESCE(?, panel_sub_id),
                    subscription_payload = COALESCE(?, subscription_payload),
                    subscription_tier = COALESCE(?, subscription_tier)
                WHERE key_id = ?
                """,
                (
                    new_host_name,
                    new_uuid,
                    expiry_date,
                    issued_key_value,
                    panel_sub_id,
                    subscription_payload,
                    subscription_tier,
                    key_id,
                ),
            )
            conn.commit()
            logging.info(f"Migrated key {key_id} to host '{new_host_name}' (uuid: {new_uuid})")
    except sqlite3.Error as e:
        logging.error(f"Failed to update key host for key {key_id}: {e}")


def update_issued_key_value(key_id: int, issued_key_value: str):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE vpn_keys SET issued_key_value = ? WHERE key_id = ?",
                (issued_key_value, key_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to update issued key value for key {key_id}: {e}")


def update_key_subscription_payload(key_id: int, subscription_payload: str) -> None:
    payload = str(subscription_payload or "").strip()
    if not payload:
        return
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE vpn_keys SET subscription_payload = ? WHERE key_id = ?",
                (payload, key_id),
            )
            conn.commit()
    except sqlite3.Error:
        logger.exception("Failed to update subscription payload for key %s", key_id)


def clear_key_subscription_payload(key_id: int) -> None:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE vpn_keys SET subscription_payload = NULL WHERE key_id = ?", (key_id,))
            conn.commit()
    except sqlite3.Error:
        logger.exception("Failed to clear subscription payload for key %s", key_id)


def upsert_mainpanel_inbound_snapshot(host_name: str, inbounds: list[dict]) -> None:
    normalized_host = str(host_name or "").strip()
    if not normalized_host:
        return
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            _ensure_mainpanel_inbound_sync_tables(cursor)
            current_inbound_ids: set[int] = set()
            for inbound in inbounds:
                inbound_id = int(inbound.get("inbound_id") or inbound.get("id") or 0)
                if inbound_id <= 0:
                    continue
                current_inbound_ids.add(inbound_id)
                cursor.execute(
                    """
                    INSERT INTO mainpanel_inbound_snapshots (
                        host_name, inbound_id, remark, tag, protocol, enable, classification, settings_hash
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(host_name, inbound_id) DO UPDATE SET
                        remark = excluded.remark,
                        tag = excluded.tag,
                        protocol = excluded.protocol,
                        enable = excluded.enable,
                        classification = excluded.classification,
                        settings_hash = excluded.settings_hash,
                        last_seen_at = CURRENT_TIMESTAMP
                    """,
                    (
                        normalized_host,
                        inbound_id,
                        str(inbound.get("remark") or ""),
                        str(inbound.get("tag") or ""),
                        str(inbound.get("protocol") or ""),
                        1 if inbound.get("enable") else 0,
                        str(inbound.get("classification") or ""),
                        str(inbound.get("settings_hash") or ""),
                    ),
                )
            if current_inbound_ids:
                cursor.execute(
                    """
                    SELECT inbound_id
                    FROM mainpanel_inbound_snapshots
                    WHERE host_name = ?
                    """,
                    (normalized_host,),
                )
                stale_inbound_ids = [int(row[0]) for row in cursor.fetchall() if int(row[0]) not in current_inbound_ids]
                for stale_inbound_id in stale_inbound_ids:
                    cursor.execute(
                        """
                        DELETE FROM mainpanel_inbound_snapshots
                        WHERE host_name = ?
                          AND inbound_id = ?
                        """,
                        (normalized_host, stale_inbound_id),
                    )
            conn.commit()
    except sqlite3.Error:
        logger.exception("Failed to upsert MainPanel inbound snapshot for host '%s'", normalized_host)


def get_mainpanel_inbound_snapshot(host_name: str) -> dict[int, dict]:
    normalized_host = str(host_name or "").strip()
    if not normalized_host:
        return {}
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            _ensure_mainpanel_inbound_sync_tables(cursor)
            cursor.execute(
                "SELECT * FROM mainpanel_inbound_snapshots WHERE host_name = ?",
                (normalized_host,),
            )
            return {int(row["inbound_id"]): dict(row) for row in cursor.fetchall()}
    except sqlite3.Error:
        logger.exception("Failed to load MainPanel inbound snapshot for host '%s'", normalized_host)
        return {}


def create_mainpanel_inbound_sync_run(
    *,
    host_name: str,
    mode: str,
    batch_limit: int = 0,
    run_id: str | None = None,
    status: str = "running",
) -> str:
    normalized_run_id = str(run_id or uuid.uuid4().hex).strip()
    try:
        safe_batch_limit = max(0, int(batch_limit or 0))
    except (TypeError, ValueError):
        safe_batch_limit = 0
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            _ensure_mainpanel_inbound_sync_tables(cursor)
            cursor.execute(
                """
                INSERT INTO mainpanel_inbound_sync_runs (
                    run_id, host_name, mode, status, batch_limit
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    normalized_run_id,
                    str(host_name or "").strip(),
                    str(mode or "").strip() or "manual",
                    str(status or "").strip() or "running",
                    safe_batch_limit,
                ),
            )
            conn.commit()
    except sqlite3.Error:
        logger.exception("Failed to create MainPanel inbound sync run '%s'", normalized_run_id)
    return normalized_run_id


def add_mainpanel_inbound_sync_item(run_id: str, item: dict) -> None:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            _ensure_mainpanel_inbound_sync_tables(cursor)
            cursor.execute(
                """
                INSERT INTO mainpanel_inbound_sync_items (
                    run_id, key_id, user_id, email, inbound_id, inbound_label, classification,
                    tier, status, action, error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_run_id,
                    item.get("key_id"),
                    item.get("user_id"),
                    str(item.get("email") or ""),
                    item.get("inbound_id"),
                    str(item.get("inbound_label") or ""),
                    str(item.get("classification") or ""),
                    str(item.get("tier") or ""),
                    str(item.get("status") or ""),
                    str(item.get("action") or ""),
                    str(item.get("error") or ""),
                ),
            )
            conn.commit()
    except sqlite3.Error:
        logger.exception("Failed to add MainPanel inbound sync item for run '%s'", normalized_run_id)


def update_mainpanel_inbound_sync_run(
    run_id: str,
    *,
    status: str | None = None,
    processed: int | None = None,
    updated: int | None = None,
    skipped: int | None = None,
    failed: int | None = None,
    needs_review: int | None = None,
    error: str | None = None,
    completed: bool = False,
) -> None:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return
    if (
        status is None
        and processed is None
        and updated is None
        and skipped is None
        and failed is None
        and needs_review is None
        and error is None
        and not completed
    ):
        return
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            _ensure_mainpanel_inbound_sync_tables(cursor)
            cursor.execute(
                """
                UPDATE mainpanel_inbound_sync_runs
                SET
                    status = COALESCE(?, status),
                    processed = COALESCE(?, processed),
                    updated = COALESCE(?, updated),
                    skipped = COALESCE(?, skipped),
                    failed = COALESCE(?, failed),
                    needs_review = COALESCE(?, needs_review),
                    error = COALESCE(?, error),
                    completed_at = CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE completed_at END
                WHERE run_id = ?
                """,
                (
                    status,
                    processed,
                    updated,
                    skipped,
                    failed,
                    needs_review,
                    error,
                    completed,
                    normalized_run_id,
                ),
            )
            conn.commit()
    except sqlite3.Error:
        logger.exception("Failed to update MainPanel inbound sync run '%s'", normalized_run_id)


def get_mainpanel_inbound_sync_run(run_id: str, *, include_items: bool = True) -> dict | None:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return None
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            _ensure_mainpanel_inbound_sync_tables(cursor)
            cursor.execute("SELECT * FROM mainpanel_inbound_sync_runs WHERE run_id = ?", (normalized_run_id,))
            row = cursor.fetchone()
            if not row:
                return None
            result = dict(row)
            if include_items:
                cursor.execute(
                    """
                    SELECT * FROM mainpanel_inbound_sync_items
                    WHERE run_id = ?
                    ORDER BY item_id
                    """,
                    (normalized_run_id,),
                )
                result["items"] = [dict(item) for item in cursor.fetchall()]
            return result
    except sqlite3.Error:
        logger.exception("Failed to load MainPanel inbound sync run '%s'", normalized_run_id)
        return None


def update_key_host_name(key_id: int, new_host_name: str) -> None:
    """Update a key's host_name only (used for legacy host renames)."""
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE vpn_keys SET host_name = ? WHERE key_id = ?", (new_host_name, key_id))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to update key host_name for key {key_id}: {e}")


def get_next_key_number(user_id: int) -> int:
    """
    Return the next key number for user-generated emails like:
      user<id>-key<N>@...

    Uses max observed N from existing key_email values and falls back to key count.
    This avoids collisions for users whose historical emails started from key2/key3.
    """
    keys = get_user_keys(user_id)
    if not keys:
        return 1

    max_observed = 0
    pattern = re.compile(rf"^user{re.escape(str(user_id))}-key(\d+)@.+$", re.IGNORECASE)
    existing_emails = set()

    for key in keys:
        email = str(key.get("key_email") or "").strip()
        if not email:
            continue
        existing_emails.add(email.lower())
        match = pattern.match(email)
        if not match:
            continue
        try:
            max_observed = max(max_observed, int(match.group(1)))
        except ValueError:
            continue

    candidate = max(max_observed + 1, len(keys) + 1)
    # Keep "vpn.bot" collision-free because this domain is used for paid/incident flows.
    while f"user{user_id}-key{candidate}@vpn.bot".lower() in existing_emails:
        candidate += 1
    return candidate


def get_keys_for_host(host_name: str) -> list[dict]:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE host_name = ?", (host_name,))
            keys = cursor.fetchall()
            return [dict(key) for key in keys]
    except sqlite3.Error as e:
        logging.error(f"Failed to get keys for host '{host_name}': {e}")
        return []


def get_all_vpn_users():
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT user_id FROM vpn_keys")
            users = cursor.fetchall()
            return [dict(user) for user in users]
    except sqlite3.Error as e:
        logging.error(f"Failed to get all vpn users: {e}")
        return []


def update_key_status_from_server(key_email: str, xui_client_data):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            if xui_client_data:
                expiry_date = datetime.fromtimestamp(xui_client_data.expiry_time / 1000)
                cursor.execute(
                    "UPDATE vpn_keys SET xui_client_uuid = ?, expiry_date = ? WHERE key_email = ?",
                    (xui_client_data.id, expiry_date, key_email),
                )
            else:
                cursor.execute("DELETE FROM vpn_keys WHERE key_email = ?", (key_email,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to update key status for {key_email}: {e}")


def get_daily_stats_for_charts(days: int = 30) -> dict:
    stats = {"users": {}, "keys": {}}
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            query_users = """
                SELECT date(registration_date) as day, COUNT(*)
                FROM users
                WHERE registration_date >= date('now', ?)
                GROUP BY day
                ORDER BY day;
            """
            cursor.execute(query_users, (f"-{days} days",))
            for row in cursor.fetchall():
                stats["users"][row[0]] = row[1]

            query_keys = """
                SELECT date(created_date) as day, COUNT(*)
                FROM vpn_keys
                WHERE created_date >= date('now', ?)
                GROUP BY day
                ORDER BY day;
            """
            cursor.execute(query_keys, (f"-{days} days",))
            for row in cursor.fetchall():
                stats["keys"][row[0]] = row[1]
    except sqlite3.Error as e:
        logging.error(f"Failed to get daily stats for charts: {e}")
    return stats


def get_dashboard_analytics(
    days: int = 30,
    tx_type: str = TX_TYPE_ALL,
    bot_source: str = BOT_SOURCE_ALL,
    status: str | None = "paid",
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    safe_days = max(1, int(days or 30))
    normalized_type = tx_type if tx_type in TX_TYPE_VALUES else TX_TYPE_ALL
    normalized_source = _normalize_bot_source_filter(bot_source)
    where_parts: list[str] = []
    where_params: list[object] = []

    effective_date_from, effective_date_to = _add_transactions_period_filter(
        where_parts,
        where_params,
        days=safe_days,
        date_from=date_from,
        date_to=date_to,
        table_alias="t",
    )

    if status:
        where_parts.append("t.status = ?")
        where_params.append(status)

    if normalized_type != TX_TYPE_ALL:
        where_parts.append(_tx_type_sql_condition(normalized_type, table_alias="t"))
    if normalized_source != BOT_SOURCE_ALL:
        where_parts.append("COALESCE(t.bot_source, 'main') = ?")
        where_params.append(normalized_source)

    where_sql = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    result = {
        "days": safe_days,
        "tx_type": normalized_type,
        "bot_source": normalized_source,
        "date_from": effective_date_from,
        "date_to": effective_date_to,
        "transactions_by_day": {},
        "revenue_by_day": {},
        "totals": {
            "transactions": 0,
            "revenue_rub": 0.0,
            "avg_check_rub": 0.0,
            "buyers": 0,
        },
    }

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                f"""
                SELECT
                    DATE(t.created_date) AS day,
                    COUNT(*) AS tx_count,
                    COALESCE(SUM(t.amount_rub), 0) AS revenue_rub
                FROM transactions t
                {where_sql}
                GROUP BY day
                ORDER BY day
                """,
                tuple(where_params),
            )
            for row in cursor.fetchall():
                day = row["day"]
                result["transactions_by_day"][day] = int(row["tx_count"] or 0)
                result["revenue_by_day"][day] = float(row["revenue_rub"] or 0.0)

            cursor.execute(
                f"""
                SELECT
                    COUNT(*) AS tx_count,
                    COALESCE(SUM(t.amount_rub), 0) AS revenue_rub,
                    COALESCE(AVG(t.amount_rub), 0) AS avg_check_rub,
                    COUNT(DISTINCT t.user_id) AS buyers_count
                FROM transactions t
                {where_sql}
                """,
                tuple(where_params),
            )
            totals_row = cursor.fetchone()
            if totals_row:
                result["totals"] = {
                    "transactions": int(totals_row["tx_count"] or 0),
                    "revenue_rub": float(totals_row["revenue_rub"] or 0.0),
                    "avg_check_rub": float(totals_row["avg_check_rub"] or 0.0),
                    "buyers": int(totals_row["buyers_count"] or 0),
                }
    except sqlite3.Error as e:
        logging.error("Failed to get dashboard analytics: %s", e)

    return result


def get_trial_conversion_analytics(
    *,
    days: int = 30,
    tx_type: str = TX_TYPE_ALL,
    bot_source: str = BOT_SOURCE_ALL,
    status: str | None = "paid",
    date_from: str | None = None,
    date_to: str | None = None,
    sample_limit: int = 10,
) -> dict:
    normalized_type = tx_type if tx_type in TX_TYPE_VALUES else TX_TYPE_ALL
    normalized_source = _normalize_bot_source_filter(bot_source)
    safe_days = max(1, int(days or 30))
    result = {
        "trial_users_total": 0,
        "converted_users_total": 0,
        "conversion_rate_total": 0.0,
        "period_converted_users": 0,
        "period_transactions": 0,
        "period_revenue_rub": 0.0,
        "sample": [],
    }

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT COUNT(*)
                FROM users u
                WHERE COALESCE(u.trial_used, 0) = 1 OR COALESCE(u.trial_super_used, 0) = 1
                """
            )
            trial_total = int((cursor.fetchone() or [0])[0] or 0)
            result["trial_users_total"] = trial_total

            all_time_where_parts = ["(COALESCE(u.trial_used, 0) = 1 OR COALESCE(u.trial_super_used, 0) = 1)"]
            all_time_params: list[object] = []
            if status:
                all_time_where_parts.append("t.status = ?")
                all_time_params.append(status)
            if normalized_type != TX_TYPE_ALL:
                all_time_where_parts.append(_tx_type_sql_condition(normalized_type, table_alias="t"))
            if normalized_source != BOT_SOURCE_ALL:
                all_time_where_parts.append("COALESCE(t.bot_source, 'main') = ?")
                all_time_params.append(normalized_source)
            all_time_where_sql = f"WHERE {' AND '.join(all_time_where_parts)}"

            cursor.execute(
                f"""
                SELECT COUNT(DISTINCT t.user_id)
                FROM transactions t
                JOIN users u ON u.telegram_id = t.user_id
                {all_time_where_sql}
                """,
                tuple(all_time_params),
            )
            converted_total = int((cursor.fetchone() or [0])[0] or 0)
            result["converted_users_total"] = converted_total
            result["conversion_rate_total"] = round((converted_total / trial_total) * 100, 1) if trial_total else 0.0

            period_where_parts = list(all_time_where_parts)
            period_params = list(all_time_params)
            _add_transactions_period_filter(
                period_where_parts,
                period_params,
                days=safe_days,
                date_from=date_from,
                date_to=date_to,
                table_alias="t",
            )
            period_where_sql = f"WHERE {' AND '.join(period_where_parts)}"

            cursor.execute(
                f"""
                SELECT
                    COUNT(*) AS tx_count,
                    COALESCE(SUM(t.amount_rub), 0) AS revenue_rub,
                    COUNT(DISTINCT t.user_id) AS buyers_count
                FROM transactions t
                JOIN users u ON u.telegram_id = t.user_id
                {period_where_sql}
                """,
                tuple(period_params),
            )
            period_row = cursor.fetchone()
            if period_row:
                result["period_transactions"] = int(period_row["tx_count"] or 0)
                result["period_revenue_rub"] = float(period_row["revenue_rub"] or 0.0)
                result["period_converted_users"] = int(period_row["buyers_count"] or 0)

            safe_sample_limit = int(sample_limit or 0)
            if safe_sample_limit > 0:
                cursor.execute(
                    f"""
                    SELECT
                        t.user_id,
                        COALESCE(MAX(NULLIF(TRIM(t.username), '')), MAX(u.username), 'N/A') AS username,
                        COUNT(*) AS tx_count,
                        COALESCE(SUM(t.amount_rub), 0) AS revenue_rub,
                        MIN(t.created_date) AS first_paid_date,
                        MAX(t.created_date) AS last_paid_date
                    FROM transactions t
                    JOIN users u ON u.telegram_id = t.user_id
                    {period_where_sql}
                    GROUP BY t.user_id
                    ORDER BY last_paid_date DESC
                    LIMIT ?
                    """,
                    tuple(period_params + [safe_sample_limit]),
                )
                result["sample"] = [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error("Failed to get trial conversion analytics: %s", e)

    return result


def get_recent_transactions(limit: int = 15, bot_source: str = BOT_SOURCE_ALL) -> list[dict]:
    transactions = []
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            normalized_source = _normalize_bot_source_filter(bot_source)
            query_parts = [
                """
                SELECT
                    k.key_id,
                    k.host_name,
                    k.created_date,
                    k.bot_source,
                    u.telegram_id,
                    u.username
                FROM vpn_keys k
                JOIN users u ON k.user_id = u.telegram_id
                """
            ]
            query_params: list[object] = []
            if normalized_source != BOT_SOURCE_ALL:
                query_parts.append("WHERE COALESCE(k.bot_source, 'main') = ?")
                query_params.append(normalized_source)
            query_parts.append("ORDER BY k.created_date DESC LIMIT ?")
            query_params.append(limit)
            cursor.execute(" ".join(query_parts), tuple(query_params))
            transactions = [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Failed to get recent transactions: {e}")
    return transactions


def add_support_thread(user_id: int, thread_id: int):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO support_threads (user_id, thread_id) VALUES (?, ?)", (user_id, thread_id)
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to add support thread for user {user_id}: {e}")


def get_support_thread_id(user_id: int) -> int | None:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT thread_id FROM support_threads WHERE user_id = ?", (user_id,))
            result = cursor.fetchone()
            return result[0] if result else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get support thread_id for user {user_id}: {e}")
        return None


def get_user_id_by_thread(thread_id: int) -> int | None:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM support_threads WHERE thread_id = ?", (thread_id,))
            result = cursor.fetchone()
            return result[0] if result else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get user_id for thread {thread_id}: {e}")
        return None


def get_user_transactions(
    user_id: int,
    limit: int = 5,
    statuses: tuple[str, ...] | None = ("paid",),
    bot_source: str = BOT_SOURCE_ALL,
) -> list[dict]:
    """Return recent user transactions, optionally filtered by status."""
    try:
        normalized_limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        normalized_limit = 5

    normalized_statuses: list[str] = []
    if statuses:
        normalized_statuses = [str(status).strip().lower() for status in statuses if str(status).strip()]

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            normalized_source = _normalize_bot_source_filter(bot_source)

            query_parts = ["SELECT * FROM transactions WHERE user_id = ?"]
            query_params: list[object] = [user_id]

            if normalized_statuses:
                placeholders = ",".join("?" for _ in normalized_statuses)
                query_parts.append(f"AND status IN ({placeholders})")
                query_params.extend(normalized_statuses)
            if normalized_source != BOT_SOURCE_ALL:
                query_parts.append("AND COALESCE(bot_source, 'main') = ?")
                query_params.append(normalized_source)

            query_parts.append("ORDER BY created_date DESC LIMIT ?")
            query_params.append(normalized_limit)

            cursor.execute(" ".join(query_parts), tuple(query_params))
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Failed to get transactions for user {user_id}: {e}")
        return []


def get_latest_transaction(user_id: int) -> dict | None:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM transactions WHERE user_id = ? ORDER BY created_date DESC LIMIT 1", (user_id,)
            )
            transaction = cursor.fetchone()
            return dict(transaction) if transaction else None
    except sqlite3.Error as e:
        logging.error(f"Failed to get latest transaction for user {user_id}: {e}")
        return None


def get_all_users() -> list[dict]:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users ORDER BY registration_date DESC")
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Failed to get all users: {e}")
        return []


def get_users_filtered(query: str) -> list[dict]:
    """Search users by telegram_id, username, or key_email."""
    query = query.strip()
    if not query:
        return get_all_users()

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            like_pattern = f"%{query}%"
            cursor.execute(
                """
                SELECT DISTINCT u.*
                FROM users u
                LEFT JOIN vpn_keys vk ON u.telegram_id = vk.user_id
                WHERE CAST(u.telegram_id AS TEXT) LIKE ?
                   OR LOWER(COALESCE(u.username, '')) LIKE LOWER(?)
                   OR LOWER(COALESCE(vk.key_email, '')) LIKE LOWER(?)
                ORDER BY u.registration_date DESC
            """,
                (like_pattern, like_pattern, like_pattern),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Failed to filter users with query '{query}': {e}")
        return []


def get_distinct_vpn_key_hosts() -> list[str]:
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT host_name
            FROM vpn_keys
            WHERE host_name IS NOT NULL AND TRIM(host_name) <> ''
            ORDER BY host_name COLLATE NOCASE
            """
        )
        return [str(row[0]) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error("Failed to get distinct vpn key hosts: %s", e)
        return []


def get_users_paginated(
    page: int,
    per_page: int,
    search_query: str | None = None,
    bot_status: str = "all",
    vpn_status: str = "all",
    registration_from: str | None = None,
    registration_to: str | None = None,
    host_name: str | None = None,
    sort_by: str = "registration_desc",
) -> tuple[list[dict], int]:
    offset = max(page - 1, 0) * per_page
    query = (search_query or "").strip()
    where_clauses: list[str] = []
    where_params: list[object] = []
    normalized_bot_status = bot_status if bot_status in {"all", "allowed", "banned"} else "all"
    normalized_vpn_status = vpn_status if vpn_status in {"all", "with_keys", "without_keys", "expired"} else "all"
    normalized_sort = (
        sort_by
        if sort_by
        in {
            "registration_desc",
            "registration_asc",
            "spent_desc",
            "spent_asc",
            "key_count_desc",
            "key_count_asc",
        }
        else "registration_desc"
    )

    if query:
        like_pattern = f"%{query}%"
        where_clauses.append(
            """
            (
                CAST(u.telegram_id AS TEXT) LIKE ?
                OR LOWER(COALESCE(u.username, '')) LIKE LOWER(?)
                OR u.telegram_id IN (
                    SELECT DISTINCT vk.user_id
                    FROM vpn_keys vk
                    WHERE LOWER(COALESCE(vk.key_email, '')) LIKE LOWER(?)
                )
            )
            """
        )
        where_params.extend([like_pattern, like_pattern, like_pattern])

    if normalized_bot_status == "allowed":
        where_clauses.append("COALESCE(u.is_banned, 0) = 0")
    elif normalized_bot_status == "banned":
        where_clauses.append("COALESCE(u.is_banned, 0) = 1")

    if normalized_vpn_status == "with_keys":
        where_clauses.append("COALESCE(k.key_count, 0) > 0")
    elif normalized_vpn_status == "without_keys":
        where_clauses.append("COALESCE(k.key_count, 0) = 0")
    elif normalized_vpn_status == "expired":
        where_clauses.append("COALESCE(k.expired_key_count, 0) > 0")

    if registration_from:
        where_clauses.append("DATE(u.registration_date) >= DATE(?)")
        where_params.append(registration_from)

    if registration_to:
        where_clauses.append("DATE(u.registration_date) <= DATE(?)")
        where_params.append(registration_to)

    normalized_host_name = (host_name or "").strip()
    if normalized_host_name:
        where_clauses.append(
            """
            u.telegram_id IN (
                SELECT DISTINCT vk.user_id
                FROM vpn_keys vk
                WHERE vk.host_name = ?
            )
            """
        )
        where_params.append(normalized_host_name)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    order_by_map = {
        "registration_desc": "u.registration_date DESC",
        "registration_asc": "u.registration_date ASC",
        "spent_desc": "COALESCE(u.total_spent, 0) DESC, u.registration_date DESC",
        "spent_asc": "COALESCE(u.total_spent, 0) ASC, u.registration_date DESC",
        "key_count_desc": "COALESCE(k.key_count, 0) DESC, u.registration_date DESC",
        "key_count_asc": "COALESCE(k.key_count, 0) ASC, u.registration_date DESC",
    }
    order_by_sql = order_by_map[normalized_sort]

    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("PRAGMA table_info(vpn_keys)")
        vpn_key_columns = {str(row[1]) for row in cursor.fetchall()}
        has_expiry_date = "expiry_date" in vpn_key_columns
        has_is_archived = "is_archived" in vpn_key_columns
        archived_predicate = "COALESCE(is_archived, 0) = 1" if has_is_archived else "0 = 1"

        if has_expiry_date:
            active_predicate = (
                "COALESCE(is_archived, 0) = 0 AND (expiry_date IS NULL OR DATETIME(expiry_date) > DATETIME('now'))"
                if has_is_archived
                else "(expiry_date IS NULL OR DATETIME(expiry_date) > DATETIME('now'))"
            )
            expired_predicate = (
                f"{archived_predicate} OR (expiry_date IS NOT NULL AND DATETIME(expiry_date) <= DATETIME('now'))"
            )
        else:
            active_predicate = "COALESCE(is_archived, 0) = 0" if has_is_archived else "1 = 1"
            expired_predicate = archived_predicate

        active_count_sql = f"""
            SUM(
                CASE
                    WHEN {active_predicate} THEN 1
                    ELSE 0
                END
            ) AS key_count
        """
        expired_count_sql = f"""
            SUM(
                CASE
                    WHEN {expired_predicate} THEN 1
                    ELSE 0
                END
            ) AS expired_key_count
        """
        from_sql = f"""
            FROM users u
            LEFT JOIN (
                SELECT
                    user_id,
                    {active_count_sql},
                    {expired_count_sql}
                FROM vpn_keys
                GROUP BY user_id
            ) k ON k.user_id = u.telegram_id
        """

        cursor.execute(
            f"SELECT COUNT(*) {from_sql} {where_sql}",
            tuple(where_params),
        )
        total_count = int(cursor.fetchone()[0] or 0)

        cursor.execute(
            f"""
                SELECT
                    u.*,
                    COALESCE(k.key_count, 0) AS key_count,
                    COALESCE(k.expired_key_count, 0) AS expired_key_count
                {from_sql}
                {where_sql}
                ORDER BY {order_by_sql}
                LIMIT ? OFFSET ?
            """,
            tuple(where_params + [per_page, offset]),
        )
        users = [dict(row) for row in cursor.fetchall()]
        return users, total_count
    except sqlite3.Error as e:
        logging.error("Failed to get paginated users: %s", e)
        return [], 0


def ban_user(telegram_id: int):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_banned = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to ban user {telegram_id}: {e}")


def unban_user(telegram_id: int):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_banned = 0 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Failed to unban user {telegram_id}: {e}")


def delete_user_keys(user_id: int):
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM vpn_keys WHERE user_id = ?", (user_id,))
            conn.commit()
    except sqlite3.Error:
        logger.exception("Failed to delete keys for user %s", user_id)


def _prune_old_archived_keys(cursor: sqlite3.Cursor, user_id: int, keep: int = 2) -> None:
    """Keep only the newest archived keys for a user, deleting older archived rows."""
    keep = max(0, int(keep))
    cursor.execute(
        """
        SELECT key_id
        FROM vpn_keys
        WHERE user_id = ? AND COALESCE(is_archived, 0) = 1
        ORDER BY
            CASE WHEN created_date IS NULL THEN 1 ELSE 0 END,
            DATETIME(created_date) DESC,
            key_id DESC
        """,
        (user_id,),
    )
    archived_ids = [int(row[0]) for row in cursor.fetchall()]
    for archived_key_id in archived_ids[keep:]:
        cursor.execute("DELETE FROM vpn_keys WHERE key_id = ?", (archived_key_id,))


# ──────────────────────────────────────────────────────────────
# Host health & maintenance
# ──────────────────────────────────────────────────────────────


def update_host_status(host_name: str, status: str, error: str | None = None) -> None:
    now = datetime.now(UTC).astimezone().replace(tzinfo=None).isoformat()
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            if status == "healthy":
                cursor.execute(
                    "UPDATE xui_hosts SET status = ?, last_check_at = ?, last_success_at = ?, last_error = NULL WHERE host_name = ?",
                    (status, now, now, host_name),
                )
            else:
                cursor.execute(
                    "UPDATE xui_hosts SET status = ?, last_check_at = ?, last_error = ? WHERE host_name = ?",
                    (status, now, error, host_name),
                )
            conn.commit()
    except sqlite3.Error:
        logger.exception("Failed to update host status for '%s'", host_name)


def update_host_node_sync_state(
    host_name: str,
    *,
    status: str,
    error: str | None = None,
    node_id: int | None = None,
    node_inbound_id: int | None = None,
) -> bool:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"pending", "running", "completed", "failed"}:
        raise ValueError(f"Unsupported node sync status: {status}")  # noqa: TRY003

    safe_error = str(error).strip()[:500] if error else None
    synced_at = datetime.now(UTC).isoformat() if normalized_status == "completed" else None
    clear_synced_at = 1 if normalized_status in {"pending", "running"} else 0
    query = """
        UPDATE xui_hosts
        SET
            node_sync_status = ?,
            node_sync_error = ?,
            node_id = COALESCE(?, node_id),
            node_inbound_id = COALESCE(?, node_inbound_id),
            node_synced_at = CASE
                WHEN ? = 'completed' THEN ?
                WHEN ? = 1 THEN NULL
                ELSE node_synced_at
            END
        WHERE host_name = ?
    """

    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                query,
                (
                    normalized_status,
                    safe_error,
                    int(node_id) if node_id is not None else None,
                    int(node_inbound_id) if node_inbound_id is not None else None,
                    normalized_status,
                    synced_at,
                    clear_synced_at,
                    host_name,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error:
        logger.exception("Failed to update node sync state for host '%s'", host_name)
        return False


def set_host_maintenance(host_name: str, is_maintenance: bool) -> None:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE xui_hosts SET is_maintenance = ? WHERE host_name = ?",
                (1 if is_maintenance else 0, host_name),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error("Failed to set maintenance for host '%s': %s", host_name, e)


def get_available_hosts() -> list[dict]:
    hosts = get_all_hosts()
    return [
        host
        for host in hosts
        if not host.get("is_maintenance")
        and is_host_delivery_active(host)
        and str(host.get("status", "")).lower() not in {"unavailable", "maintenance"}
    ]


def is_host_available(host_name: str) -> bool:
    for host in get_all_hosts():
        if host.get("host_name") != host_name:
            continue
        if host.get("is_maintenance"):
            return False
        if not is_host_delivery_active(host):
            return False
        return str(host.get("status", "")).lower() not in {"unavailable", "maintenance"}
    return False


def get_hosts_with_status() -> list[dict]:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM xui_hosts")
            return [_with_effective_host_status(dict(row)) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error("Failed to get hosts with status: %s", e)
        return []


def is_global_maintenance() -> bool:
    return get_setting("maintenance_mode") == "true"


def get_maintenance_message() -> str:
    msg = get_setting("maintenance_message")
    return msg or "Покупка временно недоступна, ведутся технические работы. Попробуйте позже."


# ──────────────────────────────────────────────────────────────
# Key migration & archival
# ──────────────────────────────────────────────────────────────


def get_active_keys_for_host(host_name: str) -> list[dict]:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM vpn_keys WHERE host_name = ? AND COALESCE(is_archived, 0) = 0 AND expiry_date > ? ORDER BY key_id DESC",
                (host_name, datetime.now().isoformat()),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error("Failed to get active keys for host '%s': %s", host_name, e)
        return []


def get_all_keys_for_host_migration(
    host_name: str,
    include_expired: bool = False,
    include_trial_super: bool = False,
) -> list[dict]:
    """
    Return non-archived keys for a host (for incident migration).

    By default returns only still-active keys (expiry_date > now).
    With include_expired=True also returns expired keys.
    By default excludes free super-trial keys (`*-trial-super@telegram.bot`).
    """
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            trial_filter = ""
            if not include_trial_super:
                trial_filter = " AND key_email NOT LIKE '%-trial-super@telegram.bot'"

            if include_expired:
                cursor.execute(
                    f"SELECT * FROM vpn_keys WHERE host_name = ? AND COALESCE(is_archived, 0) = 0{trial_filter}",
                    (host_name,),
                )
            else:
                cursor.execute(
                    f"SELECT * FROM vpn_keys WHERE host_name = ? AND COALESCE(is_archived, 0) = 0{trial_filter} AND expiry_date > ?",
                    (host_name, datetime.now().isoformat()),
                )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error("Failed to get all keys for host '%s' migration: %s", host_name, e)
        return []


def get_mainpanel_migration_source_keys(
    main_panel_host_name: str,
    *,
    include_trial_super: bool = False,
) -> list[dict]:
    """
    Return non-archived keys from legacy hosts for MainPanel tariff migration.

    Expired keys are intentionally included so dry-run can report them as skipped.
    """
    normalized_main_panel = str(main_panel_host_name or "").strip()
    if not normalized_main_panel:
        return []

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT *
                FROM vpn_keys
                WHERE COALESCE(is_archived, 0) = 0
                  AND host_name <> ?
                  AND (? = 1 OR key_email NOT LIKE ?)
                ORDER BY user_id ASC, key_id ASC
                """,
                (normalized_main_panel, 1 if include_trial_super else 0, "%-trial-super@telegram.bot"),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error:
        logger.exception("Failed to get MainPanel migration source keys")
        return []


def get_successful_transactions_for_migration_keys(
    keys: list[dict], *, per_key_limit: int = 8
) -> dict[int, list[dict]]:
    if not keys:
        return {}

    keys_by_user: dict[int, list[dict]] = {}
    for key in keys:
        try:
            user_id = int(key.get("user_id") or 0)
            key_id = int(key.get("key_id") or 0)
        except (TypeError, ValueError):
            continue
        if user_id <= 0 or key_id <= 0:
            continue
        keys_by_user.setdefault(user_id, []).append(key)

    if not keys_by_user:
        return {}

    result: dict[int, list[dict]] = {int(key.get("key_id") or 0): [] for key in keys}
    user_ids = sorted(keys_by_user)
    safe_limit = max(1, min(int(per_key_limit or 8), 20))

    def _metadata_matches_key(metadata: dict, key: dict) -> bool:
        try:
            metadata_key_id = int(str(metadata.get("key_id") or metadata.get("vpn_key_id") or "").strip())
        except (TypeError, ValueError):
            metadata_key_id = 0
        key_id = int(key.get("key_id") or 0)
        if metadata_key_id and metadata_key_id == key_id:
            return True

        metadata_host = str(metadata.get("host_name") or "").strip().lower()
        key_host = str(key.get("host_name") or "").strip().lower()
        return bool(metadata_host and key_host and metadata_host == key_host)

    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            for user_id in user_ids:
                cursor.execute(
                    """
                    SELECT transaction_id, payment_id, user_id, status, issuance_state,
                           metadata, created_date, confirmed_at
                    FROM transactions
                    WHERE user_id = ?
                      AND metadata IS NOT NULL
                      AND TRIM(metadata) <> ''
                      AND (
                          status = 'paid'
                          OR issuance_state = ?
                          OR CASE
                              WHEN json_valid(metadata)
                              THEN COALESCE(json_extract(metadata, '$.payment_confirmed'), 0)
                              ELSE 0
                             END = 1
                      )
                    ORDER BY user_id ASC, transaction_id DESC
                    """,
                    (user_id, ISSUANCE_STATE_ISSUED),
                )
                for row in cursor.fetchall():
                    row_dict = dict(row)
                    try:
                        metadata = json.loads(str(row_dict.get("metadata") or "{}"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(metadata, dict):
                        continue
                    row_dict["metadata_dict"] = metadata
                    try:
                        row_user_id = int(row_dict.get("user_id") or 0)
                    except (TypeError, ValueError):
                        continue
                    for key in keys_by_user.get(row_user_id, []):
                        key_id = int(key.get("key_id") or 0)
                        if key_id <= 0 or len(result.get(key_id, [])) >= safe_limit:
                            continue
                        if _metadata_matches_key(metadata, key):
                            result.setdefault(key_id, []).append(row_dict)
    except sqlite3.Error:
        logger.exception("Failed to get successful migration transaction metadata")
        return result
    else:
        return result


def claim_mainpanel_migration_item(
    *,
    incident_id: str,
    user_id: int,
    old_key_id: int,
    old_host: str,
    new_host: str,
    gift_days: int,
    remaining_days: float,
) -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                """
                SELECT status
                FROM incident_migrations
                WHERE old_key_id = ? AND incident_id = ?
                ORDER BY migration_id DESC
                LIMIT 1
                """,
                (old_key_id, incident_id),
            )
            row = cursor.fetchone()
            if row and str(row[0] or "") in {"processing", "migrated"}:
                conn.rollback()
                return False

            cursor.execute(
                """SELECT 1
                   FROM vpn_keys
                   WHERE migrated_from_key_id = ?
                     AND migration_incident_id = ?
                     AND COALESCE(is_archived, 0) = 0
                   LIMIT 1""",
                (old_key_id, incident_id),
            )
            if cursor.fetchone():
                conn.rollback()
                return False

            cursor.execute(
                """INSERT INTO incident_migrations
                   (incident_id, user_id, old_key_id, new_key_id, old_host, new_host,
                    gift_days, remaining_days_at_migration, status, error_message)
                   VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 'processing', NULL)""",
                (incident_id, user_id, old_key_id, old_host, new_host, gift_days, remaining_days),
            )
            conn.commit()
            return True
    except sqlite3.Error:
        logger.exception("Failed to claim MainPanel migration key %s", old_key_id)
        return False


def finish_mainpanel_migration_item(
    *,
    incident_id: str,
    old_key_id: int,
    status: str,
    new_key_id: int | None = None,
    error_msg: str | None = None,
) -> bool:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE incident_migrations
                SET status = ?,
                    new_key_id = COALESCE(?, new_key_id),
                    error_message = ?
                WHERE migration_id = (
                    SELECT migration_id
                    FROM incident_migrations
                    WHERE old_key_id = ?
                      AND incident_id = ?
                      AND status = 'processing'
                    ORDER BY migration_id DESC
                    LIMIT 1
                )
                """,
                (status, new_key_id, error_msg, old_key_id, incident_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error:
        logger.exception("Failed to finish MainPanel migration key %s", old_key_id)
        return False


def archive_key(
    key_id: int,
    *,
    replaced_due_to_incident: bool = False,
    migration_reason: str | None = None,
) -> None:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM vpn_keys WHERE key_id = ?", (key_id,))
            row = cursor.fetchone()
            if row is None:
                return
            user_id = int(row[0])
            cursor.execute(
                """
                UPDATE vpn_keys
                SET is_archived = 1,
                    replaced_due_to_incident = ?,
                    migration_reason = COALESCE(?, migration_reason)
                WHERE key_id = ?
                """,
                (1 if replaced_due_to_incident else 0, migration_reason, key_id),
            )
            _prune_old_archived_keys(cursor, user_id, keep=2)
            conn.commit()
    except sqlite3.Error as e:
        logging.error("Failed to archive key %s: %s", key_id, e)


def add_migrated_key(
    user_id: int,
    host_name: str,
    xui_client_uuid: str,
    key_email: str,
    expiry_timestamp_ms: int,
    migrated_from_key_id: int,
    gift_days: int,
    incident_id: str,
    issued_key_value: str | None = None,
    bot_source: str = "main",
    panel_sub_id: str | None = None,
    subscription_payload: str | None = None,
    subscription_tier: str | None = None,
) -> int | None:
    expiry_date = datetime.fromtimestamp(expiry_timestamp_ms / 1000).isoformat()
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO vpn_keys
                   (user_id, host_name, xui_client_uuid, key_email, expiry_date,
                    migrated_from_key_id, gift_days, migration_incident_id, migration_reason, is_archived,
                    issued_key_value, subscription_payload, subscription_tier, bot_source, panel_sub_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'incident_migration', 0, ?, ?, ?, ?, ?)""",
                (
                    user_id,
                    host_name,
                    xui_client_uuid,
                    key_email,
                    expiry_date,
                    migrated_from_key_id,
                    gift_days,
                    incident_id,
                    issued_key_value,
                    subscription_payload,
                    subscription_tier,
                    bot_source,
                    panel_sub_id,
                ),
            )
            conn.commit()
            return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error("Failed to add migrated key for user %s: %s", user_id, e)
        return None


def log_migration(
    incident_id: str,
    user_id: int,
    old_key_id: int,
    new_key_id: int | None,
    old_host: str,
    new_host: str,
    gift_days: int,
    remaining_days: float,
    status: str,
    error_msg: str | None = None,
) -> None:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO incident_migrations
                   (incident_id, user_id, old_key_id, new_key_id, old_host, new_host,
                    gift_days, remaining_days_at_migration, status, error_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    incident_id,
                    user_id,
                    old_key_id,
                    new_key_id,
                    old_host,
                    new_host,
                    gift_days,
                    remaining_days,
                    status,
                    error_msg,
                ),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error("Failed to log migration for key %s: %s", old_key_id, e)


def get_migration_for_key(old_key_id: int, incident_id: str) -> dict | None:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """SELECT *
                   FROM incident_migrations
                   WHERE old_key_id = ? AND incident_id = ?
                   ORDER BY migration_id DESC
                   LIMIT 1""",
                (old_key_id, incident_id),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error("Failed to check migration for key %s: %s", old_key_id, e)
        return None


def get_replacement_key_for_incident(old_key_id: int, incident_id: str) -> dict | None:
    """
    Return already created replacement key for source key in an incident.

    Useful for recovering partially successful migrations where panel key was created
    and saved in DB, but incident_migrations row remained in error state.
    """
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """SELECT *
                   FROM vpn_keys
                   WHERE migrated_from_key_id = ?
                     AND migration_incident_id = ?
                     AND COALESCE(is_archived, 0) = 0
                   ORDER BY key_id DESC
                   LIMIT 1""",
                (old_key_id, incident_id),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error(
            "Failed to get replacement key for old_key_id=%s incident=%s: %s",
            old_key_id,
            incident_id,
            e,
        )
        return None


# ──────────────────────────────────────────────────────────────
# Incidents
# ──────────────────────────────────────────────────────────────


def create_incident(incident_id: str, message: str) -> None:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO incidents (incident_id, message) VALUES (?, ?)",
                (incident_id, message),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error("Failed to create incident '%s': %s", incident_id, e)


def get_unseen_incidents_for_user(user_id: int) -> list[dict]:
    try:
        with _create_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """SELECT DISTINCT i.incident_id, i.message
                   FROM incidents i
                   JOIN incident_migrations im ON im.incident_id = i.incident_id AND im.user_id = ?
                   WHERE i.is_active = 1
                     AND im.status IN ('migrated', 'gifted')
                     AND NOT EXISTS (
                         SELECT 1 FROM user_incidents_seen uis
                         WHERE uis.user_id = ? AND uis.incident_id = i.incident_id
                     )
                   ORDER BY i.created_at""",
                (user_id, user_id),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error("Failed to get unseen incidents for user %s: %s", user_id, e)
        return []


def get_latest_migrated_key_id_for_user_incident(user_id: int, incident_id: str) -> int | None:
    """
    Return the latest active migrated key for a user within an incident.

    Used to open the newly issued key directly from incident notification.
    """
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT im.new_key_id
                FROM incident_migrations im
                JOIN vpn_keys vk ON vk.key_id = im.new_key_id
                WHERE im.user_id = ?
                  AND im.incident_id = ?
                  AND im.status IN ('migrated', 'gifted')
                  AND im.new_key_id IS NOT NULL
                  AND COALESCE(vk.is_archived, 0) = 0
                ORDER BY im.migration_id DESC
                LIMIT 1
                """,
                (user_id, incident_id),
            )
            row = cursor.fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except sqlite3.Error as e:
        logging.error(
            "Failed to get latest migrated key for user %s and incident '%s': %s",
            user_id,
            incident_id,
            e,
        )
        return None


def mark_incident_seen(user_id: int, incident_id: str) -> None:
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO user_incidents_seen (user_id, incident_id) VALUES (?, ?)",
                (user_id, incident_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error("Failed to mark incident '%s' as seen for user %s: %s", incident_id, user_id, e)


def set_incident_opened_new_key_at(user_id: int, incident_id: str) -> None:
    """Record that the user clicked 'Open new key' for this incident (used new key)."""
    try:
        with _create_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR IGNORE INTO user_incidents_seen (user_id, incident_id) VALUES (?, ?)",
                (user_id, incident_id),
            )
            cursor.execute(
                "UPDATE user_incidents_seen SET opened_new_key_at = CURRENT_TIMESTAMP WHERE user_id = ? AND incident_id = ?",
                (user_id, incident_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(
            "Failed to set opened_new_key_at for incident '%s' user %s: %s",
            incident_id,
            user_id,
            e,
        )
