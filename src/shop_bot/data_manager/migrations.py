"""Versioned schema migrations for the backend-migration epic.

All migrations are idempotent: ``IF NOT EXISTS`` for tables/indexes,
column-existence check before ``ALTER TABLE``.  Called from
``database.initialize_db()`` after the legacy ``run_migration()``.
"""
from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

_PAYPEAR_MODERN_DEFAULT_BASE_URL = "https://engine.paypear.io/api/v1"
_PAYPEAR_LEGACY_DEFAULT_BASE_URL = "https://api.paypear.ru/v1"


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cursor)


def _migrate_001_issuance_attempts(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS issuance_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL DEFAULT 1,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT,
            result TEXT NOT NULL DEFAULT 'pending',
            error_code TEXT,
            error_class TEXT,
            error_message TEXT,
            host_name TEXT,
            key_id INTEGER,
            connection_string TEXT,
            trigger_source TEXT,
            bot_source TEXT,
            duration_ms INTEGER,
            UNIQUE(payment_id, attempt_number)
        );

        CREATE INDEX IF NOT EXISTS idx_issuance_attempts_payment
            ON issuance_attempts(payment_id);
        CREATE INDEX IF NOT EXISTS idx_issuance_attempts_result
            ON issuance_attempts(result, started_at);
    """)


def _migrate_002_webhook_inbox(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS webhook_inbox (
            event_id TEXT PRIMARY KEY,
            payment_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            received_at TEXT NOT NULL DEFAULT (datetime('now')),
            processed_at TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            raw_payload TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_webhook_inbox_payment
            ON webhook_inbox(payment_id);
        CREATE INDEX IF NOT EXISTS idx_webhook_inbox_status
            ON webhook_inbox(status);
    """)


def _migrate_003_transactions_columns(conn: sqlite3.Connection) -> None:
    new_columns = [
        ("error_class", "TEXT"),
        ("locked_by", "TEXT"),
        ("locked_at", "TEXT"),
        ("idempotency_key", "TEXT"),
    ]
    for col_name, col_type in new_columns:
        if not _has_column(conn, "transactions", col_name):
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col_name} {col_type}")
            logger.info("Migration 003: added column transactions.%s", col_name)


def _migrate_004_retry_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_transactions_retry_scan
            ON transactions(status, issuance_state, next_retry_at)
            WHERE status IN ('pending', 'paid');

        CREATE INDEX IF NOT EXISTS idx_transactions_payment_id_status
            ON transactions(payment_id, status);

        CREATE INDEX IF NOT EXISTS idx_transactions_issuance_state
            ON transactions(issuance_state)
            WHERE issuance_state IN ('retryable_failed', 'confirmed', 'issuing');

        CREATE INDEX IF NOT EXISTS idx_transactions_idempotency
            ON transactions(idempotency_key)
            WHERE idempotency_key IS NOT NULL;
    """)


def _migrate_005_shadow_diffs(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS shadow_diffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT NOT NULL,
            legacy_status TEXT,
            shadow_status TEXT,
            legacy_reason TEXT,
            shadow_reason TEXT,
            legacy_ms INTEGER,
            shadow_ms INTEGER,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_shadow_diffs_payment
            ON shadow_diffs(payment_id);
        CREATE INDEX IF NOT EXISTS idx_shadow_diffs_created
            ON shadow_diffs(created_at);
    """)


def _migrate_006_paypear_default_base_url(conn: sqlite3.Connection) -> None:
    if not _has_table(conn, "bot_settings"):
        return

    row = conn.execute(
        "SELECT value FROM bot_settings WHERE key = 'paypear_api_base_url' LIMIT 1",
    ).fetchone()
    if not row:
        return

    current_value = str(row[0] or "").strip().rstrip("/")
    if current_value != _PAYPEAR_LEGACY_DEFAULT_BASE_URL:
        return

    conn.execute(
        "UPDATE bot_settings SET value = ? WHERE key = 'paypear_api_base_url'",
        (_PAYPEAR_MODERN_DEFAULT_BASE_URL,),
    )
    logger.info(
        "Migration 006: paypear_api_base_url updated from legacy to modern default.",
    )


_MIGRATIONS: list[tuple[str, object]] = [
    ("001_issuance_attempts", _migrate_001_issuance_attempts),
    ("002_webhook_inbox", _migrate_002_webhook_inbox),
    ("003_transactions_columns", _migrate_003_transactions_columns),
    ("004_retry_indexes", _migrate_004_retry_indexes),
    ("005_shadow_diffs", _migrate_005_shadow_diffs),
    ("006_paypear_default_base_url", _migrate_006_paypear_default_base_url),
]


def run_backend_migrations(conn: sqlite3.Connection) -> int:
    """Run all pending backend-migration schema changes. Idempotent.

    Returns the number of newly applied migrations.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()

    applied = {
        row[0] for row in conn.execute("SELECT version FROM schema_migrations")
    }

    count = 0
    for version, migrate_fn in _MIGRATIONS:
        if version in applied:
            continue
        logger.info("Applying backend migration %s …", version)
        migrate_fn(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (?)", (version,),
        )
        conn.commit()
        logger.info("Backend migration %s applied.", version)
        count += 1

    return count
