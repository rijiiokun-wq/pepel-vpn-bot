import asyncio
import concurrent.futures
import logging
import os
import threading
import time
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramConflictError
from yookassa import Configuration

from shop_bot.bot import handlers, support_handlers
from shop_bot.bot.handlers import get_user_router
from shop_bot.bot.middlewares import (
    BanMiddleware,
    CallbackDebounceMiddleware,
    ChannelGateMiddleware,
    MainServiceModeMiddleware,
    ReferralAttributionMiddleware,
)
from shop_bot.bot.support_handlers import get_support_router
from shop_bot.config import BOT_SOURCE
from shop_bot.data_manager import database
from shop_bot.errors import BotError, classify_error, send_critical_alert

logger = logging.getLogger(__name__)

_STOP_WAIT_TIMEOUT_SECONDS = 10
_STARTUP_TELEGRAM_PROBE_TIMEOUT_SECONDS = 3.0
_SHOP_HEALTH_PROBE_TIMEOUT_SECONDS = max(
    2.0,
    float(str(os.environ.get("SHOP_HEALTH_PROBE_TIMEOUT_SECONDS", "6")).strip() or "6"),
)


class _LockedAttr:
    """Descriptor that wraps attribute access with the owning object's ``_lock``."""

    def __init__(self, attr: str):
        self._attr = attr

    def __set_name__(self, _owner, name):
        pass

    def __get__(self, obj, _objtype=None):
        if obj is None:
            return self
        with obj._lock:
            return getattr(obj, self._attr)

    def __set__(self, obj, value):
        with obj._lock:
            setattr(obj, self._attr, value)


class BotController:
    """
    Manages lifecycle of Shop and Support bots.

    Thread-safety: all mutable state is protected by ``_lock``.
    Flask routes (running in a separate thread) and the asyncio polling
    coroutine both mutate these fields, so every read/write must go
    through the lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._loop = None

        self._shop_bot: Bot | None = None
        self._shop_dp: Dispatcher | None = None
        self._shop_task = None
        self._shop_is_starting = False
        self._shop_is_running = False
        self._shop_is_stopping = False
        self._shop_desired_running = True
        self._shop_autorestart_paused = False
        self._shop_last_failure: str | None = None
        self._shop_last_failure_at: datetime | None = None
        self._shutting_down = False
        self._loop_thread_id: int | None = None
        self._shop_health_checked_monotonic = 0.0
        self._shop_health_checked_at: datetime | None = None
        self._shop_health_ok = False
        self._shop_health_message = "not_checked"

        self._support_bot: Bot | None = None
        self._support_dp: Dispatcher | None = None
        self._support_task = None
        self._support_is_running = False

    # ── Thread-safe property accessors (via _LockedAttr descriptor) ──
    shop_bot = _LockedAttr("_shop_bot")
    shop_dp = _LockedAttr("_shop_dp")
    shop_task = _LockedAttr("_shop_task")
    shop_is_running = _LockedAttr("_shop_is_running")
    shop_desired_running = _LockedAttr("_shop_desired_running")
    shop_autorestart_paused = _LockedAttr("_shop_autorestart_paused")
    shop_last_failure = _LockedAttr("_shop_last_failure")
    shop_last_failure_at = _LockedAttr("_shop_last_failure_at")
    support_bot = _LockedAttr("_support_bot")
    support_dp = _LockedAttr("_support_dp")
    support_task = _LockedAttr("_support_task")
    support_is_running = _LockedAttr("_support_is_running")

    @staticmethod
    def _is_retryable_shop_start_error(message: str) -> bool:
        normalized = (message or "").lower()
        retryable_markers = (
            "конфликт getupdates",
            "telegramconflicterror",
            "terminated by other getupdates request",
            "polling_conflict",
        )
        return any(marker in normalized for marker in retryable_markers)

    @staticmethod
    def _is_strict_shop_start_error(message: str) -> bool:
        if BotController._is_retryable_shop_start_error(message):
            return False
        normalized = (message or "").lower()
        strict_markers = (
            "критическая ошибка",
            "не все обязательные настройки",
            "проверьте токен",
            "invalid token",
            "unauthorized",
            "forbidden",
            "401",
            "403",
        )
        return any(marker in normalized for marker in strict_markers)

    @staticmethod
    def _resolve_shop_credentials_keys() -> tuple[str, str]:
        if BOT_SOURCE == "reserve":
            return "reserve_bot_token", "reserve_bot_username"
        return "telegram_bot_token", "telegram_bot_username"

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self._loop_thread_id = threading.get_ident()
        logger.info("BotController: Event loop has been set.")

    def get_bot_instance(self) -> Bot | None:
        return self.shop_bot


    async def _start_polling(self, bot, dp, name):
        logger.info("BotController: Polling task for '%s' has been started.", name)
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, drop_pending_updates=False, handle_signals=False)
        except asyncio.CancelledError:
            logger.info("BotController: Polling task for '%s' was cancelled.", name)
        except TelegramConflictError as e:
            logger.error("BotController: TelegramConflictError for '%s': %s", name, e)
            if name == "ShopBot":
                with self._lock:
                    self._shop_last_failure = "Конфликт getUpdates: найден второй polling-инстанс."
                    self._shop_last_failure_at = datetime.now()
                    self._shop_health_ok = False
                    self._shop_health_message = "polling_conflict"
                    self._shop_health_checked_at = datetime.now()
                    self._shop_health_checked_monotonic = time.monotonic()
                send_critical_alert(
                    BotError.BOT_CONFLICT,
                    details=f"Polling conflict detected: {e}",
                    respect_cooldown=False,
                )
            raise
        except Exception as e:
            logger.error("BotController: An error occurred during polling for '%s': %s", name, e, exc_info=True)
            if name == "ShopBot":
                with self._lock:
                    self._shop_last_failure = f"Краш во время работы: {e}"
                    self._shop_last_failure_at = datetime.now()
                    self._shop_health_ok = False
                    self._shop_health_message = f"polling_error:{type(e).__name__}"
                    self._shop_health_checked_at = datetime.now()
                    self._shop_health_checked_monotonic = time.monotonic()
                error = classify_error(str(e))
                send_critical_alert(
                    error if error != BotError.UNKNOWN else BotError.BOT_CRASH,
                    details=f"Polling crash: {e}",
                    respect_cooldown=False,
                )
            raise
        finally:
            logger.info("BotController: Polling for '%s' has gracefully stopped.", name)
            if bot:
                await bot.close()
            with self._lock:
                if name == "ShopBot":
                    self._shop_is_starting = False
                    self._shop_is_running = False
                    self._shop_is_stopping = False
                    self._shop_task = None
                    self._shop_bot = None
                    self._shop_dp = None
                    if self._shop_desired_running and self._is_retryable_shop_start_error(self._shop_last_failure or ""):
                        self._shop_autorestart_paused = False
                    self._shop_health_ok = False
                    self._shop_health_message = "polling_stopped"
                    self._shop_health_checked_at = datetime.now()
                    self._shop_health_checked_monotonic = time.monotonic()
                elif name == "SupportBot":
                    self._support_is_running = False
                    self._support_task = None
                    self._support_bot = None
                    self._support_dp = None

    def _validate_startup(self, task: concurrent.futures.Future, bot_name: str):
        """
        Detect immediate startup failures instead of returning false-positive success.
        If polling is still running after a short timeout, startup is considered successful.
        """
        try:
            task.result(timeout=0.5)
        except concurrent.futures.TimeoutError:
            # Normal case: polling coroutine is running.
            return None
        except Exception as e:
            logger.error("BotController: %s failed during startup: %s", bot_name, e, exc_info=True)
            return {"status": "error", "message": f"{bot_name} не удалось запустить: {e}"}

        return {"status": "error", "message": f"{bot_name} сразу завершился после запуска. Проверьте токен и логи."}

    async def _startup_telegram_probe(self, bot: Bot) -> None:
        """
        Lightweight probe before polling start.

        - get_me validates token/network quickly
        - get_updates(timeout=0) surfaces active getUpdates conflict early
        """
        await bot.get_me()
        await bot.get_updates(timeout=0, limit=1, allowed_updates=[])

    def _run_startup_telegram_probe(self, bot: Bot, *, ignore_conflict: bool = False):
        if not self._loop or not self._loop.is_running():
            return None
        if self._loop_thread_id == threading.get_ident():
            logger.debug("BotController: Startup Telegram probe skipped from event-loop thread.")
            return None
        try:
            probe = asyncio.run_coroutine_threadsafe(
                self._startup_telegram_probe(bot),
                self._loop,
            )
            probe.result(timeout=_STARTUP_TELEGRAM_PROBE_TIMEOUT_SECONDS)
            return None
        except concurrent.futures.TimeoutError:
            logger.warning("BotController: Startup Telegram probe timed out; continuing with polling start.")
            return None
        except TelegramConflictError as e:
            if ignore_conflict:
                logger.warning(
                    "BotController: Startup Telegram probe conflict ignored by request: %s",
                    e,
                )
                return None
            send_critical_alert(
                BotError.BOT_CONFLICT,
                details=f"Startup probe conflict: {e}",
                respect_cooldown=False,
            )
            return {
                "status": "error",
                "message": "Конфликт getUpdates: бот уже запущен в другом процессе.",
                "strict_reason": False,
            }
        except Exception as e:
            logger.warning("BotController: Startup Telegram probe failed (non-blocking): %s", e)
            return None

    def start_shop_bot(self, initiated_by: str = "manual"):
        logger.info("BotController: start_shop_bot requested by %s (BOT_SOURCE=%s).", initiated_by, BOT_SOURCE)

        with self._lock:
            self._shop_desired_running = True
            self._shop_autorestart_paused = False

            if self._shop_is_running:
                return {"status": "error", "message": "Бот уже запущен."}
            if self._shop_is_starting:
                return {"status": "error", "message": "Бот уже запускается."}

            need_wait_stop = self._shop_is_stopping

        # Wait for the previous stop to actually finish instead of returning
        # an error — prevents the race where stop→start leaves the bot dead.
        if need_wait_stop:
            logger.info("BotController: Waiting for previous stop to complete (up to %ss)...", _STOP_WAIT_TIMEOUT_SECONDS)
            deadline = time.monotonic() + _STOP_WAIT_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                time.sleep(0.25)
                with self._lock:
                    if not self._shop_is_stopping:
                        break
            with self._lock:
                if self._shop_is_stopping:
                    return {
                        "status": "error",
                        "message": f"Бот не остановился за {_STOP_WAIT_TIMEOUT_SECONDS} сек, попробуйте позже.",
                    }

        with self._lock:
            # Re-check after potential wait
            if self._shop_is_running:
                return {"status": "error", "message": "Бот уже запущен."}
            if self._shop_is_starting:
                return {"status": "error", "message": "Бот уже запускается."}

            if not self._loop or not self._loop.is_running():
                message = "Критическая ошибка: цикл событий не установлен."
                self._shop_last_failure = message
                self._shop_last_failure_at = datetime.now()
                return {"status": "error", "message": message, "strict_reason": True}
            self._shop_is_starting = True
            self._shop_health_ok = False
            self._shop_health_message = "starting"
            self._shop_health_checked_at = datetime.now(UTC)
            self._shop_health_checked_monotonic = time.monotonic()

        token_key, username_key = self._resolve_shop_credentials_keys()
        token = database.get_setting(token_key)
        bot_username = database.get_setting(username_key)
        admin_id = database.get_setting("admin_telegram_id")

        if not all([token, bot_username, admin_id]):
            message = (
                "Невозможно запустить: не все обязательные настройки Telegram заполнены "
                f"({token_key}, {username_key}, admin_telegram_id)."
            )
            with self._lock:
                self._shop_is_starting = False
                self._shop_last_failure = message
                self._shop_last_failure_at = datetime.now()
                self._shop_health_ok = False
                self._shop_health_message = "settings_missing"
                self._shop_health_checked_at = datetime.now(UTC)
                self._shop_health_checked_monotonic = time.monotonic()
            return {
                "status": "error",
                "message": message,
                "strict_reason": True,
            }

        try:
            bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            allow_manual_force_start = initiated_by in {"manual", "runtime-control"}
            probe_error = self._run_startup_telegram_probe(
                bot,
                ignore_conflict=allow_manual_force_start,
            )
            if probe_error:
                with self._lock:
                    self._shop_is_starting = False
                    self._shop_last_failure = probe_error.get("message")
                    self._shop_last_failure_at = datetime.now()
                    self._shop_health_ok = False
                    self._shop_health_message = "startup_probe_failed"
                    self._shop_health_checked_at = datetime.now(UTC)
                    self._shop_health_checked_monotonic = time.monotonic()
                return probe_error

            dp = Dispatcher()
            # Apply middlewares on message/callback layers so they receive
            # concrete Telegram events (Message/CallbackQuery), not raw Update.
            dp.message.middleware(ReferralAttributionMiddleware())
            dp.message.middleware(MainServiceModeMiddleware())
            dp.callback_query.middleware(MainServiceModeMiddleware())
            dp.callback_query.middleware(CallbackDebounceMiddleware())
            dp.message.middleware(BanMiddleware())
            dp.callback_query.middleware(BanMiddleware())
            dp.message.middleware(ChannelGateMiddleware())
            dp.callback_query.middleware(ChannelGateMiddleware())
            dp.include_router(get_user_router())

            yookassa_shop_id = database.get_setting("yookassa_shop_id")
            yookassa_secret_key = database.get_setting("yookassa_secret_key")
            main_payments_enabled = (
                str(database.get_setting("yookassa_payments_enabled") or "").strip().lower() == "true"
            )
            yookassa_enabled = bool(
                yookassa_shop_id
                and yookassa_secret_key
                and BOT_SOURCE == "main"
            )

            paypear_secret_key = (
                os.environ.get("PAYPEAR_SECRET_KEY")
                or os.environ.get("PAYPEAR_API_KEY")
                or ""
            ).strip() or (
                database.get_setting("paypear_secret_key")
                or database.get_setting("paypear_api_key")
            )
            paypear_shop_id = (
                os.environ.get("PAYPEAR_SHOP_ID")
                or database.get_setting("paypear_shop_id")
                or ""
            ).strip()
            paypear_enabled = bool(paypear_secret_key and paypear_shop_id)

            lava_shop_id = str(os.environ.get("LAVA_SHOP_ID") or "").strip()
            lava_secret_key = str(os.environ.get("LAVA_SECRET_KEY") or "").strip()
            lava_webhook_secret = str(
                os.environ.get("LAVA_WEBHOOK_SECRET")
                or os.environ.get("LAVA_ADDITIONAL_KEY")
                or ""
            ).strip()
            lava_checkout_enabled = str(
                os.environ.get("LAVA_CHECKOUT_ENABLED") or ""
            ).strip().lower() in {"1", "true", "yes", "on"}
            lava_enabled = bool(
                BOT_SOURCE == "reserve"
                and lava_checkout_enabled
                and lava_shop_id
                and lava_secret_key
                and lava_webhook_secret
            )
            if lava_enabled:
                # Keep PayPear webhook/reconciliation alive for old invoices,
                # but never offer it for newly created card/SBP payments.
                paypear_enabled = False

            cryptobot_token = database.get_setting("cryptobot_token")
            cryptobot_enabled = bool(cryptobot_token)

            heleket_shop_id = database.get_setting("heleket_merchant_id")
            heleket_api_key = database.get_setting("heleket_api_key")
            heleket_enabled = bool(heleket_api_key and heleket_shop_id)

            ton_wallet_address = database.get_setting("ton_wallet_address")
            tonapi_key = database.get_setting("tonapi_key")
            tonconnect_enabled = bool(ton_wallet_address and tonapi_key)

            if BOT_SOURCE == "main" and not main_payments_enabled:
                yookassa_enabled = False
                lava_enabled = False
                paypear_enabled = False
                cryptobot_enabled = False
                heleket_enabled = False
                tonconnect_enabled = False

            if yookassa_enabled:
                Configuration.account_id = yookassa_shop_id
                Configuration.secret_key = yookassa_secret_key

            handlers.PAYMENT_METHODS = {
                "yookassa": yookassa_enabled,
                "lava": lava_enabled,
                "paypear": paypear_enabled,
                "heleket": heleket_enabled,
                "cryptobot": cryptobot_enabled,
                "tonconnect": tonconnect_enabled
            }
            handlers.TELEGRAM_BOT_USERNAME = bot_username
            handlers.ADMIN_ID = admin_id
            logger.info(
                "BotController: payment matrix source=%s yookassa=%s lava=%s paypear=%s "
                "heleket=%s cryptobot=%s tonconnect=%s",
                BOT_SOURCE,
                yookassa_enabled,
                lava_enabled,
                paypear_enabled,
                heleket_enabled,
                cryptobot_enabled,
                tonconnect_enabled,
            )

            with self._lock:
                self._shop_bot = bot
                self._shop_dp = dp
                self._shop_is_starting = False
                self._shop_is_running = True
                self._shop_health_ok = True
                self._shop_health_message = "starting"
                self._shop_health_checked_at = datetime.now(UTC)
                self._shop_health_checked_monotonic = time.monotonic()

            task = asyncio.run_coroutine_threadsafe(
                self._start_polling(bot, dp, "ShopBot"), self._loop,
            )

            with self._lock:
                self._shop_task = task

            startup_error = self._validate_startup(task, "ShopBot")
            if startup_error:
                with self._lock:
                    self._shop_is_starting = False
                    self._shop_is_running = False
                    self._shop_is_stopping = False
                    self._shop_task = None
                    self._shop_bot = None
                    self._shop_dp = None
                    self._shop_health_ok = False
                    self._shop_health_message = "startup_failed"
                    self._shop_health_checked_at = datetime.now()
                    self._shop_health_checked_monotonic = time.monotonic()
                    self._shop_last_failure = startup_error.get("message")
                    self._shop_last_failure_at = datetime.now()
                startup_error["strict_reason"] = self._is_strict_shop_start_error(startup_error.get("message", ""))
                return startup_error

            with self._lock:
                self._shop_last_failure = None
                self._shop_last_failure_at = None
            logger.info("BotController: Start command sent to event loop.")
            return {"status": "success", "message": "Команда на запуск бота отправлена."}

        except Exception as e:
            logger.error("Failed to start bot: %s", e, exc_info=True)
            with self._lock:
                self._shop_bot = None
                self._shop_dp = None
                self._shop_is_starting = False
                self._shop_is_running = False
                self._shop_last_failure = f"Ошибка при запуске: {e}"
                self._shop_last_failure_at = datetime.now()
                self._shop_health_ok = False
                self._shop_health_message = f"startup_error:{type(e).__name__}"
                self._shop_health_checked_at = datetime.now(UTC)
                self._shop_health_checked_monotonic = time.monotonic()
            return {
                "status": "error",
                "message": self._shop_last_failure,
                "strict_reason": self._is_strict_shop_start_error(str(e)),
            }

    def start_support_bot(self):
        if BOT_SOURCE == "reserve":
            return {
                "status": "error",
                "message": "Support-бот отключен в режиме reserve.",
            }
        if database.get_setting("chatwoot_enabled") == "true":
            return {
                "status": "error",
                "message": "Support-бот не запущен: режим Chatwoot активен. "
                           "Токен бота используется Chatwoot. "
                           "Отключите Chatwoot в Настройках чтобы использовать форум-бот.",
            }

        with self._lock:
            if self._support_is_running:
                return {"status": "error", "message": "Бот-Саппорт уже запущен."}

        token = database.get_setting("support_bot_token")
        group_id = database.get_setting("support_group_id")

        if not token or not group_id:
            return {"status": "error", "message": "Токен для Бота-Саппорта и Айди группы не указаны."}

        try:
            bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            dp = Dispatcher()

            support_handlers.SUPPORT_GROUP_ID = int(group_id)
            support_handlers.user_bot = self.shop_bot

            support_router = get_support_router()
            dp.include_router(support_router)

            with self._lock:
                self._support_bot = bot
                self._support_dp = dp
                self._support_is_running = True

            task = asyncio.run_coroutine_threadsafe(
                self._start_polling(bot, dp, "SupportBot"), self._loop,
            )
            with self._lock:
                self._support_task = task

            startup_error = self._validate_startup(task, "SupportBot")
            if startup_error:
                return startup_error
            return {"status": "success", "message": "Команда на запуск бота отправлена."}
        except Exception as e:
            with self._lock:
                self._support_bot = None
                self._support_dp = None
                self._support_is_running = False
            logger.error("Failed to start Support Bot: %s", e, exc_info=True)
            return {"status": "error", "message": f"Error starting Support Bot: {e}"}

    def stop_shop_bot(self):
        with self._lock:
            if not self._shop_is_running and not self._shop_is_stopping:
                return {"status": "error", "message": "Бот не запущен."}

            if self._shop_is_stopping:
                return {"status": "error", "message": "Бот уже останавливается."}

            if not self._loop or not self._shop_dp:
                return {"status": "error", "message": "Критическая ошибка: компоненты бота недоступны."}

            # Keep running flag until polling actually exits in _start_polling.finally.
            # _shop_is_stopping prevents a new start during graceful shutdown.
            self._shop_is_stopping = True
            self._shop_desired_running = False
            self._shop_autorestart_paused = False
            self._shop_health_ok = False
            self._shop_health_message = "stopping"
            self._shop_health_checked_at = datetime.now()
            self._shop_health_checked_monotonic = time.monotonic()
            dp = self._shop_dp

        logger.info("BotController: Sending graceful stop signal...")
        asyncio.run_coroutine_threadsafe(dp.stop_polling(), self._loop)

        return {"status": "success", "message": "Команда на остановку бота отправлена."}

    def stop_support_bot(self):
        with self._lock:
            if not self._support_is_running:
                return {"status": "error", "message": "Бот не запущен."}

            if not self._loop or not self._support_dp:
                return {"status": "error", "message": "Критическая ошибка: компоненты бота недоступны."}

            self._support_is_running = False
            dp = self._support_dp

        logger.info("BotController: Sending graceful stop signal...")
        asyncio.run_coroutine_threadsafe(dp.stop_polling(), self._loop)

        return {"status": "success", "message": "Команда на остановку бота отправлена."}

    async def _probe_shop_runtime(self, bot: Bot) -> None:
        # Lightweight Telegram API probe to confirm polling runtime responsiveness.
        await bot.get_me()

    def _set_shop_health(self, healthy: bool, message: str) -> None:
        with self._lock:
            self._shop_health_ok = healthy
            self._shop_health_message = message
            self._shop_health_checked_at = datetime.now()
            self._shop_health_checked_monotonic = time.monotonic()

    def _resolve_shop_health(self) -> tuple[bool, str]:
        cache_ttl_seconds = 8.0
        probe_timeout_seconds = _SHOP_HEALTH_PROBE_TIMEOUT_SECONDS
        now_monotonic = time.monotonic()

        with self._lock:
            shop_running = self._shop_is_running
            shop_starting = self._shop_is_starting
            loop = self._loop
            loop_thread_id = self._loop_thread_id
            bot = self._shop_bot
            task = self._shop_task
            last_checked_mono = self._shop_health_checked_monotonic
            cached_ok = self._shop_health_ok
            cached_message = self._shop_health_message

        if not shop_running:
            if shop_starting:
                return cached_ok, cached_message
            self._set_shop_health(False, "stopped")
            return False, "stopped"

        # Trust task completion over cached flags: if polling future is already done,
        # runtime is not alive even if _shop_is_running was stale.
        if isinstance(task, concurrent.futures.Future) and task.done():
            task_message = "polling_task_done"
            try:
                task.result()
            except Exception as e:
                task_message = f"polling_task_failed:{type(e).__name__}"
            with self._lock:
                self._shop_is_running = False
                self._shop_is_stopping = False
                self._shop_task = None
                self._shop_bot = None
                self._shop_dp = None
            self._set_shop_health(False, task_message)
            return False, task_message

        if now_monotonic - last_checked_mono < cache_ttl_seconds:
            return cached_ok, cached_message

        if not loop or not loop.is_running() or not bot:
            self._set_shop_health(False, "runtime_components_unavailable")
            return False, "runtime_components_unavailable"

        # Avoid deadlock if called from the same thread as asyncio event loop.
        if loop_thread_id == threading.get_ident():
            return cached_ok, cached_message

        try:
            probe_future = asyncio.run_coroutine_threadsafe(
                self._probe_shop_runtime(bot), loop
            )
            probe_future.result(timeout=probe_timeout_seconds)
        except concurrent.futures.TimeoutError:
            self._set_shop_health(False, "probe_timeout")
            return False, "probe_timeout"
        except Exception as e:
            self._set_shop_health(False, f"probe_error:{type(e).__name__}")
            return False, f"probe_error:{type(e).__name__}"

        self._set_shop_health(True, "ok")
        return True, "ok"

    def get_status(self, *, include_health: bool = False):
        with self._lock:
            # If polling task already finished, treat runtime as stopped regardless of cached flags.
            if isinstance(self._shop_task, concurrent.futures.Future) and self._shop_task.done():
                self._shop_is_starting = False
                self._shop_is_running = False
                self._shop_is_stopping = False
                self._shop_bot = None
                self._shop_dp = None
                self._shop_task = None
            status = {
                "shop_bot_running": self._shop_is_running,
                "shop_bot_starting": self._shop_is_starting,
                "shop_bot_desired_running": self._shop_desired_running,
                "shop_autorestart_paused": self._shop_autorestart_paused,
                "shop_last_failure": self._shop_last_failure,
                "shop_last_failure_at": self._shop_last_failure_at.isoformat() if self._shop_last_failure_at else None,
                "shop_bot_healthy": self._shop_health_ok,
                "shop_bot_health_message": self._shop_health_message,
                "shop_bot_health_checked_at": self._shop_health_checked_at.isoformat() if self._shop_health_checked_at else None,
                "support_bot_running": self._support_is_running,
                "chatwoot_enabled": database.get_setting("chatwoot_enabled") == "true",
            }
        if include_health:
            healthy, health_message = self._resolve_shop_health()
            status["shop_bot_healthy"] = healthy
            status["shop_bot_health_message"] = health_message
            with self._lock:
                status["shop_bot_health_checked_at"] = (
                    self._shop_health_checked_at.isoformat() if self._shop_health_checked_at else None
                )
        return status
