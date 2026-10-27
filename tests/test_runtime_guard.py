from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from shop_bot import runtime_guard
from shop_bot.errors import BotError


def test_runtime_marker_distinguishes_graceful_and_unclean_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_STATE_DIR", str(tmp_path))

    previous = runtime_guard.mark_runtime_started("main")
    assert previous is None

    runtime_guard.mark_runtime_graceful_shutdown("main", signal_name="SIGTERM")
    previous = runtime_guard.mark_runtime_started("main")

    assert previous is not None
    assert runtime_guard.previous_exit_was_unclean(previous) is False
    assert previous["graceful_shutdown"] is True
    assert previous["shutdown_signal"] == "SIGTERM"

    previous = runtime_guard.mark_runtime_started("main")

    assert previous is not None
    assert runtime_guard.previous_exit_was_unclean(previous) is True


def test_runtime_shutdown_request_suppresses_deploy_restart_alert(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("RUNTIME_SHUTDOWN_REQUEST_GRACE_SECONDS", "300")

    runtime_guard.mark_runtime_started("main")
    runtime_guard.mark_runtime_shutdown_requested("main", signal_name="SIGTERM")
    previous = runtime_guard.mark_runtime_started("main")

    assert previous is not None
    assert previous["shutdown_requested"] is True
    assert previous["shutdown_signal"] == "SIGTERM"
    assert runtime_guard.previous_exit_was_unclean(previous) is False


def test_runtime_shutdown_request_expires_after_grace(monkeypatch):
    monkeypatch.setenv("RUNTIME_SHUTDOWN_REQUEST_GRACE_SECONDS", "0")
    previous = {
        "source": "main",
        "graceful_shutdown": False,
        "shutdown_requested": True,
        "shutdown_signal": "DEPLOY",
        "shutdown_requested_at": "1970-01-01T00:00:00+00:00",
    }

    assert runtime_guard.previous_exit_was_unclean(previous) is True


def test_unclean_exit_alert_is_sent_once_per_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("RUNTIME_UNCLEAN_EXIT_ALERT_COOLDOWN_SECONDS", "3600")
    captured: list[dict[str, object]] = []

    def _capture(error, *, details: str = "", respect_cooldown: bool = True):
        captured.append(
            {
                "error": error,
                "details": details,
                "respect_cooldown": respect_cooldown,
            }
        )
        return True

    monkeypatch.setattr(runtime_guard, "send_critical_alert_nonblocking", _capture)
    previous = {
        "source": "main",
        "pid": 123,
        "server": "prod-node-1",
        "started_at": "2026-05-29T18:00:00+00:00",
        "last_heartbeat_at": "2026-05-29T18:01:00+00:00",
        "graceful_shutdown": False,
        "last_memory": {
            "used_bytes": 90 * 1024 * 1024,
            "limit_bytes": 100 * 1024 * 1024,
            "usage_percent": 90.0,
        },
    }

    assert runtime_guard.alert_on_unclean_previous_exit(previous, source="main") is True
    assert runtime_guard.alert_on_unclean_previous_exit(previous, source="main") is False

    assert len(captured) == 1
    assert captured[0]["error"] is BotError.BOT_PROCESS_UNCLEAN_EXIT
    assert captured[0]["respect_cooldown"] is False
    assert "reason=suspected_oom_kill" in str(captured[0]["details"])
    assert "Last heartbeat: 2026-05-29T18:01:00+00:00" in str(captured[0]["details"])


def test_memory_pressure_alert_uses_cgroup_percent_and_cooldown(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("ENABLE_RUNTIME_GUARD", "1")
    monkeypatch.setenv("RUNTIME_MEMORY_WARNING_PERCENT", "80")
    monkeypatch.setenv("RUNTIME_MEMORY_CRITICAL_PERCENT", "90")
    monkeypatch.setenv("RUNTIME_MEMORY_ALERT_COOLDOWN_SECONDS", "3600")
    monkeypatch.setenv("BOT_SOURCE", "reserve")
    captured: list[dict[str, object]] = []

    def _capture(error, *, details: str = "", respect_cooldown: bool = True):
        captured.append(
            {
                "error": error,
                "details": details,
                "respect_cooldown": respect_cooldown,
            }
        )
        return True

    monkeypatch.setattr(runtime_guard, "send_critical_alert_nonblocking", _capture)
    snapshot = runtime_guard.MemorySnapshot(
        rss_bytes=85 * 1024 * 1024,
        cgroup_current_bytes=85 * 1024 * 1024,
        cgroup_limit_bytes=100 * 1024 * 1024,
    )

    assert runtime_guard.memory_pressure_level(snapshot) == "warning"
    assert runtime_guard.emit_memory_pressure_alert_if_needed(reason="runtime_guard", snapshot=snapshot) is True
    assert runtime_guard.emit_memory_pressure_alert_if_needed(reason="runtime_guard", snapshot=snapshot) is False

    assert len(captured) == 1
    assert captured[0]["error"] is BotError.BOT_MEMORY_PRESSURE_WARNING
    assert "source=reserve" in str(captured[0]["details"])
    assert "memory_usage_percent=85.0%" in str(captured[0]["details"])


def test_runtime_state_dir_explicit_env_wins(tmp_path, monkeypatch):
    marker_dir = tmp_path / "runtime"
    monkeypatch.setenv("RUNTIME_STATE_DIR", str(marker_dir))

    assert runtime_guard.runtime_marker_path("reserve") == marker_dir / "reserve.json"
