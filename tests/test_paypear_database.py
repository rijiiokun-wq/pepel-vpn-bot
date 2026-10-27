import pytest

from shop_bot.data_manager import database


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_users.db"
    monkeypatch.setattr(database, "DB_FILE", db_file)
    monkeypatch.setattr(database, "_WAL_INITIALIZED", False)
    database.initialize_db()
    return db_file


def test_get_pending_paypear_transaction_by_order_id_returns_provider_payment_id(temp_db):
    user_id = 100000001
    payment_id = "paypear-provider-id"
    order_id = "paypear-order-id"
    database.register_user_if_not_exists(user_id, "paypear_user", None)

    metadata = {
        "user_id": user_id,
        "months": 1,
        "price": 300.0,
        "action": "new",
        "key_id": 0,
        "host_name": "test-host",
        "plan_id": 1,
        "plan_name": "test-plan",
        "payment_method": "PayPear",
        "paypear_order_id": order_id,
        "external_payment_id": payment_id,
        "bot_source": "reserve",
    }
    transaction_id = database.create_pending_transaction(
        payment_id=payment_id,
        user_id=user_id,
        amount_rub=300.0,
        metadata=metadata,
    )

    found = database.get_pending_paypear_transaction_by_order_id(order_id)

    assert transaction_id > 0
    assert found is not None
    assert found["payment_id"] == payment_id
    assert found["metadata"]["paypear_order_id"] == order_id
    assert database.get_pending_paypear_transaction_by_order_id("missing-order") is None
