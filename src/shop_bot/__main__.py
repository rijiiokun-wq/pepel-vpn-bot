import asyncio
import logging
import os
import signal
import socket
import threading

from shop_bot.logging_config import setup_logging

# Configure logging before any other shop_bot imports (so all logs use our format)
setup_logging()

from shop_bot import runtime_guard
from shop_bot.bot import handlers
from shop_bot.bot_controller import BotController
from shop_bot.config import BOT_SOURCE
from shop_bot.data_manager import database
from shop_bot.data_manager.referral_notifications import periodic_withdrawal_notifications
from shop_bot.data_manager.scheduler import (
    periodic_delivery_sla_check,
    periodic_host_health_check,
    periodic_mainpanel_inbound_discovery,
    periodic_payment_issuance_autoretry,
    periodic_payment_issuance_daily_catchup,
    periodic_shop_watchdog,
    periodic_subscription_check,
    periodic_whitelist_limit_check,
)
from shop_bot.errors import BotError, send_critical_alert
from shop_bot.webhook_server.app import create_webhook_app

_PROCESS_SHUTDOWN_ALERT_TIMEOUT_SECONDS = 6.0
_PAYMENT_SHUTDOWN_DRAIN_TIMEOUT_SECONDS = 120.0


def _env_flag(name: str, *, default: bool = True) -> bool:
    raw = str(os.environ.get(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_positive_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = str(os.environ.get(name, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _is_reserve_runtime() -> bool:
    return str(BOT_SOURCE).strip().lower() == "reserve"


def _should_start_host_health_monitor() -> bool:
    if not _env_flag("ENABLE_HOST_HEALTH_MONITOR", default=True):
        return False
    return not (_is_reserve_runtime() and not _env_flag("ENABLE_RESERVE_HOST_HEALTH_MONITOR", default=False))


def _should_emit_process_shutdown_alert(status: dict[str, object]) -> bool:
    return bool(
        status.get("shop_bot_running") or status.get("shop_bot_desired_running") or status.get("support_bot_running")
    )


def _build_process_shutdown_alert_details(sig: signal.Signals, status: dict[str, object]) -> str:
    shop_running = int(bool(status.get("shop_bot_running")))
    support_running = int(bool(status.get("support_bot_running")))
    desired_running = int(bool(status.get("shop_bot_desired_running")))
    runtime_flags = f"shop_running={shop_running} support_running={support_running} desired_running={desired_running}"
    return (
        f"source={BOT_SOURCE} reason=process_signal_shutdown signal={sig.name} "
        f"pid={os.getpid()} server={socket.gethostname()} "
        f"{runtime_flags} "
        "\nПроцесс бота получил сигнал остановки от ОС/контейнера. "
        "Если это не ручной деплой, проверьте reboot/shutdown сервера и journalctl/docker logs."
    )


async def _send_process_shutdown_alert(
    sig: signal.Signals,
    status: dict[str, object],
    logger: logging.Logger,
) -> bool:
    if not _env_flag("ENABLE_PROCESS_SHUTDOWN_ALERT", default=True):
        return False
    if not _should_emit_process_shutdown_alert(status):
        return False

    timeout = _env_positive_float(
        "PROCESS_SHUTDOWN_ALERT_TIMEOUT_SECONDS",
        _PROCESS_SHUTDOWN_ALERT_TIMEOUT_SECONDS,
    )
    loop = asyncio.get_running_loop()
    done = asyncio.Event()
    result: dict[str, bool] = {"sent": False}

    def _send() -> None:
        try:
            result["sent"] = bool(
                send_critical_alert(
                    BotError.BOT_PROCESS_STOPPING,
                    details=_build_process_shutdown_alert_details(sig, status),
                    respect_cooldown=False,
                )
            )
        except Exception:
            logger.exception("Process shutdown alert failed")
        finally:
            loop.call_soon_threadsafe(done.set)

    threading.Thread(target=_send, name="process-shutdown-alert", daemon=True).start()

    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except TimeoutError:
        logger.warning("Process shutdown alert did not finish within %.1fs.", timeout)
        return False
    return result["sent"]


def main():
    logger = logging.getLogger(__name__)

    database.initialize_db()
    logger.info("Database initialization check complete.")
    previous_runtime_marker = runtime_guard.mark_runtime_started(BOT_SOURCE)
    if runtime_guard.alert_on_unclean_previous_exit(previous_runtime_marker, source=BOT_SOURCE):
        logger.warning("Runtime guard reported an unclean previous process exit.")

    bot_controller = BotController()
    flask_app = create_webhook_app(bot_controller)
    shutdown_started = False

    async def shutdown(sig: signal.Signals, loop: asyncio.AbstractEventLoop):
        nonlocal shutdown_started
        if shutdown_started:
            logger.info("Received signal %s while shutdown is already in progress.", sig.name)
            return
        shutdown_started = True
        logger.info("Received signal: %s. Shutting down...", sig.name)
        runtime_guard.mark_runtime_shutdown_requested(BOT_SOURCE, signal_name=sig.name)
        bot_controller._shutting_down = True
        handlers.begin_payment_shutdown()
        bot_controller.shop_desired_running = False
        status = bot_controller.get_status() or {}
        await _send_process_shutdown_alert(sig, status, logger)
        remaining_payments = await handlers.drain_active_payments(
            _env_positive_float(
                "PAYMENT_SHUTDOWN_DRAIN_TIMEOUT_SECONDS",
                _PAYMENT_SHUTDOWN_DRAIN_TIMEOUT_SECONDS,
            )
        )
        if remaining_payments:
            logger.warning(
                "Payment shutdown drain timed out: %s payment(s) retain their lease for safe recovery.",
                remaining_payments,
            )
        # Keep the bot session open until payments have saved and delivered their keys.
        if status.get("shop_bot_running"):
            bot_controller.stop_shop_bot()
        if status.get("support_bot_running"):
            bot_controller.stop_support_bot()
        # Give polling tasks a moment to exit gracefully before cancelling everything.
        if status.get("shop_bot_running") or status.get("support_bot_running"):
            await asyncio.sleep(2)
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if tasks:
            [task.cancel() for task in tasks]
            await asyncio.gather(*tasks, return_exceptions=True)
        runtime_guard.mark_runtime_graceful_shutdown(BOT_SOURCE, signal_name=sig.name)
        loop.stop()

    async def start_services():
        loop = asyncio.get_running_loop()
        bot_controller.set_loop(loop)
        flask_app.config["EVENT_LOOP"] = loop
        background_tasks: set[asyncio.Task] = set()

        def _start_background_task(coro, *, name: str) -> asyncio.Task:
            task = asyncio.create_task(coro, name=name)
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)
            return task

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda sig=sig: asyncio.create_task(shutdown(sig, loop)))

        flask_thread = threading.Thread(
            target=lambda: flask_app.run(host="0.0.0.0", port=8080, use_reloader=False, debug=False), daemon=True
        )
        flask_thread.start()

        logger.info("Flask server started in a background thread on http://0.0.0.0:8080")

        # Start runtime bot immediately; watchdog remains a safety net for recovery.
        startup_result = bot_controller.start_shop_bot(initiated_by="startup")
        if startup_result.get("status") == "success":
            logger.info("Startup: shop bot start command sent successfully.")
        else:
            message = startup_result.get("message", "unknown startup error")
            strict_reason = bool(startup_result.get("strict_reason"))
            if strict_reason:
                logger.error("Startup: shop bot auto-start failed (strict): %s", message)
            else:
                logger.warning("Startup: shop bot auto-start failed: %s", message)

        logger.info("Application is running. Bot can be controlled from the web panel.")

        # Withdrawal decisions must still be delivered when new referral activity is disabled.
        _start_background_task(periodic_withdrawal_notifications(bot_controller), name="withdrawal_notifications")
        finance_history = os.environ.get('LAVA_FINANCE_HISTORY_FILE', '').strip()
        if _is_reserve_runtime() and finance_history:
            from shop_bot.data_manager.finance_live import periodic_sync
            _start_background_task(periodic_sync(database.DB_FILE, finance_history), name='finance_sync')

        if runtime_guard.env_flag("ENABLE_RUNTIME_GUARD", default=True):
            _start_background_task(runtime_guard.runtime_guard_loop(BOT_SOURCE), name="runtime_guard")
        else:
            logger.warning("Runtime guard disabled by ENABLE_RUNTIME_GUARD=0")

        if _should_start_host_health_monitor():
            _start_background_task(periodic_host_health_check(bot_controller), name="host_health_check")
        elif _is_reserve_runtime() and _env_flag("ENABLE_HOST_HEALTH_MONITOR", default=True):
            logger.warning("Host health monitor disabled in reserve runtime by ENABLE_RESERVE_HOST_HEALTH_MONITOR=0")
        else:
            logger.warning("Host health monitor disabled by ENABLE_HOST_HEALTH_MONITOR=0")

        if _env_flag("ENABLE_SUBSCRIPTION_SCHEDULER", default=True):
            _start_background_task(periodic_subscription_check(bot_controller), name="subscription_check")
        else:
            logger.warning("Subscription scheduler disabled by ENABLE_SUBSCRIPTION_SCHEDULER=0")

        if _is_reserve_runtime() and _env_flag("MAINPANEL_INBOUND_DISCOVERY_ENABLED", default=False):
            logger.warning("MainPanel inbound discovery disabled in reserve runtime.")
        elif _env_flag("MAINPANEL_INBOUND_DISCOVERY_ENABLED", default=False):
            _start_background_task(
                periodic_mainpanel_inbound_discovery(bot_controller),
                name="mainpanel_inbound_discovery",
            )
        else:
            logger.warning("MainPanel inbound discovery disabled by MAINPANEL_INBOUND_DISCOVERY_ENABLED=0")

        backend_retry_worker_enabled = _env_flag(
            "ISSUANCE_BACKEND_RETRY_WORKER_ENABLED",
            default=False,
        )
        if backend_retry_worker_enabled:
            logger.warning("Payment issuance auto-retry owned by issuance-backend; Python autoretry disabled.")
        elif _env_flag("ENABLE_PAYMENT_ISSUANCE_AUTORETRY", default=True):
            _start_background_task(
                periodic_payment_issuance_autoretry(bot_controller),
                name="payment_issuance_autoretry",
            )
        else:
            logger.warning("Payment issuance auto-retry disabled by ENABLE_PAYMENT_ISSUANCE_AUTORETRY=0")

        if backend_retry_worker_enabled:
            logger.warning("Payment issuance daily catch-up owned by issuance-backend; Python catch-up disabled.")
        elif _env_flag("ENABLE_PAYMENT_ISSUANCE_DAILY_CATCHUP", default=True):
            _start_background_task(
                periodic_payment_issuance_daily_catchup(bot_controller),
                name="payment_issuance_daily_catchup",
            )
        else:
            logger.warning("Payment issuance daily catch-up disabled by ENABLE_PAYMENT_ISSUANCE_DAILY_CATCHUP=0")

        if _env_flag("ENABLE_SHOP_WATCHDOG", default=True):
            _start_background_task(periodic_shop_watchdog(bot_controller), name="shop_watchdog")
        else:
            logger.warning("Shop watchdog disabled by ENABLE_SHOP_WATCHDOG=0")

        if _env_flag("ENABLE_WHITELIST_LIMIT_MONITOR", default=True):
            _start_background_task(periodic_whitelist_limit_check(bot_controller), name="whitelist_limit_check")
        else:
            logger.warning("WHITELIST limit monitor disabled by ENABLE_WHITELIST_LIMIT_MONITOR=0")

        if _env_flag("ENABLE_DELIVERY_SLA_CHECK", default=True):
            _start_background_task(periodic_delivery_sla_check(), name="delivery_sla_check")
        else:
            logger.warning("Delivery SLA check disabled by ENABLE_DELIVERY_SLA_CHECK=0")

        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            # Expected during shutdown.
            return

    try:
        asyncio.run(start_services())
    finally:
        logger.info("Application is shutting down.")


if __name__ == "__main__":
    main()
