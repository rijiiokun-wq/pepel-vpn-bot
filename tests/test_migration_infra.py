"""Tests for migration infrastructure: deprecation tracker, metrics, runtime status."""

import pytest

from shop_bot.domain.deprecation import (
    get_legacy_call_counts,
    legacy_removal_safe,
    reset_legacy_counts,
    total_legacy_calls,
    track_legacy_call,
)
from shop_bot.domain.metrics import get, inc, reset, runtime_status, snapshot


class TestDeprecationTracker:
    @pytest.fixture(autouse=True)
    def _reset(self):
        reset_legacy_counts()
        yield
        reset_legacy_counts()

    def test_track_increments(self):
        track_legacy_call("handlers.process_payment")
        track_legacy_call("handlers.process_payment")
        track_legacy_call("scheduler.retry_loop")
        counts = get_legacy_call_counts()
        assert counts["handlers.process_payment"] == 2
        assert counts["scheduler.retry_loop"] == 1

    def test_total(self):
        track_legacy_call("a")
        track_legacy_call("b")
        assert total_legacy_calls() == 2

    def test_removal_safe_initially(self):
        assert legacy_removal_safe()

    def test_removal_not_safe_after_call(self):
        track_legacy_call("x")
        assert not legacy_removal_safe()

    def test_reset_clears(self):
        track_legacy_call("y")
        reset_legacy_counts()
        assert legacy_removal_safe()


class TestMetrics:
    @pytest.fixture(autouse=True)
    def _reset(self):
        reset()
        yield
        reset()

    def test_inc_and_get(self):
        inc("issuance_requests_total", source="main", mode="legacy")
        inc("issuance_requests_total", source="main", mode="legacy")
        assert get("issuance_requests_total", source="main", mode="legacy") == 2

    def test_get_default_zero(self):
        assert get("nonexistent") == 0

    def test_snapshot(self):
        inc("a")
        inc("b")
        s = snapshot()
        assert s["a"] == 1
        assert s["b"] == 1

    def test_labels_isolation(self):
        inc("counter", label="x")
        inc("counter", label="y")
        assert get("counter", label="x") == 1
        assert get("counter", label="y") == 1


class TestRuntimeStatus:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        reset()
        reset_legacy_counts()
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", "legacy")
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "0")
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")

    def test_runtime_status_structure(self):
        status = runtime_status()
        assert "mode" in status
        assert "canary_percent" in status
        assert "kill_switch" in status
        assert "config_warnings" in status
        assert "shadow" in status
        assert "legacy_calls" in status
        assert "counters" in status

    def test_runtime_status_reflects_env(self, monkeypatch):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", "canary")
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "25")
        status = runtime_status()
        assert status["mode"] == "canary"
        assert status["canary_percent"] == 25
        assert status["config_warnings"] == []

    @pytest.mark.parametrize(
        ("raw_value", "expected", "reason"),
        [("not-a-number", 0, "invalid_integer"), ("-25", 0, "below_min"), ("250", 100, "above_max")],
    )
    def test_runtime_status_normalizes_canary_percent(self, monkeypatch, raw_value, expected, reason):
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", raw_value)
        status = runtime_status()
        assert status["canary_percent"] == expected
        assert status["config_warnings"] == [
            {
                "field": "ISSUANCE_CANARY_PERCENT",
                "reason": reason,
                "normalized_to": expected,
            }
        ]
        assert raw_value not in str(status["config_warnings"])

    def test_kill_switch_in_status(self, monkeypatch):
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "1")
        status = runtime_status()
        assert status["kill_switch"] is True
