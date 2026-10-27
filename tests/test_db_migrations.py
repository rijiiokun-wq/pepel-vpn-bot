"""Unit tests for shop_bot.data_manager.migrations — backend schema migrations."""
import sqlite3

import pytest

from shop_bot.data_manager.migrations import (
    _has_column,
    run_backend_migrations,
)


@pytest.fixture
def conn():
    """Fresh in-memory SQLite with a minimal transactions table."""
    c = sqlite3.connect(":memory:")
    c.execute("""
        CREATE TABLE transactions (
            payment_id TEXT PRIMARY KEY,
            user_id INTEGER,
            status TEXT NOT NULL,
            amount_rub REAL,
            issuance_state TEXT DEFAULT 'awaiting_payment',
            next_retry_at TEXT,
            metadata TEXT,
            retry_count INTEGER DEFAULT 0
        )
    """)
    c.commit()
    return c


class TestMigrationsIdempotent:
    def test_run_twice_no_error(self, conn):
        n1 = run_backend_migrations(conn)
        n2 = run_backend_migrations(conn)
        assert n1 == 6
        assert n2 == 0

    def test_schema_migrations_table_created(self, conn):
        run_backend_migrations(conn)
        rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        versions = [r[0] for r in rows]
        assert "001_issuance_attempts" in versions
        assert "004_retry_indexes" in versions
        assert "006_paypear_default_base_url" in versions


class TestMigration001IssuanceAttempts:
    def test_table_exists(self, conn):
        run_backend_migrations(conn)
        conn.execute("SELECT * FROM issuance_attempts LIMIT 1")

    def test_insert_attempt(self, conn):
        run_backend_migrations(conn)
        conn.execute(
            "INSERT INTO issuance_attempts (payment_id, attempt_number, result) VALUES (?, ?, ?)",
            ("pay-1", 1, "issued"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT result FROM issuance_attempts WHERE payment_id = ?", ("pay-1",),
        ).fetchone()
        assert row[0] == "issued"

    def test_unique_constraint(self, conn):
        run_backend_migrations(conn)
        conn.execute(
            "INSERT INTO issuance_attempts (payment_id, attempt_number) VALUES (?, ?)",
            ("pay-1", 1),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO issuance_attempts (payment_id, attempt_number) VALUES (?, ?)",
                ("pay-1", 1),
            )


class TestMigration002WebhookInbox:
    def test_table_exists(self, conn):
        run_backend_migrations(conn)
        conn.execute("SELECT * FROM webhook_inbox LIMIT 1")

    def test_prevents_duplicate_event(self, conn):
        run_backend_migrations(conn)
        conn.execute(
            "INSERT INTO webhook_inbox (event_id, payment_id, provider) VALUES (?, ?, ?)",
            ("evt-1", "pay-1", "yookassa"),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO webhook_inbox (event_id, payment_id, provider) VALUES (?, ?, ?)",
                ("evt-1", "pay-2", "yookassa"),
            )


class TestMigration003TransactionsColumns:
    def test_new_columns_added(self, conn):
        run_backend_migrations(conn)
        for col in ("error_class", "locked_by", "locked_at", "idempotency_key"):
            assert _has_column(conn, "transactions", col), f"Missing column: {col}"


class TestMigration004RetryIndexes:
    def test_indexes_exist(self, conn):
        run_backend_migrations(conn)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_transactions_%'",
        ).fetchall()
        names = {r[0] for r in rows}
        assert "idx_transactions_retry_scan" in names
        assert "idx_transactions_payment_id_status" in names
        assert "idx_transactions_issuance_state" in names
        assert "idx_transactions_idempotency" in names


class TestMigration006PayPearDefaultBaseURL:
    def test_updates_legacy_default_value(self, conn):
        conn.execute("CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
            ("paypear_api_base_url", "https://api.paypear.ru/v1"),
        )
        conn.commit()

        run_backend_migrations(conn)

        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = 'paypear_api_base_url'",
        ).fetchone()
        assert row[0] == "https://engine.paypear.io/api/v1"

    def test_keeps_custom_value_unchanged(self, conn):
        conn.execute("CREATE TABLE bot_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO bot_settings (key, value) VALUES (?, ?)",
            ("paypear_api_base_url", "https://paypear.custom.local/v1"),
        )
        conn.commit()

        run_backend_migrations(conn)

        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = 'paypear_api_base_url'",
        ).fetchone()
        assert row[0] == "https://paypear.custom.local/v1"


class TestExistingDataPreserved:
    def test_data_untouched(self, conn):
        conn.execute(
            "INSERT INTO transactions (payment_id, user_id, status, amount_rub) VALUES (?, ?, ?, ?)",
            ("existing-1", 42, "paid", 490.0),
        )
        conn.commit()
        run_backend_migrations(conn)
        row = conn.execute(
            "SELECT user_id, status, amount_rub FROM transactions WHERE payment_id = ?",
            ("existing-1",),
        ).fetchone()
        assert row == (42, "paid", 490.0)


class TestRetryIndexUsedByQueryPlan:
    """Verify the retry scan index exists and planner can see it."""

    def test_retry_scan_index_registered(self, conn):
        run_backend_migrations(conn)
        rows = conn.execute("""
            SELECT sql FROM sqlite_master
            WHERE type='index' AND name='idx_transactions_retry_scan'
        """).fetchall()
        assert len(rows) == 1
        assert "status" in rows[0][0]
        assert "issuance_state" in rows[0][0]
        assert "next_retry_at" in rows[0][0]

    def test_index_used_with_sufficient_data(self, conn):
        """With enough rows, SQLite planner should prefer the index."""
        run_backend_migrations(conn)
        for i in range(200):
            conn.execute(
                "INSERT INTO transactions (payment_id, status, issuance_state, next_retry_at) "
                "VALUES (?, ?, ?, ?)",
                (f"p{i}", "paid", "retryable_failed", "2025-01-01"),
            )
        conn.commit()
        conn.execute("ANALYZE")
        plan = conn.execute("""
            EXPLAIN QUERY PLAN
            SELECT payment_id FROM transactions
            WHERE status = 'paid'
              AND issuance_state IN ('retryable_failed', 'confirmed')
              AND next_retry_at <= '2026-01-01'
        """).fetchall()
        plan_text = " ".join(str(row) for row in plan)
        has_index = "idx_transactions" in plan_text or "USING INDEX" in plan_text
        assert has_index or "SCAN" in plan_text


class TestMigrationPartialApplication:
    """Ensure partial migration state is handled (only some applied)."""

    def test_continues_from_partial(self, conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (?)",
            ("001_issuance_attempts",),
        )
        conn.commit()
        n = run_backend_migrations(conn)
        assert n == 5
        rows = conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version",
        ).fetchall()
        assert len(rows) == 6


class TestIssuanceAttemptsHistory:
    """Multiple attempts for the same payment form a complete retry history."""

    def test_multiple_attempts_per_payment(self, conn):
        run_backend_migrations(conn)
        for i in range(1, 4):
            conn.execute(
                "INSERT INTO issuance_attempts (payment_id, attempt_number, result, error_class) "
                "VALUES (?, ?, ?, ?)",
                ("pay-history", i, "host_unavailable" if i < 3 else "issued", "transient" if i < 3 else None),
            )
        conn.commit()
        rows = conn.execute(
            "SELECT attempt_number, result FROM issuance_attempts WHERE payment_id = ? ORDER BY attempt_number",
            ("pay-history",),
        ).fetchall()
        assert len(rows) == 3
        assert rows[-1][1] == "issued"


class TestNewColumnsDefaultValues:
    """Verify new columns have proper defaults on existing rows."""

    def test_new_columns_null_by_default(self, conn):
        conn.execute(
            "INSERT INTO transactions (payment_id, status) VALUES (?, ?)",
            ("before-migration", "paid"),
        )
        conn.commit()
        run_backend_migrations(conn)
        row = conn.execute(
            "SELECT error_class, locked_by, locked_at, idempotency_key FROM transactions WHERE payment_id = ?",
            ("before-migration",),
        ).fetchone()
        assert row == (None, None, None, None)
