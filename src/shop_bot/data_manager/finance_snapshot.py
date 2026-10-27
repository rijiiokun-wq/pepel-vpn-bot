"""Aggregate the known PePel journal without inferring another project's money."""

import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from shop_bot.data_manager.dashboard_analytics import payment_time_moscow
from shop_bot.data_manager.finance_ledger import (
    format_cents,
    money_to_cents,
    normalize_pepel_lava_income,
)


def build_history_finance_snapshot(history_path, *, date_from=None, date_to=None):
    """Use one complete export snapshot; never add it to the bot's journal.

    Both sources include the same PePel invoices. Combining their totals would
    double count payments and reintroduce invoices since refunded at LAVA.
    """
    from shop_bot.data_manager.finance_history import load_history

    history = load_history(history_path, date_from=date_from, date_to=date_to)
    from shop_bot.data_manager.finance_live import read_live, merge_income
    live = read_live(history_path, history)
    if live is not None and date_to is None:
        history['end'] = max(history['end'], datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat())
    merge_income(history, live)
    subscriptions_live = read_live(history_path, history, project_id='subscriptions')
    if subscriptions_live is not None and date_to is None:
        history['end'] = max(history['end'], datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat())
    merge_income(history, subscriptions_live, project_id='subscriptions')
    history["recorded_at_label"] = datetime.fromisoformat(history["recorded_at"]).astimezone(
        ZoneInfo("Europe/Moscow")
    ).strftime("%d.%m.%Y %H:%M МСК")
    daily = defaultdict(lambda: {"cents": 0, "count": 0})
    for project in history["projects"]:
        for row in project["daily"]:
            daily[row["date"]]["cents"] += money_to_cents(row["gross_rub"])
            daily[row["date"]]["count"] += row["count"]
    return {
        "start": history["start"], "end": history["end"],
        "gross_rub": format_cents(sum(row["cents"] for row in daily.values())),
        "count": sum(row["count"] for row in daily.values()),
        "gross_by_day": [
            {"date": day, "gross_rub": format_cents(values["cents"]), "count": values["count"]}
            for day, values in sorted(daily.items(), reverse=True)
        ],
        "history": history, "source": "lava_export_snapshot",
        "history_configured": True, "history_complete": False,
        "subscriptions_connected": bool(subscriptions_live and subscriptions_live['last_webhook']), "allocation_available": False,
    }


def build_finance_snapshot(db_path, *, date_from, date_to):
    start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
    if start > end or (end - start).days > 3660:
        raise ValueError("Invalid finance period")
    # Deliberately exclude customer names, IDs, addresses and credentials.
    query = """
      WITH source AS (
        SELECT *, CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END AS safe_meta
        FROM transactions
      )
      SELECT payment_id, status, amount_rub, confirmed_at, created_date, bot_source,
        COALESCE(NULLIF(payment_method, ''), json_extract(safe_meta, '$.payment_method')) AS payment_method,
        json_extract(safe_meta, '$.payment_confirmed') AS payment_confirmed,
        json_extract(safe_meta, '$.payment_confirmed_at') AS payment_confirmed_at,
        json_extract(safe_meta, '$.paid_at') AS paid_at,
        json_extract(safe_meta, '$.lava_pay_service') AS payment_service
      FROM source
      WHERE status='paid' OR (status IN ('pending','processing')
        AND json_extract(safe_meta, '$.payment_confirmed')=1)
    """
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        records = [dict(row) for row in conn.execute(query)]
    ledger = normalize_pepel_lava_income(records, payment_time_resolver=payment_time_moscow)
    selected = []
    missing_dates = 0
    uncertain_dates = 0
    for payment in ledger.payments:
        if not payment.paid_at:
            missing_dates += 1
            continue
        day = datetime.fromisoformat(payment.paid_at).date()
        if start <= day <= end:
            selected.append(payment)
            uncertain_dates += payment.date_source in {"legacy_unshifted", "ambiguous_confirmation"}
    daily = defaultdict(lambda: {"cents": 0, "count": 0})
    for payment in selected:
        day = datetime.fromisoformat(payment.paid_at).date().isoformat()
        daily[day]["cents"] += payment.gross_cents
        daily[day]["count"] += 1
    total = sum(item.gross_cents for item in selected)
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "gross_rub": format_cents(total), "count": len(selected),
        "gross_by_day": [{"date": day, "label": date.fromisoformat(day).strftime("%d.%m.%Y"),
                          "gross_rub": format_cents(values["cents"]), "count": values["count"]}
                         for day, values in sorted(daily.items(), reverse=True)],
        "missing_dates": missing_dates, "uncertain_dates": uncertain_dates,
        "source": "pepel_bot_confirmed_lava_payments",
        "history_complete": False, "payouts_complete": False,
        "subscriptions_connected": False, "allocation_available": False,
    }
