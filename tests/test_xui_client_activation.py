from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def _build_existing_client(*, xui_api, enabled: bool = False):
    return xui_api.Client(
        id="client-1",
        email="user1-key1-trial-super@telegram.bot",
        enable=enabled,
        flow="xtls-rprx-vision",
        expiry_time=1_700_000_000_000,
    )


def test_update_existing_client_uses_client_update_endpoint():
    from shop_bot.modules import xui_api

    existing_client = _build_existing_client(xui_api=xui_api, enabled=False)
    inbound_obj = SimpleNamespace(id=11, settings=SimpleNamespace(clients=[existing_client]))

    calls = {"client_update": 0, "inbound_update": 0}

    class _InboundApi:
        def get_by_id(self, _inbound_id: int):
            return inbound_obj

        def update(self, _inbound_id: int, _inbound):
            calls["inbound_update"] += 1

    class _ClientApi:
        def update(self, client_uuid: str, client):
            calls["client_update"] += 1
            assert client_uuid == "client-1"
            assert bool(client.enable) is True
            assert int(client.expiry_time) == 1_800_000_000_000
            assert int(client.inbound_id) == 11

    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=11,
        email="user1-key1-trial-super@telegram.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
    )

    assert client_uuid == "client-1"
    assert int(expiry_ms or 0) == 1_800_000_000_000
    assert calls["client_update"] == 1
    assert calls["inbound_update"] == 0


def test_update_existing_client_fails_closed_on_non_404_client_api_error():
    from shop_bot.modules import xui_api

    existing_client = _build_existing_client(xui_api=xui_api, enabled=False)
    inbound_obj = SimpleNamespace(id=11, settings=SimpleNamespace(clients=[existing_client]))

    calls = {"client_update": 0, "inbound_update": 0}

    class _InboundApi:
        def get_by_id(self, _inbound_id: int):
            return inbound_obj

        def update(self, _inbound_id: int, inbound):
            calls["inbound_update"] += 1
            patched = inbound.settings.clients[0]
            assert bool(patched.enable) is True
            assert int(patched.expiry_time) == 1_800_000_000_000

    class _ClientApi:
        def update(self, _client_uuid: str, _client):
            calls["client_update"] += 1
            raise RuntimeError("updateClient temporary error")

    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=11,
        email="user1-key1-trial-super@telegram.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
    )

    assert client_uuid is None
    assert expiry_ms is None
    assert calls["client_update"] == 1
    assert calls["inbound_update"] == 0


def test_add_new_client_uses_add_client_endpoint(monkeypatch: pytest.MonkeyPatch):
    from shop_bot.modules import xui_api

    inbound_obj = SimpleNamespace(id=11, settings=SimpleNamespace(clients=[]))
    calls = {"client_add": 0, "inbound_update": 0}
    monkeypatch.setattr(xui_api.uuid, "uuid4", lambda: "client-new")

    class _InboundApi:
        def get_by_id(self, _inbound_id: int):
            return inbound_obj

        def update(self, _inbound_id: int, _inbound):
            calls["inbound_update"] += 1

    class _ClientApi:
        def add(self, inbound_id: int, clients):
            calls["client_add"] += 1
            assert inbound_id == 11
            assert len(clients) == 1
            assert clients[0].id == "client-new"
            assert bool(clients[0].enable) is True
            assert int(clients[0].inbound_id) == 11

    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=11,
        email="user1-key2@vpn.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
    )

    assert client_uuid == "client-new"
    assert int(expiry_ms or 0) == 1_800_000_000_000
    assert calls["client_add"] == 1
    assert calls["inbound_update"] == 0


def test_update_existing_client_populates_missing_sub_id():
    from shop_bot.modules import xui_api

    existing_client = _build_existing_client(xui_api=xui_api, enabled=False)
    existing_client.sub_id = ""
    inbound_obj = SimpleNamespace(id=11, settings=SimpleNamespace(clients=[existing_client]))

    captured: dict[str, str] = {}

    class _InboundApi:
        def get_by_id(self, _inbound_id: int):
            return inbound_obj

        def update(self, _inbound_id: int, _inbound):
            raise AssertionError

    class _ClientApi:
        def update(self, client_uuid: str, client):
            assert client_uuid == "client-1"
            sub_id = str(getattr(client, "sub_id", "") or "")
            assert sub_id
            captured["sub_id"] = sub_id

    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    _client_uuid, _expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=11,
        email="user1-key1-trial-super@telegram.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
    )

    assert captured["sub_id"]


def test_add_new_client_sets_sub_id(monkeypatch: pytest.MonkeyPatch):
    from shop_bot.modules import xui_api

    inbound_obj = SimpleNamespace(id=11, settings=SimpleNamespace(clients=[]))
    monkeypatch.setattr(xui_api.uuid, "uuid4", lambda: "client-new")

    captured: dict[str, str] = {}

    class _InboundApi:
        def get_by_id(self, _inbound_id: int):
            return inbound_obj

        def update(self, _inbound_id: int, _inbound):
            raise AssertionError

    class _ClientApi:
        def add(self, inbound_id: int, clients):
            assert inbound_id == 11
            assert len(clients) == 1
            client = clients[0]
            sub_id = str(getattr(client, "sub_id", "") or "")
            assert sub_id
            captured["sub_id"] = sub_id

    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    _client_uuid, _expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=11,
        email="user1-key2@vpn.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
    )

    assert captured["sub_id"] == "clientnew"


def test_add_new_client_accepts_forced_sub_id_and_traffic_limit(monkeypatch: pytest.MonkeyPatch):
    from shop_bot.modules import xui_api

    inbound_obj = SimpleNamespace(id=11, settings=SimpleNamespace(clients=[]))
    monkeypatch.setattr(xui_api.uuid, "uuid4", lambda: "client-new")

    captured: dict[str, object] = {}

    class _InboundApi:
        def get_by_id(self, _inbound_id: int):
            return inbound_obj

        def update(self, _inbound_id: int, _inbound):
            raise AssertionError

    class _ClientApi:
        def add(self, inbound_id: int, clients):
            assert inbound_id == 11
            client = clients[0]
            captured["sub_id"] = str(getattr(client, "sub_id", "") or "")
            captured["reset"] = int(getattr(client, "reset", 0) or 0)
            captured["total_gb"] = int(getattr(client, "total_gb", 0) or 0)

    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    _client_uuid, _expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=11,
        email="user1-key2@vpn.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="same-sub",
        total_gb_bytes=30 * 1024 * 1024 * 1024,
    )

    assert captured == {
        "sub_id": "same-sub",
        "reset": 30,
        "total_gb": 30 * 1024 * 1024 * 1024,
    }


def test_update_existing_client_applies_monthly_reset_for_traffic_limit():
    from shop_bot.modules import xui_api

    existing_client = _build_existing_client(xui_api=xui_api, enabled=False)
    existing_client.reset = 0
    existing_client.total_gb = 0
    inbound_obj = SimpleNamespace(id=11, settings=SimpleNamespace(clients=[existing_client]))
    captured: dict[str, int] = {}

    class _InboundApi:
        def get_by_id(self, _inbound_id: int):
            return inbound_obj

        def update(self, _inbound_id: int, _inbound):
            raise AssertionError

    class _ClientApi:
        def update(self, _client_uuid: str, client):
            captured["reset"] = int(getattr(client, "reset", 0) or 0)
            captured["total_gb"] = int(getattr(client, "total_gb", 0) or 0)

    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    _client_uuid, _expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=11,
        email="user1-key1-trial-super@telegram.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
        total_gb_bytes=15 * 1024 * 1024 * 1024,
    )

    assert captured == {
        "reset": 30,
        "total_gb": 15 * 1024 * 1024 * 1024,
    }


def test_add_new_client_accepts_shared_uuid_for_multi_node_subscription():
    from shop_bot.modules import xui_api

    inbound_obj = SimpleNamespace(id=11, settings=SimpleNamespace(clients=[]))

    class _InboundApi:
        def get_by_id(self, _inbound_id: int):
            return inbound_obj

        def update(self, _inbound_id: int, _inbound):
            raise AssertionError

    class _ClientApi:
        def add(self, inbound_id: int, clients):
            assert inbound_id == 11
            assert clients[0].id == "shared-uuid"

    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    client_uuid, _expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=11,
        email="user12345-key1-white2-2@vpn.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="same-sub",
        target_client_uuid="shared-uuid",
    )

    assert client_uuid == "shared-uuid"


def test_update_existing_client_matches_by_uuid_when_email_changed():
    from shop_bot.modules import xui_api

    existing_client = xui_api.Client(
        id="shared-uuid",
        email="old-email@vpn.bot",
        enable=False,
        flow="xtls-rprx-vision",
        expiry_time=1_700_000_000_000,
    )
    inbound_obj = SimpleNamespace(id=11, settings=SimpleNamespace(clients=[existing_client]))
    captured: dict[str, object] = {}

    class _InboundApi:
        def get_by_id(self, _inbound_id: int):
            return inbound_obj

        def update(self, _inbound_id: int, _inbound):
            raise AssertionError

    class _ClientApi:
        def update(self, client_uuid: str, client):
            captured["update_uuid"] = client_uuid
            captured["email"] = client.email

        def add(self, _inbound_id: int, _clients: list[object]):
            raise AssertionError

    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=11,
        email="new-email@vpn.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
        target_client_uuid="shared-uuid",
    )

    assert client_uuid == "shared-uuid"
    assert int(expiry_ms or 0) == 1_800_000_000_000
    assert captured == {"update_uuid": "shared-uuid", "email": "new-email@vpn.bot"}


def test_update_or_create_client_on_panel_refuses_conflicting_email_identity():
    from shop_bot.modules import xui_api

    canonical_client = xui_api.Client(
        id="shared-uuid",
        email="old-email@vpn.bot",
        enable=True,
        flow="xtls-rprx-vision",
        expiry_time=1_700_000_000_000,
    )
    duplicate_client = xui_api.Client(
        id="duplicate-uuid",
        email="new-email@vpn.bot",
        enable=True,
        flow="xtls-rprx-vision",
        expiry_time=1_700_000_000_000,
    )
    inbound_obj = SimpleNamespace(
        id=17,
        settings=SimpleNamespace(clients=[canonical_client, duplicate_client]),
    )
    deleted_uuids: list[str] = []
    updated_uuids: list[str] = []

    class _InboundApi:
        def get_by_id(self, _inbound_id: int):
            return inbound_obj

        def update(self, _inbound_id: int, _inbound):
            raise AssertionError

    class _ClientApi:
        def delete(self, inbound_id: int, client_uuid: str):
            assert inbound_id == 17
            deleted_uuids.append(client_uuid)
            inbound_obj.settings.clients = [
                client for client in inbound_obj.settings.clients
                if str(getattr(client, "id", "")) != client_uuid
            ]

        def update(self, client_uuid: str, _client: object):
            updated_uuids.append(client_uuid)

        def add(self, _inbound_id: int, _clients: list[object]):
            raise AssertionError

    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    client_uuid, _expiry_ms = xui_api.update_or_create_client_on_panel(
        api=api,
        inbound_id=17,
        email="new-email@vpn.bot",
        days_to_add=30,
        target_expiry_ms=1_800_000_000_000,
        target_client_uuid="shared-uuid",
    )

    # An email owned by another UUID is an identity conflict, not a duplicate
    # that normal issuance may delete. Fail without modifying either client.
    assert client_uuid is None
    assert _expiry_ms is None
    assert deleted_uuids == []
    assert updated_uuids == []
    assert inbound_obj.settings.clients == [canonical_client, duplicate_client]
    assert canonical_client.email == "old-email@vpn.bot"
    assert duplicate_client.email == "new-email@vpn.bot"
    assert canonical_client.expiry_time == 1_700_000_000_000
    assert duplicate_client.expiry_time == 1_700_000_000_000
