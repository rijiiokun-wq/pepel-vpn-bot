import hashlib
import hmac
import json

import pytest

pytest.importorskip("aiogram")

from shop_bot.webhook_server import app as app_module


class _DummyLoop:
    def is_running(self):
        return True


class _DummyBotController:
    def get_status(self):
        return {
            "shop_bot_running": False,
            "shop_bot_desired_running": False,
            "shop_autorestart_paused": False,
            "shop_last_failure": None,
            "shop_last_failure_at": None,
            "support_bot_running": False,
            "chatwoot_enabled": False,
        }

    def get_bot_instance(self):
        return object()


def _sign(payload: dict, secret: str) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    body = json.dumps(unsigned, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _build_app(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "get_setting",
        lambda key: "secret" if key == "paypear_webhook_secret" else None,
    )
    app = app_module.create_webhook_app(_DummyBotController())
    app.config["TESTING"] = True
    app.config["EVENT_LOOP"] = _DummyLoop()
    return app


def test_paypear_webhook_invalid_signature_returns_403(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()

    payload = {
        "event": "payment.confirmed",
        "object": {"id": "tx-1", "status": "CONFIRMED"},
        "signature": "deadbeef",
    }
    response = client.post(
        "/paypear-webhook",
        data=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        content_type="application/json",
    )

    assert response.status_code == 403


def test_paypear_webhook_confirmed_is_idempotent(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()

    calls = {"run": 0, "update": 0}
    issued_state = {"issued": False}
    pending_metadata = {
        "user_id": 1,
        "months": 1,
        "price": 100.0,
        "action": "new",
        "key_id": 0,
        "host_name": "test-host",
        "plan_id": 1,
        "plan_name": "test-plan",
        "payment_method": "PayPear",
        "bot_source": "main",
    }

    async def _fake_process_successful_payment(_bot, _metadata):
        pass

    def _fake_run_coroutine_threadsafe(_coro, _loop):
        calls["run"] += 1
        _coro.close()

    monkeypatch.setattr(app_module.handlers, "process_successful_payment", _fake_process_successful_payment)
    monkeypatch.setattr(app_module.asyncio, "run_coroutine_threadsafe", _fake_run_coroutine_threadsafe)
    monkeypatch.setattr(app_module, "is_transaction_fully_issued", lambda _payment_id: issued_state["issued"])
    monkeypatch.setattr(app_module, "is_transaction_processing", lambda _payment_id: False)
    monkeypatch.setattr(app_module, "get_pending_transaction_metadata", lambda _payment_id: dict(pending_metadata))
    monkeypatch.setattr(app_module, "start_transaction_processing", lambda _payment_id: True)
    monkeypatch.setattr(
        app_module,
        "update_transaction_metadata",
        lambda _payment_id, _metadata, statuses=("pending",): calls.__setitem__("update", calls["update"] + 1) or True,
    )

    payload = {
        "event": "payment.confirmed",
        "object": {"id": "tx-2", "status": "CONFIRMED"},
    }
    payload["signature"] = _sign(payload, "secret")
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    first = client.post("/paypear-webhook", data=raw, content_type="application/json")
    assert first.status_code == 200
    assert calls["run"] == 1

    issued_state["issued"] = True
    second = client.post("/paypear-webhook", data=raw, content_type="application/json")
    assert second.status_code == 200
    assert calls["run"] == 1


def test_paypear_webhook_accepts_official_signature_header(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()

    calls = {"run": 0}
    pending_metadata = {
        "user_id": 1,
        "months": 1,
        "price": 100.0,
        "action": "new",
        "key_id": 0,
        "host_name": "test-host",
        "plan_id": 1,
        "plan_name": "test-plan",
        "payment_method": "PayPear",
        "bot_source": "main",
    }

    async def _fake_process_successful_payment(_bot, _metadata):
        pass

    def _fake_run_coroutine_threadsafe(_coro, _loop):
        calls["run"] += 1
        _coro.close()

    monkeypatch.setattr(app_module.handlers, "process_successful_payment", _fake_process_successful_payment)
    monkeypatch.setattr(app_module.asyncio, "run_coroutine_threadsafe", _fake_run_coroutine_threadsafe)
    monkeypatch.setattr(app_module, "is_transaction_fully_issued", lambda _payment_id: False)
    monkeypatch.setattr(app_module, "is_transaction_processing", lambda _payment_id: False)
    monkeypatch.setattr(app_module, "get_pending_transaction_metadata", lambda _payment_id: dict(pending_metadata))
    monkeypatch.setattr(app_module, "start_transaction_processing", lambda _payment_id: True)
    monkeypatch.setattr(app_module, "update_transaction_metadata", lambda *_args, **_kwargs: True)

    payload = {
        "id": "tx-header",
        "status": "COMPLETED",
        "referenceId": "order-header",
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    response = client.post(
        "/paypear-webhook",
        data=raw,
        content_type="application/json",
        headers={"Signature": _sign(payload, "secret")},
    )

    assert response.status_code == 200
    assert calls["run"] == 1


def test_paypear_webhook_resolves_pending_transaction_by_reference_id(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()

    calls = {"run": 0, "started_payment_id": ""}
    pending_metadata = {
        "user_id": 1,
        "months": 1,
        "price": 100.0,
        "action": "new",
        "key_id": 0,
        "host_name": "test-host",
        "plan_id": 1,
        "plan_name": "test-plan",
        "payment_method": "PayPear",
        "paypear_order_id": "order-ref",
        "bot_source": "main",
    }

    async def _fake_process_successful_payment(_bot, _metadata):
        pass

    def _fake_run_coroutine_threadsafe(_coro, _loop):
        calls["run"] += 1
        _coro.close()

    def _fake_start_transaction_processing(payment_id):
        calls["started_payment_id"] = payment_id
        return True

    monkeypatch.setattr(app_module.handlers, "process_successful_payment", _fake_process_successful_payment)
    monkeypatch.setattr(app_module.asyncio, "run_coroutine_threadsafe", _fake_run_coroutine_threadsafe)
    monkeypatch.setattr(app_module, "is_transaction_fully_issued", lambda _payment_id: False)
    monkeypatch.setattr(app_module, "is_transaction_processing", lambda _payment_id: False)
    monkeypatch.setattr(app_module, "get_pending_transaction_metadata", lambda _payment_id: None)
    monkeypatch.setattr(
        app_module,
        "get_pending_paypear_transaction_by_order_id",
        lambda order_id: {
            "payment_id": "tx-local",
            "metadata": dict(pending_metadata),
        } if order_id == "order-ref" else None,
    )
    monkeypatch.setattr(app_module, "start_transaction_processing", _fake_start_transaction_processing)
    monkeypatch.setattr(app_module, "update_transaction_metadata", lambda *_args, **_kwargs: True)

    payload = {
        "status": "COMPLETED",
        "referenceId": "order-ref",
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    response = client.post(
        "/paypear-webhook",
        data=raw,
        content_type="application/json",
        headers={"Signature": _sign(payload, "secret")},
    )

    assert response.status_code == 200
    assert calls["started_payment_id"] == "tx-local"
    assert calls["run"] == 1
