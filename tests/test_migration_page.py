import pytest

pytest.importorskip("aiogram")

from shop_bot.webhook_server import app as app_module


class _DummyBotController:
    def get_status(self):
        return {
            "shop_bot_running": True,
            "shop_bot_desired_running": True,
            "shop_autorestart_paused": False,
            "shop_last_failure": None,
            "shop_last_failure_at": None,
            "support_bot_running": False,
            "chatwoot_enabled": False,
        }


def test_migration_page_renders_with_base_template_context(monkeypatch):
    settings = {
        "flask_secret_key": "test-secret",
        "telegram_bot_token": "token",
        "telegram_bot_username": "bot",
        "admin_telegram_id": "1",
        "chatwoot_api_url": "",
    }

    monkeypatch.setattr(app_module, "BOT_SOURCE", "main")
    monkeypatch.setattr(app_module, "get_setting", lambda key: settings.get(key))
    monkeypatch.setattr(app_module, "get_all_settings", lambda: dict(settings))
    monkeypatch.setattr(app_module, "get_all_settings_cached", lambda: dict(settings))
    monkeypatch.setattr(
        app_module,
        "get_all_hosts",
        lambda: [
            {
                "host_name": "SUPER@",
                "host_url": "https://panel.example.test:2666/panel-path/",
                "status": "healthy",
                "is_maintenance": 0,
            }
        ],
    )

    app = app_module.create_webhook_app(_DummyBotController())
    app.config["TESTING"] = True

    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    response = client.get("/migration")

    assert response.status_code == 200
    assert "Миграция" in response.get_data(as_text=True)
