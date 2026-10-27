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


def test_platega_webhook_route_removed(monkeypatch):
    app = app_module.create_webhook_app(_DummyBotController())
    app.config["TESTING"] = True
    app.config["EVENT_LOOP"] = _DummyLoop()
    client = app.test_client()

    response = client.post("/platega-webhook", data=b"{}", content_type="application/json")
    assert response.status_code == 404
