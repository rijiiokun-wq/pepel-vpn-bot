"""
Centralised error definitions for PEPEL BOT.

Each error has:
- a machine-readable code (enum member name)
- a severity level (CRITICAL / WARNING / INFO)
- a human-readable Russian description for Telegram alerts
- a short technical tag for logs

Usage:
    from shop_bot.errors import BotError, send_critical_alert

    send_critical_alert(BotError.HOST_UNREACHABLE, details="host=de-1, timeout 10s")
"""

from __future__ import annotations

import enum
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

try:
    _MOSCOW_TZ = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    _MOSCOW_TZ = timezone(timedelta(hours=3))


_ALERT_DISPATCH_QUEUE_MAXSIZE = max(
    10,
    int(str(os.environ.get("ALERT_DISPATCH_QUEUE_MAXSIZE", "100")).strip() or "100"),
)
_alert_dispatch_queue: queue.Queue[tuple[BotError, str, bool]] | None = None
_alert_dispatch_worker: threading.Thread | None = None
_alert_dispatch_lock = threading.Lock()
_alert_thread_local = threading.local()
_alerts_dropped_count = 0
_alerts_dropped_lock = threading.Lock()
_PAYMENT_ALERT_THROTTLE_DB = os.environ.get(
    "PAYMENT_ALERT_THROTTLE_DB",
    os.path.join("runtime", "payment_alert_throttle.sqlite"),
)
_PAYMENT_ALERT_DAILY_AFTER = max(
    1,
    int(str(os.environ.get("PAYMENT_ALERT_DAILY_AFTER", "5")).strip() or "5"),
)
_PAYMENT_ALERT_DAILY_INTERVAL_SECONDS = max(
    3600,
    int(str(os.environ.get("PAYMENT_ALERT_DAILY_INTERVAL_SECONDS", "86400")).strip() or "86400"),
)
_PAYMENT_ID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


class Severity(enum.StrEnum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"
    SUCCESS = "SUCCESS"


class BotError(enum.Enum):
    """
    Central error catalogue.

    Tuple format: (severity, short_tag, human_description_ru)
    """

    # ── Bot lifecycle ──────────────────────────────────────────────
    BOT_CRASH = (
        Severity.CRITICAL,
        "bot_crash",
        "Shop-бот неожиданно упал. Watchdog попытается перезапустить автоматически.",
    )
    BOT_START_FAILED = (
        Severity.CRITICAL,
        "bot_start_failed",
        "Не удалось запустить Shop-бота. Проверьте настройки в веб-панели.",
    )
    BOT_TOKEN_INVALID = (
        Severity.CRITICAL,
        "bot_token_invalid",
        "Токен Telegram-бота недействителен или заблокирован. Watchdog продолжит попытки запуска после исправления токена.",
    )
    BOT_SETTINGS_MISSING = (
        Severity.CRITICAL,
        "bot_settings_missing",
        "Не заполнены обязательные настройки (токен, username или ID админа). Бот не может запуститься.",
    )
    BOT_CONFLICT = (
        Severity.CRITICAL,
        "bot_conflict",
        "Конфликт: бот уже запущен в другом процессе (TelegramConflictError). Остановите дубликат.",
    )
    BOT_MANUAL_STOP = (
        Severity.INFO,
        "bot_manual_stop",
        "Shop-бот остановлен вручную из веб-панели.",
    )
    BOT_MANUAL_START = (
        Severity.INFO,
        "bot_manual_start",
        "Shop-бот запущен вручную из веб-панели.",
    )
    BOT_AUTO_RESTART = (
        Severity.SUCCESS,
        "bot_auto_restart",
        "Shop-бот восстановлен автоматически watchdog.",
    )
    BOT_DEPLOY_RESTARTED = (
        Severity.SUCCESS,
        "bot_deploy_restarted",
        "Деплой завершён: Shop-бот перезапущен и работает в штатном режиме.",
    )
    BOT_PROCESS_STOPPING = (
        Severity.CRITICAL,
        "bot_process_stopping",
        "Процесс Shop-бота получает сигнал остановки. Возможна перезагрузка сервера, "
        "остановка контейнера или рестарт сервиса.",
    )
    BOT_PROCESS_UNCLEAN_EXIT = (
        Severity.CRITICAL,
        "bot_process_unclean_exit",
        "Предыдущий процесс Shop-бота завершился без штатного shutdown. Возможен OOM kill, SIGKILL или crash контейнера.",
    )
    BOT_MEMORY_PRESSURE_WARNING = (
        Severity.WARNING,
        "bot_memory_pressure_warning",
        "Память процесса Shop-бота приближается к лимиту контейнера.",
    )
    BOT_MEMORY_PRESSURE_CRITICAL = (
        Severity.CRITICAL,
        "bot_memory_pressure_critical",
        "Память процесса Shop-бота почти исчерпана. Тяжёлые фоновые операции временно пропускаются.",
    )
    EVENT_LOOP_DEAD = (
        Severity.CRITICAL,
        "event_loop_dead",
        "Цикл событий (event loop) не работает. Приложение требует полного перезапуска.",
    )

    # ── Host / VPN panel ───────────────────────────────────────────
    HOST_UNREACHABLE = (
        Severity.CRITICAL,
        "host_unreachable",
        "VPN-сервер недоступен: не удалось подключиться к панели управления. Новые ключи на этом сервере не выдаются.",
    )
    HOST_LOGIN_FAILED = (
        Severity.CRITICAL,
        "host_login_failed",
        "Не удалось авторизоваться на VPN-панели. Проверьте логин/пароль хоста.",
    )
    HOST_INBOUND_MISSING = (
        Severity.CRITICAL,
        "host_inbound_missing",
        "Inbound не найден на VPN-панели. Проверьте Inbound ID в настройках хоста.",
    )
    HOST_SYNC_FAILED = (
        Severity.WARNING,
        "host_sync_failed",
        "Ошибка синхронизации ключей с VPN-сервером. Некоторые ключи могут быть неактуальны.",
    )
    HOST_ALL_DOWN = (
        Severity.CRITICAL,
        "host_all_down",
        "Все VPN-серверы недоступны! Продажа ключей невозможна.",
    )
    HOST_RECOVERED = (
        Severity.SUCCESS,
        "host_recovered",
        "VPN-сервер снова доступен. Выдача ключей восстановлена.",
    )
    TRIAL_ISSUE_RETRYING = (
        Severity.WARNING,
        "trial_issue_retrying",
        "Выдача пробного ключа временно отложена. Пользователь может повторить попытку.",
    )
    TRIAL_ISSUE_PENDING_VERIFICATION = (
        Severity.WARNING,
        "trial_issue_pending_verification",
        "Выдача триала ушла в неопределённое состояние. Бот проверяет панель перед ответом пользователю.",
    )
    TRIAL_ISSUE_RECOVERED = (
        Severity.SUCCESS,
        "trial_issue_recovered",
        "Пробный ключ успешно выдан после временной ошибки.",
    )
    TRIAL_ISSUE_FAILED = (
        Severity.CRITICAL,
        "trial_issue_failed",
        "Не удалось выдать триал: пользователь нажал получение, но ключ не был выдан.",
    )
    TRIAL_RETRY_PROMPT_SHOWN_AGAIN = (
        Severity.WARNING,
        "trial_retry_prompt_shown_again",
        "Пользователю повторно показана кнопка «Попробовать ещё раз» для триала.",
    )
    TRIAL_ISSUE_CONSUMED = (
        Severity.SUCCESS,
        "trial_issue_consumed",
        "Пользователь успешно воспользовался триалом.",
    )

    # ── Blitz (Hysteria2) ─────────────────────────────────────────
    BLITZ_UNREACHABLE = (
        Severity.CRITICAL,
        "blitz_unreachable",
        "Blitz-сервер (Hysteria2) недоступен. Ключи Hysteria2 не выдаются.",
    )
    BLITZ_AUTH_FAILED = (
        Severity.CRITICAL,
        "blitz_auth_failed",
        "Ошибка авторизации на Blitz-панели. Проверьте API-токен или логин/пароль.",
    )
    BLITZ_SYNC_FAILED = (
        Severity.WARNING,
        "blitz_sync_failed",
        "Ошибка синхронизации Blitz-ключей. Даты истечения могут быть неактуальны.",
    )

    # ── Payments ──────────────────────────────────────────────────
    PAYMENT_YOOKASSA_FAILED = (
        Severity.CRITICAL,
        "payment_yookassa",
        "Ошибка платежной системы ЮKassa. Оплата через ЮKassa временно недоступна.",
    )
    PAYMENT_CRYPTOBOT_FAILED = (
        Severity.CRITICAL,
        "payment_cryptobot",
        "Ошибка CryptoBot. Оплата криптой временно недоступна.",
    )
    PAYMENT_HELEKET_FAILED = (
        Severity.CRITICAL,
        "payment_heleket",
        "Ошибка Heleket. Оплата через Heleket временно недоступна.",
    )
    PAYMENT_TON_FAILED = (
        Severity.CRITICAL,
        "payment_ton",
        "Ошибка TON Connect. Оплата через TON временно недоступна.",
    )
    PAYMENT_PLATEGA_FAILED = (
        Severity.CRITICAL,
        "payment_platega",
        "Ошибка Platega. Оплата картой в резервном боте временно недоступна.",
    )
    PAYMENT_PAYPEAR_FAILED = (
        Severity.CRITICAL,
        "payment_paypear",
        "Ошибка PayPear. Оплата временно недоступна.",
    )
    PAYMENT_PAYPEAR_CREATE_FAILED = (
        Severity.WARNING,
        "payment_paypear_create",
        "PayPear не создал ссылку на оплату. Деньги не списаны, клиенту предложено повторить попытку "
        "или выбрать другой способ.",
    )
    PAYMENT_LAVA_FAILED = (
        Severity.CRITICAL,
        "payment_lava",
        "Ошибка LAVA. Оплата картой и через СБП временно недоступна.",
    )
    PAYMENT_LAVA_CREATE_FAILED = (
        Severity.WARNING,
        "payment_lava_create",
        "LAVA не создала ссылку на оплату. Деньги не списаны, клиенту предложено повторить попытку.",
    )
    PAYMENT_RECONCILE_FAILED = (
        Severity.WARNING,
        "payment_reconcile",
        "Ошибка сверки платежей. Некоторые оплаты могут не обработаться автоматически.",
    )
    PAYMENT_KEY_ISSUE_FAILED = (
        Severity.CRITICAL,
        "payment_key_issue",
        "Платёж получен, но ключ не выдан! Клиент заплатил, но не получил доступ. Требуется ручное вмешательство.",
    )
    PAYMENT_KEY_ISSUE_RETRYING = (
        Severity.WARNING,
        "payment_key_issue_retrying",
        "Платёж подтверждён, но выдача ключа временно отложена. Система повторит автоматически.",
    )
    PAYMENT_ISSUE_RECOVERED = (
        Severity.SUCCESS,
        "payment_issue_recovered",
        "Ключ успешно довыдан после временного сбоя. Клиент получил доступ.",
    )

    # ── Database ──────────────────────────────────────────────────
    DATABASE_ERROR = (
        Severity.CRITICAL,
        "database_error",
        "Ошибка базы данных. Часть функций может не работать.",
    )
    DATABASE_LOCKED = (
        Severity.WARNING,
        "database_locked",
        "База данных заблокирована (SQLite lock). Операции записи задерживаются.",
    )

    # ── Scheduler / background tasks ─────────────────────────────
    SCHEDULER_CRASH = (
        Severity.CRITICAL,
        "scheduler_crash",
        "Фоновый планировщик задач упал. Уведомления, синхронизация и сверка платежей остановлены.",
    )
    WATCHDOG_ERROR = (
        Severity.WARNING,
        "watchdog_error",
        "Ошибка в watchdog-цикле. Автоматический мониторинг может быть нарушен.",
    )

    # ── Network / infrastructure ─────────────────────────────────
    TELEGRAM_API_ERROR = (
        Severity.WARNING,
        "telegram_api",
        "Ошибка Telegram API. Уведомления пользователям могут не доставляться.",
    )
    SSL_CERTIFICATE_ERROR = (
        Severity.CRITICAL,
        "ssl_cert_error",
        "Ошибка SSL-сертификата при подключении к внешнему сервису.",
    )
    DNS_RESOLUTION_FAILED = (
        Severity.CRITICAL,
        "dns_failed",
        "Не удалось разрешить DNS-имя. Проверьте сетевое подключение сервера.",
    )

    # ── Web panel ────────────────────────────────────────────────
    WEB_PANEL_ERROR = (
        Severity.WARNING,
        "web_panel_error",
        "Ошибка веб-панели управления.",
    )

    # ── Generic ──────────────────────────────────────────────────
    UNKNOWN = (
        Severity.CRITICAL,
        "unknown",
        "Неизвестная критическая ошибка. Проверьте логи сервера.",
    )

    # ── Helpers ───────────────────────────────────────────────────

    @property
    def severity(self) -> Severity:
        return self.value[0]

    @property
    def tag(self) -> str:
        return self.value[1]

    @property
    def description(self) -> str:
        return self.value[2]

    @property
    def emoji(self) -> str:
        return {
            Severity.CRITICAL: "\U0001f6a8",   # 🚨
            Severity.WARNING: "\u26a0\ufe0f",   # ⚠️
            Severity.INFO: "\u2139\ufe0f",       # ℹ️
            Severity.SUCCESS: "\U0001f7e2",      # 🟢
        }[self.severity]

    def format_alert(self, details: str = "") -> str:
        """Build a human-friendly Telegram message."""
        title = _ALERT_TITLES.get(self, self.tag.replace("_", " ").strip().title())
        severity_text = _SEVERITY_LABELS.get(self.severity, self.severity.value)
        parsed, plain_tail = _parse_alert_details(details)

        lines = [
            f"{self.emoji} {severity_text}: {title}",
            "",
            self.description,
        ]

        detail_lines = _format_alert_details(parsed, plain_tail)
        if detail_lines:
            lines.append("")
            lines.append("Детали:")
            lines.extend(detail_lines)

        action_hint = _ALERT_ACTION_HINTS.get(self)
        if action_hint:
            lines.append("")
            lines.append(f"Что сделать: {action_hint}")

        lines.append("")
        lines.append(f"Код: {self.tag}")
        lines.append(f"Время: {datetime.now(_MOSCOW_TZ).strftime('%d.%m.%Y %H:%M:%S')}")
        return "\n".join(lines)


_SEVERITY_LABELS: dict[Severity, str] = {
    Severity.CRITICAL: "Критично",
    Severity.WARNING: "Предупреждение",
    Severity.INFO: "Информация",
    Severity.SUCCESS: "Восстановлено",
}

_ALERT_TITLES: dict[BotError, str] = {
    BotError.BOT_CRASH: "Падение Shop-бота",
    BotError.BOT_START_FAILED: "Сбой запуска Shop-бота",
    BotError.BOT_TOKEN_INVALID: "Некорректный токен бота",
    BotError.BOT_SETTINGS_MISSING: "Неполные настройки бота",
    BotError.BOT_CONFLICT: "Конфликт запуска бота",
    BotError.BOT_MANUAL_STOP: "Бот остановлен вручную",
    BotError.BOT_MANUAL_START: "Бот запущен вручную",
    BotError.BOT_AUTO_RESTART: "Watchdog восстановил бота",
    BotError.BOT_DEPLOY_RESTARTED: "Деплой завершён успешно",
    BotError.BOT_PROCESS_STOPPING: "Остановка процесса Shop-бота",
    BotError.BOT_PROCESS_UNCLEAN_EXIT: "Нештатное завершение процесса",
    BotError.BOT_MEMORY_PRESSURE_WARNING: "Высокое потребление памяти",
    BotError.BOT_MEMORY_PRESSURE_CRITICAL: "Критическое потребление памяти",
    BotError.EVENT_LOOP_DEAD: "Event loop недоступен",
    BotError.HOST_UNREACHABLE: "Хост недоступен",
    BotError.HOST_LOGIN_FAILED: "Ошибка авторизации на хосте",
    BotError.HOST_INBOUND_MISSING: "Inbound не найден на хосте",
    BotError.HOST_SYNC_FAILED: "Ошибка синхронизации хоста",
    BotError.HOST_ALL_DOWN: "Все хосты недоступны",
    BotError.HOST_RECOVERED: "Хост восстановлен",
    BotError.TRIAL_ISSUE_RETRYING: "Выдача триала отложена",
    BotError.TRIAL_ISSUE_PENDING_VERIFICATION: "Выдача триала проверяется",
    BotError.TRIAL_ISSUE_RECOVERED: "Довыдача триала успешна",
    BotError.TRIAL_ISSUE_FAILED: "Не удалось выдать триал",
    BotError.TRIAL_RETRY_PROMPT_SHOWN_AGAIN: "Триал показан повторно",
    BotError.TRIAL_ISSUE_CONSUMED: "Пользователь воспользовался триалом",
    BotError.BLITZ_UNREACHABLE: "Blitz недоступен",
    BotError.BLITZ_AUTH_FAILED: "Ошибка авторизации Blitz",
    BotError.BLITZ_SYNC_FAILED: "Ошибка синхронизации Blitz",
    BotError.PAYMENT_YOOKASSA_FAILED: "Проблема оплаты YooKassa",
    BotError.PAYMENT_CRYPTOBOT_FAILED: "Проблема оплаты CryptoBot",
    BotError.PAYMENT_HELEKET_FAILED: "Проблема оплаты Heleket",
    BotError.PAYMENT_TON_FAILED: "Проблема оплаты TON",
    BotError.PAYMENT_PLATEGA_FAILED: "Проблема оплаты Platega",
    BotError.PAYMENT_PAYPEAR_FAILED: "Проблема оплаты PayPear",
    BotError.PAYMENT_PAYPEAR_CREATE_FAILED: "PayPear: ссылка не создана",
    BotError.PAYMENT_LAVA_FAILED: "Проблема оплаты LAVA",
    BotError.PAYMENT_LAVA_CREATE_FAILED: "LAVA: ссылка не создана",
    BotError.PAYMENT_RECONCILE_FAILED: "Ошибка сверки платежей",
    BotError.PAYMENT_KEY_ISSUE_FAILED: "Оплата есть, ключ не выдан",
    BotError.PAYMENT_KEY_ISSUE_RETRYING: "Выдача ключа отложена",
    BotError.DATABASE_ERROR: "Ошибка базы данных",
    BotError.DATABASE_LOCKED: "База данных занята",
    BotError.SCHEDULER_CRASH: "Падение планировщика",
    BotError.WATCHDOG_ERROR: "Ошибка watchdog",
    BotError.TELEGRAM_API_ERROR: "Ошибка Telegram API",
    BotError.SSL_CERTIFICATE_ERROR: "Ошибка SSL",
    BotError.DNS_RESOLUTION_FAILED: "Ошибка DNS",
    BotError.WEB_PANEL_ERROR: "Ошибка веб-панели",
    BotError.UNKNOWN: "Неизвестная ошибка",
}

_DETAIL_LABELS: dict[str, str] = {
    "source": "Источник",
    "payment_source": "Источник платежа",
    "runtime_source": "Текущий инстанс",
    "task": "Задача",
    "commit": "Коммит",
    "ref": "Ветка",
    "pipeline": "Pipeline",
    "provider": "Провайдер",
    "payment_id": "ID платежа",
    "user_id": "ID Telegram",
    "username": "Имя пользователя",
    "method": "Метод оплаты",
    "host": "Хост",
    "flow": "Сценарий",
    "plan_id": "ID тарифа",
    "key_id": "ID ключа",
    "key_email": "Email ключа",
    "retry_count": "Попыток",
    "retry_limit": "Лимит попыток",
    "count": "Количество",
    "checked": "Проверено",
    "errors": "Ошибок",
    "recovered": "Восстановлено",
    "issue_count": "Счётчик инцидента",
    "attempts": "Попыток в запросе",
    "verification_attempts": "Попыток проверки",
    "timeout": "Таймаут (сек)",
    "sla_seconds": "SLA (сек)",
    "payment_ids": "ID платежей",
    "next_retry_at": "Следующая попытка",
    "message_shown_again": "Сообщение повторно показано",
    "user_notified_retry": "Пользователь уведомлён о повторе",
    "trial_recovered_after_issue": "Триал восстановлен после ошибки",
    "order_id": "ID заказа",
    "payment_variant": "Вариант оплаты",
    "status": "Статус",
    "previous_status": "Предыдущий статус",
    "path": "URL webhook",
    "reason": "Причина",
    "metadata_keys": "Ключи metadata",
    "error": "Ошибка",
    "failed_hosts": "Недоступных хостов",
    "via": "Источник вызова",
    "url": "URL",
    "signal": "Сигнал",
    "pid": "PID",
    "server": "Сервер",
    "memory_used": "Память использовано",
    "memory_limit": "Лимит памяти",
    "memory_usage_percent": "Использование памяти",
    "rss": "RSS процесса",
}

_DEFAULT_COMPACT_COPYABLE_DETAIL_KEYS: set[str] = {
    "payment_id",
    "payment_ids",
    "order_id",
    "user_id",
    "host",
    "key_id",
    "task",
    "commit",
    "ref",
    "pipeline",
    "signal",
    "pid",
    "server",
    "memory_used",
    "memory_limit",
    "memory_usage_percent",
    "rss",
}

_PAYMENT_PROVIDER_WARNING_COPYABLE_DETAIL_KEYS: set[str] = {
    "payment_variant",
    "error",
}

_PAYMENT_RECONCILE_WARNING_COPYABLE_DETAIL_KEYS: set[str] = {
    "provider",
    "checked",
    "errors",
    "recovered",
    "error",
}

_HOST_ALERT_ERRORS: set[BotError] = {
    BotError.HOST_UNREACHABLE,
    BotError.HOST_LOGIN_FAILED,
    BotError.HOST_INBOUND_MISSING,
    BotError.HOST_SYNC_FAILED,
    BotError.HOST_ALL_DOWN,
    BotError.HOST_RECOVERED,
}

_TRIAL_ALERT_ERRORS: set[BotError] = {
    BotError.TRIAL_ISSUE_RETRYING,
    BotError.TRIAL_ISSUE_PENDING_VERIFICATION,
    BotError.TRIAL_ISSUE_RECOVERED,
    BotError.TRIAL_ISSUE_FAILED,
    BotError.TRIAL_RETRY_PROMPT_SHOWN_AGAIN,
    BotError.TRIAL_ISSUE_CONSUMED,
}

_PAYMENT_ISSUE_ALERT_ERRORS: set[BotError] = {
    BotError.PAYMENT_KEY_ISSUE_FAILED,
    BotError.PAYMENT_KEY_ISSUE_RETRYING,
}

_PAYMENT_PROVIDER_WARNING_ALERT_ERRORS: set[BotError] = {
    BotError.PAYMENT_PAYPEAR_CREATE_FAILED,
}

_PAYMENT_RECONCILE_WARNING_ALERT_ERRORS: set[BotError] = {
    BotError.PAYMENT_RECONCILE_FAILED,
}

_WATCHDOG_ONLY_ERRORS: set[BotError] = {
    BotError.HOST_UNREACHABLE,
    BotError.HOST_LOGIN_FAILED,
    BotError.HOST_INBOUND_MISSING,
    BotError.HOST_SYNC_FAILED,
    BotError.HOST_RECOVERED,
    BotError.TRIAL_ISSUE_RETRYING,
    BotError.TRIAL_ISSUE_PENDING_VERIFICATION,
    BotError.TRIAL_ISSUE_RECOVERED,
    BotError.TRIAL_ISSUE_FAILED,
    BotError.TRIAL_RETRY_PROMPT_SHOWN_AGAIN,
    BotError.TRIAL_ISSUE_CONSUMED,
    BotError.BLITZ_UNREACHABLE,
    BotError.BLITZ_AUTH_FAILED,
    BotError.BLITZ_SYNC_FAILED,
    BotError.DATABASE_LOCKED,
    BotError.BOT_PROCESS_STOPPING,
    BotError.BOT_PROCESS_UNCLEAN_EXIT,
    BotError.BOT_MEMORY_PRESSURE_WARNING,
    BotError.BOT_MEMORY_PRESSURE_CRITICAL,
    BotError.WATCHDOG_ERROR,
    BotError.TELEGRAM_API_ERROR,
    BotError.SSL_CERTIFICATE_ERROR,
    BotError.DNS_RESOLUTION_FAILED,
}

_HOST_COMPACT_COPYABLE_DETAIL_KEYS: set[str] = {
    "host",
    "status",
    "previous_status",
    "url",
    "error",
    "outage_seconds",
    "failed_hosts",
}

_TRIAL_COMPACT_COPYABLE_DETAIL_KEYS: set[str] = {
    "payment_id",
    "user_id",
    "host",
    "key_id",
}

_PAYMENT_COMPACT_COPYABLE_DETAIL_KEYS: set[str] = {
    "payment_id",
    "payment_ids",
    "order_id",
    "user_id",
    "host",
    "key_id",
}

_COMPACT_BOOLEAN_FLAG_LABELS: dict[str, tuple[str, str, str]] = {
    "trial_recovered_after_issue": (
        "Триал восстановлен после ошибки",
        "ДА",
        "НЕТ",
    ),
    "message_shown_again": (
        "Повторное сообщение пользователю",
        "ПОКАЗАНО",
        "НЕ ПОКАЗАНО",
    ),
    "user_notified_retry": (
        "Пользователь уведомлён о повторе",
        "ДА",
        "НЕТ",
    ),
}

_DETAIL_ORDER: tuple[str, ...] = (
    "source",
    "payment_source",
    "runtime_source",
    "task",
    "commit",
    "ref",
    "pipeline",
    "provider",
    "method",
    "payment_id",
    "order_id",
    "user_id",
    "username",
    "flow",
    "host",
    "plan_id",
    "key_id",
    "key_email",
    "issue_count",
    "retry_count",
    "attempts",
    "verification_attempts",
    "timeout",
    "retry_limit",
    "count",
    "checked",
    "errors",
    "recovered",
    "sla_seconds",
    "payment_ids",
    "next_retry_at",
    "message_shown_again",
    "user_notified_retry",
    "trial_recovered_after_issue",
    "status",
    "previous_status",
    "reason",
    "error",
    "metadata_keys",
    "failed_hosts",
    "path",
    "url",
    "outage_seconds",
    "signal",
    "pid",
    "server",
    "memory_used",
    "memory_limit",
    "memory_usage_percent",
    "rss",
)

_ALERT_ACTION_HINTS: dict[BotError, str] = {
    BotError.HOST_UNREACHABLE: "Проверьте доступность панели, SSL/домен и логин-пароль хоста.",
    BotError.HOST_ALL_DOWN: "Включите maintenance и восстановите минимум один VPN-хост.",
    BotError.PAYMENT_PLATEGA_FAILED: "Проверьте настройки Platega (API key, shop id, webhook secret) и доступность API.",
    BotError.PAYMENT_PAYPEAR_FAILED: "Проверьте настройки PayPear (shop id, secret key, webhook secret) и доступность API.",
    BotError.PAYMENT_PAYPEAR_CREATE_FAILED: (
        "Проверьте доступность PayPear и динамику повторов. Если webhook/оплаченные транзакции не страдают, "
        "ручное вмешательство не требуется."
    ),
    BotError.PAYMENT_LAVA_FAILED: (
        "Проверьте LAVA shop id, основной/дополнительный ключи, webhook и доступность API."
    ),
    BotError.PAYMENT_LAVA_CREATE_FAILED: (
        "Проверьте доступность LAVA и настройки способов оплаты. Деньги по этому запросу не списаны."
    ),
    BotError.PAYMENT_RECONCILE_FAILED: (
        "Проверьте доступность API провайдера и метаданные pending-транзакций; "
        "при росте ошибок проверьте credentials и сетевой доступ."
    ),
    BotError.PAYMENT_KEY_ISSUE_FAILED: "Проверьте статус хоста и повторите выдачу ключа по ID платежа.",
    BotError.PAYMENT_KEY_ISSUE_RETRYING: "Проверьте хост и мониторьте повторные попытки довыдачи.",
    BotError.TRIAL_ISSUE_RETRYING: "Сообщите пользователю повторить попытку. Если проблема повторяется — проверьте хост.",
    BotError.TRIAL_ISSUE_PENDING_VERIFICATION: "Мониторьте recovery-alert. Если verification часто не находит ключ, проверьте XUI/туннель.",
    BotError.TRIAL_ISSUE_FAILED: "Проверьте доступность хоста и предложите пользователю повторить получение триала.",
    BotError.TRIAL_RETRY_PROMPT_SHOWN_AGAIN: "Проверьте хост: пользователю уже повторно показана кнопка триала.",
    BotError.BOT_START_FAILED: "Проверьте токен/username/ID админа и запустите бот из панели.",
    BotError.BOT_SETTINGS_MISSING: "Проверьте токен/username/ID админа и запустите бот из панели.",
    BotError.BOT_TOKEN_INVALID: "Проверьте токен в настройках и права бота в Telegram.",
    BotError.BOT_PROCESS_STOPPING: (
        "Проверьте, был ли reboot/shutdown сервера или рестарт контейнера. "
        "Если сервер недоступен полностью, нужен внешний uptime-мониторинг вне этого сервера."
    ),
    BotError.BOT_PROCESS_UNCLEAN_EXIT: (
        "Проверьте docker inspect OOMKilled, journalctl -k и docker compose logs. "
        "Если сервер перезагружался целиком, нужен внешний uptime-мониторинг вне этого сервера."
    ),
    BotError.BOT_MEMORY_PRESSURE_WARNING: "Проверьте фоновые sync/retry операции и текущий docker stats.",
    BotError.BOT_MEMORY_PRESSURE_CRITICAL: (
        "Проверьте docker stats и логи sync/retry. До снижения памяти тяжёлые операции будут пропускаться."
    ),
    BotError.TELEGRAM_API_ERROR: (
        "Проверьте доступность api.telegram.org с сервера, сетевые таймауты и занятость event loop. "
        "Если сообщения обрабатываются, перезапуск бота не требуется."
    ),
}

_REASON_LABELS: dict[str, str] = {
    "create_payment_exception": "Ошибка запроса к платежному провайдеру.",
    "invalid_create_payment_payload": "Платежный провайдер вернул некорректный ответ.",
    "pending_transaction_not_created": "Транзакция не записалась в БД.",
    "webhook_secret_missing": "Не настроен секрет проверки webhook.",
    "missing_payment_id": "Webhook пришел без payment_id.",
    "no_pending_metadata": "Не найдена pending-транзакция для оплаты.",
    "could_not_start_processing": "Не удалось перевести транзакцию в processing.",
    "bot_or_loop_unavailable": "Обработчик платежа недоступен (бот или event loop).",
    "wrong_bot_instance": "Платёж пришёл не в тот инстанс бота.",
    "metadata_not_found_for_pending_transaction": "Для pending-платежа не найдены метаданные.",
    "payment_processor_unavailable": "Платежный процессор недоступен.",
    "blitz_disabled": "Выдача через Blitz выключена в админке.",
    "key_creation_failed": "Не удалось создать или продлить ключ на панели.",
    "deploy_restart_ok": "Деплой выполнен, бот перезапущен успешно.",
    "process_signal_shutdown": "Процесс получил сигнал остановки от ОС или контейнера.",
    "suspected_oom_kill": "Предыдущий процесс завершился нештатно; вероятен OOM kill или SIGKILL.",
    "runtime_guard": "Runtime guard обнаружил высокое потребление памяти.",
    "scheduler_sync_memory_guard": "Синхронизация с панелями пропущена из-за memory pressure.",
    "bot_down_detected": "Бот перестал отвечать, watchdog начал восстановление.",
    "all_hosts_unavailable": "Все VPN-хосты в статусе недоступности.",
    "bot_recovered_after_crash": "Watchdog успешно восстановил бота после падения.",
    "watchdog_retry": "Автоперезапуск не удался, будет повторная попытка.",
    "watchdog_strict_failure": "Строгая ошибка запуска. Watchdog продолжит повторные попытки.",
    "watchdog_probe_timeout_persistent": "Health-probe Telegram API не отвечает дольше порога.",
    "host_health_monitor_crash": "Фоновый мониторинг хостов упал.",
    "login_or_inbound_lookup_failed": "Не удалось войти в панель или найти inbound.",
    "host_sync_exception": "Неожиданная ошибка синхронизации хоста.",
    "host_timeout": "Панель не ответила за отведённое время.",
    "host_connection_refused": "Панель отклонила соединение.",
    "host_connection_reset": "Соединение с панелью было сброшено.",
    "host_dns_failed": "Не удалось разрешить доменное имя панели.",
    "host_ssl_error": "Ошибка SSL при подключении к панели.",
    "host_invalid_http_response": "Панель вернула некорректный HTTP-ответ.",
    "host_login_failed": "Не удалось авторизоваться в панели.",
    "host_inbound_missing": "На панели не найден указанный inbound.",
    "host_login_exception": "Неожиданная ошибка входа в панель.",
    "panel_raw_lookup_failed": "Raw lookup панели недоступен; бот пропустил тяжёлую синхронизацию без удаления локальных ключей.",
    "trial_issue_timeout": "Панель долго не отвечает при выдаче триала.",
    "trial_issue_pending_verification": "Панель/туннель ответили неоднозначно; бот проверяет, создан ли ключ.",
    "trial_issue_recovered": "Выдача триала восстановлена после временной ошибки.",
    "trial_issue_failed": "Триал не выдан: пользователь не получил ключ.",
    "trial_retry_prompt_shown_again": "Пользователю повторно показана кнопка триала.",
    "trial_issue_consumed": "Пользователь успешно получил триал.",
    "autoretry_exhausted_host_dead": "Автодовыдача исчерпала лимит попыток, хост всё ещё недоступен.",
    "host_unavailable_at_issuance": "Хост недоступен в момент выдачи нового ключа.",
    "host_unavailable_at_extension": "Хост недоступен в момент продления ключа.",
    "retry_not_marked_paid": "Обработка завершилась, но транзакция не перешла в статус paid.",
    "retry_exception:timeouterror": "Таймаут при попытке довыдачи ключа.",
    "retry_exception:connectionerror": "Ошибка соединения при попытке довыдачи.",
    "startup_catchup": "Довыдача при запуске бота: обработка зависших транзакций.",
    "stale_processing_recovered": "Транзакция застряла в processing после краша бота — восстановлена для повторной выдачи.",
    "issuance_autoretry_success": "Автодовыдача ключа завершилась успехом.",
    "platega_reconcile_error": "Сбой сверки платежей Platega.",
    "paypear_reconcile_error": "Сбой сверки платежей PayPear.",
    "lava_reconcile_error": "Сбой сверки платежей LAVA.",
    "lava_create_payment_exception": "LAVA не создала ссылку на оплату.",
    "lava_invalid_create_payment_payload": "LAVA вернула некорректный ответ при создании оплаты.",
    "lava_pending_transaction_not_created": "Счёт LAVA создан, но локальная транзакция не записана.",
    "yookassa_reconcile_error": "Сбой сверки платежей YooKassa.",
}

_SOURCE_LABELS: dict[str, str] = {
    "main": "Основной бот",
    "reserve": "Резервный бот",
}

_STATUS_LABELS: dict[str, str] = {
    "healthy": "доступен (healthy)",
    "degraded": "ошибка (degraded)",
    "unavailable": "недоступен (unavailable)",
}

_SUPPRESSED_COMPACT_REASON_CODES: set[str] = {
    "deploy_restart_ok",
    "bot_recovered_after_crash",
    "watchdog_retry",
    "watchdog_strict_failure",
}

_ALERT_DEDUPE_WINDOWS: dict[BotError, int] = {
    BotError.PAYMENT_PAYPEAR_CREATE_FAILED: 15 * 60,
    BotError.PAYMENT_LAVA_CREATE_FAILED: 15 * 60,
}

_STABLE_ALERT_DEDUPE_REASON_CODES: set[str] = {
    "watchdog_probe_timeout_persistent",
}


def humanize_alert_reason(reason: str | None) -> str:
    raw_reason = str(reason or "").strip()
    if not raw_reason:
        return "Не указана."

    normalized = raw_reason.lower()
    mapped = _REASON_LABELS.get(normalized)
    if mapped:
        return mapped

    if normalized.startswith("precheck_blocked:"):
        precheck_code = raw_reason.split(":", 1)[1].strip()
        if precheck_code == "global_maintenance":
            return "Выдача отключена: включен глобальный maintenance."
        if precheck_code == "host_unavailable":
            return "Хост временно недоступен, выдача отложена до восстановления."
        if precheck_code == "host_maintenance":
            return "Хост находится в maintenance, выдача временно отключена."
        if precheck_code == "super_host_maintenance":
            return "Super-хост в maintenance, выдача временно отключена."
        if precheck_code == "super_host_unavailable":
            return "Super-хост недоступен, выдача временно отключена."
        return f"Pre-check заблокировал выдачу ({precheck_code})."

    if normalized.startswith("exception:"):
        exception_name = raw_reason.split(":", 1)[1].strip() or "UnknownException"
        return f"Исключение во время обработки оплаты: {exception_name}."

    if normalized.startswith("retry_exception:"):
        exception_name = raw_reason.split(":", 1)[1].strip() or "UnknownException"
        return f"Ошибка при довыдаче ключа: {exception_name}."

    if normalized.startswith("key_creation_failed:"):
        suffix = raw_reason.split(":", 1)[1].strip()
        return f"Не удалось создать ключ. {suffix}"

    if normalized.startswith("metadata_incomplete_after_merge:"):
        fields = raw_reason.split(":", 1)[1].strip() or "unknown"
        return f"После объединения metadata не хватает обязательных полей: {fields}."

    if normalized.startswith("async_processor_crashed:"):
        exception_name = raw_reason.split(":", 1)[1].strip() or "UnknownException"
        return f"Асинхронная обработка оплаты упала с исключением: {exception_name}."

    return raw_reason


def _build_alert_dedupe_key(error: BotError, parsed: dict[str, str], details: str) -> str:
    reason_code = str(parsed.get("reason") or "").strip().lower()
    if reason_code in _STABLE_ALERT_DEDUPE_REASON_CODES:
        source = str(
            parsed.get("source")
            or parsed.get("runtime_source")
            or parsed.get("payment_source")
            or ""
        ).strip().lower()
        parts = [error.tag, f"reason={reason_code}"]
        if source:
            parts.append(f"source={source}")
        return "|".join(parts)[:512]

    normalized_details = " ".join(str(details or "").split())
    return f"{error.tag}|{normalized_details}"[:512]


def _parse_alert_details(details: str) -> tuple[dict[str, str], str]:
    raw = str(details or "").strip()
    if not raw:
        return {}, ""

    compact = raw.replace("\n", " ")
    parts = [part for part in compact.split(" ") if part]
    parsed: dict[str, str] = {}
    tail_parts: list[str] = []

    for part in parts:
        if "=" not in part:
            tail_parts.append(part)
            continue
        key, value = part.split("=", 1)
        normalized_key = key.strip().lower()
        normalized_value = value.strip()
        if not normalized_key or not normalized_value:
            tail_parts.append(part)
            continue
        parsed[normalized_key] = normalized_value

    plain_tail = " ".join(tail_parts).strip()
    return parsed, plain_tail


def _extract_payment_alert_ids(parsed: dict[str, str], details: str) -> list[str]:
    candidates: list[str] = []
    for key in (
        "payment_id",
        "payment_ids",
        "external_payment_id",
        "transaction_id",
        "id_платежа",
    ):
        value = parsed.get(key)
        if value:
            candidates.extend(_PAYMENT_ID_RE.findall(str(value)))
    candidates.extend(_PAYMENT_ID_RE.findall(str(details or "")))
    return sorted(set(candidates))


def _payment_key_issue_alert_allowed(error: BotError, parsed: dict[str, str], details: str) -> bool:
    if error is not BotError.PAYMENT_KEY_ISSUE_FAILED:
        return True

    payment_ids = _extract_payment_alert_ids(parsed, details)
    if not payment_ids:
        return True

    now = int(time.time())
    db_path = os.path.abspath(_PAYMENT_ALERT_THROTTLE_DB)
    try:
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with sqlite3.connect(db_path, timeout=3.0) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS payment_alert_throttle (
                    alert_key TEXT PRIMARY KEY,
                    payment_id TEXT NOT NULL,
                    error_tag TEXT NOT NULL,
                    send_count INTEGER NOT NULL DEFAULT 0,
                    first_sent_at INTEGER NOT NULL,
                    last_sent_at INTEGER NOT NULL
                )
                """
            )

            allowed = False
            for payment_id in payment_ids:
                alert_key = f"{error.tag}:{payment_id}"
                row = conn.execute(
                    "SELECT send_count, last_sent_at FROM payment_alert_throttle WHERE alert_key = ?",
                    (alert_key,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO payment_alert_throttle (
                            alert_key, payment_id, error_tag, send_count, first_sent_at, last_sent_at
                        ) VALUES (?, ?, ?, 1, ?, ?)
                        """,
                        (alert_key, payment_id, error.tag, now, now),
                    )
                    allowed = True
                    continue

                send_count, last_sent_at = int(row[0] or 0), int(row[1] or 0)
                if send_count < _PAYMENT_ALERT_DAILY_AFTER:
                    conn.execute(
                        """
                        UPDATE payment_alert_throttle
                           SET send_count = send_count + 1, last_sent_at = ?
                         WHERE alert_key = ?
                        """,
                        (now, alert_key),
                    )
                    allowed = True
                    continue

                if now - last_sent_at >= _PAYMENT_ALERT_DAILY_INTERVAL_SECONDS:
                    conn.execute(
                        """
                        UPDATE payment_alert_throttle
                           SET send_count = send_count + 1, last_sent_at = ?
                         WHERE alert_key = ?
                        """,
                        (now, alert_key),
                    )
                    allowed = True
                    continue

                logger.info(
                    "Suppressed repeated payment key alert payment_id=%s count=%s last_sent_at=%s",
                    payment_id,
                    send_count,
                    last_sent_at,
                )
            return allowed
    except Exception:
        logger.exception("Payment alert throttle failed; sending alert without throttle.")
        return True


def _format_detail_value(key: str, value: str) -> str:
    if key == "reason":
        human = humanize_alert_reason(value)
        if human != value:
            return f"{human} ({value})"
        return human
    if key == "source":
        return _SOURCE_LABELS.get(value.lower(), value)
    if key in {"status", "previous_status"}:
        return _STATUS_LABELS.get(value.lower(), value)
    return value


def _format_alert_details(parsed: dict[str, str], plain_tail: str) -> list[str]:
    if not parsed and not plain_tail:
        return []

    lines: list[str] = []
    used: set[str] = set()
    for key in _DETAIL_ORDER:
        if key not in parsed:
            continue
        used.add(key)
        label = _DETAIL_LABELS.get(key, key)
        display_value = _format_detail_value(key, parsed[key])
        lines.append(f"- {label} = {display_value}")

    for key in sorted(parsed.keys()):
        if key in used:
            continue
        label = _DETAIL_LABELS.get(key, key)
        lines.append(f"- {label} = {_format_detail_value(key, parsed[key])}")

    if plain_tail:
        lines.append(f"- Сообщение = {plain_tail}")
    return lines


def _compact_copyable_keys_for_error(error: BotError) -> set[str]:
    if error in _HOST_ALERT_ERRORS:
        return _HOST_COMPACT_COPYABLE_DETAIL_KEYS
    if error in _TRIAL_ALERT_ERRORS:
        return _TRIAL_COMPACT_COPYABLE_DETAIL_KEYS
    if error in _PAYMENT_ISSUE_ALERT_ERRORS:
        return _PAYMENT_COMPACT_COPYABLE_DETAIL_KEYS
    if error in _PAYMENT_RECONCILE_WARNING_ALERT_ERRORS:
        return _PAYMENT_RECONCILE_WARNING_COPYABLE_DETAIL_KEYS
    if error in _PAYMENT_PROVIDER_WARNING_ALERT_ERRORS:
        return _PAYMENT_PROVIDER_WARNING_COPYABLE_DETAIL_KEYS
    return _DEFAULT_COMPACT_COPYABLE_DETAIL_KEYS


def _format_compact_alert_details(
    parsed: dict[str, str],
    plain_tail: str,
    *,
    copyable_keys: set[str],
) -> list[str]:
    if not parsed and not plain_tail:
        return []

    lines: list[str] = []
    used: set[str] = set()
    for key in _DETAIL_ORDER:
        if key not in parsed:
            continue
        if key in {"source", "payment_source", "runtime_source"}:
            continue
        if key not in copyable_keys:
            continue
        used.add(key)
        label = _DETAIL_LABELS.get(key, key)
        if key == "reason":
            display_value = humanize_alert_reason(parsed[key])
        else:
            display_value = _format_detail_value(key, parsed[key])
        lines.append(f"{label} = {display_value}")

    for key in sorted(parsed.keys()):
        if key in used:
            continue
        if key in {"source", "payment_source", "runtime_source"}:
            continue
        if key not in copyable_keys:
            continue
        label = _DETAIL_LABELS.get(key, key)
        if key == "reason":
            display_value = humanize_alert_reason(parsed[key])
        else:
            display_value = _format_detail_value(key, parsed[key])
        lines.append(f"{label} = {display_value}")

    if plain_tail and not parsed:
        lines.append(f"Сообщение = {plain_tail}")
    return lines


def _parse_truthy(value: str | None) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in {"1", "true", "yes", "y", "on", "да"}


def _format_compact_boolean_flags(parsed: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for key in ("trial_recovered_after_issue", "message_shown_again", "user_notified_retry"):
        raw_value = parsed.get(key)
        if raw_value is None:
            continue
        label, true_label, false_label = _COMPACT_BOOLEAN_FLAG_LABELS[key]
        lines.append(f"{label}: {true_label if _parse_truthy(raw_value) else false_label}")
    return lines


def _reason_is_relevant_for_error(error: BotError, reason_code: str) -> bool:
    normalized_reason = str(reason_code or "").strip().lower()
    if not normalized_reason:
        return False
    return not (error in _HOST_ALERT_ERRORS and normalized_reason.startswith("trial_"))


def _extract_compact_alert_source(parsed: dict[str, str]) -> str | None:
    for key in ("source", "payment_source", "runtime_source"):
        value = parsed.get(key)
        if not value:
            continue
        return _format_detail_value(key, value)
    return None


# ── Classifier: map raw exception / message to a BotError ─────────

_EXCEPTION_MAP: list[tuple[list[str], BotError]] = [
    # Host / panel auth failures must be recognized before generic auth markers.
    (
        [
            "host_login_exception",
            "неожиданная ошибка входа в панель",
            "panel authentication failed",
            "forbidden for url",
        ],
        BotError.HOST_LOGIN_FAILED,
    ),
    # Bot lifecycle
    (["invalid token", "unauthorized", "401"], BotError.BOT_TOKEN_INVALID),
    (["telegramconflicterror", "conflict"], BotError.BOT_CONFLICT),
    (["не все обязательные настройки", "bot_settings_missing"], BotError.BOT_SETTINGS_MISSING),
    (["цикл событий не установлен", "event loop"], BotError.EVENT_LOOP_DEAD),
    # Hosts
    (["connection timeout", "socket.timeout", "timed out", "unreachable"], BotError.HOST_UNREACHABLE),
    (["login or inbound", "login failed", "could not log in"], BotError.HOST_LOGIN_FAILED),
    (["inbound not found", "inbound with id"], BotError.HOST_INBOUND_MISSING),
    (["unknownprotocol", "http/0.0"], BotError.HOST_UNREACHABLE),
    # Blitz
    (["blitz login failed", "blitz api is not configured", "blitz_api_token"], BotError.BLITZ_AUTH_FAILED),
    (["blitz create_user", "blitz get_user", "blitz delete_user", "blitz update_user", "blitz get_uri"], BotError.BLITZ_UNREACHABLE),
    (["blitz sync failed", "blitz sync"], BotError.BLITZ_SYNC_FAILED),
    # Payments
    (["yookassa", "yookassa_shop_id"], BotError.PAYMENT_YOOKASSA_FAILED),
    (["platega"], BotError.PAYMENT_PLATEGA_FAILED),
    (["paypear"], BotError.PAYMENT_PAYPEAR_FAILED),
    (["lava"], BotError.PAYMENT_LAVA_FAILED),
    (["cryptobot"], BotError.PAYMENT_CRYPTOBOT_FAILED),
    (["heleket"], BotError.PAYMENT_HELEKET_FAILED),
    (["ton connect", "tonapi", "ton_wallet"], BotError.PAYMENT_TON_FAILED),
    # Database
    (["sqlite3.error", "database is locked", "disk i/o error"], BotError.DATABASE_ERROR),
    (["database is locked"], BotError.DATABASE_LOCKED),
    # SSL / DNS
    (["ssl", "certificate verify failed", "cert"], BotError.SSL_CERTIFICATE_ERROR),
    (["nodename nor servname", "name or service not known", "dns"], BotError.DNS_RESOLUTION_FAILED),
]


def classify_error(message: str) -> BotError:
    """Determine the best BotError for a raw error message string."""
    normalized = (message or "").lower()
    for markers, error in _EXCEPTION_MAP:
        if any(m in normalized for m in markers):
            return error
    return BotError.UNKNOWN


# ── Convenience alert sender ──────────────────────────────────────

def send_critical_alert(
    error: BotError,
    *,
    details: str = "",
    respect_cooldown: bool = True,
) -> bool:
    """
    Send a structured alert through all configured channels.

    Wraps scheduler.send_shop_ops_alert with error enum metadata.
    Import is deferred to avoid circular dependencies.
    """
    from shop_bot.data_manager.scheduler import send_shop_ops_alert

    title = _ALERT_TITLES.get(error, error.tag.replace("_", " ").strip().title())
    parsed, plain_tail = _parse_alert_details(details)
    if not _payment_key_issue_alert_allowed(error, parsed, details):
        return False
    source_label = _extract_compact_alert_source(parsed)
    compact_reason = ""
    raw_reason_code = str(parsed.get("reason") or "").strip().lower()
    if (
        raw_reason_code
        and raw_reason_code not in _SUPPRESSED_COMPACT_REASON_CODES
        and _reason_is_relevant_for_error(error, raw_reason_code)
    ):
        compact_reason = humanize_alert_reason(parsed["reason"])

    compact_meta_parsed = dict(parsed)
    compact_meta_parsed.pop("reason", None)
    detail_parts = _format_compact_alert_details(
        compact_meta_parsed,
        "",
        copyable_keys=_compact_copyable_keys_for_error(error),
    )
    compact_flags = _format_compact_boolean_flags(compact_meta_parsed) if error in _TRIAL_ALERT_ERRORS else []

    body_lines = [error.description]
    if compact_reason and compact_reason not in error.description:
        body_lines.append(f"Причина = {compact_reason}")

    if detail_parts:
        body_lines.append("")
        body_lines.extend(detail_parts)

    body_lines.extend(compact_flags)

    if plain_tail:
        body_lines.append(plain_tail)

    action_hint = _ALERT_ACTION_HINTS.get(error)
    if action_hint and error.severity not in {Severity.INFO, Severity.SUCCESS}:
        body_lines.append("")
        body_lines.append(f"Что сделать: {action_hint}")

    text = "\n".join(line for line in body_lines if line)
    dedupe_key = _build_alert_dedupe_key(error, parsed, details)

    return send_shop_ops_alert(
        title=title,
        reason=text,
        level=error.severity.value.lower(),
        source_label=source_label,
        manual_start_required=False,
        respect_cooldown=respect_cooldown,
        dedupe_key=dedupe_key,
        dedupe_window_seconds=_ALERT_DEDUPE_WINDOWS.get(error, 180),
        watchdog_only=(error in _WATCHDOG_ONLY_ERRORS),
    )


def get_alerts_dropped_count() -> int:
    """Number of alerts dropped due to queue overflow (for monitoring)."""
    return _alerts_dropped_count


def _ensure_alert_dispatcher() -> tuple[queue.Queue[tuple[BotError, str, bool]], threading.Thread]:
    global _alert_dispatch_queue  # noqa: PLW0603 - dispatcher singleton is module-level state.
    global _alert_dispatch_worker  # noqa: PLW0603 - dispatcher singleton is module-level state.

    with _alert_dispatch_lock:
        if _alert_dispatch_queue is None:
            _alert_dispatch_queue = queue.Queue(maxsize=_ALERT_DISPATCH_QUEUE_MAXSIZE)

        if _alert_dispatch_worker is None or not _alert_dispatch_worker.is_alive():
            def _worker() -> None:
                _alert_thread_local.in_dispatch = True
                while True:
                    task_error, task_details, task_respect_cooldown = _alert_dispatch_queue.get()
                    try:
                        send_critical_alert(
                            task_error,
                            details=task_details,
                            respect_cooldown=task_respect_cooldown,
                        )
                    except Exception:
                        logger.exception("Alert dispatcher worker failed.")
                    finally:
                        _alert_dispatch_queue.task_done()

            _alert_dispatch_worker = threading.Thread(
                target=_worker,
                name="alert-dispatcher",
                daemon=True,
            )
            _alert_dispatch_worker.start()

    return _alert_dispatch_queue, _alert_dispatch_worker


def send_critical_alert_nonblocking(
    error: BotError,
    *,
    details: str = "",
    respect_cooldown: bool = True,
) -> None:
    """Fire-and-forget sender via bounded background queue.

    Prevents unbounded thread growth during error bursts.
    Reentrancy guard: if called from within the alert dispatch worker
    thread, the call is silently dropped to prevent circular queuing.
    """
    global _alerts_dropped_count  # noqa: PLW0603 - alert drop counter is module-level state.

    if getattr(_alert_thread_local, "in_dispatch", False):
        logger.warning(
            "Reentrancy guard: skipping recursive alert '%s' from dispatch worker",
            error.tag,
        )
        return

    dispatch_queue, _ = _ensure_alert_dispatcher()
    try:
        dispatch_queue.put_nowait((error, details, respect_cooldown))
    except queue.Full:
        with _alerts_dropped_lock:
            _alerts_dropped_count += 1
        logger.warning(
            "Alert dispatcher queue is full (%s). Dropping alert '%s'. Total dropped: %d",
            _ALERT_DISPATCH_QUEUE_MAXSIZE,
            error.tag,
            _alerts_dropped_count,
        )
