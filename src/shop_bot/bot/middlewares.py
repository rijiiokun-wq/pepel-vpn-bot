import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable

import aiohttp
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.config import BOT_SOURCE
from shop_bot.data_manager import referral_withdrawal_actions, referrals
from shop_bot.data_manager.database import (
    get_setting,
    get_user,
    has_channel_gate_pass,
    record_channel_gate_user,
)

logger = logging.getLogger(__name__)

_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_DEFAULT_MAIN_SERVICE_MESSAGE = (
    "Уважаемые пользователи, в данный момент покупка ключей доступна только "
    "в резервном боте — @probelvpnbot"
)
_DEFAULT_MAIN_SERVICE_RESERVE_BOT_URL = "https://t.me/probelvpnbot"
_DEFAULT_CALLBACK_DEBOUNCE_TTL_SECONDS = 120.0
_DEFAULT_CALLBACK_DEBOUNCE_MAX_ENTRIES = 4096
_CHANNEL_GATE_CHECK_CALLBACK = "channel_gate_check"
_CHANNEL_GATE_MEMBER_STATUSES = {"creator", "administrator", "member"}
_CHANNEL_GATE_POSITIVE_CACHE_TTL_SECONDS = 120.0
_CHANNEL_GATE_NEGATIVE_CACHE_TTL_SECONDS = 5.0
_CHANNEL_GATE_HTTP_TIMEOUT_SECONDS = 8.0
_DEFAULT_CHANNEL_GATE_MESSAGE = (
    "Для дальнейшей работы с ботом нужно пройти проверку подписки на наш канал.\n\n"
    "Нажмите кнопку ниже: там бот проверит подписку на @pepelproxy и вернёт вас сюда для покупки."
)


def _is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in _TRUTHY_VALUES


def _is_private_event(event: TelegramObject) -> bool:
    if isinstance(event, Message):
        return str(event.chat.type) == "private"
    if isinstance(event, CallbackQuery) and event.message and isinstance(event.message, Message):
        return str(event.message.chat.type) == "private"
    return False


async def _is_withdrawal_reviewer_callback(event: TelegramObject) -> bool:
    """Only authenticated private withdrawal decisions bypass customer gates."""
    if not isinstance(event, CallbackQuery) or not isinstance(event.message, Message):
        return False
    if (event.from_user.is_bot or not _is_private_event(event)
            or event.message.chat.id != event.from_user.id
            or referral_withdrawal_actions.parse_callback(event.data) is None):
        return False
    try:
        return await asyncio.to_thread(referral_withdrawal_actions.is_reviewer, event.from_user.id)
    except Exception:
        logger.warning("Withdrawal reviewer permission check failed", exc_info=True)
        return False


def _build_main_service_keyboard(reserve_bot_url: str | None):
    if not reserve_bot_url:
        return None
    builder = InlineKeyboardBuilder()
    builder.button(text="🔁 Перейти в бота", url=reserve_bot_url)
    builder.adjust(1)
    return builder.as_markup()


def _proxy_gate_bot_url() -> str:
    username = str(os.environ.get("PROXY_GATE_BOT_USERNAME") or "").strip().lstrip("@")
    if not username:
        username = "ppsstbot"
    return f"https://t.me/{username}?start=from_probel"


def _build_channel_gate_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Перейти к проверке", url=_proxy_gate_bot_url())
    builder.adjust(1)
    return builder.as_markup()


def _event_name(event: TelegramObject) -> str:
    if isinstance(event, CallbackQuery):
        return str(event.data or "callback")
    if isinstance(event, Message):
        return str(event.text or "message")[:80]
    return type(event).__name__


def _user_identity(user) -> dict[str, str | int | None]:
    return {
        "user_id": int(user.id),
        "username": getattr(user, "username", None),
        "first_name": getattr(user, "first_name", None),
        "last_name": getattr(user, "last_name", None),
    }


async def _load_main_service_mode_payload() -> tuple[bool, str, str]:
    enabled_raw = await asyncio.to_thread(get_setting, "main_service_mode_enabled")
    enabled = _is_truthy(enabled_raw)
    if not enabled:
        return False, "", ""

    service_message = str(os.environ.get("MAIN_SERVICE_MODE_MESSAGE") or "").strip()
    if not service_message:
        service_message = str(await asyncio.to_thread(get_setting, "main_service_mode_message") or "").strip()
    if not service_message:
        service_message = str(await asyncio.to_thread(get_setting, "maintenance_message") or "").strip()
    if not service_message:
        service_message = _DEFAULT_MAIN_SERVICE_MESSAGE

    reserve_bot_url = str(os.environ.get("MAIN_SERVICE_MODE_RESERVE_BOT_URL") or "").strip()
    if not reserve_bot_url:
        reserve_bot_url = str(await asyncio.to_thread(get_setting, "main_service_mode_reserve_bot_url") or "").strip()
    if not reserve_bot_url:
        reserve_bot_username = str(await asyncio.to_thread(get_setting, "reserve_bot_username") or "").strip().lstrip("@")
        if reserve_bot_username:
            reserve_bot_url = f"https://t.me/{reserve_bot_username}"
    if not reserve_bot_url:
        reserve_bot_url = _DEFAULT_MAIN_SERVICE_RESERVE_BOT_URL

    return True, service_message, reserve_bot_url


class ReferralAttributionMiddleware(BaseMiddleware):
    """Keep a valid invitation across service-mode and channel-gate redirects."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and _is_private_event(event) and event.from_user:
            match = re.fullmatch(r"/start(?:@[A-Za-z0-9_]+)?\s+ref_([1-9][0-9]{0,18})", event.text or "")
            if match and int(match.group(1)) <= 9223372036854775807:
                # Persist only attribution, never membership or an account. The
                # normal gates must still pass before registration can consume it.
                await asyncio.to_thread(referrals.capture_invite, event.from_user.id, int(match.group(1)))
        return await handler(event, data)


class MainServiceModeMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, object]], Awaitable[object]],
        event: TelegramObject,
        data: dict[str, object]
    ) -> object:
        if BOT_SOURCE != "main":
            return await handler(event, data)

        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)
        if not _is_private_event(event):
            return await handler(event, data)
        if await _is_withdrawal_reviewer_callback(event):
            return await handler(event, data)

        try:
            enabled, service_message, reserve_bot_url = await _load_main_service_mode_payload()
        except Exception:
            logger.warning("MainServiceModeMiddleware: settings check failed, allowing request", exc_info=True)
            return await handler(event, data)

        if not enabled:
            return await handler(event, data)

        keyboard = _build_main_service_keyboard(reserve_bot_url)
        try:
            if isinstance(event, CallbackQuery):
                try:
                    await event.answer("⚙️ Ведутся технические работы.")
                except Exception:
                    logger.debug("MainServiceModeMiddleware: callback answer failed", exc_info=True)
                if event.message and isinstance(event.message, Message):
                    await event.message.answer(
                        service_message,
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                return
            if isinstance(event, Message):
                await event.answer(
                    service_message,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
                return
        except Exception:
            logger.warning("MainServiceModeMiddleware: failed to send service message", exc_info=True)
            return

        return await handler(event, data)


class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, object]], Awaitable[object]],
        event: TelegramObject,
        data: dict[str, object]
    ) -> object:
        user = data.get('event_from_user')
        if not user:
            return await handler(event, data)

        try:
            user_data = await asyncio.to_thread(get_user, user.id)
        except Exception:
            logger.warning("BanMiddleware: get_user(%s) failed, allowing request", user.id, exc_info=True)
            return await handler(event, data)

        if user_data and user_data.get('is_banned'):
            ban_message_text = "Вы заблокированы и не можете использовать этого бота."
            if isinstance(event, CallbackQuery):
                await event.answer(ban_message_text, show_alert=True)
            elif isinstance(event, Message):
                await event.answer(ban_message_text)
            return

        return await handler(event, data)


class ChannelGateMiddleware(BaseMiddleware):
    def __init__(self) -> None:
        self._cache: dict[int, tuple[bool, str, float]] = {}
        self._lock = asyncio.Lock()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, object]], Awaitable[object]],
        event: TelegramObject,
        data: dict[str, object],
    ) -> object:
        if BOT_SOURCE != "reserve":
            return await handler(event, data)

        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)
        if not _is_private_event(event):
            return await handler(event, data)
        if await _is_withdrawal_reviewer_callback(event):
            return await handler(event, data)

        try:
            enabled = _is_truthy(await asyncio.to_thread(get_setting, "proxy_gate_enabled"))
        except Exception:
            logger.warning("ChannelGateMiddleware: settings check failed, blocking request", exc_info=True)
            return await self._block_event(event, status="settings_error")
        if not enabled:
            return await handler(event, data)

        force_refresh = isinstance(event, CallbackQuery) and event.data == _CHANNEL_GATE_CHECK_CALLBACK
        allowed, status, error = await self._check_user(user.id, force_refresh=force_refresh)
        if allowed:
            try:
                proxy_gate_passed = await asyncio.to_thread(has_channel_gate_pass, int(user.id))
            except Exception:
                logger.warning("ChannelGateMiddleware: proxy pass check failed for user_id=%s", user.id, exc_info=True)
                proxy_gate_passed = False
                status = "proxy_gate_error"
                error = "proxy_gate_pass_check_failed"
            if not proxy_gate_passed:
                allowed = False
                status = "proxy_gate_required"
        await self._record_user(user, event, status=status, allowed=allowed, error=error)

        if allowed:
            return await handler(event, data)

        return await self._block_event(event, status=status)

    async def _check_user(self, user_id: int, *, force_refresh: bool = False) -> tuple[bool, str, str | None]:
        now = time.monotonic()
        async with self._lock:
            cached = self._cache.get(int(user_id))
            if cached and not force_refresh:
                allowed, status, expires_at = cached
                if expires_at > now:
                    return allowed, status, None
                self._cache.pop(int(user_id), None)

        token = str(
            os.environ.get("PROXY_GATE_BOT_TOKEN")
            or await asyncio.to_thread(get_setting, "proxy_gate_bot_token")
            or ""
        ).strip()
        required_chat = str(
            os.environ.get("PROXY_GATE_REQUIRED_CHAT")
            or await asyncio.to_thread(get_setting, "proxy_gate_required_chat")
            or "@pepelproxy"
        ).strip()
        if not token or not required_chat:
            return False, "not_configured", "proxy_gate_not_configured"

        api_url = f"https://api.telegram.org/bot{token}/getChatMember"
        try:
            timeout = aiohttp.ClientTimeout(total=_CHANNEL_GATE_HTTP_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(api_url, data={"chat_id": required_chat, "user_id": str(user_id)}) as response:
                    payload = await response.json(content_type=None)
        except Exception as exc:
            logger.warning("ChannelGateMiddleware: getChatMember failed for user_id=%s: %s", user_id, exc)
            return False, "check_error", f"{type(exc).__name__}:{exc}"

        if not payload.get("ok"):
            description = str(payload.get("description") or payload)
            logger.warning("ChannelGateMiddleware: getChatMember rejected for user_id=%s: %s", user_id, description)
            return False, "api_error", description[:240]

        status = str(((payload.get("result") or {}).get("status")) or "unknown").lower()
        allowed = status in _CHANNEL_GATE_MEMBER_STATUSES
        ttl = _CHANNEL_GATE_POSITIVE_CACHE_TTL_SECONDS if allowed else _CHANNEL_GATE_NEGATIVE_CACHE_TTL_SECONDS
        async with self._lock:
            self._cache[int(user_id)] = (allowed, status, time.monotonic() + ttl)
        return allowed, status, None

    async def _record_user(
        self,
        user,
        event: TelegramObject,
        *,
        status: str,
        allowed: bool,
        error: str | None,
    ) -> None:
        try:
            identity = _user_identity(user)
            await asyncio.to_thread(
                record_channel_gate_user,
                **identity,
                bot_source=BOT_SOURCE,
                event=_event_name(event),
                status=status,
                allowed=allowed,
                error=error,
            )
        except Exception:
            logger.warning("ChannelGateMiddleware: failed to record user_id=%s", getattr(user, "id", None), exc_info=True)

    async def _block_event(self, event: TelegramObject, *, status: str) -> None:
        message = str(
            os.environ.get("PROXY_GATE_MESSAGE")
            or await asyncio.to_thread(get_setting, "proxy_gate_message")
            or _DEFAULT_CHANNEL_GATE_MESSAGE
        ).strip()
        keyboard = _build_channel_gate_keyboard()

        if isinstance(event, CallbackQuery):
            answer_text = (
                "Сначала перейдите к проверке подписки."
                if status in {"left", "kicked", "restricted", "unknown", "proxy_gate_required"}
                else "Не удалось проверить подписку. Попробуйте ещё раз чуть позже."
            )
            try:
                await event.answer(answer_text, show_alert=True)
            except TelegramAPIError:
                logger.debug("ChannelGateMiddleware: callback answer failed", exc_info=True)
            if event.message and isinstance(event.message, Message):
                try:
                    await event.message.answer(
                        message,
                        reply_markup=keyboard,
                        disable_web_page_preview=True,
                    )
                except TelegramAPIError:
                    logger.debug("ChannelGateMiddleware: callback gate message failed", exc_info=True)
            return

        if isinstance(event, Message):
            try:
                await event.answer(
                    message,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except TelegramAPIError:
                logger.debug("ChannelGateMiddleware: message gate response failed", exc_info=True)
            return


class CallbackDebounceMiddleware(BaseMiddleware):
    """Suppress duplicate in-flight callback presses from the same user/button."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_CALLBACK_DEBOUNCE_TTL_SECONDS,
        max_entries: int = _DEFAULT_CALLBACK_DEBOUNCE_MAX_ENTRIES,
    ) -> None:
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_entries = max(16, int(max_entries))
        self._active: dict[str, float] = {}
        self._lock = asyncio.Lock()

    def _callback_key(self, event: CallbackQuery) -> str:
        message = event.message if isinstance(event.message, Message) else None
        chat_id = getattr(getattr(message, "chat", None), "id", "inline")
        message_id = getattr(message, "message_id", event.inline_message_id or "inline")
        user_id = getattr(event.from_user, "id", "unknown")
        return f"{user_id}:{chat_id}:{message_id}:{event.data or ''}"

    async def _prune_expired_locked(self, now: float) -> None:
        expired = [key for key, expires_at in self._active.items() if expires_at <= now]
        for key in expired:
            self._active.pop(key, None)
        overflow = len(self._active) - self._max_entries
        if overflow > 0:
            for key in list(self._active.keys())[:overflow]:
                self._active.pop(key, None)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, object]], Awaitable[object]],
        event: TelegramObject,
        data: dict[str, object],
    ) -> object:
        if not isinstance(event, CallbackQuery):
            return await handler(event, data)

        key = self._callback_key(event)
        now = time.monotonic()
        async with self._lock:
            await self._prune_expired_locked(now)
            if key in self._active:
                try:
                    await event.answer("⏳ Уже обрабатываю, подождите.", cache_time=1)
                except TelegramAPIError:
                    logger.debug("CallbackDebounceMiddleware: duplicate callback answer failed", exc_info=True)
                return None
            self._active[key] = now + self._ttl_seconds

        try:
            return await handler(event, data)
        finally:
            async with self._lock:
                self._active.pop(key, None)
