"""Durable, read-only-provider payout accounting; never creates a withdrawal.

Callbacks merely enqueue identifiers. Only authenticated payoff/info responses
populate money/status. Allocation requires an independently verified snapshot
at the actual debit instant; callback and polling timestamps cannot substitute.
"""
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shop_bot.data_manager.finance_history import (
    _read_history_document,
    payout_operation_ref,
)
from shop_bot.data_manager.finance_ledger import (
    BalanceSnapshot,
    format_cents,
    money_to_cents,
    preview_payout_allocation,
)

STATUSES = {'created', 'pending', 'success', 'rejected'}
LEASE_SECONDS = 90
SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 key TEXT PRIMARY KEY,payoff_id TEXT,order_id TEXT,digest TEXT NOT NULL,
 generation INTEGER NOT NULL DEFAULT 1,verified_generation INTEGER NOT NULL DEFAULT 0,
 first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
 next_attempt TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,done INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT,lease_until TEXT,operation_ref TEXT,last_error TEXT);
CREATE TABLE IF NOT EXISTS payouts(
 operation_ref TEXT PRIMARY KEY,payoff_id TEXT UNIQUE NOT NULL,order_id TEXT,
 status TEXT NOT NULL,provider_status TEXT NOT NULL,debit INTEGER NOT NULL,
 fee INTEGER NOT NULL,received INTEGER NOT NULL,currency TEXT NOT NULL,service TEXT NOT NULL,
 first_seen TEXT NOT NULL,verified_at TEXT NOT NULL,provider_debit_at TEXT,
 conflict INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS allocations(
 operation_ref TEXT PRIMARY KEY,debit_at TEXT NOT NULL,evidence TEXT NOT NULL,
 allocation_json TEXT NOT NULL,allocation_sha256 TEXT NOT NULL,recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unrecognized(
 digest TEXT PRIMARY KEY,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL,
 resolved INTEGER NOT NULL DEFAULT 0);
'''


def _instant(value=None):
    value = datetime.now(UTC) if value is None else value
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('Timestamp requires timezone')
    return result.astimezone(UTC)


def _time(value=None):
    return _instant(value).isoformat(timespec='microseconds')


def _identifier(payoff_id=None, order_id=None):
    payoff = str(uuid.UUID(str(payoff_id))) if payoff_id else None
    order = str(order_id).strip() if order_id is not None else None
    if order is not None and (not order or len(order) > 128 or any(ord(c) < 32 for c in order)):
        raise ValueError('Invalid order identifier')
    if not payoff and not order:
        raise ValueError('Payout identifier required')
    return payoff, order


def journal_path(history_path):
    if not history_path:
        raise ValueError('Finance history required')
    return Path(history_path).with_name('payouts-live.sqlite3')


def _connect(path, *, create=False, readonly=False):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Journal must not be a symlink')
    if create:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    conn = sqlite3.connect(path.resolve().as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                           uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute('PRAGMA query_only=ON')
    elif create:
        conn.executescript(SCHEMA)
        os.chmod(path, 0o600)
    return conn


def initialize(history_path):
    """Create an empty journal without claiming receipt of any webhook."""
    path = journal_path(history_path)
    with closing(_connect(path,create=True)):
        pass
    return str(path)


def record_unrecognized(history_path, payload_digest, observed_at=None):
    """Quarantine an authentic but unsupported callback using its digest only."""
    if not isinstance(payload_digest,str) or not re.fullmatch('[0-9a-f]{64}',payload_digest):
        raise ValueError('Payload digest must be SHA256')
    stamp = _time(observed_at)
    with closing(_connect(journal_path(history_path),create=True)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('''INSERT INTO unrecognized(digest,first_seen,last_seen) VALUES(?,?,?)
          ON CONFLICT(digest) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen)''',
                     (payload_digest,stamp,stamp))
        conn.execute("INSERT INTO settings VALUES('last_webhook',?) ON CONFLICT(key) DO UPDATE "
                     "SET value=MAX(value,excluded.value)", (stamp,))


def enqueue(history_path, payoff_id=None, order_id=None, payload_digest=None, observed_at=None,
            *, source='webhook'):
    """Persist a hint for API verification; signed or unsigned callbacks supply no money."""
    payoff, order = _identifier(payoff_id, order_id)
    if source not in ('webhook','known_id'):
        raise ValueError('Invalid discovery source')
    if not isinstance(payload_digest, str) or not re.fullmatch('[0-9a-f]{64}', payload_digest):
        raise ValueError('Payload digest must be SHA256')
    stamp = _time(observed_at)
    key = ('p:' + payoff) if payoff else ('o:' + order)
    with closing(_connect(journal_path(history_path), create=True)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        if source == 'webhook':
            conn.execute("INSERT INTO settings VALUES('last_webhook',?) ON CONFLICT(key) DO UPDATE "
                         "SET value=MAX(value,excluded.value)", (stamp,))
        # A later supported replay moves this exact digest into API verification.
        conn.execute('UPDATE unrecognized SET resolved=1 WHERE digest=?', (payload_digest,))
        previous = conn.execute('SELECT * FROM jobs WHERE key=?', (key,)).fetchone()
        if previous:
            if previous['order_id'] and order and previous['order_id'] != order:
                raise ValueError('Conflicting payout identifiers')
            if previous['digest'] == payload_digest:
                return key
            conn.execute('''UPDATE jobs SET digest=?,generation=generation+1,last_seen=?,
                next_attempt=?,done=0,last_error=NULL,order_id=COALESCE(order_id,?) WHERE key=?''',
                         (payload_digest, stamp, stamp, order, key))
        else:
            conn.execute('''INSERT INTO jobs(key,payoff_id,order_id,digest,first_seen,last_seen,next_attempt)
                VALUES(?,?,?,?,?,?,?)''', (key,payoff,order,payload_digest,stamp,stamp,stamp))
    return key


def _claim(path, now):
    stamp = _time(now)
    with closing(_connect(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('''SELECT * FROM jobs WHERE done=0 AND next_attempt<=?
          AND (lease_until IS NULL OR lease_until<=?) ORDER BY next_attempt,key LIMIT 1''',
                           (stamp,stamp)).fetchone()
        if row is None:
            return None
        token = uuid.uuid4().hex
        conn.execute('UPDATE jobs SET lease_token=?,lease_until=? WHERE key=?',
                     (token,_time(now+timedelta(seconds=LEASE_SECONDS)),row['key']))
        return {**dict(row), 'lease_token': token}


def _normalized(data, job):
    if not isinstance(data, dict):
        raise TypeError('Invalid API data')
    payoff, order = _identifier(data.get('payoff_id'), data.get('order_id'))
    if not payoff or (job['payoff_id'] and payoff != job['payoff_id']):
        raise ValueError('Payout identifier mismatch')
    if job['order_id'] and order != job['order_id']:
        raise ValueError('Order identifier mismatch')
    status = data.get('status')
    if status not in STATUSES or data.get('provider_status') not in STATUSES:
        raise ValueError('Unknown payout status')
    if data.get('currency') != 'RUB':
        raise ValueError('Unsupported debit currency')
    amounts = [money_to_cents(data[name]) for name in ('debit_rub','fee_rub','received_rub')]
    if min(amounts) < 0 or amounts[0] <= 0 or max(amounts) > 10**14 or amounts[0] != sum(amounts[1:]):
        raise ValueError('Invalid payout amounts')
    service = data.get('service')
    if not isinstance(service, str) or not service.strip() or len(service)>128:
        raise ValueError('Invalid payout service')
    return {'operation_ref': payout_operation_ref(payoff), 'payoff_id': payoff, 'order_id': order,
            'status':status, 'provider_status':data['provider_status'], 'debit':amounts[0],
            'fee':amounts[1], 'received':amounts[2], 'currency':'RUB', 'service':service,
            'verified_at':_time(data['verified_at']),
            'provider_debit_at':_time(data['debit_at']) if data.get('debit_at') else None}


def _complete(path, job, data, now):
    values = _normalized(data, job)
    with closing(_connect(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        queued = conn.execute('SELECT * FROM jobs WHERE key=?', (job['key'],)).fetchone()
        if queued is None or queued['lease_token'] != job['lease_token']:
            return
        previous = conn.execute('SELECT * FROM payouts WHERE operation_ref=?',
                                (values['operation_ref'],)).fetchone()
        conflict = False
        if previous:
            conflict = any(previous[k] != values[k] for k in ('debit','fee','received','currency','service'))
            conflict |= bool(previous['order_id'] and values['order_id'] != previous['order_id'])
            conflict |= previous['status'] in ('success','rejected') and values['status'] != previous['status']
            conflict |= bool(previous['provider_debit_at'] and values['provider_debit_at']
                             and previous['provider_debit_at'] != values['provider_debit_at'])
        allocation = conn.execute('SELECT debit_at FROM allocations WHERE operation_ref=?',
                                  (values['operation_ref'],)).fetchone()
        if allocation and values['provider_debit_at']:
            conflict |= values['provider_debit_at'] != allocation['debit_at']
        if conflict:
            conn.execute('UPDATE payouts SET conflict=1 WHERE operation_ref=?', (values['operation_ref'],))
        elif previous:
            conn.execute('''UPDATE payouts SET status=?,provider_status=?,verified_at=?,
                provider_debit_at=COALESCE(provider_debit_at,?),order_id=COALESCE(order_id,?) WHERE operation_ref=?''',
                (values['status'],values['provider_status'],values['verified_at'],
                 values['provider_debit_at'],values['order_id'],values['operation_ref']))
        else:
            fields = tuple(values) + ('first_seen',)
            conn.execute('INSERT INTO payouts('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',
                         tuple(values.values()) + (job['first_seen'],))
        # A new callback during a network call must remain queued for verification.
        same_generation = queued['generation'] == job['generation']
        terminal = values['status'] in ('success','rejected') and not conflict and same_generation
        next_at = now+timedelta(seconds=60) if same_generation else now
        conn.execute('''UPDATE jobs SET operation_ref=?,done=?,attempts=0,last_error=?,next_attempt=?,verified_generation=?,
          lease_token=NULL,lease_until=NULL WHERE key=?''',
                     (values['operation_ref'],int(terminal),'conflict' if conflict else None,
                      _time(next_at),job['generation'],job['key']))


def _failed(path, job, error, now):
    code = getattr(error, 'code', None)
    code = code if isinstance(code,str) and re.fullmatch('[a-zA-Z0-9_]{1,64}',code) else 'verification_failed'
    with closing(_connect(path)) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM jobs WHERE key=?', (job['key'],)).fetchone()
        if row is None or row['lease_token'] != job['lease_token']:
            return
        attempts = min(row['attempts']+1,100)
        delay = min(3600,15*2**min(attempts-1,8))
        next_at = now if row['generation'] != job['generation'] else now+timedelta(seconds=delay)
        conn.execute('''UPDATE jobs SET attempts=?,last_error=?,next_attempt=?,lease_token=NULL,
          lease_until=NULL WHERE key=?''', (attempts,code,_time(next_at),job['key']))


def sync_once(history_path, *, profile_id=None, secret=None, fetcher=None, now=None, limit=10):
    """Verify queued operations using payoff/info only, with a cross-process lease."""
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError('Invalid sync batch limit')
    path = journal_path(history_path)
    if not path.exists():
        return {'checked':0,'failed':0,'configured':bool(profile_id and secret)}
    profile_id = profile_id or os.environ.get('LAVA_FINANCE_PROFILE_ID')
    secret = secret or os.environ.get('LAVA_FINANCE_PROFILE_SECRET_KEY')
    if not profile_id or not secret:
        return {'checked':0,'failed':0,'configured':False}
    if fetcher is None:
        from shop_bot.modules.lava_payout_api import fetch_info
        fetcher = fetch_info
    result = {'checked':0,'failed':0,'configured':True}
    for _ in range(limit):
        instant = _instant(now)
        job = _claim(path, instant)
        if job is None:
            break
        try:
            data = fetcher(profile_id,secret,payoff_id=job['payoff_id'],order_id=job['order_id'])
            _complete(path,job,data,_instant(now))
            result['checked'] += 1
        except (ValueError, TypeError, KeyError, sqlite3.Error, OSError) as error:
            _failed(path,job,error,_instant(now))
            result['failed'] += 1
    with closing(_connect(path)) as conn, conn:
        conn.execute("INSERT INTO settings VALUES('last_sync',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (_time(now),))
    return result


def store_allocation(history_path, operation_ref, snapshot, *, debit_at, evidence, now=None):
    """Freeze an evidenced allocation once. This does not initiate a payout.

    The caller must independently verify actual debit time and authoritative
    balances at that instant. Observed/callback timestamps are not evidence.
    """
    if not isinstance(snapshot, BalanceSnapshot) or not re.fullmatch('[0-9a-f]{64}',operation_ref):
        raise ValueError('Verified snapshot and payout reference required')
    stamp = _time(debit_at)
    if _time(snapshot.as_of) != stamp or _instant(stamp) > _instant(now):
        raise ValueError('Snapshot must match actual debit time')
    if not isinstance(evidence,str) or not evidence.strip() or len(evidence)>2048:
        raise ValueError('Verified debit evidence required')
    with closing(_connect(journal_path(history_path))) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM payouts WHERE operation_ref=?', (operation_ref,)).fetchone()
        if row is None or row['conflict'] or row['status'] not in ('created','pending','success'):
            raise ValueError('Payout is not verified for allocation')
        if row['provider_debit_at'] and row['provider_debit_at'] != stamp:
            raise ValueError('Provider debit time differs')
        allocation = preview_payout_allocation(snapshot,payout_amount=format_cents(row['received']),
                                               payout_fee=format_cents(row['fee'])).to_dict()
        encoded = json.dumps(allocation,sort_keys=True,separators=(',',':'))
        previous = conn.execute('SELECT * FROM allocations WHERE operation_ref=?', (operation_ref,)).fetchone()
        if previous:
            if previous['allocation_json'] != encoded or previous['debit_at'] != stamp or previous['evidence'] != evidence:
                raise ValueError('Allocation is immutable; reconciliation required')
            return allocation
        conn.execute('INSERT INTO allocations VALUES(?,?,?,?,?,?)',
                     (operation_ref,stamp,evidence,encoded,hashlib.sha256(encoded.encode()).hexdigest(),_time(now)))
    return allocation


def read_view(history_path, history=None):
    """Detached safe UI rows and allocation effects; legacy operations dedup once."""
    path = journal_path(history_path)
    empty = {'rows':[], 'unresolved':False, 'unresolved_count':0, 'unverified_count':0,'unsupported_count':0,
             'last_sync':None, 'last_webhook':None, 'allocations':[], 'configured':path.exists(),
             'applied_debits':{'pepel':0,'subscriptions':0}, 'accounted_refs':[], 'pending_count':0}
    if not path.exists():
        if path.with_name('payout-webhook-secret.json').exists():
            return {**empty,'configured':True,'unresolved':True,'unresolved_count':1,
                    'error':True,'reason':'Журнал выводов недоступен. Остатки требуют сверки.'}
        return empty
    # Period-filtered UI data must never hide old payout IDs or change balances.
    if Path(history_path).exists():
        history = _read_history_document(history_path)
    baseline = {r.get('operation_ref') for r in ((history or {}).get('payouts',{}).get('rows',[])
                 + (history or {}).get('payouts',{}).get('api_rows',[])) if r.get('operation_ref')}
    with closing(_connect(path,readonly=True)) as conn:
        conn.execute('BEGIN')
        settings = dict(conn.execute('SELECT key,value FROM settings'))
        payouts = list(conn.execute('''SELECT p.*,a.debit_at,a.allocation_json,a.allocation_sha256 FROM payouts p
                       LEFT JOIN allocations a USING(operation_ref) ORDER BY p.first_seen,p.operation_ref'''))
        jobs = list(conn.execute('SELECT * FROM jobs WHERE done=0'))
        unsupported = conn.execute('SELECT COUNT(*) FROM unrecognized WHERE resolved=0').fetchone()[0]
    rows, allocations, unresolved_refs, accounted_refs = [], [], set(), set()
    applied_debits = {'pepel':0,'subscriptions':0}
    pending_count = 0
    for row in payouts:
        ref = row['operation_ref']
        if (row['allocation_json'] and hashlib.sha256(row['allocation_json'].encode()).hexdigest()
                != row['allocation_sha256']):
            raise ValueError('Allocation fingerprint differs; reconciliation required')
        allocation = json.loads(row['allocation_json']) if row['allocation_json'] else None
        reason = ('Данные LAVA расходятся; требуется сверка.' if row['conflict'] else
                  'Вывод обрабатывается LAVA; его доли ещё не определены.'
                  if row['status'] in ('created','pending') and allocation is None else
                  'Нужны подтверждённые остатки обоих проектов на момент списания и время списания.'
                  if row['status']=='success' and allocation is None and ref not in baseline else None)
        if reason:
            unresolved_refs.add(ref)
        if row['status'] in ('created','pending'):
            pending_count += 1
        display_row = {'operation_ref':ref,'status':row['status'],'provider_status':row['provider_status'],
            'debit_rub':format_cents(row['debit']),'fee_rub':format_cents(row['fee']),
            'received_rub':format_cents(row['received']),'currency':row['currency'],'service':row['service'],
            'first_seen':row['first_seen'],'verified_at':row['verified_at'],'debit_at':row['debit_at'],
            'conflict':bool(row['conflict']),'allocation':allocation,
            'allocation_sha256':row['allocation_sha256'],'reason':reason}
        if ref not in baseline or row['conflict']:
            rows.append(display_row)
        if allocation and not row['conflict'] and row['status'] != 'rejected':
            allocations.append({'operation_ref':ref,'debit_at':row['debit_at'],
                'state':'settled' if row['status']=='success' else 'reserved',
                'total_debit_cents':allocation['total_debit_cents'],
                'projects':allocation['allocations']})
            for project in allocation['allocations']:
                applied_debits[project['project_id']] += project['debit_cents']
        if not row['conflict'] and (allocation or row['status']=='rejected'):
            accounted_refs.add(ref)
    unverified = 0
    for row in jobs:
        if not row['last_error'] and (row['operation_ref'] in baseline or
                (row['payoff_id'] and payout_operation_ref(row['payoff_id']) in baseline)):
            continue
        if (row['operation_ref'] in accounted_refs and not row['last_error']
                and row['generation'] == row['verified_generation']):
            continue
        # Normal polling of an already-verified pending payout is counted once.
        if row['operation_ref']:
            unresolved_refs.add(row['operation_ref'])
        else:
            unverified += 1
    return {**empty,'rows':rows,'unresolved':bool(unresolved_refs or unverified or unsupported),
            'unresolved_count':len(unresolved_refs)+unverified+unsupported,'unverified_count':unverified,
            'unsupported_count':unsupported,
            'last_sync':settings.get('last_sync'),'allocations':allocations,
            'last_webhook':settings.get('last_webhook'),
            'applied_debits':applied_debits,'accounted_refs':sorted(accounted_refs),
            'pending_count':pending_count}
