"""Unit tests for shop_bot.domain.alerts — dedupe, debounce, dispatch facade."""
import time

from shop_bot.domain.alerts import (
    AlertDebouncer,
    AlertDeduplicator,
    build_dedupe_key,
    format_alert_fields,
)


class TestAlertDeduplicator:
    def test_first_occurrence_is_not_duplicate(self):
        d = AlertDeduplicator(window_seconds=60)
        assert d.is_duplicate("key1") is False

    def test_recorded_within_window_is_duplicate(self):
        d = AlertDeduplicator(window_seconds=60)
        d.record("key1")
        assert d.is_duplicate("key1") is True

    def test_different_key_is_not_duplicate(self):
        d = AlertDeduplicator(window_seconds=60)
        d.record("key1")
        assert d.is_duplicate("key2") is False

    def test_expired_entry_is_not_duplicate(self):
        d = AlertDeduplicator(window_seconds=1)
        d.record("key1")
        time.sleep(1.1)
        assert d.is_duplicate("key1") is False

    def test_max_entries_evicts_oldest(self):
        d = AlertDeduplicator(window_seconds=300, max_entries=10)
        for i in range(15):
            d.record(f"k{i}")
        assert len(d._store) == 10
        assert "k0" not in d._store
        assert "k14" in d._store

    def test_clear(self):
        d = AlertDeduplicator()
        d.record("a")
        d.clear()
        assert d.is_duplicate("a") is False


class TestBuildDedupeKey:
    def test_basic(self):
        key = build_dedupe_key("HOST_UNREACHABLE", "host=SUPER timeout=30")
        assert key == "HOST_UNREACHABLE|host=SUPER timeout=30"

    def test_normalizes_whitespace(self):
        key = build_dedupe_key("ERR", "  a   b  c  ")
        assert key == "ERR|a b c"

    def test_truncated_at_max_len(self):
        key = build_dedupe_key("X", "a" * 600, max_len=20)
        assert len(key) == 20


# ── AlertDebouncer ───────────────────────────────────────────────────

class TestAlertDebouncer:
    def test_suppresses_short_flap(self):
        d = AlertDebouncer(default_debounce=60)
        assert d.should_alert_down("host1") is False

    def test_alerts_after_threshold(self):
        d = AlertDebouncer(default_debounce=0)
        assert d.should_alert_down("host1") is True

    def test_primary_host_immediate_alert(self):
        d = AlertDebouncer(default_debounce=60, primary_debounce=0, primary_hosts={"SUPER"})
        assert d.should_alert_down("SUPER") is True

    def test_recovery_only_after_down_alert(self):
        d = AlertDebouncer(default_debounce=0)
        d.should_alert_down("h1")
        assert d.should_alert_recovery("h1") is True

    def test_no_recovery_on_flap(self):
        d = AlertDebouncer(default_debounce=60)
        d.should_alert_down("h1")
        assert d.should_alert_recovery("h1") is False

    def test_retry_result_silent_on_zero_activity(self):
        assert AlertDebouncer.should_alert_retry_result(errors=0, recovered=0) is False

    def test_retry_result_alerts_on_errors(self):
        assert AlertDebouncer.should_alert_retry_result(errors=1, recovered=0) is True

    def test_retry_result_alerts_on_recovered(self):
        assert AlertDebouncer.should_alert_retry_result(errors=0, recovered=1) is True

    def test_down_alert_not_sent_twice(self):
        d = AlertDebouncer(default_debounce=0)
        assert d.should_alert_down("h1") is True
        assert d.should_alert_down("h1") is False

    def test_case_insensitive_host(self):
        d = AlertDebouncer(default_debounce=0, primary_hosts={"SUPER"})
        assert d.should_alert_down("super") is True

    def test_clear(self):
        d = AlertDebouncer(default_debounce=0)
        d.should_alert_down("h1")
        d.clear()
        assert d.should_alert_down("h1") is True


class TestAlertDebouncerTimeBased:
    """Verify debounce window actually works with time progression."""

    def test_alerts_after_debounce_window_passes(self):
        d = AlertDebouncer(default_debounce=1)
        assert d.should_alert_down("h1") is False
        time.sleep(1.1)
        assert d.should_alert_down("h1") is True

    def test_multiple_hosts_independent_state(self):
        d = AlertDebouncer(default_debounce=0)
        assert d.should_alert_down("h1") is True
        assert d.should_alert_down("h2") is True
        assert d.should_alert_recovery("h1") is True
        assert d.should_alert_recovery("h2") is True

    def test_recovery_resets_for_next_down(self):
        d = AlertDebouncer(default_debounce=0)
        d.should_alert_down("h1")
        d.should_alert_recovery("h1")
        assert d.should_alert_down("h1") is True


class TestRetryResultEdgeCases:
    def test_both_errors_and_recovered(self):
        assert AlertDebouncer.should_alert_retry_result(errors=3, recovered=2) is True

    def test_negative_values_treated_as_zero(self):
        assert AlertDebouncer.should_alert_retry_result(errors=-1, recovered=-1) is False


class TestFormatAlertFields:
    def test_formats_known_fields(self):
        result = format_alert_fields({
            "payment_id": "pay-1",
            "user_id": 42,
            "host_name": "SUPER",
            "unknown_field": "ignored",
        })
        assert "payment_id = pay-1" in result
        assert "user_id = 42" in result
        assert "host_name = SUPER" in result
        assert "unknown_field" not in result

    def test_skips_none_and_empty(self):
        result = format_alert_fields({"payment_id": None, "user_id": ""})
        assert result == ""

    def test_key_id_included(self):
        result = format_alert_fields({"key_id": 999, "payment_id": "p1"})
        assert "key_id = 999" in result
        assert "payment_id = p1" in result

    def test_all_empty_returns_empty_string(self):
        result = format_alert_fields({})
        assert result == ""
