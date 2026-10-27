"""Signed accounting-only mirror for the external Subscriptions shop."""
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from flask import request
from shop_bot.data_manager.finance_history import _money
from shop_bot.data_manager.finance_live import connect, journal_path, record, reference
from shop_bot.modules.lava_api import verify_webhook_request

SHOP_ID = os.environ.get("LAVA_SUBSCRIPTIONS_FINANCE_SHOP_ID", "").strip()
ENDPOINT = '/webhooks/lava/subscriptions-finance'


def capture(data, history_path, *, now=None):
    # Caller verified the dedicated project signature. No access to customer DB.
    for key in ('shop_id', 'shopId'):
        if data.get(key) and data[key] != SHOP_ID:
            raise ValueError('Wrong shop')
    invoice = str(uuid.UUID(str(data.get('invoice_id') or data.get('invoiceId') or '')))
    order = str(data.get('order_id') or data.get('orderId') or '').strip()
    if not order or len(order)>128:
        raise ValueError('Invalid order')
    status = str(data.get('status') or '').lower()
    if status in ('created', 'pending', 'fail', 'expired'):
        return
    if status not in ('success', 'refund'):
        raise ValueError('Unknown status')
    gross = _money(str(data.get('amount')), positive=True)
    net = _money(str(data['credited'])) if data.get('credited') is not None else None
    received = now or datetime.now(timezone.utc)
    try:
        paid = datetime.fromisoformat(str(data.get('pay_time') or data.get('payTime') or '').replace('Z','+00:00'))
        if paid.tzinfo is None or paid > received:
            paid = received
    except ValueError:
        paid = received
    path = journal_path(history_path, 'subscriptions')
    with closing(connect(path)) as conn, conn:
        record(conn, reference(invoice), gross, net, paid.isoformat(), status, 'webhook')
        conn.execute("INSERT INTO settings VALUES('last_webhook',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (received.isoformat(),))


def register_subscriptions_finance(flask_app, *, history_path, secret_reader=None):
    def read_secret(path):
        config = json.loads(Path(path).with_name('subscriptions-webhook-secret.json').read_text())
        if not SHOP_ID or config.get('shop_id') != SHOP_ID or not config.get('additional_key'):
            raise ValueError('Missing configuration')
        return config['additional_key']

    def receive():
        if request.content_length is not None and request.content_length > 65536:
            return 'Too large', 413
        raw = request.stream.read(65537)
        if not raw or len(raw)>65536:
            return 'Bad request', 400
        path = history_path()
        if not path:
            return 'Not configured', 503
        try:
            secret = (secret_reader or read_secret)(path)
        except (OSError, ValueError, TypeError):
            return 'Not configured', 503
        if not verify_webhook_request(raw, request.headers, secret=secret, allow_legacy=True):
            return 'Forbidden', 403
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError('Object expected')
            capture(data, path)
        except (ValueError, TypeError, KeyError):
            return 'Invalid event', 400
        except (OSError, sqlite3.Error):
            return 'Retry later', 503
        return 'OK', 200

    flask_app.add_url_rule(ENDPOINT, 'subscriptions_finance_webhook', receive, methods=['POST'])
