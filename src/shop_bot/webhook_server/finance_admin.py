"""Admin-only read-only finance routes, separate from payment finalization."""

import os
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import render_template, request
from shop_bot.data_manager.finance_availability import get_pepel_availability
from shop_bot.data_manager.finance_ledger import FinanceDataError
from shop_bot.data_manager.finance_snapshot import (
    build_finance_snapshot,
    build_history_finance_snapshot,
)
from shop_bot.modules.lava_finance_api import get_profile_balance


def register_finance_routes(flask_app, *, login_required, resolve_period, db_path,
                            snapshot_reader=None, profile_reader=None,
                            history_path=None, history_reader=None):
    read_snapshot = snapshot_reader or build_finance_snapshot
    read_history = history_reader or build_history_finance_snapshot
    read_profile = profile_reader or get_profile_balance
    from shop_bot.webhook_server.subscriptions_finance import (
        register_subscriptions_finance,
    )
    register_subscriptions_finance(flask_app, history_path=history_path or
        (lambda: os.environ.get('LAVA_FINANCE_HISTORY_FILE', '').strip()))
    flask_app.jinja_env.filters["finance_money"] = lambda value: format(Decimal(str(value)), ",.2f").replace(",", " ")

    from shop_bot.webhook_server.payout_finance import register_payout_finance
    register_payout_finance(flask_app, history_path=history_path or
        (lambda: os.environ.get('LAVA_FINANCE_HISTORY_FILE', '').strip()))

    def finance_time(value):
        if not value:
            return 'Нет данных'
        try:
            moment = datetime.fromisoformat(str(value))
            if moment.tzinfo is None:
                return 'Нет подтверждённого времени'
            return moment.astimezone(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M:%S МСК')
        except (ValueError, TypeError):
            return 'Нет подтверждённого времени'
    flask_app.jinja_env.filters['finance_time'] = finance_time

    @flask_app.context_processor
    def finance_navigation():
        # The shared template can be installed before both runtimes restart.
        # An older runtime will omit the link instead of resolving a new route.
        return {"finance_enabled": True}

    def finance_page():
        period = resolve_period(request.args)
        export_path = history_path() if history_path else os.environ.get("LAVA_FINANCE_HISTORY_FILE", "").strip()
        all_history = (export_path and request.args.get("period") == "all"
                       and not request.args.get("date_from") and not request.args.get("date_to"))
        try:
            if export_path:
                finance = read_history(
                    export_path,
                    date_from=None if all_history else period["date_from"],
                    date_to=None if all_history else period["date_to"],
                )
                if all_history:
                    period["selected_period"] = "all"
            else:
                finance = read_snapshot(db_path(), date_from=period["date_from"], date_to=period["date_to"])
        except (sqlite3.Error, OSError, ValueError, FinanceDataError):
            # An unreadable journal is not zero revenue. Never expose DB details.
            finance = {"start": period["date_from"], "end": period["date_to"],
                       "gross_rub": None, "count": None, "gross_by_day": [],
                       "journal_error": not bool(export_path), "history_complete": False,
                       "history_error": bool(export_path), "history_configured": bool(export_path),
                       "allocation_available": False}
        profile = read_profile()
        from shop_bot.data_manager.finance_payouts import read_view
        try:
            payout_live = read_view(export_path)
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
            payout_live = {'rows': [], 'unresolved': True, 'error': True}
        payout_live['receiver_ready'] = bool(export_path and Path(export_path).with_name('payout-webhook-secret.json').is_file())
        # Payouts whose debit time is unavailable remain visible by observation
        # time. A historical period filter must never hide an unresolved debit.
        finance.update(period=period["selected_period"], profile=profile,
                       payout_live=payout_live,
                       pepel_availability=get_pepel_availability(export_path, profile=profile))
        response = flask_app.make_response(render_template("finance.html", finance=finance))
        response.headers["Cache-Control"] = "no-store"
        return response

    flask_app.add_url_rule("/finance", "finance_page", login_required(finance_page), methods=["GET"])

    def pepel_balance_fragment():
        export_path = history_path() if history_path else os.environ.get("LAVA_FINANCE_HISTORY_FILE", "").strip()
        balance = get_pepel_availability(export_path, profile=read_profile())
        response = flask_app.make_response(render_template("finance_pepel_balance.html", balance=balance))
        response.headers["Cache-Control"] = "no-store"
        return response

    flask_app.add_url_rule("/finance/pepel-balance", "pepel_balance_fragment",
                          login_required(pepel_balance_fragment), methods=["GET"])
