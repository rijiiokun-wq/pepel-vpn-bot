from __future__ import annotations

import inspect
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


DAY_MS = 24 * 60 * 60 * 1000
RESET_PERIOD_MS = 30 * DAY_MS
TRAFFIC_LIMIT_BYTES = 40 * 1024 * 1024 * 1024
BASE_EMAIL = "user100000001-key1@vpn.bot"
CLIENT_UUID = "00000000-0000-4000-8000-000000000010"
SUB_ID = "cycle-sub-id"


def _ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=UTC).timestamp() * 1000)


def test_new_three_month_key_gets_first_30_day_deadline():
    from shop_bot.modules import xui_api

    anchor_ms = _ms("2026-06-19T12:00:00")
    paid_expiry_ms = _ms("2026-09-19T12:00:00")

    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        anchor_ms,
        0,
        paid_expiry_ms,
        anchor_ms,
    )

    assert target_ms == anchor_ms + RESET_PERIOD_MS
    assert target_ms == _ms("2026-07-19T12:00:00")
    assert reset_due is False


def test_active_renewal_keeps_original_cycle_but_expired_renewal_reanchors():
    from shop_bot.modules import subscription_dates

    created_at = datetime(2026, 1, 1, 12, tzinfo=UTC)
    paid_at = datetime(2026, 7, 21, 12, tzinfo=UTC)

    active_anchor = subscription_dates.resolve_recurring_cycle_anchor(
        paid_at=paid_at,
        current_expiry=datetime(2026, 8, 1, 12, tzinfo=UTC),
        created_at=created_at,
    )
    expired_anchor = subscription_dates.resolve_recurring_cycle_anchor(
        paid_at=paid_at,
        current_expiry=datetime(2026, 7, 20, 12, tzinfo=UTC),
        created_at=created_at,
    )

    assert active_anchor == created_at
    assert expired_anchor == paid_at


def test_requested_cycle_anchor_overrides_stale_database_anchor(monkeypatch):
    from shop_bot.modules import xui_api

    requested_anchor_ms = _ms("2026-07-21T12:00:00")
    monkeypatch.setattr(
        xui_api,
        "get_key_by_email",
        lambda _email: {"created_date": "2025-01-01T00:00:00"},
    )

    assert (
        xui_api._whitelist_cycle_anchor_ms_for_client(
            BASE_EMAIL,
            requested_anchor_ms=requested_anchor_ms,
            now_ms=requested_anchor_ms,
        )
        == requested_anchor_ms
    )


def test_new_ordinary_node_uses_paid_db_expiry_for_disabled_whitelist_source():
    from shop_bot.modules import xui_api

    now_ms = _ms("2026-07-21T12:00:00")
    paid_expiry_ms = _ms("2026-09-19T12:00:00")
    source_client = SimpleNamespace(enable=False, expiry_time=_ms("2026-07-19T12:00:00"))

    eligible, resolved_expiry_ms = xui_api._mainpanel_source_client_eligibility(
        source_client,
        {
            "subscription_tier": "super",
            "expiry_date": "2026-09-19T12:00:00+00:00",
            "is_archived": 0,
        },
        now_ms=now_ms,
    )

    assert eligible is True
    assert resolved_expiry_ms == paid_expiry_ms


def test_new_ordinary_node_rejects_archived_local_key_even_if_panel_client_is_enabled():
    from shop_bot.modules import xui_api

    now_ms = _ms("2026-07-21T12:00:00")
    source_client = SimpleNamespace(
        enable=True,
        expiry_time=_ms("2026-09-19T12:00:00"),
    )

    eligible, _resolved_expiry_ms = xui_api._mainpanel_source_client_eligibility(
        source_client,
        {
            "subscription_tier": "super",
            "expiry_date": "2026-09-19T12:00:00+00:00",
            "is_archived": 1,
        },
        now_ms=now_ms,
    )

    assert eligible is False


def test_new_ordinary_client_clears_whitelist_reset_policy():
    from shop_bot.modules import xui_api

    paid_expiry_ms = _ms("2026-09-19T12:00:00")
    captured = []
    inbound = SimpleNamespace(
        id=2,
        protocol="vless",
        stream_settings=SimpleNamespace(network="tcp", security="reality"),
        settings=SimpleNamespace(clients=[]),
    )

    class _InboundApi:
        @staticmethod
        def get_by_id(_inbound_id):
            return inbound

    class _ClientApi:
        @staticmethod
        def add(_inbound_id, clients):
            captured.extend(clients)

    source_client = xui_api.Client(
        id=CLIENT_UUID,
        email=BASE_EMAIL,
        enable=False,
        expiry_time=_ms("2026-08-18T12:00:00"),
        totalGB=TRAFFIC_LIMIT_BYTES,
        reset=30,
    )
    result_uuid, result_expiry_ms = xui_api.update_or_create_client_on_panel(
        api=SimpleNamespace(inbound=_InboundApi(), client=_ClientApi()),
        inbound_id=2,
        email="user100000001-key1-estonia2-2@vpn.bot",
        days_to_add=0,
        target_expiry_ms=paid_expiry_ms,
        target_sub_id="ordinary-sub-id",
        target_client_uuid=CLIENT_UUID,
        total_gb_bytes=0,
        source_client=source_client,
    )

    assert (result_uuid, result_expiry_ms) == (CLIENT_UUID, paid_expiry_ms)
    assert len(captured) == 1
    assert int(captured[0].total_gb) == 0
    assert int(captured[0].reset) == 0


def test_overdue_runtime_reset_happens_before_deadline_update_and_retries(monkeypatch):
    from shop_bot.modules import xui_api

    old_deadline_ms = _ms("2026-07-19T12:00:00")
    new_deadline_ms = _ms("2026-08-18T12:00:00")
    existing = xui_api.Client(
        id=CLIENT_UUID,
        email=BASE_EMAIL,
        enable=False,
        expiry_time=old_deadline_ms,
        totalGB=TRAFFIC_LIMIT_BYTES,
        reset=30,
    )
    inbound = SimpleNamespace(
        id=1,
        protocol="vless",
        stream_settings=SimpleNamespace(network="tcp", security="reality"),
        settings=SimpleNamespace(clients=[existing]),
    )
    update_calls = []
    reset_results = iter([False, True])

    class _InboundApi:
        @staticmethod
        def get_by_id(_inbound_id):
            return inbound

    class _ClientApi:
        @staticmethod
        def update(_client_uuid, client):
            update_calls.append(int(client.expiry_time))

    monkeypatch.setattr(
        xui_api,
        "_reset_client_traffic_on_panel",
        lambda *_args, **_kwargs: next(reset_results),
    )
    api = SimpleNamespace(inbound=_InboundApi(), client=_ClientApi())
    kwargs = {
        "api": api,
        "inbound_id": 1,
        "email": BASE_EMAIL,
        "days_to_add": 0,
        "target_expiry_ms": new_deadline_ms,
        "target_client_uuid": CLIENT_UUID,
        "total_gb_bytes": TRAFFIC_LIMIT_BYTES,
        "traffic_reset_days": 30,
        "reset_client_traffic": True,
        "preserve_existing_enable": True,
    }

    assert xui_api.update_or_create_client_on_panel(**kwargs) == (None, None)
    assert existing.expiry_time == old_deadline_ms
    assert update_calls == []

    assert xui_api.update_or_create_client_on_panel(**kwargs) == (
        CLIENT_UUID,
        new_deadline_ms,
    )
    assert update_calls == [new_deadline_ms]
    assert existing.enable is True


def test_hysteria_ordinary_payload_clears_inherited_reset_policy():
    from shop_bot.modules import xui_api

    payload = xui_api._build_hysteria_raw_client(
        inbound_id=2,
        email="user100000001-key1-estonia2-2@vpn.bot",
        client_uuid=CLIENT_UUID,
        expiry_ms=_ms("2026-09-19T12:00:00"),
        sub_id="ordinary-sub-id",
        total_gb_bytes=0,
        traffic_reset_days=None,
        existing_client={"reset": 30, "enable": False, "auth": "secret"},
        source_client=None,
        preserve_existing_enable=False,
        reset_client_traffic=False,
    )

    assert payload["totalGB"] == 0
    assert payload["reset"] == 0


def test_secondary_recovery_cannot_advance_overdue_deadline_without_reset(monkeypatch):
    from shop_bot.modules import xui_api

    raw_inbound = {
        "id": 2,
        "protocol": "vless",
        "settings": json.dumps(
            {
                "clients": [
                    {
                        "id": CLIENT_UUID,
                        "email": BASE_EMAIL,
                        "subId": SUB_ID,
                        "expiryTime": _ms("2026-07-19T12:00:00"),
                    }
                ]
            }
        ),
    }
    monkeypatch.setattr(
        xui_api,
        "_reset_client_traffic_on_panel",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda *_args, **_kwargs: raw_inbound,
    )
    monkeypatch.setattr(
        xui_api,
        "_api_post_client_update",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not update after reset refusal")),
    )

    result = xui_api._recover_secondary_client_update_via_clients_api(
        SimpleNamespace(),
        SimpleNamespace(id=2, protocol="vless"),
        BASE_EMAIL,
        client_uuid=CLIENT_UUID,
        target_expiry_ms=_ms("2026-08-18T12:00:00"),
        target_sub_id=SUB_ID,
        total_gb_bytes=TRAFFIC_LIMIT_BYTES,
        source_client=None,
        preserve_existing_enable=False,
        traffic_reset_days=30,
        reset_client_traffic=True,
    )

    assert result == (None, None)


def test_secondary_recovery_refuses_foreign_email_uuid_before_reset(monkeypatch):
    from shop_bot.modules import xui_api

    reset_calls = []
    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda *_args, **_kwargs: {
            "id": 2,
            "protocol": "vless",
            "settings": json.dumps(
                {
                    "clients": [
                        {
                            "id": "foreign-uuid",
                            "email": BASE_EMAIL,
                            "subId": "foreign-sub",
                        }
                    ]
                }
            ),
        },
    )
    monkeypatch.setattr(
        xui_api,
        "_reset_client_traffic_on_panel",
        lambda *_args, **_kwargs: reset_calls.append(True) or True,
    )
    monkeypatch.setattr(
        xui_api,
        "_api_post_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("must not attach, update, or delete foreign client")
        ),
    )

    result = xui_api._recover_secondary_client_update_via_clients_api(
        SimpleNamespace(),
        SimpleNamespace(id=2, protocol="vless"),
        BASE_EMAIL,
        client_uuid=CLIENT_UUID,
        target_expiry_ms=_ms("2026-08-18T12:00:00"),
        target_sub_id=SUB_ID,
        total_gb_bytes=TRAFFIC_LIMIT_BYTES,
        source_client=None,
        traffic_reset_days=30,
        reset_client_traffic=True,
    )

    assert result == (None, None)
    assert reset_calls == []


def test_primary_upsert_refuses_foreign_email_uuid_before_reset(monkeypatch):
    from shop_bot.modules import xui_api

    foreign = xui_api.Client(
        id="foreign-uuid",
        email=BASE_EMAIL,
        enable=True,
        expiry_time=_ms("2026-07-19T12:00:00"),
        totalGB=TRAFFIC_LIMIT_BYTES,
        reset=30,
    )
    inbound = SimpleNamespace(
        id=1,
        protocol="vless",
        stream_settings=SimpleNamespace(network="tcp", security="reality"),
        settings=SimpleNamespace(clients=[foreign]),
    )
    reset_calls = []
    monkeypatch.setattr(
        xui_api,
        "_reset_client_traffic_on_panel",
        lambda *_args, **_kwargs: reset_calls.append(True) or True,
    )

    result = xui_api.update_or_create_client_on_panel(
        api=SimpleNamespace(
            inbound=SimpleNamespace(get_by_id=lambda _inbound_id: inbound),
            client=SimpleNamespace(
                update=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not update foreign client"))
            ),
        ),
        inbound_id=1,
        email=BASE_EMAIL,
        days_to_add=0,
        target_expiry_ms=_ms("2026-08-18T12:00:00"),
        target_client_uuid=CLIENT_UUID,
        total_gb_bytes=TRAFFIC_LIMIT_BYTES,
        traffic_reset_days=30,
        reset_client_traffic=True,
    )

    assert result == (None, None)
    assert reset_calls == []


def test_whitelist_renewal_preserves_disabled_state_before_reset_deadline():
    from shop_bot.modules import xui_api

    existing = xui_api.Client(
        id=CLIENT_UUID,
        email=BASE_EMAIL,
        enable=False,
        expiry_time=_ms("2026-08-18T12:00:00"),
        totalGB=TRAFFIC_LIMIT_BYTES,
        reset=30,
    )
    inbound = SimpleNamespace(
        id=1,
        protocol="vless",
        stream_settings=SimpleNamespace(network="tcp", security="reality"),
        settings=SimpleNamespace(clients=[existing]),
    )

    class _InboundApi:
        @staticmethod
        def get_by_id(_inbound_id):
            return inbound

    class _ClientApi:
        @staticmethod
        def update(_client_uuid, client):
            assert client.enable is False

    result_uuid, _expiry_ms = xui_api.update_or_create_client_on_panel(
        api=SimpleNamespace(inbound=_InboundApi(), client=_ClientApi()),
        inbound_id=1,
        email=BASE_EMAIL,
        days_to_add=0,
        target_expiry_ms=_ms("2026-08-18T12:00:00"),
        target_client_uuid=CLIENT_UUID,
        total_gb_bytes=TRAFFIC_LIMIT_BYTES,
        reset_client_traffic=False,
        preserve_existing_enable=True,
    )

    assert result_uuid == CLIENT_UUID


def test_whitelist_snapshot_preserves_cross_inbound_identity_collisions(monkeypatch):
    from shop_bot.modules import xui_api

    first = SimpleNamespace(
        id=1,
        remark="WHITELIST1",
        tag="",
        protocol="vless",
        enable=True,
        port=443,
    )
    second = SimpleNamespace(
        id=2,
        remark="WHITELIST2",
        tag="",
        protocol="vless",
        enable=True,
        port=8443,
    )
    raw_inbounds = [
        _raw_inbound(
            inbound,
            email=BASE_EMAIL,
            current_reset_ms=_ms("2026-08-18T12:00:00"),
            paid_expiry_ms=_ms("2026-09-19T12:00:00"),
        )
        for inbound in (first, second)
    ]
    fake_api = SimpleNamespace()
    monkeypatch.setattr(
        xui_api,
        "get_host",
        lambda _host: {
            "host_url": "https://panel.example.test",
            "host_username": "admin",
            "host_pass": "secret",
        },
    )
    monkeypatch.setattr(xui_api, "_create_api", lambda *_args, **_kwargs: fake_api)
    monkeypatch.setattr(xui_api, "_authenticate_api", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(xui_api, "_list_inbounds_raw", lambda _api: raw_inbounds)

    snapshot = xui_api.get_whitelist_traffic_snapshot("MainPanel")

    assert len(snapshot) == 2
    assert {row["inbound_id"] for row in snapshot.values()} == {1, 2}


def test_valid_future_deadline_is_preserved_on_renewal():
    from shop_bot.modules import xui_api

    anchor_ms = _ms("2026-06-19T12:00:00")
    current_reset_ms = _ms("2026-07-19T12:00:00")
    extended_paid_expiry_ms = _ms("2026-12-19T12:00:00")

    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        anchor_ms,
        current_reset_ms,
        extended_paid_expiry_ms,
        _ms("2026-07-01T12:00:00"),
    )

    assert target_ms == current_reset_ms
    assert reset_due is False


def test_legacy_paid_expiry_is_not_accepted_as_multi_cycle_reset_deadline():
    from shop_bot.modules import xui_api

    anchor_ms = _ms("2026-06-19T12:00:00")
    paid_expiry_ms = _ms("2026-09-19T12:00:00")

    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        anchor_ms,
        paid_expiry_ms,
        paid_expiry_ms,
        _ms("2026-06-20T12:00:00"),
    )

    assert target_ms == _ms("2026-07-19T12:00:00")
    assert reset_due is False


def test_renewal_repairs_legacy_deadline_that_was_the_previous_paid_expiry():
    from shop_bot.modules import xui_api

    anchor_ms = _ms("2026-06-19T12:00:00")
    previous_paid_expiry_ms = _ms("2026-09-19T12:00:00")
    extended_paid_expiry_ms = _ms("2026-12-19T12:00:00")

    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        anchor_ms,
        previous_paid_expiry_ms,
        extended_paid_expiry_ms,
        _ms("2026-06-20T12:00:00"),
    )

    assert target_ms == _ms("2026-07-19T12:00:00")
    assert reset_due is False


def test_overdue_deadline_advances_to_strict_future_anchor_boundary():
    from shop_bot.modules import xui_api

    anchor_ms = _ms("2026-06-19T12:00:00")
    paid_expiry_ms = _ms("2026-09-19T12:00:00")

    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        anchor_ms,
        _ms("2026-07-19T12:00:00"),
        paid_expiry_ms,
        _ms("2026-07-21T12:00:00"),
    )

    assert target_ms == anchor_ms + 2 * RESET_PERIOD_MS
    assert target_ms == _ms("2026-08-18T12:00:00")
    assert reset_due is True


def test_calculated_boundary_is_capped_by_paid_expiry():
    from shop_bot.modules import xui_api

    anchor_ms = _ms("2026-06-19T12:00:00")
    paid_expiry_ms = _ms("2026-08-10T12:00:00")

    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        anchor_ms,
        _ms("2026-07-19T12:00:00"),
        paid_expiry_ms,
        _ms("2026-07-21T12:00:00"),
    )

    assert target_ms == paid_expiry_ms
    assert reset_due is True


def test_active_terminal_renewal_moves_to_recurring_grid_without_second_reset():
    from shop_bot.modules import xui_api

    anchor_ms = _ms("2026-06-19T12:00:00")
    terminal_deadline_ms = _ms("2026-09-19T12:00:00")
    extended_paid_expiry_ms = _ms("2026-12-19T12:00:00")

    before_boundary, before_reset_due = xui_api._next_whitelist_reset_deadline_ms(
        anchor_ms,
        terminal_deadline_ms,
        extended_paid_expiry_ms,
        _ms("2026-09-16T12:00:00"),
        current_reset_days=0,
    )
    after_boundary, after_reset_due = xui_api._next_whitelist_reset_deadline_ms(
        anchor_ms,
        terminal_deadline_ms,
        extended_paid_expiry_ms,
        _ms("2026-09-18T12:00:00"),
        current_reset_days=0,
    )

    assert before_boundary == _ms("2026-09-17T12:00:00")
    assert after_boundary == _ms("2026-10-17T12:00:00")
    assert before_reset_due is False
    assert after_reset_due is False


def test_lapsed_terminal_renewal_reanchors_and_resets_fresh_cycle():
    from shop_bot.modules import xui_api

    renewal_ms = _ms("2026-09-21T12:00:00")
    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        renewal_ms,
        _ms("2026-09-19T12:00:00"),
        _ms("2026-12-21T12:00:00"),
        renewal_ms,
        current_reset_days=0,
    )

    assert target_ms == _ms("2026-10-21T12:00:00")
    assert reset_due is True


def test_auto_renewed_deadline_beyond_paid_expiry_only_switches_terminal_policy():
    from shop_bot.modules import xui_api

    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        _ms("2026-06-19T12:00:00"),
        _ms("2026-10-17T12:00:00"),
        _ms("2026-09-19T12:00:00"),
        _ms("2026-09-18T12:00:00"),
        current_reset_days=30,
    )

    assert target_ms == _ms("2026-09-19T12:00:00")
    assert reset_due is False


def test_far_future_overshoot_before_prior_boundary_does_not_hide_missed_reset():
    from shop_bot.modules import xui_api

    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        _ms("2026-06-19T12:00:00"),
        _ms("2026-10-17T12:00:00"),
        _ms("2026-09-19T12:00:00"),
        _ms("2026-07-20T12:00:00"),
        current_reset_days=30,
    )

    assert target_ms == _ms("2026-08-18T12:00:00")
    assert reset_due is True


def test_legacy_terminal_deadline_with_reset_policy_resets_missed_boundary():
    from shop_bot.modules import xui_api

    paid_expiry_ms = _ms("2026-08-10T12:00:00")
    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        _ms("2026-06-19T12:00:00"),
        paid_expiry_ms,
        paid_expiry_ms,
        _ms("2026-07-21T12:00:00"),
        current_reset_days=30,
    )

    assert target_ms == paid_expiry_ms
    assert reset_due is True


def test_future_strict_grid_terminal_switch_is_policy_only():
    from shop_bot.modules import xui_api

    paid_expiry_ms = _ms("2026-08-18T12:00:00")
    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        _ms("2026-06-19T12:00:00"),
        paid_expiry_ms,
        paid_expiry_ms,
        _ms("2026-07-21T12:00:00"),
        current_reset_days=30,
    )

    assert target_ms == paid_expiry_ms
    assert reset_due is False


def test_aligned_multi_month_paid_expiry_is_not_auto_reset_proof():
    from shop_bot.modules import xui_api

    target_ms, reset_due = xui_api._next_whitelist_reset_deadline_ms(
        _ms("2026-01-01T12:00:00"),
        _ms("2026-04-01T12:00:00"),
        _ms("2026-04-01T12:00:00"),
        _ms("2026-02-02T12:00:00"),
        current_reset_days=30,
    )

    assert target_ms == _ms("2026-03-02T12:00:00")
    assert reset_due is True


def test_delayed_continuous_terminal_renewal_waits_for_next_grid_boundary():
    from shop_bot.modules import xui_api

    anchor_ms = _ms("2026-06-19T12:00:00")
    old_terminal_ms = _ms("2026-08-10T12:00:00")
    extended_paid_expiry_ms = _ms("2026-09-10T12:00:00")

    target_before, reset_before = xui_api._next_whitelist_reset_deadline_ms(
        anchor_ms,
        old_terminal_ms,
        extended_paid_expiry_ms,
        _ms("2026-08-11T12:00:00"),
        current_reset_days=0,
    )
    target_at_boundary, reset_at_boundary = xui_api._next_whitelist_reset_deadline_ms(
        anchor_ms,
        old_terminal_ms,
        extended_paid_expiry_ms,
        _ms("2026-08-18T12:00:00"),
        current_reset_days=0,
    )

    assert target_before == _ms("2026-08-18T12:00:00")
    assert reset_before is False
    assert target_at_boundary == _ms("2026-09-10T12:00:00")
    assert reset_at_boundary is True


def _raw_inbound(
    inbound: SimpleNamespace,
    *,
    email: str | None = None,
    current_reset_ms: int = 0,
    paid_expiry_ms: int = 0,
    used_bytes: int = 0,
) -> dict[str, Any]:
    clients: list[dict[str, Any]] = []
    stats: list[dict[str, Any]] = []
    if email:
        clients.append(
            {
                "id": CLIENT_UUID,
                "email": email,
                "subId": SUB_ID,
                "enable": True,
                "expiryTime": paid_expiry_ms,
                "totalGB": TRAFFIC_LIMIT_BYTES,
                "reset": 30,
            }
        )
        stats.append(
            {
                "email": email,
                "enable": True,
                "expiryTime": current_reset_ms,
                "total": TRAFFIC_LIMIT_BYTES,
                "up": used_bytes,
                "down": 0,
                "reset": 30,
            }
        )
    return {
        "id": inbound.id,
        "remark": inbound.remark,
        "tag": inbound.tag,
        "protocol": inbound.protocol,
        "enable": inbound.enable,
        "port": inbound.port,
        "settings": json.dumps({"clients": clients}),
        "clientStats": stats,
    }


def _patch_key_workflow(
    monkeypatch,
    *,
    now_ms: int,
    whitelist_raw: dict[str, Any],
) -> tuple[Any, list[dict[str, Any]]]:
    from shop_bot.modules import xui_api

    whitelist = SimpleNamespace(
        id=1,
        remark="WHITELIST1",
        tag="",
        protocol="vless",
        enable=True,
        port=443,
    )
    ordinary = SimpleNamespace(
        id=2,
        remark="ESTONIA2",
        tag="",
        protocol="vless",
        enable=True,
        port=8443,
    )
    raw_inbounds = [whitelist_raw, _raw_inbound(ordinary)]
    fake_api = SimpleNamespace()
    calls: list[dict[str, Any]] = []

    monkeypatch.setattr(xui_api.time, "time", lambda: now_ms / 1000)
    monkeypatch.setattr(xui_api.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(xui_api, "get_key_by_email", lambda _email: {})
    monkeypatch.setattr(xui_api, "login_to_host", lambda **_kwargs: (fake_api, whitelist))
    monkeypatch.setattr(xui_api, "_node_public_address_maps", lambda _api: ({}, {}))
    monkeypatch.setattr(xui_api, "_white_node_inbound_ids_for_panel", lambda _host: {1})
    monkeypatch.setattr(xui_api, "_list_inbounds_raw", lambda _api: raw_inbounds)
    monkeypatch.setattr(
        xui_api,
        "_subscription_inbounds",
        lambda *_args, **_kwargs: [whitelist, ordinary],
    )
    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda _api, inbound_id, **_kwargs: next(raw for raw in raw_inbounds if raw["id"] == inbound_id),
    )

    def _fake_update(_api, inbound_id, email, _days_to_add, **kwargs):
        calls.append({"inbound_id": inbound_id, "email": email, **kwargs})
        metadata = kwargs.get("result_metadata")
        if isinstance(metadata, dict):
            metadata["sub_id"] = kwargs["target_sub_id"]
        return kwargs["target_client_uuid"], kwargs["target_expiry_ms"]

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _fake_update)
    monkeypatch.setattr(
        xui_api,
        "get_connection_string",
        lambda inbound, client_uuid, _host_url, *, remark, **_kwargs: (
            f"vless://{client_uuid}@node.example:{inbound.port}?type=tcp#{remark}"
        ),
    )
    def _links(*_args, **_kwargs):
        client_uuid = calls[0]["target_client_uuid"] if calls else CLIENT_UUID
        return [
            f"vless://{client_uuid}@white.example:443?type=tcp#WHITELIST1",
            f"vless://{client_uuid}@estonia.example:8443?type=tcp#ESTONIA2",
        ]

    monkeypatch.setattr(xui_api, "_get_subscription_links_with_retry", _links)
    monkeypatch.setattr(xui_api, "_build_subscription_links_from_panel_clients", _links)
    monkeypatch.setattr(
        xui_api,
        "_filter_subscription_links_for_inbounds",
        lambda values, _inbounds: list(values),
    )
    monkeypatch.setattr(
        xui_api,
        "patch_subscription_links_for_inbounds",
        lambda values, *_args, **_kwargs: list(values),
    )
    return xui_api, calls


def _run_key_workflow(
    xui_api,
    *,
    anchor_ms: int,
    paid_expiry_ms: int,
    target_client_uuid: str | None,
) -> dict[str, Any] | None:
    return xui_api._create_or_update_key_on_host_once(
        host_name="MainPanel",
        host_data={
            "host_name": "MainPanel",
            "host_url": "https://panel.example.test",
            "host_username": "admin",
            "host_pass": "secret",
            "host_inbound_id": 1,
        },
        email=BASE_EMAIL,
        days_to_add=90,
        months=3,
        target_expiry_ms=paid_expiry_ms,
        target_sub_id=SUB_ID,
        target_client_uuid=target_client_uuid,
        reset_limited_traffic=False,
        whitelist_cycle_anchor_ms=anchor_ms,
        include_white_inbounds=True,
        require_white_inbound=True,
    )


def test_new_three_month_issuance_returns_paid_expiry_and_per_inbound_targets(monkeypatch):
    anchor_ms = _ms("2026-06-19T12:00:00")
    paid_expiry_ms = _ms("2026-09-19T12:00:00")
    whitelist = SimpleNamespace(
        id=1,
        remark="WHITELIST1",
        tag="",
        protocol="vless",
        enable=True,
        port=443,
    )
    xui_api, calls = _patch_key_workflow(
        monkeypatch,
        now_ms=anchor_ms,
        whitelist_raw=_raw_inbound(whitelist),
    )

    result = _run_key_workflow(
        xui_api,
        anchor_ms=anchor_ms,
        paid_expiry_ms=paid_expiry_ms,
        target_client_uuid=None,
    )

    assert result is not None
    assert result["expiry_timestamp_ms"] == paid_expiry_ms
    assert result["whitelist_reset_timestamp_ms"] == _ms("2026-07-19T12:00:00")
    assert [(call["inbound_id"], call["target_expiry_ms"]) for call in calls] == [
        (1, _ms("2026-07-19T12:00:00")),
        (2, paid_expiry_ms),
    ]
    assert [call["total_gb_bytes"] for call in calls] == [TRAFFIC_LIMIT_BYTES, 0]
    assert [call["reset_client_traffic"] for call in calls] == [False, False]


def test_renewal_preserves_future_whitelist_deadline_and_counters(monkeypatch):
    anchor_ms = _ms("2026-06-19T12:00:00")
    current_reset_ms = _ms("2026-07-19T12:00:00")
    extended_paid_expiry_ms = _ms("2026-12-19T12:00:00")
    whitelist = SimpleNamespace(
        id=1,
        remark="WHITELIST1",
        tag="",
        protocol="vless",
        enable=True,
        port=443,
    )
    xui_api, calls = _patch_key_workflow(
        monkeypatch,
        now_ms=_ms("2026-07-01T12:00:00"),
        whitelist_raw=_raw_inbound(
            whitelist,
            email=BASE_EMAIL,
            current_reset_ms=current_reset_ms,
            paid_expiry_ms=extended_paid_expiry_ms,
            used_bytes=123_456,
        ),
    )

    result = _run_key_workflow(
        xui_api,
        anchor_ms=anchor_ms,
        paid_expiry_ms=extended_paid_expiry_ms,
        target_client_uuid=CLIENT_UUID,
    )

    assert result is not None
    assert result["expiry_timestamp_ms"] == extended_paid_expiry_ms
    assert result["whitelist_reset_timestamp_ms"] == current_reset_ms
    assert calls[0]["target_expiry_ms"] == current_reset_ms
    assert calls[0]["reset_client_traffic"] is False
    assert calls[1]["target_expiry_ms"] == extended_paid_expiry_ms
    assert calls[1]["reset_client_traffic"] is False


def test_last_partial_cycle_disables_whitelist_auto_renew(monkeypatch):
    anchor_ms = _ms("2026-06-19T12:00:00")
    paid_expiry_ms = _ms("2026-08-10T12:00:00")
    whitelist = SimpleNamespace(
        id=1,
        remark="WHITELIST1",
        tag="",
        protocol="vless",
        enable=True,
        port=443,
    )
    xui_api, calls = _patch_key_workflow(
        monkeypatch,
        now_ms=_ms("2026-07-21T12:00:00"),
        whitelist_raw=_raw_inbound(
            whitelist,
            email=BASE_EMAIL,
            current_reset_ms=_ms("2026-07-19T12:00:00"),
            paid_expiry_ms=paid_expiry_ms,
            used_bytes=1,
        ),
    )

    result = _run_key_workflow(
        xui_api,
        anchor_ms=anchor_ms,
        paid_expiry_ms=paid_expiry_ms,
        target_client_uuid=CLIENT_UUID,
    )

    assert result is not None
    assert calls[0]["target_expiry_ms"] == paid_expiry_ms
    assert calls[0]["traffic_reset_days"] == 0
    assert calls[0]["reset_client_traffic"] is True


def test_overdue_cycle_resets_only_limited_inbound_at_exact_anchor_boundary(monkeypatch):
    anchor_ms = _ms("2026-06-19T12:00:00")
    paid_expiry_ms = _ms("2026-09-19T12:00:00")
    whitelist = SimpleNamespace(
        id=1,
        remark="WHITELIST1",
        tag="",
        protocol="vless",
        enable=True,
        port=443,
    )
    xui_api, calls = _patch_key_workflow(
        monkeypatch,
        now_ms=_ms("2026-07-21T12:00:00"),
        whitelist_raw=_raw_inbound(
            whitelist,
            email=BASE_EMAIL,
            current_reset_ms=_ms("2026-07-19T12:00:00"),
            paid_expiry_ms=paid_expiry_ms,
            used_bytes=TRAFFIC_LIMIT_BYTES,
        ),
    )

    result = _run_key_workflow(
        xui_api,
        anchor_ms=anchor_ms,
        paid_expiry_ms=paid_expiry_ms,
        target_client_uuid=CLIENT_UUID,
    )

    assert result is not None
    assert result["whitelist_reset_timestamp_ms"] == _ms("2026-08-18T12:00:00")
    assert calls[0]["target_expiry_ms"] == anchor_ms + 2 * RESET_PERIOD_MS
    assert calls[0]["reset_client_traffic"] is True
    assert calls[1]["target_expiry_ms"] == paid_expiry_ms
    assert calls[1]["reset_client_traffic"] is False


def test_mainpanel_sync_does_not_treat_whitelist_cycle_deadline_as_stale(monkeypatch):
    from shop_bot.modules import xui_api

    anchor_ms = _ms("2026-06-19T12:00:00")
    current_reset_ms = _ms("2026-07-19T12:00:00")
    paid_expiry_ms = _ms("2026-09-19T12:00:00")
    inbound = SimpleNamespace(
        id=1,
        remark="WHITELIST1",
        tag="",
        protocol="vless",
        enable=True,
        port=443,
    )
    raw_inbound = _raw_inbound(
        inbound,
        email=BASE_EMAIL,
        current_reset_ms=current_reset_ms,
        paid_expiry_ms=current_reset_ms,
        used_bytes=123_456,
    )
    key = {
        "key_id": 10,
        "user_id": 100000001,
        "host_name": "MainPanel",
        "xui_client_uuid": CLIENT_UUID,
        "key_email": BASE_EMAIL,
        "panel_sub_id": SUB_ID,
        "subscription_tier": "super",
        "created_date": datetime.fromtimestamp(anchor_ms / 1000, tz=UTC),
        "expiry_date": datetime.fromtimestamp(paid_expiry_ms / 1000, tz=UTC),
    }
    calls: list[dict[str, Any]] = []
    cleared: list[int] = []

    monkeypatch.setattr(xui_api.time, "time", lambda: _ms("2026-07-01T12:00:00") / 1000)
    monkeypatch.setattr(
        xui_api,
        "scan_mainpanel_subscription_inbounds",
        lambda _host_name: {
            "api": SimpleNamespace(),
            "primary_inbound_id": 1,
            "raw_inbounds": [raw_inbound],
            "inbounds": [
                {
                    "inbound_id": 1,
                    "id": 1,
                    "label": "WHITELIST1",
                    "classification": xui_api.MAINPANEL_INBOUND_CLASS_WHITELIST,
                    "is_new": False,
                    "runtime": inbound,
                }
            ],
        },
    )
    monkeypatch.setattr(xui_api, "get_active_keys_for_host", lambda _host: [key])
    monkeypatch.setattr(xui_api, "get_key_by_email", lambda _email: key)
    monkeypatch.setattr(xui_api, "create_mainpanel_inbound_sync_run", lambda **_kwargs: "cycle-run")
    monkeypatch.setattr(xui_api, "add_mainpanel_inbound_sync_item", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(xui_api, "update_mainpanel_inbound_sync_run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(xui_api, "get_mainpanel_inbound_sync_run", lambda *_args, **_kwargs: None)

    def _unexpected_update(**kwargs):
        calls.append(kwargs)
        return kwargs["target_client_uuid"], kwargs["target_expiry_ms"]

    monkeypatch.setattr(xui_api, "update_or_create_client_on_panel", _unexpected_update)
    monkeypatch.setattr(xui_api, "clear_key_subscription_payload", cleared.append)

    result = xui_api.sync_existing_mainpanel_clients_to_inbounds(
        "MainPanel",
        dry_run=False,
        batch_limit=10,
        persist_snapshot=False,
    )

    assert result["status"] == "completed"
    assert result["updated"] == 0
    assert result["skipped"] == 1
    assert result["details"][0]["action"] == "already_synced"
    assert calls == []
    assert cleared == []


def test_runtime_cycle_guard_sets_terminal_policy_after_panel_auto_renew(monkeypatch):
    from shop_bot.modules import xui_api

    anchor_ms = _ms("2026-06-19T12:00:00")
    paid_expiry_ms = _ms("2026-09-19T12:00:00")
    auto_renewed_deadline_ms = _ms("2026-10-17T12:00:00")
    calls = []
    key = {
        "key_id": 10,
        "host_name": "MainPanel",
        "xui_client_uuid": CLIENT_UUID,
        "key_email": BASE_EMAIL,
        "subscription_tier": "super",
        "created_date": datetime.fromtimestamp(anchor_ms / 1000, tz=UTC),
        "expiry_date": datetime.fromtimestamp(paid_expiry_ms / 1000, tz=UTC),
        "is_archived": 0,
    }
    snapshot = {
        BASE_EMAIL: {
            "email": BASE_EMAIL,
            "client_uuid": CLIENT_UUID,
            "sub_id": SUB_ID,
            "inbound_id": 1,
            "used_bytes": 123,
            "total_bytes": TRAFFIC_LIMIT_BYTES,
            "reset_days": 30,
            "cycle_deadline_ms": auto_renewed_deadline_ms,
            "reset_timestamp_ms": auto_renewed_deadline_ms,
            "panel_enable": True,
        }
    }

    def _repair(host_name, **kwargs):
        calls.append((host_name, kwargs))
        return {
            "status": "updated",
            "verified_reset_ms": kwargs["target_reset_ms"],
            "verified_reset_days": kwargs["target_reset_days"],
            "reset_performed": kwargs["reset_traffic"],
        }

    monkeypatch.setattr(xui_api, "repair_whitelist_reset_cycle_on_host", _repair)

    result = xui_api.reconcile_active_whitelist_reset_cycles(
        "MainPanel",
        keys=[key],
        snapshot=snapshot,
        now_ms=_ms("2026-09-18T12:00:00"),
    )

    assert result == {
        "checked": 1,
        "already_correct": 0,
        "updated": 1,
        "needs_review": 0,
        "failed": 0,
    }
    assert calls == [
        (
            "MainPanel",
            {
                "inbound_id": 1,
                "email": BASE_EMAIL,
                "expected_client_uuid": CLIENT_UUID,
                "expected_sub_id": SUB_ID,
                "target_reset_ms": paid_expiry_ms,
                "target_reset_days": 0,
                "reset_traffic": False,
            },
        )
    ]


def test_runtime_cycle_guard_repairs_distinct_whitelist_components(monkeypatch):
    from shop_bot.modules import xui_api

    anchor_ms = _ms("2026-06-19T12:00:00")
    paid_expiry_ms = _ms("2026-09-19T12:00:00")
    key = {
        "key_id": 10,
        "host_name": "MainPanel",
        "xui_client_uuid": CLIENT_UUID,
        "key_email": BASE_EMAIL,
        "subscription_tier": "super",
        "created_date": datetime.fromtimestamp(anchor_ms / 1000, tz=UTC),
        "expiry_date": datetime.fromtimestamp(paid_expiry_ms / 1000, tz=UTC),
        "is_archived": 0,
    }
    snapshot = {
        f"{BASE_EMAIL}#1": {
            "email": BASE_EMAIL,
            "client_uuid": CLIENT_UUID,
            "sub_id": SUB_ID,
            "inbound_id": 1,
            "used_bytes": 0,
            "total_bytes": TRAFFIC_LIMIT_BYTES,
            "reset_days": 30,
            "cycle_deadline_ms": _ms("2026-10-17T12:00:00"),
            "panel_enable": True,
        },
        f"{BASE_EMAIL}#2": {
            "email": "user100000001-key1-whitelist2-2@vpn.bot",
            "client_uuid": CLIENT_UUID,
            "sub_id": SUB_ID,
            "inbound_id": 2,
            "used_bytes": 0,
            "total_bytes": TRAFFIC_LIMIT_BYTES,
            "reset_days": 30,
            "cycle_deadline_ms": _ms("2026-10-17T12:00:00"),
            "panel_enable": True,
        },
    }
    calls = []

    def _repair(_host, **kwargs):
        calls.append(kwargs)
        return {"status": "updated"}

    monkeypatch.setattr(xui_api, "repair_whitelist_reset_cycle_on_host", _repair)

    result = xui_api.reconcile_active_whitelist_reset_cycles(
        "MainPanel",
        keys=[key],
        snapshot=snapshot,
        now_ms=_ms("2026-09-18T12:00:00"),
    )

    assert result["checked"] == 2
    assert result["updated"] == 2
    assert {call["inbound_id"] for call in calls} == {1, 2}


def test_guarded_repair_updates_only_exact_whitelist_client_and_verifies(monkeypatch):
    from shop_bot.modules import xui_api

    target_reset_ms = _ms("2026-08-18T12:00:00")
    stream_settings = json.dumps({"network": "ws", "wsSettings": {"path": "/api/user"}})
    raw_inbound = {
        "id": 1,
        "remark": "WHITELIST1",
        "protocol": "vless",
        "enable": True,
        "streamSettings": stream_settings,
        "settings": json.dumps(
            {
                "clients": [
                    {
                        "id": CLIENT_UUID,
                        "email": BASE_EMAIL,
                        "subId": SUB_ID,
                        "enable": False,
                        "expiryTime": _ms("2026-09-19T12:00:00"),
                        "totalGB": TRAFFIC_LIMIT_BYTES,
                        "reset": 30,
                    }
                ]
            }
        ),
        "clientStats": [
            {
                "email": BASE_EMAIL,
                "enable": False,
                "expiryTime": _ms("2026-07-19T12:00:00"),
                "total": TRAFFIC_LIMIT_BYTES,
                "up": TRAFFIC_LIMIT_BYTES,
                "down": 0,
                "reset": 30,
            }
        ],
    }
    calls: list[tuple[str, dict[str, Any]]] = []
    journal: list[dict[str, Any]] = []

    monkeypatch.setattr(
        xui_api,
        "scan_mainpanel_subscription_inbounds",
        lambda _host: {
            "api": SimpleNamespace(),
            "inbounds": [
                {
                    "inbound_id": 1,
                    "classification": xui_api.MAINPANEL_INBOUND_CLASS_WHITELIST,
                }
            ],
        },
    )
    monkeypatch.setattr(xui_api, "_get_inbound_raw_by_id", lambda *_args, **_kwargs: raw_inbound)

    def _post(_api, endpoint, payload):
        calls.append((endpoint, payload))
        request = payload["items"][0]
        clients = json.loads(raw_inbound["settings"])["clients"]
        clients[0].update(
            {
                "expiryTime": request["target"]["expiryTime"],
                "totalGB": request["target"]["total"],
                "reset": request["target"]["reset"],
                "enable": True,
            }
        )
        raw_inbound["settings"] = json.dumps({"clients": clients})
        raw_inbound["clientStats"][0].update(
            {
                "expiryTime": request["target"]["expiryTime"],
                "total": request["target"]["total"],
                "reset": request["target"]["reset"],
                "enable": True,
                "up": 0,
                "down": 0,
            }
        )
        return {
            "success": True,
            "obj": {
                "updated": 1,
                "needRestart": True,
                "items": [
                    {
                        "index": 0,
                        "status": "updated",
                        "reason": "applied",
                        "trafficReset": True,
                        "reenabled": True,
                    }
                ],
            },
        }

    monkeypatch.setattr(xui_api, "_api_post_json_once_raw", _post)
    monkeypatch.setattr(
        xui_api,
        "_reset_client_traffic_on_panel",
        lambda *_args, **_kwargs: pytest.fail("legacy reset endpoint is forbidden"),
    )
    monkeypatch.setattr(
        xui_api,
        "_api_post_inbound_client_update",
        lambda *_args, **_kwargs: pytest.fail("legacy updateClient endpoint is forbidden"),
    )
    monkeypatch.setattr(
        xui_api,
        "_api_post_client_update",
        lambda *_args, **_kwargs: pytest.fail("WHITELIST repair must not use the centralized clients endpoint"),
    )

    result = xui_api.repair_whitelist_reset_cycle_on_host(
        "MainPanel",
        inbound_id=1,
        email=BASE_EMAIL,
        expected_client_uuid=CLIENT_UUID,
        expected_sub_id=SUB_ID,
        target_reset_ms=target_reset_ms,
        target_reset_days=30,
        reset_traffic=True,
        before_post_callback=journal.append,
    )

    assert result["status"] == "updated"
    assert result["reason"] == "deadline_repaired"
    assert result["verified_reset_ms"] == target_reset_ms
    assert result["verified_reset_days"] == 30
    assert result["reset_performed"] is True
    assert result["receipt_confirmed"] is True
    assert len(calls) == 1
    endpoint, payload = calls[0]
    assert endpoint == "panel/api/inbounds/1/repairClientTrafficCycles"
    request = payload["items"][0]
    assert request["expectedSettings"]["expiryTime"] == _ms("2026-09-19T12:00:00")
    assert request["expectedTraffic"]["expiryTime"] == _ms("2026-07-19T12:00:00")
    assert request["target"]["expiryTime"] == target_reset_ms
    assert request["resetTraffic"] is True
    assert journal[0]["items"] == payload["items"]
    assert raw_inbound["streamSettings"] == stream_settings


def test_sub_id_drift_fails_precondition_before_journal_and_post(monkeypatch):
    from shop_bot.modules import xui_api

    raw_inbound = {
        "id": 1,
        "remark": "WHITELIST1",
        "protocol": "vless",
        "enable": True,
        "streamSettings": '{"network":"ws"}',
        "settings": json.dumps(
            {
                "clients": [
                    {
                        "id": CLIENT_UUID,
                        "email": BASE_EMAIL,
                        "subId": SUB_ID,
                        "enable": True,
                        "expiryTime": _ms("2026-07-19T12:00:00"),
                        "totalGB": TRAFFIC_LIMIT_BYTES,
                        "reset": 30,
                    }
                ]
            }
        ),
        "clientStats": [
            {
                "email": BASE_EMAIL,
                "enable": True,
                "expiryTime": _ms("2026-07-19T12:00:00"),
                "total": TRAFFIC_LIMIT_BYTES,
                "up": 100,
                "down": 20,
                "reset": 30,
            }
        ],
    }
    monkeypatch.setattr(
        xui_api,
        "scan_mainpanel_subscription_inbounds",
        lambda _host: {
            "api": SimpleNamespace(),
            "inbounds": [
                {
                    "inbound_id": 1,
                    "classification": xui_api.MAINPANEL_INBOUND_CLASS_WHITELIST,
                }
            ],
        },
    )
    monkeypatch.setattr(xui_api, "_get_inbound_raw_by_id", lambda *_args, **_kwargs: raw_inbound)
    monkeypatch.setattr(
        xui_api,
        "_api_post_json_once_raw",
        lambda *_args, **_kwargs: pytest.fail("sub_id drift must block the POST"),
    )
    journal_calls = []

    result = xui_api.repair_whitelist_reset_cycle_on_host(
        "MainPanel",
        inbound_id=1,
        email=BASE_EMAIL,
        expected_client_uuid=CLIENT_UUID,
        expected_sub_id="snapshot-sub-id",
        target_reset_ms=_ms("2026-08-18T12:00:00"),
        target_reset_days=30,
        reset_traffic=True,
        before_post_callback=journal_calls.append,
    )

    assert result == {
        "status": "failed",
        "reason": "stale_sub_id_precondition",
        "reset_performed": False,
    }
    assert journal_calls == []


def test_atomic_cycle_repair_batches_two_exact_clients_in_one_post(monkeypatch):
    from shop_bot.modules import xui_api

    second_email = "user100000002-key1@vpn.bot"
    second_uuid = "00000000-0000-4000-8000-000000000020"
    target_reset_ms = _ms("2026-08-18T12:00:00")
    raw_inbound = {
        "id": 1,
        "remark": "WHITELIST1",
        "protocol": "vless",
        "enable": True,
        "streamSettings": '{"network":"ws"}',
        "settings": json.dumps(
            {
                "clients": [
                    {
                        "id": CLIENT_UUID,
                        "email": BASE_EMAIL,
                        "subId": SUB_ID,
                        "enable": True,
                        "expiryTime": _ms("2026-09-19T12:00:00"),
                        "totalGB": TRAFFIC_LIMIT_BYTES,
                        "reset": 30,
                    },
                    {
                        "id": second_uuid,
                        "email": second_email,
                        "subId": "second-sub-id",
                        "enable": True,
                        "expiryTime": _ms("2026-09-19T12:00:00"),
                        "totalGB": TRAFFIC_LIMIT_BYTES,
                        "reset": 30,
                    },
                ]
            }
        ),
        "clientStats": [
            {
                "email": BASE_EMAIL,
                "enable": True,
                "expiryTime": _ms("2026-07-19T12:00:00"),
                "total": TRAFFIC_LIMIT_BYTES,
                "up": 10,
                "down": 20,
                "reset": 30,
            },
            {
                "email": second_email,
                "enable": True,
                "expiryTime": _ms("2026-07-19T12:00:00"),
                "total": TRAFFIC_LIMIT_BYTES,
                "up": 30,
                "down": 40,
                "reset": 30,
            },
        ],
    }
    posts = []
    monkeypatch.setattr(
        xui_api,
        "scan_mainpanel_subscription_inbounds",
        lambda _host: {
            "api": SimpleNamespace(),
            "inbounds": [
                {
                    "inbound_id": 1,
                    "classification": xui_api.MAINPANEL_INBOUND_CLASS_WHITELIST,
                }
            ],
        },
    )
    monkeypatch.setattr(xui_api, "_get_inbound_raw_by_id", lambda *_args, **_kwargs: raw_inbound)

    def _post(_api, endpoint, payload):
        posts.append((endpoint, payload))
        clients = json.loads(raw_inbound["settings"])["clients"]
        clients_by_email = {client["email"]: client for client in clients}
        stats_by_email = {stat["email"]: stat for stat in raw_inbound["clientStats"]}
        receipt_items = []
        for index, request in enumerate(payload["items"]):
            clients_by_email[request["email"]].update(
                {
                    "expiryTime": request["target"]["expiryTime"],
                    "totalGB": request["target"]["total"],
                    "reset": request["target"]["reset"],
                }
            )
            stats_by_email[request["email"]].update(
                {
                    "expiryTime": request["target"]["expiryTime"],
                    "total": request["target"]["total"],
                    "reset": request["target"]["reset"],
                    "up": 0 if request["resetTraffic"] else stats_by_email[request["email"]]["up"],
                    "down": 0 if request["resetTraffic"] else stats_by_email[request["email"]]["down"],
                }
            )
            receipt_items.append(
                {
                    "index": index,
                    "status": "updated",
                    "reason": "applied",
                    "trafficReset": request["resetTraffic"],
                    "reenabled": False,
                }
            )
        raw_inbound["settings"] = json.dumps({"clients": clients})
        return {
            "success": True,
            "obj": {
                "updated": 2,
                "needRestart": False,
                "items": receipt_items,
            },
        }

    monkeypatch.setattr(xui_api, "_api_post_json_once_raw", _post)
    result = xui_api.repair_whitelist_reset_cycles_on_host(
        "MainPanel",
        inbound_id=1,
        repairs=[
            {
                "email": BASE_EMAIL,
                "expected_client_uuid": CLIENT_UUID,
                "expected_sub_id": SUB_ID,
                "target_reset_ms": target_reset_ms,
                "target_reset_days": 30,
                "reset_traffic": True,
            },
            {
                "email": second_email,
                "expected_client_uuid": second_uuid,
                "expected_sub_id": "second-sub-id",
                "target_reset_ms": target_reset_ms,
                "target_reset_days": 30,
                "reset_traffic": False,
            },
        ],
    )

    assert result["status"] == "updated"
    assert len(result["items"]) == 2
    assert [item["reset_performed"] for item in result["items"]] == [True, False]
    assert len(posts) == 1
    assert len(posts[0][1]["items"]) == 2


def test_inbound_scoped_cycle_update_preserves_shared_sub_id_without_runtime_fields(monkeypatch):
    from shop_bot.modules import xui_api

    calls: list[tuple[str, dict[str, Any]]] = []
    sibling_email = "user100000001-key1-finland-2@vpn.bot"
    panel_state = {
        7: {
            "streamSettings": '{"network":"ws","wsSettings":{"path":"/white"}}',
            "client": {
                "id": CLIENT_UUID,
                "email": BASE_EMAIL,
                "subId": SUB_ID,
                "expiryTime": _ms("2026-09-19T12:00:00"),
                "reset": 30,
            },
        },
        8: {
            "streamSettings": '{"network":"tcp","security":"reality"}',
            "client": {
                "id": CLIENT_UUID,
                "email": sibling_email,
                "subId": SUB_ID,
                "expiryTime": _ms("2026-09-19T12:00:00"),
                "reset": 0,
            },
        },
    }
    sibling_before = json.loads(json.dumps(panel_state[8]))
    streams_before = {inbound_id: state["streamSettings"] for inbound_id, state in panel_state.items()}

    def _post(_api, endpoint, payload):
        calls.append((endpoint, payload))
        selected_inbound_id = int(payload["id"])
        panel_state[selected_inbound_id]["client"] = json.loads(payload["settings"])["clients"][0]
        return {"success": True}

    monkeypatch.setattr(xui_api, "_api_post_json", _post)

    result = xui_api._api_post_inbound_client_update(
        SimpleNamespace(),
        inbound_id=7,
        expected_email=BASE_EMAIL,
        expected_client_uuid=CLIENT_UUID,
        client={
            "id": CLIENT_UUID,
            "email": BASE_EMAIL,
            "subId": SUB_ID,
            "enable": True,
            "expiryTime": _ms("2026-08-18T12:00:00"),
            "totalGB": TRAFFIC_LIMIT_BYTES,
            "reset": 30,
            "up": 123,
            "down": 456,
            "total": 579,
            "inboundId": 99,
        },
    )

    assert result == {"success": True}
    assert len(calls) == 1
    endpoint, request = calls[0]
    assert endpoint == f"panel/api/inbounds/updateClient/{CLIENT_UUID}"
    assert request["id"] == 7
    clients = json.loads(request["settings"])["clients"]
    assert len(clients) == 1
    assert clients[0]["id"] == CLIENT_UUID
    assert clients[0]["email"] == BASE_EMAIL
    assert clients[0]["subId"] == SUB_ID
    assert clients[0]["expiryTime"] == _ms("2026-08-18T12:00:00")
    assert clients[0]["totalGB"] == TRAFFIC_LIMIT_BYTES
    assert clients[0]["reset"] == 30
    assert "up" not in clients[0]
    assert "down" not in clients[0]
    assert "total" not in clients[0]
    assert "inboundId" not in clients[0]
    assert "streamSettings" not in request
    assert panel_state[8] == sibling_before
    assert {inbound_id: state["streamSettings"] for inbound_id, state in panel_state.items()} == streams_before


def test_ambiguous_atomic_reset_post_fails_even_at_target_and_never_retries(monkeypatch):
    from shop_bot.modules import xui_api

    old_reset_ms = _ms("2026-09-19T12:00:00")
    target_reset_ms = _ms("2026-08-18T12:00:00")
    stream_settings = '{"network":"ws","wsSettings":{"path":"/api/user"}}'
    raw_inbound = {
        "id": 1,
        "remark": "WHITELIST1",
        "protocol": "vless",
        "enable": True,
        "streamSettings": stream_settings,
        "settings": json.dumps(
            {
                "clients": [
                    {
                        "id": CLIENT_UUID,
                        "email": BASE_EMAIL,
                        "subId": SUB_ID,
                        "enable": True,
                        "expiryTime": old_reset_ms,
                        "totalGB": TRAFFIC_LIMIT_BYTES,
                        "reset": 30,
                    }
                ]
            }
        ),
        "clientStats": [
            {
                "email": BASE_EMAIL,
                "enable": True,
                "expiryTime": old_reset_ms,
                "total": TRAFFIC_LIMIT_BYTES,
                "up": TRAFFIC_LIMIT_BYTES // 3,
                "down": 0,
                "reset": 30,
            }
        ],
    }
    post_calls = 0

    monkeypatch.setattr(
        xui_api,
        "scan_mainpanel_subscription_inbounds",
        lambda _host: {
            "api": SimpleNamespace(),
            "inbounds": [
                {
                    "inbound_id": 1,
                    "classification": xui_api.MAINPANEL_INBOUND_CLASS_WHITELIST,
                }
            ],
        },
    )
    monkeypatch.setattr(
        xui_api,
        "_get_inbound_raw_by_id",
        lambda *_args, **_kwargs: raw_inbound,
    )

    def _post(_api, endpoint, payload):
        nonlocal post_calls
        assert endpoint == "panel/api/inbounds/1/repairClientTrafficCycles"
        post_calls += 1
        request = payload["items"][0]
        assert request["resetTraffic"] is True
        clients = json.loads(raw_inbound["settings"])["clients"]
        clients[0].update(
            {
                "expiryTime": request["target"]["expiryTime"],
                "totalGB": request["target"]["total"],
                "reset": request["target"]["reset"],
            }
        )
        raw_inbound["settings"] = json.dumps({"clients": clients})
        raw_inbound["clientStats"][0].update(
            {
                "expiryTime": request["target"]["expiryTime"],
                "total": request["target"]["total"],
                "reset": request["target"]["reset"],
                "up": 0,
                "down": 0,
            }
        )
        raise TimeoutError("response lost after commit")

    monkeypatch.setattr(xui_api, "_api_post_json_once_raw", _post)
    monkeypatch.setattr(
        xui_api,
        "_reset_client_traffic_on_panel",
        lambda *_args, **_kwargs: pytest.fail("legacy reset endpoint is forbidden"),
    )
    monkeypatch.setattr(
        xui_api,
        "_api_post_inbound_client_update",
        lambda *_args, **_kwargs: pytest.fail("legacy updateClient endpoint is forbidden"),
    )
    monkeypatch.setattr(
        xui_api,
        "_api_post_client_update",
        lambda *_args, **_kwargs: pytest.fail("centralized fallback is forbidden"),
    )
    monkeypatch.setattr(
        xui_api,
        "_update_inbound_clients_preserving_raw_stream_settings",
        lambda *_args, **_kwargs: pytest.fail("full inbound fallback is forbidden"),
    )

    recovered = xui_api.repair_whitelist_reset_cycle_on_host(
        "MainPanel",
        inbound_id=1,
        email=BASE_EMAIL,
        expected_client_uuid=CLIENT_UUID,
        expected_sub_id=SUB_ID,
        target_reset_ms=target_reset_ms,
        target_reset_days=30,
        reset_traffic=True,
    )
    idempotent = xui_api.repair_whitelist_reset_cycle_on_host(
        "MainPanel",
        inbound_id=1,
        email=BASE_EMAIL,
        expected_client_uuid=CLIENT_UUID,
        expected_sub_id=SUB_ID,
        target_reset_ms=target_reset_ms,
        target_reset_days=30,
        # Even if an operator accidentally repeats the original intent, the
        # fresh durable read suppresses a second reset POST.
        reset_traffic=True,
    )

    assert recovered["status"] == "failed"
    assert recovered["reason"] == "post_outcome_ambiguous"
    assert recovered["receipt_confirmed"] is False
    assert recovered["reset_performed"] is True
    assert recovered["reset_outcome_ambiguous"] is True
    assert recovered["safe_retry_reset_traffic"] is False
    assert recovered["verified_reset_ms"] == target_reset_ms
    assert recovered["verified_reset_days"] == 30
    assert idempotent["status"] == "already_correct"
    assert idempotent["reset_performed"] is False
    assert idempotent["reset_suppressed"] is True
    assert post_calls == 1
    assert raw_inbound["clientStats"][0]["up"] == 0
    assert raw_inbound["streamSettings"] == stream_settings


def test_settings_only_drift_is_repaired_without_resetting_counters(monkeypatch):
    from shop_bot.modules import xui_api

    target_reset_ms = _ms("2026-08-18T12:00:00")
    raw_inbound = {
        "id": 1,
        "remark": "WHITELIST1",
        "protocol": "vless",
        "enable": True,
        "streamSettings": '{"network":"ws"}',
        "settings": json.dumps(
            {
                "clients": [
                    {
                        "id": CLIENT_UUID,
                        "email": BASE_EMAIL,
                        "subId": SUB_ID,
                        "enable": True,
                        "expiryTime": _ms("2026-09-19T12:00:00"),
                        "totalGB": TRAFFIC_LIMIT_BYTES,
                        "reset": 30,
                    }
                ]
            }
        ),
        "clientStats": [
            {
                "email": BASE_EMAIL,
                "enable": True,
                "expiryTime": target_reset_ms,
                "total": TRAFFIC_LIMIT_BYTES,
                "up": 321,
                "down": 654,
                "reset": 30,
            }
        ],
    }
    posts = []
    monkeypatch.setattr(
        xui_api,
        "scan_mainpanel_subscription_inbounds",
        lambda _host: {
            "api": SimpleNamespace(),
            "inbounds": [
                {
                    "inbound_id": 1,
                    "classification": xui_api.MAINPANEL_INBOUND_CLASS_WHITELIST,
                }
            ],
        },
    )
    monkeypatch.setattr(xui_api, "_get_inbound_raw_by_id", lambda *_args, **_kwargs: raw_inbound)

    def _post(_api, endpoint, payload):
        posts.append((endpoint, payload))
        request = payload["items"][0]
        assert request["resetTraffic"] is False
        client = json.loads(raw_inbound["settings"])["clients"][0]
        client.update(
            {
                "expiryTime": request["target"]["expiryTime"],
                "totalGB": request["target"]["total"],
                "reset": request["target"]["reset"],
            }
        )
        raw_inbound["settings"] = json.dumps({"clients": [client]})
        return {
            "success": True,
            "obj": {
                "updated": 1,
                "needRestart": False,
                "items": [
                    {
                        "index": 0,
                        "status": "updated",
                        "reason": "applied",
                        "trafficReset": False,
                        "reenabled": False,
                    }
                ],
            },
        }

    monkeypatch.setattr(xui_api, "_api_post_json_once_raw", _post)
    result = xui_api.repair_whitelist_reset_cycle_on_host(
        "MainPanel",
        inbound_id=1,
        email=BASE_EMAIL,
        expected_client_uuid=CLIENT_UUID,
        expected_sub_id=SUB_ID,
        target_reset_ms=target_reset_ms,
        target_reset_days=30,
        reset_traffic=True,
    )

    assert result["status"] == "updated"
    assert result["reset_performed"] is False
    assert result["reset_suppressed"] is True
    assert raw_inbound["clientStats"][0]["up"] == 321
    assert raw_inbound["clientStats"][0]["down"] == 654
    assert len(posts) == 1


@pytest.mark.parametrize("decreased_counter", ["up", "down"])
def test_no_reset_readback_fails_when_a_traffic_counter_decreases(
    monkeypatch,
    decreased_counter,
):
    from shop_bot.modules import xui_api

    target_reset_ms = _ms("2026-08-18T12:00:00")
    expected_up = 321
    expected_down = 654
    raw_inbound = {
        "id": 1,
        "remark": "WHITELIST1",
        "protocol": "vless",
        "enable": True,
        "streamSettings": '{"network":"ws"}',
        "settings": json.dumps(
            {
                "clients": [
                    {
                        "id": CLIENT_UUID,
                        "email": BASE_EMAIL,
                        "subId": SUB_ID,
                        "enable": True,
                        "expiryTime": _ms("2026-09-19T12:00:00"),
                        "totalGB": TRAFFIC_LIMIT_BYTES,
                        "reset": 30,
                    }
                ]
            }
        ),
        "clientStats": [
            {
                "email": BASE_EMAIL,
                "enable": True,
                "expiryTime": target_reset_ms,
                "total": TRAFFIC_LIMIT_BYTES,
                "up": expected_up,
                "down": expected_down,
                "reset": 30,
            }
        ],
    }
    posts = []
    monkeypatch.setattr(
        xui_api,
        "scan_mainpanel_subscription_inbounds",
        lambda _host: {
            "api": SimpleNamespace(),
            "inbounds": [
                {
                    "inbound_id": 1,
                    "classification": xui_api.MAINPANEL_INBOUND_CLASS_WHITELIST,
                }
            ],
        },
    )
    monkeypatch.setattr(xui_api, "_get_inbound_raw_by_id", lambda *_args, **_kwargs: raw_inbound)

    def _post(_api, endpoint, payload):
        posts.append((endpoint, payload))
        request = payload["items"][0]
        assert request["resetTraffic"] is False
        assert request["expectedTraffic"]["up"] == expected_up
        assert request["expectedTraffic"]["down"] == expected_down
        client = json.loads(raw_inbound["settings"])["clients"][0]
        client.update(
            {
                "expiryTime": request["target"]["expiryTime"],
                "totalGB": request["target"]["total"],
                "reset": request["target"]["reset"],
            }
        )
        raw_inbound["settings"] = json.dumps({"clients": [client]})
        raw_inbound["clientStats"][0][decreased_counter] -= 1
        return {
            "success": True,
            "obj": {
                "updated": 1,
                "needRestart": False,
                "items": [
                    {
                        "index": 0,
                        "status": "updated",
                        "reason": "applied",
                        "trafficReset": False,
                        "reenabled": False,
                    }
                ],
            },
        }

    monkeypatch.setattr(xui_api, "_api_post_json_once_raw", _post)
    result = xui_api.repair_whitelist_reset_cycle_on_host(
        "MainPanel",
        inbound_id=1,
        email=BASE_EMAIL,
        expected_client_uuid=CLIENT_UUID,
        expected_sub_id=SUB_ID,
        target_reset_ms=target_reset_ms,
        target_reset_days=30,
        reset_traffic=True,
    )

    assert result["status"] == "failed"
    assert result["reason"] == "traffic_counters_decreased"
    assert result["receipt_confirmed"] is True
    assert result["reset_performed"] is False
    assert result["reset_outcome_ambiguous"] is False
    assert result["safe_retry_reset_traffic"] is False
    assert len(posts) == 1


def test_unresolved_timeout_is_ambiguous_and_never_retries_post(monkeypatch):
    from shop_bot.modules import xui_api

    old_reset_ms = _ms("2026-07-19T12:00:00")
    raw_inbound = {
        "id": 1,
        "remark": "WHITELIST1",
        "protocol": "vless",
        "enable": True,
        "streamSettings": '{"network":"ws"}',
        "settings": json.dumps(
            {
                "clients": [
                    {
                        "id": CLIENT_UUID,
                        "email": BASE_EMAIL,
                        "subId": SUB_ID,
                        "enable": True,
                        "expiryTime": old_reset_ms,
                        "totalGB": TRAFFIC_LIMIT_BYTES,
                        "reset": 30,
                    }
                ]
            }
        ),
        "clientStats": [
            {
                "email": BASE_EMAIL,
                "enable": True,
                "expiryTime": old_reset_ms,
                "total": TRAFFIC_LIMIT_BYTES,
                "up": 123,
                "down": 456,
                "reset": 30,
            }
        ],
    }
    post_calls: list[tuple[str, dict[str, Any]]] = []
    inbound_api = SimpleNamespace(
        session="test-session",
        cookies={"3x-ui": "test-session"},
        use_tls_verify=True,
        custom_certificate_path="/tmp/test-panel-ca.pem",
        _url=lambda endpoint: f"https://panel.example/{endpoint}",
    )
    api = SimpleNamespace(inbound=inbound_api)
    monkeypatch.setattr(
        xui_api,
        "scan_mainpanel_subscription_inbounds",
        lambda _host: {
            "api": api,
            "inbounds": [
                {
                    "inbound_id": 1,
                    "classification": xui_api.MAINPANEL_INBOUND_CLASS_WHITELIST,
                }
            ],
        },
    )
    monkeypatch.setattr(xui_api, "_get_inbound_raw_by_id", lambda *_args, **_kwargs: raw_inbound)

    def _timeout(url, **kwargs):
        post_calls.append((url, kwargs))
        raise xui_api.requests.exceptions.Timeout(
            f"unknown server outcome for {BASE_EMAIL}"
        )

    monkeypatch.setattr(xui_api.requests, "post", _timeout)
    for forbidden in (
        "_reset_client_traffic_on_panel",
        "_api_post_inbound_client_update",
        "_api_post_client_update",
        "_update_inbound_clients_preserving_raw_stream_settings",
    ):
        monkeypatch.setattr(
            xui_api,
            forbidden,
            lambda *_args, **_kwargs: pytest.fail("legacy fallback is forbidden"),
        )
    result = xui_api.repair_whitelist_reset_cycle_on_host(
        "MainPanel",
        inbound_id=1,
        email=BASE_EMAIL,
        expected_client_uuid=CLIENT_UUID,
        expected_sub_id=SUB_ID,
        target_reset_ms=_ms("2026-08-18T12:00:00"),
        target_reset_days=30,
        reset_traffic=True,
    )

    assert result["status"] == "failed"
    assert result["reason"] == "post_outcome_ambiguous"
    assert result["reset_outcome_ambiguous"] is True
    assert result["safe_retry_reset_traffic"] is False
    assert result["receipt_confirmed"] is False
    assert BASE_EMAIL not in json.dumps(result)
    assert CLIENT_UUID not in json.dumps(result)
    assert len(post_calls) == 1
    post_url, post_kwargs = post_calls[0]
    assert post_url == "https://panel.example/panel/api/inbounds/1/repairClientTrafficCycles"
    assert post_kwargs["cookies"] == {"3x-ui": "test-session"}
    assert post_kwargs["allow_redirects"] is False
    assert post_kwargs["headers"] == {"Accept": "application/json"}
    assert post_kwargs["timeout"] == xui_api.XUI_API_TIMEOUT
    assert post_kwargs["verify"] == "/tmp/test-panel-ca.pem"
    assert post_kwargs["json"]["items"][0]["resetTraffic"] is True


def test_atomic_success_false_exposes_exact_cas_rejection_without_retry_or_fallback(
    monkeypatch,
):
    from shop_bot.modules import xui_api

    old_reset_ms = _ms("2026-07-19T12:00:00")
    raw_inbound = {
        "id": 1,
        "remark": "WHITELIST1",
        "protocol": "vless",
        "enable": True,
        "streamSettings": '{"network":"ws"}',
        "settings": json.dumps(
            {
                "clients": [
                    {
                        "id": CLIENT_UUID,
                        "email": BASE_EMAIL,
                        "subId": SUB_ID,
                        "enable": True,
                        "expiryTime": old_reset_ms,
                        "totalGB": TRAFFIC_LIMIT_BYTES,
                        "reset": 30,
                    }
                ]
            }
        ),
        "clientStats": [
            {
                "email": BASE_EMAIL,
                "enable": True,
                "expiryTime": old_reset_ms,
                "total": TRAFFIC_LIMIT_BYTES,
                "up": 123,
                "down": 456,
                "reset": 30,
            }
        ],
    }
    inbound_api = SimpleNamespace(
        session=None,
        cookies={},
        use_tls_verify=False,
        custom_certificate_path=None,
        _url=lambda endpoint: f"https://panel.example/{endpoint}",
    )
    api = SimpleNamespace(inbound=inbound_api, _pepel_api_token="test-bearer-token")
    read_calls = 0
    post_calls = 0
    monkeypatch.setattr(
        xui_api,
        "scan_mainpanel_subscription_inbounds",
        lambda _host: {
            "api": api,
            "inbounds": [
                {
                    "inbound_id": 1,
                    "classification": xui_api.MAINPANEL_INBOUND_CLASS_WHITELIST,
                }
            ],
        },
    )

    def _read(*_args, **_kwargs):
        nonlocal read_calls
        read_calls += 1
        return raw_inbound

    def _post(url, **kwargs):
        nonlocal post_calls
        post_calls += 1
        assert url == "https://panel.example/panel/api/inbounds/1/repairClientTrafficCycles"
        assert kwargs["verify"] is False
        assert kwargs["allow_redirects"] is False
        assert kwargs["cookies"] == {}
        assert kwargs["headers"] == {
            "Accept": "application/json",
            "Authorization": "Bearer test-bearer-token",
        }
        response = xui_api.requests.Response()
        response.status_code = 200
        response.url = url
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {
                "success": False,
                "msg": f"CAS rejected for {BASE_EMAIL}",
                "obj": {
                    "updated": 0,
                    "items": [
                        {
                            "index": 0,
                            "status": "rejected",
                            "reason": "stale_traffic_precondition",
                        }
                    ],
                },
            }
        ).encode()
        return response

    monkeypatch.setattr(xui_api, "_get_inbound_raw_by_id", _read)
    monkeypatch.setattr(xui_api.requests, "post", _post)
    for forbidden in (
        "_reset_client_traffic_on_panel",
        "_api_post_inbound_client_update",
        "_api_post_client_update",
        "_update_inbound_clients_preserving_raw_stream_settings",
    ):
        monkeypatch.setattr(
            xui_api,
            forbidden,
            lambda *_args, **_kwargs: pytest.fail("legacy fallback is forbidden"),
        )

    result = xui_api.repair_whitelist_reset_cycle_on_host(
        "MainPanel",
        inbound_id=1,
        email=BASE_EMAIL,
        expected_client_uuid=CLIENT_UUID,
        expected_sub_id=SUB_ID,
        target_reset_ms=_ms("2026-08-18T12:00:00"),
        target_reset_days=30,
        reset_traffic=True,
    )

    assert result == {
        "status": "failed",
        "reason": "stale_traffic_precondition",
        "reset_performed": False,
    }
    assert post_calls == 1
    assert read_calls == 1
    assert BASE_EMAIL not in json.dumps(result)
    assert CLIENT_UUID not in json.dumps(result)


def test_atomic_cycle_repair_rejects_more_than_50_items_before_panel_access(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.setattr(
        xui_api,
        "scan_mainpanel_subscription_inbounds",
        lambda *_args, **_kwargs: pytest.fail("oversize batch must fail before panel access"),
    )
    result = xui_api.repair_whitelist_reset_cycles_on_host(
        "MainPanel",
        inbound_id=1,
        repairs=[
            {
                "email": BASE_EMAIL,
                "expected_client_uuid": CLIENT_UUID,
                "expected_sub_id": SUB_ID,
                "target_reset_ms": _ms("2026-08-18T12:00:00"),
                "target_reset_days": 30,
                "reset_traffic": False,
            }
        ]
        * 51,
    )

    assert result == {"status": "failed", "reason": "invalid_arguments", "items": []}


def test_atomic_cycle_repair_source_has_no_legacy_or_full_inbound_fallback():
    from shop_bot.modules import xui_api

    source = inspect.getsource(xui_api.repair_whitelist_reset_cycles_on_host)
    transport_source = inspect.getsource(xui_api._api_post_json_once_raw)

    assert "repairClientTrafficCycles" in source
    assert "_api_post_json_once_raw" in source
    assert "_api_post_json(" not in source
    assert "requests.post(" in transport_source
    assert "._post(" not in transport_source
    assert "_request_with_retry" not in transport_source
    for forbidden in (
        "resetClientTraffic",
        "updateClient/",
        "_reset_client_traffic_on_panel",
        "_api_post_inbound_client_update",
        "_api_post_client_update",
        "_update_inbound_clients_preserving_raw_stream_settings",
    ):
        assert forbidden not in source


def test_inbound_scoped_cycle_update_rejects_identity_mismatch_before_post(monkeypatch):
    from shop_bot.modules import xui_api

    monkeypatch.setattr(
        xui_api,
        "_api_post_json",
        lambda *_args, **_kwargs: pytest.fail("identity mismatch must fail before panel write"),
    )

    with pytest.raises(ValueError, match="inbound_client_identity_mismatch"):
        xui_api._api_post_inbound_client_update(
            SimpleNamespace(),
            inbound_id=7,
            expected_email=BASE_EMAIL,
            expected_client_uuid=CLIENT_UUID,
            client={
                "id": "foreign-uuid",
                "email": BASE_EMAIL,
                "subId": SUB_ID,
            },
        )
