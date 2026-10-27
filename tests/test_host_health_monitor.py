from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


@pytest.fixture
def scheduler_module(monkeypatch, tmp_path):
    from shop_bot.data_manager import scheduler

    monkeypatch.setenv("RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler, "_last_shop_watchdog_alert_at", None)
    monkeypatch.setattr(scheduler, "_shop_watchdog_transient_unhealthy_cycles", 0)
    scheduler._host_unhealthy_since.clear()
    scheduler._host_unhealthy_alert_sent.clear()
    monkeypatch.setattr(
        scheduler.database,
        "get_setting",
        lambda key: "SUPER" if key == "super_key_host_name" else None,
    )
    return scheduler


def test_probe_host_health_does_not_load_full_clients_by_default(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    from shop_bot.modules.xui_api import HostLoginResult

    class _InboundAPI:
        def get_by_id(self, _inbound_id):
            raise AssertionError("health probe must not load full inbound clients by default")

    host = {
        "host_name": "SUPER",
        "host_url": "https://pepelbot.com:1666/rknblowme/",
        "host_username": "noname",
        "host_pass": "secret",
        "host_inbound_id": 1,
        "status": "healthy",
        "last_error": "",
        "is_maintenance": 0,
    }
    api = SimpleNamespace(inbound=_InboundAPI())
    inbound = SimpleNamespace(id=1, settings=SimpleNamespace(clients=[]))
    login_kwargs: dict[str, object] = {}

    monkeypatch.setattr(scheduler, "HOST_HEALTH_COLLECT_CLIENT_COUNT", False)

    def _fake_login_to_host(**kwargs):
        login_kwargs.update(kwargs)
        return HostLoginResult(api=api, inbound=inbound, attempts=1)

    monkeypatch.setattr(
        scheduler.xui_api,
        "login_to_host_detailed",
        _fake_login_to_host,
    )
    monkeypatch.setattr(scheduler.database, "get_host", lambda _host_name: host)
    monkeypatch.setattr(scheduler.database, "update_host_status", lambda *_args, **_kwargs: None)

    _result, current_status, clients_count = scheduler._probe_host_health(host)

    assert current_status == "healthy"
    assert clients_count is None
    assert login_kwargs["fallback_to_lightweight_inbound_lookup"] is True
    assert login_kwargs["fallback_to_full_inbound_lookup"] is False
    assert login_kwargs["allow_minimal_inbound_on_lookup_failure"] is True


def test_probe_host_health_sends_recovery_immediately(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    from shop_bot.errors import BotError
    from shop_bot.modules.xui_api import HostLoginResult

    host = {
        "host_name": "SUPER",
        "host_url": "https://pepelbot.com:1666/rknblowme/",
        "host_username": "noname",
        "host_pass": "secret",
        "host_inbound_id": 1,
        "status": "degraded",
        "last_error": "Panel refused the connection",
        "is_maintenance": 0,
    }
    updates: list[tuple[str, str, str | None]] = []
    alerts: list[dict] = []

    monkeypatch.setattr(
        scheduler.xui_api,
        "login_to_host_detailed",
        lambda **_kwargs: HostLoginResult(api=object(), inbound=object(), attempts=1),
    )
    monkeypatch.setattr(
        scheduler.database,
        "update_host_status",
        lambda host_name, status, error=None: updates.append((host_name, status, error)),
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_host",
        lambda _host_name: {
            "host_name": "SUPER",
            "status": "degraded",
            "raw_status": "degraded",
            "last_error": "Panel refused the connection",
        },
    )

    def _capture_alert(error, **kwargs):
        alerts.append({"error": error, **kwargs})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    from datetime import datetime

    scheduler._host_unhealthy_since["super"] = datetime.now()
    scheduler._host_unhealthy_alert_sent["super"] = True

    result, current_status, clients_count = scheduler._probe_host_health(host)

    assert result.api is not None
    assert result.inbound is not None
    assert current_status == "healthy"
    assert clients_count is None
    assert updates == [("SUPER", "healthy", None)]
    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.HOST_RECOVERED
    assert "reason=host_recovered" in str(alerts[0].get("details", ""))
    assert alerts[0].get("respect_cooldown") is False


def test_probe_host_health_sends_recovery_from_fresh_db_state(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    from shop_bot.errors import BotError
    from shop_bot.modules.xui_api import HostLoginResult

    # Simulate stale host snapshot: caller still sees healthy,
    # but DB already has an unhealthy status from another monitor loop.
    host = {
        "host_name": "SUPER",
        "host_url": "https://pepelbot.com:1666/rknblowme/",
        "host_username": "noname",
        "host_pass": "secret",
        "host_inbound_id": 1,
        "status": "healthy",
        "last_error": "",
        "is_maintenance": 0,
    }
    updates: list[tuple[str, str, str | None]] = []
    alerts: list[dict] = []

    monkeypatch.setattr(
        scheduler.xui_api,
        "login_to_host_detailed",
        lambda **_kwargs: HostLoginResult(api=object(), inbound=object(), attempts=1),
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_host",
        lambda _host_name: {
            "host_name": "SUPER",
            "status": "unavailable",
            "raw_status": "unavailable",
            "last_error": "timeout",
        },
    )
    monkeypatch.setattr(
        scheduler.database,
        "update_host_status",
        lambda host_name, status, error=None: updates.append((host_name, status, error)),
    )

    def _capture_alert(error, **kwargs):
        alerts.append({"error": error, **kwargs})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    from datetime import datetime

    scheduler._host_unhealthy_since["super"] = datetime.now()
    scheduler._host_unhealthy_alert_sent["super"] = True

    result, current_status, clients_count = scheduler._probe_host_health(host)

    assert result.api is not None
    assert result.inbound is not None
    assert current_status == "healthy"
    assert clients_count is None
    assert updates == [("SUPER", "healthy", None)]
    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.HOST_RECOVERED
    assert "reason=host_recovered" in str(alerts[0].get("details", ""))
    assert alerts[0].get("respect_cooldown") is False


def test_probe_host_health_marks_network_failure_unavailable(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    from shop_bot.modules.xui_api import HostLoginResult

    host = {
        "host_name": "SUPER",
        "host_url": "https://pepelbot.com:1666/rknblowme/",
        "host_username": "noname",
        "host_pass": "secret",
        "host_inbound_id": 1,
        "status": "healthy",
        "last_error": "",
        "is_maintenance": 0,
    }
    updates: list[tuple[str, str, str | None]] = []
    alerts: list[dict] = []

    monkeypatch.setattr(
        scheduler.xui_api,
        "login_to_host_detailed",
        lambda **_kwargs: HostLoginResult(
            api=None,
            inbound=None,
            error_code="host_connection_refused",
            error_message="Panel refused the connection: connection refused",
            attempts=3,
        ),
    )
    monkeypatch.setattr(
        scheduler.database,
        "update_host_status",
        lambda host_name, status, error=None: updates.append((host_name, status, error)),
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_host",
        lambda _host_name: {"host_name": "SUPER", "status": "healthy", "raw_status": "healthy", "last_error": ""},
    )

    def _capture_alert(error, **kwargs):
        alerts.append({"error": error, **kwargs})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    result, current_status, clients_count = scheduler._probe_host_health(host)

    assert result.api is None
    assert result.inbound is None
    assert current_status == "unavailable"
    assert clients_count is None
    assert updates == [("SUPER", "unavailable", "Panel refused the connection: connection refused")]
    assert alerts == []


def test_probe_host_health_marks_circuit_open_unavailable(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    from shop_bot.modules.xui_api import HostLoginResult

    host = {
        "host_name": "SUPER@",
        "host_url": "https://panel.example.test:2666/panel-path/",
        "host_username": "noname",
        "host_pass": "secret",
        "host_inbound_id": 1,
        "status": "healthy",
        "last_error": "",
        "is_maintenance": 0,
    }
    updates: list[tuple[str, str, str | None]] = []
    alerts: list[dict] = []

    monkeypatch.setattr(
        scheduler.xui_api,
        "login_to_host_detailed",
        lambda **_kwargs: HostLoginResult(
            api=None,
            inbound=None,
            error_code="host_circuit_open",
            error_message="Circuit breaker is open for host",
            attempts=0,
        ),
    )
    monkeypatch.setattr(
        scheduler.database,
        "update_host_status",
        lambda host_name, status, error=None: updates.append((host_name, status, error)),
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_setting",
        lambda key, default=None: "SUPER@" if key == "super_key_host_name" else default,
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_host",
        lambda _host_name: {"host_name": "SUPER@", "status": "healthy", "raw_status": "healthy", "last_error": ""},
    )

    def _capture_alert(error, **kwargs):
        alerts.append({"error": error, **kwargs})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    result, current_status, clients_count = scheduler._probe_host_health(host)

    assert result.api is None
    assert result.inbound is None
    assert current_status == "unavailable"
    assert clients_count is None
    assert updates == [("SUPER@", "unavailable", "Circuit breaker is open for host")]
    assert alerts == []


def test_probe_host_health_marks_subscription_unreachable_even_if_panel_login_ok(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    from shop_bot.modules.xui_api import HostLoginResult

    host = {
        "host_name": "SUPER",
        "host_url": "https://pepelbot.com:1666/rknblowme/",
        "host_subscription_base_url": "https://pepelbot.com:55259/podpiska/",
        "host_username": "noname",
        "host_pass": "secret",
        "host_inbound_id": 1,
        "status": "healthy",
        "last_error": "",
        "is_maintenance": 0,
    }
    updates: list[tuple[str, str, str | None]] = []
    alerts: list[dict] = []

    monkeypatch.setattr(
        scheduler.xui_api,
        "login_to_host_detailed",
        lambda **_kwargs: HostLoginResult(api=object(), inbound=object(), attempts=1),
    )
    monkeypatch.setattr(
        scheduler,
        "_probe_host_subscription_endpoint",
        lambda _host: (False, "host_subscription_unreachable", "Subscription endpoint is unreachable"),
    )
    monkeypatch.setattr(
        scheduler.database,
        "update_host_status",
        lambda host_name, status, error=None: updates.append((host_name, status, error)),
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_host",
        lambda _host_name: {"host_name": "SUPER", "status": "healthy", "raw_status": "healthy", "last_error": ""},
    )

    def _capture_alert(error, **kwargs):
        alerts.append({"error": error, **kwargs})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    result, current_status, clients_count = scheduler._probe_host_health(host)

    assert result.api is not None
    assert result.inbound is not None
    assert current_status == "unavailable"
    assert clients_count is None
    assert updates == [("SUPER", "unavailable", "Subscription endpoint is unreachable")]
    assert alerts == []


def test_periodic_host_health_check_uses_configured_delay_and_jitter(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    sleep_calls: list[float] = []
    jitter_calls: list[tuple[int, int]] = []

    def _fake_with_jitter(base_seconds: int, jitter_seconds: int) -> float:
        jitter_calls.append((base_seconds, jitter_seconds))
        return float(base_seconds) + 0.5

    async def _fake_sleep(seconds: float):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise StopAsyncIteration

    monkeypatch.setattr(scheduler, "_sleep_seconds_with_jitter", _fake_with_jitter)
    monkeypatch.setattr(scheduler.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(scheduler.database, "get_all_hosts", lambda: [])

    with pytest.raises(StopAsyncIteration):
        asyncio.run(scheduler.periodic_host_health_check())

    assert jitter_calls == [
        (scheduler.HOST_HEALTH_CHECK_INITIAL_DELAY_SECONDS, scheduler.HOST_HEALTH_CHECK_JITTER_SECONDS),
        (scheduler.HOST_HEALTH_CHECK_INTERVAL_SECONDS, scheduler.HOST_HEALTH_CHECK_JITTER_SECONDS),
    ]
    assert sleep_calls == [
        float(scheduler.HOST_HEALTH_CHECK_INITIAL_DELAY_SECONDS) + 0.5,
        float(scheduler.HOST_HEALTH_CHECK_INTERVAL_SECONDS) + 0.5,
    ]


def test_probe_host_health_primary_host_realerts_on_repeated_unavailable(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    from shop_bot.modules.xui_api import HostLoginResult

    repeated_error = "Panel refused the connection: connection refused"
    host = {
        "host_name": "SUPER@",
        "host_url": "https://panel.example.test:2666/panel-path/",
        "host_username": "noname",
        "host_pass": "secret",
        "host_inbound_id": 1,
        "status": "unavailable",
        "last_error": repeated_error,
        "is_maintenance": 0,
    }
    alerts: list[dict] = []

    monkeypatch.setattr(
        scheduler.xui_api,
        "login_to_host_detailed",
        lambda **_kwargs: HostLoginResult(
            api=None,
            inbound=None,
            error_code="host_connection_refused",
            error_message=repeated_error,
            attempts=3,
        ),
    )
    monkeypatch.setattr(
        scheduler.database,
        "update_host_status",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_setting",
        lambda key, default=None: "SUPER@" if key == "super_key_host_name" else default,
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_host",
        lambda _host_name: {
            "host_name": "SUPER@",
            "status": "unavailable",
            "raw_status": "unavailable",
            "last_error": repeated_error,
        },
    )

    def _capture_alert(error, **kwargs):
        alerts.append({"error": error, **kwargs})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    _, current_status, clients_count = scheduler._probe_host_health(host)

    assert current_status == "unavailable"
    assert clients_count is None
    assert alerts == []


def test_probe_host_health_alerts_only_after_debounce_window(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    from datetime import datetime, timedelta

    from shop_bot.errors import BotError
    from shop_bot.modules.xui_api import HostLoginResult

    host = {
        "host_name": "SUPER",
        "host_url": "https://pepelbot.com:1666/rknblowme/",
        "host_username": "noname",
        "host_pass": "secret",
        "host_inbound_id": 1,
        "status": "unavailable",
        "last_error": "Panel refused the connection",
        "is_maintenance": 0,
    }
    alerts: list[dict] = []

    monkeypatch.setattr(
        scheduler.xui_api,
        "login_to_host_detailed",
        lambda **_kwargs: HostLoginResult(
            api=None,
            inbound=None,
            error_code="host_connection_refused",
            error_message="Panel refused the connection: connection refused",
            attempts=3,
        ),
    )
    monkeypatch.setattr(
        scheduler.database,
        "update_host_status",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_host",
        lambda _host_name: {
            "host_name": "SUPER",
            "status": "unavailable",
            "raw_status": "unavailable",
            "last_error": "Panel refused the connection",
        },
    )

    def _capture_alert(error, **kwargs):
        alerts.append({"error": error, **kwargs})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)
    scheduler._host_unhealthy_since["super"] = datetime.now() - timedelta(seconds=61)
    scheduler._host_unhealthy_alert_sent["super"] = False

    _, current_status, clients_count = scheduler._probe_host_health(host)

    assert current_status == "unavailable"
    assert clients_count is None
    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.HOST_UNREACHABLE
    assert "reason=host_connection_refused" in alerts[0]["details"]


def test_probe_host_health_non_primary_host_suppresses_repeated_unavailable(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    from shop_bot.modules.xui_api import HostLoginResult

    repeated_error = "Panel refused the connection: connection refused"
    host = {
        "host_name": "SUPER",
        "host_url": "https://pepelbot.com:1666/rknblowme/",
        "host_username": "noname",
        "host_pass": "secret",
        "host_inbound_id": 1,
        "status": "unavailable",
        "last_error": repeated_error,
        "is_maintenance": 0,
    }
    alerts: list[dict] = []

    monkeypatch.setattr(
        scheduler.xui_api,
        "login_to_host_detailed",
        lambda **_kwargs: HostLoginResult(
            api=None,
            inbound=None,
            error_code="host_connection_refused",
            error_message=repeated_error,
            attempts=3,
        ),
    )
    monkeypatch.setattr(
        scheduler.database,
        "update_host_status",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_setting",
        lambda key, default=None: "SUPER@" if key == "super_key_host_name" else default,
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_host",
        lambda _host_name: {
            "host_name": "SUPER",
            "status": "unavailable",
            "raw_status": "unavailable",
            "last_error": repeated_error,
        },
    )

    def _capture_alert(_error, **_kwargs):
        alerts.append({})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    _, current_status, clients_count = scheduler._probe_host_health(host)

    assert current_status == "unavailable"
    assert clients_count is None
    assert alerts == []


def test_host_health_alert_cooldown_is_shared_by_panel_url(tmp_path, monkeypatch, scheduler_module):
    scheduler = scheduler_module
    from datetime import UTC, datetime, timedelta

    from shop_bot.errors import BotError
    from shop_bot.modules.xui_api import HostLoginResult

    panel_url = "https://192.0.2.14:1312/3xuivless/"
    first_host = {
        "host_name": "pepelmain",
        "host_url": panel_url,
        "host_username": "noname",
        "host_pass": "secret",
        "host_inbound_id": 1,
        "status": "unavailable",
        "last_error": "Circuit breaker is open",
        "is_maintenance": 0,
    }
    alias_host = {
        **first_host,
        "host_name": "Купить",
    }
    host_statuses = {
        "pepelmain": {
            "host_name": "pepelmain",
            "status": "unavailable",
            "raw_status": "unavailable",
            "last_error": "Circuit breaker is open",
        },
        "Купить": {
            "host_name": "Купить",
            "status": "unavailable",
            "raw_status": "unavailable",
            "last_error": "Circuit breaker is open",
        },
    }
    alerts: list[dict] = []

    monkeypatch.setenv("RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(scheduler, "HOST_HEALTH_ALERT_COOLDOWN_SECONDS", 18_000)
    monkeypatch.setattr(
        scheduler.xui_api,
        "login_to_host_detailed",
        lambda **_kwargs: HostLoginResult(
            api=None,
            inbound=None,
            error_code="host_circuit_open",
            error_message="Circuit breaker is open",
            attempts=0,
        ),
    )
    monkeypatch.setattr(scheduler.database, "get_host", lambda host_name: host_statuses[host_name])
    monkeypatch.setattr(scheduler.database, "update_host_status", lambda *_args, **_kwargs: None)

    def _capture_alert(error, **kwargs):
        alerts.append({"error": error, **kwargs})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)
    now = datetime.now(UTC).replace(tzinfo=None)
    scheduler._host_unhealthy_since["pepelmain"] = now - timedelta(seconds=120)
    scheduler._host_unhealthy_since["купить"] = now - timedelta(seconds=120)

    scheduler._probe_host_health(first_host)
    scheduler._probe_host_health(alias_host)

    assert len(alerts) == 1
    assert alerts[0]["error"] == BotError.HOST_UNREACHABLE
    assert "host=pepelmain" in alerts[0]["details"]
    assert scheduler._host_unhealthy_alert_sent["pepelmain"] is True
    assert scheduler._host_unhealthy_alert_sent.get("купить") is not True
