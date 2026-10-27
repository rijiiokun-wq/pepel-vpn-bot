from __future__ import annotations

import hashlib
import hmac
import json

import pytest

pytest.importorskip("aiogram")

from shop_bot.errors import BotError, classify_error
from shop_bot.webhook_server import app as app_module


class _DummyLoop:
    def is_running(self):
        return True


class _DummyBotController:
    def __init__(self, bot_instance):
        self._bot_instance = bot_instance

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
        return self._bot_instance


def _build_app(monkeypatch, *, bot_instance=object(), settings: dict | None = None):
    settings_map = settings or {}
    monkeypatch.setattr(app_module, "BOT_SOURCE", "main")
    monkeypatch.setattr(app_module, "get_setting", lambda key: settings_map.get(key))
    app = app_module.create_webhook_app(_DummyBotController(bot_instance))
    app.config["TESTING"] = True
    app.config["EVENT_LOOP"] = _DummyLoop()
    return app


def _cryptobot_signature(raw_body: bytes, token: str) -> str:
    secret = hashlib.sha256(token.encode("utf-8")).digest()
    return hmac.new(secret, raw_body, hashlib.sha256).hexdigest()


def test_yookassa_missing_payment_id_sends_critical_alert(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()

    monkeypatch.setattr(
        app_module,
        "_resolve_trusted_yookassa_ip",
        lambda: ("185.71.76.1", ["185.71.76.1"]),
    )

    alerts: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alerts.append(
            {
                "error": error,
                "details": details,
                "respect_cooldown": respect_cooldown,
            }
        )
        return True

    monkeypatch.setattr(app_module, "send_critical_alert", _capture_alert)

    response = client.post(
        "/yookassa-webhook",
        json={
            "event": "payment.succeeded",
            "object": {"metadata": {"user_id": "1001"}},
        },
    )

    assert response.status_code == 200
    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.PAYMENT_KEY_ISSUE_FAILED
    assert alerts[0]["respect_cooldown"] is False
    assert "provider=YooKassa" in alerts[0]["details"]
    assert "reason=missing_payment_id" in alerts[0]["details"]
    assert "metadata_keys=user_id" in alerts[0]["details"]


def test_classify_error_detects_platega_failures():
    assert classify_error("Platega API timeout") == BotError.PAYMENT_PLATEGA_FAILED


def test_yookassa_canceled_marks_transaction_without_alert(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()

    monkeypatch.setattr(
        app_module,
        "_resolve_trusted_yookassa_ip",
        lambda: ("185.71.76.1", ["185.71.76.1"]),
    )

    marked: list[tuple[str, str]] = []

    def _mark(payment_id, payment_method=""):
        marked.append((payment_id, payment_method))
        return True

    alerts: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alerts.append({"error": error, "details": details})
        return True

    monkeypatch.setattr(app_module, "mark_transaction_canceled", _mark)
    monkeypatch.setattr(app_module, "send_critical_alert", _capture_alert)

    response = client.post(
        "/yookassa-webhook",
        json={
            "event": "payment.canceled",
            "object": {"id": "pay-cancel-001"},
        },
    )

    assert response.status_code == 200
    assert marked == [("pay-cancel-001", "YooKassa")]
    assert alerts == []


def test_yookassa_missing_required_metadata_fields_sends_precise_alert(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()

    monkeypatch.setattr(
        app_module,
        "_resolve_trusted_yookassa_ip",
        lambda: ("185.71.76.1", ["185.71.76.1"]),
    )
    monkeypatch.setattr(app_module, "is_transaction_fully_issued", lambda _pid: False)
    monkeypatch.setattr(app_module, "is_transaction_processing", lambda _pid: False)
    monkeypatch.setattr(app_module, "get_pending_transaction_metadata", lambda _pid: None)

    create_pending_calls: list[tuple] = []
    monkeypatch.setattr(
        app_module,
        "create_pending_transaction",
        lambda *args, **kwargs: create_pending_calls.append((args, kwargs)) or 0,
    )

    alerts: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alerts.append(
            {
                "error": error,
                "details": details,
                "respect_cooldown": respect_cooldown,
            }
        )
        return True

    monkeypatch.setattr(app_module, "send_critical_alert", _capture_alert)

    response = client.post(
        "/yookassa-webhook",
        json={
            "event": "payment.succeeded",
            "object": {
                "id": "pay-missing-meta-001",
                "metadata": {"user_id": "1002", "months": 3},
            },
        },
    )

    assert response.status_code == 200
    assert create_pending_calls == []
    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.PAYMENT_KEY_ISSUE_FAILED
    assert "provider=YooKassa" in alerts[0]["details"]
    assert "payment_id=pay-missing-meta-001" in alerts[0]["details"]
    assert "reason=missing_metadata_fields:price,action,key_id,host_name,plan_id" in alerts[0]["details"]
    assert "metadata_keys=months,user_id" in alerts[0]["details"]


def test_yookassa_processor_unavailable_sends_alert_and_resets(monkeypatch):
    app = _build_app(monkeypatch, bot_instance=object())
    client = app.test_client()

    monkeypatch.setattr(
        app_module,
        "_resolve_trusted_yookassa_ip",
        lambda: ("185.71.76.1", ["185.71.76.1"]),
    )
    monkeypatch.setattr(app_module, "is_transaction_fully_issued", lambda _pid: False)
    monkeypatch.setattr(app_module, "is_transaction_processing", lambda _pid: False)
    monkeypatch.setattr(
        app_module,
        "get_pending_transaction_metadata",
        lambda _pid: {
            "user_id": 1003,
            "months": 3,
            "price": 840.0,
            "action": "new",
            "key_id": 0,
            "host_name": "SUPER",
            "plan_id": 1,
            "plan_name": "Суперключ 3 месяца",
            "payment_method": "YooKassa",
        },
    )
    monkeypatch.setattr(app_module, "start_transaction_processing", lambda _pid: True)
    monkeypatch.setattr(app_module, "update_transaction_metadata", lambda *args, **kwargs: True)

    reset_calls: list[str] = []
    monkeypatch.setattr(
        app_module,
        "reset_transaction_to_pending",
        lambda payment_id: reset_calls.append(payment_id) or True,
    )
    monkeypatch.setattr(app_module.handlers, "process_successful_payment", None, raising=False)

    alerts: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alerts.append(
            {
                "error": error,
                "details": details,
                "respect_cooldown": respect_cooldown,
            }
        )
        return True

    monkeypatch.setattr(app_module, "send_critical_alert", _capture_alert)

    response = client.post(
        "/yookassa-webhook",
        json={
            "event": "payment.succeeded",
            "object": {"id": "pay-no-processor-001", "metadata": {}},
        },
    )

    assert response.status_code == 200
    assert reset_calls == ["pay-no-processor-001"]
    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.PAYMENT_KEY_ISSUE_RETRYING
    assert alerts[0]["respect_cooldown"] is True
    assert "reason=payment_processor_unavailable" in alerts[0]["details"]
    assert "payment_id=pay-no-processor-001" in alerts[0]["details"]


def test_yookassa_bot_unavailable_marks_retryable_metadata(monkeypatch):
    app = _build_app(monkeypatch, bot_instance=None)
    client = app.test_client()

    monkeypatch.setattr(
        app_module,
        "_resolve_trusted_yookassa_ip",
        lambda: ("185.71.76.1", ["185.71.76.1"]),
    )
    monkeypatch.setattr(app_module, "is_transaction_fully_issued", lambda _pid: False)
    monkeypatch.setattr(app_module, "is_transaction_processing", lambda _pid: False)
    monkeypatch.setattr(
        app_module,
        "get_pending_transaction_metadata",
        lambda _pid: {
            "user_id": 10031,
            "months": 1,
            "price": 300.0,
            "action": "new",
            "key_id": 0,
            "host_name": "SUPER",
            "plan_id": 0,
            "plan_name": "Суперключ 1 месяц",
            "payment_method": "YooKassa",
            "external_payment_id": "pay-no-bot-001",
        },
    )
    monkeypatch.setattr(app_module, "start_transaction_processing", lambda _pid: True)

    update_calls: list[dict] = []

    def _capture_update(payment_id, metadata, **kwargs):
        update_calls.append(
            {
                "payment_id": payment_id,
                "metadata": dict(metadata or {}),
                "kwargs": kwargs,
            }
        )
        return True

    monkeypatch.setattr(app_module, "update_transaction_metadata", _capture_update)

    reset_calls: list[str] = []
    monkeypatch.setattr(
        app_module,
        "reset_transaction_to_pending",
        lambda payment_id: reset_calls.append(payment_id) or True,
    )

    alerts: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alerts.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})
        return True

    monkeypatch.setattr(app_module, "send_critical_alert", _capture_alert)

    response = client.post(
        "/yookassa-webhook",
        json={
            "event": "payment.succeeded",
            "object": {"id": "pay-no-bot-001", "metadata": {}},
        },
    )

    assert response.status_code == 200
    assert reset_calls == ["pay-no-bot-001"]
    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.PAYMENT_KEY_ISSUE_RETRYING
    assert "reason=bot_unavailable" in alerts[0]["details"]

    retry_mark_call = next(
        (call for call in update_calls if call["metadata"].get("issuance_failed") is True),
        None,
    )
    assert retry_mark_call is not None
    marked_metadata = retry_mark_call["metadata"]
    assert marked_metadata["payment_confirmed"] is True
    assert marked_metadata["issuance_failure_alert_sent"] is True
    assert marked_metadata["issuance_failure_reason"] == "bot_unavailable"
    assert marked_metadata["retry_source"] == "pending_confirmed_retry"
    assert marked_metadata["issuance_failure_logs"]["stage"] == "webhook_dispatch"
    assert retry_mark_call["kwargs"].get("statuses") == ("pending", "processing")


def test_yookassa_wrong_bot_instance_resets_pending_and_skips_processing(monkeypatch):
    app = _build_app(monkeypatch, bot_instance=object())
    client = app.test_client()

    monkeypatch.setattr(
        app_module,
        "_resolve_trusted_yookassa_ip",
        lambda: ("185.71.76.1", ["185.71.76.1"]),
    )
    monkeypatch.setattr(app_module, "is_transaction_fully_issued", lambda _pid: False)
    monkeypatch.setattr(app_module, "is_transaction_processing", lambda _pid: False)
    monkeypatch.setattr(
        app_module,
        "get_pending_transaction_metadata",
        lambda _pid: {
            "user_id": 1004,
            "months": 3,
            "price": 840.0,
            "action": "new",
            "key_id": 0,
            "host_name": "SUPER",
            "plan_id": 1,
            "plan_name": "Суперключ 3 месяца",
            "payment_method": "YooKassa",
            "bot_source": "reserve",
        },
    )
    monkeypatch.setattr(app_module, "start_transaction_processing", lambda _pid: True)
    monkeypatch.setattr(app_module, "update_transaction_metadata", lambda *args, **kwargs: True)

    reset_calls: list[str] = []
    monkeypatch.setattr(
        app_module,
        "reset_transaction_to_pending",
        lambda payment_id: reset_calls.append(payment_id) or True,
    )

    scheduled: list[str] = []

    def _fake_schedule(*args, **kwargs):
        scheduled.append("scheduled")
        return None

    monkeypatch.setattr(app_module, "_schedule_payment_processing", _fake_schedule)

    alerts: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alerts.append({"error": error, "details": details})
        return True

    monkeypatch.setattr(app_module, "send_critical_alert", _capture_alert)

    response = client.post(
        "/yookassa-webhook",
        json={
            "event": "payment.succeeded",
            "object": {"id": "pay-wrong-bot-001", "metadata": {"bot_source": "reserve"}},
        },
    )

    assert response.status_code == 200
    assert scheduled == []
    assert reset_calls == ["pay-wrong-bot-001"]
    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.PAYMENT_KEY_ISSUE_FAILED
    assert "reason=wrong_bot_instance" in alerts[0]["details"]
    assert "payment_source=reserve" in alerts[0]["details"]
    assert "runtime_source=main" in alerts[0]["details"]


def test_yookassa_async_processor_exception_is_logged_and_reset(monkeypatch):
    app = _build_app(monkeypatch, bot_instance=object())
    client = app.test_client()

    monkeypatch.setattr(
        app_module,
        "_resolve_trusted_yookassa_ip",
        lambda: ("185.71.76.1", ["185.71.76.1"]),
    )
    monkeypatch.setattr(app_module, "is_transaction_fully_issued", lambda _pid: False)
    processing_state = {"active": False}
    monkeypatch.setattr(app_module, "is_transaction_processing", lambda _pid: processing_state["active"])
    monkeypatch.setattr(
        app_module,
        "get_pending_transaction_metadata",
        lambda _pid: {
            "user_id": 1005,
            "months": 1,
            "price": 300.0,
            "action": "new",
            "key_id": 0,
            "host_name": "SUPER",
            "plan_id": 0,
            "plan_name": "Суперключ 1 месяц",
            "payment_method": "YooKassa",
        },
    )
    monkeypatch.setattr(
        app_module,
        "start_transaction_processing",
        lambda _pid: processing_state.__setitem__("active", True) or True,
    )
    monkeypatch.setattr(app_module, "update_transaction_metadata", lambda *args, **kwargs: True)

    reset_calls: list[str] = []
    monkeypatch.setattr(
        app_module,
        "reset_transaction_to_pending",
        lambda payment_id: reset_calls.append(payment_id) or True,
    )

    async def _fake_process_successful_payment(_bot, _metadata):
        return None

    class _FailingFuture:
        def add_done_callback(self, callback):
            callback(self)

        def result(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(app_module.handlers, "process_successful_payment", _fake_process_successful_payment)

    def _fake_run_coroutine_threadsafe(_coro, _loop):
        _coro.close()
        return _FailingFuture()

    monkeypatch.setattr(app_module.asyncio, "run_coroutine_threadsafe", _fake_run_coroutine_threadsafe)

    alerts: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alerts.append({"error": error, "details": details})
        return True

    monkeypatch.setattr(app_module, "send_critical_alert", _capture_alert)

    response = client.post(
        "/yookassa-webhook",
        json={"event": "payment.succeeded", "object": {"id": "pay-async-crash-001", "metadata": {}}},
    )

    assert response.status_code == 200
    assert reset_calls == ["pay-async-crash-001"]
    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.PAYMENT_KEY_ISSUE_RETRYING
    assert "reason=async_processor_crashed:RuntimeError" in alerts[0]["details"]
    assert "payment_id=pay-async-crash-001" in alerts[0]["details"]


def test_cryptobot_invalid_signature_returns_403(monkeypatch):
    app = _build_app(monkeypatch, settings={"cryptobot_token": "token-123"})
    client = app.test_client()

    response = client.post(
        "/cryptobot-webhook",
        data=b'{"update_type":"invoice_paid","payload":{"payload":"x"}}',
        content_type="application/json",
        headers={"crypto-pay-api-signature": "deadbeef"},
    )

    assert response.status_code == 403


def test_cryptobot_empty_payload_sends_critical_alert(monkeypatch):
    token = "token-456"
    app = _build_app(monkeypatch, settings={"cryptobot_token": token})
    client = app.test_client()

    alerts: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alerts.append(
            {
                "error": error,
                "details": details,
                "respect_cooldown": respect_cooldown,
            }
        )
        return True

    monkeypatch.setattr(app_module, "send_critical_alert", _capture_alert)

    payload_dict = {
        "update_type": "invoice_paid",
        "payload": {
            "invoice_id": "inv-empty-001",
            "payload": "",
        },
    }
    raw_body = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
    signature = _cryptobot_signature(raw_body, token)

    response = client.post(
        "/cryptobot-webhook",
        data=raw_body,
        content_type="application/json",
        headers={"crypto-pay-api-signature": signature},
    )

    assert response.status_code == 200
    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.PAYMENT_KEY_ISSUE_FAILED
    assert alerts[0]["respect_cooldown"] is False
    assert "provider=CryptoBot" in alerts[0]["details"]
    assert "payment_id=inv-empty-001" in alerts[0]["details"]
    assert "reason=empty_payload" in alerts[0]["details"]


def test_platega_webhook_removed(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()

    response = client.post(
        "/platega-webhook",
        data=b"{}",
        content_type="application/json",
    )

    assert response.status_code == 404


def test_paypear_missing_webhook_secret_sends_critical_alert(monkeypatch):
    app = _build_app(monkeypatch, settings={"paypear_webhook_secret": ""})
    client = app.test_client()

    alerts: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alerts.append(
            {
                "error": error,
                "details": details,
                "respect_cooldown": respect_cooldown,
            }
        )
        return True

    monkeypatch.setattr(app_module, "send_critical_alert", _capture_alert)

    response = client.post(
        "/paypear-webhook",
        data=b"{}",
        content_type="application/json",
    )

    assert response.status_code == 500
    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.PAYMENT_PAYPEAR_FAILED
    assert alerts[0]["respect_cooldown"] is False
    assert "reason=webhook_secret_missing" in alerts[0]["details"]
    assert "path=/paypear-webhook" in alerts[0]["details"]


def test_ton_webhook_requires_authorization_when_key_configured(monkeypatch):
    app = _build_app(monkeypatch, settings={"tonapi_key": "ton-secret"})
    client = app.test_client()

    response = client.post("/ton-webhook", json={"tx_id": "tx-1", "in_progress_txs": [], "txs": []})

    assert response.status_code == 403


def test_ton_webhook_accepts_valid_authorization(monkeypatch):
    app = _build_app(monkeypatch, settings={"tonapi_key": "ton-secret"})
    client = app.test_client()

    response = client.post(
        "/ton-webhook",
        json={"tx_id": "tx-1", "in_progress_txs": [], "txs": []},
        headers={"Authorization": "Bearer ton-secret"},
    )

    assert response.status_code == 200


def test_yookassa_resolve_trusted_ip_ignores_spoofed_forwarded_headers(monkeypatch):
    app = _build_app(monkeypatch)

    with app.test_request_context(
        "/yookassa-webhook",
        method="POST",
        headers={
            "X-Forwarded-For": "185.71.76.1, 203.0.113.10",
            "X-Real-IP": "203.0.113.10",
        },
        environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
    ):
        trusted_ip, candidates = app_module._resolve_trusted_yookassa_ip()

    assert trusted_ip is None
    assert candidates == ["203.0.113.10"]


def test_yookassa_resolve_trusted_ip_from_x_real_ip_when_reverse_proxy(monkeypatch):
    app = _build_app(monkeypatch)

    with app.test_request_context(
        "/yookassa-webhook",
        method="POST",
        headers={
            "X-Forwarded-For": "203.0.113.10, 185.71.76.1",
            "X-Real-IP": "185.71.76.1",
        },
        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
    ):
        trusted_ip, candidates = app_module._resolve_trusted_yookassa_ip()

    assert trusted_ip == "185.71.76.1"
    assert candidates[0] == "185.71.76.1"
