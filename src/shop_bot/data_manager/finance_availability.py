"""Estimated PePel availability from verified export data and a full-withdrawal anchor.

This never writes balances, performs payouts, or merges the bot payment journal.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from shop_bot.data_manager.finance_history import (
    FinanceHistoryError, _money, _read_bounded, _read_history_document,
    _read_transactions, _digest, _no_duplicate_json_keys,
)

MSK = ZoneInfo('Europe/Moscow')


def instant(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Timestamp requires timezone')
    return parsed.astimezone(timezone.utc)


def money_label(cents):
    return f'{cents // 100:,}'.replace(',', ' ') + f',{cents % 100:02d} ₽'


def source_path(history_path):
    return Path(history_path).with_name('pepel-availability.json')


def build_source(csv_path, policy, *, exported_at):
    """Rebuild an identifier-free schedule; policy is explicitly verified, not guessed."""
    project, _ = _read_transactions(csv_path, 'pepel')
    import csv
    import io
    raw = _read_bounded(csv_path, 16 * 1024 * 1024)
    rows = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')))
    anchor = instant(policy['anchor']['at'])
    hold = timedelta(seconds=policy['hold_seconds'])
    payments = []
    for row in rows:
        if row['Статус'] != 'Успех':
            continue
        paid = datetime.fromisoformat(row['Дата платежа']).replace(tzinfo=MSK)
        if paid + hold > anchor:
            payments.append({'paid_at': paid.isoformat(), 'net_kopecks': _money(row['Выручка'])})
    result = {key: policy[key] for key in ('hold_seconds', 'terms_verified_at', 'anchor')}
    result.update(schema_version=1, project_id='pepel', source_sha256=project['source_sha256'],
                  exported_at=exported_at, payments=sorted(payments, key=lambda row: row['paid_at']))
    validate_source(result)
    return result


def validate_source(data):
    if set(data) != {'schema_version', 'project_id', 'source_sha256', 'exported_at',
                     'hold_seconds', 'terms_verified_at', 'anchor', 'payments'}:
        raise ValueError('Unexpected availability schema')
    if type(data['schema_version']) is not int or data['schema_version'] != 1 or data['project_id'] != 'pepel':
        raise ValueError('Unsupported availability schema')
    if type(data['hold_seconds']) is not int or data['hold_seconds'] != 86400:
        raise ValueError('Unverified hold rule')
    _digest(data['source_sha256'])
    exported = instant(data['exported_at'])
    instant(data['terms_verified_at'])
    anchor = data['anchor']
    if set(anchor) != {'at', 'operation_ref', 'shared_debit_rub', 'baseline_refund_count'}:
        raise ValueError('Invalid anchor')
    at = instant(anchor['at'])
    _digest(anchor['operation_ref'])
    _money(anchor['shared_debit_rub'], positive=True)
    if type(anchor['baseline_refund_count']) is not int or anchor['baseline_refund_count'] < 0:
        raise ValueError('Invalid refund baseline')
    if not isinstance(data['payments'], list) or len(data['payments']) > 100000:
        raise ValueError('Invalid payment schedule')
    for row in data['payments']:
        if set(row) != {'paid_at', 'net_kopecks'} or type(row['net_kopecks']) is not int or row['net_kopecks'] < 0:
            raise ValueError('Invalid payment record')
        paid = instant(row['paid_at'])
        if paid > exported or paid + timedelta(seconds=86400) <= at:
            raise ValueError('Payment outside source coverage')


def calculate(history, data, *, now=None, profile=None, live=None, payout_live=None):
    validate_source(data)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Current time requires timezone')
    now = now.astimezone(timezone.utc)
    anchor = data['anchor']
    anchor_at = instant(anchor['at'])
    if now < anchor_at:
        raise ValueError('Calculation precedes anchor')
    project = next(p for p in history['projects'] if p['project_id'] == 'pepel')
    if project['source_sha256'] != data['source_sha256']:
        raise ValueError('Schedule and export differ')
    if project['status_counts'].get('Успешный возврат', 0) != anchor['baseline_refund_count']:
        return unavailable('В истории появились возвраты. Остаток требует сверки.')
    payout_live = payout_live or {}
    if payout_live.get('unresolved') or payout_live.get('error'):
        return unavailable('Получен новый вывод или расхождение по выплате. Пока списание не проверено и не распределено между проектами, доступный остаток требует сверки.')
    accounted_refs = set(payout_live.get('accounted_refs', []))
    payouts = history['payouts']
    anchor_row = next((row for row in payouts.get('api_rows', []) + payouts['rows']
                       if row['operation_ref'] == anchor['operation_ref']), None)
    if (anchor_row is None or anchor_row['status'] != 'success'
            or ('amount_pay_rub' in anchor_row and anchor_row['amount_pay_rub'] != anchor['shared_debit_rub'])):
        return unavailable('Нужно подтвердить исходный вывод для расчёта остатка.')
    for row in payouts['rows'] + payouts.get('api_rows', []):
        sent = datetime.fromisoformat(row['date']).replace(tzinfo=MSK)
        if (sent >= anchor_at and row.get('operation_ref') != anchor['operation_ref']
                and row.get('operation_ref') not in accounted_refs):
            return unavailable('После исходного вывода появился новый вывод. Нужно распределить его между проектами.')
    available = frozen = 0
    upcoming = []
    payments = list(data['payments'])
    if live is not None:
        if live['requires_reconciliation']:
            return unavailable('Есть возврат или расхождение данных новой оплаты. Остаток требует сверки.')
        payments += [{'paid_at':r['paid_at'], 'net_kopecks':r['net']} for r in live['rows']
                     if r['net'] is not None and r['baseline_status'] not in ('Успех','Успешный возврат')
                     and instant(r['paid_at']) + timedelta(seconds=data['hold_seconds']) > anchor_at]
    for row in payments:
        paid = instant(row['paid_at'])
        if paid > now:
            continue
        release = paid + timedelta(seconds=data['hold_seconds'])
        if release <= now:
            available += row['net_kopecks']
        else:
            frozen += row['net_kopecks']
            upcoming.append((release, row['net_kopecks']))
    debit = 0
    # Replay against the same maturity schedule used for today's balance.
    # A late/backfilled receipt must not silently rewrite a frozen allocation.
    for allocation in sorted(payout_live.get('allocations', []), key=lambda a: instant(a['debit_at'])):
        debit_at = instant(allocation['debit_at'])
        if allocation['operation_ref'] == anchor['operation_ref'] or debit_at <= anchor_at:
            continue
        if debit_at > now:
            return unavailable('Время списания находится в будущем. Требуется сверка.')
        item = next(p for p in allocation['projects'] if p['project_id'] == 'pepel')
        before = sum(r['net_kopecks'] for r in payments
                     if instant(r['paid_at']) + timedelta(seconds=data['hold_seconds']) <= debit_at) - debit
        if before != item['available_before_cents']:
            return unavailable('История оплат изменилась относительно распределённого вывода. Остаток требует сверки.')
        amount = item['debit_cents']
        if type(amount) is not int or amount < 0 or amount > before:
            return unavailable('Распределённое списание не сходится с остатком Пепла. Требуется сверка.')
        debit += amount
    if debit > available:
        return unavailable('Распределённые выводы превышают остаток Пепла. Требуется сверка.')
    available -= debit
    profile = profile or {}
    if profile.get('status') == 'ok' and not profile.get('stale') and profile.get('active_balance') is not None:
        if available > _money(str(profile['active_balance'])):
            return unavailable('Расчёт превышает доступный баланс LAVA. Проверьте новые выводы и удержания.')
    exported = instant(data['exported_at'])
    overdue = now >= exported + timedelta(seconds=data['hold_seconds'])
    if live is not None:
        overdue = live['stale'] or bool(live['pending_net'])
    next_at = min((time for time, _ in upcoming), default=None)
    next_sum = sum(amount for time, amount in upcoming if time == next_at)
    result = {'status': 'partial' if overdue else 'estimated', 'available_rub': f'{available // 100}.{available % 100:02d}',
            'available_label': money_label(available), 'frozen_label': money_label(frozen),
            'checked_at_label': now.astimezone(MSK).strftime('%d.%m.%Y %H:%M:%S МСК'),
            'exported_at_label': exported.astimezone(MSK).strftime('%d.%m.%Y %H:%M МСК'),
            'next_release_label': next_at.astimezone(MSK).strftime('%d.%m.%Y %H:%M:%S МСК') if next_at else None,
            'next_release_amount_label': money_label(next_sum) if next_at else None,
            'notice': ('Выгрузка старше суток: новые оплаты уже могли разморозиться. Показана только загруженная история.'
                       if overdue else 'Новые оплаты появятся после обновления выгрузки. Расчёт предполагает неизменный суточный холд и отсутствие дополнительных удержаний.')}
    if live is not None:
        result['live_enabled'] = True
        result['sync_label'] = datetime.fromisoformat(live['last_sync']).astimezone(MSK).strftime('%d.%m.%Y %H:%M:%S МСК') if live['last_sync'] else 'Ожидается'
        result['notice'] = 'Новые оплаты учитываются автоматически. Суточный холд отсчитывается от подтверждённого времени; если часовой пояс LAVA не указан — от получения уведомления.'
        if live['pending_net']:
            result['notice'] += f" У {live['pending_net']} оплат нет точной суммы зачисления: они видны в доходах, но не включены в этот неполный остаток."
        if live['stale']:
            result['notice'] += ' Сверка с ботом задерживается; показаны последние сохранённые данные.'
    return result


def unavailable(reason):
    return {'status': 'unavailable', 'available_rub': None, 'available_label': 'Требует сверки', 'notice': reason}


def get_pepel_availability(history_path, *, profile=None, now=None):
    try:
        history = _read_history_document(history_path)
        data = json.loads(_read_bounded(source_path(history_path), 8 * 1024 * 1024), object_pairs_hook=_no_duplicate_json_keys)
        from shop_bot.data_manager.finance_live import read_live
        live = read_live(history_path, history, now=now)
        from shop_bot.data_manager.finance_payouts import read_view
        payout_live = read_view(history_path, history=history)
        return calculate(history, data, now=now, profile=profile, live=live, payout_live=payout_live)
    except (OSError, ValueError, TypeError, KeyError, StopIteration, sqlite3.Error, FinanceHistoryError):
        return unavailable('Расчёт пока недоступен: обновите и сверьте историю Пепла.')
