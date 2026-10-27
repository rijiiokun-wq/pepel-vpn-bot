import threading
import time

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
        "standard_key_host_name": "SUPER@",
        "super_key_host_name": "SUPER@",
    }
    monkeypatch.setattr(app_module, "BOT_SOURCE", "main")
    monkeypatch.setattr(app_module, "get_setting", settings.get)
    monkeypatch.setattr(app_module, "get_all_settings", lambda: dict(settings))
    monkeypatch.setattr(app_module, "get_all_settings_cached", lambda: dict(settings))
    monkeypatch.setattr(
        app_module,
        "get_all_hosts",
        lambda: [
            {
                "host_name": "SUPER@",
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


def test_issue_by_id_context_returns_keys(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    monkeypatch.setattr(
        app_module,
        "get_user",
        lambda user_id: {"telegram_id": user_id, "username": "demo", "total_spent": 1200},
    )
    monkeypatch.setattr(
        app_module,
        "get_user_keys",
        lambda user_id: [
            {
                "key_id": 1,
                "host_name": "SUPER@",
                "key_email": "user1@vpn.bot",
                "xui_client_uuid": "uuid-1",
                "expiry_date": "2099-01-01T00:00:00",
                "is_archived": 0,
            },
            {
                "key_id": 2,
                "host_name": "SUPER@",
                "key_email": "user2@vpn.bot",
                "xui_client_uuid": "uuid-2",
                "expiry_date": "2024-01-01T00:00:00",
                "is_archived": 1,
            },
        ],
    )

    response = client.post("/api/issue-by-id/context", json={"telegram_id": "442482830"})
    data = response.get_json()

    assert response.status_code == 200
    assert data["telegram_id"] == 442482830
    assert data["keys_total"] == 2
    assert data["keys_active"] == 1
    assert data["keys"][0]["key_id"] == 1


def test_issue_by_id_execute_replace_uses_target_key(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)
    monkeypatch.setenv("MANAGED_SUBSCRIPTION_BASE_URL", "https://pepelbot.com/podpiska")

    monkeypatch.setattr(app_module.blitz_api, "is_blitz_enabled", lambda: False)
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name} if host_name == "SUPER@" else None,
    )
    monkeypatch.setattr(app_module, "get_user", lambda user_id: {"telegram_id": user_id})
    monkeypatch.setattr(app_module, "register_user_if_not_exists", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        app_module,
        "get_key_by_id",
        lambda key_id: {
            "key_id": key_id,
            "user_id": 442482830,
            "host_name": "SUPER@",
            "is_archived": 0,
        },
    )
    monkeypatch.setattr(app_module, "get_next_key_number", lambda user_id: 2)
    monkeypatch.setattr(app_module, "get_key_by_email", lambda key_email: None)

    captured = {"archive_key_id": None, "add_kwargs": None, "issue_kwargs": None}

    def _add_new_key(**kwargs):
        captured["add_kwargs"] = kwargs
        return 777

    def _archive_key(key_id, **kwargs):
        captured["archive_key_id"] = key_id
        return True

    monkeypatch.setattr(app_module, "add_new_key", _add_new_key)
    monkeypatch.setattr(app_module, "archive_key", _archive_key)

    def _create_or_update_key_on_host(*_args, **kwargs):
        captured["issue_kwargs"] = kwargs
        return _async_value(
            {
                "client_uuid": "uuid-new",
                "email": "user442482830-key2@vpn.bot",
                "expiry_timestamp_ms": 1893456000000,
                "connection_string": "vless://new",
                "subscription_link": "https://sub/new",
                "sub_id": "new-sub",
            }
        )

    monkeypatch.setattr(
        app_module.xui_api,
        "create_or_update_key_on_host",
        _create_or_update_key_on_host,
    )

    response = client.post(
        "/api/issue-by-id/execute",
        json={
            "telegram_id": "442482830",
            "host_name": "",
            "duration_days": 30,
            "duration_months": 0,
            "duration_years": 0,
            "unlimited": False,
            "send_message": False,
            "replace_existing": True,
            "target_key_id": 9001,
            "bot_source": "main",
        },
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["status"] == "ok"
    assert data["replace_existing"] is True
    assert data["subscription_tier"] == "super"
    assert data["replaced_key_id"] == 9001
    assert data["host_name"] == "SUPER@"
    assert captured["archive_key_id"] == 9001
    assert captured["add_kwargs"]["host_name"] == "SUPER@"
    assert captured["add_kwargs"]["subscription_tier"] == "super"
    assert captured["add_kwargs"]["issued_key_value"] == "https://pepelbot.com/podpiska/new-sub"
    assert data["issued_key_value"] == "https://pepelbot.com/podpiska/new-sub"
    assert captured["issue_kwargs"]["include_white_inbounds"] is True
    assert captured["issue_kwargs"]["require_white_inbound"] is True
    assert captured["issue_kwargs"]["panel_call_timeout_seconds"] == app_module.MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS
    assert captured["issue_kwargs"]["max_attempts"] == 1


def test_issue_by_id_execute_standard_includes_white_with_standard_quota(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    monkeypatch.setattr(app_module.blitz_api, "is_blitz_enabled", lambda: False)
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name} if host_name == "SUPER@" else None,
    )
    monkeypatch.setattr(app_module, "get_user", lambda user_id: {"telegram_id": user_id})
    monkeypatch.setattr(app_module, "get_next_key_number", lambda user_id: 1)
    monkeypatch.setattr(app_module, "get_key_by_email", lambda key_email: None)
    monkeypatch.setattr(app_module, "add_new_key", lambda **_kwargs: 778)

    captured = {"issue_kwargs": None}

    def _create_or_update_key_on_host(*_args, **kwargs):
        captured["issue_kwargs"] = kwargs
        return _async_value(
            {
                "client_uuid": "uuid-standard",
                "email": "user442482830-key1@vpn.bot",
                "expiry_timestamp_ms": 1893456000000,
                "connection_string": "vless://standard",
                "subscription_link": "https://sub/standard",
            }
        )

    monkeypatch.setattr(
        app_module.xui_api,
        "create_or_update_key_on_host",
        _create_or_update_key_on_host,
    )

    response = client.post(
        "/api/issue-by-id/execute",
        json={
            "telegram_id": "442482830",
            "host_name": "SUPER@",
            "subscription_tier": "standard",
            "duration_days": 30,
            "duration_months": 0,
            "duration_years": 0,
            "unlimited": False,
            "send_message": False,
            "bot_source": "main",
        },
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["status"] == "ok"
    assert data["subscription_tier"] == "standard"
    assert captured["issue_kwargs"]["include_white_inbounds"] is True
    assert captured["issue_kwargs"]["require_white_inbound"] is True
    assert captured["issue_kwargs"]["white_traffic_limit_bytes"] == 5 * 1024**3


def test_issue_by_id_execute_replays_same_request_without_second_key(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    monkeypatch.setattr(app_module.blitz_api, "is_blitz_enabled", lambda: False)
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name} if host_name == "SUPER@" else None,
    )
    monkeypatch.setattr(app_module, "get_user", lambda user_id: {"telegram_id": user_id})
    monkeypatch.setattr(app_module, "get_next_key_number", lambda user_id: 1)
    monkeypatch.setattr(app_module, "get_key_by_email", lambda key_email: None)

    calls = {"panel": 0, "database": 0}
    panel_started = threading.Event()
    release_panel = threading.Event()

    def _add_new_key(**_kwargs):
        calls["database"] += 1
        return 779

    def _create_or_update_key_on_host(*_args, **_kwargs):
        calls["panel"] += 1
        panel_started.set()
        assert release_panel.wait(timeout=2)
        return _async_value(
            {
                "client_uuid": "uuid-idempotent",
                "email": "user442482830-key1@vpn.bot",
                "expiry_timestamp_ms": 1893456000000,
                "connection_string": "vless://idempotent",
                "subscription_link": "https://sub/idempotent",
            }
        )

    monkeypatch.setattr(app_module, "add_new_key", _add_new_key)
    monkeypatch.setattr(
        app_module.xui_api,
        "create_or_update_key_on_host",
        _create_or_update_key_on_host,
    )

    payload = {
        "request_id": "00000000-0000-4000-8000-000000000002",
        "telegram_id": "442482830",
        "host_name": "SUPER@",
        "subscription_tier": "super",
        "duration_days": 30,
        "duration_months": 0,
        "duration_years": 0,
        "unlimited": False,
        "send_message": False,
        "replace_existing": False,
        "bot_source": "main",
    }

    responses = {}

    def _post(name):
        thread_client = app.test_client()
        _login(thread_client)
        responses[name] = thread_client.post("/api/issue-by-id/execute", json=payload)

    first_thread = threading.Thread(target=_post, args=("first",))
    second_thread = threading.Thread(target=_post, args=("second",))
    first_thread.start()
    assert panel_started.wait(timeout=2)
    second_thread.start()
    time.sleep(0.05)
    release_panel.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    first = responses["first"]
    second = responses["second"]
    conflicting = client.post(
        "/api/issue-by-id/execute",
        json={**payload, "duration_days": 31},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.get_json()["key_id"] == 779
    assert second.get_json() == first.get_json()
    assert second.headers["X-Issue-Idempotent-Replay"] == "true"
    assert conflicting.status_code == 409
    assert "другими параметрами" in conflicting.get_json()["error"]
    assert calls == {"panel": 1, "database": 1}


def test_issue_by_id_rejects_non_main_panel_host(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name} if host_name in {"SUPER@", "LEGACY@"} else None,
    )
    monkeypatch.setattr(app_module, "get_user", lambda user_id: {"telegram_id": user_id})

    response = client.post(
        "/api/issue-by-id/execute",
        json={
            "telegram_id": "442482830",
            "host_name": "LEGACY@",
            "subscription_tier": "standard",
            "duration_days": 30,
            "send_message": False,
        },
    )
    data = response.get_json()

    assert response.status_code == 400
    assert 'только через "SUPER@"' in data["error"]


def test_issue_by_id_rejects_mainpanel_when_standard_and_super_settings_diverge(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    settings = {
        "standard_key_host_name": "STANDARD@",
        "super_key_host_name": "SUPER@",
    }
    monkeypatch.setattr(app_module, "get_setting", settings.get)
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name} if host_name == "MainPanel" else None,
    )

    response = client.post(
        "/api/issue-by-id/preview",
        json={
            "telegram_id": "442482830",
            "host_name": "MainPanel",
            "subscription_tier": "standard",
            "duration_days": 30,
        },
    )
    data = response.get_json()

    assert response.status_code == 400
    assert "standard_key_host_name и super_key_host_name" in data["error"]


def test_issue_by_id_rejects_invalid_subscription_tier(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/api/issue-by-id/preview",
        json={
            "telegram_id": "442482830",
            "host_name": "SUPER@",
            "subscription_tier": "premium",
            "duration_days": 30,
        },
    )
    data = response.get_json()

    assert response.status_code == 400
    assert "Некорректный тариф" in data["error"]


def test_issue_by_id_execute_returns_panel_failure_reason(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    monkeypatch.setattr(app_module.blitz_api, "is_blitz_enabled", lambda: False)
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name} if host_name == "SUPER@" else None,
    )
    monkeypatch.setattr(app_module, "get_user", lambda user_id: {"telegram_id": user_id})
    monkeypatch.setattr(app_module, "get_next_key_number", lambda user_id: 1)
    monkeypatch.setattr(app_module, "get_key_by_email", lambda key_email: None)

    def _create_or_update_key_on_host(*_args, **kwargs):
        kwargs["failure_context"]["reason"] = "sub_id_missing"
        return _async_value(None)

    monkeypatch.setattr(
        app_module.xui_api,
        "create_or_update_key_on_host",
        _create_or_update_key_on_host,
    )

    response = client.post(
        "/api/issue-by-id/execute",
        json={
            "telegram_id": "442482830",
            "host_name": "SUPER@",
            "duration_days": 30,
            "send_message": False,
        },
    )
    data = response.get_json()

    assert response.status_code == 502
    assert data["reason"] == "sub_id_missing"
    assert "sub_id_missing" in data["error"]


def test_issue_by_id_extend_updates_selected_key(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)
    monkeypatch.setenv("MANAGED_SUBSCRIPTION_BASE_URL", "https://pepelbot.com/podpiska")

    monkeypatch.setattr(
        app_module,
        "get_key_by_id",
        lambda key_id: {
            "key_id": key_id,
            "user_id": 442482830,
            "host_name": "SUPER@",
            "key_email": "user442482830-key1@vpn.bot",
            "xui_client_uuid": "uuid-old",
            "issued_key_value": "vless://old",
            "expiry_date": "2026-04-01T10:00:00",
            "is_archived": 0,
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name} if host_name == "SUPER@" else None,
    )
    captured = {"updated": None, "issue_kwargs": None}

    def _create_or_update_key_on_host(*_args, **kwargs):
        captured["issue_kwargs"] = kwargs
        return _async_value(
            {
                "client_uuid": "uuid-updated",
                "email": "user442482830-key1@vpn.bot",
                "expiry_timestamp_ms": 1893456000000,
                "connection_string": "vless://updated",
                "subscription_link": "https://sub/updated",
                "sub_id": "updated-sub",
            }
        )

    monkeypatch.setattr(
        app_module.xui_api,
        "create_or_update_key_on_host",
        _create_or_update_key_on_host,
    )

    def _update_key_info(**kwargs):
        captured["updated"] = kwargs
        return True

    monkeypatch.setattr(app_module, "update_key_info", _update_key_info)

    response = client.post(
        "/api/issue-by-id/extend",
        json={
            "telegram_id": "442482830",
            "target_key_id": 5001,
            "duration_days": 7,
            "duration_months": 0,
            "duration_years": 0,
            "unlimited": False,
            "send_message": False,
            "bot_source": "main",
            "subscription_tier": "standard",
        },
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["status"] == "ok"
    assert data["mode"] == "extend"
    assert data["subscription_tier"] == "standard"
    assert data["key_id"] == 5001
    assert data["host_name"] == "SUPER@"
    assert data["issued_key_value"] == "https://pepelbot.com/podpiska/updated-sub"
    assert captured["updated"]["key_id"] == 5001
    assert captured["updated"]["new_xui_uuid"] == "uuid-updated"
    assert captured["updated"]["subscription_tier"] == "standard"
    assert captured["updated"]["issued_key_value"] == "https://pepelbot.com/podpiska/updated-sub"
    assert captured["issue_kwargs"]["panel_call_timeout_seconds"] == app_module.MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS
    assert captured["issue_kwargs"]["panel_call_retries"] == 1
    assert captured["issue_kwargs"]["max_attempts"] == 1
    assert captured["issue_kwargs"]["include_white_inbounds"] is True
    assert captured["issue_kwargs"]["require_white_inbound"] is True
    assert captured["issue_kwargs"]["white_traffic_limit_bytes"] == 5 * 1024**3


async def _async_value(value):
    return value
