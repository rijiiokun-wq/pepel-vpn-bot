from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_sync_keys_with_panels_host_exception_is_isolated(tmp_path, monkeypatch):
    from shop_bot.data_manager import scheduler

    host = {
        "host_name": "SUPER",
        "host_url": "https://super.example/xui/",
        "host_username": "u",
        "host_pass": "p",
        "host_inbound_id": 1,
        "is_maintenance": 0,
        "status": "healthy",
    }

    class _InboundApi:
        def get_by_id(self, _inbound_id):
            raise AssertionError("sync must not load full inbound clients")

    fake_api = SimpleNamespace(inbound=_InboundApi())
    fake_inbound = SimpleNamespace(id=1)

    status_updates: list[tuple[str, str, str | None]] = []
    alerts: list[tuple[object, str]] = []

    monkeypatch.setenv("RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler.database, "get_all_hosts", lambda: [host])
    monkeypatch.setattr(scheduler.database, "get_keys_for_host", lambda _host_name: [])
    monkeypatch.setattr(
        scheduler.database,
        "update_host_status",
        lambda host_name, status, error=None: status_updates.append((host_name, status, error)),
    )
    monkeypatch.setattr(
        scheduler,
        "_probe_host_health",
        lambda _host: (
            SimpleNamespace(
                api=fake_api,
                inbound=fake_inbound,
                recovered_on_retry=False,
            ),
            "healthy",
            None,
        ),
    )
    monkeypatch.setattr(
        scheduler.xui_api,
        "scan_inbound_clients_minimal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("panel read failed")),
    )

    async def _no_blitz_sync():
        return 0

    monkeypatch.setattr(scheduler, "_sync_blitz_keys", _no_blitz_sync)
    monkeypatch.setattr(
        scheduler,
        "send_critical_alert_nonblocking",
        lambda error, **kwargs: alerts.append((error, kwargs.get("details", ""))) or True,
    )

    asyncio.run(scheduler.sync_keys_with_panels())

    assert status_updates == []
    assert alerts
    assert "reason=panel_raw_lookup_failed" in alerts[-1][1]


def test_sync_keys_with_panels_uses_minimal_panel_scan(monkeypatch):
    from shop_bot.data_manager import scheduler
    from shop_bot.modules import xui_api

    host = {
        "host_name": "SUPER",
        "host_url": "https://super.example/xui/",
        "host_username": "u",
        "host_pass": "p",
        "host_inbound_id": 1,
        "is_maintenance": 0,
        "status": "healthy",
    }

    class _InboundApi:
        def get_by_id(self, _inbound_id):
            raise AssertionError("sync must not load full inbound clients")

    fake_api = SimpleNamespace(inbound=_InboundApi())
    fake_inbound = SimpleNamespace(id=1)
    local_expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=7)
    remote_expiry_ms = int((local_expiry + timedelta(days=1)).timestamp() * 1000)
    updated_keys: list[tuple[str, str | None, int | None]] = []
    scan_calls: list[tuple[object, int, set[str], int]] = []

    def _scan(api, inbound_id, local_key_emails, orphan_sample_limit):
        scan_calls.append((api, inbound_id, set(local_key_emails), orphan_sample_limit))
        return xui_api.PanelClientScan(
            clients_by_email={
                "local@example.com": xui_api.PanelClientSummary(
                    id="client-uuid",
                    email="local@example.com",
                    expiry_time=remote_expiry_ms,
                )
            },
            orphan_total=1,
            orphan_sample=["orphan@example.com"],
        )

    monkeypatch.setattr(scheduler.database, "get_all_hosts", lambda: [host])
    monkeypatch.setattr(
        scheduler.database,
        "get_keys_for_host",
        lambda _host_name: [
            {
                "key_email": "local@example.com",
                "expiry_date": local_expiry.isoformat(),
            }
        ],
    )
    monkeypatch.setattr(scheduler.database, "update_host_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scheduler.database,
        "update_key_status_from_server",
        lambda email, client: updated_keys.append((email, client.id, client.expiry_time)),
    )
    monkeypatch.setattr(
        scheduler,
        "_probe_host_health",
        lambda _host: (
            SimpleNamespace(
                api=fake_api,
                inbound=fake_inbound,
                recovered_on_retry=False,
            ),
            "healthy",
            None,
        ),
    )
    monkeypatch.setattr(scheduler.xui_api, "scan_inbound_clients_minimal", _scan)

    async def _no_blitz_sync():
        return 0

    monkeypatch.setattr(scheduler, "_sync_blitz_keys", _no_blitz_sync)

    asyncio.run(scheduler.sync_keys_with_panels())

    assert scan_calls == [(fake_api, 1, {"local@example.com"}, scheduler.PANEL_SYNC_ORPHAN_SAMPLE_LIMIT)]
    assert updated_keys == [("local@example.com", "client-uuid", remote_expiry_ms)]


def test_sync_keys_with_panels_skips_xui_hosts_on_warning_memory_pressure(monkeypatch):
    from shop_bot import runtime_guard
    from shop_bot.data_manager import scheduler

    alerts: list[str] = []
    snapshot = runtime_guard.MemorySnapshot(
        rss_bytes=85 * 1024 * 1024,
        cgroup_current_bytes=85 * 1024 * 1024,
        cgroup_limit_bytes=100 * 1024 * 1024,
    )

    monkeypatch.setenv("RUNTIME_MEMORY_WARNING_PERCENT", "80")
    monkeypatch.setenv("RUNTIME_MEMORY_CRITICAL_PERCENT", "90")
    monkeypatch.setattr(scheduler.runtime_guard, "get_memory_snapshot", lambda: snapshot)
    monkeypatch.setattr(
        scheduler.runtime_guard,
        "emit_memory_pressure_alert_if_needed",
        lambda **kwargs: alerts.append(kwargs["reason"]) or True,
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_all_hosts",
        lambda: (_ for _ in ()).throw(AssertionError("XUI hosts must be skipped under warning pressure")),
    )

    async def _no_blitz_sync():
        return 0

    monkeypatch.setattr(scheduler, "_sync_blitz_keys", _no_blitz_sync)

    asyncio.run(scheduler.sync_keys_with_panels())

    assert alerts == ["scheduler_sync_memory_guard"]


def test_periodic_subscription_check_skips_panel_sync_for_reserve_by_default(monkeypatch):
    from shop_bot.data_manager import scheduler

    calls: list[str] = []

    async def _record(name: str):
        calls.append(name)
        return {}

    monkeypatch.setattr(scheduler, "BOT_SOURCE", "reserve")
    monkeypatch.setattr(scheduler, "ENABLE_RESERVE_PANEL_SYNC", False)
    monkeypatch.setattr(scheduler, "reconcile_pending_platega_transactions", lambda _controller: _record("platega"))
    monkeypatch.setattr(scheduler, "reconcile_pending_paypear_transactions", lambda _controller: _record("paypear"))
    monkeypatch.setattr(scheduler, "reconcile_pending_yookassa_transactions", lambda _controller: _record("yookassa"))
    monkeypatch.setattr(scheduler, "retry_pending_confirmed_transactions", lambda _controller: _record("retry"))
    monkeypatch.setattr(
        scheduler,
        "sync_keys_with_panels",
        lambda: (_ for _ in ()).throw(AssertionError("reserve runtime must not run panel sync by default")),
    )

    controller = SimpleNamespace(get_status=lambda: {"shop_bot_running": False})

    asyncio.run(scheduler.periodic_subscription_check.__wrapped__(controller))

    assert calls == ["platega", "paypear", "yookassa", "retry"]


def test_sync_keys_with_panels_stops_remaining_hosts_when_pressure_rises_mid_cycle(monkeypatch):
    from shop_bot import runtime_guard
    from shop_bot.data_manager import scheduler
    from shop_bot.modules import xui_api

    low_snapshot = runtime_guard.MemorySnapshot(
        rss_bytes=50 * 1024 * 1024,
        cgroup_current_bytes=50 * 1024 * 1024,
        cgroup_limit_bytes=100 * 1024 * 1024,
    )
    high_snapshot = runtime_guard.MemorySnapshot(
        rss_bytes=85 * 1024 * 1024,
        cgroup_current_bytes=85 * 1024 * 1024,
        cgroup_limit_bytes=100 * 1024 * 1024,
    )
    snapshots = iter([low_snapshot, low_snapshot, high_snapshot, high_snapshot])
    processed_hosts: list[str] = []
    alerts: list[str] = []

    hosts = [
        {
            "host_name": "FIRST",
            "host_url": "https://first.example/xui/",
            "host_username": "u",
            "host_pass": "p",
            "host_inbound_id": 1,
            "is_maintenance": 0,
            "status": "healthy",
        },
        {
            "host_name": "SECOND",
            "host_url": "https://second.example/xui/",
            "host_username": "u",
            "host_pass": "p",
            "host_inbound_id": 2,
            "is_maintenance": 0,
            "status": "healthy",
        },
    ]
    fake_api = SimpleNamespace(inbound=SimpleNamespace())

    monkeypatch.setenv("RUNTIME_MEMORY_WARNING_PERCENT", "80")
    monkeypatch.setenv("RUNTIME_MEMORY_CRITICAL_PERCENT", "90")
    monkeypatch.setattr(scheduler.runtime_guard, "get_memory_snapshot", lambda: next(snapshots, high_snapshot))
    monkeypatch.setattr(
        scheduler.runtime_guard,
        "emit_memory_pressure_alert_if_needed",
        lambda **kwargs: alerts.append(kwargs["reason"]) or True,
    )
    monkeypatch.setattr(scheduler.database, "get_all_hosts", lambda: hosts)
    monkeypatch.setattr(scheduler.database, "get_keys_for_host", lambda host_name: processed_hosts.append(host_name) or [])
    monkeypatch.setattr(scheduler.database, "update_host_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scheduler,
        "_probe_host_health",
        lambda host: (
            SimpleNamespace(
                api=fake_api,
                inbound=SimpleNamespace(id=host["host_inbound_id"]),
                recovered_on_retry=False,
            ),
            "healthy",
            None,
        ),
    )
    monkeypatch.setattr(
        scheduler.xui_api,
        "scan_inbound_clients_minimal",
        lambda *_args, **_kwargs: xui_api.PanelClientScan({}, 0, []),
    )

    async def _no_blitz_sync():
        return 0

    monkeypatch.setattr(scheduler, "_sync_blitz_keys", _no_blitz_sync)

    asyncio.run(scheduler.sync_keys_with_panels())

    assert processed_hosts == ["FIRST"]
    assert alerts == ["scheduler_sync_memory_guard"]


def test_sync_keys_with_panels_skips_db_mutation_when_minimal_scan_fails(tmp_path, monkeypatch):
    from shop_bot.data_manager import scheduler

    host = {
        "host_name": "SUPER",
        "host_url": "https://super.example/xui/",
        "host_username": "u",
        "host_pass": "p",
        "host_inbound_id": 1,
        "is_maintenance": 0,
        "status": "healthy",
    }
    fake_api = SimpleNamespace(inbound=SimpleNamespace())
    fake_inbound = SimpleNamespace(id=1)
    local_expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=7)
    updated_keys: list[tuple[str, object]] = []
    alerts: list[tuple[object, str]] = []

    monkeypatch.setenv("RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler.database, "get_all_hosts", lambda: [host])
    monkeypatch.setattr(
        scheduler.database,
        "get_keys_for_host",
        lambda _host_name: [
            {
                "key_email": "local@example.com",
                "expiry_date": local_expiry.isoformat(),
            }
        ],
    )
    monkeypatch.setattr(scheduler.database, "update_host_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        scheduler.database,
        "update_key_status_from_server",
        lambda email, client: updated_keys.append((email, client)),
    )
    monkeypatch.setattr(
        scheduler,
        "_probe_host_health",
        lambda _host: (
            SimpleNamespace(
                api=fake_api,
                inbound=fake_inbound,
                recovered_on_retry=False,
            ),
            "healthy",
            None,
        ),
    )
    monkeypatch.setattr(
        scheduler.xui_api,
        "scan_inbound_clients_minimal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("panel_call_timeout")),
    )
    monkeypatch.setattr(
        scheduler,
        "send_critical_alert_nonblocking",
        lambda error, *, details="", respect_cooldown=True: alerts.append((error, details)),
    )

    async def _no_blitz_sync():
        return 0

    monkeypatch.setattr(scheduler, "_sync_blitz_keys", _no_blitz_sync)

    asyncio.run(scheduler.sync_keys_with_panels())

    assert updated_keys == []
    assert alerts
    assert "reason=panel_raw_lookup_failed" in alerts[0][1]


def test_host_sync_alert_cooldown_is_shared_by_host_and_reason(tmp_path, monkeypatch):
    from shop_bot.data_manager import scheduler

    monkeypatch.setenv("RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler, "HOST_SYNC_ALERT_COOLDOWN_SECONDS", 900)

    assert scheduler._host_sync_alert_allowed("Купить", "panel_raw_lookup_failed") is True
    assert scheduler._host_sync_alert_allowed("Купить", "panel_raw_lookup_failed") is False
    assert scheduler._host_sync_alert_allowed("Купить", "host_ssl_error") is True
