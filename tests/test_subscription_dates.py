from datetime import datetime

from shop_bot.modules import subscription_dates


def test_add_calendar_months_handles_month_end_non_leap():
    anchor = datetime(2025, 1, 31, 10, 15, 0)
    result = subscription_dates.add_calendar_months(anchor, 1)
    assert result == datetime(2025, 2, 28, 10, 15, 0)


def test_add_calendar_months_handles_month_end_leap():
    anchor = datetime(2024, 1, 31, 10, 15, 0)
    result = subscription_dates.add_calendar_months(anchor, 1)
    assert result == datetime(2024, 2, 29, 10, 15, 0)


def test_build_purchase_window_new_key_uses_paid_at_anchor():
    paid_at = datetime(2026, 3, 12, 19, 33, 0)
    window = subscription_dates.build_purchase_window(months=1, paid_at=paid_at)
    assert window.anchor_at == paid_at
    assert window.expiry_at == datetime(2026, 4, 12, 19, 33, 0)
    assert window.panel_days == 31


def test_build_purchase_window_extension_uses_active_expiry_anchor():
    paid_at = datetime(2026, 3, 12, 19, 33, 0)
    active_expiry = datetime(2026, 4, 1, 8, 0, 0)
    window = subscription_dates.build_purchase_window(
        months=1,
        paid_at=paid_at,
        current_expiry=active_expiry,
    )
    assert window.anchor_at == active_expiry
    assert window.expiry_at == datetime(2026, 5, 1, 8, 0, 0)
    assert window.panel_days == 30


def test_panel_days_until_includes_remaining_active_period():
    # Mirrors Blitz extension case:
    # paid_at is before current expiry, target expiry is one month after current expiry.
    now = datetime(2026, 2, 15, 16, 6, 0)
    target_expiry = datetime(2026, 4, 15, 19, 28, 0)
    days = subscription_dates.panel_days_until(target_expiry, now=now)
    assert days == 60


def test_panel_days_until_returns_minimum_one_day_for_past_target():
    now = datetime(2026, 3, 17, 12, 0, 0)
    target_expiry = datetime(2026, 3, 16, 12, 0, 0)
    days = subscription_dates.panel_days_until(target_expiry, now=now)
    assert days == 1
