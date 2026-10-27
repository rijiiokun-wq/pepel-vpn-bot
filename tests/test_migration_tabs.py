import threading
from html import unescape

import pytest

pytest.importorskip("aiogram")

from shop_bot.config import get_subscription_ready_notification_template
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
    return app


def test_migration_page_contains_bulk_issue_and_marketing_tabs(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    response = client.get("/migration")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Общая" in body
    assert "По ID" in body
    assert "Маркетинг" in body
    assert "Inbound sync" in body
    assert "Сброс trial" in body
    assert "Параметры миграции" in body
    assert "Параметры выдачи" in body
    assert "Параметры выдачи кампании" in body
    assert "Параметры sync" in body
    assert "Параметры сброса" in body
    assert "mk-keep-old-keys" in body
    assert "Выдавать от бота" in body
    assert "Срок доступа" in body
    assert "Мес." in body
    assert "Годы" in body
    assert "Безлимит" in body
    assert "Основной" in body
    assert "Резервный" in body
    issue_bot_select = body[body.index('id="issue-bot-source"') : body.index('id="issue-bot-source"') + 220]
    assert '<option value="main">Основной</option>' in issue_bot_select
    assert '<option value="reserve" selected>Резервный</option>' in issue_bot_select
    assert "Уважаемый пользователь, ваша новая подписка готова" in body
    textarea_prefix = '<textarea id="issue-message-text" rows="12" class="migration-textarea">'
    textarea_start = body.index(textarea_prefix) + len(textarea_prefix)
    textarea_value = unescape(body[textarea_start : body.index("</textarea>", textarea_start)])
    assert textarea_value == get_subscription_ready_notification_template(dynamic_quota=True)
    assert "Хост: <b>{host}</b>" in textarea_value
    assert "<pre>{key}</pre>" in textarea_value


def test_migration_page_marks_only_direct_subscription_hosts(monkeypatch):
    app = _build_app(monkeypatch)
    monkeypatch.setattr(
        app_module,
        "get_all_hosts",
        lambda: [
            {
                "host_name": "MAINPANEL",
                "host_url": "https://main.example/panel",
                "host_subscription_base_url": "https://sub.example/base/",
                "status": "healthy",
                "is_maintenance": 0,
            },
            {
                "host_name": "PUBLIC-MANAGED",
                "host_url": "https://public.example/panel",
                "host_subscription_base_url": "https://pepelbot.com/podpiska",
                "status": "healthy",
                "is_maintenance": 0,
            },
        ],
    )
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    body = client.get("/migration?tab=issue").get_data(as_text=True)

    assert 'value="MAINPANEL"' in body
    assert 'value="PUBLIC-MANAGED"' in body
    assert body.count('data-has-subscription-url="true"') == 1
    assert body.count('data-has-subscription-url="false"') >= 1


def test_issue_by_id_redirects_to_issue_tab(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    response = client.get("/issue-by-id", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/migration?tab=issue")


def test_migration_reset_trials_api_smoke(monkeypatch):
    app = _build_app(monkeypatch)
    seen = {}
    monkeypatch.setattr(
        app_module.trial_reset,
        "reset_user_trials_by_username",
        lambda username: seen.setdefault(
            "result",
            {
                "telegram_id": 123,
                "username": username,
                "trial_used_before": 1,
                "trial_super_used_before": 1,
                "trial_used_after": 0,
                "trial_super_used_after": 0,
                "changed": True,
            },
        ),
    )
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    response = client.post(
        "/api/migration/reset-trials",
        json={"username": "noname_hui", "confirm": "reset-user-trials"},
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert data["telegram_id"] == 123
    assert data["trial_used_before"] == 1
    assert seen["result"]["username"] == "noname_hui"


def test_migration_reset_trials_api_reports_not_found(monkeypatch):
    app = _build_app(monkeypatch)

    def _raise_not_found(username):
        raise app_module.trial_reset.TrialResetError("user_not_found")

    monkeypatch.setattr(app_module.trial_reset, "reset_user_trials_by_username", _raise_not_found)
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    response = client.post(
        "/api/migration/reset-trials",
        json={"username": "missing_user", "confirm": "reset-user-trials"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "user_not_found"


def test_mainpanel_inbound_sync_preview_api_smoke(monkeypatch):
    app = _build_app(monkeypatch)
    monkeypatch.setattr(
        app_module, "get_host", lambda host_name: {"host_name": host_name} if host_name == "SUPER@" else None
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "sync_existing_mainpanel_clients_to_inbounds",
        lambda host_name, **kwargs: {
            "run_id": "run-1",
            "host_name": host_name,
            "status": "completed",
            "total": 1,
            "processed": 1,
            "updated": 1,
            "skipped": 0,
            "failed": 0,
            "needs_review": 0,
            "new_inbounds": 1,
            "details": [
                {
                    "user_id": 100,
                    "email": "user1-key1@vpn.bot",
                    "inbound_id": 4,
                    "inbound_label": "GERMANY",
                    "classification": "ordinary",
                    "tier": "standard",
                    "status": "would_update",
                    "action": "upsert_client",
                }
            ],
        },
    )
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    response = client.post(
        "/api/migration/mainpanel-inbounds/preview",
        json={"host_name": "SUPER@", "batch_limit": 25},
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["host_name"] == "SUPER@"
    assert data["updated"] == 1
    assert data["details"][0]["inbound_label"] == "GERMANY"


def test_migration_preview_api_smoke(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "SUPER": {"host_name": "SUPER"},
            "SUPER@": {"host_name": "SUPER@"},
        }.get(host_name),
    )
    monkeypatch.setattr(
        app_module,
        "get_all_keys_for_host_migration",
        lambda host_name, include_trial_super=False: [
            {
                "key_id": 101,
                "user_id": 442482830,
                "key_email": "user442482830-key1@vpn.bot",
                "expiry_date": "2099-04-01T12:00:00",
            }
        ],
    )

    response = client.post(
        "/api/migration/preview",
        json={
            "old_host": "SUPER",
            "new_host": "SUPER@",
            "gift_days": 7,
        },
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["total"] == 1
    assert data["skipped"] == 0
    assert data["details"][0]["user_id"] == 442482830


def test_marketing_preview_api_smoke(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    monkeypatch.setattr(
        app_module,
        "get_setting",
        lambda key: (
            "TARGET@"
            if key in {"standard_key_host_name", "super_key_host_name"}
            else {
                "flask_secret_key": "test-secret",
                "telegram_bot_token": "token",
                "telegram_bot_username": "bot",
                "admin_telegram_id": "1",
                "chatwoot_api_url": "",
            }.get(key)
        ),
    )
    monkeypatch.setattr(
        app_module,
        "get_all_hosts",
        lambda: [
            {
                "host_name": "SUPER@",
                "host_url": "https://super.example/panel",
                "status": "healthy",
                "is_maintenance": 0,
            },
            {
                "host_name": "STANDART@",
                "host_url": "https://std.example/panel",
                "status": "healthy",
                "is_maintenance": 0,
            },
            {
                "host_name": "TARGET@",
                "host_url": "https://target.example/panel",
                "status": "healthy",
                "is_maintenance": 0,
            },
        ],
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "SUPER@": {"host_name": "SUPER@", "host_url": "https://super.example/panel"},
            "STANDART@": {"host_name": "STANDART@", "host_url": "https://std.example/panel"},
            "TARGET@": {"host_name": "TARGET@", "host_url": "https://target.example/panel"},
        }.get(host_name),
    )
    monkeypatch.setattr(
        app_module,
        "get_all_keys_for_host_migration",
        lambda host_name, include_trial_super=False: (
            [
                {
                    "key_id": 201,
                    "user_id": 442482830,
                    "key_email": "user442482830-key1@vpn.bot",
                    "expiry_date": "2099-04-21T12:00:00",
                }
            ]
            if host_name == "STANDART@"
            else (
                [
                    {
                        "key_id": 202,
                        "user_id": 5172885994,
                        "key_email": "user5172885994-key1-super@vpn.bot",
                        "expiry_date": "2099-04-25T12:00:00",
                    }
                ]
                if host_name == "SUPER@"
                else []
            )
        ),
    )
    monkeypatch.setattr(app_module, "get_migration_for_key", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "get_replacement_key_for_incident", lambda old_key_id, incident_id: None)

    response = client.post(
        "/api/migration/marketing/preview",
        json={
            "segment": "standard",
            "target_host": "TARGET@",
            "gift_days": 5,
            "incident_id": "MARKETING-TEST",
        },
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["segment"] == "standard"
    assert data["target_host"] == "TARGET@"
    assert data["keep_old_keys"] is True
    assert data["total"] == 1
    assert data["details"][0]["user_id"] == 442482830
    assert data["details"][0]["old_key_action"] == "keep"


def test_marketing_preview_accepts_offset_standard_expiry(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    monkeypatch.setattr(
        app_module,
        "get_setting",
        lambda key: "MARKETING-TARGET" if key in {"standard_key_host_name", "super_key_host_name"} else "test-secret",
    )
    monkeypatch.setattr(
        app_module,
        "get_all_hosts",
        lambda: [
            {
                "host_name": "StandartVLESS",
                "host_url": "https://standard.example/panel",
                "status": "healthy",
                "is_maintenance": 0,
            },
            {
                "host_name": "MARKETING-TARGET",
                "host_url": "https://target.example/panel",
                "status": "healthy",
                "is_maintenance": 0,
            },
        ],
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "StandartVLESS": {"host_name": "StandartVLESS"},
            "MARKETING-TARGET": {"host_name": "MARKETING-TARGET"},
        }.get(host_name),
    )
    monkeypatch.setattr(
        app_module,
        "get_all_keys_for_host_migration",
        lambda host_name, include_trial_super=False: (
            [
                {
                    "key_id": 411,
                    "user_id": 442482830,
                    "key_email": "user442482830-key1@vpn.bot",
                    "expiry_date": "2099-04-21T12:00:00+00:00",
                }
            ]
            if host_name == "StandartVLESS"
            else []
        ),
    )
    monkeypatch.setattr(app_module, "get_migration_for_key", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "get_replacement_key_for_incident", lambda old_key_id, incident_id: None)

    response = client.post(
        "/api/migration/marketing/preview",
        json={
            "segment": "standard",
            "target_host": "MARKETING-TARGET",
            "gift_days": 14,
            "incident_id": "MARKETING-OFFSET-EXPIRY",
        },
    )
    data = response.get_json()

    assert response.status_code == 200
    assert data["total"] == 1
    assert data["details"][0]["user_id"] == 442482830
    assert data["details"][0]["new_expiry"]


def test_marketing_execute_deletes_old_key_when_requested(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    source_email = "user5172885994-key1-super@vpn.bot"
    db_deleted = set()
    remote_deleted = []

    monkeypatch.setattr(
        app_module,
        "get_setting",
        lambda key: "TARGET@" if key in {"standard_key_host_name", "super_key_host_name"} else "test-secret",
    )
    monkeypatch.setattr(
        app_module,
        "get_all_hosts",
        lambda: [
            {
                "host_name": "SUPER@",
                "host_url": "https://super.example/panel",
                "status": "healthy",
                "is_maintenance": 0,
            },
            {
                "host_name": "TARGET@",
                "host_url": "https://target.example/panel",
                "status": "healthy",
                "is_maintenance": 0,
            },
        ],
    )
    hosts_by_name = {
        "SUPER@": {"host_name": "SUPER@", "host_url": "https://super.example/panel"},
        "TARGET@": {"host_name": "TARGET@", "host_url": "https://target.example/panel"},
    }
    monkeypatch.setattr(app_module, "get_host", hosts_by_name.get)
    monkeypatch.setattr(
        app_module,
        "get_all_keys_for_host_migration",
        lambda host_name, include_trial_super=False: (
            [
                {
                    "key_id": 202,
                    "user_id": 5172885994,
                    "key_email": source_email,
                    "expiry_date": "2099-04-25T12:00:00",
                }
            ]
            if host_name == "SUPER@"
            else []
        ),
    )
    monkeypatch.setattr(app_module, "get_migration_for_key", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "get_replacement_key_for_incident", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "create_incident", lambda incident_id, description: None)
    monkeypatch.setattr(app_module, "get_next_key_number", lambda user_id: 1)
    monkeypatch.setattr(app_module, "add_migrated_key", lambda **kwargs: 901)
    monkeypatch.setattr(app_module, "log_migration", lambda **kwargs: None)

    def _fake_get_key_by_email(email):
        if email == source_email and email not in db_deleted:
            return {"key_email": source_email, "xui_client_uuid": "old-uuid"}
        return None

    monkeypatch.setattr(app_module, "get_key_by_email", _fake_get_key_by_email)

    def _fake_delete_key_by_email(email):
        db_deleted.add(email)

    monkeypatch.setattr(app_module, "delete_key_by_email", _fake_delete_key_by_email)

    issued_kwargs = []
    wait_for_timeouts = []

    async def _fake_wait_for(awaitable, **kwargs):
        wait_for_timeouts.append(kwargs.get("timeout"))
        return await awaitable

    monkeypatch.setattr(app_module.asyncio, "wait_for", _fake_wait_for)

    async def _fake_create_or_update_key_on_host(host_name, email, days_to_add, *, target_expiry_ms=None, **kwargs):
        issued_kwargs.append(kwargs)
        return {
            "client_uuid": "new-uuid",
            "email": email,
            "expiry_timestamp_ms": target_expiry_ms,
            "subscription_link": "vless://new-key",
        }

    async def _fake_delete_client_on_host(host_name, key_email):
        remote_deleted.append((host_name, key_email))
        return True

    monkeypatch.setattr(app_module.xui_api, "create_or_update_key_on_host", _fake_create_or_update_key_on_host)
    monkeypatch.setattr(app_module.xui_api, "delete_client_on_host", _fake_delete_client_on_host)

    class _ImmediateThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading, "Thread", _ImmediateThread)

    response = client.post(
        "/api/migration/marketing/execute",
        json={
            "segment": "super",
            "target_host": "TARGET@",
            "gift_days": 5,
            "incident_id": "MARKETING-DELETE-TEST",
            "send_notify": False,
            "keep_old_keys": False,
        },
    )
    data = response.get_json()

    assert response.status_code == 200
    status = client.get(f"/api/migration/marketing/status/{data['job_id']}").get_json()
    assert status["status"] == "completed"
    assert status["issued"] == 1
    assert status["deleted"] == 1
    assert status["delete_errors"] == 0
    assert status["details"][0]["status"] == "issued_deleted"
    assert remote_deleted == [("SUPER@", source_email)]
    assert source_email in db_deleted
    assert issued_kwargs == [
        {
            "white_traffic_limit_bytes": 40 * 1024**3,
            "include_white_inbounds": True,
            "require_white_inbound": True,
            "panel_call_timeout_seconds": app_module.MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
            "max_attempts": 2,
        }
    ]
    assert wait_for_timeouts == [
        app_module.MANUAL_ISSUE_PANEL_JOB_TIMEOUT_SECONDS,
        app_module.MANUAL_ISSUE_PANEL_JOB_TIMEOUT_SECONDS,
    ]


def test_marketing_execute_does_not_repeat_source_delete_after_failure(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    delete_calls = []

    monkeypatch.setattr(
        app_module,
        "get_setting",
        lambda key: "TARGET@" if key in {"standard_key_host_name", "super_key_host_name"} else "test-secret",
    )
    monkeypatch.setattr(
        app_module,
        "get_all_hosts",
        lambda: [
            {
                "host_name": "SUPER@",
                "host_url": "https://super.example/panel",
                "status": "healthy",
                "is_maintenance": 0,
            },
            {
                "host_name": "TARGET@",
                "host_url": "https://target.example/panel",
                "status": "healthy",
                "is_maintenance": 0,
            },
        ],
    )
    hosts_by_name = {
        "SUPER@": {"host_name": "SUPER@", "host_url": "https://super.example/panel"},
        "TARGET@": {"host_name": "TARGET@", "host_url": "https://target.example/panel"},
    }
    monkeypatch.setattr(app_module, "get_host", hosts_by_name.get)
    monkeypatch.setattr(
        app_module,
        "get_all_keys_for_host_migration",
        lambda host_name, include_trial_super=False: (
            [
                {
                    "key_id": 301,
                    "user_id": 1001,
                    "key_email": "user1001-key1-super@vpn.bot",
                    "expiry_date": "2099-04-25T12:00:00",
                },
                {
                    "key_id": 302,
                    "user_id": 1002,
                    "key_email": "user1002-key1-super@vpn.bot",
                    "expiry_date": "2099-04-26T12:00:00",
                },
            ]
            if host_name == "SUPER@"
            else []
        ),
    )
    monkeypatch.setattr(app_module, "get_migration_for_key", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "get_replacement_key_for_incident", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "create_incident", lambda incident_id, description: None)
    monkeypatch.setattr(app_module, "get_next_key_number", lambda user_id: 1)
    monkeypatch.setattr(app_module, "add_migrated_key", lambda **kwargs: int(kwargs["user_id"]) + 9000)
    monkeypatch.setattr(app_module, "log_migration", lambda **kwargs: None)
    monkeypatch.setattr(app_module, "get_key_by_email", lambda email: None)
    monkeypatch.setattr(app_module, "delete_key_by_email", lambda email: None)

    issued_kwargs = []
    wait_for_timeouts = []

    async def _fake_wait_for(awaitable, **kwargs):
        wait_for_timeouts.append(kwargs.get("timeout"))
        return await awaitable

    monkeypatch.setattr(app_module.asyncio, "wait_for", _fake_wait_for)

    async def _fake_create_or_update_key_on_host(host_name, email, days_to_add, *, target_expiry_ms=None, **kwargs):
        issued_kwargs.append(kwargs)
        return {
            "client_uuid": f"new-{email}",
            "email": email,
            "expiry_timestamp_ms": target_expiry_ms,
            "subscription_link": "vless://new-key",
        }

    async def _fake_delete_client_on_host(host_name, key_email):
        delete_calls.append((host_name, key_email))
        return False

    monkeypatch.setattr(app_module.xui_api, "create_or_update_key_on_host", _fake_create_or_update_key_on_host)
    monkeypatch.setattr(app_module.xui_api, "delete_client_on_host", _fake_delete_client_on_host)

    class _ImmediateThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading, "Thread", _ImmediateThread)

    response = client.post(
        "/api/migration/marketing/execute",
        json={
            "segment": "super",
            "target_host": "TARGET@",
            "gift_days": 5,
            "incident_id": "MARKETING-DELETE-FAIL-TEST",
            "send_notify": False,
            "keep_old_keys": False,
        },
    )
    data = response.get_json()

    assert response.status_code == 200
    status = client.get(f"/api/migration/marketing/status/{data['job_id']}").get_json()
    assert status["status"] == "completed"
    assert status["issued"] == 2
    assert status["deleted"] == 0
    assert status["delete_errors"] == 2
    assert len(delete_calls) == 1
    assert [detail["status"] for detail in status["details"]] == [
        "issued_delete_failed",
        "issued_delete_failed",
    ]
    assert issued_kwargs == [
        {
            "white_traffic_limit_bytes": 40 * 1024**3,
            "include_white_inbounds": True,
            "require_white_inbound": True,
            "panel_call_timeout_seconds": app_module.MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
            "max_attempts": 2,
        },
        {
            "white_traffic_limit_bytes": 40 * 1024**3,
            "include_white_inbounds": True,
            "require_white_inbound": True,
            "panel_call_timeout_seconds": app_module.MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
            "max_attempts": 2,
        },
    ]
    assert wait_for_timeouts == [
        app_module.MANUAL_ISSUE_PANEL_JOB_TIMEOUT_SECONDS,
        app_module.MANUAL_ISSUE_PANEL_JOB_TIMEOUT_SECONDS,
        app_module.MANUAL_ISSUE_PANEL_JOB_TIMEOUT_SECONDS,
    ]


def test_migration_execute_uses_manual_issue_panel_timeout(monkeypatch):
    app = _build_app(monkeypatch)
    client = app.test_client()
    with client.session_transaction() as session:
        session["logged_in"] = True

    monkeypatch.setattr(
        app_module,
        "get_setting",
        lambda key: "TARGET@" if key in {"standard_key_host_name", "super_key_host_name"} else "test-secret",
    )
    hosts_by_name = {
        "OLD@": {"host_name": "OLD@", "host_url": "https://old.example/panel"},
        "TARGET@": {"host_name": "TARGET@", "host_url": "https://target.example/panel"},
    }
    monkeypatch.setattr(app_module, "get_host", hosts_by_name.get)
    monkeypatch.setattr(
        app_module,
        "get_all_keys_for_host_migration",
        lambda host_name, include_trial_super=False: (
            [
                {
                    "key_id": 501,
                    "user_id": 1001,
                    "key_email": "user1001-key1@vpn.bot",
                    "expiry_date": "2099-04-25T12:00:00",
                },
            ]
            if host_name == "OLD@"
            else []
        ),
    )
    monkeypatch.setattr(app_module, "get_migration_for_key", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "get_replacement_key_for_incident", lambda old_key_id, incident_id: None)
    monkeypatch.setattr(app_module, "create_incident", lambda incident_id, description: None)
    monkeypatch.setattr(app_module, "get_next_key_number", lambda user_id: 1)
    monkeypatch.setattr(app_module, "get_key_by_email", lambda email: None)
    monkeypatch.setattr(app_module, "archive_key", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "log_migration", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "add_migrated_key", lambda **kwargs: 9901)

    issued_kwargs = []

    async def _fake_create_or_update_key_on_host(host_name, email, days_to_add, **kwargs):
        issued_kwargs.append(kwargs)
        return {
            "client_uuid": "new-uuid",
            "email": email,
            "expiry_timestamp_ms": 4080718800000,
            "subscription_link": "vless://new-key",
        }

    monkeypatch.setattr(app_module.xui_api, "create_or_update_key_on_host", _fake_create_or_update_key_on_host)

    class _ImmediateThread:
        def __init__(self, target, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(threading, "Thread", _ImmediateThread)

    response = client.post(
        "/api/migration/execute",
        json={
            "old_host": "OLD@",
            "new_host": "TARGET@",
            "gift_days": 5,
            "incident_id": "MIGRATION-TIMEOUT-TEST",
            "send_notify": False,
            "subscription_tier": "standard",
        },
    )
    data = response.get_json()

    assert response.status_code == 200
    status = client.get(f"/api/migration/status/{data['job_id']}").get_json()
    assert status["status"] == "completed"
    assert status["migrated"] == 1
    assert status["subscription_tier"] == "standard"
    assert issued_kwargs == [
        {
            "white_traffic_limit_bytes": 5 * 1024**3,
            "include_white_inbounds": True,
            "require_white_inbound": True,
            "panel_call_timeout_seconds": app_module.MANUAL_ISSUE_PANEL_CALL_TIMEOUT_SECONDS,
            "max_attempts": 2,
        }
    ]
