from datetime import datetime

from shop_bot.webhook_server import app as app_module


def test_resolve_issue_expiry_options_supports_calendar_months_and_years():
    options = app_module._resolve_issue_expiry_options(
        raw_days=2,
        raw_months=1,
        raw_years=1,
        raw_unlimited=False,
        now=datetime(2026, 1, 31, 10, 0, 0),
    )

    assert options["days"] == 2
    assert options["months"] == 1
    assert options["years"] == 1
    assert options["unlimited"] is False
    assert options["expiry_dt"] == datetime(2027, 3, 2, 10, 0, 0)
    assert options["expiry_label"] == "02.03.2027 10:00"


def test_resolve_issue_expiry_options_supports_unlimited():
    options = app_module._resolve_issue_expiry_options(
        raw_days=0,
        raw_months=0,
        raw_years=0,
        raw_unlimited=True,
        now=datetime(2026, 3, 25, 12, 0, 0),
    )

    assert options["unlimited"] is True
    assert options["expiry_dt"] == datetime(2099, 12, 31, 23, 59, 59)
    assert options["expiry_label"] == "Безлимит"
    assert options["panel_days"] > 20000
