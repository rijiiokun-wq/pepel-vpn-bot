"""Unit tests for shop_bot.domain.host_health — circuit breaker & health."""
from shop_bot.domain.host_health import (
    CircuitBreaker,
    check_host_for_autoretry,
    host_status_from_raw,
    is_unhealthy,
)
from shop_bot.domain.protocols import CircuitState, HostStatus


class TestHostStatusFromRaw:
    def test_healthy(self):
        assert host_status_from_raw("online") == HostStatus.HEALTHY
        assert host_status_from_raw("") == HostStatus.HEALTHY

    def test_degraded(self):
        assert host_status_from_raw("degraded") == HostStatus.DEGRADED
        assert host_status_from_raw("maintenance") == HostStatus.DEGRADED

    def test_unavailable(self):
        assert host_status_from_raw("unavailable") == HostStatus.UNAVAILABLE
        assert host_status_from_raw("offline") == HostStatus.UNAVAILABLE


class TestIsUnhealthy:
    def test_unhealthy_statuses(self):
        for s in ("degraded", "unavailable", "maintenance"):
            assert is_unhealthy(s) is True

    def test_healthy_statuses(self):
        for s in ("online", "healthy", "", None):
            assert is_unhealthy(s) is False


class TestCheckHostForAutoretry:
    def test_empty_name_not_unhealthy(self):
        unhealthy, _status = check_host_for_autoretry("")
        assert unhealthy is False

    def test_missing_host_returns_missing(self):
        unhealthy, status = check_host_for_autoretry(
            "no_such", get_host_fn=lambda _: None,
        )
        assert unhealthy is False
        assert status == "missing"

    def test_degraded_host_is_unhealthy(self):
        unhealthy, status = check_host_for_autoretry(
            "h1",
            get_host_fn=lambda _: {"raw_status": "degraded"},
        )
        assert unhealthy is True
        assert status == "degraded"

    def test_online_host_is_healthy(self):
        unhealthy, _status = check_host_for_autoretry(
            "h1",
            get_host_fn=lambda _: {"raw_status": "online"},
        )
        assert unhealthy is False


class TestCircuitBreaker:
    def test_closed_by_default(self):
        cb = CircuitBreaker(failure_threshold=3)
        state, remaining = cb.precheck("host1")
        assert state == CircuitState.CLOSED
        assert remaining == 0

    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
        cb.on_failure("h1")
        opened = cb.on_failure("h1")
        assert opened is True
        state, remaining = cb.precheck("h1")
        assert state == CircuitState.OPEN
        assert remaining > 0

    def test_success_resets(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.on_failure("h1")
        cb.on_failure("h1")
        cb.on_success("h1")
        state, _ = cb.precheck("h1")
        assert state == CircuitState.CLOSED

    def test_reset_clears_all(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.on_failure("h1")
        cb.reset()
        state, _ = cb.precheck("h1")
        assert state == CircuitState.CLOSED

    def test_half_open_after_cooldown(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=1)
        cb.on_failure("h1")
        state, remaining = cb.precheck("h1")
        assert state == CircuitState.OPEN
        assert remaining >= 1
        import time
        time.sleep(1.1)
        state, remaining = cb.precheck("h1")
        assert state == CircuitState.HALF_OPEN
        assert remaining == 0

    def test_multiple_hosts_independent(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.on_failure("h1")
        cb.on_failure("h1")
        cb.on_failure("h2")
        s1, _ = cb.precheck("h1")
        s2, _ = cb.precheck("h2")
        assert s1 == CircuitState.OPEN
        assert s2 == CircuitState.CLOSED

    def test_on_success_clears_only_target(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.on_failure("h1")
        cb.on_failure("h2")
        cb.on_success("h1")
        s1, _ = cb.precheck("h1")
        s2, _ = cb.precheck("h2")
        assert s1 == CircuitState.CLOSED
        assert s2 == CircuitState.OPEN


class TestHostStatusFromRawEdgeCases:
    def test_error_status(self):
        assert host_status_from_raw("error") == HostStatus.UNAVAILABLE

    def test_whitespace_trimmed(self):
        assert host_status_from_raw("  degraded  ") == HostStatus.DEGRADED

    def test_case_insensitive(self):
        assert host_status_from_raw("OFFLINE") == HostStatus.UNAVAILABLE
        assert host_status_from_raw("Degraded") == HostStatus.DEGRADED

    def test_none_is_healthy(self):
        assert host_status_from_raw(None) == HostStatus.HEALTHY


class TestCheckHostForAutoretryFallback:
    def test_host_with_status_field_fallback(self):
        unhealthy, status = check_host_for_autoretry(
            "h1",
            get_host_fn=lambda _: {"status": "degraded"},
        )
        assert unhealthy is True
        assert status == "degraded"

    def test_host_with_empty_raw_status(self):
        unhealthy, _ = check_host_for_autoretry(
            "h1",
            get_host_fn=lambda _: {"raw_status": ""},
        )
        assert unhealthy is False
