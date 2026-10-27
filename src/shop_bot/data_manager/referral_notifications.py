"""Durable Telegram delivery of withdrawal decisions, independent of referrals being enabled."""

import asyncio
import logging
from decimal import Decimal

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from shop_bot.config import BOT_SOURCE
from shop_bot.data_manager import database, referrals

logger = logging.getLogger(__name__)

SEND_TIMEOUT_SECONDS = 25
POLL_INTERVAL_SECONDS = 10


def withdrawal_notification_text(row: dict) -> str:
    amount = f"{Decimal(row['amount_cents']) / 100:.2f}".replace(".", ",")
    if row["action"] == "paid":
        text = f"✅ Заявка на вывод №{row['withdrawal_id']} отмечена администратором как выплаченная.\nСумма: {amount} ₽."
    else:
        text = f"❌ Заявка на вывод №{row['withdrawal_id']} отклонена администратором.\nСумма {amount} ₽ возвращена на ваш реферальный баланс."
    if row.get("note"):
        text += f"\n\nКомментарий администратора: {str(row['note'])[:2000]}"
    if row["action"] == "rejected":
        text += "\n\nЕсли у вас есть вопросы, напишите в техподдержку."
    return text


def withdrawal_notification_keyboard(row: dict, support_url: str | None = None) -> InlineKeyboardMarkup | None:
    if row["action"] != "rejected":
        return None
    support_url = (support_url or "").strip()
    contact = {"url": support_url} if support_url else {"callback_data": "show_help"}
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🆘 Написать в техподдержку", **contact),
    ]])


def _delivery_error(exc: Exception) -> tuple[str, bool]:
    """Store a safe diagnosis, never exception text (it can contain bot credentials)."""
    if isinstance(exc, TelegramForbiddenError):
        return "Клиент заблокировал бота или запретил сообщения. После снятия блокировки повторите отправку.", False
    if isinstance(exc, TelegramBadRequest):
        return "Telegram отклонил сообщение: чат недоступен или запрос некорректен. Проверьте получателя.", False
    if isinstance(exc, TelegramUnauthorizedError):
        return "Telegram не принял авторизацию бота. Требуется проверка настроек бота.", False
    if isinstance(exc, TelegramRetryAfter):
        return "Telegram ограничил частоту сообщений.", True
    if isinstance(exc, (TimeoutError, TelegramNetworkError)):
        return "Нет подтверждения отправки от Telegram.", True
    return "Временный сбой отправки уведомления.", True


async def deliver_pending_withdrawal_notifications(bot, *, source: str, limit: int = 10) -> dict:
    """Claim a runtime's outbox rows and acknowledge only successful Telegram responses.

    SQLite leases prevent concurrent workers sending the same row. A crash after the
    Telegram acknowledgement and before the SQLite acknowledgement may cause a repeat
    message; Telegram sendMessage has no idempotency key. No financial state changes here.
    """
    if source not in {"main", "reserve"}:
        raise ValueError("Неизвестный источник бота.")
    stats = {"claimed": 0, "sent": 0, "failed": 0}
    if bot is None:
        return stats
    # Ten sends at the per-send timeout remain below the storage's five-minute lease.
    rows = await asyncio.to_thread(referrals.claim_withdrawal_notifications, source, limit=min(10, max(1, limit)))
    stats["claimed"] = len(rows)
    for row in rows:
        try:
            support_url = await asyncio.to_thread(database.get_setting, "support_user") if row["action"] == "rejected" else None
            response = await asyncio.wait_for(
                bot.send_message(
                    row["user_id"],
                    withdrawal_notification_text(row),
                    parse_mode=None,
                    reply_markup=withdrawal_notification_keyboard(row, support_url),
                ),
                timeout=SEND_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - isolate a delivery; persist bounded retry without credentials.
            error, retryable = _delivery_error(exc)
            await asyncio.to_thread(
                referrals.complete_withdrawal_notification,
                row["id"],
                success=False,
                error=error,
                retryable=retryable,
                lease_token=row["lease_token"],
                retry_after_seconds=exc.retry_after if isinstance(exc, TelegramRetryAfter) else None,
            )
            stats["failed"] += 1
            logger.warning("Withdrawal notification attempt failed: id=%s source=%s retryable=%s", row["id"], source, retryable)
        else:
            await asyncio.to_thread(
                referrals.complete_withdrawal_notification,
                row["id"],
                success=True,
                message_id=response.message_id,
                lease_token=row["lease_token"],
            )
            stats["sent"] += 1
    return stats


async def periodic_withdrawal_notifications(bot_controller):
    """Immediate startup recovery and continuous delivery using this runtime's bot."""
    logger.info("Withdrawal notification worker started: source=%s", BOT_SOURCE)
    while True:
        try:
            await deliver_pending_withdrawal_notifications(bot_controller.get_bot_instance(), source=BOT_SOURCE)
        except Exception:  # noqa: BLE001 - keep pending durable rows recoverable after transient storage failures.
            logger.warning("Withdrawal notification worker could not complete its cycle; pending rows remain queued.")
        try:
            from shop_bot.data_manager.referral_admin_notifications import deliver

            await deliver(bot_controller.get_bot_instance(), source=BOT_SOURCE)
        except Exception:
            logger.warning("Withdrawal admin reminders could not complete cycle; pending requests remain recoverable.")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
