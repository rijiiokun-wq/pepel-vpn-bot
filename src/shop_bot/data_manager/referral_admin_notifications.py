"""Durable per-recipient reminders while a withdrawal awaits a decision."""
import asyncio
import logging
import re
import sqlite3
from contextlib import closing
from decimal import Decimal
from html import escape
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from shop_bot.data_manager import database

logger = logging.getLogger(__name__)
INTERVAL_SECONDS = 900
RECIPIENT_SETTING = 'referral_withdrawal_notify_ids'


def initialize_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS referral_admin_reminders (
        withdrawal_id INTEGER NOT NULL, recipient_id INTEGER NOT NULL,
        next_at INTEGER NOT NULL DEFAULT 0, lease_until INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT, sent_count INTEGER NOT NULL DEFAULT 0,
        last_sent_at TEXT, message_id TEXT, error TEXT, delivery_source TEXT,
        PRIMARY KEY (withdrawal_id, recipient_id)
    )''')


def recipients(conn):
    settings = dict(conn.execute(
        'SELECT key,value FROM bot_settings WHERE key IN (?,?)',
        (RECIPIENT_SETTING, 'admin_telegram_id'),
    ))
    value = settings.get(RECIPIENT_SETTING, settings.get('admin_telegram_id', '')) or ''
    parts = [part.strip() for part in str(value).split(',') if part.strip()]
    if not parts or any(not part.isdigit() or int(part) <= 0 for part in parts):
        return []  # Fail closed on malformed recipients.
    return sorted({int(part) for part in parts})


def claim(source, *, withdrawal_id=None, limit=10):
    if source not in {'main', 'reserve'}:
        raise ValueError('Unknown source')
    with closing(database._create_connection()) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        initialize_schema(conn)
        targets = recipients(conn)
        for recipient in targets:
            conn.execute('''INSERT OR IGNORE INTO referral_admin_reminders(withdrawal_id,recipient_id)
                SELECT id,? FROM referral_withdrawals WHERE status='pending' AND source=?''',
                         (recipient, source))
        rows = conn.execute('''SELECT r.*,w.user_id,w.amount_cents,w.details,w.source,u.username
            FROM referral_admin_reminders r JOIN referral_withdrawals w ON w.id=r.withdrawal_id
            LEFT JOIN users u ON u.telegram_id=w.user_id
            WHERE w.status='pending' AND w.source=? AND r.next_at<=unixepoch()
            AND r.lease_until<=unixepoch() AND (? IS NULL OR w.id=?)
            ORDER BY r.next_at,r.withdrawal_id,r.recipient_id''',
                            (source, withdrawal_id, withdrawal_id)).fetchall()
        claimed = []
        for row in rows:
            if row['recipient_id'] not in targets:
                continue
            token = uuid4().hex
            conn.execute('''UPDATE referral_admin_reminders SET lease_token=?,lease_until=unixepoch()+300
                WHERE withdrawal_id=? AND recipient_id=?''',
                         (token, row['withdrawal_id'], row['recipient_id']))
            claimed.append({**dict(row), 'lease_token': token})
            if len(claimed) >= min(10, max(1, limit)):
                break
        return claimed


def still_pending(row):
    with closing(database._create_connection()) as conn:
        return row['recipient_id'] in recipients(conn) and conn.execute('''
            SELECT 1 FROM referral_admin_reminders r JOIN referral_withdrawals w ON w.id=r.withdrawal_id
            WHERE r.withdrawal_id=? AND r.recipient_id=? AND r.lease_token=?
            AND r.lease_until>unixepoch() AND w.status='pending' ''',
            (row['withdrawal_id'], row['recipient_id'], row['lease_token'])).fetchone() is not None


def complete(row, *, message_id=None, error=None, delay=INTERVAL_SECONDS, delivery_source=None):
    with closing(database._create_connection()) as conn, conn:
        conn.execute('''UPDATE referral_admin_reminders SET
            lease_token=NULL,lease_until=0,next_at=unixepoch()+?,error=?,
            sent_count=sent_count+?,last_sent_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE last_sent_at END,
            message_id=COALESCE(?,message_id),delivery_source=COALESCE(?,delivery_source)
            WHERE withdrawal_id=? AND recipient_id=? AND lease_token=?''',
            (max(INTERVAL_SECONDS, int(delay)), error, int(error is None), int(error is None),
             str(message_id) if message_id is not None else None, delivery_source,
             row['withdrawal_id'], row['recipient_id'], row['lease_token']))


def reminder_text(row):
    amount = Decimal(row['amount_cents']) / 100
    origin = "@vpnpepelbot" if row["source"] == "main" else "@probelvpnbot"
    title = 'Напоминание: заявка на вывод' if row['sent_count'] else 'Заявка на вывод'
    username = str(row.get('username') or '').strip().lstrip('@')
    if username == f"user_{row['user_id']}" or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username):
        username_text = 'username не указан'
    else:
        username_text = '@' + username
    return (f"💸 <b>{title} №{row['withdrawal_id']}</b>\n"
            f"Источник заявки: {origin}\n"
            f"Пользователь: {username_text}\n"
            f"Пользователь ID: <code>{row['user_id']}</code>\n"
            f"Сумма: <b>{amount:.2f} RUB</b>\n"
            f"Реквизиты: <code>{escape(str(row['details']))}</code>\n\n"
            "Повтор каждые 15 минут до решения по заявке.\n"
            "Перевод выполняется вручную. Кнопки фиксируют решение по заявке.\n"
            "Решение также доступно в админке, раздел «Рефералы».")


def reminder_keyboard(row):
    request_id = row['withdrawal_id']
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='✅ вывод подтверждаю, деньги отправил',
                              callback_data=f'referral_withdraw_paid:{request_id}')],
        [InlineKeyboardButton(text='❌ отменить вывод, деньги не отправлял',
                              callback_data=f'referral_withdraw_rejected:{request_id}')],
    ])


async def send_with_fallback(bot, row):
    """A private-chat restriction in one bot must not hide a request from staff."""
    text = reminder_text(row)
    markup = reminder_keyboard(row)
    try:
        response = await bot.send_message(row['recipient_id'], text, parse_mode='HTML', reply_markup=markup)
        return response, row['source']
    except (TelegramBadRequest, TelegramForbiddenError):
        alternate = 'reserve' if row['source'] == 'main' else 'main'
        key = 'reserve_bot_token' if alternate == 'reserve' else 'telegram_bot_token'
        token = await asyncio.to_thread(database.get_setting, key)
        if not token or not await asyncio.to_thread(still_pending, row):
            raise
        async with Bot(token) as fallback_bot:
            response = await fallback_bot.send_message(row['recipient_id'], text, parse_mode='HTML', reply_markup=markup)
        return response, alternate


async def deliver(bot, *, source, withdrawal_id=None, limit=10):
    from shop_bot.data_manager.referral_notifications import _delivery_error

    stats = {'claimed': 0, 'sent': 0, 'failed': 0}
    if bot is None:
        return stats
    rows = await asyncio.to_thread(claim, source, withdrawal_id=withdrawal_id, limit=limit)
    stats['claimed'] = len(rows)
    for row in rows:
        # Recheck each row: a decision may arrive while another recipient is being sent.
        if not await asyncio.to_thread(still_pending, row):
            continue
        try:
            response, delivery_source = await asyncio.wait_for(
                send_with_fallback(bot, row), timeout=25,
            )
        except Exception as exc:  # noqa: BLE001 - isolate recipient failure and persist a safe retry.
            error, _ = _delivery_error(exc)
            await asyncio.to_thread(complete, row, error=error, delay=getattr(exc, 'retry_after', 900))
            stats['failed'] += 1
            logger.warning('Withdrawal reminder failed: request=%s recipient=%s source=%s',
                           row['withdrawal_id'], row['recipient_id'], source)
        else:
            await asyncio.to_thread(complete, row, message_id=response.message_id, delivery_source=delivery_source)
            stats['sent'] += 1
    return stats
