from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlparse

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def _issue90_primary_inbound():
    return SimpleNamespace(id=1, remark="WHITELIST1", tag="", protocol="vless", enable=True, port=443)


def _issue90_options_payload() -> dict:
    return {
        "obj": [
            {"id": 1, "remark": "wHiTe_LiSt1", "tag": "", "protocol": "vless", "enable": True, "port": 443},
            {"id": 2, "remark": "FINLAND", "tag": "", "protocol": "vless", "enable": True, "port": 8443},
            {"id": 4, "remark": "GERMANY", "tag": "", "protocol": "vless", "enable": True, "port": 9443},
            {"id": 5, "remark": "disabled", "tag": "", "protocol": "vless", "enable": False, "port": 10443},
            {"id": 6, "remark": "trojan", "tag": "", "protocol": "trojan", "enable": True, "port": 11443},
        ]
    }


def _identity_fixture(*, email="user10001-key1@vpn.bot", client_uuid="shared-uuid", sub_id="shared-sub"):
    return {
        "id": client_uuid, "email": email, "subId": sub_id,
        "expiryTime": 1_800_000_000_000, "enable": True, "totalGB": 0, "reset": 0,
    }


def _attach_raw_inbound_snapshots(api, inbounds, *, clients_by_id, unavailable_ids=()):
    """Serve complete, independent panel responses instead of bypassing identity planning."""
    rows = {}
    for inbound in inbounds:
        row = dict(inbound) if isinstance(inbound, dict) else vars(inbound).copy()
        if "stream_settings" in row:
            stream_settings = row.pop("stream_settings")
            row["streamSettings"] = (vars(stream_settings).copy()
                                     if isinstance(stream_settings, SimpleNamespace) else stream_settings)
        row["settings"] = json.dumps({"clients": clients_by_id.get(row["id"], [])})
        rows[row["id"]] = row
    reads = []

    def get(endpoint, _headers, **_kwargs):
        if endpoint.startswith("panel/api/inbounds/get/"):
            inbound_id = int(endpoint.rsplit("/", 1)[1])
            reads.append(inbound_id)
            if inbound_id in unavailable_ids:
                raise RuntimeError("synthetic identity snapshot unavailable")
            payload = {"success": True, "obj": rows[inbound_id]}
        elif endpoint in {"panel/api/inbounds/options", "panel/api/inbounds/list", "panel/api/inbounds/list/slim"}:
            payload = {"success": True, "obj": list(rows.values())}
        elif endpoint == "panel/api/nodes/list":
            payload = {"success": True, "obj": []}
        else:
            raise AssertionError(f"Unexpected read endpoint: {endpoint}")
        return SimpleNamespace(json=lambda: json.loads(json.dumps(payload)))

    def record_client(inbound_id, email, values):
        clients = json.loads(rows[inbound_id]["settings"])["clients"]
        payload = {
            "id": values["target_client_uuid"], "email": email, "subId": values["target_sub_id"],
            "enable": True, "expiryTime": values["target_expiry_ms"],
            "totalGB": values["total_gb_bytes"], "reset": values.get("traffic_reset_days") or 0,
        }
        rows[inbound_id]["settings"] = json.dumps({
            "clients": [client for client in clients if client["email"] != email] + [payload],
        })
        return payload["id"], payload["expiryTime"]

    api.inbound._url = lambda endpoint: endpoint
    api.inbound._get = get
    api.inbound._fixture_upsert_client = record_client
    return reads


def test_issue92_classifies_whitelist_by_case_insensitive_substring():
    from shop_bot.modules import xui_api

    assert (
        xui_api.classify_mainpanel_subscription_inbound(
            SimpleNamespace(id=1, remark="WHITELIST3", tag="", protocol="vless", enable=True)
        )
        == xui_api.MAINPANEL_INBOUND_CLASS_WHITELIST
    )
    assert (
        xui_api.classify_mainpanel_subscription_inbound(
            SimpleNamespace(id=2, remark="!Whitelist_germany", tag="", protocol="vless", enable=True)
        )
        == xui_api.MAINPANEL_INBOUND_CLASS_WHITELIST
    )
    assert (
        xui_api.classify_mainpanel_subscription_inbound(
            SimpleNamespace(id=3, remark="WHITE_ONLY", tag="", protocol="vless", enable=True)
        )
        == xui_api.MAINPANEL_INBOUND_CLASS_ORDINARY
    )
    assert (
        xui_api.classify_mainpanel_subscription_inbound(
            SimpleNamespace(id=4, remark="WHITELIST-disabled", tag="", protocol="vless", enable=False)
        )
        == xui_api.MAINPANEL_INBOUND_CLASS_UNSUPPORTED
    )


def test_whitelist_traffic_snapshot_uses_client_stats_expiry_for_reset(monkeypatch):
    from shop_bot.modules import xui_api

    email = "user1-key1-whitelist1-1@vpn.bot"
    traffic_reset_ms = 1_789_473_540_000
    subscription_expiry_ms = 1_798_335_540_000
    fake_api = SimpleNamespace()
    primary_inbound = SimpleNamespace(id=21, remark="WHITELIST1", tag="", protocol="vless", enable=True)
    raw_inbounds = [
        {
            "id": 21,
            "remark": "WHITELIST1",
            "tag": "",
            "protocol": "vless",
            "enable": True,
            "settings": json.dumps(
                {
                    "clients": [
                        {
                            "id": "uuid-1",
                            "email": email,
                            "subId": "sub-1",
                            "enable": True,
                            "totalGB": 50,
                            "reset": 30,
                            "expiryTime": subscription_expiry_ms,
                        }
                    ]
                }
            ),
            "clientStats": [
                {
                    "email": email,
                    "up": 10,
                    "down": 20,
                    "total": 50,
                    "reset": 30,
                    "expiryTime": traffic_reset_ms,
                    "enable": True,
                }
            ],
        }
    ]

    monkeypatch.setattr(
        xui_api,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_url": "https://panel.example.test",
            "host_username": "admin",
            "host_pass": "pass",
            "host_inbound_id": 21,
        },
    )
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary_inbound))
    monkeypatch.setattr(xui_api, "_authenticate_api", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(xui_api, "_list_inbounds_raw", lambda _api: raw_inbounds)

    snapshot = xui_api.get_whitelist_traffic_snapshot("MainPanel")

    assert snapshot[email]["client_uuid"] == "uuid-1"
    assert snapshot[email]["reset_days"] == 30
    assert snapshot[email]["reset_timestamp_ms"] == traffic_reset_ms
    assert snapshot[email]["reset_timestamp_ms"] != subscription_expiry_ms
    assert snapshot[email]["settings_expiry_ms"] == subscription_expiry_ms
    assert snapshot[email]["traffic_expiry_ms"] == traffic_reset_ms
    assert snapshot[email]["settings_total_bytes"] == 50
    assert snapshot[email]["traffic_total_bytes"] == 50
    assert snapshot[email]["traffic_up_bytes"] == 10
    assert snapshot[email]["traffic_down_bytes"] == 20


def _issue92_raw_inbounds():
    settings_with_primary_client = json.dumps(
        {
            "clients": [
                {
                    "id": "uuid-1",
                    "email": "user1-key1@vpn.bot",
                    "subId": "sub-1",
                    "enable": True,
                    "expiryTime": 1_800_014_400_000,
                }
            ]
        }
    )
    empty_settings = json.dumps({"clients": []})
    return [
        {
            "id": 1,
            "remark": "WHITELIST1",
            "tag": "",
            "protocol": "vless",
            "enable": True,
            "port": 443,
            "settings": empty_settings,
        },
        {
            "id": 2,
            "remark": "FINLAND",
            "tag": "",
            "protocol": "vless",
            "enable": True,
            "port": 8443,
            "settings": settings_with_primary_client,
        },
        {
            "id": 4,
            "remark": "GERMANY",
            "tag": "",
            "protocol": "vless",
            "enable": True,
            "port": 9443,
            "settings": empty_settings,
        },
    ]


def _patch_issue92_mainpanel_sync(monkeypatch, subscription_tier):
    from shop_bot.modules import xui_api

    monkeypatch.setattr(xui_api.time, "time", lambda: 1_790_000_000)
    raw_inbounds = _issue92_raw_inbounds()
    primary_inbound = SimpleNamespace(id=2, remark="FINLAND", tag="", protocol="vless", enable=True, port=8443)
    fake_api = SimpleNamespace()
    calls = []
    cleared = []

    monkeypatch.setattr(
        xui_api,
        "get_host",
        lambda host_name: {
            "host_name": host_name,
            "host_url": "https://panel.example.test",
            "host_username": "admin",
            "host_pass": "pass",
            "host_inbound_id": 2,
        },
    )
    monkeypatch.setattr(xui_api, "login_to_host", lambda **kwargs: (fake_api, primary_inbound))
    monkeypatch.setattr(xui_api, "_list_inbounds_raw", lambda api: raw_inbounds)
    monkeypatch.setattr(xui_api, "get_mainpanel_inbound_snapshot", lambda host_name: {})
    monkeypatch.setattr(xui_api, "upsert_mainpanel_inbound_snapshot", lambda host_name, inbounds: None)
    monkeypatch.setattr(xui_api, "create_mainpanel_inbound_sync_run", lambda **kwargs: "run-issue92")
    monkeypatch.setattr(xui_api, "add_mainpanel_inbound_sync_item", lambda run_id, item: None)
    monkeypatch.setattr(xui_api, "update_mainpanel_inbound_sync_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(xui_api, "get_mainpanel_inbound_sync_run", lambda run_id, include_items=True: None)

    def _track_cleared_key(key_id):
        cleared.append(key_id)

    monkeypatch.setattr(xui_api, "clear_key_subscription_payload", _track_cleared_key)
    monkeypatch.setattr(
        xui_api,
        "get_active_keys_for_host",
        lambda host_name: [
            {
                "key_id": 10,
                "user_id": 100,
                "host_name": host_name,
                "xui_client_uuid": "uuid-1",
                "key_email": "user1-key1@vpn.bot",
                "panel_sub_id": "sub-1",
                "subscription_tier": subscription_tier,
                "expiry_date": "2027-01-15T12:00:00",
            }
        ],
    )

    def _fake_update_or_create_client_on_panel(**kwargs):
        calls.append(kwargs)
        return kwargs["target_client_uuid"], kwargs["target_expiry_ms"]

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _fake_update_or_create_client_on_panel)
    return xui_api, calls, cleared


def test_issue92_sync_adds_capped_whitelist_for_standard(monkeypatch):
    xui_api, calls, cleared = _patch_issue92_mainpanel_sync(monkeypatch, "standard")

    result = xui_api.sync_existing_mainpanel_clients_to_inbounds(
        "SUPER2",
        dry_run=False,
        batch_limit=10,
        persist_snapshot=True,
    )

    assert result["updated"] == 2
    assert [(call["inbound_id"], call["email"], call["total_gb_bytes"]) for call in calls] == [
        (1, "user1-key1-whitelist1-1@vpn.bot", 5 * 1024 ** 3),
        (4, "user1-key1-germany-4@vpn.bot", 0)
    ]
    assert cleared == [10]


def test_issue92_sync_adds_whitelist_for_super_with_traffic_limit(monkeypatch):
    xui_api, calls, cleared = _patch_issue92_mainpanel_sync(monkeypatch, "super")

    result = xui_api.sync_existing_mainpanel_clients_to_inbounds(
        "SUPER2",
        dry_run=False,
        batch_limit=10,
        persist_snapshot=True,
    )

    assert result["updated"] == 2
    assert [(call["inbound_id"], call["total_gb_bytes"]) for call in calls] == [
        (1, xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES),
        (4, 0),
    ]
    assert cleared == [10]


def test_issue92_sync_clears_cache_when_new_inbound_already_synced(monkeypatch):
    xui_api, calls, cleared = _patch_issue92_mainpanel_sync(monkeypatch, "standard")

    raw_inbounds = _issue92_raw_inbounds()
    raw_inbounds[2]["settings"] = json.dumps(
        {
            "clients": [
                {
                    "id": "uuid-1",
                    "email": "user1-key1-germany-4@vpn.bot",
                    "subId": "sub-1",
                    "enable": True,
                    "expiryTime": 1_800_014_400_000,
                }
            ]
        }
    )
    raw_inbounds[0]["settings"] = json.dumps({"clients": [{
        "id": "uuid-1", "email": "user1-key1-whitelist1-1@vpn.bot",
        "subId": "sub-white", "enable": True, "totalGB": 5 * 1024 ** 3,
        "expiryTime": 1_792_592_000_000, "reset": 30,
    }]})
    monkeypatch.setattr(xui_api, "_list_inbounds_raw", lambda api: raw_inbounds)

    result = xui_api.sync_existing_mainpanel_clients_to_inbounds(
        "SUPER2",
        dry_run=False,
        batch_limit=10,
        persist_snapshot=True,
    )

    assert result["updated"] == 0
    assert calls == []
    assert cleared == [10]


def test_issue92_snapshot_prunes_removed_inbounds(monkeypatch, tmp_path):
    from shop_bot.data_manager import database

    db_file = tmp_path / "test_users.db"
    monkeypatch.setattr(database, "DB_FILE", db_file)
    database.initialize_db()

    database.upsert_mainpanel_inbound_snapshot(
        "MainPanel",
        [
            {"inbound_id": 1, "remark": "WHITELIST1", "protocol": "vless", "enable": True},
            {"inbound_id": 2, "remark": "FINLAND", "protocol": "vless", "enable": True},
            {"inbound_id": 4, "remark": "GERMANY", "protocol": "vless", "enable": True},
        ],
    )
    database.upsert_mainpanel_inbound_snapshot(
        "MainPanel",
        [
            {"inbound_id": 1, "remark": "WHITELIST1", "protocol": "vless", "enable": True},
            {"inbound_id": 4, "remark": "GERMANY", "protocol": "vless", "enable": True},
            {"inbound_id": 6, "remark": "ESTONIA", "protocol": "vless", "enable": True},
        ],
    )

    snapshot = database.get_mainpanel_inbound_snapshot("MainPanel")

    assert set(snapshot) == {1, 4, 6}
    assert snapshot[6]["remark"] == "ESTONIA"
    assert 2 not in snapshot


def test_normalize_subscription_payload_decodes_base64_body():
    from shop_bot.modules import xui_api

    raw_payload = (
        "vless://uuid@cdn.example:443?type=xhttp&security=tls#cdn\n"
        "vless://uuid@reality.example:443?type=tcp&security=reality#reality"
    )
    encoded_payload = base64.b64encode(raw_payload.encode()).decode()

    assert xui_api.normalize_subscription_payload(encoded_payload) == raw_payload


def test_subscription_payload_from_result_uses_primary_fallback_for_not_ready_multi_node_result():
    from shop_bot.modules import xui_api

    payload = xui_api.subscription_payload_from_result(
        {
            "connection_string": "vless://uuid@main.example:443?type=tcp&security=reality#SUPER2",
            "subscription_links": [],
            "secondary_inbounds": [{"inbound_id": 16}],
        }
    )

    assert payload == "vless://uuid@main.example:443?type=tcp&security=reality#Standard"


def test_subscription_payload_from_result_preserves_node_labels_for_both_tiers():
    from shop_bot.modules import xui_api

    result = {
        "subscription_links": [
            "vless://uuid@main.example:8443?type=tcp&security=reality#FINLAND",
            "vless://uuid@main.example:9443?type=tcp&security=reality#GERMANY",
        ]
    }

    assert xui_api.subscription_payload_from_result(
        result,
        subscription_tier="standard",
    ).splitlines() == [
        "vless://uuid@main.example:8443?type=tcp&security=reality#FINLAND",
        "vless://uuid@main.example:9443?type=tcp&security=reality#GERMANY",
    ]
    assert xui_api.subscription_payload_from_result(
        result,
        subscription_tier="super",
    ).splitlines() == [
        "vless://uuid@main.example:8443?type=tcp&security=reality#FINLAND",
        "vless://uuid@main.example:9443?type=tcp&security=reality#GERMANY",
    ]


def test_managed_subscription_links_from_direct_payload_preserves_connection_fields():
    from shop_bot.modules import xui_api

    finland = (
        "vless://uuid@finland.example:443"
        "?type=tcp&security=reality&pbk=fin-key&fp=chrome&sni=example.org"
        "&sid=fin-short-id&spx=%2F&flow=xtls-rprx-vision"
        "#FINLAND-user-key"
    )
    germany = (
        "vless://uuid@germany.example:8443"
        "?type=tcp&security=reality&pbk=de-key&fp=chrome&sni=example.net"
        "&sid=de-short-id&spx=%2F&flow=xtls-rprx-vision"
        "#GERMANY-user-key"
    )
    estonia = (
        "vless://uuid@192.0.2.10:50790"
        "?type=tcp&security=reality&pbk=ee-key&fp=chrome&sni=example.ee"
        "&sid=ee-short-id&spx=%2F&flow=xtls-rprx-vision"
        "#ESTONIA-user-key"
    )
    white = (
        "vless://uuid@cdn.example:443"
        "?encryption=none&type=ws&path=%2Fapi%2Fuser&host=cdn.example"
        "&security=tls&sni=cdn.example&fp=chrome"
        "#WHITELIST1-user-key"
    )

    links = xui_api.managed_subscription_links_from_direct_payload(
        "\n".join((white, germany, estonia, finland)),
        subscription_tier="super",
        expected_labels=("WHITELIST1", "FINLAND", "GERMANY", "ESTONIA"),
    )

    assert [unquote(urlparse(link).fragment) for link in links] == [
        "WHITELIST1",
        "FINLAND",
        "GERMANY",
        "ESTONIA",
    ]
    assert [urlparse(link)._replace(fragment="").geturl() for link in links] == [
        urlparse(white)._replace(fragment="").geturl(),
        urlparse(finland)._replace(fragment="").geturl(),
        urlparse(germany)._replace(fragment="").geturl(),
        urlparse(estonia)._replace(fragment="").geturl(),
    ]


def test_managed_subscription_links_from_direct_payload_filters_white_for_standard():
    from shop_bot.modules import xui_api

    links = xui_api.managed_subscription_links_from_direct_payload(
        "\n".join(
            (
                "vless://uuid@cdn.example:443?type=ws#WHITELIST1-user-key",
                "vless://uuid@finland.example:443?type=tcp#FINLAND-user-key",
                "vless://uuid@germany.example:443?type=tcp#GERMANY-user-key",
                "vless://uuid@192.0.2.10:50790?type=tcp#ESTONIA-user-key",
            )
        ),
        subscription_tier="standard",
        expected_labels=("FINLAND", "GERMANY", "ESTONIA"),
    )

    assert [unquote(urlparse(link).fragment) for link in links] == ["FINLAND", "GERMANY", "ESTONIA"]
    assert all("WHITELIST" not in link for link in links)


def test_managed_subscription_links_from_direct_payload_fails_closed_when_incomplete():
    from shop_bot.modules import xui_api

    links = xui_api.managed_subscription_links_from_direct_payload(
        "vless://uuid@finland.example:443?type=tcp#FINLAND-user-key",
        subscription_tier="standard",
        expected_labels=("FINLAND", "GERMANY", "ESTONIA"),
    )

    assert links == []


def test_blocking_panel_call_timeout_does_not_wait_for_stuck_function():
    from shop_bot.modules import xui_api

    def _stuck_call():
        time.sleep(2)
        return "late"

    started = time.monotonic()
    try:
        asyncio.run(
            xui_api._run_blocking_with_timeout(
                _stuck_call,
                timeout=0.1,
                retries=1,
                error_context="test-stuck-panel-call",
            )
        )
    except TimeoutError as exc:
        assert "panel_call_timeout:0.1s" in str(exc)
    else:
        raise AssertionError("Expected stuck panel call to time out")

    assert time.monotonic() - started < 1.0


def test_login_to_host_detailed_recovers_after_transient_connection_refused(monkeypatch):
    from shop_bot.modules import xui_api

    attempts = {"count": 0}

    class _FakeInboundApi:
        def get_list(self):
            return [SimpleNamespace(id=1)]

    class _FakeApi:
        def __init__(self, **_kwargs):
            self.inbound = _FakeInboundApi()

        def login(self):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError(
                    "HTTPSConnectionPool: Failed to establish a new connection: [Errno 111] Connection refused"
                )

    monkeypatch.setattr(xui_api, "Api", _FakeApi)
    monkeypatch.setattr(xui_api.time, "sleep", lambda _seconds: None)

    result = xui_api.login_to_host_detailed(
        host_url="https://panel.example.com",
        username="user",
        password="pass",
        inbound_id=1,
        retries=1,
        retry_delay_seconds=0,
    )

    assert attempts["count"] == 2
    assert result.api is not None
    assert result.inbound is not None
    assert result.error_code is None
    assert result.recovered_on_retry is True
    assert result.attempts == 2


def test_login_to_host_detailed_falls_back_when_raw_lookup_fails(monkeypatch):
    from shop_bot.modules import xui_api

    class _FakeInboundApi:
        def get_list(self):
            return [SimpleNamespace(id=7)]

    class _FakeApi:
        def __init__(self, **_kwargs):
            self.inbound = _FakeInboundApi()

        def login(self):
            return None

    monkeypatch.setattr(xui_api, "Api", _FakeApi)
    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("raw_inbound_lookup_failed")),
    )

    result = xui_api.login_to_host_detailed(
        host_url="https://panel.example.com",
        username="user",
        password="pass",
        inbound_id=7,
        retries=0,
        prefer_raw_inbound_lookup=True,
    )

    assert result.error_code is None
    assert result.api is not None
    assert result.inbound is not None
    assert result.inbound.id == 7


def test_login_to_host_detailed_can_skip_full_lookup_for_health_probe(monkeypatch):
    from shop_bot.modules import xui_api

    list_calls = {"count": 0}

    class _FakeInboundApi:
        def get_list(self):
            list_calls["count"] += 1
            return [SimpleNamespace(id=7)]

    class _FakeApi:
        def __init__(self, **_kwargs):
            self.inbound = _FakeInboundApi()

        def login(self):
            return None

    monkeypatch.setattr(xui_api, "Api", _FakeApi)
    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("raw_inbound_lookup_failed")),
    )

    result = xui_api.login_to_host_detailed(
        host_url="https://panel.example.com",
        username="user",
        password="pass",
        inbound_id=7,
        retries=0,
        prefer_raw_inbound_lookup=True,
        fallback_to_full_inbound_lookup=False,
        allow_minimal_inbound_on_lookup_failure=True,
    )

    assert list_calls["count"] == 0
    assert result.error_code is None
    assert result.api is not None
    assert result.inbound is not None
    assert result.inbound.id == 7


def test_login_to_host_detailed_does_not_mask_missing_raw_inbound(monkeypatch):
    from shop_bot.modules import xui_api

    list_calls = {"count": 0}

    class _FakeInboundApi:
        def get_list(self):
            list_calls["count"] += 1
            return [SimpleNamespace(id=7)]

    class _FakeApi:
        def __init__(self, **_kwargs):
            self.inbound = _FakeInboundApi()

        def login(self):
            return None

    monkeypatch.setattr(xui_api, "Api", _FakeApi)
    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(xui_api.PanelInboundLookupError("panel_inbound_not_found", 7)),
    )

    result = xui_api.login_to_host_detailed(
        host_url="https://panel.example.com",
        username="user",
        password="pass",
        inbound_id=7,
        retries=0,
        prefer_raw_inbound_lookup=True,
        fallback_to_full_inbound_lookup=False,
        allow_minimal_inbound_on_lookup_failure=True,
    )

    assert list_calls["count"] == 0
    assert result.api is not None
    assert result.inbound is None
    assert result.error_code == "host_inbound_missing"


def test_login_to_host_detailed_does_not_mask_raw_lookup_ssl_error(monkeypatch):
    from shop_bot.modules import xui_api

    class _FakeInboundApi:
        def get_list(self):
            raise AssertionError("ssl raw lookup failure must not fallback to full inbound list")

    class _FakeApi:
        def __init__(self, **_kwargs):
            self.inbound = _FakeInboundApi()

        def login(self):
            return None

    monkeypatch.setattr(xui_api, "Api", _FakeApi)
    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            xui_api.PanelInboundLookupError(
                "panel_raw_lookup_failed",
                7,
                "[SSL: WRONG_VERSION_NUMBER] wrong version number",
            )
        ),
    )

    result = xui_api.login_to_host_detailed(
        host_url="https://panel.example.com",
        username="user",
        password="pass",
        inbound_id=7,
        retries=0,
        prefer_raw_inbound_lookup=True,
        fallback_to_full_inbound_lookup=False,
        allow_minimal_inbound_on_lookup_failure=True,
    )

    assert result.api is None
    assert result.inbound is None
    assert result.error_code == "host_ssl_error"


def test_login_to_host_detailed_fails_closed_without_minimal_inbound(monkeypatch):
    from shop_bot.modules import xui_api

    class _FakeInboundApi:
        def get_list(self):
            raise RuntimeError("list_lookup_failed")

    class _FakeApi:
        def __init__(self, **_kwargs):
            self.inbound = _FakeInboundApi()

        def login(self):
            return None

    monkeypatch.setattr(xui_api, "Api", _FakeApi)
    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("raw_inbound_lookup_failed")),
    )

    result = xui_api.login_to_host_detailed(
        host_url="https://panel.example.com",
        username="user",
        password="pass",
        inbound_id=7,
        retries=0,
        prefer_raw_inbound_lookup=True,
    )

    assert result.api is None
    assert result.inbound is None
    assert result.error_code == "host_login_exception"
    assert "list_lookup_failed" in str(result.error_message)


def test_attach_api_token_adds_bearer_header_to_panel_requests():
    from shop_bot.modules import xui_api

    captured: dict[str, str] = {}

    class _FakeApiPart:
        def _request_with_retry(self, _method, _url, headers, **_kwargs):
            captured.update(headers)
            return SimpleNamespace()

        def _get(self, _url, _headers, **_kwargs):
            return SimpleNamespace()

        def _post(self, _url, _headers, _data, **_kwargs):
            return SimpleNamespace()

    fake_api = SimpleNamespace(
        client=_FakeApiPart(),
        inbound=_FakeApiPart(),
        database=_FakeApiPart(),
        server=_FakeApiPart(),
    )

    xui_api._attach_api_token(fake_api, "secret-token")
    fake_api.inbound._request_with_retry(lambda *_args, **_kwargs: None, "https://panel/panel/api/inbounds/list", {})

    assert captured["Authorization"] == "Bearer secret-token"


def test_authenticate_api_token_mode_does_not_call_login_or_set_cookie_session():
    from shop_bot.modules import xui_api

    class _FakeApi:
        def __init__(self):
            self.session = None
            self.cookie_name = None
            self.login_calls = 0

        def login(self):
            self.login_calls += 1

    fake_api = _FakeApi()

    mode = xui_api._authenticate_api(fake_api, "secret-token")

    assert mode == "bearer"
    assert fake_api.session is None
    assert fake_api.cookie_name is None
    assert fake_api.login_calls == 0


def test_login_to_host_detailed_token_mode_skips_login_call(monkeypatch):
    from shop_bot.modules import xui_api

    class _FakeApiPart:
        def _request_with_retry(self, _method, _url, _headers, **_kwargs):
            return SimpleNamespace()

        def _get(self, _url, _headers, **_kwargs):
            return SimpleNamespace()

        def _post(self, _url, _headers, _data, **_kwargs):
            return SimpleNamespace()

    class _FakeInboundApi:
        def _request_with_retry(self, _method, _url, _headers, **_kwargs):
            return SimpleNamespace()

        def _get(self, _url, _headers, **_kwargs):
            return SimpleNamespace()

        def _post(self, _url, _headers, _data, **_kwargs):
            return SimpleNamespace()

        def get_list(self):
            return [SimpleNamespace(id=3)]

    class _FakeApi:
        def __init__(self, **_kwargs):
            self.client = _FakeApiPart()
            self.inbound = _FakeInboundApi()
            self.database = _FakeApiPart()
            self.server = _FakeApiPart()
            self.session = None
            self.cookie_name = None

        def login(self):
            raise AssertionError

    monkeypatch.setattr(xui_api, "Api", _FakeApi)

    result = xui_api.login_to_host_detailed(
        host_url="https://panel.example.com",
        username="user",
        password="pass",
        inbound_id=3,
        api_token="secret-token",
        retries=0,
    )

    assert result.error_code is None
    assert result.inbound is not None


def test_resolve_host_api_token_reads_configured_env(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.setenv("XUI_API_TOKEN_HOST", "secret-token")

    token = xui_api._resolve_host_api_token({"host_api_token_env": "XUI_API_TOKEN_HOST"})

    assert token == "secret-token"


def test_resolve_host_api_token_accepts_direct_literal():
    from shop_bot.modules import xui_api

    token = xui_api._resolve_host_api_token({"host_api_token_env": "literal-token-value"})

    assert token == "literal-token-value"


def test_resolve_host_api_token_uses_direct_literal_when_env_name_is_missing(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.delenv("XUI_API_TOKEN_MISSING", raising=False)

    token = xui_api._resolve_host_api_token({"host_api_token_env": "XUI_API_TOKEN_MISSING"})

    assert token == "XUI_API_TOKEN_MISSING"


def test_resolve_host_api_token_returns_none_for_missing_explicit_env_name(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.delenv("XUI_API_TOKEN_MISSING", raising=False)

    token = xui_api._resolve_host_api_token({"host_api_token_env": "env:XUI_API_TOKEN_MISSING"})

    assert token is None


def test_resolve_host_api_token_uses_auto_env_name_from_host_name(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.setenv("XUI_API_TOKEN_STANDARTVLESS", "auto-token")

    token = xui_api._resolve_host_api_token(
        {
            "host_name": "StandartVLESS",
            "host_api_token_env": "",
        }
    )

    assert token == "auto-token"


def test_resolve_host_api_token_keeps_none_when_auto_env_is_absent(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.delenv("XUI_API_TOKEN_STANDARTVLESS", raising=False)

    token = xui_api._resolve_host_api_token(
        {
            "host_name": "StandartVLESS",
            "host_api_token_env": "",
        }
    )

    assert token is None


def test_standard_subscription_helper_forwards_purchase_timeout(monkeypatch):
    from shop_bot.modules import xui_api

    calls: list[dict] = []

    async def _fake_create_or_update_key_on_host(host_name, email, days_to_add, **kwargs):
        calls.append(
            {
                "host_name": host_name,
                "email": email,
                "days_to_add": days_to_add,
                **kwargs,
            }
        )
        return {"host_name": host_name}

    monkeypatch.setattr(xui_api, "create_or_update_key_on_host", _fake_create_or_update_key_on_host)

    result = asyncio.run(
        xui_api.create_or_update_standard_subscription_on_hosts(
            "MainPanel",
            "user1-key1@vpn.bot",
            30,
            1,
            max_attempts_per_host=5,
            panel_call_timeout_seconds=68,
        )
    )

    assert result == {"host_name": "MainPanel", "months": 1}
    assert calls[0]["max_attempts"] == 5
    assert calls[0]["panel_call_timeout_seconds"] == 68


def test_fallback_helper_forwards_purchase_timeout(monkeypatch):
    from shop_bot.modules import xui_api

    calls: list[dict] = []

    async def _fake_create_or_update_key_on_host(host_name, email, days_to_add, **kwargs):
        calls.append(
            {
                "host_name": host_name,
                "email": email,
                "days_to_add": days_to_add,
                **kwargs,
            }
        )
        return {"host_name": host_name}

    monkeypatch.setattr(xui_api, "create_or_update_key_on_host", _fake_create_or_update_key_on_host)

    result = asyncio.run(
        xui_api.create_or_update_key_with_fallback(
            "MainPanel",
            "user1-key1@vpn.bot",
            30,
            retry_attempts_per_host=5,
            panel_call_timeout_seconds=68,
        )
    )

    assert result == {"host_name": "MainPanel"}
    assert calls[0]["max_attempts"] == 5
    assert calls[0]["panel_call_timeout_seconds"] == 68


def test_login_to_host_detailed_classifies_inbound_missing(monkeypatch):
    from shop_bot.modules import xui_api

    class _FakeInboundApi:
        def get_list(self):
            return [SimpleNamespace(id=2)]

    class _FakeApi:
        def __init__(self, **_kwargs):
            self.inbound = _FakeInboundApi()

        def login(self):
            return None

    monkeypatch.setattr(xui_api, "Api", _FakeApi)

    result = xui_api.login_to_host_detailed(
        host_url="https://panel.example.com",
        username="user",
        password="pass",
        inbound_id=1,
        retries=0,
    )

    assert result.api is not None
    assert result.inbound is None
    assert result.error_code == "host_inbound_missing"
    assert "Inbound with ID '1'" in str(result.error_message)


def test_login_to_host_detailed_fails_fast_on_auth_error(monkeypatch):
    from shop_bot.modules import xui_api

    attempts = {"count": 0}

    class _FakeApi:
        def __init__(self, **_kwargs):
            self.inbound = SimpleNamespace(get_list=lambda: [])

        def login(self):
            attempts["count"] += 1
            raise RuntimeError("login failed: unauthorized")

    monkeypatch.setattr(xui_api, "Api", _FakeApi)
    monkeypatch.setattr(xui_api.time, "sleep", lambda _seconds: None)

    result = xui_api.login_to_host_detailed(
        host_url="https://panel.example.com",
        username="user",
        password="bad-pass",
        inbound_id=1,
        retries=2,
        retry_delay_seconds=0,
    )

    assert attempts["count"] == 1
    assert result.api is None
    assert result.inbound is None
    assert result.error_code == "host_login_failed"
    assert result.recovered_on_retry is False


def test_login_to_host_detailed_opens_circuit_after_repeated_network_failures(monkeypatch):
    from shop_bot.modules import xui_api

    xui_api.reset_host_circuit_breaker_state()
    monkeypatch.setattr(xui_api, "HOST_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 2)
    monkeypatch.setattr(xui_api, "HOST_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 60)
    monkeypatch.setattr(xui_api.time, "sleep", lambda _seconds: None)

    attempts = {"count": 0}

    class _FakeApi:
        def __init__(self, **_kwargs):
            self.inbound = SimpleNamespace(get_list=lambda: [])

        def login(self):
            attempts["count"] += 1
            raise RuntimeError("timed out")

    monkeypatch.setattr(xui_api, "Api", _FakeApi)

    host_url = "https://circuit.example.com"

    result_1 = xui_api.login_to_host_detailed(
        host_url=host_url,
        username="user",
        password="pass",
        inbound_id=1,
        retries=0,
    )
    result_2 = xui_api.login_to_host_detailed(
        host_url=host_url,
        username="user",
        password="pass",
        inbound_id=1,
        retries=0,
    )
    result_3 = xui_api.login_to_host_detailed(
        host_url=host_url,
        username="user",
        password="pass",
        inbound_id=1,
        retries=0,
    )

    assert result_1.error_code == "host_timeout"
    assert result_2.error_code == "host_circuit_open"
    assert result_3.error_code == "host_circuit_open"
    assert attempts["count"] == 2


def test_host_circuit_breaker_state_prunes_to_max_entries(monkeypatch):
    from shop_bot.modules import xui_api

    xui_api.reset_host_circuit_breaker_state()
    monkeypatch.setattr(xui_api, "HOST_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 99)
    monkeypatch.setattr(xui_api, "HOST_CIRCUIT_BREAKER_MAX_ENTRIES", 2)
    monkeypatch.setattr(xui_api, "HOST_CIRCUIT_BREAKER_STATE_TTL_SECONDS", 10_000)

    now_ref = {"value": 1_000.0}
    monkeypatch.setattr(xui_api.time, "time", lambda: now_ref["value"])

    xui_api._host_circuit_breaker_on_failure("https://h1.example", "host_timeout")
    now_ref["value"] += 1
    xui_api._host_circuit_breaker_on_failure("https://h2.example", "host_timeout")
    now_ref["value"] += 1
    xui_api._host_circuit_breaker_on_failure("https://h3.example", "host_timeout")

    with xui_api._host_circuit_breaker_lock:
        keys = set(xui_api._host_circuit_breaker_state.keys())
    assert len(keys) == 2
    assert "https://h1.example" not in keys


def test_host_circuit_breaker_precheck_drops_expired_open_state(monkeypatch):
    from shop_bot.modules import xui_api

    xui_api.reset_host_circuit_breaker_state()
    monkeypatch.setattr(xui_api, "HOST_CIRCUIT_BREAKER_FAILURE_THRESHOLD", 1)
    monkeypatch.setattr(xui_api, "HOST_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 30)
    monkeypatch.setattr(xui_api, "HOST_CIRCUIT_BREAKER_STATE_TTL_SECONDS", 300)

    now_ref = {"value": 2_000.0}
    monkeypatch.setattr(xui_api.time, "time", lambda: now_ref["value"])

    host_url = "https://cooldown.example"
    xui_api._host_circuit_breaker_on_failure(host_url, "host_timeout")
    is_open, remaining = xui_api._host_circuit_breaker_precheck(host_url)
    assert is_open is True
    assert remaining > 0

    now_ref["value"] += 31
    is_open, remaining = xui_api._host_circuit_breaker_precheck(host_url)
    assert is_open is False
    assert remaining == 0

    with xui_api._host_circuit_breaker_lock:
        assert xui_api._host_circuit_breaker_state.get(host_url) is None


def test_host_health_cache_returns_clients_only_for_healthy_recent_snapshot():
    from shop_bot.modules import xui_api

    now_ts = xui_api.time.time()
    xui_api.update_host_health_snapshot(
        "h1",
        status="healthy",
        clients_count=17,
        checked_at=now_ts,
    )
    assert xui_api.get_cached_host_clients_count("h1", max_age_seconds=60) == 17

    xui_api.update_host_health_snapshot(
        "h1",
        status="unavailable",
        clients_count=17,
        checked_at=now_ts,
    )
    assert xui_api.get_cached_host_clients_count("h1", max_age_seconds=60) is None

    xui_api.update_host_health_snapshot(
        "h1",
        status="healthy",
        clients_count=17,
        checked_at=now_ts - 600,
    )
    assert xui_api.get_cached_host_clients_count("h1", max_age_seconds=60) is None


def test_host_health_cache_prunes_to_max_entries(monkeypatch):
    from shop_bot.modules import xui_api

    with xui_api._host_health_cache_lock:
        xui_api._host_health_cache.clear()

    monkeypatch.setattr(xui_api, "HOST_HEALTH_CACHE_MAX_ENTRIES", 3)
    monkeypatch.setattr(xui_api, "HOST_HEALTH_CACHE_GC_MAX_AGE_SECONDS", 10_000)
    monkeypatch.setattr(xui_api.time, "time", lambda: 3_000.0)

    now_ts = 3_000.0
    for idx in range(5):
        xui_api.update_host_health_snapshot(
            f"h{idx}",
            status="healthy",
            clients_count=idx,
            checked_at=now_ts + idx,
        )

    with xui_api._host_health_cache_lock:
        keys = set(xui_api._host_health_cache.keys())
    assert keys == {"h2", "h3", "h4"}


def test_host_health_cache_prunes_stale_entries(monkeypatch):
    from shop_bot.modules import xui_api

    with xui_api._host_health_cache_lock:
        xui_api._host_health_cache.clear()

    monkeypatch.setattr(xui_api, "HOST_HEALTH_CACHE_MAX_ENTRIES", 20)
    monkeypatch.setattr(xui_api, "HOST_HEALTH_CACHE_GC_MAX_AGE_SECONDS", 120)

    now_ref = {"value": 4_000.0}
    monkeypatch.setattr(xui_api.time, "time", lambda: now_ref["value"])

    xui_api.update_host_health_snapshot(
        "stale-host",
        status="healthy",
        clients_count=1,
        checked_at=3_700.0,
    )
    xui_api.update_host_health_snapshot(
        "fresh-host",
        status="healthy",
        clients_count=2,
        checked_at=3_990.0,
    )

    with xui_api._host_health_cache_lock:
        keys = set(xui_api._host_health_cache.keys())
    assert "stale-host" not in keys
    assert "fresh-host" in keys


def test_build_host_specific_email_adds_host_suffix():
    from shop_bot.modules import xui_api

    email = xui_api.build_host_specific_email("user1-key1@vpn.bot", "SUPER 2")

    assert email == "user1-key1-super-2@vpn.bot"


def test_standard_subscription_uses_main_panel_issue_path(monkeypatch):
    from shop_bot.modules import xui_api

    calls: list[dict] = []

    async def _fake_issue(host_name, email, days_to_add, **kwargs):
        calls.append(
            {
                "host_name": host_name,
                "email": email,
                "days_to_add": days_to_add,
                "months": kwargs.get("months"),
                "target_sub_id": kwargs.get("target_sub_id"),
                "total_gb_bytes": kwargs.get("total_gb_bytes"),
                "include_white_inbounds": kwargs.get("include_white_inbounds"),
            }
        )
        return {
            "client_uuid": f"uuid-{host_name}",
            "email": email,
            "expiry_timestamp_ms": kwargs.get("target_expiry_ms"),
            "connection_string": f"vless://{host_name}",
            "subscription_link": f"https://sub.example/{host_name}",
            "host_name": host_name,
            "sub_id": "shared-sub",
        }

    monkeypatch.setattr(xui_api, "create_or_update_key_on_host", _fake_issue)

    result = asyncio.run(
        xui_api.create_or_update_standard_subscription_on_hosts(
            "standard-1",
            "user1-key1@vpn.bot",
            30,
            2,
            target_expiry_ms=1_800_000_000_000,
        )
    )

    assert result is not None
    assert result["host_name"] == "standard-1"
    assert result["sub_id"] == "shared-sub"
    assert result["months"] == 2
    assert calls == [
        {
            "host_name": "standard-1",
            "email": "user1-key1@vpn.bot",
            "days_to_add": 30,
            "months": 2,
            "target_sub_id": None,
            "total_gb_bytes": None,
            "include_white_inbounds": True,
        }
    ]


def test_issue90_subscription_inbounds_prefer_options_and_filter_modes(monkeypatch):
    from shop_bot.modules import xui_api

    endpoints: list[tuple[str, bool]] = []

    def _fake_api_get_json(_api, endpoint: str, *, skip_check: bool = False):
        endpoints.append((endpoint, skip_check))
        if endpoint == "panel/api/inbounds/options":
            return _issue90_options_payload()
        if endpoint.startswith("panel/api/inbounds/get/"):
            inbound_id = int(endpoint.rsplit("/", 1)[1])
            return {"obj": next(item for item in _issue90_options_payload()["obj"] if item["id"] == inbound_id)}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    class _InboundApi:
        @staticmethod
        def get_list():
            raise AssertionError("get_list fallback should not be used when options returns inbounds")

    fake_api = SimpleNamespace(inbound=_InboundApi())
    primary = _issue90_primary_inbound()
    monkeypatch.setattr(xui_api, "_api_get_json", _fake_api_get_json)

    all_inbounds = xui_api._subscription_inbounds(
        fake_api,
        primary,
        include_white_inbounds=True,
    )
    standard_inbounds = xui_api._subscription_inbounds(
        fake_api,
        primary,
        include_white_inbounds=False,
    )

    assert [item.id for item in all_inbounds] == [1, 2, 4]
    assert [item.id for item in standard_inbounds] == [2, 4]
    assert endpoints.count(("panel/api/inbounds/options", True)) == 2
    # Options enumerate candidates; live details supply the transport metadata.
    for inbound_id in (1, 2, 4, 5, 6):
        assert endpoints.count((f"panel/api/inbounds/get/{inbound_id}", True)) == 2


def test_issue90_list_inbounds_raw_falls_back_to_list_slim(monkeypatch):
    from shop_bot.modules import xui_api

    endpoints: list[tuple[str, bool]] = []

    def _fake_api_get_json(_api, endpoint: str, *, skip_check: bool = False):
        endpoints.append((endpoint, skip_check))
        if endpoint == "panel/api/inbounds/list/slim":
            return {"obj": [{"id": 7, "remark": "GERMANY", "protocol": "vless", "enable": True}]}
        return {"obj": []}

    monkeypatch.setattr(xui_api, "_api_get_json", _fake_api_get_json)

    assert xui_api._list_inbounds_raw(SimpleNamespace()) == [
        {"id": 7, "remark": "GERMANY", "protocol": "vless", "enable": True}
    ]
    assert endpoints == [
        ("panel/api/inbounds/options", True),
        ("panel/api/inbounds/list", False),
        ("panel/api/inbounds/list/slim", False),
    ]


def test_host_issue_preserves_public_identity_with_distinct_component_bindings(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.setattr(xui_api.time, "time", lambda: 1_790_000_000)
    primary = SimpleNamespace(id=1, remark="main", tag="", protocol="vless", enable=True, port=443)
    white2 = SimpleNamespace(id=2, remark="white2", tag="", protocol="vless", enable=True, port=8443)
    white3 = SimpleNamespace(id=3, remark="Vilnius", tag="node-vilnius", protocol="vless", enable=True, port=9443)
    disabled = SimpleNamespace(id=4, remark="disabled", tag="", protocol="vless", enable=False, port=10443)
    trojan = SimpleNamespace(id=5, remark="trojan", tag="", protocol="trojan", enable=True, port=11443)
    verified_sub_ids: list[str] = []

    fake_api = SimpleNamespace(inbound=SimpleNamespace(get_list=lambda: [primary, white2, white3, disabled, trojan]))
    identity_reads = _attach_raw_inbound_snapshots(
        fake_api, [primary, white2, white3, disabled, trojan], clients_by_id={1: [_identity_fixture()]},
    )
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary))
    monkeypatch.setattr(
        xui_api,
        "get_setting",
        lambda key: "StandartVLESS" if key == "super_key_host_name" else None,
    )
    monkeypatch.setattr(
        xui_api,
        "get_all_hosts",
        lambda: [
            {"host_name": "WHITE3", "is_white_host": 1, "node_inbound_id": 3},
            {"host_name": "REGULAR2", "is_white_host": 0, "node_inbound_id": 2},
        ],
    )
    monkeypatch.setattr(
        xui_api,
        "_get_client_sub_id_from_panel",
        lambda _api, _inbound_id, **_kwargs: "shared-sub",
    )
    monkeypatch.setattr(xui_api, "get_connection_string", lambda inbound, client_uuid, _host, remark, **_kwargs:
        f"vless://{client_uuid}@main.example:{inbound.port}?type=tcp#{remark}")

    def _verify_links(_api, sub_id: str):
        verified_sub_ids.append(sub_id)
        return [
            "vless://uuid@main.example:443?type=tcp",
            "vless://uuid@main.example:8443?type=tcp",
            "vless://uuid@main.example:9443?type=tcp",
        ]

    monkeypatch.setattr(xui_api, "_get_subscription_links", _verify_links)

    calls: list[dict] = []

    def _fake_update(_api, inbound_id, email, _days_to_add, **kwargs):
        calls.append({"inbound_id": inbound_id, "email": email, **kwargs})
        return _api.inbound._fixture_upsert_client(inbound_id, email, kwargs)

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _fake_update)

    result = xui_api._create_or_update_key_on_host_once(
        host_name="StandartVLESS",
        host_data={
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
            "host_subscription_base_url": "https://sub.example/base/",
        },
        email="user10001-key1@vpn.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
        months=3,
    )

    assert result is not None
    assert identity_reads[:3] == [1, 2, 3]
    assert verified_sub_ids == ["shared-sub"]
    assert result["subscription_link"] == "https://sub.example/base/shared-sub"
    assert result["subscription_links"] == [
        "vless://shared-uuid@main.example:443?type=tcp#main",
        "vless://shared-uuid@main.example:8443?type=tcp#white2",
        "vless://shared-uuid@main.example:9443?type=tcp#Vilnius",
    ]
    assert [call["inbound_id"] for call in calls] == [1, 2, 3]
    assert [call["email"] for call in calls] == [
        "user10001-key1@vpn.bot",
        "user10001-key1-white2-2@vpn.bot",
        "user10001-key1-vilnius-3@vpn.bot",
    ]
    assert calls[0]["total_gb_bytes"] == 0
    assert calls[1]["target_client_uuid"] == "shared-uuid"
    assert result["sub_id"] == "shared-sub"
    assert result["secondary_inbounds"][0]["sub_id"] == calls[1]["target_sub_id"]
    assert calls[1]["target_sub_id"] != "shared-sub"
    assert calls[1]["target_expiry_ms"] == 1_792_592_000_000
    assert calls[1]["traffic_reset_days"] == 30
    assert calls[1]["total_gb_bytes"] == 40 * 1024 * 1024 * 1024
    assert calls[2]["target_client_uuid"] == "shared-uuid"
    assert result["secondary_inbounds"][1]["sub_id"] == calls[2]["target_sub_id"]
    assert len({"shared-sub", calls[1]["target_sub_id"], calls[2]["target_sub_id"]}) == 3
    assert calls[2]["target_expiry_ms"] == 1_792_592_000_000
    assert calls[2]["traffic_reset_days"] == 30
    assert calls[2]["total_gb_bytes"] == 40 * 1024 * 1024 * 1024


def test_white_node_inbound_ids_skip_disabled_delivery_hosts(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.setattr(
        xui_api,
        "get_setting",
        lambda key: "SUPER2" if key == "super_key_host_name" else None,
    )
    monkeypatch.setattr(
        xui_api,
        "get_all_hosts",
        lambda: [
            {
                "host_name": "WHITE_ACTIVE",
                "is_white_host": 1,
                "node_inbound_id": 3,
                "delivery_state": "active",
            },
            {
                "host_name": "WHITE_DRAFT",
                "is_white_host": 1,
                "node_inbound_id": 4,
                "delivery_state": "draft",
            },
            {
                "host_name": "WHITE_DISABLED",
                "is_white_host": 1,
                "node_inbound_id": 5,
                "delivery_state": "disabled",
            },
        ],
    )

    assert xui_api._white_node_inbound_ids_for_panel("SUPER2") == {3}


def test_explicit_ordinary_only_issue_requires_repair_for_excluded_whitelist_bindings(monkeypatch):
    from shop_bot.modules import xui_api

    primary = SimpleNamespace(id=1, remark="main", tag="", protocol="vless", enable=True, port=443)
    regular = SimpleNamespace(id=2, remark="europe", tag="", protocol="vless", enable=True, port=8443)
    white = SimpleNamespace(id=3, remark="WHITE2", tag="", protocol="vless", enable=True, port=9443)
    cdn_white = SimpleNamespace(
        id=4,
        remark="cdn",
        tag="",
        protocol="vless",
        enable=True,
        port=18081,
        stream_settings={
            "network": "xhttp",
            "externalProxy": [{"dest": "cdn.example", "port": 443}],
        },
    )
    wrong_client = SimpleNamespace(id="shared-uuid", email="stale@vpn.bot", sub_id="shared-sub")
    inbound_by_id = {
        3: SimpleNamespace(settings=SimpleNamespace(clients=[wrong_client])),
        4: SimpleNamespace(settings=SimpleNamespace(clients=[wrong_client])),
    }
    deleted: list[tuple[int, str]] = []
    detached = []

    def _detach(_api, email, inbound_id):
        detached.append((inbound_id, email))
        return {"success": True}

    monkeypatch.setattr(xui_api, "_api_post_client_detach", _detach)

    def _fake_delete(inbound_id: int, uuid: str) -> None:
        deleted.append((inbound_id, uuid))

    def _fake_get_list() -> list[SimpleNamespace]:
        return [primary, regular, white, cdn_white]

    def _fake_get_by_id(inbound_id: int) -> SimpleNamespace:
        return inbound_by_id[inbound_id]

    fake_api = SimpleNamespace(
        inbound=SimpleNamespace(
            get_list=_fake_get_list,
            get_by_id=_fake_get_by_id,
        ),
        client=SimpleNamespace(delete=_fake_delete),
    )
    _attach_raw_inbound_snapshots(
        fake_api, [primary, regular, white, cdn_white], clients_by_id={
            1: [_identity_fixture()],
            3: [_identity_fixture(email="stale@vpn.bot")],
            4: [_identity_fixture(email="stale@vpn.bot")],
        },
    )
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary))

    def _fake_get_setting(key: str) -> str | None:
        return {
            "standard_key_host_name": "standard-main",
            "super_key_host_name": "SUPER2",
        }.get(key)

    monkeypatch.setattr(
        xui_api,
        "get_setting",
        _fake_get_setting,
    )
    monkeypatch.setattr(
        xui_api,
        "get_all_hosts",
        lambda: [
            {"host_name": "europe", "is_white_host": 0, "node_inbound_id": 2},
            {"host_name": "WHITE2", "is_white_host": 1, "node_inbound_id": 3},
            {"host_name": "WHITE-CDN", "is_white_host": 1, "node_inbound_id": 4},
        ],
    )
    monkeypatch.setattr(
        xui_api,
        "_get_client_sub_id_from_panel",
        lambda _api, _inbound_id, **_kwargs: "shared-sub",
    )
    monkeypatch.setattr(xui_api, "get_connection_string", lambda *_args, **_kwargs: "vless://main")

    expected_counts: list[int] = []

    def _verify_links(_api, _sub_id: str, *, expected_count: int | None = None, **_kwargs):
        expected_counts.append(int(expected_count or 0))
        return ["vless://uuid@main.example:443?type=tcp#main",
                "vless://uuid@main.example:8443?type=tcp#europe"]

    monkeypatch.setattr(xui_api, "_get_subscription_links_with_retry", _verify_links)

    calls: list[dict] = []

    def _fake_update(_api, inbound_id, email, _days_to_add, **kwargs):
        calls.append({"inbound_id": inbound_id, "email": email, **kwargs})
        return "shared-uuid", 1_800_000_000_000

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _fake_update)

    failure_context = {}
    result = xui_api._create_or_update_key_on_host_once(
        host_name="standard-main",
        host_data={
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
            "host_subscription_base_url": "https://sub.example/base/",
        },
        email="user10001-key1@vpn.bot",
        days_to_add=30,
        months=1,
        include_white_inbounds=False,
        failure_context=failure_context,
    )

    assert result is None
    assert failure_context["reason"] == "subscription_identity_repair_required"
    assert calls == []
    assert deleted == []
    assert detached == []


def test_issue90_super_issue_uses_all_options_inbounds_and_limits_only_whitelist(monkeypatch):
    from shop_bot.modules import xui_api

    primary = _issue90_primary_inbound()
    fake_api = SimpleNamespace(inbound=SimpleNamespace(get_list=list))
    _attach_raw_inbound_snapshots(
        fake_api, _issue90_options_payload()["obj"], clients_by_id={1: [_identity_fixture()]},
    )
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary))
    monkeypatch.setattr(xui_api, "get_setting", lambda key: "SUPER2" if key == "super_key_host_name" else None)
    monkeypatch.setattr(xui_api, "get_all_hosts", list)
    monkeypatch.setattr(
        xui_api,
        "_get_client_sub_id_from_panel",
        lambda _api, _inbound_id, **_kwargs: "shared-sub",
    )
    monkeypatch.setattr(xui_api, "get_connection_string", lambda inbound, client_uuid, _host, remark, **_kwargs:
        f"vless://{client_uuid}@main.example:{inbound.port}?type=tcp#{remark}")
    monkeypatch.setattr(
        xui_api,
        "_get_subscription_links_with_retry",
        lambda _api, _sub_id, *, expected_count=None, **_kwargs: [
            "vless://uuid@main.example:8443?type=tcp#finland",
            "vless://uuid@main.example:443?type=tcp#whitelist",
            "vless://uuid@main.example:9443?type=tcp#germany",
        ],
    )

    calls: list[dict] = []

    def _fake_update(_api, inbound_id, email, _days_to_add, **kwargs):
        calls.append({"inbound_id": inbound_id, "email": email, **kwargs})
        return _api.inbound._fixture_upsert_client(inbound_id, email, kwargs)

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _fake_update)

    result = xui_api._create_or_update_key_on_host_once(
        host_name="SUPER2",
        host_data={
            "host_name": "SUPER2",
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
        },
        email="user10001-key1@vpn.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
        months=2,
        include_white_inbounds=True,
        require_white_inbound=True,
    )

    assert result is not None
    assert [call["inbound_id"] for call in calls] == [1, 2, 4]
    assert [item["inbound_id"] for item in result["secondary_inbounds"]] == [2, 4]
    assert calls[0]["total_gb_bytes"] == 40 * 1024 ** 3
    assert calls[1]["total_gb_bytes"] == 0
    assert calls[1]["target_client_uuid"] == "shared-uuid"
    assert result["sub_id"] == "shared-sub"
    assert result["secondary_inbounds"][0]["sub_id"] == calls[1]["target_sub_id"]
    assert result["secondary_inbounds"][1]["sub_id"] == calls[2]["target_sub_id"]
    assert len({"shared-sub", calls[1]["target_sub_id"], calls[2]["target_sub_id"]}) == 3
    assert calls[2]["total_gb_bytes"] == 0


def test_issue90_issue_refuses_incomplete_subscription_payload(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.setattr(xui_api.time, "sleep", lambda _seconds: None)
    failure_context = {}
    primary = _issue90_primary_inbound()
    fake_api = SimpleNamespace(inbound=SimpleNamespace(get_list=list))
    _attach_raw_inbound_snapshots(
        fake_api, _issue90_options_payload()["obj"], clients_by_id={1: [_identity_fixture()]},
    )
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary))
    monkeypatch.setattr(xui_api, "get_setting", lambda key: "SUPER2" if key == "super_key_host_name" else None)
    monkeypatch.setattr(xui_api, "get_all_hosts", list)
    monkeypatch.setattr(
        xui_api,
        "_get_client_sub_id_from_panel",
        lambda _api, _inbound_id, **_kwargs: "shared-sub",
    )
    monkeypatch.setattr(xui_api, "get_connection_string", lambda *_args, **_kwargs: "vless://finland")
    monkeypatch.setattr(xui_api, "_get_subscription_links_with_retry", lambda *_args, **_kwargs: [])

    def _fake_update(_api, inbound_id, _email, _days_to_add, **_kwargs):
        return "shared-uuid", 1_800_000_000_000

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _fake_update)

    result = xui_api._create_or_update_key_on_host_once(
        host_name="SUPER2",
        host_data={
            "host_name": "SUPER2",
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
        },
        email="user10001-key1@vpn.bot",
        days_to_add=30,
        months=1,
        include_white_inbounds=True,
        require_white_inbound=True,
        failure_context=failure_context,
    )

    assert result is None
    assert failure_context["reason"] == "subscription_payload_incomplete"


def test_explicit_ordinary_only_issue_requires_repair_for_excluded_options_binding(monkeypatch):
    from shop_bot.modules import xui_api

    primary = _issue90_primary_inbound()
    stale_client = SimpleNamespace(id="shared-uuid", email="stale@vpn.bot", sub_id="shared-sub")
    deleted: list[tuple[int, str]] = []
    detached = []

    def _detach(_api, email, inbound_id):
        detached.append((inbound_id, email))
        return {"success": True}

    monkeypatch.setattr(xui_api, "_api_post_client_detach", _detach)

    class _InboundApi:
        @staticmethod
        def get_list():
            return []

        @staticmethod
        def get_by_id(inbound_id: int):
            option = next(item for item in _issue90_options_payload()["obj"] if item["id"] == inbound_id)
            return SimpleNamespace(**option, settings=SimpleNamespace(clients=[stale_client] if inbound_id == 1 else []))

    fake_api = SimpleNamespace(
        inbound=_InboundApi(),
        client=SimpleNamespace(delete=lambda inbound_id, uuid: deleted.append((inbound_id, uuid))),
    )
    _attach_raw_inbound_snapshots(
        fake_api, _issue90_options_payload()["obj"], clients_by_id={
            1: [_identity_fixture(email="stale@vpn.bot")],
            2: [_identity_fixture()],
        },
    )
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary))

    def _fake_get_setting(key: str) -> str | None:
        return {"standard_key_host_name": "standard-main", "super_key_host_name": "SUPER2"}.get(key)

    monkeypatch.setattr(xui_api, "get_setting", _fake_get_setting)
    monkeypatch.setattr(xui_api, "get_all_hosts", list)
    monkeypatch.setattr(
        xui_api,
        "_get_client_sub_id_from_panel",
        lambda _api, _inbound_id, **_kwargs: "shared-sub",
    )
    monkeypatch.setattr(xui_api, "get_connection_string", lambda *_args, **_kwargs: "vless://finland")
    monkeypatch.setattr(
        xui_api,
        "_get_subscription_links_with_retry",
        lambda _api, _sub_id, *, expected_count=None, **_kwargs: [
            "vless://uuid@main.example:8443?type=tcp#finland",
            "vless://uuid@main.example:9443?type=tcp#germany",
        ],
    )

    calls: list[dict] = []

    def _fake_update(_api, inbound_id, email, _days_to_add, **kwargs):
        calls.append({"inbound_id": inbound_id, "email": email, **kwargs})
        return "shared-uuid", 1_800_000_000_000

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _fake_update)

    failure_context = {}
    result = xui_api._create_or_update_key_on_host_once(
        host_name="standard-main",
        host_data={
            "host_name": "standard-main",
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
        },
        email="user10001-key1@vpn.bot",
        days_to_add=30,
        months=1,
        include_white_inbounds=False,
        failure_context=failure_context,
    )

    assert result is None
    assert failure_context["reason"] == "subscription_identity_repair_required"
    assert calls == []
    assert deleted == []
    assert detached == []


def test_issue90_whitelist_secondary_failure_fails_closed(monkeypatch):
    from shop_bot.modules import xui_api

    primary = _issue90_primary_inbound()
    fake_api = SimpleNamespace(inbound=SimpleNamespace(get_list=list))
    _attach_raw_inbound_snapshots(
        fake_api, _issue90_options_payload()["obj"], clients_by_id={1: [_identity_fixture()]},
    )
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary))
    monkeypatch.setattr(xui_api, "get_setting", lambda key: "SUPER2" if key == "super_key_host_name" else None)
    monkeypatch.setattr(xui_api, "get_all_hosts", list)
    monkeypatch.setattr(
        xui_api,
        "_get_client_sub_id_from_panel",
        lambda _api, _inbound_id, **_kwargs: "shared-sub",
    )
    monkeypatch.setattr(xui_api, "get_connection_string", lambda *_args, **_kwargs: "vless://finland")

    def _fake_update(_api, inbound_id, _email, _days_to_add, **_kwargs):
        if inbound_id == 1:
            return None, None
        return "shared-uuid", 1_800_000_000_000

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _fake_update)

    result = xui_api._create_or_update_key_on_host_once(
        host_name="SUPER2",
        host_data={
            "host_name": "SUPER2",
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
        },
        email="user10001-key1@vpn.bot",
        days_to_add=30,
        months=1,
        include_white_inbounds=True,
        require_white_inbound=True,
    )

    assert result is None


def test_cleanup_standard_key_white_clients_on_host_preserves_paid_whitelist_entitlement(monkeypatch):
    from shop_bot.modules import xui_api

    primary = SimpleNamespace(id=1, remark="main", tag="", protocol="vless", enable=True, port=443)
    regular = SimpleNamespace(id=2, remark="europe", tag="", protocol="vless", enable=True, port=8443)
    white = SimpleNamespace(id=3, remark="WHITE2", tag="", protocol="vless", enable=True, port=9443)
    cdn_white = SimpleNamespace(
        id=4,
        remark="cdn",
        tag="",
        protocol="vless",
        enable=True,
        port=18081,
        stream_settings={
            "network": "xhttp",
            "externalProxy": [{"dest": "cdn.example", "port": 443}],
        },
    )
    matching_client = SimpleNamespace(id="shared-uuid", email="old-white@vpn.bot", sub_id="shared-sub")
    other_client = SimpleNamespace(id="other-uuid", email="other@vpn.bot", sub_id="other-sub")
    inbound_by_id = {
        3: SimpleNamespace(settings=SimpleNamespace(clients=[matching_client, other_client])),
        4: SimpleNamespace(settings=SimpleNamespace(clients=[matching_client])),
    }
    deleted: list[tuple[int, str]] = []

    def _fake_delete(inbound_id: int, uuid: str) -> None:
        deleted.append((inbound_id, uuid))

    fake_api = SimpleNamespace(
        inbound=SimpleNamespace(
            get_list=lambda: [primary, regular, white, cdn_white],
            get_by_id=lambda inbound_id: inbound_by_id[inbound_id],
        ),
        client=SimpleNamespace(delete=_fake_delete),
    )

    def _fake_get_setting(key: str) -> str | None:
        return {
            "standard_key_host_name": "standard-main",
            "super_key_host_name": "SUPER2",
        }.get(key)

    def _fake_get_all_hosts() -> list[dict]:
        return [{"host_name": "WHITE2", "is_white_host": 1, "node_inbound_id": 3}]

    def _fake_login_to_host(**_kwargs):
        return fake_api, primary

    monkeypatch.setattr(xui_api, "login_to_host", _fake_login_to_host)
    monkeypatch.setattr(xui_api, "get_setting", _fake_get_setting)
    monkeypatch.setattr(xui_api, "get_all_hosts", _fake_get_all_hosts)
    key = {
        "xui_client_uuid": "shared-uuid",
        "panel_sub_id": "shared-sub",
        "key_email": "user10001-key1@vpn.bot",
    }

    dry_run = xui_api.cleanup_standard_key_white_clients_on_host(
        host_name="standard-main",
        host_data={
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
        },
        keys=[key],
        execute=False,
    )

    assert dry_run["matched_clients"] == 0
    assert dry_run["reason"] == "standard_whitelist_entitled"
    assert dry_run["deleted_clients"] == 0
    assert deleted == []

    executed = xui_api.cleanup_standard_key_white_clients_on_host(
        host_name="standard-main",
        host_data={
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
        },
        keys=[key],
        execute=True,
    )

    assert executed["matched_clients"] == 0
    assert executed["deleted_clients"] == 0
    assert executed["reason"] == "standard_whitelist_entitled"
    assert deleted == []


def test_host_issue_refuses_partial_access_when_secondary_node_fails(monkeypatch):
    from shop_bot.modules import xui_api

    primary = SimpleNamespace(id=1, remark="main", tag="", protocol="vless", enable=True)
    failed_node = SimpleNamespace(id=2, remark="white2", tag="", protocol="vless", enable=True)
    fake_api = SimpleNamespace(inbound=SimpleNamespace(get_list=lambda: [primary, failed_node]))
    _attach_raw_inbound_snapshots(
        fake_api, [primary, failed_node], clients_by_id={1: [_identity_fixture()]},
    )
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary))
    monkeypatch.setattr(
        xui_api,
        "_get_client_sub_id_from_panel",
        lambda _api, _inbound_id, **_kwargs: "shared-sub",
    )
    monkeypatch.setattr(xui_api, "get_connection_string", lambda *_args, **_kwargs: "vless://main")

    expected_counts: list[int] = []

    def _verify_links(_api, sub_id: str, *, expected_count: int | None = None, **_kwargs):
        expected_counts.append(int(expected_count or 0))
        return ["vless://main", "vless://vilnius"]

    monkeypatch.setattr(xui_api, "_get_subscription_links_with_retry", _verify_links)

    def _fake_update(_api, inbound_id, email, _days_to_add, **_kwargs):
        if inbound_id == 2:
            return None, None
        return "shared-uuid", 1_800_000_000_000

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _fake_update)

    result = xui_api._create_or_update_key_on_host_once(
        host_name="StandartVLESS",
        host_data={
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
            "host_subscription_base_url": "https://sub.example/base/",
        },
        email="user10001-key1@vpn.bot",
        days_to_add=30,
        months=3,
    )

    assert result is None
    assert expected_counts == []


def test_host_issue_refuses_partial_subscription_when_another_node_succeeds(monkeypatch):
    from shop_bot.modules import xui_api

    failure_context = {}
    primary = SimpleNamespace(id=1, remark="main", tag="", protocol="vless", enable=True)
    stale_node = SimpleNamespace(id=2, remark="old-broken", tag="", protocol="vless", enable=True)
    configured_node = SimpleNamespace(id=3, remark="europe-one", tag="", protocol="vless", enable=True)
    fake_api = SimpleNamespace(inbound=SimpleNamespace(get_list=lambda: [primary, stale_node, configured_node]))
    _attach_raw_inbound_snapshots(
        fake_api, [primary, stale_node, configured_node], clients_by_id={1: [_identity_fixture()]},
    )
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary))
    monkeypatch.setattr(
        xui_api,
        "_get_client_sub_id_from_panel",
        lambda _api, _inbound_id, **_kwargs: "shared-sub",
    )
    monkeypatch.setattr(xui_api, "get_connection_string", lambda *_args, **_kwargs: "vless://main")

    updated_inbound_ids: list[int] = []

    def _fake_update(_api, inbound_id, _email, _days_to_add, **_kwargs):
        updated_inbound_ids.append(inbound_id)
        if inbound_id == 2:
            return None, None
        return "shared-uuid", 1_800_000_000_000

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _fake_update)
    monkeypatch.setattr(
        xui_api,
        "_get_subscription_links_with_retry",
        lambda *_args, **_kwargs: ["vless://main", "vless://europe"],
    )

    result = xui_api._create_or_update_key_on_host_once(
        host_name="SUPER2",
        host_data={
            "host_name": "SUPER2",
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
            "host_subscription_base_url": "https://sub.example/base/",
        },
        email="user10001-key1@vpn.bot",
        days_to_add=30,
        months=3,
        failure_context=failure_context,
    )

    assert result is None
    assert updated_inbound_ids == [1, 2, 3]
    assert failure_context["reason"] == "secondary_update_failed"


def test_host_issue_uses_selected_host_subscription_base_url(monkeypatch):
    from shop_bot.modules import xui_api

    primary = SimpleNamespace(id=1, remark="main", tag="", protocol="vless", enable=True, port=443)
    fake_api = SimpleNamespace(inbound=SimpleNamespace(get_list=lambda: [primary]))
    monkeypatch.setenv("PAYPEAR_PUBLIC_BASE_URL", "https://pepelbot.ru")
    _attach_raw_inbound_snapshots(fake_api, [primary], clients_by_id={1: [_identity_fixture()]})
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary))
    monkeypatch.setattr(
        xui_api,
        "_get_client_sub_id_from_panel",
        lambda _api, _inbound_id, **_kwargs: "shared-sub",
    )
    monkeypatch.setattr(xui_api, "get_connection_string", lambda *_args, **_kwargs: "vless://uuid@main.example:443?type=tcp#main")
    monkeypatch.setattr(xui_api, "_get_subscription_links", lambda _api, _sub_id: ["vless://uuid@main.example:443?type=tcp#main"])
    monkeypatch.setattr(
        xui_api,
        "update_or_create_client_on_panel",
        lambda *_args, **_kwargs: ("shared-uuid", 1_800_000_000_000),
    )

    result = xui_api._create_or_update_key_on_host_once(
        host_name="StandartVLESS",
        host_data={
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
            "host_subscription_base_url": "https://panel.example/sub/",
        },
        email="user10001-key1@vpn.bot",
        days_to_add=30,
        months=1,
    )

    assert result is not None
    assert result["subscription_link"] == "https://panel.example/sub/shared-sub"


def test_subscription_base_url_ignores_public_shop_domain(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.setenv("PAYPEAR_PUBLIC_BASE_URL", "https://pepelbot.ru")
    monkeypatch.setenv("SHOP_PUBLIC_BASE_URL", "https://pepelbot.ru")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://pepelbot.ru")
    monkeypatch.setenv("MANAGED_SUBSCRIPTION_BASE_URL", "https://pepelbot.ru/podpiska")

    assert xui_api._resolve_subscription_base_url({"host_subscription_base_url": ""}) == ""
    assert (
        xui_api._resolve_subscription_base_url({"host_subscription_base_url": "https://panel.example/sub/"})
        == "https://panel.example/sub/"
    )
    assert (
        xui_api._resolve_subscription_base_url(
            {
                "host_name": "SUPER2",
                "host_subscription_base_url": "https://pepelbot.ru/podpiska",
            }
        )
        == ""
    )
    assert (
        xui_api._resolve_subscription_base_url(
            {
                "host_name": "SUPER2",
                "host_subscription_base_url": "https://pepelbot.com/podpiska",
            }
        )
        == ""
    )
    assert (
        xui_api._resolve_subscription_base_url(
            {
                "host_name": "SUPER2",
                "host_subscription_base_url": "https://panel.example/podpiska",
            }
        )
        == ""
    )


def test_multi_node_white_limit_supports_future_white_hosts():
    from shop_bot.modules import xui_api

    white4 = SimpleNamespace(id=4, remark="White4", tag="", protocol="vless", enable=True)
    whitelist = SimpleNamespace(id=6, remark="Whitelistonly2", tag="inbound-433", protocol="vless", enable=True)
    tagged_white = SimpleNamespace(id=7, remark="", tag="node-white3", protocol="vless", enable=True)
    regular = SimpleNamespace(id=5, remark="standard", tag="", protocol="vless", enable=True)

    assert xui_api._multi_node_total_gb_bytes(white4, months=2) == 40 * 1024 * 1024 * 1024
    assert xui_api._multi_node_total_gb_bytes(whitelist, months=2) == 40 * 1024 * 1024 * 1024
    assert xui_api._multi_node_total_gb_bytes(tagged_white, months=2) == 40 * 1024 * 1024 * 1024
    assert xui_api._multi_node_total_gb_bytes(regular, months=2) == 0


def test_update_or_create_client_on_panel_copies_source_client_config_to_new_client():
    from shop_bot.modules import xui_api

    source_client = SimpleNamespace(
        flow="xtls-rprx-vision",
        method="none",
        password="secret",
        reset=30,
        limit_ip=4,
        tg_id=123456,
        comment="primary comment",
    )
    inbound = SimpleNamespace(settings=SimpleNamespace(clients=[]))
    added_clients: list[object] = []

    class _FakeInboundApi:
        @staticmethod
        def get_by_id(_inbound_id: int):
            return inbound

        @staticmethod
        def update(_inbound_id: int, _inbound):
            raise AssertionError

    class _FakeClientApi:
        @staticmethod
        def add(_inbound_id: int, clients: list[object]):
            added_clients.extend(clients)

    api = SimpleNamespace(inbound=_FakeInboundApi(), client=_FakeClientApi())

    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=44,
        email="user1-white2@vpn.bot",
        days_to_add=0,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="sub-active",
        target_client_uuid="uuid-active",
        total_gb_bytes=xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES,
        source_client=source_client,
    )

    assert client_uuid == "uuid-active"
    assert expiry_ms == 1_800_000_000_000
    assert len(added_clients) == 1

    added_client = added_clients[0]
    assert added_client.flow == "xtls-rprx-vision"
    assert added_client.method == "none"
    assert added_client.password == "secret"
    assert added_client.reset == 30
    assert added_client.limit_ip == 4
    assert added_client.tg_id == 123456
    assert added_client.comment == "primary comment"
    assert added_client.sub_id == "sub-active"
    assert added_client.total_gb == xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES


def test_update_or_create_client_on_panel_copies_source_client_config_to_existing_client():
    from shop_bot.modules import xui_api

    source_client = SimpleNamespace(
        flow="xtls-rprx-vision",
        method="none",
        password="secret",
        reset=30,
        limit_ip=4,
        tg_id=123456,
        comment="primary comment",
    )
    existing_client = SimpleNamespace(
        email="user1-white2@vpn.bot",
        id="uuid-active",
        expiry_time=1,
        enable=False,
        inbound_id=44,
        flow="",
        method="",
        password="",
        reset=None,
        limit_ip=0,
        tg_id="",
        comment="",
        sub_id="",
        total_gb=0,
    )
    inbound = SimpleNamespace(settings=SimpleNamespace(clients=[existing_client]))
    updated_clients: list[object] = []

    class _FakeInboundApi:
        @staticmethod
        def get_by_id(_inbound_id: int):
            return inbound

        @staticmethod
        def update(_inbound_id: int, _inbound):
            raise AssertionError

    class _FakeClientApi:
        @staticmethod
        def update(update_uuid: str, client: object):
            assert update_uuid == "uuid-active"
            updated_clients.append(client)

    api = SimpleNamespace(inbound=_FakeInboundApi(), client=_FakeClientApi())

    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=44,
        email="user1-white2@vpn.bot",
        days_to_add=0,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="sub-active",
        target_client_uuid="uuid-active",
        total_gb_bytes=xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES,
        source_client=source_client,
    )

    assert client_uuid == "uuid-active"
    assert expiry_ms == 1_800_000_000_000
    assert updated_clients == [existing_client]
    assert existing_client.id == "uuid-active"
    assert existing_client.flow == "xtls-rprx-vision"
    assert existing_client.method == "none"
    assert existing_client.password == "secret"
    assert existing_client.reset == 30
    assert existing_client.limit_ip == 4
    assert existing_client.tg_id == 123456
    assert existing_client.comment == "primary comment"
    assert existing_client.sub_id == "sub-active"
    assert existing_client.total_gb == xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES


def test_update_or_create_client_on_panel_uses_clients_add_after_legacy_404():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(settings=SimpleNamespace(clients=[]))
    posts: list[tuple[str, dict]] = []

    def _not_found() -> RuntimeError:
        exc = RuntimeError("404")
        exc.response = SimpleNamespace(status_code=404)
        return exc

    class _FakeInboundApi:
        @staticmethod
        def get_by_id(_inbound_id: int):
            return inbound

        @staticmethod
        def _url(endpoint: str):
            return endpoint

        @staticmethod
        def _post(url: str, _headers: dict, data: dict):
            if "panel/api/inbounds/update" in url:
                raise AssertionError("full inbound fallback must not be used")
            posts.append((url, data))
            return SimpleNamespace(json=lambda: {"success": True})

    class _FakeClientApi:
        @staticmethod
        def add(_inbound_id: int, _clients: list[object]):
            raise _not_found()

    api = SimpleNamespace(inbound=_FakeInboundApi(), client=_FakeClientApi())

    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=44,
        email="user1-key1@vpn.bot",
        days_to_add=0,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="sub-active",
        target_client_uuid="uuid-active",
        total_gb_bytes=xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES,
    )

    assert client_uuid == "uuid-active"
    assert expiry_ms == 1_800_000_000_000
    assert posts == [
        (
            "panel/api/clients/add",
            {
                "client": {
                    "id": "uuid-active",
                    "email": "user1-key1@vpn.bot",
                    "enable": True,
                    "password": "",
                    "inboundId": 44,
                    "up": 0,
                    "down": 0,
                    "expiryTime": 1_800_000_000_000,
                    "total": 0,
                    "reset": 30,
                    "flow": "xtls-rprx-vision",
                    "method": "",
                    "limitIp": 0,
                    "subId": "sub-active",
                    "tgId": 0,
                    "totalGB": xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES,
                },
                "inboundIds": [44],
            },
        )
    ]


def test_update_or_create_client_on_panel_uses_clients_update_after_legacy_404():
    from shop_bot.modules import xui_api

    existing_client = SimpleNamespace(
        email="old-user@vpn.bot",
        id="uuid-active",
        expiry_time=1,
        enable=False,
        inbound_id=44,
        flow="",
        method="",
        password="",
        reset=None,
        limit_ip=0,
        tg_id="",
        comment="",
        sub_id="",
        total_gb=0,
    )
    inbound = SimpleNamespace(settings=SimpleNamespace(clients=[existing_client]))
    posts: list[tuple[str, dict]] = []

    def _not_found() -> RuntimeError:
        exc = RuntimeError("404")
        exc.response = SimpleNamespace(status_code=404)
        return exc

    class _FakeInboundApi:
        @staticmethod
        def get_by_id(_inbound_id: int):
            return inbound

        @staticmethod
        def _url(endpoint: str):
            return endpoint

        @staticmethod
        def _get(url: str, _headers: dict, **_kwargs):
            if url == "panel/api/inbounds/get/44":
                return SimpleNamespace(json=lambda: {"obj": {
                    "id": 44, "protocol": "vless", "port": 443,
                    "streamSettings": json.dumps({"network": "tcp", "security": "reality"}),
                    "settings": json.dumps({"clients": [{
                        "id": "uuid-active", "email": "old-user@vpn.bot",
                        "enable": False, "expiryTime": 1, "subId": "",
                        "flow": "xtls-rprx-vision",
                    }]}),
                }})
            assert url == "panel/api/clients/list"
            return SimpleNamespace(json=lambda: {"obj": [{
                "id": 99, "uuid": "uuid-active", "email": "old-user@vpn.bot",
                "inboundIds": [44, 45], "customField": "retained", "traffic": {"up": 7},
            }]})

        @staticmethod
        def _post(url: str, _headers: dict, data: dict):
            if "panel/api/inbounds/update" in url:
                raise AssertionError("full inbound fallback must not be used")
            posts.append((url, data))
            return SimpleNamespace(json=lambda: {"success": True})

    class _FakeClientApi:
        @staticmethod
        def update(_update_uuid: str, _client: object):
            raise _not_found()

    api = SimpleNamespace(inbound=_FakeInboundApi(), client=_FakeClientApi())

    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=44,
        email="old-user@vpn.bot",
        days_to_add=0,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="sub-active",
        target_client_uuid="uuid-active",
        total_gb_bytes=xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES,
    )

    assert client_uuid == "uuid-active"
    assert expiry_ms == 1_800_000_000_000
    assert posts == [
        (
            "panel/api/clients/update/old-user%40vpn.bot?inboundIds=44",
            {
                "id": "uuid-active",
                "email": "old-user@vpn.bot",
                "enable": True,
                "uuid": "uuid-active",
                "inboundIds": [44, 45],
                "customField": "retained",
                "expiryTime": 1_800_000_000_000,
                "totalGB": xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES,
                "subId": "sub-active",
                "limitIp": 0,
                "tgId": 0,
                "password": "",
                "flow": "xtls-rprx-vision",
                "method": "",
                "reset": 30,
            },
        )
    ]


def test_update_or_create_client_on_panel_clears_flow_for_xhttp_client():
    from shop_bot.modules import xui_api

    source_client = SimpleNamespace(flow="xtls-rprx-vision")
    inbound = SimpleNamespace(
        settings=SimpleNamespace(clients=[]),
        stream_settings=SimpleNamespace(network="xhttp", security="none"),
    )
    added_clients: list[object] = []

    class _FakeInboundApi:
        @staticmethod
        def get_by_id(_inbound_id: int):
            return inbound

    class _FakeClientApi:
        @staticmethod
        def add(_inbound_id: int, clients: list[object]):
            added_clients.extend(clients)

    api = SimpleNamespace(inbound=_FakeInboundApi(), client=_FakeClientApi())

    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=21,
        email="user1-xhttp@vpn.bot",
        days_to_add=0,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="sub-active",
        target_client_uuid="uuid-active",
        source_client=source_client,
    )

    assert client_uuid == "uuid-active"
    assert expiry_ms == 1_800_000_000_000
    assert len(added_clients) == 1
    assert added_clients[0].flow == ""
    assert added_clients[0].sub_id == "sub-active"


def test_update_or_create_client_on_panel_clears_existing_xhttp_flow():
    from shop_bot.modules import xui_api

    source_client = SimpleNamespace(flow="xtls-rprx-vision")
    existing_client = SimpleNamespace(
        email="user1-xhttp@vpn.bot",
        id="uuid-active",
        expiry_time=1,
        enable=False,
        inbound_id=21,
        flow="xtls-rprx-vision",
        sub_id="",
        total_gb=0,
    )
    inbound = SimpleNamespace(
        settings=SimpleNamespace(clients=[existing_client]),
        stream_settings=SimpleNamespace(network="xhttp", security="none"),
    )
    updated_clients: list[object] = []

    class _FakeInboundApi:
        @staticmethod
        def get_by_id(_inbound_id: int):
            return inbound

    class _FakeClientApi:
        @staticmethod
        def update(_update_uuid: str, client: object):
            updated_clients.append(client)

    api = SimpleNamespace(inbound=_FakeInboundApi(), client=_FakeClientApi())

    xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=21,
        email="user1-xhttp@vpn.bot",
        days_to_add=0,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="sub-active",
        target_client_uuid="uuid-active",
        source_client=source_client,
    )

    assert updated_clients == [existing_client]
    assert existing_client.id == "uuid-active"
    assert existing_client.flow == ""
    assert existing_client.sub_id == "sub-active"


def test_update_or_create_client_on_panel_refuses_xhttp_full_inbound_update_fallback():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(
        settings=SimpleNamespace(clients=[]),
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[{"dest": "oyiterhekepog.begetcdn.cloud", "port": 443}],
            xhttp_settings={
                "path": "/api/user",
                "mode": "packet-up",
                "extra": {"uplinkHTTPMethod": "GET"},
            },
        ),
    )
    original_xhttp_settings = dict(inbound.stream_settings.xhttp_settings)
    inbound_update_calls: list[int] = []

    class _FakeInboundApi:
        @staticmethod
        def get_by_id(_inbound_id: int):
            return inbound

        @staticmethod
        def update(inbound_id: int, _inbound):
            inbound_update_calls.append(inbound_id)

    class _FakeClientApi:
        @staticmethod
        def add(_inbound_id: int, _clients: list[object]):
            raise RuntimeError

    api = SimpleNamespace(inbound=_FakeInboundApi(), client=_FakeClientApi())

    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=21,
        email="user1-xhttp@vpn.bot",
        days_to_add=0,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="sub-active",
        target_client_uuid="uuid-active",
    )

    assert client_uuid is None
    assert expiry_ms is None
    assert inbound_update_calls == []
    assert inbound.stream_settings.xhttp_settings == original_xhttp_settings


def test_update_or_create_client_on_panel_refuses_xhttp_update_fallback_for_existing_client():
    from shop_bot.modules import xui_api

    existing_client = SimpleNamespace(
        email="user1-xhttp@vpn.bot",
        id="uuid-active",
        expiry_time=1,
        enable=False,
        inbound_id=21,
        flow="xtls-rprx-vision",
        sub_id="sub-active",
        total_gb=0,
    )
    inbound = SimpleNamespace(
        settings=SimpleNamespace(clients=[existing_client]),
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[{"dest": "oyiterhekepog.begetcdn.cloud", "port": 443}],
            xhttp_settings={"path": "/api/user", "mode": "packet-up"},
        ),
    )
    inbound_update_calls: list[int] = []

    class _FakeInboundApi:
        @staticmethod
        def get_by_id(_inbound_id: int):
            return inbound

        @staticmethod
        def update(inbound_id: int, _inbound):
            inbound_update_calls.append(inbound_id)

    class _FakeClientApi:
        @staticmethod
        def update(_update_uuid: str, _client: object):
            raise RuntimeError

    api = SimpleNamespace(inbound=_FakeInboundApi(), client=_FakeClientApi())

    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=21,
        email="user1-xhttp@vpn.bot",
        days_to_add=0,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="sub-active",
        target_client_uuid="uuid-active",
    )

    assert client_uuid is None
    assert expiry_ms is None
    assert inbound_update_calls == []
    assert existing_client.flow == ""


def test_xhttp_external_proxy_stream_settings_patch_repairs_defaults_for_maintenance():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[
                {
                    "dest": "oyiterhekepog.begetcdn.cloud",
                    "port": 443,
                    "alpn": "h3,h2",
                }
            ],
            xhttp_settings={},
        )
    )

    patch = xui_api._xhttp_external_proxy_stream_settings_patch(inbound)

    assert patch is not None
    patched_settings, changed = patch
    assert changed is True
    assert patched_settings["xhttp_settings"]["path"] == "/api/user"
    assert patched_settings["xhttp_settings"]["mode"] == "packet-up"
    assert patched_settings["xhttp_settings"]["extra"]["uplinkHTTPMethod"] == "GET"
    assert patched_settings["external_proxy"][0]["alpn"] == "h2"

    xui_api._apply_stream_settings_patch(inbound, patched_settings)

    assert inbound.stream_settings.xhttp_settings["path"] == "/api/user"
    assert inbound.stream_settings.external_proxy[0]["alpn"] == "h2"


def test_create_or_update_key_on_host_retries_with_backoff(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.setattr(
        xui_api,
        "get_host",
        lambda _host_name: {
            "host_name": "h1",
            "host_url": "https://h1.example/xui/",
            "host_username": "u",
            "host_pass": "p",
            "host_inbound_id": 1,
            "host_subscription_base_url": "",
        },
    )

    attempts: list[int] = []

    def _always_fail_once(**_kwargs):
        attempts.append(1)
        return None

    monkeypatch.setattr(xui_api, "_create_or_update_key_on_host_once", _always_fail_once)

    cleanup_calls = []

    async def _cleanup(**kwargs):
        cleanup_calls.append(kwargs)
        return 0

    monkeypatch.setattr(xui_api, "_cleanup_failed_subscription_attempt_on_host", _cleanup)
    probe_calls: list[int] = []

    def _probe(**_kwargs):
        probe_calls.append(1)
        return xui_api.HostLoginResult(
            api=None,
            inbound=None,
            error_code="host_timeout",
            error_message="timeout",
        )

    monkeypatch.setattr(xui_api, "login_to_host_detailed", _probe)

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float):
        sleep_calls.append(float(seconds))

    monkeypatch.setattr(xui_api.asyncio, "sleep", _fake_sleep)

    result = asyncio.run(
        xui_api.create_or_update_key_on_host(
            "h1",
            "u1@example.com",
            30,
            max_attempts=3,
            retry_backoff_seconds=(2, 4, 8),
        )
    )

    assert result is None
    assert len(attempts) == 3
    assert cleanup_calls == []
    assert len(probe_calls) == 2
    assert sleep_calls == [2.0, 4.0]


def test_create_or_update_key_on_host_uses_custom_panel_call_timeout(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.setattr(
        xui_api,
        "get_host",
        lambda _host_name: {
            "host_name": "h1",
            "host_url": "https://h1.example/xui/",
            "host_username": "u",
            "host_pass": "p",
            "host_inbound_id": 1,
            "host_subscription_base_url": "",
        },
    )

    captured_timeouts: list[float] = []

    async def _fake_run_blocking(_func, **kwargs):
        captured_timeouts.append(float(kwargs["timeout"]))
        return {
            "client_uuid": "uuid",
            "email": "u1@example.com",
            "expiry_timestamp_ms": 1_800_000_000_000,
            "connection_string": "vless://key",
            "host_name": "h1",
        }

    monkeypatch.setattr(xui_api, "_run_blocking_with_timeout", _fake_run_blocking)

    result = asyncio.run(
        xui_api.create_or_update_key_on_host(
            "h1",
            "u1@example.com",
            30,
            panel_call_timeout_seconds=60,
        )
    )

    assert result is not None
    assert captured_timeouts == [60.0]


def test_host_to_node_payload_uses_normalized_base_path():
    from shop_bot.modules import xui_api

    payload = xui_api._host_to_node_payload(
        "WHITE2",
        {"host_url": "https://192.0.2.11:1488/3xuivless"},
        api_token="node-secret",
    )

    assert payload["name"] == "WHITE2"
    assert payload["scheme"] == "https"
    assert payload["address"] == "192.0.2.11"
    assert payload["port"] == 1488
    assert payload["basePath"] == "/3xuivless/"
    assert payload["apiToken"] == "node-secret"
    assert payload["enable"] is True


def test_create_or_resolve_node_inbound_posts_node_id_and_unique_tag(monkeypatch):
    from shop_bot.modules import xui_api

    class _PrimaryInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 443,
                "protocol": "vless",
                "settings": '{"clients":[{"id":"a"}]}',
                "streamSettings": '{"network":"tcp"}',
                "sniffing": '{"enabled":true}',
            }

    call_state = {"count": 0}

    def _fake_list_inbounds(_api):
        call_state["count"] += 1
        if call_state["count"] == 1:
            return []
        return [
            {
                "id": 77,
                "nodeId": 55,
                "port": 443,
                "protocol": "vless",
                "tag": xui_api._build_node_inbound_tag("WHITE2", 443),
            }
        ]

    captured_payload: dict = {}

    def _fake_post_json(_api, endpoint: str, data: dict):
        captured_payload["endpoint"] = endpoint
        captured_payload["data"] = dict(data)
        return {"success": True}

    monkeypatch.setattr(xui_api, "_list_inbounds_raw", _fake_list_inbounds)
    monkeypatch.setattr(xui_api, "_api_post_json", _fake_post_json)

    inbound_id = xui_api._create_or_resolve_node_inbound(
        api=SimpleNamespace(),
        primary_inbound=_PrimaryInbound(),
        node_id=55,
        host_name="WHITE2",
    )

    assert inbound_id == 77
    assert captured_payload["endpoint"] == "panel/api/inbounds/add"
    assert captured_payload["data"]["nodeId"] == 55
    assert captured_payload["data"]["tag"] == "node_white2_inbound_443"


def test_create_or_resolve_node_inbound_uses_white_template_inbound(monkeypatch):
    from shop_bot.modules import xui_api

    class _PrimaryInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 443,
                "protocol": "vless",
                "settings": '{"clients":[{"id":"main"}]}',
                "streamSettings": '{"network":"tcp"}',
                "sniffing": '{"enabled":true}',
                "remark": "MAIN",
            }

    class _WhiteTemplateInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 10000,
                "protocol": "vless",
                "settings": '{"clients":[{"id":"white"}]}',
                "streamSettings": '{"network":"xhttp"}',
                "sniffing": '{"enabled":false}',
                "remark": "WHITE_LIST_ONLY",
            }

    call_state = {"count": 0}

    def _fake_list_inbounds(_api):
        call_state["count"] += 1
        if call_state["count"] == 1:
            return []
        return [
            {
                "id": 88,
                "nodeId": 55,
                "port": 10000,
                "protocol": "vless",
                "remark": "WHITE_LIST_ONLY",
                "tag": xui_api._build_node_inbound_tag("NGENIX2", 10000),
            }
        ]

    captured_payload: dict = {}

    def _fake_post_json(_api, endpoint: str, data: dict):
        captured_payload["endpoint"] = endpoint
        captured_payload["data"] = dict(data)
        return {"success": True}

    monkeypatch.setattr(xui_api, "_list_inbounds_raw", _fake_list_inbounds)
    monkeypatch.setattr(xui_api, "_api_post_json", _fake_post_json)

    inbound_id = xui_api._create_or_resolve_node_inbound(
        api=SimpleNamespace(),
        primary_inbound=_PrimaryInbound(),
        node_id=55,
        host_name="NGENIX2",
        template_inbound=_WhiteTemplateInbound(),
        preserve_template_remark=True,
    )

    assert inbound_id == 88
    assert captured_payload == {}


def test_create_or_resolve_node_inbound_imports_disabled_white_mirror(monkeypatch):
    from shop_bot.modules import xui_api

    class _PrimaryInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 443,
                "protocol": "vless",
                "settings": '{"clients":[{"id":"main"}]}',
                "streamSettings": '{"network":"tcp"}',
                "sniffing": "{}",
                "remark": "MAIN",
                "tag": "main-tag",
            }

    class _WhiteTemplateInbound:
        tag = "white-list-only-tag"

        def to_json(self):
            return {
                "enable": True,
                "port": 10000,
                "protocol": "vless",
                "settings": '{"clients":[{"id":"white"}]}',
                "streamSettings": '{"network":"ws","security":"tls"}',
                "sniffing": '{"enabled":false}',
                "remark": "WHITE_LIST_ONLY",
            }

    added_inbound: dict | None = None

    def _fake_list_inbounds(_api):
        return [added_inbound] if added_inbound else []

    def _fake_post_json(_api, endpoint: str, data: dict):
        nonlocal added_inbound
        assert endpoint == "panel/api/inbounds/add"
        assert data["enable"] is False
        assert data["settings"] == '{"clients": []}'
        assert data["streamSettings"] == '{"network":"ws","security":"tls"}'
        added_inbound = {
            "id": 101,
            "nodeId": data["nodeId"],
            "port": data["port"],
            "protocol": data["protocol"],
            "remark": data["remark"],
            "tag": data["tag"],
        }
        return {"success": True, "obj": {"id": 101}}

    monkeypatch.setattr(xui_api, "_list_inbounds_raw", _fake_list_inbounds)
    monkeypatch.setattr(xui_api, "_wait_for_node_inbound_import", lambda **_kwargs: None)
    monkeypatch.setattr(xui_api, "_api_post_json", _fake_post_json)

    inbound_id = xui_api._create_or_resolve_node_inbound(
        api=SimpleNamespace(),
        primary_inbound=_PrimaryInbound(),
        node_id=55,
        host_name="NGENIX2",
        template_inbound=_WhiteTemplateInbound(),
        preserve_template_remark=True,
    )

    assert inbound_id == 101
    assert added_inbound is not None
    assert added_inbound["tag"] == "white-list-only-tag"
    assert added_inbound["remark"] == "WHITE_LIST_ONLY"


def test_create_or_resolve_node_inbound_replaces_stale_white_mirror(monkeypatch):
    from shop_bot.modules import xui_api

    class _PrimaryInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 443,
                "protocol": "vless",
                "settings": '{"clients":[]}',
                "streamSettings": '{"network":"tcp"}',
                "sniffing": "{}",
                "remark": "MAIN",
            }

    class _WhiteTemplateInbound:
        tag = "remote-white-tag"

        def to_json(self):
            return {
                "enable": True,
                "port": 10000,
                "protocol": "vless",
                "settings": '{"clients":[]}',
                "streamSettings": '{"network":"ws"}',
                "sniffing": "{}",
                "remark": "WHITE_LIST_ONLY",
            }

    added_inbound: dict | None = None
    stale_inbound = {
        "id": 23,
        "nodeId": 55,
        "port": 10000,
        "protocol": "vless",
        "remark": "WHITE_LIST_ONLY",
        "tag": "node_ngenix2_inbound_10000",
    }
    post_calls: list[str] = []

    def _fake_list_inbounds(_api):
        return [added_inbound] if added_inbound else [stale_inbound]

    def _fake_post_json(_api, endpoint: str, data: dict):
        nonlocal added_inbound
        post_calls.append(endpoint)
        if endpoint == "panel/api/inbounds/del/23":
            return {"success": True}
        assert endpoint == "panel/api/inbounds/add"
        assert data["tag"] == "remote-white-tag"
        added_inbound = {
            "id": 101,
            "nodeId": data["nodeId"],
            "port": data["port"],
            "protocol": data["protocol"],
            "remark": data["remark"],
            "tag": data["tag"],
        }
        return {"success": True, "obj": {"id": 101}}

    monkeypatch.setattr(xui_api, "_list_inbounds_raw", _fake_list_inbounds)
    monkeypatch.setattr(xui_api, "_wait_for_node_inbound_import", lambda **_kwargs: None)
    monkeypatch.setattr(xui_api, "_api_post_json", _fake_post_json)

    inbound_id = xui_api._create_or_resolve_node_inbound(
        api=SimpleNamespace(),
        primary_inbound=_PrimaryInbound(),
        node_id=55,
        host_name="NGENIX2",
        template_inbound=_WhiteTemplateInbound(),
        preserve_template_remark=True,
    )

    assert inbound_id == 101
    assert post_calls == ["panel/api/inbounds/del/23", "panel/api/inbounds/add"]


def test_create_or_resolve_node_inbound_reuses_existing_canonical_white_inbound(monkeypatch):
    from shop_bot.modules import xui_api

    class _PrimaryInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 443,
                "protocol": "vless",
                "settings": '{"clients":[]}',
                "streamSettings": '{"network":"tcp"}',
                "sniffing": "{}",
                "remark": "MAIN",
            }

    class _WhiteTemplateInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 10000,
                "protocol": "vless",
                "settings": '{"clients":[]}',
                "streamSettings": '{"network":"ws"}',
                "sniffing": "{}",
                "remark": "WHITE_LIST_ONLY",
            }

    monkeypatch.setattr(
        xui_api,
        "_list_inbounds_raw",
        lambda _api: [
            {
                "id": 99,
                "port": 10000,
                "protocol": "vless",
                "remark": "WHITE_LIST_ONLY",
            }
        ],
    )

    post_calls: list[dict] = []
    monkeypatch.setattr(
        xui_api,
        "_api_post_json",
        lambda _api, endpoint, data: post_calls.append({"endpoint": endpoint, "data": data}),
    )

    inbound_id = xui_api._create_or_resolve_node_inbound(
        api=SimpleNamespace(),
        primary_inbound=_PrimaryInbound(),
        node_id=55,
        host_name="NGENIX2",
        template_inbound=_WhiteTemplateInbound(),
        preserve_template_remark=True,
    )

    assert inbound_id == 99
    assert post_calls == []


def test_create_or_resolve_node_inbound_reuses_existing_white_port_when_label_missing(monkeypatch):
    from shop_bot.modules import xui_api

    class _PrimaryInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 443,
                "protocol": "vless",
                "settings": '{"clients":[]}',
                "streamSettings": '{"network":"tcp"}',
                "sniffing": "{}",
                "remark": "MAIN",
            }

    class _WhiteTemplateInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 10000,
                "protocol": "vless",
                "settings": '{"clients":[]}',
                "streamSettings": '{"network":"ws"}',
                "sniffing": "{}",
                "remark": "WHITE_LIST_ONLY",
            }

    monkeypatch.setattr(
        xui_api,
        "_list_inbounds_raw",
        lambda _api: [{"id": 99, "port": 10000, "protocol": "vless"}],
    )
    monkeypatch.setattr(
        xui_api,
        "_api_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not add inbound")),
    )

    inbound_id = xui_api._create_or_resolve_node_inbound(
        api=SimpleNamespace(),
        primary_inbound=_PrimaryInbound(),
        node_id=55,
        host_name="NGENIX2",
        template_inbound=_WhiteTemplateInbound(),
        preserve_template_remark=True,
    )

    assert inbound_id == 99


def test_create_or_resolve_node_inbound_treats_white_source_as_canonical(monkeypatch):
    from shop_bot.modules import xui_api

    class _WhitePrimaryInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 10000,
                "protocol": "vless",
                "settings": '{"clients":[]}',
                "streamSettings": '{"network":"ws"}',
                "sniffing": "{}",
                "remark": "WHITE_LIST_ONLY",
            }

    call_state = {"count": 0}

    def _fake_list_inbounds(_api):
        call_state["count"] += 1
        if call_state["count"] == 1:
            return []
        return [{"id": 99, "port": 10000, "protocol": "vless"}]

    monkeypatch.setattr(xui_api, "_list_inbounds_raw", _fake_list_inbounds)
    monkeypatch.setattr(
        xui_api,
        "_api_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not add inbound")),
    )

    inbound_id = xui_api._create_or_resolve_node_inbound(
        api=SimpleNamespace(),
        primary_inbound=_WhitePrimaryInbound(),
        node_id=55,
        host_name="NGENIX2",
    )

    assert inbound_id == 99
    assert call_state["count"] == 2


def test_create_or_resolve_node_inbound_recovers_from_white_port_conflict(monkeypatch):
    from shop_bot.modules import xui_api

    class _PrimaryInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 443,
                "protocol": "vless",
                "settings": '{"clients":[]}',
                "streamSettings": '{"network":"tcp"}',
                "sniffing": "{}",
                "remark": "MAIN",
            }

    class _WhiteTemplateInbound:
        def to_json(self):
            return {
                "enable": True,
                "port": 10000,
                "protocol": "vless",
                "settings": '{"clients":[]}',
                "streamSettings": '{"network":"ws"}',
                "sniffing": "{}",
                "remark": "WHITE_LIST_ONLY",
            }

    call_state = {"count": 0}

    def _fake_list_inbounds(_api):
        call_state["count"] += 1
        if call_state["count"] == 1:
            return []
        return [{"id": 99, "port": 10000, "protocol": "vless"}]

    def _fake_post_json(_api, _endpoint, _data):
        msg = "Response status is not successful, message: Port already exists: 10000"
        raise ValueError(msg)

    monkeypatch.setattr(xui_api, "_list_inbounds_raw", _fake_list_inbounds)
    monkeypatch.setattr(xui_api, "_api_post_json", _fake_post_json)

    inbound_id = xui_api._create_or_resolve_node_inbound(
        api=SimpleNamespace(),
        primary_inbound=_PrimaryInbound(),
        node_id=55,
        host_name="NGENIX2",
        template_inbound=_WhiteTemplateInbound(),
        preserve_template_remark=True,
    )

    assert inbound_id == 99
    assert call_state["count"] == 2


def test_extract_inbound_list_accepts_nested_response_shapes():
    from shop_bot.modules import xui_api

    payload = {"success": True, "obj": {"inbounds": [{"id": 11}, "bad", {"id": 12}]}}

    assert xui_api._extract_inbound_list(payload) == [{"id": 11}, {"id": 12}]


def test_probe_node_requires_online_status(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.setattr(
        xui_api,
        "_api_post_json",
        lambda *_args, **_kwargs: {"success": True, "obj": {"status": "offline"}},
    )

    assert xui_api._probe_node(SimpleNamespace(), 55) is False

    monkeypatch.setattr(
        xui_api,
        "_api_post_json",
        lambda *_args, **_kwargs: {"success": True, "obj": {"status": "online"}},
    )

    assert xui_api._probe_node(SimpleNamespace(), 55) is True


def test_resolve_node_sync_template_inbound_infers_white_configured_inbound(monkeypatch):
    from shop_bot.modules import xui_api

    host_data = {
        "host_url": "https://node.example/3xuivless/",
        "host_username": "admin",
        "host_pass": "pass",
        "host_inbound_id": 11,
        "host_api_token_env": "node-token-env",
        "is_white_host": 0,
    }
    configured_inbound = SimpleNamespace(remark="WHITE_LIST_ONLY", tag="", id=11)
    login_calls: list[dict] = []

    def _fake_login_to_host(**kwargs):
        login_calls.append(kwargs)
        return SimpleNamespace(), configured_inbound

    monkeypatch.setattr(xui_api, "_resolve_host_api_token", lambda _host: "node-token")
    monkeypatch.setattr(xui_api, "login_to_host", _fake_login_to_host)

    template_inbound, is_white_host = xui_api._resolve_node_sync_template_inbound(
        "NGENIX2",
        host_data,
    )

    assert template_inbound is configured_inbound
    assert is_white_host is True
    assert login_calls[0]["inbound_id"] == 11
    assert login_calls[0]["api_token"] == "node-token"


def test_resolve_node_sync_template_inbound_finds_white_inbound_when_selected_is_wrong(monkeypatch):
    from shop_bot.modules import xui_api

    host_data = {
        "host_url": "https://node.example/3xuivless/",
        "host_username": "admin",
        "host_pass": "pass",
        "host_inbound_id": 22,
        "host_api_token_env": "node-token-env",
        "is_white_host": 0,
    }
    wrong_configured_inbound = SimpleNamespace(remark="NGENIX2", tag="", id=22)
    white_inbound = SimpleNamespace(remark="WHITE_LIST_ONLY", tag="", id=11)

    class _FakeInboundApi:
        def get_list(self):
            return [wrong_configured_inbound, white_inbound]

    monkeypatch.setattr(xui_api, "_resolve_host_api_token", lambda _host: "node-token")
    monkeypatch.setattr(
        xui_api,
        "login_to_host",
        lambda **_kwargs: (SimpleNamespace(inbound=_FakeInboundApi()), wrong_configured_inbound),
    )

    template_inbound, is_white_host = xui_api._resolve_node_sync_template_inbound(
        "NGENIX2",
        host_data,
    )

    assert template_inbound is white_inbound
    assert is_white_host is True


def test_white_bypass_label_detects_panel_white_list_names():
    from shop_bot.modules import xui_api

    assert xui_api._looks_like_white_bypass_label("WHITE_LIST_ONLY")
    assert xui_api._looks_like_white_bypass_label("white-list-only")
    assert xui_api._looks_like_white_bypass_label("WHITE2")


def test_sync_new_host_into_main_panel_copies_only_active_clients_and_applies_white_limit(monkeypatch):
    from shop_bot.modules import xui_api

    now_ms = 1_000_000
    active_expiry = now_ms + 45 * 24 * 60 * 60 * 1000
    monkeypatch.setattr(xui_api.time, "time", lambda: now_ms / 1000)
    monkeypatch.setattr(xui_api, "get_setting", lambda key: "MAIN" if key == "super_key_host_name" else None)

    def _fake_get_host(host_name: str):
        if host_name == "MAIN":
            return {
                "host_name": "MAIN",
                "host_url": "https://main.example/3xuivless/",
                "host_username": "admin",
                "host_pass": "pass",
                "host_inbound_id": 10,
                "host_api_token_env": "main-direct-token",
            }
        if host_name == "WHITE2":
            return {
                "host_name": "WHITE2",
                "host_url": "https://node.example/3xuivless/",
                "host_username": "admin",
                "host_pass": "pass",
                "host_inbound_id": 11,
                "host_api_token_env": "node-direct-token",
                "is_white_host": 1,
                "node_inbound_id": 22,
            }
        return None

    monkeypatch.setattr(xui_api, "get_host", _fake_get_host)

    active_client = SimpleNamespace(
        enable=True,
        expiry_time=active_expiry,
        id="uuid-active",
        email="user1@vpn.bot",
        sub_id="sub-active",
    )
    disabled_client = SimpleNamespace(
        enable=False,
        expiry_time=active_expiry,
        id="uuid-disabled",
        email="user2@vpn.bot",
        sub_id="sub-disabled",
    )
    expired_client = SimpleNamespace(
        enable=True,
        expiry_time=now_ms - 10_000,
        id="uuid-expired",
        email="user3@vpn.bot",
        sub_id="sub-expired",
    )
    no_subid_client = SimpleNamespace(
        enable=True,
        expiry_time=active_expiry,
        id="uuid-no-subid",
        email="user4@vpn.bot",
        sub_id=None,
    )

    primary_inbound = SimpleNamespace(
        id=10,
        settings=SimpleNamespace(clients=[active_client, disabled_client, expired_client, no_subid_client]),
    )
    white_template_inbound = SimpleNamespace(id=11, remark="WHITE_LIST_ONLY")
    node_inbound = SimpleNamespace(id=44)
    stale_client = SimpleNamespace(
        id="uuid-active",
        email="user1-white2-22@vpn.bot",
        sub_id="sub-active",
    )
    stale_inbound = SimpleNamespace(
        id=22,
        settings=SimpleNamespace(clients=[stale_client]),
    )

    class _FakeInboundApi:
        def get_by_id(self, inbound_id: int):
            if inbound_id == 10:
                return primary_inbound
            if inbound_id == 44:
                return node_inbound
            if inbound_id == 22:
                return stale_inbound
            return None

    delete_calls: list[tuple[int, str]] = []
    detach_calls = []

    def _detach(_api, email, inbound_id):
        detach_calls.append((inbound_id, email))
        return {"success": True}

    monkeypatch.setattr(xui_api, "_api_post_client_detach", _detach)

    class _FakeClientApi:
        def delete(self, inbound_id: int, client_uuid: str):
            delete_calls.append((inbound_id, client_uuid))
            stale_inbound.settings.clients = [
                client for client in stale_inbound.settings.clients if client.id != client_uuid
            ]

    fake_api = SimpleNamespace(inbound=_FakeInboundApi(), client=_FakeClientApi())

    def _fake_login_to_host(**kwargs):
        if int(kwargs["inbound_id"]) == 11:
            return SimpleNamespace(), white_template_inbound
        return fake_api, primary_inbound

    monkeypatch.setattr(xui_api, "login_to_host", _fake_login_to_host)
    monkeypatch.setattr(xui_api, "_resolve_or_create_node", lambda *_args, **_kwargs: 27)
    create_node_calls: list[dict] = []

    def _fake_create_or_resolve_node_inbound(**kwargs):
        create_node_calls.append(kwargs)
        return 44

    monkeypatch.setattr(xui_api, "_create_or_resolve_node_inbound", _fake_create_or_resolve_node_inbound)

    update_calls: list[dict] = []

    def _fake_update_or_create(*, api, inbound_id, email, days_to_add, **kwargs):
        _ = api
        update_calls.append(
            {
                "inbound_id": inbound_id,
                "email": email,
                "days_to_add": days_to_add,
                **kwargs,
            }
        )
        return kwargs["target_client_uuid"], kwargs["target_expiry_ms"]

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _fake_update_or_create)

    verified_sub_ids: list[str] = []
    monkeypatch.setattr(
        xui_api,
        "_get_subscription_links",
        lambda _api, sub_id: verified_sub_ids.append(sub_id) or ["vless://sub"],
    )

    result = xui_api.sync_new_host_into_main_panel("WHITE2")

    assert result["node_id"] == 27
    assert result["node_inbound_id"] == 44
    assert result["copied_clients"] == 1
    assert result["skipped_clients"] == 3
    assert result["verified_sub_ids"] == 1
    assert result["stale_node_inbound_id"] == 22
    assert result["stale_node_removed_clients"] == 1
    assert verified_sub_ids == ["sub-active"]
    assert delete_calls == []
    assert detach_calls == [(22, "user1-white2-22@vpn.bot")]
    assert create_node_calls[0]["template_inbound"] is white_template_inbound
    assert create_node_calls[0]["preserve_template_remark"] is True

    assert len(update_calls) == 1
    update_call = update_calls[0]
    assert update_call["inbound_id"] == 44
    assert update_call["email"] == "user1-white2-44@vpn.bot"
    assert update_call["target_sub_id"] == "sub-active"
    assert update_call["target_client_uuid"] == "uuid-active"
    assert update_call["target_expiry_ms"] == now_ms + 30 * 24 * 60 * 60 * 1000
    assert update_call["traffic_reset_days"] == 30
    assert active_client.expiry_time == active_expiry
    assert update_call["total_gb_bytes"] == xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES


def test_sync_new_host_into_main_panel_limits_subscription_smoke_checks(monkeypatch):
    from shop_bot.modules import xui_api

    now_ms = 1_000_000
    active_expiry = now_ms + 45 * 24 * 60 * 60 * 1000
    monkeypatch.setattr(xui_api.time, "time", lambda: now_ms / 1000)
    monkeypatch.setattr(xui_api, "get_setting", lambda key: "MAIN" if key == "super_key_host_name" else None)

    def _fake_get_host(host_name: str):
        base_host = {
            "host_url": "https://panel.example/3xuivless/",
            "host_username": "admin",
            "host_pass": "pass",
            "host_api_token_env": "direct-token",
        }
        if host_name == "MAIN":
            return {"host_name": "MAIN", "host_inbound_id": 10, **base_host}
        if host_name == "WHITE2":
            return {
                "host_name": "WHITE2",
                "host_inbound_id": 11,
                "is_white_host": 1,
                **base_host,
            }
        return None

    monkeypatch.setattr(xui_api, "get_host", _fake_get_host)

    active_clients = [
        SimpleNamespace(
            enable=True,
            expiry_time=active_expiry,
            id=f"uuid-{index}",
            email=f"user{index}@vpn.bot",
            sub_id=f"sub-{index}",
        )
        for index in range(3)
    ]
    primary_inbound = SimpleNamespace(
        id=10,
        settings=SimpleNamespace(clients=active_clients),
    )
    white_template_inbound = SimpleNamespace(id=11, remark="WHITE_LIST_ONLY")
    node_inbound = SimpleNamespace(id=44)

    class _FakeInboundApi:
        def get_by_id(self, inbound_id: int):
            if inbound_id == 10:
                return primary_inbound
            if inbound_id == 44:
                return node_inbound
            return None

    fake_api = SimpleNamespace(inbound=_FakeInboundApi())

    def _fake_login_to_host(**kwargs):
        if int(kwargs["inbound_id"]) == 11:
            return SimpleNamespace(), white_template_inbound
        return fake_api, primary_inbound

    monkeypatch.setattr(xui_api, "login_to_host", _fake_login_to_host)
    monkeypatch.setattr(xui_api, "_resolve_or_create_node", lambda *_args, **_kwargs: 27)
    monkeypatch.setattr(xui_api, "_create_or_resolve_node_inbound", lambda **_kwargs: 44)
    monkeypatch.setattr(
        xui_api,
        "update_or_create_client_on_panel",
        lambda **kwargs: (kwargs["target_client_uuid"], kwargs["target_expiry_ms"]),
    )

    verified_sub_ids: list[str] = []
    progress_events: list[dict[str, int]] = []
    monkeypatch.setattr(
        xui_api,
        "_get_subscription_links",
        lambda _api, sub_id: verified_sub_ids.append(sub_id) or ["vless://sub"],
    )

    result = xui_api.sync_new_host_into_main_panel(
        "WHITE2",
        subscription_verify_limit=1,
        progress_every=1,
        progress_callback=progress_events.append,
    )

    assert result["copied_clients"] == 3
    assert result["verified_sub_ids"] == 1
    assert verified_sub_ids == ["sub-0"]
    assert progress_events[-1] == {"processed": 3, "copied": 3, "skipped": 0}


def test_sync_new_host_into_main_panel_persists_resolved_white_inbound_id(monkeypatch):
    from shop_bot.modules import xui_api

    now_ms = 1_000_000
    active_expiry = now_ms + 45 * 24 * 60 * 60 * 1000
    monkeypatch.setattr(xui_api.time, "time", lambda: now_ms / 1000)
    monkeypatch.setattr(xui_api, "get_setting", lambda key: "MAIN" if key == "super_key_host_name" else None)

    def _fake_get_host(host_name: str):
        base_host = {
            "host_url": "https://panel.example/3xuivless/",
            "host_username": "admin",
            "host_pass": "pass",
            "host_api_token_env": "direct-token",
        }
        if host_name == "MAIN":
            return {"host_name": "MAIN", "host_inbound_id": 10, **base_host}
        if host_name == "NGENIX2":
            return {
                "host_name": "NGENIX2",
                "host_inbound_id": 1,
                "is_white_host": 1,
                **base_host,
            }
        return None

    monkeypatch.setattr(xui_api, "get_host", _fake_get_host)

    active_client = SimpleNamespace(
        enable=True,
        expiry_time=active_expiry,
        id="uuid-1",
        email="user1@vpn.bot",
        sub_id="sub-1",
    )
    primary_inbound = SimpleNamespace(
        id=10,
        settings=SimpleNamespace(clients=[active_client]),
    )
    white_template_inbound = SimpleNamespace(id=3, remark="WHITE_LIST_ONLY")
    node_inbound = SimpleNamespace(id=44)

    class _FakeInboundApi:
        def get_by_id(self, inbound_id: int):
            if inbound_id == 10:
                return primary_inbound
            if inbound_id == 44:
                return node_inbound
            return None

    fake_api = SimpleNamespace(inbound=_FakeInboundApi())
    monkeypatch.setattr(
        xui_api,
        "login_to_host",
        lambda **kwargs: (
            (SimpleNamespace(), white_template_inbound)
            if kwargs["host_url"] == "https://panel.example/3xuivless/" and int(kwargs["inbound_id"]) == 1
            else (fake_api, primary_inbound)
        ),
    )
    monkeypatch.setattr(
        xui_api,
        "_resolve_node_sync_template_inbound",
        lambda _host_name, _host_data: (white_template_inbound, True),
    )
    monkeypatch.setattr(xui_api, "_resolve_or_create_node", lambda *_args, **_kwargs: 27)
    monkeypatch.setattr(xui_api, "_create_or_resolve_node_inbound", lambda **_kwargs: 44)
    monkeypatch.setattr(
        xui_api,
        "update_or_create_client_on_panel",
        lambda **kwargs: (kwargs["target_client_uuid"], kwargs["target_expiry_ms"]),
    )
    monkeypatch.setattr(xui_api, "_get_subscription_links", lambda *_args, **_kwargs: ["vless://sub"])

    persisted_inbound_ids: list[tuple[str, int]] = []
    monkeypatch.setattr(
        xui_api,
        "update_host_inbound_id",
        lambda host_name, inbound_id: persisted_inbound_ids.append((host_name, inbound_id)) or True,
    )

    result = xui_api.sync_new_host_into_main_panel("NGENIX2", subscription_verify_limit=0)

    assert result["copied_clients"] == 1
    assert persisted_inbound_ids == [("NGENIX2", 3)]


def test_get_subscription_links_normalizes_nested_payload_and_deduplicates():
    from shop_bot.modules import xui_api

    payload = {
        "success": True,
        "obj": {
            "links": [
                "vless://node-main",
                "vless://node-main",
                "vless://node-white2\nvless://node-white3",
            ],
            "subscriptionLinks": "https://sub.example/shared",
            "noise": {"message": "ok"},
        },
    }

    class _FakeInboundApi:
        @staticmethod
        def _url(endpoint: str) -> str:
            return endpoint

        @staticmethod
        def _get(_url: str, _headers: dict):
            return SimpleNamespace(json=lambda: payload)

    fake_api = SimpleNamespace(inbound=_FakeInboundApi())
    links = xui_api._get_subscription_links(fake_api, "sub-123")

    assert sorted(links) == sorted(
        [
            "vless://node-main",
            "vless://node-white2",
            "vless://node-white3",
            "https://sub.example/shared",
        ]
    )


def test_get_subscription_links_accepts_raw_base64_panel_body():
    from shop_bot.modules import xui_api

    raw_payload = "vless://node-main\nvless://node-white"
    encoded_payload = base64.b64encode(raw_payload.encode()).decode()

    class _FakeResponse:
        text = encoded_payload
        content = encoded_payload.encode()

        @staticmethod
        def json():
            raise ValueError("not json")

    class _FakeInboundApi:
        @staticmethod
        def _url(endpoint: str) -> str:
            return endpoint

        @staticmethod
        def _get(_url: str, _headers: dict):
            return _FakeResponse()

    fake_api = SimpleNamespace(inbound=_FakeInboundApi())
    links = xui_api._get_subscription_links(fake_api, "sub-123")

    assert links == ["vless://node-main", "vless://node-white"]


def test_managed_subscription_builds_links_from_panel_clients_when_generator_is_empty(monkeypatch):
    from shop_bot.modules import xui_api

    primary = SimpleNamespace(id=3, remark="", tag="", protocol="vless", enable=True)
    fake_api = SimpleNamespace()
    raw_inbounds = [
        {
            "id": 3,
            "port": 53174,
            "protocol": "vless",
            "enable": True,
            "remark": "",
            "settings": json.dumps(
                {"clients": [{"id": "uuid-1", "subId": "sub-123", "email": "main", "enable": True}]}
            ),
            "streamSettings": json.dumps({"network": "tcp", "security": "reality"}),
        },
        {
            "id": 25,
            "port": 10000,
            "protocol": "vless",
            "enable": True,
            "remark": "WHITE_LIST_ONLY",
            "settings": json.dumps(
                {"clients": [{"id": "uuid-1", "subId": "sub-123", "email": "white", "enable": True}]}
            ),
            "streamSettings": json.dumps({"network": "ws", "security": "none"}),
        },
    ]

    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary))
    monkeypatch.setattr(xui_api, "_list_inbounds_raw", lambda _api: raw_inbounds)
    monkeypatch.setattr(xui_api, "_get_subscription_links_with_retry", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        xui_api,
        "get_connection_string",
        lambda inbound, uuid, _host_url, remark, **_kwargs: (
            f"vless://{uuid}@node{inbound.id}.example:{inbound.port}?type=tcp&security=reality#{remark}"
        ),
    )

    links = xui_api.get_managed_subscription_links(
        {
            "host_name": "SUPER2",
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 3,
        },
        "sub-123",
        include_white_inbounds=True,
    )

    assert len(links) == 2
    assert [unquote(urlparse(link).fragment) for link in links] == ["Standard", "White (без Wi-Fi)"]


def test_managed_subscription_loads_detailed_inbounds_when_options_have_no_settings(monkeypatch):
    from shop_bot.modules import xui_api

    primary = SimpleNamespace(id=1, remark="FINLAND", tag="", protocol="vless", enable=True)
    fake_api = SimpleNamespace()
    option_inbounds = [
        {"id": 1, "port": 443, "protocol": "vless", "enable": True, "remark": "FINLAND", "settings": None},
        {"id": 2, "port": 8443, "protocol": "vless", "enable": True, "remark": "GERMANY", "settings": None},
    ]
    detailed_inbounds = {
        1: {
            "id": 1,
            "port": 443,
            "protocol": "vless",
            "enable": True,
            "remark": "FINLAND",
            "settings": json.dumps(
                {"clients": [{"id": "uuid-1", "subId": "sub-123", "email": "main", "enable": True}]}
            ),
            "streamSettings": json.dumps({"network": "tcp", "security": "reality"}),
        },
        2: {
            "id": 2,
            "port": 8443,
            "protocol": "vless",
            "enable": True,
            "remark": "GERMANY",
            "settings": json.dumps(
                {"clients": [{"id": "uuid-1", "subId": "sub-123", "email": "node", "enable": True}]}
            ),
            "streamSettings": json.dumps({"network": "tcp", "security": "reality"}),
        },
    }

    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, primary))
    monkeypatch.setattr(xui_api, "_list_inbounds_raw", lambda _api: option_inbounds)
    monkeypatch.setattr(xui_api, "_get_inbound_raw_by_id", lambda _api, inbound_id, **_kwargs: detailed_inbounds[int(inbound_id)])
    monkeypatch.setattr(xui_api, "_get_subscription_links", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(xui_api, "_get_subscription_links_with_retry", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        xui_api,
        "get_connection_string",
        lambda inbound, uuid, _host_url, remark, **_kwargs: (
            f"vless://{uuid}@node{inbound.id}.example:{inbound.port}?type=tcp&security=reality#{remark}"
        ),
    )

    links = xui_api.get_managed_subscription_links(
        {
            "host_name": "MainPanel",
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
        },
        "sub-123",
        include_white_inbounds=True,
    )

    assert len(links) == 2
    assert [unquote(urlparse(link).fragment) for link in links] == ["Standard", "Standard"]

    labelled_links = xui_api.get_managed_subscription_links(
        {
            "host_name": "MainPanel",
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
        },
        "sub-123",
        include_white_inbounds=True,
        remark_mode="inbound_label",
    )

    assert len(labelled_links) == 2
    assert [unquote(urlparse(link).fragment) for link in labelled_links] == ["FINLAND", "GERMANY"]


def test_get_subscription_links_with_retry_waits_for_expected_count(monkeypatch):
    from shop_bot.modules import xui_api

    responses = iter(
        [
            ["vless://node-main"],
            ["vless://node-main", "vless://node-white2"],
            ["vless://node-main", "vless://node-white2", "vless://node-white3"],
        ]
    )
    sleep_calls: list[float] = []

    monkeypatch.setattr(
        xui_api,
        "_get_subscription_links",
        lambda _api, _sub_id: next(responses),
    )
    monkeypatch.setattr(xui_api.time, "sleep", lambda value: sleep_calls.append(float(value)))

    links = xui_api._get_subscription_links_with_retry(
        api=SimpleNamespace(),
        sub_id="sub-123",
        expected_count=3,
        attempts=4,
        delay_seconds=0.2,
    )

    assert links == ["vless://node-main", "vless://node-white2", "vless://node-white3"]
    assert sleep_calls == [0.2, 0.2]


def test_xhttp_external_proxy_connection_string_uses_cdn_client_settings():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(
        id=9,
        remark="Beget CDN",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[
                {
                    "dest": "oyiterhekepog.begetcdn.cloud",
                    "port": 443,
                    "forceTls": "tls",
                    "security": "tls",
                    "sni": "oyiterhekepog.begetcdn.cloud",
                    "host": "oyiterhekepog.begetcdn.cloud",
                    "fp": "chrome",
                    "alpn": "h3,h2",
                }
            ],
            xhttp_settings={
                "path": "/api/user",
                "mode": "packet-up",
                "extra": {
                    "xPaddingHeader": "Authorization",
                    "xPaddingKey": "token",
                    "xPaddingMethod": "tokenish",
                    "uplinkHTTPMethod": "GET",
                    "xPaddingObfsMode": True,
                    "xPaddingPlacement": "queryInHeader",
                },
            },
        ),
    )

    link = xui_api.get_connection_string(inbound, "uuid-1", "https://origin.example/panel/", "CDN")

    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "vless"
    assert parsed.username == "uuid-1"
    assert parsed.hostname == "oyiterhekepog.begetcdn.cloud"
    assert parsed.port == 443
    assert parsed.query.startswith(
        "encryption=none&type=xhttp&path=%2Fapi%2Fuser&host=oyiterhekepog.begetcdn.cloud&mode=packet-up&extra="
    )
    assert "&security=tls&sni=oyiterhekepog.begetcdn.cloud&fp=chrome&alpn=h2" in parsed.query
    assert query["encryption"] == ["none"]
    assert query["type"] == ["xhttp"]
    assert query["security"] == ["tls"]
    assert query["sni"] == ["oyiterhekepog.begetcdn.cloud"]
    assert query["host"] == ["oyiterhekepog.begetcdn.cloud"]
    assert query["fp"] == ["chrome"]
    assert query["alpn"] == ["h2"]
    assert query["path"] == ["/api/user"]
    assert query["mode"] == ["packet-up"]
    assert "%257B" in parsed.query
    assert query["extra"][0].startswith("%7B")
    extra = json.loads(unquote(query["extra"][0]))
    assert extra["path"] == "/api/user"
    assert extra["mode"] == "packet-up"
    assert extra["xPaddingHeader"] == "Authorization"
    assert extra["xPaddingKey"] == "token"
    assert extra["xPaddingMethod"] == "tokenish"
    assert extra["uplinkHTTPMethod"] == "GET"
    assert extra["xPaddingObfsMode"] is True
    assert extra["xPaddingPlacement"] == "queryInHeader"
    assert extra["headers"]["User-Agent"].startswith("Mozilla/5.0")


def test_xhttp_external_proxy_connection_string_accepts_upstream_tls_aliases():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(
        id=9,
        remark="Beget CDN",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[
                {
                    "dest": "oyiterhekepog.begetcdn.cloud",
                    "port": 443.0,
                    "forceTls": "tls",
                    "fingerprint": "chrome",
                    "alpn": ["h3", "h2"],
                }
            ],
            xhttp_settings={"path": "/api/user", "mode": "packet-up"},
        ),
    )

    link = xui_api.get_connection_string(inbound, "uuid-1", "https://origin.example/panel/", "CDN")
    parsed = urlparse(link)
    query = parse_qs(parsed.query)

    assert parsed.hostname == "oyiterhekepog.begetcdn.cloud"
    assert parsed.netloc.endswith(":443")
    assert parsed.port == 443
    assert query["security"] == ["tls"]
    assert query["fp"] == ["chrome"]
    assert query["alpn"] == ["h2"]


def test_xhttp_external_proxy_extra_preserves_existing_headers_user_agent():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(
        id=9,
        remark="Beget CDN",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[{"dest": "cdn.example", "port": 443, "forceTls": "tls"}],
            xhttp_settings={
                "path": "/api/user",
                "mode": "packet-up",
                "extra": {
                    "headers": {
                        "User-Agent": "CustomAgent/1.0",
                        "X-Test": "keep",
                    },
                },
            },
        ),
    )

    link = xui_api.get_connection_string(inbound, "uuid-1", "https://origin.example/panel/", "CDN")
    query = parse_qs(urlparse(link).query)
    extra = json.loads(unquote(query["extra"][0]))

    assert extra["headers"] == {
        "User-Agent": "CustomAgent/1.0",
        "X-Test": "keep",
    }


def test_xhttp_external_proxy_connection_string_falls_back_host_and_sni_to_dest():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(
        id=9,
        remark="Beget CDN",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[{"dest": "oyiterhekepog.begetcdn.cloud", "port": 443, "forceTls": "tls"}],
            xhttp_settings={"path": "/api/user", "mode": "packet-up"},
        ),
    )

    link = xui_api.get_connection_string(inbound, "uuid-1", "https://origin.example/panel/", "CDN")
    query = parse_qs(urlparse(link).query)

    assert query["sni"] == ["oyiterhekepog.begetcdn.cloud"]
    assert query["host"] == ["oyiterhekepog.begetcdn.cloud"]


def test_xhttp_external_proxy_connection_string_accepts_json_stream_settings():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(
        id=9,
        remark="Beget CDN",
        protocol="vless",
        enable=True,
        stream_settings=json.dumps(
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

    link = xui_api.get_connection_string(inbound, "uuid-1", "https://origin.example/panel/", "CDN")
    query = parse_qs(urlparse(link).query)
    extra = json.loads(unquote(query["extra"][0]))

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


def test_xhttp_external_proxy_connection_string_accepts_model_dump_settings():
    from shop_bot.modules import xui_api

    class _XhttpSettingsModel:
        def model_dump(self, **_kwargs):
            return {
                "path": "/api/user",
                "mode": "packet-up",
                "extra": {
                    "xPaddingHeader": "Authorization",
                    "xPaddingKey": "token",
                    "uplinkHTTPMethod": "GET",
                },
            }

    inbound = SimpleNamespace(
        id=9,
        remark="Beget CDN",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[
                SimpleNamespace(
                    dest="oyiterhekepog.begetcdn.cloud",
                    port=443,
                    forceTls="tls",
                    security="tls",
                    fp="chrome",
                    alpn="h3,h2",
                )
            ],
            xhttp_settings=_XhttpSettingsModel(),
        ),
    )

    link = xui_api.get_connection_string(inbound, "uuid-1", "https://origin.example/panel/", "CDN")
    query = parse_qs(urlparse(link).query)
    extra = json.loads(unquote(query["extra"][0]))

    assert query["path"] == ["/api/user"]
    assert query["mode"] == ["packet-up"]
    assert query["security"] == ["tls"]
    assert query["host"] == ["oyiterhekepog.begetcdn.cloud"]
    assert extra["path"] == "/api/user"
    assert extra["mode"] == "packet-up"
    assert extra["uplinkHTTPMethod"] == "GET"
    assert extra["headers"]["User-Agent"].startswith("Mozilla/5.0")


def test_xhttp_external_proxy_connection_string_defaults_missing_xhttp_settings():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(
        id=9,
        remark="Beget CDN",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[
                {
                    "dest": "oyiterhekepog.begetcdn.cloud",
                    "port": 443,
                    "forceTls": "tls",
                    "security": "tls",
                    "fp": "chrome",
                    "alpn": "h3,h2",
                }
            ],
            xhttp_settings={},
        ),
    )

    link = xui_api.get_connection_string(inbound, "uuid-1", "https://origin.example/panel/", "CDN")
    parsed = urlparse(link)
    query = parse_qs(parsed.query)
    extra = json.loads(unquote(query["extra"][0]))

    assert "None" not in parsed.query
    assert query["path"] == ["/api/user"]
    assert query["mode"] == ["packet-up"]
    assert query["security"] == ["tls"]
    assert extra["path"] == "/api/user"
    assert extra["mode"] == "packet-up"
    assert extra["xPaddingHeader"] == "Authorization"
    assert extra["xPaddingKey"] == "token"
    assert extra["xPaddingMethod"] == "tokenish"
    assert extra["uplinkHTTPMethod"] == "GET"
    assert extra["xPaddingObfsMode"] is True
    assert extra["xPaddingPlacement"] == "queryInHeader"
    assert extra["headers"]["User-Agent"].startswith("Mozilla/5.0")


def test_reality_connection_string_includes_only_explicit_client_transport_fields():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(
        port=443,
        stream_settings=SimpleNamespace(
            reality_settings={
                "settings": {"publicKey": "public-key", "fingerprint": "chrome"},
                "serverNames": ["server.example"],
                "shortIds": ["abcd"],
            }
        ),
    )

    link = xui_api.get_connection_string(inbound, "uuid-1", "https://panel.example/base/", "Reality")

    assert link == (
        "vless://uuid-1@panel.example:443"
        "?encryption=none&type=tcp&security=reality&pbk=public-key&fp=chrome&sni=server.example"
        "&sid=abcd#Reality"
    )


    inbound.stream_settings.reality_settings["settings"]["spiderX"] = "/"
    configured = xui_api.get_connection_string(
        inbound, "uuid-1", "https://panel.example/base/", "Reality",
        client=SimpleNamespace(flow="xtls-rprx-vision"),
    )
    query = parse_qs(urlparse(configured).query)
    assert query["spx"] == ["/"]
    assert query["flow"] == ["xtls-rprx-vision"]


def test_reality_connection_string_uses_node_address_for_mainpanel_inbound():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(
        id=4,
        node_id=2,
        remark="GERMANY",
        port=443,
        stream_settings=SimpleNamespace(
            reality_settings={
                "settings": {"publicKey": "public-key", "fingerprint": "chrome"},
                "serverNames": ["server.example"],
                "shortIds": ["abcd"],
            }
        ),
    )

    link = xui_api.get_connection_string(
        inbound,
        "uuid-1",
        "https://s83313.cdn.ngenix.net/panel/",
        "GERMANY",
        node_address_by_id={2: "192.0.2.15"},
    )

    parsed = urlparse(link)
    assert parsed.hostname == "192.0.2.15"
    assert parsed.port == 443
    assert unquote(parsed.fragment) == "GERMANY"


def test_get_managed_subscription_links_rewrites_mainpanel_node_hosts_from_node_map(monkeypatch):
    from shop_bot.modules import xui_api

    primary_inbound = SimpleNamespace(
        id=4,
        node_id=2,
        port=443,
        remark="GERMANY",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(network="tcp", security="reality"),
    )
    fake_api = SimpleNamespace()
    raw_inbounds = [
        {
            "id": 4,
            "nodeId": 2,
            "remark": "GERMANY",
            "protocol": "vless",
            "enable": True,
            "port": 443,
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "settings": {"publicKey": "de-key", "fingerprint": "chrome"},
                    "serverNames": ["www.ozon.ru"],
                    "shortIds": ["32"],
                },
            },
        },
        {
            "id": 6,
            "nodeId": 3,
            "remark": "ESTONIA",
            "protocol": "vless",
            "enable": True,
            "port": 50790,
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "settings": {"publicKey": "ee-key", "fingerprint": "chrome"},
                    "serverNames": ["www.ozon.ru"],
                    "shortIds": ["ec63ef0b"],
                },
            },
        },
    ]
    raw_links = [
        (
            "vless://uuid-1@s83313.cdn.ngenix.net:443"
            "?type=tcp&security=reality&pbk=de-key&fp=chrome&sni=www.ozon.ru&sid=32"
            "&spx=%2F&flow=xtls-rprx-vision#GERMANY"
        ),
        (
            "vless://uuid-1@s83313.cdn.ngenix.net:50790"
            "?type=tcp&security=reality&pbk=ee-key&fp=chrome&sni=www.ozon.ru&sid=ec63ef0b"
            "&spx=%2F&flow=xtls-rprx-vision#ESTONIA"
        ),
    ]

    def _fake_login_to_host(**_kwargs):
        return fake_api, primary_inbound

    def _fake_get_setting(key):
        return {
            "standard_key_host_name": "MainPanel",
            "super_key_host_name": "SUPER2",
        }.get(key)

    monkeypatch.setattr(xui_api, "login_to_host", _fake_login_to_host)
    monkeypatch.setattr(xui_api, "get_setting", _fake_get_setting)
    monkeypatch.setattr(xui_api, "get_all_hosts", list)
    monkeypatch.setattr(
        xui_api,
        "_list_nodes_raw",
        lambda _api: [
            {"id": 2, "name": "GERMANY", "address": "192.0.2.15"},
            {"id": 3, "name": "ESTONIA", "address": "192.0.2.10"},
        ],
    )
    monkeypatch.setattr(xui_api, "_list_inbounds_raw", lambda _api: raw_inbounds)
    monkeypatch.setattr(xui_api, "_get_subscription_links", lambda _api, _sub_id: raw_links)

    links = xui_api.get_managed_subscription_links(
        {
            "host_name": "MainPanel",
            "host_url": "https://s83313.cdn.ngenix.net/synthetic-panel-path-001/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 4,
        },
        "sub-1",
        include_white_inbounds=False,
        remark_mode="inbound_label",
    )

    assert [urlparse(link).hostname for link in links] == ["192.0.2.15", "192.0.2.10"]
    assert [unquote(urlparse(link).fragment) for link in links] == ["GERMANY", "ESTONIA"]


def test_patch_subscription_links_changes_only_xhttp_external_proxy_link():
    from shop_bot.modules import xui_api

    cdn_inbound = SimpleNamespace(
        id=9,
        remark="cdn",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[{"dest": "oyiterhekepog.begetcdn.cloud", "port": 443, "forceTls": "tls"}],
            xhttp_settings={"path": "/api/user", "mode": "packet-up"},
        ),
    )
    old_cdn_link = "vless://uuid-1@oyiterhekepog.begetcdn.cloud:443?type=xhttp&security=none#cdn"
    reality_link = "vless://uuid-2@reality.example:443?type=tcp&security=reality#reality"
    other_link = "https://sub.example/shared"

    patched = xui_api.patch_subscription_links_for_inbounds(
        [old_cdn_link, reality_link, other_link],
        [cdn_inbound],
    )

    patched_cdn_query = parse_qs(urlparse(patched[0]).query)
    assert patched[0] != old_cdn_link
    assert urlparse(patched[0]).hostname == "oyiterhekepog.begetcdn.cloud"
    assert patched_cdn_query["encryption"] == ["none"]
    assert patched_cdn_query["security"] == ["tls"]
    assert patched_cdn_query["type"] == ["xhttp"]
    assert unquote(urlparse(patched[1]).fragment) == "Standard"
    assert patched[2] == other_link


def test_patch_subscription_links_converts_websocket_external_proxy_link():
    from shop_bot.modules import xui_api

    ws_inbound = SimpleNamespace(
        id=31,
        port=10000,
        remark="WHITE_LIST_ONLY",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="ws",
            security="none",
            external_proxy=[
                {
                    "dest": "s20527.cdn.ngenix.net",
                    "port": 443,
                    "forceTls": "tls",
                    "security": "tls",
                    "sni": "s20527.cdn.ngenix.net",
                    "host": "s20527.cdn.ngenix.net",
                    "fp": "chrome",
                    "alpn": "h2,http/1.1",
                }
            ],
            ws_settings={"path": "/api/user"},
        ),
    )
    origin_link = "vless://uuid-1@192.0.2.12:10000?type=ws&security=none#NGENIX2"

    patched = xui_api.patch_subscription_links_for_inbounds([origin_link], [ws_inbound])
    parsed = urlparse(patched[0])
    query = parse_qs(parsed.query)

    assert parsed.hostname == "s20527.cdn.ngenix.net"
    assert parsed.port == 443
    assert unquote(parsed.fragment) == "White (без Wi-Fi)"
    assert query["encryption"] == ["none"]
    assert query["type"] == ["ws"]
    assert query["security"] == ["tls"]
    assert query["path"] == ["/api/user"]
    assert query["host"] == ["s20527.cdn.ngenix.net"]
    assert query["sni"] == ["s20527.cdn.ngenix.net"]
    assert query["fp"] == ["chrome"]
    assert query["alpn"] == ["h2,http/1.1"]


def test_subscription_filter_keeps_websocket_external_proxy_cdn_link():
    from shop_bot.modules import xui_api

    ws_inbound = SimpleNamespace(
        id=31,
        port=10000,
        remark="WHITE_LIST_ONLY",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="ws",
            external_proxy=[{"dest": "s20527.cdn.ngenix.net", "port": 443}],
            ws_settings={"path": "/api/user"},
        ),
    )
    links = [
        "vless://uuid-1@s20527.cdn.ngenix.net:443?type=ws&security=tls#WHITE_LIST_ONLY",
        "vless://uuid-2@other.example:443?type=ws&security=tls#other",
    ]

    filtered = xui_api._filter_subscription_links_for_inbounds(links, [ws_inbound])

    assert filtered == [links[0]]


def test_managed_subscription_filter_removes_white_inbounds_for_standard_keys(monkeypatch):
    from shop_bot.modules import xui_api

    standard_inbound = SimpleNamespace(
        id=1,
        port=443,
        remark="standard",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(network="tcp"),
    )
    regular_node = SimpleNamespace(
        id=2,
        port=8443,
        remark="regular node",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(network="tcp"),
    )
    white_cdn = SimpleNamespace(
        id=3,
        port=18081,
        remark="white cdn",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[{"dest": "cdn.example", "port": 443, "forceTls": "tls"}],
            xhttp_settings={"path": "/api/user", "mode": "packet-up"},
        ),
    )
    fake_api = SimpleNamespace(inbound=SimpleNamespace(get_list=lambda: [standard_inbound, regular_node, white_cdn]))
    raw_links = [
        "vless://uuid-1@standard.example:443?type=tcp&security=reality#standard",
        "vless://uuid-1@regular.example:8443?type=tcp&security=reality#regular",
        "vless://uuid-1@cdn.example:443?type=xhttp&security=none#white",
    ]
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, standard_inbound))

    def _fake_get_setting(key: str) -> str | None:
        return {
            "standard_key_host_name": "standard-main",
            "super_key_host_name": "SUPER2",
        }.get(key)

    monkeypatch.setattr(
        xui_api,
        "get_setting",
        _fake_get_setting,
    )
    monkeypatch.setattr(xui_api, "get_all_hosts", list)

    def _fake_get_subscription_links(*_args, **_kwargs):
        return raw_links

    monkeypatch.setattr(xui_api, "_get_subscription_links_with_retry", _fake_get_subscription_links)

    links = xui_api.get_managed_subscription_links(
        {
            "host_name": "standard-main",
            "host_url": "https://main.example/panel/",
            "host_username": "user",
            "host_pass": "pass",
            "host_inbound_id": 1,
        },
        "shared-sub",
        include_white_inbounds=False,
    )

    assert len(links) == 2
    assert all("cdn.example" not in link for link in links)
    assert [unquote(urlparse(link).fragment) for link in links] == ["Standard", "Standard"]


def test_patch_subscription_links_converts_xhttp_origin_link_to_external_proxy():
    from shop_bot.modules import xui_api

    cdn_inbound = SimpleNamespace(
        id=21,
        port=18081,
        remark="Обход белых, не для WIFI",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[
                {
                    "dest": "oyiterhekepog.begetcdn.cloud",
                    "port": 443,
                    "forceTls": "tls",
                    "security": "tls",
                    "sni": "oyiterhekepog.begetcdn.cloud",
                    "host": "oyiterhekepog.begetcdn.cloud",
                    "fp": "chrome",
                    "alpn": "h3,h2",
                }
            ],
            xhttp_settings={
                "path": "/api/user",
                "mode": "packet-up",
                "extra": {
                    "xPaddingHeader": "Authorization",
                    "xPaddingKey": "token",
                    "xPaddingMethod": "tokenish",
                    "uplinkHTTPMethod": "GET",
                    "xPaddingObfsMode": True,
                    "xPaddingPlacement": "queryInHeader",
                },
            },
        ),
    )
    origin_link = (
        "vless://uuid-1@192.0.2.14:18081"
        "?type=xhttp&security=none&path=/api/user&mode=packet-up#Обход белых, не для WIFI"
    )

    patched = xui_api.patch_subscription_links_for_inbounds([origin_link], [cdn_inbound])
    parsed = urlparse(patched[0])
    query = parse_qs(parsed.query)
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


def test_patch_subscription_links_fills_missing_cdn_host_sni_and_extra_from_json_stream_settings():
    from shop_bot.modules import xui_api

    cdn_inbound = SimpleNamespace(
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
        "?type=xhttp&security=tls&fp=chrome&alpn=h3,h2&path=/api/user&mode=packet-up"
        "&extra=%7B%22mode%22:%22packet-up%22,%22xPaddingHeader%22:%22Authorization%22%7D#cdn"
    )

    patched = xui_api.patch_subscription_links_for_inbounds([raw_link], [cdn_inbound])
    query = parse_qs(urlparse(patched[0]).query)
    extra = json.loads(unquote(query["extra"][0]))

    assert query["sni"] == ["oyiterhekepog.begetcdn.cloud"]
    assert query["host"] == ["oyiterhekepog.begetcdn.cloud"]
    assert extra["path"] == "/api/user"
    assert extra["mode"] == "packet-up"
    assert extra["uplinkHTTPMethod"] == "GET"
    assert extra["headers"]["User-Agent"].startswith("Mozilla/5.0")


def test_patch_subscription_links_uses_safe_name_for_white_bypass_cdn_link():
    from shop_bot.modules import xui_api

    cdn_inbound = SimpleNamespace(
        id=9,
        remark="white cdn",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[{"dest": "oyiterhekepog.begetcdn.cloud", "port": 443, "forceTls": "tls"}],
            xhttp_settings={"path": "/api/user", "mode": "packet-up"},
        ),
    )
    raw_link = (
        "vless://uuid-1@oyiterhekepog.begetcdn.cloud:443"
        "?type=xhttp&security=none#Обход белых, не для WIFI-user442482830-key2"
    )
    reality_link = (
        "vless://uuid-2@reality.example:443?type=tcp&security=reality#Очень длинный технический user442482830-key2"
    )

    patched = xui_api.patch_subscription_links_for_inbounds([raw_link, reality_link], [cdn_inbound])

    assert unquote(urlparse(patched[0]).fragment) == "White (без Wi-Fi)"
    assert unquote(urlparse(patched[1]).fragment) == "Standard"


def test_patch_subscription_links_uses_standard_name_for_technical_xhttp_label():
    from shop_bot.modules import xui_api

    cdn_inbound = SimpleNamespace(
        id=9,
        remark="node_cdn_inbound_443",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(
            network="xhttp",
            security="none",
            external_proxy=[{"dest": "oyiterhekepog.begetcdn.cloud", "port": 443, "forceTls": "tls"}],
            xhttp_settings={"path": "/api/user", "mode": "packet-up"},
        ),
    )
    raw_link = (
        "vless://uuid-1@oyiterhekepog.begetcdn.cloud:443"
        "?type=xhttp&security=none#Очень длинный технический user442482830-key2"
    )

    patched = xui_api.patch_subscription_links_for_inbounds([raw_link], [cdn_inbound])

    assert unquote(urlparse(patched[0]).fragment) == "Standard"


def test_patch_subscription_links_sanitizes_technical_reality_link_names():
    from shop_bot.modules import xui_api

    links = [
        "vless://uuid-1@reality.example:443?type=tcp&security=reality#user7073206859-key10@vpn.bot-pepelmain",
        "vless://uuid-2@white.example:443?type=tcp&security=reality#Whitelistonly2-user7073206859-key10",
        "vless://uuid-3@super.example:443?type=tcp&security=reality#SUPER2-user7073206859-key10",
        "vless://uuid-4@custom.example:443?type=tcp&security=reality#Custom Node",
    ]

    patched = xui_api.patch_subscription_links_for_inbounds(links, [])

    assert unquote(urlparse(patched[0]).fragment) == "Standard"
    assert unquote(urlparse(patched[1]).fragment) == "White (без Wi-Fi)"
    assert unquote(urlparse(patched[2]).fragment) == "Standard"
    assert unquote(urlparse(patched[3]).fragment) == "Standard"
    assert parse_qs(urlparse(patched[0]).query)["security"] == ["reality"]


def test_patch_subscription_links_ignores_xhttp_without_external_proxy():
    from shop_bot.modules import xui_api

    inbound = SimpleNamespace(
        id=9,
        remark="origin",
        protocol="vless",
        enable=True,
        stream_settings=SimpleNamespace(network="xhttp", security="none", external_proxy=[], xhttp_settings={}),
    )
    link = "vless://uuid-1@origin.example:443?type=xhttp&security=none#origin"

    patched = xui_api.patch_subscription_links_for_inbounds([link], [inbound])

    assert unquote(urlparse(patched[0]).fragment) == "Standard"


def _renewal_with_component_identity_snapshot(monkeypatch, *, secondary_read_fails=False):
    """A central-client panel with a stable public ID and an existing distinct component."""
    from shop_bot.modules import xui_api

    monkeypatch.setattr(xui_api.time, "sleep", lambda _seconds: None)
    public_sub_id = "public-subscription-fixture"
    component_sub_id = "existing-component-fixture"
    primary = SimpleNamespace(id=1, remark="PRIMARY", protocol="vless", enable=True, port=443)
    api = SimpleNamespace(inbound=SimpleNamespace())
    rows = [
        {"id": 1, "remark": "PRIMARY", "protocol": "vless", "enable": True, "port": 443,
         "settings": json.dumps({"clients": [{"id": "fixture-client", "email": "user1@vpn.bot",
             "enable": True, "expiryTime": 1_800_000_000_000, "subId": public_sub_id}]})},
        {"id": 2, "remark": "SECONDARY", "protocol": "vless", "enable": True, "port": 8443,
         "settings": None if secondary_read_fails else json.dumps({"clients": [{
             "id": "fixture-client", "email": "user1-secondary-2@vpn.bot", "enable": True,
             "expiryTime": 1_800_000_000_000, "subId": component_sub_id}]})},
    ]

    _attach_raw_inbound_snapshots(
        api, rows,
        clients_by_id={row["id"]: json.loads(row["settings"])["clients"]
                       for row in rows if row["settings"] is not None},
        unavailable_ids={2} if secondary_read_fails else set(),
    )

    calls = []

    def update_client(_api, inbound_id, email, _days, **kwargs):
        calls.append({"inbound_id": inbound_id, "email": email, **kwargs})
        return "fixture-client", kwargs["target_expiry_ms"]

    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (api, primary))
    monkeypatch.setattr(xui_api, "get_setting", lambda _key: None)
    monkeypatch.setattr(xui_api, "get_all_hosts", lambda: [])
    monkeypatch.setattr(xui_api, "get_key_by_email", lambda _email: {"subscription_tier": "standard"})
    monkeypatch.setattr(xui_api, "_node_public_address_maps", lambda _api: ({}, {}))
    monkeypatch.setattr(xui_api, "_generate_sub_id", lambda: "unexpected-new-component-fixture")
    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", update_client)
    monkeypatch.setattr(xui_api, "_get_subscription_links_with_retry", lambda *_args, **_kwargs: [
        "vless://fixture-client@panel.example:443?type=tcp#PRIMARY",
        "vless://fixture-client@panel.example:8443?type=tcp#SECONDARY",
    ])
    monkeypatch.setattr(xui_api, "get_connection_string", lambda inbound, client_uuid, _host, remark, **_kwargs:
        f"vless://{client_uuid}@panel.example:{inbound.port}?type=tcp#{remark}")
    result = xui_api._create_or_update_key_on_host_once(
        host_name="synthetic-panel",
        host_data={"host_url": "https://panel.example", "host_username": "fixture",
                   "host_pass": "fixture", "host_inbound_id": 1,
                   "host_subscription_base_url": "https://subscription.example/sub/"},
        email="user1@vpn.bot", days_to_add=30,
        target_client_uuid="fixture-client", target_sub_id=public_sub_id,
        target_expiry_ms=1_800_000_000_000,
    )
    return result, calls


def test_renewal_preserves_existing_secondary_component_sub_id(monkeypatch):
    result, calls = _renewal_with_component_identity_snapshot(monkeypatch)

    assert result is not None
    assert result["sub_id"] == "public-subscription-fixture"
    assert result["subscription_link"] == "https://subscription.example/sub/public-subscription-fixture"
    assert [call["inbound_id"] for call in calls] == [1, 2]
    assert calls[1]["target_sub_id"] == "existing-component-fixture"


def test_renewal_identity_read_failure_stops_before_any_client_mutation(monkeypatch):
    result, calls = _renewal_with_component_identity_snapshot(monkeypatch, secondary_read_fails=True)

    assert calls == []
    assert result is None
