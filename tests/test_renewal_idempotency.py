from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    from shop_bot.data_manager import database

    db_file = tmp_path / "renewal.db"
    monkeypatch.setattr(database, "DB_FILE", db_file)
    monkeypatch.setattr(database, "_WAL_INITIALIZED", False)
    database.initialize_db()
    return db_file


def test_payment_accounting_is_applied_once(temp_db):
    from shop_bot.data_manager import database

    user_id = 81001
    referrer_id = 81002
    database.update_setting("enable_referrals", "true")
    database.update_setting("referral_percentage", "10")
    database.register_user_if_not_exists(referrer_id, "referrer", None)
    database.register_user_if_not_exists(user_id, "buyer", referrer_id)

    first = database.apply_payment_accounting_once(
        "renewal-accounting-001",
        user_id=user_id,
        amount_rub=600.0,
        months=1,
        referrer_id=referrer_id,
        referral_reward=60.0,
        activate_super=True,
    )
    second = database.apply_payment_accounting_once(
        "renewal-accounting-001",
        user_id=user_id,
        amount_rub=600.0,
        months=1,
        referrer_id=referrer_id,
        referral_reward=60.0,
        activate_super=True,
    )

    buyer = database.get_user(user_id)
    referrer = database.get_user(referrer_id)
    assert first is True
    assert second is False
    assert database.is_payment_accounting_applied("renewal-accounting-001") is True
    assert buyer["total_spent"] == 600.0
    assert buyer["total_months"] == 1
    assert buyer["has_active_beta"] == 1
    assert referrer["referral_balance"] == 60.0
    assert referrer["referral_balance_all"] == 60.0
    with sqlite3.connect(temp_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM payment_accounting_effects").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("provider", "candidate_loader"),
    [
        ("YooKassa", "get_pending_yookassa_transactions"),
        ("Platega", "get_pending_platega_transactions"),
        ("PayPear", "get_pending_paypear_transactions"),
    ],
)
def test_provider_reconcile_does_not_bypass_retry_schedule(provider, candidate_loader):
    from shop_bot.data_manager import database

    payment_id = f"renewal-provider-{provider.lower()}"
    metadata = {
        "user_id": 81003,
        "payment_method": provider,
        "payment_confirmed": True,
    }
    database.register_user_if_not_exists(81003, "buyer", None)
    database.create_pending_transaction(payment_id, 81003, 600.0, metadata)
    database.mark_transaction_retryable_failure(
        payment_id,
        reason="duplicate_email",
        metadata=metadata,
        payment_method=provider,
        next_retry_at=datetime(2099, 1, 1, tzinfo=UTC),
        statuses=("pending",),
    )

    candidates = getattr(database, candidate_loader)(limit=20, min_age_seconds=0)
    assert payment_id not in {row["payment_id"] for row in candidates}


def test_extension_readback_accepts_only_matching_complete_subscription(monkeypatch):
    from shop_bot.bot import handlers

    target_expiry_ms = 1_800_000_000_000
    safe_result = {
        "client_uuid": "same-uuid",
        "email": "user81004-key1@vpn.bot",
        "host_name": "MainPanel",
        "expiry_timestamp_ms": target_expiry_ms,
        "subscription_links": [
            "vless://same-uuid@node.example:443?type=tcp&security=reality#FINLAND",
        ],
    }

    async def _by_uuid(**_kwargs):
        return safe_result

    async def _by_email(**_kwargs):
        raise AssertionError("UUID readback already proved the renewal")

    monkeypatch.setattr(handlers.xui_api, "get_existing_key_by_uuid_on_host", _by_uuid)
    monkeypatch.setattr(handlers.xui_api, "get_existing_key_by_email_on_host", _by_email)
    monkeypatch.setattr(
        handlers.xui_api,
        "mainpanel_expected_subscription_labels",
        lambda _host, _tier: ["FINLAND"],
    )

    recovered = asyncio.run(
        handlers._reconcile_extension_after_panel_error(
            host_name="MainPanel",
            email="user81004-key1@vpn.bot",
            client_uuid="same-uuid",
            target_expiry_ms=target_expiry_ms,
            subscription_tier="standard",
        )
    )
    assert recovered == safe_result


def test_extension_readback_rejects_email_collision_with_other_uuid(monkeypatch):
    from shop_bot.bot import handlers

    target_expiry_ms = 1_800_000_000_000

    async def _missing_uuid(**_kwargs):
        return None

    async def _wrong_email_owner(**_kwargs):
        return {
            "client_uuid": "other-uuid",
            "email": "user81005-key1@vpn.bot",
            "host_name": "MainPanel",
            "expiry_timestamp_ms": target_expiry_ms,
            "subscription_links": [
                "vless://other-uuid@node.example:443?type=tcp&security=reality#FINLAND",
            ],
        }

    monkeypatch.setattr(handlers.xui_api, "get_existing_key_by_uuid_on_host", _missing_uuid)
    monkeypatch.setattr(handlers.xui_api, "get_existing_key_by_email_on_host", _wrong_email_owner)
    monkeypatch.setattr(
        handlers.xui_api,
        "mainpanel_expected_subscription_labels",
        lambda _host, _tier: ["FINLAND"],
    )

    recovered = asyncio.run(
        handlers._reconcile_extension_after_panel_error(
            host_name="MainPanel",
            email="user81005-key1@vpn.bot",
            client_uuid="expected-uuid",
            target_expiry_ms=target_expiry_ms,
            subscription_tier="standard",
        )
    )
    assert recovered is None


def test_duplicate_email_update_recovers_through_client_attach(monkeypatch):
    from shop_bot.modules import xui_api

    existing_client = SimpleNamespace(
        email="user81007-key1@vpn.bot",
        id="same-uuid",
        expiry_time=1,
        enable=True,
        inbound_id=7,
        flow="",
        method="",
        password="",
        reset=None,
        limit_ip=0,
        tg_id="",
        comment="",
        sub_id="same-sub",
        total_gb=0,
    )
    inbound = SimpleNamespace(
        id=7,
        protocol="vless",
        stream_settings=SimpleNamespace(network="tcp"),
        settings=SimpleNamespace(clients=[existing_client]),
    )

    def _not_found():
        error = RuntimeError("404")
        error.response = SimpleNamespace(status_code=404)
        return error

    class _InboundApi:
        @staticmethod
        def get_by_id(_inbound_id):
            return inbound

        @staticmethod
        def _url(endpoint):
            return endpoint

        @staticmethod
        def _post(_url, _headers, data):
            del data
            raise ValueError("Something went wrong (Duplicate email: user81007-key1@vpn.bot)")

    class _ClientApi:
        @staticmethod
        def update(_uuid, _client):
            raise _not_found()

    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    recoveries: list[dict] = []

    def _recover(_api, _inbound, email, **kwargs):
        recoveries.append({"email": email, **kwargs})
        return "same-uuid", 1_800_000_000_000

    def _duplicate_client_update(*args, **kwargs):
        raise ValueError("Duplicate email: user81007-key1@vpn.bot")

    monkeypatch.setattr(xui_api, "_api_post_client_update", _duplicate_client_update)
    monkeypatch.setattr(xui_api, "_recover_secondary_client_update_via_clients_api", _recover)
    monkeypatch.setattr(
        xui_api,
        "_finish_client_upsert",
        lambda _api, **kwargs: (kwargs["client_uuid"], kwargs["expiry_ms"]),
    )

    client_uuid, expiry_ms = xui_api.update_or_create_client_on_panel(
        api,
        7,
        "user81007-key1@vpn.bot",
        0,
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="same-sub",
        target_client_uuid="same-uuid",
    )

    assert (client_uuid, expiry_ms) == ("same-uuid", 1_800_000_000_000)
    assert recoveries[0]["email"] == "user81007-key1@vpn.bot"
    assert recoveries[0]["client_uuid"] == "same-uuid"


def test_verified_orphan_is_recreated_without_using_numeric_database_id(monkeypatch):
    from shop_bot.modules import xui_api

    email = "user81008-key1@vpn.bot"
    client_uuid = "same-logical-uuid"
    target_expiry_ms = 1_800_000_000_000
    state: dict[str, object] = {"recreated": False, "deleted": False, "added_client": None}
    node_inbound = SimpleNamespace(
        id=7,
        protocol="vless",
        remark="ORDINARY",
        stream_settings=SimpleNamespace(network="tcp"),
    )

    def _raw_inbound(_api, _inbound_id):
        clients = [state["added_client"]] if state["recreated"] else []
        return {
            "id": 7,
            "protocol": "vless",
            "settings": json.dumps({"clients": clients}),
            "streamSettings": json.dumps({"network": "tcp"}),
        }

    def _get_json(_api, endpoint, *, skip_check=False):
        del skip_check
        assert endpoint == "panel/api/clients/list"
        return {
            "success": True,
            "obj": [
                {
                    "id": 43123,
                    "email": email,
                    "uuid": client_uuid,
                    "subId": "same-sub",
                    "inboundIds": [],
                    "reset": 0,
                }
            ],
        }

    def _post_json(_api, endpoint, payload):
        if endpoint == "panel/api/clients/bulkAttach":
            return {"success": True}
        if endpoint.startswith("panel/api/clients/del/"):
            assert "keepTraffic=1" in endpoint
            state["deleted"] = True
            return {"success": True}
        if endpoint == "panel/api/clients/add":
            added_client = dict(payload["client"])
            assert payload["inboundIds"] == [7]
            assert added_client["id"] == client_uuid
            assert added_client["id"] != "43123"
            assert added_client["subId"] == "same-sub"
            state["added_client"] = added_client
            state["recreated"] = True
            return {"success": True}
        raise AssertionError(endpoint)

    monkeypatch.setattr(xui_api, "_get_inbound_raw_by_id", _raw_inbound)
    monkeypatch.setattr(xui_api, "_api_get_json", _get_json)
    monkeypatch.setattr(xui_api, "_api_post_json", _post_json)

    recovered = xui_api._recover_secondary_client_update_via_clients_api(
        SimpleNamespace(),
        node_inbound,
        email,
        client_uuid=client_uuid,
        target_expiry_ms=target_expiry_ms,
        target_sub_id="same-sub",
        total_gb_bytes=0,
        source_client=None,
    )

    assert recovered == (client_uuid, target_expiry_ms)
    assert state["deleted"] is True
    assert state["recreated"] is True


def test_orphan_recreate_fails_closed_for_uuid_collision(monkeypatch):
    from shop_bot.modules import xui_api

    email = "user81009-key1@vpn.bot"
    node_inbound = SimpleNamespace(
        id=7,
        protocol="vless",
        remark="ORDINARY",
        stream_settings=SimpleNamespace(network="tcp"),
    )
    posts: list[str] = []

    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda _api, _inbound_id: {
            "id": 7,
            "protocol": "vless",
            "settings": json.dumps({"clients": []}),
            "streamSettings": json.dumps({"network": "tcp"}),
        },
    )
    monkeypatch.setattr(
        xui_api,
        "_api_get_json",
        lambda _api, _endpoint, *, skip_check=False: {
            "success": True,
            "obj": [
                {
                    "id": 43124,
                    "email": email,
                    "uuid": "other-uuid",
                    "subId": "same-sub",
                    "inboundIds": [],
                }
            ],
        },
    )

    def _post_json(_api, endpoint, _payload):
        posts.append(endpoint)
        return {"success": True}

    monkeypatch.setattr(xui_api, "_api_post_json", _post_json)

    recovered = xui_api._recover_secondary_client_update_via_clients_api(
        SimpleNamespace(),
        node_inbound,
        email,
        client_uuid="expected-uuid",
        target_expiry_ms=1_800_000_000_000,
        target_sub_id="same-sub",
        total_gb_bytes=0,
        source_client=None,
    )

    assert recovered == (None, None)
    assert not any(endpoint.startswith("panel/api/clients/del/") for endpoint in posts)
    assert "panel/api/clients/add" not in posts


def test_healthy_host_persistent_failure_is_exhausted(monkeypatch):
    from shop_bot.data_manager import database, scheduler
    from shop_bot.errors import BotError

    payment_id = "renewal-exhaust-healthy-001"
    user_id = 81006
    host_name = "MainPanel"
    metadata = {
        "user_id": user_id,
        "host_name": host_name,
        "payment_method": "PayPear",
        "payment_confirmed": True,
        "external_payment_id": payment_id,
        "issuance_failure_reason": "duplicate_email",
    }
    database.register_user_if_not_exists(user_id, "buyer", None)
    database.create_host(host_name, "https://panel.example", "u", "p", 1)
    database.update_host_status(host_name, "healthy")
    database.create_pending_transaction(payment_id, user_id, 600.0, metadata)
    for _ in range(8):
        database.mark_transaction_retryable_failure(
            payment_id,
            reason="duplicate_email",
            metadata=metadata,
            payment_method="PayPear",
            next_retry_at=datetime.now(UTC),
            statuses=("pending",),
        )

    captured: list[dict] = []

    def _capture(error: BotError, *, details: str = "", respect_cooldown: bool = True):
        captured.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture)
    assert scheduler._emit_autoretry_exhausted_alerts(max_attempts=8, limit=20) == 1
    tx = database.get_transaction_by_payment_id(payment_id)
    assert tx["issuance_state"] == database.ISSUANCE_STATE_EXHAUSTED
    assert "autoretry_exhausted_persistent_failure" in captured[0]["details"]
