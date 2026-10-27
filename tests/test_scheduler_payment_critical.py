from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

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


class _DummyBotController:
    def __init__(self, bot_instance=object()):
        self._bot_instance = bot_instance

    def get_bot_instance(self):
        return self._bot_instance


def _build_metadata(
    *,
    user_id: int,
    payment_id: str,
    payment_method: str = "YooKassa",
    bot_source: str = "main",
) -> dict:
    return {
        "user_id": user_id,
        "months": 3,
        "price": 840.0,
        "action": "new",
        "key_id": 0,
        "host_name": "SUPER",
        "plan_id": 1,
        "plan_name": "Суперключ 3 месяца",
        "customer_email": f"user{user_id}-key1@vpn.bot",
        "payment_method": payment_method,
        "external_payment_id": payment_id,
        "payment_confirmed": True,
        "bot_source": bot_source,
    }


def _read_tx(db_file: Path, payment_id: str) -> dict:
    with sqlite3.connect(str(db_file)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payment_id, status, issuance_state, metadata FROM transactions WHERE payment_id = ?",
            (payment_id,),
        )
        row = cursor.fetchone()
    assert row is not None
    return dict(row)


def test_scheduler_uses_service_interface_for_payment_processing() -> None:
    scheduler_source = Path(SRC_DIR, "shop_bot", "data_manager", "scheduler.py").read_text(encoding="utf-8")
    assert "from shop_bot.bot import handlers as bot_handlers" not in scheduler_source
    assert "process_confirmed_payment(" in scheduler_source


def test_retry_pending_confirmed_unpaid_not_counted_and_reset(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler

    user_id = 61001
    payment_id = "sched-retry-unpaid-001"
    metadata = _build_metadata(user_id=user_id, payment_id=payment_id)

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)
    monkeypatch.setattr(scheduler, "YOOKASSA_RECONCILE_MIN_AGE_SECONDS", 0)

    async def _fake_process_successful_payment(_bot, _metadata):
        return None

    monkeypatch.setattr(handlers, "process_successful_payment", _fake_process_successful_payment)

    stats = asyncio.run(
        scheduler.retry_pending_confirmed_transactions(_DummyBotController())
    )

    tx = _read_tx(_use_temp_db, payment_id)
    assert stats["checked"] == 1
    assert stats["recovered"] == 0
    assert stats["errors"] == 0
    assert tx["status"] == "pending"
    assert '"retry_source": "pending_confirmed_retry"' in str(tx["metadata"] or "")


def test_retry_pending_confirmed_preserves_fatal_state_after_processing(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler

    user_id = 61008
    payment_id = "sched-retry-fatal-001"
    metadata = _build_metadata(user_id=user_id, payment_id=payment_id)

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    async def _fake_process_successful_payment(_bot, tx_metadata):
        database.mark_transaction_fatal_failure(
            payment_id,
            reason="precheck_blocked:blitz_disabled",
            metadata=tx_metadata,
            payment_method="YooKassa",
            statuses=("processing", "pending"),
        )

    monkeypatch.setattr(handlers, "process_successful_payment", _fake_process_successful_payment)

    stats = asyncio.run(
        scheduler.retry_pending_confirmed_transactions(
            _DummyBotController(),
            min_age_seconds=0,
            limit=20,
            emit_sla_alert=False,
        )
    )

    tx = database.get_transaction_by_payment_id(payment_id)
    assert stats["checked"] == 1
    assert stats["recovered"] == 0
    assert stats["errors"] == 0
    assert isinstance(tx, dict)
    assert tx["status"] == "pending"
    assert tx["issuance_state"] == database.ISSUANCE_STATE_FATAL_FAILED
    assert tx["last_error"] == "precheck_blocked:blitz_disabled"


def test_reconcile_yookassa_unpaid_not_counted_and_reset(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler
    import yookassa

    user_id = 61002
    payment_id = "sched-reconcile-unpaid-001"
    metadata = _build_metadata(user_id=user_id, payment_id=payment_id)

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    monkeypatch.setattr(scheduler, "_last_yookassa_reconcile_at", None)
    monkeypatch.setattr(scheduler, "YOOKASSA_RECONCILE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(scheduler, "YOOKASSA_RECONCILE_MIN_AGE_SECONDS", 0)

    original_get_setting = scheduler.database.get_setting

    def _get_setting(key: str):
        if key == "yookassa_shop_id":
            return "shop-id"
        if key == "yookassa_secret_key":
            return "secret-key"
        return original_get_setting(key)

    monkeypatch.setattr(scheduler.database, "get_setting", _get_setting)
    monkeypatch.setattr(
        yookassa.Payment,
        "find_one",
        lambda _payment_id: SimpleNamespace(status="succeeded"),
    )

    async def _fake_process_successful_payment(_bot, _metadata):
        return None

    monkeypatch.setattr(handlers, "process_successful_payment", _fake_process_successful_payment)

    stats = asyncio.run(
        scheduler.reconcile_pending_yookassa_transactions(_DummyBotController())
    )

    tx = _read_tx(_use_temp_db, payment_id)
    assert stats["checked"] == 1
    assert stats["canceled"] == 0
    assert stats["recovered"] == 0
    assert stats["errors"] == 0
    assert tx["status"] == "pending"
    assert '"retry_source": "yookassa_reconcile"' in str(tx["metadata"] or "")


def test_reconcile_yookassa_canceled_marks_transaction(monkeypatch, _use_temp_db):
    from shop_bot.data_manager import database, scheduler
    import yookassa

    user_id = 61003
    payment_id = "sched-reconcile-canceled-001"
    metadata = _build_metadata(user_id=user_id, payment_id=payment_id)

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    monkeypatch.setattr(scheduler, "_last_yookassa_reconcile_at", None)
    monkeypatch.setattr(scheduler, "YOOKASSA_RECONCILE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(scheduler, "YOOKASSA_RECONCILE_MIN_AGE_SECONDS", 0)

    original_get_setting = scheduler.database.get_setting

    def _get_setting(key: str):
        if key == "yookassa_shop_id":
            return "shop-id"
        if key == "yookassa_secret_key":
            return "secret-key"
        return original_get_setting(key)

    monkeypatch.setattr(scheduler.database, "get_setting", _get_setting)
    monkeypatch.setattr(
        yookassa.Payment,
        "find_one",
        lambda _payment_id: SimpleNamespace(status="canceled"),
    )

    stats = asyncio.run(
        scheduler.reconcile_pending_yookassa_transactions(_DummyBotController())
    )

    tx = _read_tx(_use_temp_db, payment_id)
    assert stats["checked"] == 1
    assert stats["canceled"] == 1
    assert stats["recovered"] == 0
    assert stats["errors"] == 0
    assert tx["status"] == "canceled"


def test_reconcile_yookassa_restores_provider_metadata_when_local_metadata_is_incomplete(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler
    import yookassa

    user_id = 61004
    payment_id = "sched-reconcile-restore-001"

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(
        payment_id,
        user_id,
        300.0,
        {"payment_method": "YooKassa"},
    )

    monkeypatch.setattr(scheduler, "_last_yookassa_reconcile_at", None)
    monkeypatch.setattr(scheduler, "YOOKASSA_RECONCILE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(scheduler, "YOOKASSA_RECONCILE_MIN_AGE_SECONDS", 0)

    original_get_setting = scheduler.database.get_setting

    def _get_setting(key: str):
        if key == "yookassa_shop_id":
            return "shop-id"
        if key == "yookassa_secret_key":
            return "secret-key"
        return original_get_setting(key)

    monkeypatch.setattr(scheduler.database, "get_setting", _get_setting)
    monkeypatch.setattr(
        yookassa.Payment,
        "find_one",
        lambda _payment_id: SimpleNamespace(
            status="succeeded",
            metadata={
                "user_id": str(user_id),
                "months": "1",
                "price": "300.0",
                "action": "new",
                "key_id": "0",
                "host_name": "SUPER",
                "plan_id": "0",
                "plan_name": "Суперключ 1 месяц",
                "payment_method": "YooKassa",
                "bot_source": "main",
            },
            captured_at="2026-03-23T12:00:00+00:00",
        ),
    )

    async def _fake_process_successful_payment(_bot, metadata):
        database.finalize_transaction_paid(
            payment_id,
            username=f"user_{user_id}",
            payment_method="YooKassa",
            amount_rub=300.0,
            metadata=json.dumps(metadata),
        )

    monkeypatch.setattr(handlers, "process_successful_payment", _fake_process_successful_payment)

    stats = asyncio.run(
        scheduler.reconcile_pending_yookassa_transactions(_DummyBotController())
    )

    tx = _read_tx(_use_temp_db, payment_id)
    assert stats["checked"] == 1
    assert stats["recovered"] == 1
    assert stats["errors"] == 0
    assert tx["status"] == "paid"
    assert '"user_id": "61004"' in str(tx["metadata"] or "")
    assert '"retry_source": "yookassa_reconcile"' in str(tx["metadata"] or "")


def test_reconcile_yookassa_skips_foreign_bot_source(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler
    import yookassa

    user_id = 61005
    payment_id = "sched-reconcile-foreign-source-001"
    metadata = _build_metadata(user_id=user_id, payment_id=payment_id)
    metadata["bot_source"] = "reserve"

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    monkeypatch.setattr(scheduler, "_last_yookassa_reconcile_at", None)
    monkeypatch.setattr(scheduler, "YOOKASSA_RECONCILE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(scheduler, "YOOKASSA_RECONCILE_MIN_AGE_SECONDS", 0)

    original_get_setting = scheduler.database.get_setting

    def _get_setting(key: str):
        if key == "yookassa_shop_id":
            return "shop-id"
        if key == "yookassa_secret_key":
            return "secret-key"
        return original_get_setting(key)

    monkeypatch.setattr(scheduler.database, "get_setting", _get_setting)
    monkeypatch.setattr(
        yookassa.Payment,
        "find_one",
        lambda _payment_id: SimpleNamespace(status="succeeded", metadata=metadata),
    )

    async def _fake_process_successful_payment(_bot, _metadata):
        raise AssertionError("foreign-source payment must not be processed in this runtime")

    monkeypatch.setattr(handlers, "process_successful_payment", _fake_process_successful_payment)

    stats = asyncio.run(
        scheduler.reconcile_pending_yookassa_transactions(_DummyBotController())
    )

    tx = _read_tx(_use_temp_db, payment_id)
    assert stats["checked"] == 1
    assert stats["recovered"] == 0
    assert stats["errors"] == 0
    assert tx["status"] == "pending"


def test_reconcile_paypear_skips_foreign_bot_source_before_provider_call(monkeypatch, _use_temp_db):
    from shop_bot.data_manager import database, scheduler

    user_id = 61010
    payment_id = "sched-reconcile-paypear-foreign-001"
    metadata = _build_metadata(
        user_id=user_id,
        payment_id=payment_id,
        payment_method="PayPear",
        bot_source="reserve",
    )

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    monkeypatch.setattr(scheduler, "BOT_SOURCE", "main")
    monkeypatch.setattr(scheduler, "_last_paypear_reconcile_at", None)
    monkeypatch.setattr(scheduler, "PAYPEAR_RECONCILE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(scheduler, "PAYPEAR_RECONCILE_MIN_AGE_SECONDS", 0)

    original_get_setting = scheduler.database.get_setting

    def _get_setting(key: str):
        if key == "paypear_shop_id":
            return "shop-id"
        if key == "paypear_secret_key":
            return "secret-key"
        return original_get_setting(key)

    monkeypatch.setattr(scheduler.database, "get_setting", _get_setting)

    provider_calls: list[str] = []

    async def _fake_check_status(_payment_id: str):
        provider_calls.append(_payment_id)
        return {"status": "pending"}

    monkeypatch.setattr(scheduler.paypear_api, "check_status", _fake_check_status)

    stats = asyncio.run(
        scheduler.reconcile_pending_paypear_transactions(_DummyBotController())
    )

    tx = _read_tx(_use_temp_db, payment_id)
    assert stats["checked"] == 0
    assert stats["canceled"] == 0
    assert stats["recovered"] == 0
    assert stats["errors"] == 0
    assert provider_calls == []
    assert tx["status"] == "pending"


def test_reconcile_paypear_recovers_paid_pending_without_webhook(monkeypatch, _use_temp_db):
    from shop_bot.data_manager import database, scheduler

    user_id = 61011
    payment_id = "sched-reconcile-paypear-success-001"
    metadata = _build_metadata(
        user_id=user_id,
        payment_id=payment_id,
        payment_method="PayPear",
        bot_source="main",
    )

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    monkeypatch.setattr(scheduler, "BOT_SOURCE", "main")
    monkeypatch.setattr(scheduler, "_last_paypear_reconcile_at", None)
    monkeypatch.setattr(scheduler, "PAYPEAR_RECONCILE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(scheduler, "PAYPEAR_RECONCILE_MIN_AGE_SECONDS", 0)

    original_get_setting = scheduler.database.get_setting

    def _get_setting(key: str):
        if key == "paypear_shop_id":
            return "shop-id"
        if key == "paypear_secret_key":
            return "secret-key"
        return original_get_setting(key)

    monkeypatch.setattr(scheduler.database, "get_setting", _get_setting)

    async def _fake_check_status(_payment_id: str):
        return {"status": "succeeded", "provider_status": "confirmed"}

    monkeypatch.setattr(scheduler.paypear_api, "check_status", _fake_check_status)

    async def _fake_process_confirmed_payment(_bot, tx_metadata):
        database.finalize_transaction_paid(
            payment_id,
            username=f"user_{user_id}",
            payment_method="PayPear",
            amount_rub=840.0,
            metadata=json.dumps(tx_metadata),
            bot_source="main",
        )

    monkeypatch.setattr(scheduler, "process_confirmed_payment", _fake_process_confirmed_payment)

    stats = asyncio.run(
        scheduler.reconcile_pending_paypear_transactions(_DummyBotController())
    )

    tx = _read_tx(_use_temp_db, payment_id)
    assert stats["checked"] == 1
    assert stats["canceled"] == 0
    assert stats["recovered"] == 1
    assert stats["errors"] == 0
    assert tx["status"] == "paid"
    assert '"retry_source": "paypear_reconcile"' in str(tx["metadata"] or "")


def test_reconcile_platega_recovers_paid_pending_without_webhook(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler

    user_id = 61007
    payment_id = "sched-reconcile-platega-001"
    metadata = _build_metadata(
        user_id=user_id,
        payment_id=payment_id,
        payment_method="Platega",
        bot_source="reserve",
    )

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    monkeypatch.setattr(scheduler, "BOT_SOURCE", "reserve")
    monkeypatch.setattr(scheduler, "_last_platega_reconcile_at", None)
    monkeypatch.setattr(scheduler, "PLATEGA_RECONCILE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(scheduler, "PLATEGA_RECONCILE_MIN_AGE_SECONDS", 0)

    original_get_setting = scheduler.database.get_setting

    def _get_setting(key: str):
        if key == "platega_shop_id":
            return "shop-id"
        if key == "platega_api_key":
            return "secret-key"
        return original_get_setting(key)

    monkeypatch.setattr(scheduler.database, "get_setting", _get_setting)

    async def _fake_check_status(_payment_id: str):
        return {"status": "succeeded", "provider_status": "confirmed"}

    monkeypatch.setattr(scheduler.platega_api, "check_status", _fake_check_status)

    async def _fake_process_successful_payment(_bot, tx_metadata):
        database.finalize_transaction_paid(
            payment_id,
            username=f"user_{user_id}",
            payment_method="Platega",
            amount_rub=840.0,
            metadata=json.dumps(tx_metadata),
            bot_source="reserve",
        )

    monkeypatch.setattr(handlers, "process_successful_payment", _fake_process_successful_payment)

    stats = asyncio.run(
        scheduler.reconcile_pending_platega_transactions(_DummyBotController())
    )

    tx = _read_tx(_use_temp_db, payment_id)
    assert stats["checked"] == 1
    assert stats["canceled"] == 0
    assert stats["recovered"] == 1
    assert stats["errors"] == 0
    assert tx["status"] == "paid"
    assert '"retry_source": "platega_reconcile"' in str(tx["metadata"] or "")


def test_reconcile_platega_preserves_fatal_state_after_processing(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler

    user_id = 61009
    payment_id = "sched-reconcile-platega-fatal-001"
    metadata = _build_metadata(
        user_id=user_id,
        payment_id=payment_id,
        payment_method="Platega",
        bot_source="reserve",
    )

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    monkeypatch.setattr(scheduler, "BOT_SOURCE", "reserve")
    monkeypatch.setattr(scheduler, "_last_platega_reconcile_at", None)
    monkeypatch.setattr(scheduler, "PLATEGA_RECONCILE_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(scheduler, "PLATEGA_RECONCILE_MIN_AGE_SECONDS", 0)

    original_get_setting = scheduler.database.get_setting

    def _get_setting(key: str):
        if key == "platega_shop_id":
            return "shop-id"
        if key == "platega_api_key":
            return "secret-key"
        return original_get_setting(key)

    monkeypatch.setattr(scheduler.database, "get_setting", _get_setting)

    async def _fake_check_status(_payment_id: str):
        return {"status": "succeeded", "provider_status": "confirmed"}

    monkeypatch.setattr(scheduler.platega_api, "check_status", _fake_check_status)

    async def _fake_process_successful_payment(_bot, tx_metadata):
        database.mark_transaction_fatal_failure(
            payment_id,
            reason="precheck_blocked:blitz_disabled",
            metadata=tx_metadata,
            payment_method="Platega",
            statuses=("processing", "pending"),
        )

    monkeypatch.setattr(handlers, "process_successful_payment", _fake_process_successful_payment)

    stats = asyncio.run(
        scheduler.reconcile_pending_platega_transactions(_DummyBotController())
    )

    tx = database.get_transaction_by_payment_id(payment_id)
    assert stats["checked"] == 1
    assert stats["canceled"] == 0
    assert stats["recovered"] == 0
    assert stats["errors"] == 0
    assert isinstance(tx, dict)
    assert tx["status"] == "pending"
    assert tx["issuance_state"] == database.ISSUANCE_STATE_FATAL_FAILED
    assert tx["last_error"] == "precheck_blocked:blitz_disabled"


def test_retry_pending_confirmed_recovers_paid_awaiting_payment_anomaly(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler

    user_id = 61006
    payment_id = "sched-retry-paid-awaiting-001"
    metadata = _build_metadata(user_id=user_id, payment_id=payment_id)

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    with sqlite3.connect(str(_use_temp_db)) as conn:
        conn.execute(
            """
            UPDATE transactions
            SET status = 'paid',
                issuance_state = 'awaiting_payment'
            WHERE payment_id = ?
            """,
            (payment_id,),
        )
        conn.commit()

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

    tx = _read_tx(_use_temp_db, payment_id)
    assert stats["checked"] == 1
    assert stats["recovered"] == 1
    assert stats["errors"] == 0
    assert tx["status"] == "paid"
    assert tx["issuance_state"] == "issued"
