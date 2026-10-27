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


def _build_app(monkeypatch):
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
                "host_name": "SUPER",
                "host_url": "https://super.example/panel",
                "status": "healthy",
                "is_maintenance": 0,
            }
        ],
    )
    app = app_module.create_webhook_app(_DummyBotController())
    app.config["TESTING"] = True
    return app


def _login(client):
    with client.session_transaction() as session:
        session["logged_in"] = True


def test_migration_page_issue_hosts_include_blitz_option(monkeypatch):
    monkeypatch.setattr(app_module.blitz_api, "is_blitz_enabled", lambda: True)

    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.get("/migration")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert app_module.blitz_api.BLITZ_HOST_NAME in body
    assert "Blitz (Hysteria 2)" in body


def test_issue_by_id_executes_blitz_path(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    monkeypatch.setattr(app_module.blitz_api, "is_blitz_enabled", lambda: True)
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": "SUPER"} if host_name == "SUPER" else None,
    )
    monkeypatch.setattr(app_module, "get_user", lambda user_id: {"telegram_id": user_id})
    monkeypatch.setattr(app_module, "register_user_if_not_exists", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "get_next_key_number", lambda user_id: 1)
    monkeypatch.setattr(app_module, "get_key_by_email", lambda key_email: None)

    captured = {}

    def _add_new_key(**kwargs):
        captured.update(kwargs)
        return 777

    monkeypatch.setattr(app_module, "add_new_key", _add_new_key)

    async def _xui_should_not_be_used(*args, **kwargs):
        raise AssertionError("XUI path must not be used for Blitz host")

    monkeypatch.setattr(app_module.xui_api, "create_or_update_key_on_host", _xui_should_not_be_used)
    monkeypatch.setattr(app_module.blitz_api, "create_user", lambda **kwargs: _async_value({"detail": "ok"}))
    monkeypatch.setattr(
        app_module.blitz_api,
        "get_connection_uri",
        lambda username: _async_value("hysteria2://demo-key"),
    )

    response = client.post(
        "/api/issue-by-id/execute",
        json={
            "telegram_id": "42",
            "host_name": app_module.blitz_api.BLITZ_HOST_NAME,
            "duration_days": 30,
            "duration_months": 0,
            "duration_years": 0,
            "unlimited": False,
            "send_message": False,
            "replace_existing": False,
            "bot_source": "main",
        },
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["status"] == "ok"
    assert data["host_name"] == app_module.blitz_api.BLITZ_HOST_NAME
    assert data["key_id"] == 777
    assert data["issued_key_value"] == "hysteria2://demo-key"
    assert captured["host_name"] == app_module.blitz_api.BLITZ_HOST_NAME
    assert captured["issued_key_value"] == "hysteria2://demo-key"
    assert captured["xui_client_uuid"].startswith("bk_")


async def _async_value(value):
    return value


def test_issue_by_id_rejects_disabled_blitz_before_provider_mutation(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)
    monkeypatch.setattr(app_module, "get_user", lambda user_id: {"telegram_id": user_id})
    monkeypatch.setattr(app_module.blitz_api, "is_blitz_enabled", lambda: False)

    def _must_not_mutate(*args, **kwargs):
        pytest.fail("Disabled Blitz must not call any issuance provider")

    monkeypatch.setattr(app_module.blitz_api, "create_user", _must_not_mutate)
    monkeypatch.setattr(app_module.blitz_api, "get_connection_uri", _must_not_mutate)
    monkeypatch.setattr(app_module.xui_api, "create_or_update_key_on_host", _must_not_mutate)
    monkeypatch.setattr(app_module, "add_new_key", _must_not_mutate)

    response = client.post(
        "/api/issue-by-id/execute",
        json={"telegram_id": "42", "host_name": app_module.blitz_api.BLITZ_HOST_NAME,
              "duration_days": 30, "send_message": False, "bot_source": "main"},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Выдача через Blitz отключена или не настроена"
