from datetime import UTC, datetime

import pytest

pytest.importorskip("aiogram")

from shop_bot.webhook_server import app as app_module


def _freeze_app_now(monkeypatch, instant: datetime) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone(UTC).replace(tzinfo=None)
            return instant.astimezone(tz)

    monkeypatch.setattr(app_module, "datetime", FrozenDateTime)


def test_dashboard_default_period_uses_moscow_month_to_date(monkeypatch):
    _freeze_app_now(monkeypatch, datetime(2026, 6, 11, 9, 15, tzinfo=UTC))

    period = app_module._resolve_dashboard_period(
        period_value=None,
        raw_date_from=None,
        raw_date_to=None,
    )

    assert period["custom_range_active"] is False
    assert period["default_range_active"] is True
    assert period["selected_period"] == "month"
    assert period["date_from"] == "2026-06-01"
    assert period["date_to"] == "2026-06-11"
    assert period["period_days"] == 11
    assert period["caption"] == "с 01.06.2026 по 11.06.2026 · МСК"


def test_dashboard_default_period_respects_moscow_date_boundary(monkeypatch):
    _freeze_app_now(monkeypatch, datetime(2026, 5, 31, 21, 30, tzinfo=UTC))

    period = app_module._resolve_dashboard_period(
        period_value=None,
        raw_date_from=None,
        raw_date_to=None,
    )

    assert period["date_from"] == "2026-06-01"
    assert period["date_to"] == "2026-06-01"
    assert period["period_days"] == 1


def test_dashboard_explicit_period_keeps_preset_days(monkeypatch):
    _freeze_app_now(monkeypatch, datetime(2026, 6, 11, 9, 15, tzinfo=UTC))

    period = app_module._resolve_dashboard_period(
        period_value=7,
        raw_date_from=None,
        raw_date_to=None,
    )

    assert period["custom_range_active"] is False
    assert period["default_range_active"] is False
    assert period["selected_period"] == 7
    assert period["period_days"] == 7
    assert period["date_from"] == "2026-06-05"
    assert period["date_to"] == "2026-06-11"
    assert period["range_start"] == "2026-06-05"
    assert period["range_end"] == "2026-06-11"
