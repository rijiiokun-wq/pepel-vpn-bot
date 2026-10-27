from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
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


def _build_base_metadata(*, payment_id: str, user_id: int, host_name: str) -> dict:
    return {
        "user_id": user_id,
        "months": 1,
        "price": 300.0,
        "action": "new",
        "key_id": 0,
        "host_name": host_name,
        "plan_id": 1,
        "plan_name": "Суперключ 1 месяц",
        "customer_email": f"user{user_id}-key1@vpn.bot",
        "payment_method": "YooKassa",
        "external_payment_id": payment_id,
        "payment_confirmed": True,
    }


def _parse_db_datetime(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    return parsed


def _create_retryable_sla_breach(
    database,
    *,
    payment_id: str,
    user_id: int,
    host_name: str,
    confirmed_at: datetime,
) -> dict:
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id, host_name=host_name)
    metadata["issuance_failure_reason"] = "key_creation_failed"
    metadata["issuance_failed"] = True

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 300.0, metadata)
    assert database.mark_transaction_confirmed(
        payment_id,
        metadata=metadata,
        payment_method="YooKassa",
        confirmed_at=confirmed_at,
    )
    marked = database.mark_transaction_retryable_failure(
        payment_id,
        reason="key_creation_failed",
        metadata=metadata,
        payment_method="YooKassa",
        next_retry_at=confirmed_at + timedelta(minutes=1),
        statuses=("pending",),
    )
    assert marked["updated"] is True
    return metadata


def test_issuance_failure_schedules_immediate_retry(_use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database

    payment_id = "auto-retry-immediate-001"
    user_id = 72001
    host_name = "HOST_AUTO_1"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id, host_name=host_name)

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 300.0, metadata)

    before_mark = datetime.now() - timedelta(seconds=2)
    handlers._mark_transaction_issuance_failure(
        payment_id=payment_id,
        metadata=metadata,
        reason="host_unavailable_at_issuance",
        failure_logs={"stage": "test"},
    )

    tx = database.get_transaction_by_payment_id(payment_id)
    assert isinstance(tx, dict)
    assert tx["status"] == "pending"
    assert int(tx.get("retry_count") or 0) >= 1
    next_retry_at = _parse_db_datetime(tx.get("next_retry_at"))
    assert next_retry_at is not None
    assert before_mark <= next_retry_at <= datetime.now() + timedelta(seconds=10)


def test_retryable_sla_breach_alert_claimed_once(monkeypatch, _use_temp_db):
    from shop_bot.data_manager import database, scheduler
    from shop_bot.errors import BotError

    monkeypatch.setattr(scheduler, "PAYMENT_ISSUANCE_SLA_SECONDS", 60)

    first_payment_id = "sla-breach-once-001"
    second_payment_id = "sla-breach-once-002"
    old_confirmed_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=20)
    _create_retryable_sla_breach(
        database,
        payment_id=first_payment_id,
        user_id=72101,
        host_name="HOST_SLA_1",
        confirmed_at=old_confirmed_at,
    )

    captured: list[dict] = []

    def _capture_alert(error: BotError, *, details: str = "", respect_cooldown: bool = True) -> None:
        captured.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    assert scheduler._emit_issuance_sla_alert_if_needed() == 1
    assert [call["error"] for call in captured] == [BotError.PAYMENT_KEY_ISSUE_FAILED]
    assert "reason=issuance_sla_breached" in captured[0]["details"]
    assert first_payment_id in captured[0]["details"]
    assert captured[0]["respect_cooldown"] is True

    tx = database.get_transaction_by_payment_id(first_payment_id)
    parsed_metadata = json.loads(tx["metadata"]) if tx and tx.get("metadata") else {}
    assert parsed_metadata.get("issuance_sla_alert_sent") is True
    assert parsed_metadata.get("issuance_sla_alert_at")
    assert database.get_retryable_failures_over_sla(sla_seconds=60, limit=20) == []

    assert scheduler._emit_issuance_sla_alert_if_needed() == 0
    assert len(captured) == 1

    _create_retryable_sla_breach(
        database,
        payment_id=second_payment_id,
        user_id=72102,
        host_name="HOST_SLA_2",
        confirmed_at=old_confirmed_at,
    )

    assert scheduler._emit_issuance_sla_alert_if_needed() == 1
    assert len(captured) == 2
    assert second_payment_id in captured[1]["details"]
    assert first_payment_id not in captured[1]["details"]


def test_autoretry_exhausted_alert_sent_once_for_dead_host(monkeypatch, _use_temp_db):
    from shop_bot.data_manager import database, scheduler
    from shop_bot.errors import BotError

    payment_id = "auto-retry-exhausted-001"
    user_id = 72002
    host_name = "HOST_DEAD_1"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id, host_name=host_name)
    metadata["issuance_failure_reason"] = "host_unavailable_at_issuance"
    metadata["issuance_failed"] = True

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_host(host_name, "https://example.invalid", "u", "p", 1)
    database.update_host_status(host_name, "unavailable", "down")
    database.create_pending_transaction(payment_id, user_id, 300.0, metadata)

    for _ in range(8):
        database.mark_transaction_retryable_failure(
            payment_id,
            reason="host_unavailable_at_issuance",
            metadata=metadata,
            payment_method="YooKassa",
            next_retry_at=datetime.now(),
            statuses=("pending",),
        )

    captured: list[dict] = []

    def _capture_alert(error: BotError, *, details: str = "", respect_cooldown: bool = True) -> None:
        captured.append(
            {
                "error": error,
                "details": details,
                "respect_cooldown": respect_cooldown,
            }
        )

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    exhausted_count = scheduler._emit_autoretry_exhausted_alerts(max_attempts=8, limit=20)
    assert exhausted_count == 1
    assert len(captured) == 1
    assert captured[0]["error"] == BotError.PAYMENT_KEY_ISSUE_FAILED
    assert "reason=autoretry_exhausted_host_dead" in captured[0]["details"]
    assert f"payment_id={payment_id}" in captured[0]["details"]

    tx = database.get_transaction_by_payment_id(payment_id)
    parsed_metadata = json.loads(tx["metadata"]) if tx and tx.get("metadata") else {}
    assert parsed_metadata.get("autoretry_exhausted_alert_sent") is True

    exhausted_count_second = scheduler._emit_autoretry_exhausted_alerts(max_attempts=8, limit=20)
    assert exhausted_count_second == 0
    assert len(captured) == 1


def test_autoretry_skips_disabled_blitz_host(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler
    from shop_bot.modules import blitz_api

    class _DummyBotController:
        def get_bot_instance(self):
            return object()

    payment_id = "auto-retry-blitz-disabled-001"
    user_id = 72004
    metadata = _build_base_metadata(
        payment_id=payment_id,
        user_id=user_id,
        host_name=blitz_api.BLITZ_HOST_NAME,
    )

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 300.0, metadata)
    before_state = database.mark_transaction_retryable_failure(
        payment_id,
        reason="key_creation_failed",
        metadata=metadata,
        payment_method="YooKassa",
        next_retry_at=datetime.now(UTC),
        statuses=("pending",),
    )

    async def _unexpected_process(_bot, _metadata):
        pytest.fail("disabled Blitz host entered payment processing")

    monkeypatch.setattr(handlers, "process_successful_payment", _unexpected_process)

    stats = asyncio.run(
        scheduler.retry_pending_confirmed_transactions(
            _DummyBotController(),
            only_retryable_failed=True,
            min_age_seconds=0,
            limit=20,
            emit_sla_alert=False,
        )
    )

    tx = database.get_transaction_by_payment_id(payment_id)
    assert int(stats.get("checked") or 0) == 0
    assert isinstance(tx, dict)
    assert int(tx.get("retry_count") or 0) == int(before_state.get("retry_count") or 0)
    assert tx.get("issuance_state") == database.ISSUANCE_STATE_RETRYABLE_FAILED


def test_purchase_precheck_blocks_blitz_when_disabled(_use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.modules import blitz_api

    result = handlers._run_purchase_precheck(
        blitz_api.BLITZ_HOST_NAME,
        is_super_key_flow=False,
    )

    assert result.allowed is False
    assert result.reason == "blitz_disabled"


def test_provider_reconcile_ignores_fatal_disabled_blitz(_use_temp_db):
    from shop_bot.data_manager import database
    from shop_bot.modules import blitz_api

    payment_id = "provider-fatal-blitz-001"
    user_id = 72006
    metadata = _build_base_metadata(
        payment_id=payment_id,
        user_id=user_id,
        host_name=blitz_api.BLITZ_HOST_NAME,
    )
    metadata["payment_method"] = "Platega"

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 300.0, metadata)
    marked = database.mark_transaction_fatal_failure(
        payment_id,
        reason="precheck_blocked:blitz_disabled",
        metadata=metadata,
        payment_method="Platega",
        statuses=("pending",),
    )

    candidates = database.get_pending_platega_transactions(
        limit=20,
        min_age_seconds=0,
    )

    assert marked is True
    assert all(row.get("payment_id") != payment_id for row in candidates)


def test_autoretry_skips_maintenance_xui_host(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler

    class _DummyBotController:
        def get_bot_instance(self):
            return object()

    payment_id = "auto-retry-maintenance-001"
    user_id = 72005
    host_name = "HOST_MAINT_1"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id, host_name=host_name)

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_host(host_name, "https://example.invalid", "u", "p", 1)
    database.set_host_maintenance(host_name, True)
    database.create_pending_transaction(payment_id, user_id, 300.0, metadata)
    before_state = database.mark_transaction_retryable_failure(
        payment_id,
        reason="key_creation_failed",
        metadata=metadata,
        payment_method="YooKassa",
        next_retry_at=datetime.now(UTC),
        statuses=("pending",),
    )

    async def _unexpected_process(_bot, _metadata):
        pytest.fail("maintenance host entered payment processing")

    monkeypatch.setattr(handlers, "process_successful_payment", _unexpected_process)

    stats = asyncio.run(
        scheduler.retry_pending_confirmed_transactions(
            _DummyBotController(),
            only_retryable_failed=True,
            min_age_seconds=0,
            limit=20,
            emit_sla_alert=False,
        )
    )

    tx = database.get_transaction_by_payment_id(payment_id)
    assert int(stats.get("checked") or 0) == 0
    assert isinstance(tx, dict)
    assert int(tx.get("retry_count") or 0) == int(before_state.get("retry_count") or 0)
    assert tx.get("issuance_state") == database.ISSUANCE_STATE_RETRYABLE_FAILED


def test_daily_catchup_ignores_next_retry_at_delay(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler

    class _DummyBotController:
        def __init__(self, bot_instance=object()):
            self._bot_instance = bot_instance

        def get_bot_instance(self):
            return self._bot_instance

    payment_id = "daily-catchup-001"
    user_id = 72003
    host_name = "HOST_DAILY_1"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id, host_name=host_name)

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 300.0, metadata)
    database.mark_transaction_retryable_failure(
        payment_id,
        reason="key_creation_failed",
        metadata=metadata,
        payment_method="YooKassa",
        next_retry_at=datetime.now() + timedelta(hours=12),
        statuses=("pending",),
    )

    async def _fake_process_successful_payment(_bot, tx_metadata):
        database.mark_transaction_paid(tx_metadata["external_payment_id"], payment_method="YooKassa")

    monkeypatch.setattr(handlers, "process_successful_payment", _fake_process_successful_payment)

    stats = asyncio.run(
        scheduler.retry_pending_confirmed_transactions(
            _DummyBotController(),
            retry_source="daily_catchup_scan",
            due_only=False,
            only_retryable_failed=False,
            min_age_seconds=0,
            limit=20,
            emit_sla_alert=False,
        )
    )

    tx = database.get_transaction_by_payment_id(payment_id)
    assert int(stats.get("checked") or 0) == 1
    assert int(stats.get("recovered") or 0) == 1
    assert isinstance(tx, dict)
    assert tx["status"] == "paid"
