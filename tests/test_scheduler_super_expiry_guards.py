from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace


def _run_panel_sync(
    monkeypatch,
    *,
    db_key: dict,
    panel_client,
    panel_delete_result: bool = True,
) -> dict[str, list]:
    from shop_bot.data_manager import scheduler
    from shop_bot.modules import xui_api

    host = {
        "host_name": "MainPanel",
        "host_url": "https://panel.example.test/xui/",
        "host_username": "u",
        "host_pass": "p",
        "host_inbound_id": 1,
        "is_maintenance": 0,
        "status": "healthy",
    }
    fake_api = SimpleNamespace(inbound=SimpleNamespace())
    fake_inbound = SimpleNamespace(id=1)
    calls: dict[str, list] = {
        "panel_delete": [],
        "db_delete": [],
        "db_sync": [],
    }

    def _delete_key_by_email(email: str) -> None:
        calls["db_delete"].append(email)

    monkeypatch.setattr(scheduler.database, "get_all_hosts", lambda: [host])
    monkeypatch.setattr(scheduler.database, "get_keys_for_host", lambda _host_name: [db_key])
    monkeypatch.setattr(scheduler.database, "update_host_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scheduler.database,
        "delete_key_by_email",
        _delete_key_by_email,
    )
    monkeypatch.setattr(
        scheduler.database,
        "update_key_status_from_server",
        lambda email, client: calls["db_sync"].append((email, client)),
    )
    monkeypatch.setattr(
        scheduler,
        "_probe_host_health",
        lambda _host: (
            SimpleNamespace(api=fake_api, inbound=fake_inbound, recovered_on_retry=False),
            "healthy",
            None,
        ),
    )
    clients_by_email = {}
    if panel_client is not None:
        clients_by_email[db_key["key_email"]] = panel_client
    monkeypatch.setattr(
        scheduler.xui_api,
        "scan_subscription_inbounds_clients_minimal",
        lambda *_args, **_kwargs: xui_api.PanelClientScan(
            clients_by_email=clients_by_email,
            orphan_total=0,
            orphan_sample=[],
        ),
    )

    async def _delete_client(host_name: str, email: str) -> bool:
        calls["panel_delete"].append((host_name, email))
        return panel_delete_result

    async def _no_blitz_sync() -> int:
        return 0

    monkeypatch.setattr(scheduler.xui_api, "delete_client_on_host", _delete_client)
    monkeypatch.setattr(scheduler, "_sync_blitz_keys", _no_blitz_sync)

    asyncio.run(scheduler.sync_keys_with_panels())
    return calls


def test_active_super_paid_expiry_is_not_overwritten_by_panel_reset(monkeypatch):
    from shop_bot.modules import xui_api

    now = datetime.now(UTC).replace(tzinfo=None)
    local_expiry = now + timedelta(days=90)
    panel_reset_ms = int((now + timedelta(days=30)).timestamp() * 1000)
    key = {
        "key_email": "managed-super@example.test",
        "expiry_date": local_expiry.isoformat(),
        "subscription_tier": "super",
    }
    panel_client = xui_api.PanelClientSummary(
        id="client-id",
        email=key["key_email"],
        expiry_time=panel_reset_ms,
    )

    calls = _run_panel_sync(monkeypatch, db_key=key, panel_client=panel_client)

    assert calls["db_sync"] == []
    assert calls["db_delete"] == []
    assert calls["panel_delete"] == []


def test_active_super_missing_from_panel_scan_keeps_paid_db_record(monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None)
    key = {
        "key_email": "missing-super@example.test",
        "expiry_date": (now + timedelta(days=30)).isoformat(),
        "subscription_tier": "SUPER",
    }

    calls = _run_panel_sync(monkeypatch, db_key=key, panel_client=None)

    assert calls["db_sync"] == []
    assert calls["db_delete"] == []
    assert calls["panel_delete"] == []


def test_expired_super_has_zero_grace_and_deletes_db_after_panel_success(monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None)
    key = {
        "key_email": "expired-super@example.test",
        "expiry_date": (now - timedelta(seconds=1)).isoformat(),
        "subscription_tier": "super",
    }

    calls = _run_panel_sync(monkeypatch, db_key=key, panel_client=None)

    assert calls["panel_delete"] == [("MainPanel", key["key_email"])]
    assert calls["db_delete"] == [key["key_email"]]
    assert calls["db_sync"] == []


def test_expired_super_keeps_db_record_when_panel_delete_is_not_confirmed(monkeypatch):
    now = datetime.now(UTC).replace(tzinfo=None)
    key = {
        "key_email": "retry-super@example.test",
        "expiry_date": (now - timedelta(seconds=1)).isoformat(),
        "subscription_tier": "super",
    }

    calls = _run_panel_sync(
        monkeypatch,
        db_key=key,
        panel_client=None,
        panel_delete_result=False,
    )

    assert calls["panel_delete"] == [("MainPanel", key["key_email"])]
    assert calls["db_delete"] == []
    assert calls["db_sync"] == []


def test_expired_standard_key_uses_current_zero_grace_policy(monkeypatch):
    from shop_bot.modules import xui_api

    now = datetime.now(UTC).replace(tzinfo=None)
    local_expiry = now - timedelta(days=1)
    key = {
        "key_email": "standard-grace@example.test",
        "expiry_date": local_expiry.isoformat(),
        "subscription_tier": "standard",
    }
    panel_client = xui_api.PanelClientSummary(
        id="client-id",
        email=key["key_email"],
        expiry_time=int(local_expiry.timestamp() * 1000),
    )

    calls = _run_panel_sync(monkeypatch, db_key=key, panel_client=panel_client)

    assert calls["panel_delete"] == [("MainPanel", key["key_email"])]
    assert calls["db_delete"] == [key["key_email"]]
    assert calls["db_sync"] == []
