from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest


@pytest.fixture
def panel(monkeypatch):
    from shop_bot.modules import xui_api

    class Panel:
        host_name = "synthetic-panel"
        stored = {"xui_client_uuid": "fixture-client", "panel_sub_id": "public-fixture"}
        failures = set()

        def __init__(self):
            self.events = []
            self.writes = []
            self.rows = {
                1: self.row(1, "PRIMARY", 443, "user1@vpn.bot", "public-fixture"),
                2: self.row(2, "SECONDARY", 8443, "user1-secondary-2@vpn.bot", "component-fixture"),
            }
            self.host = {"host_url": "https://panel.example", "host_username": "fixture",
                         "host_pass": "fixture", "host_inbound_id": 1,
                         "host_subscription_base_url": "https://subscription.example/sub/"}
            self.api = SimpleNamespace(
                inbound=SimpleNamespace(_url=lambda endpoint: endpoint, _get=self.get, _post=self.post),
                client=SimpleNamespace(), database=SimpleNamespace(), server=SimpleNamespace(),
            )

        @staticmethod
        def row(inbound_id, label, port, email, sub_id):
            return {"id": inbound_id, "remark": label, "port": port, "protocol": "vless", "enable": True,
                    "settings": {"clients": [{"id": "fixture-client", "email": email, "subId": sub_id,
                        "enable": True, "expiryTime": 1_800_000_000_000}]},
                    "streamSettings": {"network": "tcp", "security": "reality"}}

        def get(self, endpoint, _headers, **_kwargs):
            if endpoint.startswith("panel/api/inbounds/get/"):
                inbound_id = int(endpoint.rsplit("/", 1)[1])
                self.events.append(("read", inbound_id))
                if inbound_id in self.failures:
                    raise RuntimeError("synthetic read failure")
                payload = copy.deepcopy(self.rows[inbound_id])
            elif endpoint.startswith("panel/api/inbounds/"):
                payload = copy.deepcopy(list(self.rows.values()))
            else:
                payload = []
            return SimpleNamespace(json=lambda: {"success": True, "obj": payload})

        def post(self, *_args, **_kwargs):
            raise AssertionError("Identity validation must not detach or repair clients")

        def upsert(self, _api, inbound_id, email, _days, **kwargs):
            self.events.append(("write", inbound_id))
            self.writes.append({"inbound_id": inbound_id, "email": email, **kwargs})
            clients = self.rows[inbound_id]["settings"]["clients"]
            existing = next((row for row in clients if row["email"] == email), None)
            if existing is None:
                existing = {}
                clients.append(existing)
            existing.update(id=kwargs["target_client_uuid"], email=email, subId=kwargs["target_sub_id"],
                            expiryTime=kwargs["target_expiry_ms"], enable=True,
                            totalGB=kwargs["total_gb_bytes"], reset=kwargs["traffic_reset_days"] or 0)
            if kwargs.get("result_metadata") is not None:
                kwargs["result_metadata"]["sub_id"] = existing["subId"]
            return existing["id"], existing["expiryTime"]

        def run(self, **kwargs):
            context = {}
            result = xui_api._create_or_update_key_on_host_once(
                host_name=self.host_name, host_data=self.host, email="user1@vpn.bot", days_to_add=30,
                target_expiry_ms=1_800_000_000_000, failure_context=context, **kwargs,
            )
            return result, context

    instance = Panel()
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (
        instance.api, SimpleNamespace(id=1, remark="PRIMARY", protocol="vless", enable=True, port=443)))
    monkeypatch.setattr(xui_api, "get_host", lambda _name: instance.host)
    monkeypatch.setattr(xui_api, "get_setting", lambda _name: None)
    monkeypatch.setattr(xui_api, "get_all_hosts", lambda: [])
    monkeypatch.setattr(xui_api, "get_key_by_email", lambda _email: instance.stored)
    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", instance.upsert)
    monkeypatch.setattr(xui_api, "_get_subscription_links_with_retry", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(xui_api, "get_connection_string", lambda inbound, client_uuid, _host, remark, **_kwargs:
        f"vless://{client_uuid}@panel.example:{inbound.port}?type=tcp#{remark}")
    monkeypatch.setattr(xui_api.time, "time", lambda: 1_790_000_000)
    monkeypatch.setattr(xui_api.time, "sleep", lambda _seconds: None)
    return instance


def test_first_creation_then_retry_preserves_all_generated_identities(panel):
    panel.stored = {}
    for row in panel.rows.values():
        row["settings"]["clients"] = []
    first, context = panel.run()
    assert first is not None, context
    first_rows = {key: copy.deepcopy(row["settings"]["clients"][0]) for key, row in panel.rows.items()}
    assert first_rows[1]["id"] == first_rows[2]["id"]
    assert first_rows[1]["subId"] != first_rows[2]["subId"]
    assert first["sub_id"] == first_rows[1]["subId"]
    first_write = next(index for index, event in enumerate(panel.events) if event[0] == "write")
    assert {event[1] for event in panel.events[:first_write]} == {1, 2}

    second, context = panel.run()
    assert second is not None, context
    assert second["subscription_link"] == first["subscription_link"]
    for inbound_id, expected in first_rows.items():
        actual = panel.rows[inbound_id]["settings"]["clients"][0]
        assert (actual["id"], actual["subId"]) == (expected["id"], expected["subId"])


def test_missing_primary_uses_verified_database_public_identity(panel):
    panel.rows[1]["settings"]["clients"] = []
    result, context = panel.run()
    assert result is not None, context
    assert result["sub_id"] == "public-fixture"
    assert panel.writes[0]["target_client_uuid"] == "fixture-client"
    assert panel.writes[1]["target_sub_id"] == "component-fixture"


@pytest.mark.parametrize("case, reason", [
    ("read_failure", "subscription_identity_unavailable"),
    ("malformed_json", "subscription_identity_malformed"),
    ("invalid_row", "subscription_identity_malformed"),
    ("ambiguous", "subscription_identity_ambiguous"),
    ("missing_component_id", "subscription_identity_missing_sub_id"),
    ("legacy_shared", "subscription_identity_repair_required"),
    ("foreign_uuid", "subscription_identity_conflict"),
    ("uuid_alias_conflict", "subscription_identity_malformed"),
    ("sub_id_alias_conflict", "subscription_identity_malformed"),
    ("structured_sub_id", "subscription_identity_malformed"),
    ("missing_public_id", "subscription_identity_public_id_unavailable"),
])
def test_uncertain_or_conflicting_identity_prevents_every_write(panel, case, reason):
    row = panel.rows[2]["settings"]["clients"][0]
    if case == "read_failure":
        panel.failures = {2}
    elif case == "malformed_json":
        panel.rows[2]["settings"] = '{"clients": []} trailing-data'
    elif case == "invalid_row":
        panel.rows[2]["settings"]["clients"].append("not-a-client")
    elif case == "ambiguous":
        panel.rows[2]["settings"]["clients"].append(copy.deepcopy(row))
    elif case == "missing_component_id":
        row["subId"] = ""
    elif case == "legacy_shared":
        row["subId"] = "public-fixture"
    elif case == "foreign_uuid":
        row["id"] = "foreign-client"
    elif case == "uuid_alias_conflict":
        row["uuid"] = "foreign-client"
    elif case == "sub_id_alias_conflict":
        row["sub_id"] = "foreign-component"
    elif case == "structured_sub_id":
        row["subId"] = {"not": "an-id"}
    elif case == "missing_public_id":
        panel.rows[1]["settings"]["clients"] = []
        panel.stored = {}
    result, context = panel.run()
    assert result is None
    assert context["reason"] == reason
    assert panel.writes == []


@pytest.mark.parametrize("requested", [
    {"target_client_uuid": "different-client"},
    {"target_sub_id": "different-public-id"},
])
def test_requested_identity_cannot_replace_stored_identity(panel, requested):
    result, context = panel.run(**requested)
    assert result is None
    assert context["reason"] == "subscription_identity_conflict"
    assert panel.writes == []


def _migrate_secondary(panel, monkeypatch, *, settle=False):
    from shop_bot.modules import xui_api
    panel.host_name = "MainPanel"
    row = panel.rows.pop(2)
    row.update(id=25, remark="WHITELIST2")
    row["settings"]["clients"][0]["email"] = "user1-oldwhite-21@vpn.bot"
    panel.rows[25] = row
    monkeypatch.setattr(xui_api.subscription_policy, "inbound_migrations", lambda _host: [
        {"old_inbound_id": 21, "new_inbound_id": 25, "old_label": "OLDWHITE", "new_label": "WHITELIST2",
         "client_profile": "clearway" if settle else None},
    ])
    monkeypatch.setattr(xui_api, "_wait_for_whitelist2_write_settle", lambda *_args, **_kwargs: True)


@pytest.mark.parametrize("settle", [False, True])
def test_migrated_component_retains_its_identity_and_uses_mapped_email(panel, monkeypatch, settle):
    _migrate_secondary(panel, monkeypatch, settle=settle)
    result, context = panel.run()
    assert result is not None, context
    assert panel.writes[1]["email"] == "user1-oldwhite-21@vpn.bot"
    assert panel.writes[1]["target_sub_id"] == "component-fixture"
    assert result["sub_id"] == "public-fixture"


def test_migrated_snapshot_baseline_is_checked_before_primary_write(panel, monkeypatch):
    _migrate_secondary(panel, monkeypatch, settle=True)
    panel.rows[25]["settings"]["clients"].append({
        "id": "unrelated-client", "email": "another@example.invalid", "subId": "",
    })
    result, context = panel.run()
    assert result is None
    assert context["reason"] == "subscription_identity_malformed"
    assert panel.writes == []


def test_outer_timeout_never_cleans_up_guessed_subscription(panel, monkeypatch):
    from shop_bot.modules import xui_api
    panel.stored = None
    cleanup = []

    async def timeout(*_args, **_kwargs):
        raise TimeoutError("panel_call_timeout")

    async def unsafe_cleanup(**kwargs):
        cleanup.append(kwargs)
        return 1

    monkeypatch.setattr(xui_api, "_run_blocking_with_timeout", timeout)
    monkeypatch.setattr(xui_api, "_cleanup_failed_subscription_attempt_on_host", unsafe_cleanup)
    assert asyncio.run(xui_api.create_or_update_key_on_host(panel.host_name, "user1@vpn.bot", 30)) is None
    assert cleanup == []
    assert panel.writes == []
