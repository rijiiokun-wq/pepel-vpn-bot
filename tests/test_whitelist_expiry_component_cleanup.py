from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_subscription_email_variant_is_scoped_to_exact_key_prefix():
    from shop_bot.modules import xui_api

    base = "user42-key3@vpn.bot"

    assert xui_api._subscription_email_variant(base, base)
    assert xui_api._subscription_email_variant(base, "user42-key3-whitelist1-2@vpn.bot")
    assert not xui_api._subscription_email_variant(base, "user42-key30-whitelist1-2@vpn.bot")
    assert not xui_api._subscription_email_variant(base, "user42-key3-whitelist1-2@other.test")


def test_expiry_cleanup_deletes_and_verifies_every_subscription_component(monkeypatch):
    from shop_bot.modules import xui_api

    base_email = "user42-key3@vpn.bot"
    client_uuid = "00000000-0000-4000-8000-000000000001"
    panel_sub_id = "managed-sub"
    components = {
        1: [
            {"id": client_uuid, "email": base_email, "subId": panel_sub_id},
            {"id": "foreign", "email": "user42-key30@vpn.bot", "subId": "foreign-sub"},
        ],
        2: [
            {
                "id": client_uuid,
                "email": "user42-key3-whitelist1-2@vpn.bot",
                "subId": "white-sub",
            }
        ],
        3: [
            {
                "id": client_uuid,
                "email": "user42-key3-estonia2-3@vpn.bot",
                "subId": "ordinary-sub",
            }
        ],
    }
    raw_inbounds = [
        {"id": inbound_id, "remark": f"inbound-{inbound_id}", "protocol": "vless", "enable": True}
        for inbound_id in components
    ]
    detached: list[tuple[int, str]] = []
    deleted: list[str] = []
    central_clients = [
        {**client, "id": index, "uuid": client_uuid}
        for index, client in enumerate(
            (
                client
                for inbound_clients in components.values()
                for client in inbound_clients
                if client["id"] == client_uuid
            ),
            start=100,
        )
    ]

    monkeypatch.setattr(
        xui_api,
        "login_to_host",
        lambda **_kwargs: (SimpleNamespace(), SimpleNamespace(id=1)),
    )
    monkeypatch.setattr(xui_api, "_list_inbounds_raw", lambda _api: raw_inbounds)
    monkeypatch.setattr(
        xui_api,
        "_api_get_json",
        lambda *_args, **_kwargs: {"obj": list(central_clients)},
    )

    def _raw(_api, inbound_id, **_kwargs):
        return {
            "id": inbound_id,
            "settings": json.dumps({"clients": components[inbound_id]}),
        }

    def _detach(_api, inbound_id, email):
        detached.append((inbound_id, email))
        components[inbound_id] = [client for client in components[inbound_id] if client["email"] != email]
        return True

    monkeypatch.setattr(xui_api, "_get_inbound_raw_by_id", _raw)
    monkeypatch.setattr(xui_api, "_detach_client_from_inbound_by_email", _detach)

    def _delete(_api, email):
        deleted.append(email)
        central_clients[:] = [client for client in central_clients if client["email"] != email]
        return True

    monkeypatch.setattr(xui_api, "_delete_detached_client_by_email", _delete)

    result = xui_api._delete_subscription_components_on_host_once(
        host_name="MainPanel",
        host_data={
            "host_url": "https://panel.example.test",
            "host_username": "admin",
            "host_pass": "secret",
            "host_inbound_id": 1,
        },
        client_email=base_email,
        client_uuid=client_uuid,
        panel_sub_id=panel_sub_id,
    )

    assert result is True
    assert detached == [
        (1, base_email),
        (2, "user42-key3-whitelist1-2@vpn.bot"),
        (3, "user42-key3-estonia2-3@vpn.bot"),
    ]
    assert deleted == sorted(email for _inbound_id, email in detached)
    assert components[1] == [{"id": "foreign", "email": "user42-key30@vpn.bot", "subId": "foreign-sub"}]


def test_expiry_cleanup_fails_closed_on_ambiguous_component(monkeypatch):
    from shop_bot.modules import xui_api

    base_email = "user42-key3@vpn.bot"
    client_uuid = "00000000-0000-4000-8000-000000000001"
    raw_clients = [
        {"id": client_uuid, "email": base_email, "subId": "sub-1"},
        {"id": client_uuid, "email": "user42-key3-duplicate@vpn.bot", "subId": "sub-2"},
    ]
    monkeypatch.setattr(
        xui_api,
        "login_to_host",
        lambda **_kwargs: (SimpleNamespace(), SimpleNamespace(id=1)),
    )
    monkeypatch.setattr(
        xui_api,
        "_list_inbounds_raw",
        lambda _api: [{"id": 1, "remark": "WHITELIST1", "protocol": "vless", "enable": True}],
    )
    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda *_args, **_kwargs: {"id": 1, "settings": json.dumps({"clients": raw_clients})},
    )
    monkeypatch.setattr(
        xui_api,
        "_api_get_json",
        lambda *_args, **_kwargs: {"obj": list(raw_clients)},
    )
    monkeypatch.setattr(
        xui_api,
        "_detach_client_from_inbound_by_email",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not mutate")),
    )

    assert not xui_api._delete_subscription_components_on_host_once(
        host_name="MainPanel",
        host_data={
            "host_url": "https://panel.example.test",
            "host_username": "admin",
            "host_pass": "secret",
            "host_inbound_id": 1,
        },
        client_email=base_email,
        client_uuid=client_uuid,
        panel_sub_id="sub-1",
    )


def test_expiry_cleanup_deletes_detached_central_orphan_by_exact_uuid(monkeypatch):
    from shop_bot.modules import xui_api

    client_uuid = "00000000-0000-4000-8000-000000000001"
    central_clients = [
        {
            "id": 123,
            "uuid": client_uuid,
            "email": "unexpected-orphan@vpn.bot",
            "subId": "orphan-sub",
            "inboundIds": [],
        }
    ]
    deleted = []
    monkeypatch.setattr(
        xui_api,
        "login_to_host",
        lambda **_kwargs: (SimpleNamespace(), SimpleNamespace(id=1)),
    )
    monkeypatch.setattr(
        xui_api,
        "_list_inbounds_raw",
        lambda _api: [{"id": 1, "remark": "ESTONIA2", "protocol": "vless"}],
    )
    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda *_args, **_kwargs: {"id": 1, "settings": json.dumps({"clients": []})},
    )
    monkeypatch.setattr(
        xui_api,
        "_api_get_json",
        lambda *_args, **_kwargs: {"obj": list(central_clients)},
    )

    def _delete(_api, email):
        deleted.append(email)
        central_clients.clear()
        return True

    monkeypatch.setattr(xui_api, "_delete_detached_client_by_email", _delete)

    assert xui_api._delete_subscription_components_on_host_once(
        host_name="MainPanel",
        host_data={
            "host_url": "https://panel.example.test",
            "host_username": "admin",
            "host_pass": "secret",
            "host_inbound_id": 1,
        },
        client_email="user42-key3@vpn.bot",
        client_uuid=client_uuid,
        panel_sub_id="managed-sub",
    )
    assert deleted == ["unexpected-orphan@vpn.bot"]


def test_expiry_cleanup_refuses_exact_email_with_conflicting_uuid(monkeypatch):
    from shop_bot.modules import xui_api

    base_email = "user42-key3@vpn.bot"
    monkeypatch.setattr(
        xui_api,
        "login_to_host",
        lambda **_kwargs: (SimpleNamespace(), SimpleNamespace(id=1)),
    )
    monkeypatch.setattr(
        xui_api,
        "_list_inbounds_raw",
        lambda _api: [{"id": 1, "remark": "ESTONIA2", "protocol": "vless"}],
    )
    monkeypatch.setattr(
        xui_api,
        "_api_get_json",
        lambda *_args, **_kwargs: {"obj": [{"uuid": "foreign-uuid", "email": base_email, "subId": "foreign-sub"}]},
    )

    assert not xui_api._delete_subscription_components_on_host_once(
        host_name="MainPanel",
        host_data={
            "host_url": "https://panel.example.test",
            "host_username": "admin",
            "host_pass": "secret",
            "host_inbound_id": 1,
        },
        client_email=base_email,
        client_uuid="expected-uuid",
        panel_sub_id="managed-sub",
    )
