from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from requests.exceptions import HTTPError

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _InboundApi:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls: list[str] = []

    def _url(self, endpoint):
        return endpoint

    def _get(self, endpoint, _headers):
        self.calls.append(endpoint)
        return _Response(self.payloads[endpoint])

    def get_by_id(self, _inbound_id):
        raise AssertionError("minimal panel scan must not use py3xui get_by_id")


def test_scan_inbound_clients_minimal_reads_only_required_client_fields():
    from shop_bot.modules import xui_api

    settings = json.dumps(
        {
            "clients": [
                {
                    "id": "client-1",
                    "email": "local@example.com",
                    "expiryTime": "1893456000000",
                    "note": "contains } and ] inside a string",
                },
                {
                    "id": "client-2",
                    "email": "orphan@example.com",
                    "expiryTime": 123,
                },
            ],
            "decryption": "none",
        }
    )
    inbound_api = _InboundApi(
        {
            "panel/api/inbounds/get/1": {
                "success": True,
                "obj": {
                    "id": 1,
                    "settings": settings,
                },
            }
        }
    )
    api = SimpleNamespace(inbound=inbound_api)

    scan = xui_api.scan_inbound_clients_minimal(
        api,
        1,
        ["local@example.com"],
        orphan_sample_limit=1,
    )

    assert inbound_api.calls == ["panel/api/inbounds/get/1"]
    assert scan.orphan_total == 1
    assert scan.orphan_sample == ["orphan@example.com"]
    assert set(scan.clients_by_email) == {"local@example.com"}
    local_client = scan.clients_by_email["local@example.com"]
    assert local_client.id == "client-1"
    assert local_client.expiry_time == 1_893_456_000_000


def test_subscription_scan_maps_suffixed_panel_email_to_local_base(monkeypatch):
    from shop_bot.modules import xui_api

    settings = json.dumps(
        {
            "clients": [
                {
                    "id": "client-key6",
                    "email": "user123-key6-estonia2-7@vpn.bot",
                    "expiryTime": "1785280407000",
                },
                {
                    "id": "different-key",
                    "email": "user123-key60-estonia2-7@vpn.bot",
                    "expiryTime": "1785280407000",
                },
            ]
        }
    )
    inbound_api = _InboundApi(
        {
            "panel/api/inbounds/get/7": {
                "success": True,
                "obj": {"id": 7, "settings": settings},
            }
        }
    )
    api = SimpleNamespace(inbound=inbound_api)
    primary_inbound = SimpleNamespace(id=7)
    monkeypatch.setattr(xui_api, "_list_inbounds_raw", lambda _api: [])
    monkeypatch.setattr(
        xui_api,
        "_subscription_inbounds",
        lambda *_args, **_kwargs: [primary_inbound],
    )

    scan = xui_api.scan_subscription_inbounds_clients_minimal(
        api,
        primary_inbound,
        "MainPanel",
        ["user123-key6@vpn.bot"],
    )

    assert set(scan.clients_by_email) == {"user123-key6@vpn.bot"}
    assert scan.clients_by_email["user123-key6@vpn.bot"].email == (
        "user123-key6-estonia2-7@vpn.bot"
    )
    assert scan.orphan_total == 1
    assert scan.orphan_sample == ["user123-key60-estonia2-7@vpn.bot"]


def test_scan_inbound_clients_minimal_fails_closed_when_settings_missing():
    from shop_bot.modules import xui_api

    inbound_api = _InboundApi(
        {
            "panel/api/inbounds/get/1": {
                "success": True,
                "obj": {"id": 1},
            }
        }
    )
    api = SimpleNamespace(inbound=inbound_api)

    with pytest.raises(ValueError, match="panel_inbound_settings_missing"):
        xui_api.scan_inbound_clients_minimal(api, 1, ["local@example.com"])


def test_count_inbound_clients_minimal_avoids_full_inbound_load():
    from shop_bot.modules import xui_api

    settings = json.dumps(
        {
            "clients": [
                {"id": "client-1", "email": "one@example.com"},
                {"id": "client-2", "email": "two@example.com"},
                {"id": "client-3", "email": "three@example.com"},
            ]
        }
    )
    inbound_api = _InboundApi(
        {
            "panel/api/inbounds/get/7": {
                "success": True,
                "obj": {
                    "id": 7,
                    "settings": settings,
                },
            }
        }
    )
    api = SimpleNamespace(inbound=inbound_api)

    assert xui_api.count_inbound_clients_minimal(api, 7) == 3
    assert inbound_api.calls == ["panel/api/inbounds/get/7"]


def test_scan_inbound_clients_minimal_does_not_fallback_to_full_list():
    from shop_bot.modules import xui_api

    inbound_api = _InboundApi(
        {
            "panel/api/inbounds/get/1": {
                "success": False,
                "msg": "not found",
            },
            "panel/api/inbounds/list": {
                "success": True,
                "obj": [{"id": 1, "settings": json.dumps({"clients": []})}],
            },
        }
    )
    api = SimpleNamespace(inbound=inbound_api)

    with pytest.raises(RuntimeError, match="panel_inbound_not_found"):
        xui_api.scan_inbound_clients_minimal(api, 1, [])

    assert inbound_api.calls == ["panel/api/inbounds/get/1"]


def test_raw_inbound_lookup_can_use_lightweight_fallback_without_full_list():
    from shop_bot.modules import xui_api

    inbound_api = _InboundApi(
        {
            "panel/api/inbounds/get/1": {
                "success": False,
                "msg": "record not found",
            },
            "panel/api/inbounds/list/slim": {
                "success": True,
                "obj": [{"id": 1, "remark": "pepelmain", "protocol": "vless"}],
            },
        }
    )
    api = SimpleNamespace(inbound=inbound_api)

    raw_inbound = xui_api._get_inbound_raw_by_id(
        api,
        1,
        allow_lightweight_fallback=True,
    )

    assert raw_inbound["remark"] == "pepelmain"
    assert inbound_api.calls == [
        "panel/api/inbounds/get/1",
        "panel/api/inbounds/options",
        "panel/api/inbounds/list/slim",
    ]


def test_raw_inbound_lookup_classifies_transport_failure_separately():
    from shop_bot.modules import xui_api

    class _BrokenInboundApi:
        calls: list[str]

        def __init__(self):
            self.calls = []

        def _url(self, endpoint):
            return endpoint

        def _get(self, endpoint, _headers, **_kwargs):
            self.calls.append(endpoint)
            raise RuntimeError("panel route unavailable")

    inbound_api = _BrokenInboundApi()
    api = SimpleNamespace(inbound=inbound_api)

    with pytest.raises(xui_api.PanelInboundLookupError, match="panel_raw_lookup_failed:1"):
        xui_api._get_inbound_raw_by_id(api, 1)

    assert inbound_api.calls == ["panel/api/inbounds/get/1"]


def test_update_or_create_client_on_panel_raw_path_avoids_full_inbound_load():
    from shop_bot.modules import xui_api

    settings = json.dumps(
        {
            "clients": [
                {
                    "id": "client-1",
                    "email": "local@example.com",
                    "enable": False,
                    "expiryTime": 1_700_000_000_000,
                    "flow": "xtls-rprx-vision",
                    "subId": "old-sub",
                },
                {
                    "id": "other-client",
                    "email": "other@example.com",
                    "expiryTime": 1_700_000_000_000,
                },
            ]
        }
    )
    stream_settings = json.dumps({"network": "tcp", "security": "reality"})
    inbound_api = _InboundApi(
        {
            "panel/api/inbounds/get/11": {
                "success": True,
                "obj": {
                    "id": 11,
                    "port": 443,
                    "protocol": "vless",
                    "settings": settings,
                    "streamSettings": stream_settings,
                },
            }
        }
    )
    updated_clients: list[tuple[str, str, int, bool]] = []

    class _ClientApi:
        def update(self, client_uuid, client):
            updated_clients.append(
                (
                    client_uuid,
                    client.email,
                    int(client.expiry_time),
                    bool(client.enable),
                )
            )

    api = SimpleNamespace(inbound=inbound_api, client=_ClientApi())

    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=11,
        email="local@example.com",
        days_to_add=0,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="new-sub",
    )

    assert inbound_api.calls == ["panel/api/inbounds/get/11"]
    assert client_uuid == "client-1"
    assert expiry_ms == 1_800_000_000_000
    assert updated_clients == [("client-1", "local@example.com", 1_800_000_000_000, True)]


def test_add_client_404_uses_client_api_without_updating_raw_inbound(monkeypatch):
    from shop_bot.modules import xui_api

    settings = json.dumps({"clients": [
        {"id": "client-1", "email": "one@example.com", "expiryTime": 1_700_000_000_000},
        {"id": "client-2", "email": "two@example.com", "expiryTime": 1_700_000_000_000},
    ]})
    stream_settings = json.dumps({"network": "tcp", "security": "reality"})
    raw_inbound = {"id": 11, "port": 443, "protocol": "vless",
                   "settings": settings, "streamSettings": stream_settings}
    posts = []

    class _ClientOnlyInboundApi(_InboundApi):
        def _post(self, endpoint, _headers, data):
            posts.append((endpoint, data))
            assert endpoint == "panel/api/clients/add"
            assert "streamSettings" not in data
            return _Response({"success": True})

    class _ClientApi:
        def __init__(self):
            self.add_calls = 0

        def add(self, inbound_id, _clients):
            assert inbound_id == 11
            self.add_calls += 1
            exc = HTTPError("404 Client Error: Not Found")
            exc.response = SimpleNamespace(status_code=404)
            raise exc

    inbound_api = _ClientOnlyInboundApi({"panel/api/inbounds/get/11": {"success": True, "obj": raw_inbound}})
    client_api = _ClientApi()
    monkeypatch.setattr(xui_api.uuid, "uuid4", lambda: "client-new")
    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=SimpleNamespace(inbound=inbound_api, client=client_api),
        inbound_id=11, email="new@example.com", days_to_add=0,
        target_expiry_ms=1_800_000_000_000,
    )

    assert inbound_api.calls == ["panel/api/inbounds/get/11"]
    assert client_api.add_calls == 1
    assert (client_uuid, expiry_ms) == ("client-new", 1_800_000_000_000)
    assert len(posts) == 1
    endpoint, payload = posts[0]
    assert endpoint == "panel/api/clients/add"
    assert payload["inboundIds"] == [11]
    assert payload["client"]["id"] == "client-new"
    assert payload["client"]["email"] == "new@example.com"
    assert payload["client"]["expiryTime"] == 1_800_000_000_000
    assert raw_inbound["settings"] == settings
    assert raw_inbound["streamSettings"] == stream_settings


@pytest.mark.parametrize("operation", ["add", "update"])
def test_client_upsert_fails_closed_when_both_client_endpoints_are_missing(operation):
    from shop_bot.modules import xui_api

    existing = {"id": "client-1", "email": "synthetic@example.invalid", "enable": True,
                "expiryTime": 1_700_000_000_000, "subId": "same-sub"}
    raw_inbound = {
        "id": 11, "port": 443, "protocol": "vless",
        "settings": json.dumps({"clients": [existing] if operation == "update" else []}),
        "streamSettings": json.dumps({"network": "ws", "wsSettings": {"path": "/synthetic"},
                                      "externalProxy": [{"dest": "cdn.example.invalid", "port": 443}],
                                      "unknownTransportField": {"keep": True}}),
    }
    before = json.dumps(raw_inbound, sort_keys=True)
    posts = []

    def missing_endpoint():
        error = HTTPError("404 Client Error: Not Found")
        error.response = SimpleNamespace(status_code=404)
        raise error

    class _MissingClientEndpoints(_InboundApi):
        def _post(self, endpoint, _headers, data):
            posts.append((endpoint, data))
            if endpoint.startswith("panel/api/clients/"):
                missing_endpoint()
            raise AssertionError("Normal issuance must not use full inbound update")

    class _LegacyClientApi:
        def add(self, _inbound_id, _clients):
            missing_endpoint()

        def update(self, _client_uuid, _client):
            missing_endpoint()

    central_client = {**existing, "uuid": "client-1", "inboundIds": [11]}
    inbound_api = _MissingClientEndpoints({
        "panel/api/inbounds/get/11": {"success": True, "obj": raw_inbound},
        "panel/api/clients/list": {"success": True, "obj": [central_client]},
    })
    result = xui_api.update_or_create_client_on_panel(
        api=SimpleNamespace(inbound=inbound_api, client=_LegacyClientApi()), inbound_id=11,
        email="synthetic@example.invalid", days_to_add=0, target_client_uuid="client-1",
        target_sub_id="same-sub", target_expiry_ms=1_800_000_000_000,
    )

    assert result == (None, None)
    expected = "panel/api/clients/add" if operation == "add" else (
        "panel/api/clients/update/synthetic%40example.invalid?inboundIds=11"
    )
    assert [endpoint for endpoint, _ in posts] == [expected]
    assert json.dumps(raw_inbound, sort_keys=True) == before


@pytest.mark.parametrize("operation", ["add", "update"])
def test_duplicate_email_does_not_trigger_full_inbound_repair(operation):
    from shop_bot.modules import xui_api

    raw_inbound = {
        "id": 11, "port": 443, "protocol": "vless",
        "settings": json.dumps({"clients": [{
            "id": "client-1", "email": "synthetic@example.invalid", "enable": True,
            "expiryTime": 1_700_000_000_000, "subId": "same-sub",
        }]}),
        "streamSettings": json.dumps({"network": "ws", "wsSettings": {"path": "/synthetic"}}),
    }
    before = json.dumps(raw_inbound, sort_keys=True)
    posts = []

    class _NoInboundRepair(_InboundApi):
        def _get(self, endpoint, headers):
            response = super()._get(endpoint, headers)
            if operation == "add" and len(self.calls) == 1:
                # A concurrent request can create the row after the initial read.
                # A duplicate response must still not authorize inbound repair.
                return _Response({"success": True, "obj": {**raw_inbound, "settings": '{"clients": []}'}})
            return response

        def _post(self, endpoint, _headers, data):
            posts.append((endpoint, data))
            raise AssertionError("Duplicate-email recovery needs separate reviewed repair")

    class _ClientApi:
        def add(self, _inbound_id, _clients):
            raise RuntimeError("duplicate email")

        def update(self, _client_uuid, _client):
            raise RuntimeError("duplicate email")

    inbound_api = _NoInboundRepair({"panel/api/inbounds/get/11": {"success": True, "obj": raw_inbound}})
    result = xui_api.update_or_create_client_on_panel(
        api=SimpleNamespace(inbound=inbound_api, client=_ClientApi()), inbound_id=11,
        email="synthetic@example.invalid", days_to_add=0, target_client_uuid="client-1",
        target_sub_id="same-sub", target_expiry_ms=1_800_000_000_000,
    )

    assert result == (None, None)
    assert posts == []
    assert json.dumps(raw_inbound, sort_keys=True) == before


@pytest.mark.parametrize("operation", ["add", "update"])
@pytest.mark.parametrize("endpoint_available", [True, False])
def test_hysteria_upsert_uses_only_client_api(operation, endpoint_available):
    from shop_bot.modules import xui_api

    existing = {
        "id": "hysteria-client", "email": "synthetic@example.invalid", "enable": True,
        "expiryTime": 1_700_000_000_000, "subId": "same-sub", "auth": "synthetic-auth",
        "password": "synthetic-auth", "security": "auto",
    }
    raw_inbound = {
        "id": 11, "port": 443, "protocol": "hysteria2",
        "settings": json.dumps({"clients": [existing] if operation == "update" else []}),
        "streamSettings": json.dumps({
            "network": "hysteria2", "security": "tls",
            "hysteriaSettings": {"version": 2}, "unknownTransportField": {"keep": True},
        }),
    }
    before = json.dumps(raw_inbound, sort_keys=True)
    posts = []

    class _ClientOnlyHysteriaApi(_InboundApi):
        def _post(self, endpoint, _headers, data):
            posts.append((endpoint, data))
            if not endpoint.startswith("panel/api/clients/"):
                raise AssertionError("Hysteria issuance must not replace an inbound")
            if not endpoint_available:
                error = HTTPError("404 Client Error: Not Found")
                error.response = SimpleNamespace(status_code=404)
                raise error
            return _Response({"success": True})

    central_client = {**existing, "uuid": "hysteria-client", "inboundIds": [11],
                      "futureClientField": {"preserve": True}}
    inbound_api = _ClientOnlyHysteriaApi({
        "panel/api/inbounds/get/11": {"success": True, "obj": raw_inbound},
        "panel/api/clients/list": {"success": True, "obj": [central_client]},
    })
    result = xui_api.update_or_create_client_on_panel(
        api=SimpleNamespace(inbound=inbound_api), inbound_id=11,
        email="synthetic@example.invalid", days_to_add=0, target_client_uuid="hysteria-client",
        target_sub_id="same-sub", target_expiry_ms=1_800_000_000_000,
        source_client={"auth": "synthetic-auth", "password": "synthetic-auth"},
        total_gb_bytes=0,
    )

    expected_endpoint = "panel/api/clients/add" if operation == "add" else (
        "panel/api/clients/update/synthetic%40example.invalid?inboundIds=11"
    )
    assert [endpoint for endpoint, _ in posts] == [expected_endpoint]
    assert json.dumps(raw_inbound, sort_keys=True) == before
    if endpoint_available:
        assert result == ("hysteria-client", 1_800_000_000_000)
        client_payload = posts[0][1]["client"] if operation == "add" else posts[0][1]
        assert client_payload["id"] == "hysteria-client"
        assert client_payload["subId"] == "same-sub"
        assert client_payload["auth"] == "synthetic-auth"
        assert client_payload["password"] == "synthetic-auth"
        assert client_payload["totalGB"] == 0
        assert client_payload["reset"] == 0
        assert "streamSettings" not in client_payload
        if operation == "update":
            assert client_payload["futureClientField"] == {"preserve": True}
    else:
        assert result == (None, None)


def test_add_client_404_client_api_preserves_whitelist_transport_and_quota(monkeypatch):
    from shop_bot.modules import xui_api

    stream_settings_payload = {
        "network": "ws", "security": "none",
        "wsSettings": {"path": "/synthetic-tunnel", "host": "cdn.example.invalid", "headers": {},
                       "acceptProxyProtocol": False},
        "externalProxy": [{"dest": "cdn.example.invalid", "port": 443, "sni": "cdn.example.invalid",
                           "forceTls": "tls", "fingerprint": "chrome", "remark": ""}],
        "futureTransportOption": {"preserve": True},
    }
    stream_settings = json.dumps(stream_settings_payload, separators=(",", ":"))
    settings = json.dumps({"clients": []})
    raw_inbound = {"id": 1, "port": 443, "protocol": "vless", "remark": "WHITELIST1", "enable": True,
                   "settings": settings, "streamSettings": stream_settings, "sniffing": "{}"}
    posts = []

    class _ClientOnlyInboundApi(_InboundApi):
        def _post(self, endpoint, _headers, data):
            posts.append((endpoint, data))
            assert endpoint == "panel/api/clients/add"
            return _Response({"success": True})

    class _ClientApi:
        @staticmethod
        def add(_inbound_id, _clients):
            exc = HTTPError("404 Client Error: Not Found")
            exc.response = SimpleNamespace(status_code=404)
            raise exc

    inbound_api = _ClientOnlyInboundApi({"panel/api/inbounds/get/1": {"success": True, "obj": raw_inbound}})
    monkeypatch.setattr(xui_api.uuid, "uuid4", lambda: "client-new")
    result = xui_api.update_or_create_client_on_panel(
        api=SimpleNamespace(inbound=inbound_api, client=_ClientApi()),
        inbound_id=1, email="synthetic-whitelist@vpn.bot", days_to_add=0,
        target_expiry_ms=1_800_000_000_000, target_sub_id="sub-test",
        total_gb_bytes=xui_api.MULTI_NODE_LIMITED_TRAFFIC_BYTES,
    )

    assert result == ("client-new", 1_800_000_000_000)
    assert len(posts) == 1
    assert posts[0][0] == "panel/api/clients/add"
    payload = posts[0][1]
    assert payload["inboundIds"] == [1]
    assert payload["client"]["totalGB"] == 40 * 1024**3
    assert payload["client"]["reset"] == 30
    assert payload["client"]["subId"] == "sub-test"
    assert payload["client"]["flow"] == ""
    assert "streamSettings" not in payload and "streamSettings" not in payload["client"]
    assert raw_inbound["settings"] == settings
    assert raw_inbound["streamSettings"] == stream_settings


def test_raw_inbound_update_payload_strips_runtime_only_client_fields():
    from shop_bot.modules import xui_api

    payload = xui_api._raw_inbound_update_payload(
        {
            "remark": "GERMANY",
            "enable": True,
            "listen": "",
            "port": 443,
            "protocol": "vless",
            "expiryTime": 0,
            "settings": json.dumps({"clients": [], "decryption": "none"}),
            "streamSettings": "{}",
            "sniffing": "{}",
        },
        [
            {
                "id": "client-1",
                "email": "test3inb-main@vpn.bot",
                "enable": True,
                "expiry_time": 1_800_000_000_000,
                "sub_id": "sub-test",
                "total_gb": 123456,
                "limit_ip": 0,
                "tg_id": "777",
                "comment": "keep me",
                "created_at": "2026-06-09T09:00:00Z",
                "updated_at": "2026-06-09T09:01:00Z",
                "up": 11,
                "down": 22,
                "total": 33,
                "inbound_id": 4,
            }
        ],
    )

    client = json.loads(payload["settings"])["clients"][0]
    assert client == {
        "id": "client-1",
        "email": "test3inb-main@vpn.bot",
        "enable": True,
        "expiryTime": 1_800_000_000_000,
        "subId": "sub-test",
        "totalGB": 123456,
        "limitIp": 0,
        "tgId": "777",
        "comment": "keep me",
    }


def test_update_client_404_uses_exact_central_identity_without_inbound_mutation():
    from shop_bot.modules import xui_api

    settings = json.dumps({"clients": [{
        "id": "client-1", "email": "synthetic@vpn.bot", "enable": False,
        "expiryTime": 1_700_000_000_000, "subId": "same-sub", "inboundId": 1,
    }], "decryption": "none"})
    stream_settings = json.dumps({
        "network": "ws", "security": "none",
        "wsSettings": {"path": "/synthetic-tunnel", "host": "cdn.example.invalid"},
        "externalProxy": [{"dest": "cdn.example.invalid", "port": 443}],
        "futureTransportOption": {"preserve": True},
    }, separators=(",", ":"))
    raw_inbound = {"id": 1, "port": 443, "protocol": "vless", "remark": "WHITELIST1", "enable": True,
                   "settings": settings, "streamSettings": stream_settings, "sniffing": "{}"}
    central_client = {
        "id": 71, "uuid": "client-1", "email": "synthetic@vpn.bot", "enable": False,
        "inboundIds": [1, 2], "expiryTime": 1_700_000_000_000, "subId": "same-sub",
        "comment": "preserve annotation", "futureClientField": {"preserve": True},
        "traffic": {"up": 10, "down": 20},
    }
    posts = []

    class _ClientOnlyInboundApi(_InboundApi):
        def _post(self, endpoint, _headers, data):
            posts.append((endpoint, data))
            assert endpoint == "panel/api/clients/update/synthetic%40vpn.bot?inboundIds=1"
            return _Response({"success": True})

    class _ClientApi:
        @staticmethod
        def update(_client_uuid, _client):
            exc = HTTPError("404 Client Error: Not Found")
            exc.response = SimpleNamespace(status_code=404)
            raise exc

    inbound_api = _ClientOnlyInboundApi({
        "panel/api/inbounds/get/1": {"success": True, "obj": raw_inbound},
        "panel/api/clients/list": {"success": True, "obj": [central_client]},
    })
    result = xui_api.update_or_create_client_on_panel(
        api=SimpleNamespace(inbound=inbound_api, client=_ClientApi()),
        inbound_id=1, email="synthetic@vpn.bot", days_to_add=0,
        target_expiry_ms=1_800_000_000_000, target_sub_id="same-sub", target_client_uuid="client-1",
    )

    assert result == ("client-1", 1_800_000_000_000)
    assert inbound_api.calls == ["panel/api/inbounds/get/1", "panel/api/clients/list"]
    assert len(posts) == 1
    payload = posts[0][1]
    assert payload["id"] == "client-1"
    assert payload["email"] == "synthetic@vpn.bot"
    assert payload["enable"] is True
    assert payload["expiryTime"] == 1_800_000_000_000
    assert payload["subId"] == "same-sub"
    assert payload["inboundIds"] == [1, 2]
    assert payload["futureClientField"] == {"preserve": True}
    assert "traffic" not in payload and "streamSettings" not in payload
    assert raw_inbound["settings"] == settings
    assert raw_inbound["streamSettings"] == stream_settings
