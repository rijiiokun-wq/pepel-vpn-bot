from __future__ import annotations

import asyncio
from datetime import UTC, datetime


def test_indestructible_loop_survives_alert_dispatch_failure(monkeypatch):
    from shop_bot.data_manager import scheduler

    with scheduler._loop_health_lock:
        scheduler._loop_health.clear()

    cycles = 0
    sleep_delays: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        sleep_delays.append(delay)

    def _raise_alert_dispatch(*_args, **_kwargs) -> None:
        raise RuntimeError("alert dispatch unavailable")

    monkeypatch.setattr(scheduler.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _raise_alert_dispatch)

    async def _cycle() -> None:
        nonlocal cycles
        cycles += 1
        if cycles == 1:
            raise RuntimeError("cycle failed")
        if cycles == 2:
            return
        raise asyncio.CancelledError

    isolated_loop = scheduler.indestructible_loop(
        name="test_loop_isolation",
        interval_fn=lambda: 0.25,
    )(_cycle)

    asyncio.run(isolated_loop())

    health = scheduler.get_loop_health()["test_loop_isolation"]
    assert cycles == 3
    assert health["consecutive_crashes"] == 0
    assert health["total_cycles"] == 1
    assert health["total_crashes"] == 1
    assert isinstance(health["last_success"], str)
    assert sleep_delays == [2, 0.25]


def test_loop_health_serializes_datetime_snapshot(monkeypatch):
    from shop_bot.data_manager import scheduler

    loop_name = "test_loop_snapshot"
    with scheduler._loop_health_lock:
        monkeypatch.setitem(
            scheduler._loop_health,
            loop_name,
            {
                "last_success": datetime(2026, 5, 22, 10, 30, 0, tzinfo=UTC),
                "consecutive_crashes": 0,
                "total_cycles": 3,
                "total_crashes": 1,
            },
        )

    health = scheduler.get_loop_health()[loop_name]

    assert health["last_success"] == "2026-05-22T10:30:00+00:00"
