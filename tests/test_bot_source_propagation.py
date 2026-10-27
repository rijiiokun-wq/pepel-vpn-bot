import json
import sqlite3
from datetime import datetime, timedelta

import pytest


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_users.db"
    monkeypatch.setattr("shop_bot.data_manager.database.DB_FILE", db_file)
    monkeypatch.setattr("shop_bot.data_manager.database._WAL_INITIALIZED", False)

    from shop_bot.data_manager import database

    database.initialize_db()
    yield db_file


def _get_transaction_bot_source(db_file, payment_id: str) -> str | None:
    with sqlite3.connect(str(db_file)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT bot_source FROM transactions WHERE payment_id = ?", (payment_id,))
        row = cur.fetchone()
        return row[0] if row else None


def test_create_pending_transaction_falls_back_to_metadata_bot_source(_use_temp_db):
    from shop_bot.data_manager import database

    payment_id = "pending_meta_source"
    metadata = {"payment_method": "Platega", "bot_source": "reserve"}

    transaction_id = database.create_pending_transaction(
        payment_id=payment_id,
        user_id=1001,
        amount_rub=199.0,
        metadata=metadata,
    )

    assert transaction_id > 0
    assert _get_transaction_bot_source(_use_temp_db, payment_id) == "reserve"


def test_finalize_transaction_paid_preserves_existing_bot_source_when_none_passed(_use_temp_db):
    from shop_bot.data_manager import database

    payment_id = "pending_then_paid"
    metadata = {"payment_method": "Platega", "bot_source": "reserve"}
    database.create_pending_transaction(
        payment_id=payment_id,
        user_id=1002,
        amount_rub=299.0,
        metadata=metadata,
        bot_source="reserve",
    )

    finalized = database.finalize_transaction_paid(
        payment_id,
        username="user_1002",
        payment_method="Platega",
        amount_rub=299.0,
        metadata=json.dumps({"bot_source": "reserve"}),
        bot_source=None,
    )

    assert finalized is True
    assert _get_transaction_bot_source(_use_temp_db, payment_id) == "reserve"


def test_add_new_key_and_log_transaction_store_bot_source(_use_temp_db):
    from shop_bot.data_manager import database

    expiry_timestamp_ms = int((datetime.now() + timedelta(days=30)).timestamp() * 1000)
    key_id = database.add_new_key(
        user_id=1003,
        host_name="blitz_hysteria2",
        xui_client_uuid="uuid-1003",
        key_email="user1003@vpn.bot",
        expiry_timestamp_ms=expiry_timestamp_ms,
        bot_source="reserve",
    )
    assert key_id is not None

    with sqlite3.connect(str(_use_temp_db)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT bot_source FROM vpn_keys WHERE key_id = ?", (key_id,))
        assert cur.fetchone()[0] == "reserve"

    payment_id = "log_tx_source"
    database.log_transaction(
        username="user1003",
        transaction_id=None,
        payment_id=payment_id,
        user_id=1003,
        status="paid",
        amount_rub=100.0,
        amount_currency=None,
        currency_name=None,
        payment_method="Platega",
        metadata=json.dumps({"bot_source": "reserve"}),
        bot_source=None,
    )

    assert _get_transaction_bot_source(_use_temp_db, payment_id) == "reserve"


def test_add_new_key_uses_runtime_bot_source_when_argument_missing(_use_temp_db, monkeypatch):
    from shop_bot.data_manager import database

    monkeypatch.setenv("BOT_SOURCE", "reserve")
    expiry_timestamp_ms = int((datetime.now() + timedelta(days=7)).timestamp() * 1000)
    key_id = database.add_new_key(
        user_id=2001,
        host_name="blitz_hysteria2",
        xui_client_uuid="uuid-2001",
        key_email="user2001@vpn.bot",
        expiry_timestamp_ms=expiry_timestamp_ms,
    )
    assert key_id is not None

    with sqlite3.connect(str(_use_temp_db)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT bot_source FROM vpn_keys WHERE key_id = ?", (key_id,))
        assert cur.fetchone()[0] == "reserve"
