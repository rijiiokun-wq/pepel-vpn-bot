from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from shop_bot.errors import BotError, humanize_alert_reason, send_critical_alert


def test_payment_key_issue_alert_is_human_readable():
    text = BotError.PAYMENT_KEY_ISSUE_FAILED.format_alert(
        "source=reserve payment_id=pay_123 user_id=77 host=SUPER reason=key_creation_failed"
    )

    assert "Критично: Оплата есть, ключ не выдан" in text
    assert "Источник = Резервный бот" in text
    assert "ID платежа = pay_123" in text
    assert "Хост = SUPER" in text
    assert "Причина = Не удалось создать или продлить ключ на панели. (key_creation_failed)" in text
    assert "Что сделать:" in text
    assert "Код: payment_key_issue" in text


def test_humanize_alert_reason_for_precheck_blocked():
    assert humanize_alert_reason("precheck_blocked:global_maintenance") == (
        "Выдача отключена: включен глобальный maintenance."
    )


def test_humanize_alert_reason_for_wrong_bot_instance():
    assert humanize_alert_reason("wrong_bot_instance") == (
        "Платёж пришёл не в тот инстанс бота."
    )


def test_scheduler_source_mentions_recovery_and_deploy_alerts():
    scheduler_path = Path(__file__).resolve().parent.parent / "src" / "shop_bot" / "data_manager" / "scheduler.py"
    source = scheduler_path.read_text(encoding="utf-8")

    assert "BotError.HOST_RECOVERED" in source
    assert "BotError.BOT_DEPLOY_RESTARTED" in source


def test_send_critical_alert_uses_compact_human_title_and_success_level(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_send_shop_ops_alert(**kwargs):
        captured.update(kwargs)
        return True

    import shop_bot.data_manager.scheduler as scheduler

    monkeypatch.setattr(scheduler, "send_shop_ops_alert", _fake_send_shop_ops_alert)

    result = send_critical_alert(
        BotError.BOT_DEPLOY_RESTARTED,
        details="source=main reason=deploy_restart_ok",
        respect_cooldown=True,
    )

    assert result is True
    assert captured["title"] == "Деплой завершён успешно"
    assert captured["level"] == "success"
    assert captured["source_label"] == "Основной бот"
    assert captured["respect_cooldown"] is True
    assert captured["reason"] == "Деплой завершён: Shop-бот перезапущен и работает в штатном режиме."


def test_send_critical_alert_formats_process_shutdown_for_watchlog(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_send_shop_ops_alert(**kwargs):
        captured.update(kwargs)
        return True

    import shop_bot.data_manager.scheduler as scheduler

    monkeypatch.setattr(scheduler, "send_shop_ops_alert", _fake_send_shop_ops_alert)

    result = send_critical_alert(
        BotError.BOT_PROCESS_STOPPING,
        details="source=main reason=process_signal_shutdown signal=SIGTERM pid=123 server=prod-1",
        respect_cooldown=False,
    )

    assert result is True
    assert captured["title"] == "Остановка процесса Shop-бота"
    assert captured["level"] == "critical"
    assert captured["source_label"] == "Основной бот"
    assert captured["respect_cooldown"] is False
    assert captured["watchdog_only"] is True
    assert "Процесс Shop-бота получает сигнал остановки" in captured["reason"]
    assert "Причина = Процесс получил сигнал остановки от ОС или контейнера." in captured["reason"]
    assert "Сигнал = SIGTERM" in captured["reason"]
    assert "PID = 123" in captured["reason"]
    assert "Сервер = prod-1" in captured["reason"]


def test_send_critical_alert_hides_raw_reason_codes_and_keeps_actionable_tail(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_send_shop_ops_alert(**kwargs):
        captured.update(kwargs)
        return True

    import shop_bot.data_manager.scheduler as scheduler

    monkeypatch.setattr(scheduler, "send_shop_ops_alert", _fake_send_shop_ops_alert)

    result = send_critical_alert(
        BotError.BOT_SETTINGS_MISSING,
        details=(
            "source=reserve reason=watchdog_strict_failure "
            "Причина: Невозможно запустить: не все обязательные настройки Telegram заполнены "
            "(reserve_bot_token, reserve_bot_username, admin_telegram_id). Автозапуск продолжает попытки."
        ),
        respect_cooldown=True,
    )

    assert result is True
    assert captured["title"] == "Неполные настройки бота"
    assert captured["level"] == "critical"
    assert captured["source_label"] == "Резервный бот"
    assert captured["reason"] == (
        "Не заполнены обязательные настройки (токен, username или ID админа). Бот не может запуститься.\n"
        "Причина: Невозможно запустить: не все обязательные настройки Telegram заполнены "
        "(reserve_bot_token, reserve_bot_username, admin_telegram_id). Автозапуск продолжает попытки.\n"
        "Что сделать: Проверьте токен/username/ID админа и запустите бот из панели."
    )
    assert "watchdog_strict_failure" not in captured["reason"]


def test_send_critical_alert_keeps_host_error_tail(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_send_shop_ops_alert(**kwargs):
        captured.update(kwargs)
        return True

    import shop_bot.data_manager.scheduler as scheduler

    monkeypatch.setattr(scheduler, "send_shop_ops_alert", _fake_send_shop_ops_alert)

    result = send_critical_alert(
        BotError.HOST_UNREACHABLE,
        details=(
            "source=main host=SUPER status=unavailable url=https://pepelbot.com:1666/rknblowme/ "
            "reason=host_connection_refused \nОшибка: HTTPSConnectionPool connection refused"
        ),
        respect_cooldown=True,
    )

    assert result is True
    assert captured["level"] == "critical"
    assert "Панель отклонила соединение." in captured["reason"]
    assert "Ошибка: HTTPSConnectionPool connection refused" in captured["reason"]


def test_paypear_create_failure_alert_is_warning_and_deduped_longer(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_send_shop_ops_alert(**kwargs):
        captured.update(kwargs)
        return True

    import shop_bot.data_manager.scheduler as scheduler

    monkeypatch.setattr(scheduler, "send_shop_ops_alert", _fake_send_shop_ops_alert)

    result = send_critical_alert(
        BotError.PAYMENT_PAYPEAR_CREATE_FAILED,
        details="source=reserve payment_variant=sbp reason=create_payment_exception error=network",
        respect_cooldown=True,
    )

    assert result is True
    assert captured["title"] == "PayPear: ссылка не создана"
    assert captured["level"] == "warning"
    assert captured["source_label"] == "Резервный бот"
    assert captured["dedupe_window_seconds"] == 15 * 60
    assert "Деньги не списаны" in captured["reason"]
    assert "Ошибка запроса к платежному провайдеру." in captured["reason"]


def test_probe_timeout_alert_uses_stable_dedupe_key_and_warning(monkeypatch):
    captured: list[dict[str, object]] = []

    def _fake_send_shop_ops_alert(**kwargs):
        captured.append(kwargs)
        return True

    import shop_bot.data_manager.scheduler as scheduler

    monkeypatch.setattr(scheduler, "send_shop_ops_alert", _fake_send_shop_ops_alert)

    for elapsed in (761, 805):
        result = send_critical_alert(
            BotError.TELEGRAM_API_ERROR,
            details=(
                "source=main reason=watchdog_probe_timeout_persistent "
                f"\nHealth: probe_timeout\nПроблема длится {elapsed} сек. Бот продолжает работу."
            ),
            respect_cooldown=True,
        )
        assert result is True

    assert len(captured) == 2
    assert captured[0]["title"] == "Ошибка Telegram API"
    assert captured[0]["level"] == "warning"
    assert captured[0]["source_label"] == "Основной бот"
    assert captured[0]["dedupe_key"] == captured[1]["dedupe_key"]
    assert captured[0]["dedupe_key"] == "telegram_api|reason=watchdog_probe_timeout_persistent|source=main"
    assert "Health-probe Telegram API не отвечает дольше порога." in captured[0]["reason"]
    assert "Health: probe_timeout" in captured[0]["reason"]
    assert "перезапуск бота не требуется" in captured[0]["reason"]


def test_payment_reconcile_alert_includes_provider_context(monkeypatch):
    captured: dict[str, object] = {}

    def _fake_send_shop_ops_alert(**kwargs):
        captured.update(kwargs)
        return True

    import shop_bot.data_manager.scheduler as scheduler

    monkeypatch.setattr(scheduler, "send_shop_ops_alert", _fake_send_shop_ops_alert)

    result = send_critical_alert(
        BotError.PAYMENT_RECONCILE_FAILED,
        details=(
            "source=main provider=PayPear checked=7 errors=2 recovered=1 "
            "reason=paypear_reconcile_error error=PayPearAPIError:500"
        ),
        respect_cooldown=True,
    )

    assert result is True
    assert captured["title"] == "Ошибка сверки платежей"
    assert captured["level"] == "warning"
    assert captured["source_label"] == "Основной бот"
    assert "Провайдер = PayPear" in captured["reason"]
    assert "Проверено = 7" in captured["reason"]
    assert "Ошибок = 2" in captured["reason"]
    assert "Восстановлено = 1" in captured["reason"]
    assert "Ошибка = PayPearAPIError:500" in captured["reason"]
    assert "Сбой сверки платежей PayPear." in captured["reason"]
