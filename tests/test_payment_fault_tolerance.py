"""Edge-case integration tests for payment fault tolerance (TASK-0 — TASK-7).

All tests use a temporary SQLite DB and mocks — no network, no external services.
"""
from __future__ import annotations

import inspect
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_users.db"
    monkeypatch.setattr("shop_bot.data_manager.database.DB_FILE", db_file)
    monkeypatch.setattr("shop_bot.data_manager.database._WAL_INITIALIZED", False)

    from shop_bot.data_manager import database

    database.initialize_db()
    yield db_file


def _build_metadata(*, payment_id: str, user_id: int, host_name: str = "TEST_HOST") -> dict:
    return {
        "user_id": user_id,
        "months": 1,
        "price": 300.0,
        "action": "new",
        "key_id": 0,
        "host_name": host_name,
        "plan_id": 1,
        "plan_name": "Test plan",
        "customer_email": f"user{user_id}@vpn.bot",
        "payment_method": "YooKassa",
        "external_payment_id": payment_id,
        "payment_confirmed": True,
        "trace_id": "test12345678",
    }


# ────────────────────────────────────────────────────────────────
# TASK-0: Idempotency guard
# ────────────────────────────────────────────────────────────────

def test_is_transaction_paid_returns_true_for_paid_tx():
    """is_transaction_paid returns True when status='paid'."""
    from shop_bot.data_manager import database

    pid = "idem-paid-001"
    database.register_user_if_not_exists(9001, "u9001", None)
    database.create_pending_transaction(pid, 9001, 100.0, _build_metadata(payment_id=pid, user_id=9001))
    assert not database.is_transaction_paid(pid)

    database.mark_transaction_paid(pid)
    assert database.is_transaction_paid(pid)


def test_paid_transaction_detected_for_accounting():
    """is_transaction_paid remains an accounting predicate, not an issuance guard.

    Full handler-level test requires aiohttp/aiogram which are not available in
    the lightweight test environment, so we verify the DB predicate separately.
    """
    from shop_bot.data_manager import database

    pid = "idem-skip-001"
    uid = 9002
    database.register_user_if_not_exists(uid, "u9002", None)
    database.create_pending_transaction(pid, uid, 100.0, _build_metadata(payment_id=pid, user_id=uid))
    database.mark_transaction_paid(pid)

    assert database.is_transaction_paid(pid) is True


def test_fully_issued_guard_does_not_hide_paid_awaiting_key(_use_temp_db):
    """paid+awaiting_payment means money is accepted but key issuance still needs recovery."""
    from shop_bot.data_manager import database

    pid = "paid-awaiting-001"
    uid = 90021
    database.register_user_if_not_exists(uid, "u90021", None)
    database.create_pending_transaction(pid, uid, 100.0, _build_metadata(payment_id=pid, user_id=uid))

    with sqlite3.connect(str(_use_temp_db)) as conn:
        conn.execute(
            "UPDATE transactions SET status = 'paid', issuance_state = ? WHERE payment_id = ?",
            (database.ISSUANCE_STATE_AWAITING_PAYMENT, pid),
        )
        conn.commit()

    assert database.is_transaction_paid(pid) is True
    assert database.is_transaction_fully_issued(pid) is False
    assert database.start_transaction_processing(pid) is True

    tx = database.get_transaction_by_payment_id(pid)
    assert tx is not None
    assert tx["status"] == "processing"
    assert tx["issuance_state"] == database.ISSUANCE_STATE_ISSUING

    database.mark_transaction_paid(pid)
    assert database.is_transaction_fully_issued(pid) is True
    assert database.start_transaction_processing(pid) is False


def test_transaction_lock_blocks_parallel_issuance_until_stale_lock_expires():
    from shop_bot.data_manager import database

    pid = "idem-lock-001"
    uid = 90022
    database.register_user_if_not_exists(uid, "u90022", None)
    database.create_pending_transaction(pid, uid, 100.0, _build_metadata(payment_id=pid, user_id=uid))

    first_token = database.try_acquire_transaction_lock(pid, ttl_seconds=120)
    assert first_token
    assert database.try_acquire_transaction_lock(pid, ttl_seconds=120) is None

    with database._create_connection() as conn:
        conn.execute(
            "UPDATE transactions SET locked_at = datetime('now', '-121 seconds') WHERE payment_id = ?",
            (pid,),
        )
        conn.commit()

    stale_replacement_token = database.try_acquire_transaction_lock(pid, ttl_seconds=120)
    assert stale_replacement_token
    assert stale_replacement_token != first_token
    assert database.release_transaction_lock(pid, first_token) is False
    assert database.release_transaction_lock(pid, stale_replacement_token) is True


# ────────────────────────────────────────────────────────────────
# TASK-1: Duplicate function removed
# ────────────────────────────────────────────────────────────────

def test_mark_transaction_retryable_failure_defined_once():
    """mark_transaction_retryable_failure must be defined exactly once (TASK-1)."""
    from shop_bot.data_manager import database as db_module

    source = inspect.getsource(db_module)
    count = source.count("def mark_transaction_retryable_failure(")
    assert count == 1, f"Expected 1 definition, found {count}"


# ────────────────────────────────────────────────────────────────
# TASK-2: Connection string validation
# ────────────────────────────────────────────────────────────────

def test_empty_connection_string_triggers_retry():
    """Empty connection string must not finalize — must reset to pending (TASK-2)."""
    from shop_bot.data_manager import database

    pid = "conn-str-empty-001"
    uid = 9003
    meta = _build_metadata(payment_id=pid, user_id=uid)
    database.register_user_if_not_exists(uid, "u9003", None)
    database.create_pending_transaction(pid, uid, 100.0, meta)

    database.mark_transaction_retryable_failure(pid, reason="empty_connection_string")
    tx = database.get_transaction_by_payment_id(pid)
    assert tx is not None
    assert tx["status"] != "paid"
    assert tx["issuance_state"] == database.ISSUANCE_STATE_RETRYABLE_FAILED


# ────────────────────────────────────────────────────────────────
# TASK-3: Dead-letter exhausted state
# ────────────────────────────────────────────────────────────────

def test_mark_transaction_exhausted():
    """Exhausted transactions must not be picked up by autoretry (TASK-3)."""
    from shop_bot.data_manager import database

    pid = "exhaust-001"
    uid = 9004
    meta = _build_metadata(payment_id=pid, user_id=uid)
    database.register_user_if_not_exists(uid, "u9004", None)
    database.create_pending_transaction(pid, uid, 100.0, meta)

    database.mark_transaction_retryable_failure(pid, reason="test_exhaust")
    ok = database.mark_transaction_exhausted(pid, reason="retry_exhausted")
    assert ok

    tx = database.get_transaction_by_payment_id(pid)
    assert tx["issuance_state"] == database.ISSUANCE_STATE_EXHAUSTED

    candidates = database.get_pending_confirmed_transactions(
        limit=100, min_age_seconds=0, due_only=False,
    )
    pids = [c["payment_id"] for c in candidates]
    assert pid not in pids


def test_mark_transaction_confirmed_does_not_reopen_terminal_states():
    """Duplicate confirm must keep terminal issuance states unchanged."""
    from shop_bot.data_manager import database

    fatal_pid = "terminal-fatal-confirm-001"
    fatal_uid = 9401
    fatal_meta = _build_metadata(payment_id=fatal_pid, user_id=fatal_uid)
    database.register_user_if_not_exists(fatal_uid, "u9401", None)
    database.create_pending_transaction(fatal_pid, fatal_uid, 100.0, fatal_meta)
    assert database.mark_transaction_fatal_failure(
        fatal_pid,
        reason="precheck_blocked",
        metadata=fatal_meta,
        statuses=("pending",),
    ) is True

    assert database.mark_transaction_confirmed(
        fatal_pid,
        metadata={**fatal_meta, "retry_source": "duplicate_webhook"},
    ) is True
    fatal_tx = database.get_transaction_by_payment_id(fatal_pid)
    assert fatal_tx["issuance_state"] == database.ISSUANCE_STATE_FATAL_FAILED
    assert database.start_transaction_processing(fatal_pid) is False

    exhausted_pid = "terminal-exhausted-confirm-001"
    exhausted_uid = 9402
    exhausted_meta = _build_metadata(payment_id=exhausted_pid, user_id=exhausted_uid)
    database.register_user_if_not_exists(exhausted_uid, "u9402", None)
    database.create_pending_transaction(exhausted_pid, exhausted_uid, 100.0, exhausted_meta)
    database.mark_transaction_retryable_failure(
        exhausted_pid,
        reason="host_unavailable",
        metadata=exhausted_meta,
        statuses=("pending",),
    )
    assert database.mark_transaction_exhausted(exhausted_pid, reason="retry_exhausted") is True

    assert database.mark_transaction_confirmed(
        exhausted_pid,
        metadata={**exhausted_meta, "retry_source": "duplicate_webhook"},
    ) is True
    exhausted_tx = database.get_transaction_by_payment_id(exhausted_pid)
    assert exhausted_tx["issuance_state"] == database.ISSUANCE_STATE_EXHAUSTED
    assert database.start_transaction_processing(exhausted_pid) is False


def test_mark_retryable_failure_does_not_override_terminal_state():
    """Retryable failure updater must ignore terminal issuance states."""
    from shop_bot.data_manager import database

    pid = "terminal-retry-guard-001"
    uid = 9403
    meta = _build_metadata(payment_id=pid, user_id=uid)
    database.register_user_if_not_exists(uid, "u9403", None)
    database.create_pending_transaction(pid, uid, 100.0, meta)
    assert database.mark_transaction_fatal_failure(
        pid,
        reason="host_not_found",
        metadata=meta,
        statuses=("pending",),
    ) is True

    result = database.mark_transaction_retryable_failure(
        pid,
        reason="should_not_reopen",
        metadata=meta,
        statuses=("pending",),
    )
    assert result["updated"] is False
    tx = database.get_transaction_by_payment_id(pid)
    assert tx["issuance_state"] == database.ISSUANCE_STATE_FATAL_FAILED
    assert tx["last_error"] == "host_not_found"


# ────────────────────────────────────────────────────────────────
# TASK-4: Panel-DB orphan reconciliation
# ────────────────────────────────────────────────────────────────

def test_update_transaction_metadata_field_atomic():
    """update_transaction_metadata_field sets a single key without overwriting others (TASK-4)."""
    from shop_bot.data_manager import database

    pid = "meta-field-001"
    uid = 9005
    meta = _build_metadata(payment_id=pid, user_id=uid)
    database.register_user_if_not_exists(uid, "u9005", None)
    database.create_pending_transaction(pid, uid, 100.0, meta)

    database.update_transaction_metadata_field(pid, "panel_client_uuid", "uuid-abc-123")
    database.update_transaction_metadata_field(pid, "panel_email", "test@vpn.bot")

    tx = database.get_transaction_by_payment_id(pid)
    stored_meta = json.loads(tx["metadata"]) if isinstance(tx["metadata"], str) else tx["metadata"]
    assert stored_meta["panel_client_uuid"] == "uuid-abc-123"
    assert stored_meta["panel_email"] == "test@vpn.bot"
    assert stored_meta.get("user_id") == uid


# ────────────────────────────────────────────────────────────────
# TASK-5: Structured transaction tracing
# ────────────────────────────────────────────────────────────────

def test_trace_id_present_in_metadata():
    """trace_id must be preserved in transaction metadata (TASK-5)."""
    from shop_bot.data_manager import database

    pid = "trace-001"
    uid = 9006
    meta = _build_metadata(payment_id=pid, user_id=uid)
    meta["trace_id"] = "abc123def456"
    database.register_user_if_not_exists(uid, "u9006", None)
    database.create_pending_transaction(pid, uid, 100.0, meta)

    tx = database.get_transaction_by_payment_id(pid)
    stored_meta = json.loads(tx["metadata"]) if isinstance(tx["metadata"], str) else tx["metadata"]
    assert stored_meta["trace_id"] == "abc123def456"


# ────────────────────────────────────────────────────────────────
# TASK-6: Delivery SLA percentiles
# ────────────────────────────────────────────────────────────────

def test_delivery_time_percentiles_empty_returns_none():
    """No data should return p50=None, count=0 (TASK-6)."""
    from shop_bot.data_manager import database

    stats = database.get_delivery_time_percentiles(period_hours=24)
    assert stats["p50"] is None
    assert stats["count"] == 0


def test_delivery_time_percentiles_with_data():
    """With delivered transactions, percentiles should compute correctly (TASK-6)."""
    from shop_bot.data_manager import database

    for i in range(5):
        pid = f"sla-{i:03d}"
        uid = 9100 + i
        confirmed = (datetime.utcnow() - timedelta(seconds=30 * (i + 1))).isoformat(timespec="seconds")
        delivered = (datetime.utcnow() - timedelta(seconds=30 * i + 5)).isoformat(timespec="seconds")
        meta = _build_metadata(payment_id=pid, user_id=uid)
        meta["payment_confirmed_at"] = confirmed
        meta["key_delivered_at"] = delivered
        database.register_user_if_not_exists(uid, f"u{uid}", None)
        database.create_pending_transaction(pid, uid, 100.0, meta)
        database.mark_transaction_paid(pid)

    stats = database.get_delivery_time_percentiles(period_hours=24)
    assert stats["count"] == 5
    assert stats["p50"] is not None
    assert stats["p50"] >= 0


# ────────────────────────────────────────────────────────────────
# TASK-7: Stale pending by provider
# ────────────────────────────────────────────────────────────────

def test_stale_pending_excludes_yookassa():
    """get_stale_pending_by_provider must exclude YooKassa transactions (TASK-7)."""
    from shop_bot.data_manager import database

    old_date = (datetime.utcnow() - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M:%S")

    for provider, pid_suffix in [("yookassa", "yk"), ("cryptobot", "cb"), ("heleket", "hk")]:
        pid = f"stale-{pid_suffix}-001"
        uid = 9200 + hash(pid_suffix) % 100
        meta = _build_metadata(payment_id=pid, user_id=uid)
        meta["payment_method"] = provider
        database.register_user_if_not_exists(uid, f"u{uid}", None)
        database.create_pending_transaction(pid, uid, 100.0, meta)
        with database._create_connection() as conn:
            conn.execute(
                "UPDATE transactions SET created_date = ?, payment_method = ? WHERE payment_id = ?",
                (old_date, provider, pid),
            )
            conn.commit()

    stale = database.get_stale_pending_by_provider(
        max_age_hours=2, exclude_providers={"yookassa"}, limit=50,
    )
    providers_found = {tx["payment_method"] for tx in stale}
    assert "yookassa" not in providers_found
    assert "cryptobot" in providers_found or "heleket" in providers_found


# ────────────────────────────────────────────────────────────────
# TASK-12: Full jitter backoff
# ────────────────────────────────────────────────────────────────

def test_retry_backoff_has_minimum_floor():
    """_retry_backoff_seconds must return at least _RETRY_MIN_BACKOFF_SECONDS (TASK-12)."""
    from shop_bot.data_manager import database

    for attempt in range(10):
        delay = database._retry_backoff_seconds(attempt)
        assert delay >= database._RETRY_MIN_BACKOFF_SECONDS, (
            f"Attempt {attempt}: delay={delay} < min={database._RETRY_MIN_BACKOFF_SECONDS}"
        )


# ────────────────────────────────────────────────────────────────
# Runtime: Stale processing recovery
# ────────────────────────────────────────────────────────────────

def test_stale_processing_recovery():
    """Transactions stuck in processing for >120s must be recoverable."""
    from shop_bot.data_manager import database

    pid = "stale-proc-001"
    uid = 9300
    meta = _build_metadata(payment_id=pid, user_id=uid)
    database.register_user_if_not_exists(uid, "u9300", None)
    database.create_pending_transaction(pid, uid, 100.0, meta)

    old_time = (datetime.utcnow() - timedelta(seconds=database.TRANSACTION_LOCK_TTL_SECONDS + 120)).isoformat(timespec="seconds")
    with database._create_connection() as conn:
        conn.execute(
            """
            UPDATE transactions
            SET status = 'processing', last_attempt_at = ?, created_date = ?,
                locked_by = 'dead-worker', locked_at = ?
            WHERE payment_id = ?
            """,
            (old_time, old_time, old_time, pid),
        )
        conn.commit()

    recovered = database.recover_stale_processing_transactions(stale_seconds=120)
    assert len(recovered) >= 1

    tx = database.get_transaction_by_payment_id(pid)
    assert tx["status"] == "pending"
    assert tx["locked_by"] is None
    assert tx["locked_at"] is None

    lock_token = database.try_acquire_transaction_lock(pid)
    assert lock_token
    assert database.release_transaction_lock(pid, lock_token) is True
