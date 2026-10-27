"""Unit tests for shop_bot.domain.issuance — key issuance helpers."""
from shop_bot.domain.issuance import (
    extract_panel_snapshot,
    generate_fallback_email,
    generate_new_email,
    mark_issuance_failure,
    pick_issuance_action,
    resolve_paid_at,
)
from shop_bot.domain.protocols import IssuanceAction


class TestMarkIssuanceFailure:
    def test_sets_failure_fields(self):
        meta = {"payment_method": "YK"}
        result = mark_issuance_failure(
            payment_id="p1", metadata=meta, reason="timeout",
        )
        assert result is meta
        assert result["issuance_failed"] is True
        assert result["issuance_failure_reason"] == "timeout"
        assert result["issuance_failure_count"] == 1
        assert "issuance_failure_first_at" in result

    def test_increments_count(self):
        meta = {"issuance_failure_count": 2}
        mark_issuance_failure(payment_id="p1", metadata=meta, reason="retry")
        assert meta["issuance_failure_count"] == 3

    def test_fatal_flag(self):
        meta = {}
        mark_issuance_failure(payment_id="p1", metadata=meta, reason="host_not_found", fatal=True)
        assert meta.get("issuance_fatal") is True

    def test_none_metadata_creates_dict(self):
        result = mark_issuance_failure(payment_id="p1", metadata=None, reason="test")
        assert isinstance(result, dict)
        assert result["issuance_failure_count"] == 1

    def test_failure_logs_attached(self):
        meta = {}
        mark_issuance_failure(
            payment_id="p1", metadata=meta, reason="err",
            failure_logs={"stage": "test", "host": "H1"},
        )
        assert meta["issuance_failure_logs"] == {"stage": "test", "host": "H1"}

    def test_clears_exhausted_flags(self):
        meta = {
            "autoretry_exhausted_alert_sent": True,
            "autoretry_exhausted_alert_at": "2026-01-01",
        }
        mark_issuance_failure(payment_id="p1", metadata=meta, reason="r")
        assert "autoretry_exhausted_alert_sent" not in meta
        assert "autoretry_exhausted_alert_at" not in meta


class TestExtractPanelSnapshot:
    def test_complete_snapshot(self):
        meta = {
            "issued_panel_host": "SUPER",
            "issued_panel_email": "u@vpn",
            "issued_panel_uuid": "abc-123",
        }
        result = extract_panel_snapshot(meta)
        assert result == {
            "host_name": "SUPER",
            "email": "u@vpn",
            "client_uuid": "abc-123",
        }

    def test_fallback_keys(self):
        meta = {
            "panel_host_name": "SUPER",
            "panel_email": "u@vpn",
            "panel_client_uuid": "abc-123",
        }
        result = extract_panel_snapshot(meta)
        assert result is not None
        assert result["host_name"] == "SUPER"

    def test_incomplete_returns_none(self):
        assert extract_panel_snapshot({"issued_panel_host": "H"}) is None

    def test_none_input(self):
        assert extract_panel_snapshot(None) is None


class TestResolvePaidAt:
    def test_with_iso_string(self):
        dt = resolve_paid_at({"paid_at": "2026-03-15T12:00:00"})
        assert dt.year == 2026
        assert dt.month == 3

    def test_fallback_to_now(self):
        from datetime import datetime

        dt = resolve_paid_at({})
        assert (datetime.now() - dt).total_seconds() < 2


class TestPickIssuanceAction:
    def test_new(self):
        assert pick_issuance_action("new") == IssuanceAction.NEW

    def test_extend(self):
        assert pick_issuance_action("extend") == IssuanceAction.EXTEND

    def test_unknown_defaults_to_new(self):
        assert pick_issuance_action("something") == IssuanceAction.NEW

    def test_none_defaults_to_new(self):
        assert pick_issuance_action(None) == IssuanceAction.NEW


class TestEmailGeneration:
    def test_new_email(self):
        assert generate_new_email(42, 3) == "user42-key3@vpn.bot"

    def test_fallback_email(self):
        assert generate_fallback_email(42, 3, 2) == "user42-key5@vpn.bot"


class TestMarkIssuanceFailureFatal:
    """Verify that fatal failures are not retried (TASK-0 spec test)."""

    def test_fatal_sets_issuance_fatal_flag(self):
        meta = {}
        mark_issuance_failure(payment_id="p1", metadata=meta, reason="host_not_found", fatal=True)
        assert meta["issuance_fatal"] is True
        assert meta["issuance_failure_reason"] == "host_not_found"

    def test_non_fatal_does_not_set_fatal_flag(self):
        meta = {}
        mark_issuance_failure(payment_id="p1", metadata=meta, reason="timeout", fatal=False)
        assert "issuance_fatal" not in meta

    def test_failure_logs_truncated_at_500_chars(self):
        meta = {}
        mark_issuance_failure(
            payment_id="p1", metadata=meta, reason="err",
            failure_logs={"long_value": "x" * 1000},
        )
        assert len(meta["issuance_failure_logs"]["long_value"]) == 500


class TestResolvePaidAtEdgeCases:
    def test_invalid_string_falls_back_to_now(self):
        from datetime import datetime
        dt = resolve_paid_at({"paid_at": "not-a-date"})
        assert (datetime.now() - dt).total_seconds() < 2

    def test_datetime_object_returned_as_is(self):
        from datetime import datetime
        now = datetime(2026, 6, 15, 12, 0)
        dt = resolve_paid_at({"paid_at": now})
        assert dt == now
