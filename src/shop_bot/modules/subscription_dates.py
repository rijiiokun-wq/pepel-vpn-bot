import calendar
import math

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PurchaseWindow:
    paid_at: datetime
    anchor_at: datetime
    expiry_at: datetime
    panel_days: int


def parse_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value

    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d")
            except ValueError:
                return None

    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def add_calendar_months(anchor: datetime, months: int) -> datetime:
    safe_months = max(0, int(months or 0))
    if safe_months == 0:
        return anchor

    month_index = (anchor.month - 1) + safe_months
    year = anchor.year + (month_index // 12)
    month = (month_index % 12) + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return anchor.replace(year=year, month=month, day=day)


def full_days_between(start_dt: datetime, end_dt: datetime) -> int:
    delta_seconds = max((end_dt - start_dt).total_seconds(), 0.0)
    # Panel APIs accept int days, round up to avoid premature expiry.
    return max(1, int(math.ceil(delta_seconds / 86400.0)))


def panel_days_until(expiry_at: datetime, *, now: datetime | None = None) -> int:
    """
    Return panel-compatible days from "now" until target expiry.

    Useful for panels that only support relative TTL in days.
    """
    start = now or datetime.now()
    return full_days_between(start, expiry_at)


def resolve_recurring_cycle_anchor(
    *,
    paid_at: datetime,
    current_expiry: datetime | None,
    created_at: datetime | None,
) -> datetime:
    """Keep the original cycle only while an existing entitlement is active."""
    if current_expiry and current_expiry > paid_at and created_at:
        return created_at
    return paid_at


def build_purchase_window(
    *,
    months: int,
    paid_at: datetime | None,
    current_expiry: datetime | None = None,
    now: datetime | None = None,
) -> PurchaseWindow:
    safe_now = now or datetime.now()
    paid_anchor = paid_at or safe_now

    if current_expiry and current_expiry > paid_anchor:
        anchor = current_expiry
    else:
        anchor = paid_anchor

    expiry = add_calendar_months(anchor, months)
    panel_days = full_days_between(anchor, expiry)
    return PurchaseWindow(
        paid_at=paid_anchor,
        anchor_at=anchor,
        expiry_at=expiry,
        panel_days=panel_days,
    )
