from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

import shop_bot.__main__ as main_module
from shop_bot.errors import BotError


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        try:
            current = asyncio.get_event_loop()
        except RuntimeError:
            current = None
        if current is not None and not current.is_closed():
            current.close()
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_build_process_shutdown_alert_details_includes_runtime_context(monkeypatch):
    monkeypatch.setattr(main_module.os, "getpid", lambda: 12345)
    monkeypatch.setattr(main_module.socket, "gethostname", lambda: "prod-node-1")

    details = main_module._build_process_shutdown_alert_details(
        signal.Signals.SIGTERM,
        {
            "shop_bot_running": True,
            "shop_bot_desired_running": True,
            "support_bot_running": False,
        },
    )

    assert "source=" in details
    assert "reason=process_signal_shutdown" in details
    assert "signal=SIGTERM" in details
    assert "pid=12345" in details
    assert "server=prod-node-1" in details
    assert "shop_running=1" in details
    assert "support_running=0" in details
    assert "desired_running=1" in details
    assert "journalctl/docker logs" in details


def test_reserve_runtime_host_health_monitor_disabled_by_default(monkeypatch):
    monkeypatch.setattr(main_module, "BOT_SOURCE", "reserve")
    monkeypatch.delenv("ENABLE_HOST_HEALTH_MONITOR", raising=False)
    monkeypatch.delenv("ENABLE_RESERVE_HOST_HEALTH_MONITOR", raising=False)

    assert main_module._should_start_host_health_monitor() is False


def test_reserve_runtime_host_health_monitor_can_be_enabled(monkeypatch):
    monkeypatch.setattr(main_module, "BOT_SOURCE", "reserve")
    monkeypatch.setenv("ENABLE_RESERVE_HOST_HEALTH_MONITOR", "1")

    assert main_module._should_start_host_health_monitor() is True


def test_send_process_shutdown_alert_dispatches(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_send_critical_alert(error, *, details: str = "", respect_cooldown: bool = True):
        captured["error"] = error
        captured["details"] = details
        captured["respect_cooldown"] = respect_cooldown
        return True

    monkeypatch.setattr(main_module, "send_critical_alert", _fake_send_critical_alert)

    result = _run_async(
        main_module._send_process_shutdown_alert(
            signal.Signals.SIGTERM,
            {"shop_bot_running": True},
            logging.getLogger("test_process_shutdown_alert"),
        )
    )

    assert result is True
    assert captured["error"] is BotError.BOT_PROCESS_STOPPING
    assert "reason=process_signal_shutdown" in str(captured["details"])
    assert captured["respect_cooldown"] is False


def test_send_process_shutdown_alert_skips_when_disabled(monkeypatch):
    called = False

    def _fake_send_critical_alert(*_args, **_kwargs):
        nonlocal called
        called = True
        return True

    monkeypatch.setenv("ENABLE_PROCESS_SHUTDOWN_ALERT", "0")
    monkeypatch.setattr(main_module, "send_critical_alert", _fake_send_critical_alert)

    result = _run_async(
        main_module._send_process_shutdown_alert(
            signal.Signals.SIGTERM,
            {"shop_bot_running": True},
            logging.getLogger("test_process_shutdown_alert"),
        )
    )

    assert result is False
    assert called is False
