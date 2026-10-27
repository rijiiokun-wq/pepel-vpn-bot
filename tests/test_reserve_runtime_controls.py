import json
import logging
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

import pytest

pytest.importorskip("aiogram")

from shop_bot.data_manager import database
from shop_bot.webhook_server import app as app_module


class _DummyBotController:
    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0

    def get_status(self):
        return {
            "shop_bot_running": False,
            "shop_bot_desired_running": True,
            "shop_autorestart_paused": False,
            "shop_last_failure": None,
            "shop_last_failure_at": None,
            "support_bot_running": False,
            "chatwoot_enabled": False,
        }

    def start_shop_bot(self, initiated_by: str = "manual"):
        self.start_calls += 1
        return {"status": "success", "message": f"start:{initiated_by}"}

    def stop_shop_bot(self):
        self.stop_calls += 1
        return {"status": "success", "message": "stop:ok"}


def _build_app(monkeypatch, *, bot_source: str = "main"):
    settings = {
        "flask_secret_key": "test-secret",
        "telegram_bot_token": "main-token",
        "telegram_bot_username": "main-bot",
        "reserve_bot_token": "reserve-token",
        "reserve_bot_username": "reserve-bot",
        "admin_telegram_id": "1",
    }
    monkeypatch.setattr(app_module, "BOT_SOURCE", bot_source)
    monkeypatch.setattr(app_module, "get_setting", lambda key: settings.get(key))
    monkeypatch.setattr(app_module, "get_all_settings", lambda: dict(settings))
    monkeypatch.setattr(app_module, "get_all_settings_cached", lambda: dict(settings))
    monkeypatch.setattr(app_module, "get_hosts_with_plans", lambda: [])
    monkeypatch.setattr(app_module, "send_critical_alert", lambda *args, **kwargs: None)

    controller = _DummyBotController()
    app = app_module.create_webhook_app(controller)
    app.config["TESTING"] = True
    return app, controller


def _login(client):
    with client.session_transaction() as session:
        session["logged_in"] = True


class _ImmediateThread:
    def __init__(self, *, target, name=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


def test_settings_page_shows_reserve_start_button_from_main_runtime(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "_load_runtime_status",
        lambda *_args, **_kwargs: {
            "available": True,
            "shop_bot_running": False,
            "all_settings_ok": True,
            "message": "OK",
        },
    )
    app, _controller = _build_app(monkeypatch, bot_source="main")
    client = app.test_client()
    _login(client)

    response = client.get("/settings")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'action="/start-reserve-bot"' in body
    assert "Управляется из reserve-контейнера" not in body


def test_update_host_api_token_route_persists_env_ref(monkeypatch):
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        app_module,
        "update_host_api_token_ref",
        lambda host_name, token_ref: calls.append((host_name, token_ref)) or True,
    )

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/update-host-api-token/SUPER2",
        data={"host_api_token_env": " env:XUI_API_TOKEN_SUPER2 "},
    )

    assert response.status_code == 302
    assert calls == [("SUPER2", "env:XUI_API_TOKEN_SUPER2")]


def test_update_host_api_token_route_builds_explicit_env_ref(monkeypatch):
    calls: list[tuple[str, str | None]] = []

    def _persist_token_ref(host_name: str, token_ref: str | None) -> bool:
        calls.append((host_name, token_ref))
        return True

    monkeypatch.setattr(
        app_module,
        "update_host_api_token_ref",
        _persist_token_ref,
    )

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/update-host-api-token/SUPER2",
        data={
            "host_api_token_mode": "env",
            "host_api_token_env_name": "XUI_API_TOKEN_SUPER2",
        },
    )

    assert response.status_code == 302
    assert calls == [("SUPER2", "env:XUI_API_TOKEN_SUPER2")]


def test_update_host_api_token_route_keeps_direct_token_mode(monkeypatch):
    calls: list[tuple[str, str | None]] = []

    def _persist_token_ref(host_name: str, token_ref: str | None) -> bool:
        calls.append((host_name, token_ref))
        return True

    monkeypatch.setattr(
        app_module,
        "update_host_api_token_ref",
        _persist_token_ref,
    )

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/update-host-api-token/SUPER2",
        data={
            "host_api_token_mode": "direct",
            "host_api_token_direct": " direct-test-token ",
        },
    )

    assert response.status_code == 302
    assert calls == [("SUPER2", "direct-test-token")]


def test_update_host_urls_route_persists_trimmed_urls(monkeypatch):
    calls: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        app_module,
        "update_host_urls",
        lambda host_name, host_url, subscription_url: calls.append((host_name, host_url, subscription_url)) or True,
    )

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/update-host-urls/SUPER2",
        data={
            "host_url": " https://panel.example/path/ ",
            "host_subscription_base_url": " https://sub.example/base/ ",
        },
    )

    assert response.status_code == 302
    assert calls == [("SUPER2", "https://panel.example/path/", "https://sub.example/base/")]


def test_update_host_urls_route_requires_panel_url(monkeypatch):
    calls: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        app_module,
        "update_host_urls",
        lambda host_name, host_url, subscription_url: calls.append((host_name, host_url, subscription_url)) or True,
    )

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/update-host-urls/SUPER2",
        data={"host_url": " ", "host_subscription_base_url": "https://sub.example/base/"},
    )

    assert response.status_code == 302
    assert calls == []


def test_managed_subscription_route_returns_patched_links(monkeypatch):
    cached_payloads: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda sub_id: {
            "key_id": 12,
            "host_name": "StandartVLESS",
            "panel_sub_id": sub_id,
        },
    )
    monkeypatch.setattr(
        app_module,
        "update_key_subscription_payload",
        lambda key_id, payload: cached_payloads.append((key_id, payload)),
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_url": "https://panel.example/base/",
            "host_username": "admin",
            "host_pass": "pass",
            "host_inbound_id": 1,
        },
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "get_managed_subscription_links",
        lambda host_data, sub_id, **_kwargs: [
            f"vless://uuid@cdn.example:443?type=xhttp&security=tls#{sub_id}",
            "vless://uuid@reality.example:443?type=tcp&security=reality#reality",
        ],
    )

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert response.get_data(as_text=True).splitlines() == [
        "vless://uuid@cdn.example:443?type=xhttp&security=tls#sub-123",
        "vless://uuid@reality.example:443?type=tcp&security=reality#reality",
    ]
    assert cached_payloads == [
        (
            12,
            "vless://uuid@cdn.example:443?type=xhttp&security=tls#sub-123\n"
            "vless://uuid@reality.example:443?type=tcp&security=reality#reality",
        )
    ]


def test_managed_subscription_route_returns_cached_payload_without_panel(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda _sub_id: {
            "key_id": 12,
            "host_name": "StandartVLESS",
            "subscription_payload": (
                "vless://uuid@cdn.example:443?type=xhttp&security=tls#cached\n"
                "vless://uuid@reality.example:443?type=tcp&security=reality#cached"
            ),
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda _host_name: pytest.fail("cached subscription must not call host lookup"),
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "get_managed_subscription_links",
        lambda *_args, **_kwargs: pytest.fail("cached subscription must not call panel"),
    )

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    assert response.status_code == 200
    assert response.get_data(as_text=True).splitlines() == [
        "vless://uuid@cdn.example:443?type=xhttp&security=tls#cached",
        "vless://uuid@reality.example:443?type=tcp&security=reality#cached",
    ]


def test_managed_subscription_route_sets_standard_profile_title(monkeypatch):
    cached_payloads: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda sub_id: {
            "key_id": 12,
            "host_name": "MainPanel",
            "panel_sub_id": sub_id,
            "subscription_tier": "standard",
            "subscription_payload": (
                "vless://uuid@finland.example:443?type=tcp#FINLAND\nvless://uuid@germany.example:443?type=tcp#GERMANY"
            ),
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_subscription_base_url": "https://sub.example/base/",
        },
    )
    monkeypatch.setattr(
        app_module,
        "update_key_subscription_payload",
        lambda key_id, payload: cached_payloads.append((key_id, payload)),
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "mainpanel_expected_subscription_labels",
        lambda host_name, tier: ["WHITELIST1", "FINLAND", "GERMANY", "ESTONIA"],
    )
    live_calls = []

    def _live_links(host_data, sub_id, **kwargs):
        live_calls.append((host_data["host_name"], sub_id, kwargs))
        return [
            'vless://uuid@cdn.example:443?type=ws&path=%2Fapi%2Fuser&host=cdn.example&security=tls#WHITELIST1',
            'vless://uuid@192.0.2.13:443?type=tcp&security=reality#FINLAND',
            'vless://uuid@192.0.2.15:443?type=tcp&security=reality#GERMANY',
            'vless://uuid@192.0.2.10:50790?type=tcp&security=reality#ESTONIA',
        ]

    monkeypatch.setattr(app_module.xui_api, "get_managed_subscription_links", _live_links)

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return 200

        def read(self, _limit=-1):
            return (
                b"vless://uuid@192.0.2.13:443?type=tcp&security=reality#FINLAND-user\n"
                b"vless://uuid@192.0.2.15:443?type=tcp&security=reality#GERMANY-user\n"
                b"vless://uuid@192.0.2.10:50790?type=tcp&security=reality#ESTONIA-user"
            )

    monkeypatch.setattr(app_module.urllib.request, "urlopen", lambda *_args, **_kwargs: _FakeResponse())

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    assert response.status_code == 200
    assert len(live_calls) == 1
    assert live_calls[0] == (
        "MainPanel", "sub-123",
        {"include_white_inbounds": True, "subscription_tier": 'standard',
         "remark_mode": "inbound_label", "client_uuid": None, "base_email": None},
    )
    assert response.headers["profile-title"] == "base64:U1RBTkRBUlQ="
    assert response.headers["content-disposition"] == 'attachment; filename="STANDART.txt"'
    assert response.get_data(as_text=True).splitlines() == [
        'vless://uuid@cdn.example:443?type=ws&path=%2Fapi%2Fuser&host=cdn.example&security=tls#WHITELIST1',
        "vless://uuid@192.0.2.13:443?type=tcp&security=reality#FINLAND",
        "vless://uuid@192.0.2.15:443?type=tcp&security=reality#GERMANY",
        "vless://uuid@192.0.2.10:50790?type=tcp&security=reality#ESTONIA",
    ]
    assert cached_payloads == [(12, response.get_data(as_text=True).strip())]


def test_managed_subscription_route_sets_super_profile_title(monkeypatch):
    cached_payloads: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda sub_id: {
            "key_id": 13,
            "host_name": "MainPanel",
            "panel_sub_id": sub_id,
            "subscription_tier": "super",
            "subscription_payload": (
                "vless://uuid@white.example:443?type=xhttp#WHITELIST1\n"
                "vless://uuid@finland.example:443?type=tcp#FINLAND\n"
                "vless://uuid@germany.example:443?type=tcp#GERMANY"
            ),
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_subscription_base_url": "https://sub.example/base/",
        },
    )
    monkeypatch.setattr(
        app_module,
        "update_key_subscription_payload",
        lambda key_id, payload: cached_payloads.append((key_id, payload)),
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "mainpanel_expected_subscription_labels",
        lambda host_name, tier: ["WHITELIST1", "FINLAND", "GERMANY", "ESTONIA"],
    )
    live_calls = []

    def _live_links(host_data, sub_id, **kwargs):
        live_calls.append((host_data["host_name"], sub_id, kwargs))
        return [
            'vless://uuid@cdn.example:443?type=ws&path=%2Fapi%2Fuser&host=cdn.example&security=tls#WHITELIST1',
            'vless://uuid@192.0.2.13:443?type=tcp&security=reality#FINLAND',
            'vless://uuid@192.0.2.15:443?type=tcp&security=reality#GERMANY',
            'vless://uuid@192.0.2.10:50790?type=tcp&security=reality#ESTONIA',
        ]

    monkeypatch.setattr(app_module.xui_api, "get_managed_subscription_links", _live_links)

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return 200

        def read(self, _limit=-1):
            return (
                b"vless://uuid@cdn.example:443?type=ws&path=%2Fapi%2Fuser&host=cdn.example&security=tls#WHITELIST1-user\n"
                b"vless://uuid@192.0.2.13:443?type=tcp&security=reality#FINLAND-user\n"
                b"vless://uuid@192.0.2.15:443?type=tcp&security=reality#GERMANY-user\n"
                b"vless://uuid@192.0.2.10:50790?type=tcp&security=reality#ESTONIA-user"
            )

    monkeypatch.setattr(app_module.urllib.request, "urlopen", lambda *_args, **_kwargs: _FakeResponse())

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    assert response.status_code == 200
    assert len(live_calls) == 1
    assert live_calls[0] == (
        "MainPanel", "sub-123",
        {"include_white_inbounds": True, "subscription_tier": 'super',
         "remark_mode": "inbound_label", "client_uuid": None, "base_email": None},
    )
    assert response.headers["profile-title"] == "base64:U1VQRVI="
    assert response.headers["content-disposition"] == 'attachment; filename="SUPER.txt"'
    assert response.get_data(as_text=True).splitlines() == [
        "vless://uuid@cdn.example:443?type=ws&path=%2Fapi%2Fuser&host=cdn.example&security=tls#WHITELIST1",
        "vless://uuid@192.0.2.13:443?type=tcp&security=reality#FINLAND",
        "vless://uuid@192.0.2.15:443?type=tcp&security=reality#GERMANY",
        "vless://uuid@192.0.2.10:50790?type=tcp&security=reality#ESTONIA",
    ]
    assert cached_payloads == [(13, response.get_data(as_text=True).strip())]


def test_managed_subscription_route_omits_profile_title_for_unknown_tier(monkeypatch, caplog):
    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda _sub_id: {
            "key_id": 14,
            "host_name": "MainPanel",
            "subscription_tier": "vip",
            "subscription_payload": (
                "vless://uuid@finland.example:443?type=tcp#FINLAND\nvless://uuid@germany.example:443?type=tcp#GERMANY"
            ),
        },
    )

    app, _controller = _build_app(monkeypatch)
    with caplog.at_level(logging.WARNING):
        response = app.test_client().get("/podpiska/sub-123")

    assert response.status_code == 200
    assert "profile-title" not in response.headers
    assert "content-disposition" not in response.headers
    assert "missing or unknown tier" in caplog.text
    assert "sub-123" not in caplog.text


def test_managed_subscription_route_uses_direct_upstream_when_cache_empty(monkeypatch):
    cached_payloads: list[tuple[int, str]] = []
    opened_urls: list[tuple[str, int]] = []

    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda sub_id: {
            "key_id": 12,
            "host_name": "StandartVLESS",
            "panel_sub_id": sub_id,
        },
    )
    monkeypatch.setattr(
        app_module,
        "update_key_subscription_payload",
        lambda key_id, payload: cached_payloads.append((key_id, payload)),
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_subscription_base_url": "https://sub.example/base/",
        },
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "get_managed_subscription_links",
        lambda *_args, **_kwargs: pytest.fail("direct subscription fallback must not call panel API"),
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return 200

        def read(self, _limit=-1):
            return (
                b"vless://uuid@cdn.example:443?type=xhttp&security=tls#direct\n"
                b"vless://uuid@reality.example:443?type=tcp&security=reality#direct"
            )

    def _fake_urlopen(request_obj, *, timeout):
        opened_urls.append((request_obj.full_url, timeout))
        return _FakeResponse()

    monkeypatch.setattr(app_module.urllib.request, "urlopen", _fake_urlopen)

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    assert response.status_code == 200
    assert response.get_data(as_text=True).splitlines() == [
        "vless://uuid@cdn.example:443?type=xhttp&security=tls#direct",
        "vless://uuid@reality.example:443?type=tcp&security=reality#direct",
    ]
    assert opened_urls == [
        ("https://sub.example/base/sub-123", app_module.MANAGED_SUBSCRIPTION_UPSTREAM_TIMEOUT_SECONDS)
    ]
    assert cached_payloads == [
        (
            12,
            "vless://uuid@cdn.example:443?type=xhttp&security=tls#direct\n"
            "vless://uuid@reality.example:443?type=tcp&security=reality#direct",
        )
    ]


def test_managed_subscription_route_uses_stored_direct_url_when_host_base_missing(monkeypatch):
    opened_urls: list[str] = []

    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda sub_id: {
            "key_id": 12,
            "host_name": "StandartVLESS",
            "panel_sub_id": sub_id,
            "issued_key_value": "https://sub.example/base/sub-123",
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_subscription_base_url": "",
        },
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "get_managed_subscription_links",
        lambda *_args, **_kwargs: pytest.fail("stored direct URL must not call panel API"),
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return 200

        def read(self, _limit=-1):
            return b"vless://uuid@cdn.example:443?type=xhttp&security=tls#stored-url"

    def _fake_urlopen(request_obj, *, timeout):
        assert timeout == app_module.MANAGED_SUBSCRIPTION_UPSTREAM_TIMEOUT_SECONDS
        opened_urls.append(request_obj.full_url)
        return _FakeResponse()

    monkeypatch.setattr(app_module.urllib.request, "urlopen", _fake_urlopen)

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    assert response.status_code == 200
    assert response.get_data(as_text=True).splitlines() == [
        "vless://uuid@cdn.example:443?type=xhttp&security=tls#stored-url",
    ]
    assert opened_urls == ["https://sub.example/base/sub-123"]


def test_managed_subscription_route_falls_back_to_stored_connection_on_panel_error(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda _sub_id: {
            "key_id": 12,
            "host_name": "StandartVLESS",
            "issued_key_value": "vless://uuid@reality.example:443?type=tcp&security=reality#stored",
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_subscription_base_url": "",
        },
    )

    def _raise_panel_error(_host_data, _sub_id, **_kwargs):
        raise RuntimeError("panel unavailable")

    monkeypatch.setattr(app_module.xui_api, "get_managed_subscription_links", _raise_panel_error)

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    assert response.status_code == 200
    assert response.get_data(as_text=True).splitlines() == [
        "vless://uuid@reality.example:443?type=tcp&security=reality#stored",
    ]


def test_managed_subscription_route_regenerates_stale_standard_cache_from_live_panel(monkeypatch):
    cached_payloads: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda sub_id: {
            "key_id": 12,
            "host_name": "MainPanel",
            "panel_sub_id": sub_id,
            "subscription_tier": "standard",
            "subscription_payload": (
                "vless://uuid@main.example:443?type=tcp#STANDART\n"
                "vless://uuid@main.example:8443?type=tcp#STANDART\n"
                "vless://uuid@main.example:9443?type=tcp#STANDART"
            ),
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_subscription_base_url": "https://sub.example/base/",
        },
    )
    monkeypatch.setattr(
        app_module,
        "update_key_subscription_payload",
        lambda key_id, payload: cached_payloads.append((key_id, payload)),
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "mainpanel_expected_subscription_labels",
        lambda host_name, tier: ["WHITELIST1", "FINLAND", "GERMANY", "ESTONIA"],
    )
    live_calls = []

    def _live_links(host_data, sub_id, **kwargs):
        live_calls.append((host_data["host_name"], sub_id, kwargs))
        return [
            'vless://uuid@cdn.example:443?type=ws&path=%2Fapi%2Fuser&host=cdn.example&security=tls#WHITELIST1',
            'vless://uuid@finland.example:8443?type=tcp&security=reality&pbk=fin-key&sni=example.org&sid=fin-id&flow=xtls-rprx-vision#FINLAND',
            'vless://uuid@germany.example:9443?type=tcp&security=reality&pbk=de-key&sni=example.net&sid=de-id&flow=xtls-rprx-vision#GERMANY',
            'vless://uuid@estonia.example:50790?type=tcp&security=reality&pbk=ee-key&sni=example.ee&sid=ee-id&flow=xtls-rprx-vision#ESTONIA',
        ]

    monkeypatch.setattr(app_module.xui_api, "get_managed_subscription_links", _live_links)

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return 200

        def read(self, _limit=-1):
            return (
                b"vless://uuid@finland.example:8443?type=tcp&security=reality"
                b"&pbk=fin-key&sni=example.org&sid=fin-id&flow=xtls-rprx-vision#FINLAND-user\n"
                b"vless://uuid@germany.example:9443?type=tcp&security=reality"
                b"&pbk=de-key&sni=example.net&sid=de-id&flow=xtls-rprx-vision#GERMANY-user\n"
                b"vless://uuid@estonia.example:50790?type=tcp&security=reality"
                b"&pbk=ee-key&sni=example.ee&sid=ee-id&flow=xtls-rprx-vision#ESTONIA-user"
            )

    monkeypatch.setattr(app_module.urllib.request, "urlopen", lambda *_args, **_kwargs: _FakeResponse())

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    expected_payload = (
        'vless://uuid@cdn.example:443?type=ws&path=%2Fapi%2Fuser&host=cdn.example&security=tls#WHITELIST1\n'
        "vless://uuid@finland.example:8443?type=tcp&security=reality"
        "&pbk=fin-key&sni=example.org&sid=fin-id&flow=xtls-rprx-vision#FINLAND\n"
        "vless://uuid@germany.example:9443?type=tcp&security=reality"
        "&pbk=de-key&sni=example.net&sid=de-id&flow=xtls-rprx-vision#GERMANY\n"
        "vless://uuid@estonia.example:50790?type=tcp&security=reality"
        "&pbk=ee-key&sni=example.ee&sid=ee-id&flow=xtls-rprx-vision#ESTONIA"
    )
    assert response.status_code == 200
    assert len(live_calls) == 1
    assert live_calls[0] == (
        "MainPanel", "sub-123",
        {"include_white_inbounds": True, "subscription_tier": 'standard',
         "remark_mode": "inbound_label", "client_uuid": None, "base_email": None},
    )
    assert response.get_data(as_text=True).strip() == expected_payload
    assert cached_payloads == [(12, expected_payload)]


def test_managed_subscription_route_falls_back_to_live_links_for_incomplete_known_tier_direct_payload(
    monkeypatch,
):
    cached_payloads: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda sub_id: {
            "key_id": 12,
            "host_name": "MainPanel",
            "panel_sub_id": sub_id,
            "subscription_tier": "standard",
            "subscription_payload": "vless://uuid@wrong.example:443?type=tcp#FINLAND",
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_subscription_base_url": "https://sub.example/base/",
        },
    )
    monkeypatch.setattr(
        app_module,
        "update_key_subscription_payload",
        lambda key_id, payload: cached_payloads.append((key_id, payload)),
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "mainpanel_expected_subscription_labels",
        lambda host_name, tier: ["FINLAND", "GERMANY", "ESTONIA"],
    )
    live_links = [
        "vless://uuid@finland.example:8443?type=tcp#FINLAND",
        "vless://uuid@germany.example:9443?type=tcp#GERMANY",
        "vless://uuid@estonia.example:50790?type=tcp#ESTONIA",
    ]
    monkeypatch.setattr(
        app_module.xui_api,
        "get_managed_subscription_links",
        lambda *_args, **_kwargs: live_links,
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return 200

        def read(self, _limit=-1):
            return b"vless://uuid@finland.example:443?type=tcp#FINLAND-user"

    monkeypatch.setattr(app_module.urllib.request, "urlopen", lambda *_args, **_kwargs: _FakeResponse())

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    expected_payload = "\n".join(live_links)
    assert response.status_code == 200
    assert response.get_data(as_text=True).strip() == expected_payload
    assert cached_payloads == [(12, expected_payload)]


def test_managed_subscription_route_ignores_mainpanel_direct_payload_when_live_links_are_available(monkeypatch):
    cached_payloads: list[tuple[int, str]] = []
    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda sub_id: {
            "key_id": 12,
            "host_name": "MainPanel",
            "panel_sub_id": sub_id,
            "subscription_tier": "standard",
            "subscription_payload": None,
        },
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_url": "https://s83313.cdn.ngenix.net/synthetic-panel-path-001/",
            "host_subscription_base_url": "https://s83313.cdn.ngenix.net/pepelvpn/",
        },
    )
    monkeypatch.setattr(
        app_module,
        "update_key_subscription_payload",
        lambda key_id, payload: cached_payloads.append((key_id, payload)),
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "mainpanel_expected_subscription_labels",
        lambda host_name, tier: ["GERMANY", "ESTONIA"],
    )
    live_links = [
        "vless://uuid@192.0.2.15:443?type=tcp&security=reality#GERMANY",
        "vless://uuid@192.0.2.10:50790?type=tcp&security=reality#ESTONIA",
    ]
    monkeypatch.setattr(
        app_module.xui_api,
        "get_managed_subscription_links",
        lambda *_args, **_kwargs: live_links,
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return 200

        def read(self, _limit=-1):
            return (
                b"vless://uuid@s83313.cdn.ngenix.net:443?type=tcp&security=reality#GERMANY-user\n"
                b"vless://uuid@s83313.cdn.ngenix.net:50790?type=tcp&security=reality#ESTONIA-user"
            )

    monkeypatch.setattr(app_module.urllib.request, "urlopen", lambda *_args, **_kwargs: _FakeResponse())

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    expected_payload = "\n".join(live_links)
    assert response.status_code == 200
    assert response.get_data(as_text=True).strip() == expected_payload
    assert cached_payloads == [(12, expected_payload)]


def test_issued_key_managed_subscription_returns_client_ready_xhttp_link(monkeypatch, tmp_path):
    db_file = tmp_path / "test_users.db"
    monkeypatch.setattr(database, "DB_FILE", db_file)
    database.initialize_db()
    key_id = database.add_new_key(
        user_id=100,
        host_name="SUPER2",
        xui_client_uuid="uuid-1",
        key_email="user100-key1@vpn.bot",
        expiry_timestamp_ms=1_800_000_000_000,
        panel_sub_id="sub-issued",
    )
    assert key_id is not None

    monkeypatch.setattr(app_module, "get_active_key_by_panel_sub_id", database.get_active_key_by_panel_sub_id)
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_url": "https://panel.example/base/",
            "host_username": "admin",
            "host_pass": "pass",
            "host_inbound_id": 1,
        },
    )

    monkeypatch.setattr(
        app_module.xui_api,
        "should_include_white_subscription_inbounds",
        lambda _host_data, **_kwargs: True,
    )

    def _fake_managed_subscription_links(_host_data, sub_id, **kwargs):
        assert sub_id == "sub-issued"
        assert kwargs["include_white_inbounds"] is True
        inbound = SimpleNamespace(
            id=9,
            remark="cdn",
            protocol="vless",
            enable=True,
            streamSettings=json.dumps(
                {
                    "network": "xhttp",
                    "security": "none",
                    "externalProxy": [
                        {
                            "dest": "oyiterhekepog.begetcdn.cloud",
                            "port": 443,
                            "forceTls": "tls",
                            "security": "tls",
                            "fp": "chrome",
                            "alpn": "h3,h2",
                        }
                    ],
                    "xhttpSettings": {
                        "path": "/api/user",
                        "mode": "packet-up",
                        "uplinkHTTPMethod": "GET",
                        "extra": {
                            "xPaddingHeader": "Authorization",
                            "xPaddingKey": "token",
                            "xPaddingMethod": "tokenish",
                            "xPaddingObfsMode": True,
                            "xPaddingPlacement": "queryInHeader",
                        },
                    },
                }
            ),
        )
        raw_link = (
            "vless://uuid-1@oyiterhekepog.begetcdn.cloud:443"
            "?type=xhttp&security=none&fp=chrome&alpn=h3,h2&path=/api/user&mode=packet-up"
            "#Обход белых, не для WIFI-user100-key1"
        )
        return app_module.xui_api.patch_subscription_links_for_inbounds([raw_link], [inbound])

    monkeypatch.setattr(
        app_module.xui_api,
        "get_managed_subscription_links",
        _fake_managed_subscription_links,
    )

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-issued")

    assert response.status_code == 200
    link = response.get_data(as_text=True).strip()
    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    assert "%257B" in parsed.query
    extra = json.loads(unquote(query["extra"][0]))
    assert parsed.hostname == "oyiterhekepog.begetcdn.cloud"
    assert parsed.port == 443
    assert "path=%2Fapi%2Fuser" in parsed.query
    assert "alpn=h2" in parsed.query
    assert unquote(parsed.fragment) == "White (без Wi-Fi)"
    assert query["encryption"] == ["none"]
    assert query["security"] == ["tls"]
    assert query["sni"] == ["oyiterhekepog.begetcdn.cloud"]
    assert query["host"] == ["oyiterhekepog.begetcdn.cloud"]
    assert query["fp"] == ["chrome"]
    assert query["alpn"] == ["h2"]
    assert query["path"] == ["/api/user"]
    assert query["mode"] == ["packet-up"]
    assert extra["path"] == "/api/user"
    assert extra["mode"] == "packet-up"
    assert extra["uplinkHTTPMethod"] == "GET"
    assert extra["headers"]["User-Agent"].startswith("Mozilla/5.0")


def test_managed_subscription_route_returns_404_for_unknown_or_expired_sub_id(monkeypatch):
    monkeypatch.setattr(app_module, "get_active_key_by_panel_sub_id", lambda _sub_id: None)
    monkeypatch.setattr(app_module, "get_key_by_panel_sub_id", lambda _sub_id: None)

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/missing")

    assert response.status_code == 404


def test_managed_subscription_route_denies_known_inactive_key_before_panel_fallback(monkeypatch):
    def _unexpected_panel_lookup(_host_name):
        raise AssertionError

    monkeypatch.setattr(app_module, "get_active_key_by_panel_sub_id", lambda _sub_id: None)
    monkeypatch.setattr(
        app_module,
        "get_key_by_panel_sub_id",
        lambda sub_id: {"key_id": 42, "panel_sub_id": sub_id, "is_archived": 1},
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        _unexpected_panel_lookup,
    )

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/inactive")

    assert response.status_code == 404


def test_managed_subscription_route_returns_safe_502_on_panel_error(monkeypatch):
    secret_message = "secret-token should not leak"

    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda _sub_id: {"key_id": 12, "host_name": "StandartVLESS"},
    )
    monkeypatch.setattr(app_module, "get_host", lambda _host_name: {"host_name": "StandartVLESS"})

    def _raise_panel_error(_host_data, _sub_id, **_kwargs):
        raise RuntimeError(secret_message)

    monkeypatch.setattr(app_module.xui_api, "get_managed_subscription_links", _raise_panel_error)

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    assert response.status_code == 502
    assert "secret-token" not in response.get_data(as_text=True)


def test_managed_subscription_route_does_not_return_500_on_unexpected_error(monkeypatch):
    secret_message = "raw-panel-secret should not leak"

    class _UnexpectedSubscriptionError(Exception):
        pass

    monkeypatch.setattr(
        app_module,
        "get_active_key_by_panel_sub_id",
        lambda _sub_id: {"key_id": 12, "host_name": "StandartVLESS"},
    )
    monkeypatch.setattr(app_module, "get_host", lambda _host_name: {"host_name": "StandartVLESS"})

    def _raise_unexpected_error(_host_data, _sub_id, **_kwargs):
        raise _UnexpectedSubscriptionError(secret_message)

    monkeypatch.setattr(app_module.xui_api, "get_managed_subscription_links", _raise_unexpected_error)

    app, _controller = _build_app(monkeypatch)
    response = app.test_client().get("/podpiska/sub-123")

    assert response.status_code == 502
    assert "raw-panel-secret" not in response.get_data(as_text=True)


def test_update_host_config_route_persists_editable_host_fields(monkeypatch):
    update_calls: list[tuple[str, dict]] = []
    sync_state_calls: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(app_module, "get_host", lambda host_name: {"host_name": host_name})
    monkeypatch.setattr(
        app_module,
        "update_host_config",
        lambda host_name, **kwargs: update_calls.append((host_name, dict(kwargs))) or True,
    )
    monkeypatch.setattr(
        app_module,
        "update_host_node_sync_state",
        lambda host_name, *, status, error=None, **_kwargs: sync_state_calls.append((host_name, status, error)) or True,
    )

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/update-host-config/WHITE2",
        data={
            "host_url": " https://panel.example/path/ ",
            "host_subscription_base_url": " https://sub.example/base/ ",
            "host_username": " admin ",
            "host_pass": " updated-pass ",
            "host_inbound_id": " 77 ",
            "is_white_host": "true",
        },
    )

    assert response.status_code == 302
    assert update_calls == [
        (
            "WHITE2",
            {
                "host_url": "https://panel.example/path/",
                "host_username": "admin",
                "host_pass": "updated-pass",
                "host_inbound_id": 77,
                "subscription_base_url": "https://sub.example/base/",
                "is_white_host": True,
                "delivery_state": "draft",
            },
        )
    ]
    assert sync_state_calls == [("WHITE2", "pending", None)]


def test_update_host_config_route_keeps_current_password_when_edit_password_is_blank(monkeypatch):
    update_calls: list[dict] = []
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name, "host_pass": "stored-pass"},
    )
    monkeypatch.setattr(
        app_module,
        "update_host_config",
        lambda _host_name, **kwargs: update_calls.append(dict(kwargs)) or True,
    )
    monkeypatch.setattr(app_module, "update_host_node_sync_state", lambda *_args, **_kwargs: True)

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/update-host-config/WHITE2",
        data={
            "host_url": "https://panel.example",
            "host_subscription_base_url": "",
            "host_username": "admin",
            "host_pass": "",
            "host_inbound_id": "77",
            "delivery_state": "active",
        },
    )

    assert response.status_code == 302
    assert update_calls[0]["host_pass"] == "stored-pass"


@pytest.mark.parametrize(
    ("data", "expected_error"),
    [
        (
            {
                "host_url": "panel.example",
                "host_username": "admin",
                "host_pass": "pass",
                "host_inbound_id": "77",
            },
            "URL панели",
        ),
        (
            {
                "host_url": "https://panel.example",
                "host_username": "admin",
                "host_pass": "pass",
                "host_inbound_id": "0",
            },
            "ID входящего подключения",
        ),
        (
            {
                "host_url": "https://panel.example",
                "host_username": "",
                "host_pass": "pass",
                "host_inbound_id": "77",
            },
            "Логин XUI",
        ),
    ],
)
def test_update_host_config_route_rejects_invalid_fields(monkeypatch, data, expected_error):
    update_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(app_module, "get_host", lambda host_name: {"host_name": host_name})
    monkeypatch.setattr(
        app_module,
        "update_host_config",
        lambda host_name, **kwargs: update_calls.append((host_name, dict(kwargs))) or True,
    )

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post("/update-host-config/WHITE2", data=data, follow_redirects=True)

    assert response.status_code == 200
    assert expected_error in response.get_data(as_text=True)
    assert update_calls == []


def test_update_host_config_route_rejects_draft_to_active_bypass(monkeypatch):
    update_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name, "host_pass": "stored-pass", "delivery_state": "draft"},
    )
    monkeypatch.setattr(
        app_module,
        "update_host_config",
        lambda host_name, **kwargs: update_calls.append((host_name, dict(kwargs))) or True,
    )

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/update-host-config/WHITE2",
        data={
            "host_url": "https://panel.example",
            "host_username": "admin",
            "host_pass": "",
            "host_inbound_id": "77",
            "delivery_state": "active",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Нельзя активировать хост через обычное сохранение" in response.get_data(as_text=True)
    assert update_calls == []


def test_add_host_route_saves_white_host_as_draft_without_background_sync(monkeypatch):
    create_calls: list[dict] = []
    node_sync_calls: list[dict] = []

    monkeypatch.setattr(
        app_module,
        "create_host",
        lambda **kwargs: create_calls.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name},
    )
    monkeypatch.setattr(
        app_module,
        "update_host_node_sync_state",
        lambda host_name, **kwargs: node_sync_calls.append({"host_name": host_name, **kwargs}) or True,
    )
    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/add-host",
        data={
            "host_name": "WHITE2",
            "host_url": "https://node.example/3xuivless/",
            "host_username": "admin",
            "host_pass": "pass",
            "host_inbound_id": "10",
            "is_white_host": "true",
            "host_api_token_env": "env:XUI_API_TOKEN_WHITE2",
        },
    )

    assert response.status_code == 302
    assert create_calls == [
        {
            "name": "WHITE2",
            "url": "https://node.example/3xuivless/",
            "user": "admin",
            "passwd": "pass",
            "inbound": 10,
            "subscription_base_url": None,
            "api_token_env": "env:XUI_API_TOKEN_WHITE2",
            "is_white_host": True,
            "delivery_state": "draft",
        }
    ]
    assert node_sync_calls == [
        {"host_name": "WHITE2", "status": "pending", "error": None},
    ]


def test_sync_host_node_route_marks_failed_status_on_sync_error(monkeypatch):
    node_sync_calls: list[dict] = []

    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name},
    )
    monkeypatch.setattr(
        app_module,
        "update_host_node_sync_state",
        lambda host_name, **kwargs: node_sync_calls.append({"host_name": host_name, **kwargs}) or True,
    )

    class _SyncFailed(RuntimeError):
        def __init__(self):
            super().__init__("sync failed")

    def _raise_sync_error(_host_name, **_kwargs):
        raise _SyncFailed

    monkeypatch.setattr(
        app_module.xui_api,
        "sync_new_host_into_main_panel",
        _raise_sync_error,
    )
    monkeypatch.setattr(app_module.threading, "Thread", _ImmediateThread)

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post("/sync-host-node/WHITE2", data={"client_limit": "50"})

    assert response.status_code == 302
    assert node_sync_calls[0] == {"host_name": "WHITE2", "status": "pending", "error": None}
    assert node_sync_calls[1] == {"host_name": "WHITE2", "status": "running", "error": None}
    assert node_sync_calls[2]["host_name"] == "WHITE2"
    assert node_sync_calls[2]["status"] == "failed"
    assert "sync failed" in str(node_sync_calls[2]["error"])


def test_sync_host_node_route_passes_batch_options(monkeypatch):
    sync_calls: list[dict] = []
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name},
    )
    monkeypatch.setattr(app_module, "update_host_node_sync_state", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        app_module.xui_api,
        "sync_new_host_into_main_panel",
        lambda host_name, **kwargs: (
            sync_calls.append({"host_name": host_name, **kwargs})
            or {
                "node_id": 12,
                "node_inbound_id": 34,
                "copied_clients": 0,
                "skipped_clients": 0,
            }
        ),
    )
    monkeypatch.setattr(app_module.threading, "Thread", _ImmediateThread)

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post(
        "/sync-host-node/WHITE2",
        data={"client_limit": "25", "dry_run": "true"},
    )

    assert response.status_code == 302
    assert sync_calls == [
        {"host_name": "WHITE2", "client_limit": 25, "dry_run": True},
    ]


def test_activate_host_route_validates_prepares_node_and_marks_active(monkeypatch):
    sync_state_calls: list[dict] = []
    delivery_state_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app_module,
        "get_host",
        lambda host_name: {"host_name": host_name, "is_white_host": 1},
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "validate_host_catalog_entry",
        lambda host_name: {"host_name": host_name, "inbound_id": 3, "inbound_remark": "WHITE_LIST_ONLY"},
    )
    monkeypatch.setattr(
        app_module.xui_api,
        "sync_new_host_into_main_panel",
        lambda host_name, **kwargs: {
            "host_name": host_name,
            "node_id": 12,
            "node_inbound_id": 34,
            "copied_clients": 0,
            "skipped_clients": 0,
            **kwargs,
        },
    )
    monkeypatch.setattr(
        app_module,
        "update_host_node_sync_state",
        lambda host_name, **kwargs: sync_state_calls.append({"host_name": host_name, **kwargs}) or True,
    )
    monkeypatch.setattr(
        app_module,
        "set_host_delivery_state",
        lambda host_name, state: delivery_state_calls.append((host_name, state)) or True,
    )

    app, _controller = _build_app(monkeypatch)
    client = app.test_client()
    _login(client)

    response = client.post("/activate-host/WHITE2")

    assert response.status_code == 302
    assert sync_state_calls == [
        {
            "host_name": "WHITE2",
            "status": "completed",
            "error": None,
            "node_id": 12,
            "node_inbound_id": 34,
        },
    ]
    assert delivery_state_calls == [("WHITE2", "active")]


def test_start_reserve_route_proxies_to_internal_runtime(monkeypatch):
    action_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app_module,
        "_dispatch_runtime_shop_action",
        lambda source, action, timeout=4.0: (
            action_calls.append((source, action))
            or {
                "status": "success",
                "message": "reserve started",
            }
        ),
    )
    monkeypatch.setattr(
        app_module,
        "_load_runtime_status",
        lambda *_args, **_kwargs: {
            "available": True,
            "shop_bot_running": False,
            "all_settings_ok": True,
            "message": "OK",
        },
    )
    app, _controller = _build_app(monkeypatch, bot_source="main")
    client = app.test_client()
    _login(client)

    response = client.post(
        "/start-reserve-bot",
        headers={"Referer": "/settings"},
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert action_calls == [("reserve", "start")]
    assert "reserve started" in body


def test_internal_runtime_status_requires_runtime_token(monkeypatch):
    monkeypatch.setenv("ISSUANCE_BACKEND_MODE", "shadow")
    monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "25")
    monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "1")
    monkeypatch.setattr(
        app_module.scheduler_module,
        "get_loop_health",
        lambda: {
            "issuance_autoretry": {
                "last_success": "2026-05-22T10:30:00",
                "consecutive_crashes": 0,
                "total_cycles": 4,
                "total_crashes": 1,
            }
        },
    )
    app, _controller = _build_app(monkeypatch, bot_source="reserve")
    client = app.test_client()

    forbidden = client.get("/internal/runtime/status")
    assert forbidden.status_code == 403

    monkeypatch.setenv("RUNTIME_CONTROL_TOKEN", "runtime-secret")
    ok = client.get(
        "/internal/runtime/status",
        headers={"X-Runtime-Control-Token": "runtime-secret"},
    )
    payload = ok.get_json()

    assert ok.status_code == 200
    assert payload["bot_source"] == "reserve"
    assert payload["all_settings_ok"] is True
    assert payload["scheduler_loops"]["issuance_autoretry"]["total_crashes"] == 1
    assert payload["issuance_backend"]["mode"] == "shadow"
    assert payload["issuance_backend"]["canary_percent"] == 25
    assert payload["issuance_backend"]["kill_switch"] is True
    assert "shadow" in payload["issuance_backend"]
    assert "legacy_calls" in payload["issuance_backend"]
    assert "legacy_calls_total" in payload["issuance_backend"]


def test_internal_runtime_shop_start_calls_controller(monkeypatch):
    monkeypatch.setenv("RUNTIME_CONTROL_TOKEN", "runtime-secret")
    app, controller = _build_app(monkeypatch, bot_source="reserve")
    client = app.test_client()

    response = client.post(
        "/internal/runtime/shop/start",
        headers={"X-Runtime-Control-Token": "runtime-secret"},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "success"
    assert controller.start_calls == 1


def test_internal_runtime_shop_stop_blocked_by_default(monkeypatch):
    monkeypatch.setenv("RUNTIME_CONTROL_TOKEN", "runtime-secret")
    app, controller = _build_app(monkeypatch, bot_source="reserve")
    client = app.test_client()

    response = client.post(
        "/internal/runtime/shop/stop",
        headers={"X-Runtime-Control-Token": "runtime-secret"},
    )
    payload = response.get_json()

    assert response.status_code == 423
    assert payload["status"] == "error"
    assert "always-on" in payload["message"]
    assert controller.stop_calls == 0


def test_internal_runtime_shop_stop_allowed_when_flag_enabled(monkeypatch):
    monkeypatch.setenv("RUNTIME_CONTROL_TOKEN", "runtime-secret")
    monkeypatch.setenv("ALLOW_RUNTIME_STOP", "true")
    app, controller = _build_app(monkeypatch, bot_source="reserve")
    client = app.test_client()

    response = client.post(
        "/internal/runtime/shop/stop",
        headers={"X-Runtime-Control-Token": "runtime-secret"},
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "success"
    assert controller.stop_calls == 1


def test_panel_stop_shop_route_blocked_by_default(monkeypatch):
    app, controller = _build_app(monkeypatch, bot_source="reserve")
    client = app.test_client()
    _login(client)

    response = client.post(
        "/stop-shop-bot",
        headers={"Referer": "/settings"},
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Остановка отключена политикой always-on." in body
    assert controller.stop_calls == 0


def test_health_live_is_always_ok(monkeypatch):
    app, _controller = _build_app(monkeypatch, bot_source="reserve")
    client = app.test_client()

    response = client.get("/health/live")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["status"] == "ok"
    assert payload["probe"] == "live"


def test_health_ready_reports_not_ready_when_shop_down(monkeypatch):
    app, _controller = _build_app(monkeypatch, bot_source="reserve")
    client = app.test_client()

    response = client.get("/health/ready")
    payload = response.get_json()

    assert response.status_code == 503
    assert payload["status"] == "not_ready"
    assert payload["probe"] == "ready"
    assert payload["shop_bot_running"] is False
