"""Read-only dashboard ledger and aggregates.

No historical values are rewritten. Financial confirmation, issuance and Telegram
delivery deliberately remain separate dimensions. All displayed period boundaries
are calendar dates in Moscow; legacy dates of unknown origin stay unshifted.
"""

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

MSK = timezone(timedelta(hours=3))
PRODUCT_TYPES = ("vless", "super", "old", "hysteria")


def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def payment_time_moscow(row):
    """Prefer recorded UTC confirmation; never add 3h to arbitrary legacy dates."""
    for field in ("confirmed_at", "payment_confirmed_at"):
        raw = str(row.get(field) or "").strip()
        parsed = _parse_datetime(raw)
        if parsed is not None:
            if parsed.tzinfo is not None:
                return parsed.astimezone(MSK), "confirmed_explicit"
            # SQLite CURRENT_TIMESTAMP is UTC with a space separator. The
            # scheduler can also store provider ISO values: naive ISO strings
            # have no proven timezone and must not be assumed UTC.
            if field == "confirmed_at" and len(raw) >= 19 and raw[10] == " ":
                parsed = parsed.replace(tzinfo=UTC)
                return parsed.astimezone(MSK), "confirmed_utc"
    # A provider timestamp with an explicit zone is unambiguous. Naive paid_at
    # was produced by several older, incompatible paths and is not reinterpreted.
    parsed = _parse_datetime(row.get("paid_at"))
    if parsed is not None and parsed.tzinfo is not None:
        return parsed.astimezone(MSK), "provider"
    parsed = _parse_datetime(row.get("created_date"))
    if parsed is None:
        return None, "invalid"
    if parsed.tzinfo is not None:
        return parsed.astimezone(MSK), "explicit"
    source = "ambiguous_confirmation" if row.get("confirmed_at") else "legacy_unshifted"
    return parsed.replace(tzinfo=MSK), source


def _money(value):
    try:
        amount = Decimal(str(value or 0))
        return amount if amount.is_finite() else Decimal(0)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0)


def _load_ledger(conn, classify_product_type):
    # Extract only display/accounting metadata. Subscription URLs, emails,
    # credentials and issuance debug payloads never enter the dashboard snapshot.
    sql = """
        WITH source AS (
            SELECT t.*, CASE WHEN json_valid(t.metadata) THEN t.metadata ELSE '{}' END AS safe_meta
            FROM transactions t
        )
        SELECT t.transaction_id, t.payment_id, t.user_id, t.status, t.amount_rub,
               COALESCE(NULLIF(t.username, ''), NULLIF(u.username, ''), 'N/A') AS username,
               COALESCE(NULLIF(t.payment_method, ''), json_extract(safe_meta, '$.payment_method'), '—') AS payment_method,
               COALESCE(t.bot_source, 'main') AS bot_source,
               t.issuance_state, t.confirmed_at, t.created_date,
               json_extract(safe_meta, '$.payment_confirmed_at') AS payment_confirmed_at,
               json_extract(safe_meta, '$.paid_at') AS paid_at,
               json_extract(safe_meta, '$.host_name') AS host_name,
               json_extract(safe_meta, '$.plan_name') AS plan_name,
               json_extract(safe_meta, '$.action') AS action,
               json_extract(safe_meta, '$.original_action') AS original_action,
               COALESCE(json_extract(safe_meta, '$.key_id'), json_extract(safe_meta, '$.payment_applied_key_id')) AS key_id,
               json_extract(safe_meta, '$.key_delivered_at') AS key_delivered_at,
               (COALESCE(u.trial_used, 0) = 1 OR COALESCE(u.trial_super_used, 0) = 1) AS used_trial
        FROM source t LEFT JOIN users u ON u.telegram_id = t.user_id
        WHERE t.status = 'paid'
           OR (t.status IN ('pending', 'processing') AND json_extract(safe_meta, '$.payment_confirmed') = 1)
    """
    rows = []
    for raw in conn.execute(sql):
        row = dict(raw)
        instant, source = payment_time_moscow(row)
        row["date_source"] = source
        row["paid_at"] = instant.isoformat(timespec="seconds") if instant else None
        row["paid_at_display"] = instant.strftime("%d.%m.%Y %H:%M:%S") if instant else "Дата неизвестна"
        row["created_date"] = row["paid_at"]
        row["day"] = instant.date().isoformat() if instant else None
        row["tx_type"] = classify_product_type(row.get("host_name"), row.get("plan_name"))
        row["missing_product_names"] = not row.get("host_name") and not row.get("plan_name")
        row["host_name"] = row.get("host_name") or "—"
        row["plan_name"] = row.get("plan_name") or "—"
        row["bot_source"] = "reserve" if row["bot_source"] == "reserve" else "main"
        row["amount_rub"] = float(_money(row["amount_rub"]))
        action = row.get("original_action") or row.get("action")
        row["action_label"] = {"new": "Покупка", "extend": "Продление", "renew": "Продление"}.get(action, "Нет отметки")
        state = row.get("issuance_state") or "awaiting_payment"
        row["issuance_label"] = {
            "issued": "Выдано",
            "issuing": "Выдаётся",
            "confirmed": "Ожидает выдачи",
            "retryable_failed": "Повтор выдачи",
            "fatal_failed": "Ошибка выдачи",
            "exhausted": "Попытки исчерпаны",
            "canceled": "Отменено",
        }.get(state, "Выдано (история)" if row["status"] == "paid" else "Ожидает выдачи")
        row["issuance_problem"] = state in {"retryable_failed", "fatal_failed", "exhausted"}
        row["paid_not_issued"] = row["status"] != "paid" and state != "issued"
        row["delivery_label"] = "Доставлено" if row.get("key_delivered_at") else "Нет отметки"
        rows.append(row)
    rows.sort(key=lambda row: (row["paid_at"] or "", int(row["transaction_id"])), reverse=True)
    return rows


def _totals(rows, first_payment_ids):
    amount = sum((_money(row["amount_rub"]) for row in rows), Decimal(0))
    count = len(rows)
    first_count = sum(row["transaction_id"] in first_payment_ids for row in rows)
    return {
        "transactions": count,
        "revenue_rub": float(amount),
        "avg_check_rub": float(amount / count) if count else 0.0,
        "buyers": len({row["user_id"] for row in rows}),
        "first_time_buyers": first_count,
        "repeat_payments": count - first_count,
        "new_purchases": sum(row["action_label"] == "Покупка" for row in rows),
        "renewals": sum(row["action_label"] == "Продление" for row in rows),
        "issuance_problems": sum(row["issuance_problem"] for row in rows),
        "paid_not_issued": sum(row["paid_not_issued"] for row in rows),
        "delivery_unconfirmed": sum(not row.get("key_delivered_at") for row in rows),
    }


def _dashboard_stats(conn, ledger, first_payment_ids):
    from shop_bot.data_manager.database import _with_effective_host_status
    from shop_bot.data_manager.host_visibility import (
        filter_customer_visible_hosts,
        filter_customer_visible_keys,
        parse_customer_visible_host_names,
    )

    setting = conn.execute("SELECT value FROM bot_settings WHERE key = 'customer_visible_host_names'").fetchone()
    visible = parse_customer_visible_host_names(setting[0] if setting else None)
    users = conn.execute("SELECT COUNT(*), COALESCE(SUM(total_spent), 0) FROM users").fetchone()
    keys = [dict(row) for row in conn.execute("SELECT host_name, is_archived, expiry_date FROM vpn_keys")]
    hosts = [
        _with_effective_host_status(dict(row))
        for row in conn.execute(
            "SELECT host_name, status, last_check_at, is_maintenance, delivery_state FROM xui_hosts"
        )
    ]
    current_keys = filter_customer_visible_keys(keys, visible_host_names=visible)
    current_hosts = filter_customer_visible_hosts(hosts, visible_host_names=visible)
    now = datetime.now(MSK)
    active_keys = 0
    for row in current_keys:
        expiry = _parse_datetime(row["expiry_date"])
        if expiry is not None and expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=MSK)
        if not row["is_archived"] and expiry is not None and expiry > now:
            active_keys += 1
    totals = _totals(ledger, first_payment_ids)
    return {
        "user_count": int(users[0]),
        "total_keys": len(current_keys),
        "all_keys_including_legacy": len(keys),
        "active_keys": active_keys,
        "archived_keys": sum(bool(row["is_archived"]) for row in current_keys),
        "total_spent": totals["revenue_rub"],
        "legacy_profile_total": float(users[1]),
        "ledger_transactions": totals["transactions"],
        "ledger_difference": float(_money(users[1]) - _money(totals["revenue_rub"])),
        "host_count": len(current_hosts),
        "all_hosts_including_legacy": len(hosts),
        "healthy_hosts": sum(row["status"] == "healthy" and not row["is_maintenance"] for row in current_hosts),
        "maintenance_hosts": sum(bool(row["is_maintenance"]) for row in current_hosts),
        "active_hosts": sum(row["delivery_state"] == "active" and not row["is_maintenance"] for row in current_hosts),
        "paid_not_issued": totals["paid_not_issued"],
        "invalid_payment_dates": sum(row["date_source"] == "invalid" for row in ledger),
        "legacy_payment_dates": sum(
            row["date_source"] in {"legacy_unshifted", "ambiguous_confirmation"} for row in ledger
        ),
        "payment_date_sources": dict(Counter(row["date_source"] for row in ledger)),
        "missing_product_names": sum(row["missing_product_names"] for row in ledger),
        "missing_payment_methods": sum(
            str(row["payment_method"]).strip().casefold() in {"", "—", "unknown", "n/a"} for row in ledger
        ),
    }


def build_dashboard_snapshot(
    conn,
    *,
    classify_product_type,
    date_from,
    date_to,
    tx_type="all",
    bot_source="all",
    search="",
    page=1,
    per_page=8,
    include_all=False,
):
    start, end = date.fromisoformat(str(date_from)), date.fromisoformat(str(date_to))
    if start > end:
        start, end = end, start
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - (end - start)
    tx_type = tx_type if tx_type in PRODUCT_TYPES else "all"
    bot_source = bot_source if bot_source in {"main", "reserve"} else "all"
    search = str(search or "").strip().casefold()[:200]
    ledger = _load_ledger(conn, classify_product_type)
    # First payment means first confirmed ledger record, not trial conversion.
    first_payment = {}
    for row in reversed(ledger):
        if row["day"]:
            first_payment.setdefault(row["user_id"], row["transaction_id"])
    first_payment_ids = set(first_payment.values())

    def matches(row, *, selected_type=True):
        return (
            (bot_source == "all" or row["bot_source"] == bot_source)
            and (not selected_type or tx_type == "all" or row["tx_type"] == tx_type)
            and (not search or search in str(row["user_id"]) or search in str(row["payment_id"]).casefold())
        )

    range_rows = [
        row
        for row in ledger
        if row["day"] and start.isoformat() <= row["day"] <= end.isoformat() and matches(row, selected_type=False)
    ]
    selected = [row for row in range_rows if matches(row)]
    previous = [
        row
        for row in ledger
        if row["day"] and previous_start.isoformat() <= row["day"] <= previous_end.isoformat() and matches(row)
    ]
    summary = dict.fromkeys(PRODUCT_TYPES, 0)
    summary.update(Counter(row["tx_type"] for row in range_rows))
    current_totals, previous_totals = _totals(selected, first_payment_ids), _totals(previous, first_payment_ids)
    counts, amounts = Counter(), defaultdict(Decimal)
    for row in selected:
        counts[row["day"]] += 1
        amounts[row["day"]] += _money(row["amount_rub"])
    trial_rows = [row for row in selected if row["used_trial"]]
    trial_ever = {row["user_id"] for row in ledger if row["used_trial"] and matches(row)}
    trial_total = int(
        conn.execute(
            "SELECT COUNT(*) FROM users WHERE COALESCE(trial_used, 0) = 1 OR COALESCE(trial_super_used, 0) = 1"
        ).fetchone()[0]
    )
    trial_totals = _totals(trial_rows, first_payment_ids)
    safe_per_page = max(1, min(int(per_page or 8), 100))
    total_pages = max(1, (len(selected) + safe_per_page - 1) // safe_per_page)
    current_page = max(1, min(int(page or 1), total_pages))
    offset = (current_page - 1) * safe_per_page
    return {
        "stats": _dashboard_stats(conn, ledger, first_payment_ids),
        "transactions": selected if include_all else selected[offset : offset + safe_per_page],
        "total_transactions": len(selected),
        "current_page": current_page,
        "total_pages": total_pages,
        "tx_type_summary": summary,
        "analytics": {
            "days": (end - start).days + 1,
            "tx_type": tx_type,
            "bot_source": bot_source,
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "transactions_by_day": dict(counts),
            "revenue_by_day": {day: float(amount) for day, amount in amounts.items()},
            "totals": current_totals,
        },
        "trial_analytics": {
            "trial_users_total": trial_total,
            "converted_users_total": len(trial_ever),
            "conversion_rate_total": round(len(trial_ever) / trial_total * 100, 1) if trial_total else 0.0,
            "period_converted_users": trial_totals["buyers"],
            "period_transactions": trial_totals["transactions"],
            "period_revenue_rub": trial_totals["revenue_rub"],
            "period_first_time_buyers": trial_totals["first_time_buyers"],
            "period_repeat_payments": trial_totals["repeat_payments"],
            "sample": [],
        },
        "comparison": {
            "previous_date_from": previous_start.isoformat(),
            "previous_date_to": previous_end.isoformat(),
            "totals": previous_totals,
            "delta_percent": {
                key: round((current_totals[key] - old) / old * 100, 1) if old else None
                for key, old in previous_totals.items()
            },
        },
    }
