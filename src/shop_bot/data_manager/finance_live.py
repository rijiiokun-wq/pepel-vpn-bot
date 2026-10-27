"""Idempotent accounting journal. Never changes customer DB or issues payments."""
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from shop_bot.data_manager.finance_history import _money, _format_money

MSK = ZoneInfo('Europe/Moscow')
SHOP_ID = os.environ.get("LAVA_FINANCE_SHOP_ID", "").strip()
log = logging.getLogger(__name__)


def reference(invoice_id):
    return hashlib.sha256(str(invoice_id).strip().encode()).hexdigest()


def journal_path(history_path, project_id='pepel'):
    if project_id not in ('pepel', 'subscriptions'):
        raise ValueError('Unknown finance project')
    return Path(history_path).with_name(project_id+'-live.sqlite3')


def connect(path, *, readonly=False):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                           uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute('PRAGMA query_only=ON')
    return conn


def create_journal(path, source_hash, baseline):
    """Deployment-only bootstrap from the exact historical CSV identifiers."""
    if Path(path).exists():
        raise ValueError('Refusing to replace existing accounting journal')
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript('''
          CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE baseline (ref TEXT PRIMARY KEY, status TEXT NOT NULL);
          CREATE TABLE payments (ref TEXT PRIMARY KEY, gross INTEGER NOT NULL,
            net INTEGER, paid_at TEXT NOT NULL, status TEXT NOT NULL,
            source TEXT NOT NULL, conflict INTEGER NOT NULL DEFAULT 0);
        ''')
        conn.execute('INSERT INTO settings VALUES (?,?)', ('source_hash', source_hash))
        conn.executemany('INSERT INTO baseline VALUES (?,?)', baseline)
    os.chmod(path, 0o600)


def record(conn, ref, gross, net, paid_at, status, source):
    if status not in ('success', 'refund') or gross <= 0 or (net is not None and not 0 <= net <= gross):
        raise ValueError('Invalid accounting event')
    instant = datetime.fromisoformat(paid_at)
    if instant.tzinfo is None:
        raise ValueError('Accounting timestamp requires timezone')
    # Serialize the read/merge/write across webhook workers and catch-up scans.
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    previous = conn.execute('SELECT * FROM payments WHERE ref=?', (ref,)).fetchone()
    if previous:
        conflict = previous['gross'] != gross
        if source != 'bot' and net is not None and previous['net'] is not None:
            conflict |= previous['net'] != net
        if conflict:
            conn.execute('UPDATE payments SET conflict=1 WHERE ref=?', (ref,))
            return
        # A late bot scan/duplicate success must never undo a refund or exact net.
        if previous['status'] == 'refund' or source == 'bot':
            return
        if previous['net'] is not None and net is None:
            if status != 'refund':
                return
            net = previous['net']
        if previous['source'] == 'csv' and status == 'success':
            return
        # Never extend a freeze on redelivery; allow a late exact event to upgrade a bot row.
        if previous['source'] != 'bot':
            paid_at = previous['paid_at']
    conn.execute('''INSERT INTO payments(ref,gross,net,paid_at,status,source) VALUES(?,?,?,?,?,?)
      ON CONFLICT(ref) DO UPDATE SET net=excluded.net,paid_at=excluded.paid_at,
       status=excluded.status,source=excluded.source''', (ref,gross,net,paid_at,status,source))


def capture_webhook(data, db_path, history_path, *, shop_id, now=None):
    """Only call AFTER signature verification. Verify local ownership and amount."""
    if not SHOP_ID or not history_path or shop_id != SHOP_ID or not journal_path(history_path).is_file():
        return
    invoice = str(data.get('invoice_id') or data.get('invoiceId') or '').strip()
    order = str(data.get('order_id') or data.get('orderId') or '').strip()
    status = str(data.get('status') or '').lower()
    if status not in ('success', 'refund') or not invoice or not order:
        return
    with closing(connect(db_path, readonly=True)) as conn:
        row = conn.execute('SELECT payment_id,amount_rub,payment_method,metadata FROM transactions '
                           'WHERE payment_id IN (?,?)', (invoice,'lava-order:'+order)).fetchone()
    if row is None:
        return
    meta = json.loads(row['metadata'] or '{}')
    if str(row['payment_method'] or meta.get('payment_method') or '').lower() != 'lava':
        return
    expected_order = meta.get('lava_order_id')
    if expected_order and expected_order != order:
        return
    gross = _money(str(data.get('amount')), positive=True)
    if gross != _money(str(row['amount_rub']), positive=True):
        return
    # The signed provider credited field is exact; absent fields remain unknown.
    net = _money(str(data['credited'])) if data.get('credited') is not None else None
    received = now or datetime.now(timezone.utc)
    raw_time = str(data.get('pay_time') or data.get('payTime') or '')
    try:
        paid = datetime.fromisoformat(raw_time.replace('Z','+00:00'))
        if paid.tzinfo is None or paid > received:
            paid = received
    except ValueError:
        paid = received
    with closing(connect(journal_path(history_path))) as conn, conn:
        record(conn, reference(invoice), gross, net, paid.isoformat(), status, 'webhook')


def catch_up(db_path, history_path, *, now=None):
    """Catch confirmed bot payments even if delivery is pending or the hook was missed."""
    now = now or datetime.now(timezone.utc)
    with closing(connect(db_path, readonly=True)) as conn:
        rows = list(conn.execute('''WITH source AS (
          SELECT *,CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END AS m FROM transactions)
          SELECT payment_id,amount_rub,confirmed_at,
            json_extract(m,'$.paid_at') paid_at, json_extract(m,'$.payment_confirmed_at') confirmed
          FROM source WHERE lower(COALESCE(NULLIF(payment_method,''),json_extract(m,'$.payment_method')))='lava'
          AND (status='paid' OR (status IN ('pending','processing') AND json_extract(m,'$.payment_confirmed')=1))'''))
    with closing(connect(journal_path(history_path))) as conn, conn:
        baseline = {r['ref'] for r in conn.execute("SELECT ref FROM baseline WHERE status IN ('Успех','Успешный возврат')")}
        for row in rows:
            ref = reference(row['payment_id'])
            if ref in baseline:
                continue
            raw = row['paid_at'] or row['confirmed_at'] or row['confirmed']
            try:
                paid = datetime.fromisoformat(str(raw).replace('Z','+00:00'))
                if paid.tzinfo is None:
                    paid = paid.replace(tzinfo=timezone.utc)
                if paid > now:
                    paid = now
            except ValueError:
                paid = now
            record(conn, ref, _money(str(row['amount_rub']),positive=True), None,
                   paid.isoformat(), 'success', 'bot')
        conn.execute("INSERT INTO settings VALUES('last_sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (now.isoformat(),))


async def periodic_sync(db_path, history_path):
    while True:
        try:
            await asyncio.to_thread(catch_up, db_path, history_path)
        except Exception as exc:
            log.error('Finance catch-up failed: %s', type(exc).__name__)
        try:
            from shop_bot.data_manager.finance_payouts import sync_once
            await asyncio.to_thread(sync_once, history_path)
        except Exception as exc:  # noqa: BLE001 - Keep payment catch-up alive if payout verification fails.
            log.error('Finance payout verification failed: %s', type(exc).__name__)
        await asyncio.sleep(60)


def read_live(history_path, history, *, now=None, project_id='pepel'):
    path = journal_path(history_path, project_id)
    if not path.exists():
        return None
    with closing(connect(path,readonly=True)) as conn:
        conn.execute('BEGIN')
        settings = dict(conn.execute('SELECT key,value FROM settings'))
        project = next(p for p in history['projects'] if p['project_id']==project_id)
        if settings['source_hash'] != project['source_sha256']:
            raise ValueError('Live journal historical baseline changed')
        rows = [dict(r) for r in conn.execute('''SELECT p.*,b.status baseline_status FROM payments p
           LEFT JOIN baseline b ON b.ref=p.ref
           WHERE b.status IS NULL OR b.status NOT IN ('Успех','Успешный возврат')
              OR (p.status='refund' AND b.status!='Успешный возврат') OR p.conflict=1''')]
    now = now or datetime.now(timezone.utc)
    synced = settings.get('last_sync')
    stale = project_id=='pepel' and (not synced or (now-datetime.fromisoformat(synced)).total_seconds()>180)
    return {'rows':rows, 'last_sync':synced, 'stale':stale,
            'last_webhook':settings.get('last_webhook'),
            'backfill_complete':settings.get('backfill_complete')=='true',
            'pending_net':sum(r['net'] is None for r in rows),
            'requires_reconciliation':any(r['status']=='refund' or r['conflict'] for r in rows)}


def merge_income(history, live, project_id='pepel'):
    """Overlay only invoice IDs absent from the baseline; never add two totals."""
    if live is None:
        return
    project = next(p for p in history['projects'] if p['project_id']==project_id)
    daily = {r['date']:r for r in project['daily']}
    missing_net = False
    for row in live['rows']:
        if row['baseline_status'] in ('Успех','Успешный возврат'):
            continue
        day = datetime.fromisoformat(row['paid_at']).astimezone(MSK).date().isoformat()
        if not history['start'] <= day <= history['end']:
            continue
        if row['status'] != 'success' or row['conflict']:
            continue
        if day not in daily:
            daily[day]={'date':day,'count':0,'refund_count':0,'gross_rub':'0.00',
              'source_revenue_rub':'0.00','refund_amount_rub':'0.00','refund_source_revenue_rub':'0.00'}
        point=daily[day]
        point['count']+=1
        point['gross_rub']=_format_money(_money(point['gross_rub'])+row['gross'])
        if row['net'] is None:
            missing_net=True
            point['live_missing_net']=True
        else:
            point['source_revenue_rub']=_format_money(_money(point['source_revenue_rub'])+row['net'])
    project['daily']=sorted(daily.values(),key=lambda r:r['date'])
    project['count']=sum(r['count'] for r in project['daily'])
    project['gross_rub']=_format_money(sum(_money(r['gross_rub']) for r in project['daily']))
    project['source_revenue_rub']=None if missing_net else _format_money(sum(_money(r['source_revenue_rub']) for r in project['daily']))
    for point in project['daily']:
        if point.pop('live_missing_net',False):
            point['source_revenue_rub']=None
    history['live' if project_id=='pepel' else 'subscriptions_live']={k:v for k,v in live.items() if k!='rows'}
