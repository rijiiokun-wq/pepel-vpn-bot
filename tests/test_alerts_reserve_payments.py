from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from shop_bot.errors import BotError, classify_error


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


def test_classify_error_maps_platega_to_dedicated_error():
    assert classify_error("Platega API timeout during checkout") == BotError.PAYMENT_PLATEGA_FAILED


def test_classify_error_maps_panel_forbidden_login_to_host_auth_error():
    assert classify_error(
        "Неожиданная ошибка входа в панель. 403 Client Error: Forbidden for url: https://panel.example.com/login"
    ) == BotError.HOST_LOGIN_FAILED


def test_paypear_webhook_secret_missing_emits_critical_alert(monkeypatch):
    pytest.importorskip("aiogram")
    pytest.importorskip("py3xui")
    from shop_bot.webhook_server import app as app_module

    monkeypatch.setattr(
        app_module,
        "get_setting",
        lambda key: {"paypear_shop_id": "shop-id"}.get(key),
    )

    alert_calls: list[dict] = []

    def _capture(error, *, details="", respect_cooldown=True):
        alert_calls.append(
            {
                "error": error,
                "details": details,
                "respect_cooldown": respect_cooldown,
            }
        )
        return True

    monkeypatch.setattr(app_module, "send_critical_alert", _capture)

    app = app_module.create_webhook_app(_DummyBotController())
    app.config["TESTING"] = True
    client = app.test_client()

    response = client.post("/paypear-webhook", data=b"{}", content_type="application/json")

    assert response.status_code == 500
    assert len(alert_calls) == 1
    assert alert_calls[0]["error"] == BotError.PAYMENT_PAYPEAR_FAILED
    assert alert_calls[0]["respect_cooldown"] is False
    assert "reason=webhook_secret_missing" in alert_calls[0]["details"]
    assert "path=/paypear-webhook" in alert_calls[0]["details"]


def test_paypear_handler_contains_all_payment_alert_reasons():
    handlers_path = Path(__file__).resolve().parent.parent / "src" / "shop_bot" / "bot" / "handlers.py"
    source = handlers_path.read_text(encoding="utf-8")

    assert "BotError.PAYMENT_PAYPEAR_FAILED" in source
    assert "BotError.PAYMENT_PAYPEAR_CREATE_FAILED" in source
    assert "reason=create_payment_exception" in source
    assert "reason=invalid_create_payment_payload" in source
    assert "reason=pending_transaction_not_created" in source

    create_failure_window = source[
        source.index("reason=create_payment_exception") - 260 : source.index("reason=create_payment_exception") + 180
    ]
    assert "BotError.PAYMENT_PAYPEAR_CREATE_FAILED" in create_failure_window
    assert "user_id=" not in create_failure_window
    assert "order_id=" not in create_failure_window


def test_trial_success_path_does_not_emit_trial_consumed_alert():
    handlers_path = Path(__file__).resolve().parent.parent / "src" / "shop_bot" / "bot" / "handlers.py"
    source = handlers_path.read_text(encoding="utf-8")

    assert "BotError.TRIAL_ISSUE_CONSUMED" not in source
    assert "reason=trial_issue_consumed" not in source
