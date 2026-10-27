import json
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


def _seed_paid_transaction(database, *, user_id: int, payment_id: str, bot_source: str, host_name: str):
    database.register_user_if_not_exists(user_id, f"user{user_id}", None)
    metadata = {
        "host_name": host_name,
        "plan_name": "1 month",
        "bot_source": bot_source,
    }
    database.log_transaction(
        username=f"user{user_id}",
        transaction_id=None,
        payment_id=payment_id,
        user_id=user_id,
        status="paid",
        amount_rub=100.0,
        amount_currency=None,
        currency_name=None,
        payment_method="Platega" if bot_source == "reserve" else "YooKassa",
        metadata=json.dumps(metadata),
        bot_source=bot_source,
    )


def test_dashboard_transactions_can_be_filtered_by_bot_source(_use_temp_db):
    from shop_bot.data_manager import database

    _seed_paid_transaction(
        database,
        user_id=7001,
        payment_id="tx-main-1",
        bot_source="main",
        host_name="main_hysteria2",
    )
    _seed_paid_transaction(
        database,
        user_id=7002,
        payment_id="tx-reserve-1",
        bot_source="reserve",
        host_name="reserve_hysteria2",
    )

    main_rows, main_total = database.get_paginated_transactions(
        page=1,
        per_page=20,
        status="paid",
        bot_source="main",
        days=30,
    )
    reserve_rows, reserve_total = database.get_paginated_transactions(
        page=1,
        per_page=20,
        status="paid",
        bot_source="reserve",
        days=30,
    )

    assert main_total == 1
    assert reserve_total == 1
    assert main_rows[0]["bot_source"] == "main"
    assert reserve_rows[0]["bot_source"] == "reserve"


def test_dashboard_analytics_and_type_summary_follow_bot_source_filter(_use_temp_db):
    from shop_bot.data_manager import database

    _seed_paid_transaction(
        database,
        user_id=7101,
        payment_id="tx-main-2",
        bot_source="main",
        host_name="main_hysteria2",
    )
    _seed_paid_transaction(
        database,
        user_id=7102,
        payment_id="tx-reserve-2",
        bot_source="reserve",
        host_name="reserve_hysteria2",
    )

    reserve_analytics = database.get_dashboard_analytics(
        days=30,
        status="paid",
        bot_source="reserve",
    )
    reserve_summary = database.get_transaction_type_summary(
        status="paid",
        bot_source="reserve",
        days=30,
    )

    assert reserve_analytics["totals"]["transactions"] == 1
    assert sum(int(v or 0) for v in reserve_summary.values()) == 1


def test_recent_and_user_transactions_support_bot_source_filter(_use_temp_db):
    from shop_bot.data_manager import database

    _seed_paid_transaction(
        database,
        user_id=7201,
        payment_id="tx-main-3",
        bot_source="main",
        host_name="main_hysteria2",
    )
    _seed_paid_transaction(
        database,
        user_id=7201,
        payment_id="tx-reserve-3",
        bot_source="reserve",
        host_name="reserve_hysteria2",
    )

    expiry_timestamp_ms = int((datetime.now() + timedelta(days=15)).timestamp() * 1000)
    database.add_new_key(
        user_id=7201,
        host_name="main_hysteria2",
        xui_client_uuid="uuid-main",
        key_email="u7201-main@vpn.bot",
        expiry_timestamp_ms=expiry_timestamp_ms,
        bot_source="main",
    )
    database.add_new_key(
        user_id=7201,
        host_name="reserve_hysteria2",
        xui_client_uuid="uuid-reserve",
        key_email="u7201-reserve@vpn.bot",
        expiry_timestamp_ms=expiry_timestamp_ms,
        bot_source="reserve",
    )

    reserve_transactions = database.get_user_transactions(
        7201,
        limit=20,
        statuses=("paid",),
        bot_source="reserve",
    )
    reserve_recent_keys = database.get_recent_transactions(limit=20, bot_source="reserve")

    assert len(reserve_transactions) == 1
    assert reserve_transactions[0]["bot_source"] == "reserve"
    assert any(row.get("bot_source") == "reserve" for row in reserve_recent_keys)
    assert all((row.get("bot_source") or "main") == "reserve" for row in reserve_recent_keys)
