from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("aiogram")

from shop_bot.domain import mainpanel_migration
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


def _future(days: int = 30) -> str:
    return (datetime.now(UTC).replace(tzinfo=None) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _past(days: int = 2) -> str:
    return (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)).replace(microsecond=0).isoformat()


def _build_app(monkeypatch):
    settings = {
        "flask_secret_key": "test-secret",
        "telegram_bot_token": "token",
        "telegram_bot_username": "bot",
        "admin_telegram_id": "1",
        "chatwoot_api_url": "",
        "standard_key_host_name": "MainPanel",
        "super_key_host_name": "MainPanel",
    }

    def _get_setting(key):
        return settings.get(key)

    hosts_by_name = {
        "MainPanel": {"host_name": "MainPanel", "host_url": "https://main.example/panel"},
        "StandartVLESS": {"host_name": "StandartVLESS", "host_url": "https://std.example/panel"},
        "SUPER2": {"host_name": "SUPER2", "host_url": "https://super.example/panel"},
    }

    monkeypatch.setattr(app_module, "BOT_SOURCE", "main")
    monkeypatch.setattr(app_module, "get_setting", _get_setting)
    monkeypatch.setattr(app_module, "get_all_settings", lambda: dict(settings))
    monkeypatch.setattr(app_module, "get_all_settings_cached", lambda: dict(settings))
    monkeypatch.setattr(
        app_module,
        "get_all_hosts",
        lambda: [
            {"host_name": "MainPanel", "host_url": "https://main.example/panel", "status": "healthy"},
            {"host_name": "StandartVLESS", "host_url": "https://std.example/panel", "status": "healthy"},
            {"host_name": "SUPER2", "host_url": "https://super.example/panel", "status": "healthy"},
        ],
    )
    monkeypatch.setattr(app_module, "get_host", hosts_by_name.get)
    app = app_module.create_webhook_app(_DummyBotController())
    app.config["TESTING"] = True
    return app


def _login(client):
    with client.session_transaction() as session:
        session["logged_in"] = True


def test_mainpanel_tier_resolver_prefers_explicit_key_tier():
    tier, source, reason = mainpanel_migration.resolve_key_tier(
        {"key_id": 10, "host_name": "SUPER2", "subscription_tier": "standard"},
        [
            {
                "metadata": {
                    "key_id": 10,
                    "subscription_tier": "super",
                    "is_super_key_flow": True,
                }
            }
        ],
        main_panel_host_name="MainPanel",
    )

    assert tier == "standard"
    assert source == "vpn_keys.subscription_tier"
    assert reason is None


def test_mainpanel_tier_resolver_uses_transaction_metadata_before_legacy_host():
    tier, source, reason = mainpanel_migration.resolve_key_tier(
        {"key_id": 11, "host_name": "StandartVLESS", "subscription_tier": None},
        [{"metadata": {"key_id": 11, "is_super_key_flow": True}}],
        main_panel_host_name="MainPanel",
    )

    assert tier == "super"
    assert source == "transactions.metadata.is_super_key_flow"
    assert reason is None


def test_mainpanel_host_without_explicit_tier_needs_review():
    item = mainpanel_migration.build_migration_item(
        {
            "key_id": 12,
            "user_id": 100,
            "host_name": "MainPanel",
            "key_email": "user100-key1@vpn.bot",
            "expiry_date": _future(),
        },
        transaction_rows=[],
        target_tier="standard",
        main_panel_host_name="MainPanel",
        bonus_days=0,
        incident_id="TEST",
    )

    assert item["status"] == mainpanel_migration.STATUS_NEEDS_REVIEW
    assert item["reason"] == "unknown_tier"


def test_mainpanel_expired_client_is_skipped():
    item = mainpanel_migration.build_migration_item(
        {
            "key_id": 13,
            "user_id": 101,
            "host_name": "StandartVLESS",
            "key_email": "user101-key1@vpn.bot",
            "subscription_tier": "standard",
            "expiry_date": _past(),
        },
        transaction_rows=[],
        target_tier="standard",
        main_panel_host_name="MainPanel",
        bonus_days=7,
        incident_id="TEST",
    )

    assert item["status"] == mainpanel_migration.STATUS_SKIPPED_EXPIRED
    assert item["reason"] == "expired"


def test_mainpanel_preview_reports_candidate_skip_and_needs_review(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    source_keys = [
        {
            "key_id": 101,
            "user_id": 1001,
            "host_name": "StandartVLESS",
            "key_email": "user1001-key1@vpn.bot",
            "subscription_tier": "standard",
            "expiry_date": "2099-01-01T12:00:00",
        },
        {
            "key_id": 102,
            "user_id": 1002,
            "host_name": "SUPER2",
            "key_email": "user1002-key1@vpn.bot",
            "subscription_tier": "super",
            "expiry_date": "2099-01-02T12:00:00",
        },
        {
            "key_id": 103,
            "user_id": 1003,
            "host_name": "StandartVLESS",
            "key_email": "user1003-key1@vpn.bot",
            "subscription_tier": "standard",
            "expiry_date": _past(),
        },
        {
            "key_id": 104,
            "user_id": 1004,
            "host_name": "legacy",
            "key_email": "user1004-key1@vpn.bot",
            "subscription_tier": None,
            "expiry_date": "2099-01-04T12:00:00",
        },
    ]
    monkeypatch.setattr(app_module, "get_mainpanel_migration_source_keys", lambda *_args, **_kwargs: source_keys)
    monkeypatch.setattr(app_module, "get_successful_transactions_for_migration_keys", lambda keys: {})
    monkeypatch.setattr(app_module, "get_migration_for_key", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "get_replacement_key_for_incident", lambda old_key_id, incident_id: None)

    response = client.post(
        "/api/migration/mainpanel/preview",
        json={"subscription_tier": "standard", "bonus_days": 2, "incident_id": "MAINPANEL-TEST"},
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["found"] == 4
    assert data["will_migrate"] == 1
    assert data["skipped_wrong_tier"] == 1
    assert data["skipped_expired"] == 1
    assert data["needs_review"] == 1
    assert data["details"][0]["new_expiry"] == "2099-01-03T12:00:00"
    assert "target_expiry_ms" not in data["details"][0]


def test_mainpanel_execute_standard_uses_standard_quota_and_preserves_sub_id(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)
    monkeypatch.setenv("MANAGED_SUBSCRIPTION_BASE_URL", "https://pepelbot.com/podpiska")

    source_keys = [
        {
            "key_id": 201,
            "user_id": 2001,
            "host_name": "StandartVLESS",
            "key_email": "user2001-key1@vpn.bot",
            "subscription_tier": "standard",
            "expiry_date": "2099-02-01T12:00:00",
            "panel_sub_id": "old-sub",
            "bot_source": "main",
        }
    ]
    monkeypatch.setattr(app_module, "get_mainpanel_migration_source_keys", lambda *_args, **_kwargs: source_keys)
    monkeypatch.setattr(app_module, "get_successful_transactions_for_migration_keys", lambda keys: {})
    monkeypatch.setattr(app_module, "get_migration_for_key", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "get_replacement_key_for_incident", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "create_incident", lambda incident_id, description: None)
    monkeypatch.setattr(app_module, "get_next_key_number", lambda user_id: 2)
    monkeypatch.setattr(app_module, "get_key_by_email", lambda email: None)

    claims = []
    finishes = []
    archives = []
    add_calls = []
    issue_calls = []

    monkeypatch.setattr(
        app_module,
        "claim_mainpanel_migration_item",
        lambda **kwargs: claims.append(kwargs) or True,
    )
    monkeypatch.setattr(
        app_module,
        "finish_mainpanel_migration_item",
        lambda **kwargs: finishes.append(kwargs) or True,
    )
    monkeypatch.setattr(app_module, "archive_key", lambda *args, **kwargs: archives.append((args, kwargs)))
    monkeypatch.setattr(app_module, "add_migrated_key", lambda **kwargs: add_calls.append(kwargs) or 901)

    async def _fake_issue(host_name, email, days_to_add, **kwargs):
        issue_calls.append({"host_name": host_name, "email": email, "days_to_add": days_to_add, **kwargs})
        return {
            "client_uuid": "new-uuid",
            "email": email,
            "expiry_timestamp_ms": kwargs["target_expiry_ms"],
            "subscription_link": "vless://new-link",
            "sub_id": kwargs["target_sub_id"],
        }

    monkeypatch.setattr(app_module.xui_api, "create_or_update_key_on_host", _fake_issue)

    class _ImmediateThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading, "Thread", _ImmediateThread)

    response = client.post(
        "/api/migration/mainpanel/execute",
        json={"subscription_tier": "standard", "bonus_days": 0, "incident_id": "MAINPANEL-STANDARD"},
    )
    data = response.get_json()

    assert response.status_code == 200
    status = client.get(f"/api/migration/mainpanel/status/{data['job_id']}").get_json()
    assert status["status"] == "completed"
    assert status["migrated"] == 1
    assert status["errors"] == 0
    assert claims[0]["old_key_id"] == 201
    assert issue_calls[0]["host_name"] == "MainPanel"
    assert issue_calls[0]["include_white_inbounds"] is True
    assert issue_calls[0]["require_white_inbound"] is True
    assert issue_calls[0]["white_traffic_limit_bytes"] == 5 * 1024**3
    assert issue_calls[0]["target_sub_id"] == "old-sub"
    assert add_calls[0]["subscription_tier"] == "standard"
    assert add_calls[0]["panel_sub_id"] == "old-sub"
    assert add_calls[0]["issued_key_value"] == "https://pepelbot.com/podpiska/old-sub"
    assert finishes[-1]["status"] == "migrated"
    assert archives[0][0][0] == 201


def test_mainpanel_execute_super_requires_whitelist(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    source_keys = [
        {
            "key_id": 301,
            "user_id": 3001,
            "host_name": "SUPER2",
            "key_email": "user3001-key1@vpn.bot",
            "subscription_tier": "super",
            "expiry_date": "2099-03-01T12:00:00",
        }
    ]
    monkeypatch.setattr(app_module, "get_mainpanel_migration_source_keys", lambda *_args, **_kwargs: source_keys)
    monkeypatch.setattr(app_module, "get_successful_transactions_for_migration_keys", lambda keys: {})
    monkeypatch.setattr(app_module, "get_migration_for_key", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "get_replacement_key_for_incident", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "create_incident", lambda incident_id, description: None)
    monkeypatch.setattr(app_module, "get_next_key_number", lambda user_id: 1)
    monkeypatch.setattr(app_module, "get_key_by_email", lambda email: None)
    monkeypatch.setattr(app_module, "claim_mainpanel_migration_item", lambda **kwargs: True)
    monkeypatch.setattr(app_module, "finish_mainpanel_migration_item", lambda **kwargs: True)
    monkeypatch.setattr(app_module, "archive_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "add_migrated_key", lambda **kwargs: 902)
    issue_calls = []

    async def _fake_issue(host_name, email, days_to_add, **kwargs):
        issue_calls.append(kwargs)
        return {
            "client_uuid": "new-uuid",
            "email": email,
            "expiry_timestamp_ms": kwargs["target_expiry_ms"],
            "subscription_link": "vless://new-link",
            "sub_id": "new-sub",
        }

    monkeypatch.setattr(app_module.xui_api, "create_or_update_key_on_host", _fake_issue)

    class _ImmediateThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading, "Thread", _ImmediateThread)

    response = client.post(
        "/api/migration/mainpanel/execute",
        json={"subscription_tier": "super", "bonus_days": 0, "incident_id": "MAINPANEL-SUPER"},
    )
    data = response.get_json()

    assert response.status_code == 200
    status = client.get(f"/api/migration/mainpanel/status/{data['job_id']}").get_json()
    assert status["status"] == "completed"
    assert issue_calls[0]["include_white_inbounds"] is True
    assert issue_calls[0]["require_white_inbound"] is True


def test_mainpanel_execute_skips_when_claim_already_exists(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    source_keys = [
        {
            "key_id": 401,
            "user_id": 4001,
            "host_name": "StandartVLESS",
            "key_email": "user4001-key1@vpn.bot",
            "subscription_tier": "standard",
            "expiry_date": "2099-04-01T12:00:00",
        }
    ]
    monkeypatch.setattr(app_module, "get_mainpanel_migration_source_keys", lambda *_args, **_kwargs: source_keys)
    monkeypatch.setattr(app_module, "get_successful_transactions_for_migration_keys", lambda keys: {})
    monkeypatch.setattr(app_module, "get_migration_for_key", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "get_replacement_key_for_incident", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "create_incident", lambda incident_id, description: None)
    monkeypatch.setattr(app_module, "claim_mainpanel_migration_item", lambda **kwargs: False)

    async def _unexpected_issue(*_args, **_kwargs):
        raise AssertionError("panel issuance must not run for an already claimed migration")

    monkeypatch.setattr(app_module.xui_api, "create_or_update_key_on_host", _unexpected_issue)

    class _ImmediateThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading, "Thread", _ImmediateThread)

    response = client.post(
        "/api/migration/mainpanel/execute",
        json={"subscription_tier": "standard", "bonus_days": 0, "incident_id": "MAINPANEL-CLAIMED"},
    )
    data = response.get_json()

    assert response.status_code == 200
    status = client.get(f"/api/migration/mainpanel/status/{data['job_id']}").get_json()
    assert status["status"] == "completed"
    assert status["migrated"] == 0
    assert status["skipped"] == 1
    assert status["details"][0]["status"] == "skipped_claimed"
