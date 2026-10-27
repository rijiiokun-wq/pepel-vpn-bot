import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.data_manager import database


logger = logging.getLogger(__name__)

MEMBER_STATUSES = {
    str(ChatMemberStatus.CREATOR),
    str(ChatMemberStatus.ADMINISTRATOR),
    str(ChatMemberStatus.MEMBER),
    "creator",
    "administrator",
    "member",
}


def _setup_logging() -> None:
    logging.basicConfig(
        level=str(os.environ.get("LOG_LEVEL") or "INFO").upper(),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def _status_value(value) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "unknown").lower()


def _setting(name: str, default: str = "") -> str:
    return str(os.environ.get(name) or database.get_setting(name.lower()) or default or "").strip()


def _bot_token() -> str:
    return _setting("PROXY_GATE_BOT_TOKEN") or str(database.get_setting("proxy_gate_bot_token") or "").strip()


def _required_chat() -> str:
    return _setting("PROXY_GATE_REQUIRED_CHAT", "@pepelproxy") or "@pepelproxy"


def _channel_url() -> str:
    return _setting("PROXY_GATE_CHANNEL_URL", "https://t.me/pepelproxy") or "https://t.me/pepelproxy"


def _target_bot_username() -> str:
    username = _setting("PROXY_GATE_TARGET_BOT_USERNAME")
    if not username:
        username = str(database.get_setting("reserve_bot_username") or "probelvpnbot").strip()
    return username.strip().lstrip("@") or "probelvpnbot"


def _target_url() -> str:
    return f"https://t.me/{_target_bot_username()}?start=proxy_gate"


def _record_user(
    user: types.User | None,
    *,
    event: str,
    status: str,
    allowed: bool,
    error: str | None = None,
) -> None:
    if not user or user.is_bot:
        return
    database.record_channel_gate_user(
        user_id=int(user.id),
        username=user.username,
        first_name=user.first_name,
        last_name=user.last_name,
        bot_source="proxy_gate",
        event=event,
        status=status,
        allowed=allowed,
        error=error,
    )


async def _check_membership(bot: Bot, user_id: int) -> tuple[bool, str, str | None]:
    _ = bot, user_id
    return True, "temporarily_disabled", None


def _keyboard(*, allow_target: bool) -> types.InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if not allow_target:
        builder.button(text="Подписаться на канал", url=_channel_url())
        builder.button(text="Проверить подписку", callback_data="proxy_gate_check")
    else:
        builder.button(text="Перейти к покупке", url=_target_url())
    builder.adjust(1)
    return builder.as_markup()


async def _send_gate_response(
    send: Callable[..., Awaitable[types.Message]],
    *,
    allowed: bool,
) -> None:
    if allowed:
        text = "Перейдите в основной бот для покупки и управления ключами."
    else:
        text = (
            "Для дальнейшей работы подпишитесь на наш канал @pepelproxy, "
            "чтобы получать важные новости по ситуации с блокировками и изменениями работы сервиса.\n\n"
            "После подписки нажмите «Проверить подписку»."
        )
    await send(text, reply_markup=_keyboard(allow_target=allowed), disable_web_page_preview=True)


router = Router()


@router.message(CommandStart())
async def start_handler(message: types.Message, bot: Bot) -> None:
    user = message.from_user
    allowed, status, error = await _check_membership(bot, int(user.id))
    await asyncio.to_thread(
        _record_user,
        user,
        event="/start",
        status=status,
        allowed=allowed,
        error=error,
    )
    await _send_gate_response(message.answer, allowed=allowed)


@router.message(F.chat.type == "private")
async def private_message_handler(message: types.Message, bot: Bot) -> None:
    user = message.from_user
    allowed, status, error = await _check_membership(bot, int(user.id))
    await asyncio.to_thread(
        _record_user,
        user,
        event=str(message.text or "message")[:80],
        status=status,
        allowed=allowed,
        error=error,
    )
    await _send_gate_response(message.answer, allowed=allowed)


@router.callback_query(F.data == "proxy_gate_check")
async def check_handler(callback: types.CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    allowed, status, error = await _check_membership(bot, int(user.id))
    await asyncio.to_thread(
        _record_user,
        user,
        event="proxy_gate_check",
        status=status,
        allowed=allowed,
        error=error,
    )
    try:
        await callback.answer("Доступ открыт." if allowed else "Пока не вижу подписку.", show_alert=not allowed)
    except TelegramAPIError:
        logger.debug("Proxy gate callback answer failed", exc_info=True)
    if callback.message and isinstance(callback.message, types.Message):
        try:
            await callback.message.edit_text(
                "Перейдите в основной бот для покупки и управления ключами."
                if allowed
                else (
                    "Для дальнейшей работы подпишитесь на наш канал @pepelproxy, "
                    "чтобы получать важные новости по ситуации с блокировками и изменениями работы сервиса.\n\n"
                    "После подписки нажмите «Проверить подписку»."
                ),
                reply_markup=_keyboard(allow_target=allowed),
                disable_web_page_preview=True,
            )
        except TelegramAPIError:
            logger.debug("Proxy gate callback edit failed", exc_info=True)


@router.chat_member()
async def chat_member_handler(event: types.ChatMemberUpdated) -> None:
    user = event.new_chat_member.user
    status = _status_value(event.new_chat_member.status)
    allowed = status in MEMBER_STATUSES
    await asyncio.to_thread(
        _record_user,
        user,
        event=f"chat_member:{getattr(event.chat, 'username', None) or event.chat.id}",
        status=status,
        allowed=allowed,
        error=None,
    )


async def _run() -> None:
    _setup_logging()
    database.initialize_db()
    token = _bot_token()
    if not token:
        raise RuntimeError("PROXY_GATE_BOT_TOKEN/proxy_gate_bot_token is empty")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    me = await bot.get_me()
    logger.info("Proxy gate bot started: @%s", me.username)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(
        bot,
        drop_pending_updates=False,
        allowed_updates=["message", "callback_query", "chat_member"],
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
