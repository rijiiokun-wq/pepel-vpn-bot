"""Unit tests for shop_bot.domain.payments — payment metadata validation."""
import pytest

from shop_bot.domain.payments import (
    _coerce_bool,
    check_payment_confirmed,
    is_fatal_precheck_reason,
    parse_payment_metadata,
)
from shop_bot.domain.protocols import IssuanceAction, PaymentMeta


class TestParsePaymentMetadata:
    def _base_meta(self, **overrides):
        meta = {
            "external_payment_id": "pay-123",
            "user_id": "42",
            "months": "1",
            "price": "490.0",
            "action": "new",
            "key_id": "7",
            "host_name": "HOST1",
            "plan_id": "2",
            "plan_name": "Basic",
            "customer_email": "user@test.com",
            "payment_method": "YooKassa",
        }
        meta.update(overrides)
        return meta

    def test_valid_metadata_returns_payment_meta(self):
        result = parse_payment_metadata(self._base_meta())
        assert isinstance(result, PaymentMeta)
        assert result.payment_id == "pay-123"
        assert result.user_id == 42
        assert result.months == 1
        assert result.price == 490.0
        assert result.action == IssuanceAction.NEW
        assert result.key_id == 7
        assert result.host_name == "HOST1"
        assert result.plan_id == 2

    def test_missing_user_id_raises(self):
        meta = self._base_meta()
        del meta["user_id"]
        with pytest.raises(ValueError, match="metadata_parse_failed"):
            parse_payment_metadata(meta)

    def test_missing_host_name_raises(self):
        meta = self._base_meta()
        del meta["host_name"]
        with pytest.raises(ValueError, match="metadata_parse_failed"):
            parse_payment_metadata(meta)

    def test_invalid_price_raises(self):
        with pytest.raises(ValueError, match="metadata_parse_failed"):
            parse_payment_metadata(self._base_meta(price="not_a_number"))

    def test_unknown_action_defaults_to_new(self):
        result = parse_payment_metadata(self._base_meta(action="unknown_action"))
        assert result.action == IssuanceAction.NEW

    def test_extend_action_parsed(self):
        result = parse_payment_metadata(self._base_meta(action="extend"))
        assert result.action == IssuanceAction.EXTEND

    def test_super_flow_callback(self):
        def always_super(**_kwargs):
            return True

        result = parse_payment_metadata(
            self._base_meta(),
            is_super_flow_fn=always_super,
        )
        assert result.is_super_flow is True

    def test_payment_confirmed_true(self):
        result = parse_payment_metadata(
            self._base_meta(payment_confirmed="true"),
        )
        assert result.payment_confirmed is True

    def test_empty_payment_id(self):
        result = parse_payment_metadata(self._base_meta(external_payment_id=""))
        assert result.payment_id == ""


class TestCheckPaymentConfirmed:
    def test_paid_status_passes(self):
        meta = PaymentMeta(
            payment_id="x", user_id=1, months=1, price=1.0,
            action=IssuanceAction.NEW, key_id=1, host_name="h", plan_id=1,
        )
        assert check_payment_confirmed(meta, tx_status="paid") is True

    def test_confirmed_flag_passes(self):
        meta = PaymentMeta(
            payment_id="x", user_id=1, months=1, price=1.0,
            action=IssuanceAction.NEW, key_id=1, host_name="h", plan_id=1,
            payment_confirmed=True,
        )
        assert check_payment_confirmed(meta, tx_status="pending") is True

    def test_neither_blocks(self):
        meta = PaymentMeta(
            payment_id="x", user_id=1, months=1, price=1.0,
            action=IssuanceAction.NEW, key_id=1, host_name="h", plan_id=1,
        )
        assert check_payment_confirmed(meta, tx_status="pending") is False


class TestIsFatalPrecheckReason:
    @pytest.mark.parametrize(
        "reason",
        ["blitz_disabled", "host_not_found", "no_host", "super_not_configured", "host_unavailable", "timeout", "", None],
    )
    def test_precheck_reasons_are_retryable(self, reason):
        assert is_fatal_precheck_reason(reason) is False


class TestCoerceBool:
    @pytest.mark.parametrize("val,expected", [
        (True, True),
        (False, False),
        ("true", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        (1, True),
        (0, False),
        (None, False),
    ])
    def test_values(self, val, expected):
        assert _coerce_bool(val) is expected


class TestParsePaymentMetadataEdgeCases:
    def _base(self, **kw):
        m = {
            "external_payment_id": "p1", "user_id": "1", "months": "1",
            "price": "100", "action": "new", "key_id": "1",
            "host_name": "H", "plan_id": "1", "plan_name": "P",
            "customer_email": "e@e", "payment_method": "PM",
        }
        m.update(kw)
        return m

    def test_zero_months_parsed(self):
        result = parse_payment_metadata(self._base(months="0"))
        assert result.months == 0

    def test_negative_price_parsed(self):
        result = parse_payment_metadata(self._base(price="-100"))
        assert result.price == -100.0

    def test_numeric_user_id_string(self):
        result = parse_payment_metadata(self._base(user_id="999"))
        assert result.user_id == 999

    def test_missing_plan_name_ok(self):
        m = self._base()
        del m["plan_name"]
        result = parse_payment_metadata(m)
        assert result.plan_name is None or result.plan_name == ""

    def test_super_flow_fn_exception_returns_false(self):
        def boom(**_kw):
            raise RuntimeError("config error")
        result = parse_payment_metadata(self._base(), is_super_flow_fn=boom)
        assert result.is_super_flow is False
