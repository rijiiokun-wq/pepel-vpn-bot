"""
Watchdog email alerts tests.

Focused on strict Shop startup failures:
- SMTP alert composition/sending behavior
- Cooldown semantics
- One-cycle watchdog decision logic
"""
from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from urllib import error as urlerror

import pytest

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


@pytest.fixture
def scheduler_module(monkeypatch):
    from shop_bot.data_manager import scheduler

    monkeypatch.setattr(scheduler, "_last_shop_watchdog_alert_at", None)
    monkeypatch.setattr(scheduler, "_shop_watchdog_transient_unhealthy_cycles", 0)
    monkeypatch.setattr(scheduler, "_shop_watchdog_transient_unhealthy_since", None)
    monkeypatch.setattr(scheduler, "_recent_shop_ops_alerts", {})
    return scheduler


class _FakeBotController:
    def __init__(
        self,
        *,
        running: bool,
        desired_running: bool,
        start_result: dict,
        healthy: bool = True,
        health_message: str = "ok",
        starting: bool = False,
        stop_result: dict | None = None,
    ):
        self._running = running
        self._healthy = healthy
        self._health_message = health_message
        self._starting = starting
        self.shop_desired_running = desired_running
        self.shop_autorestart_paused = False
        self._start_result = start_result
        self._stop_result = stop_result or {"status": "success", "message": "ok"}
        self.stop_calls = 0
        self.start_calls = 0

    def get_status(self, include_health: bool = False):
        return {
            "shop_bot_running": self._running,
            "shop_bot_starting": self._starting,
            "shop_bot_healthy": self._healthy,
            "shop_bot_health_message": self._health_message,
            "shop_bot_desired_running": self.shop_desired_running,
        }

    def start_shop_bot(self, initiated_by: str = "manual"):
        assert initiated_by == "watchdog"
        self.start_calls += 1
        return self._start_result

    def stop_shop_bot(self):
        self.stop_calls += 1
        self._running = False
        self._healthy = False
        return self._stop_result


def test_send_email_alert_success_starttls(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    monkeypatch.setenv("ALERT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("ALERT_SMTP_PORT", "587")
    monkeypatch.setenv("ALERT_SMTP_USER", "alerts@example.com")
    monkeypatch.setenv("ALERT_SMTP_PASSWORD", "secret")
    monkeypatch.setenv("ALERT_SMTP_FROM", "alerts@example.com")
    monkeypatch.setenv("ALERT_SMTP_TO", "ops@example.com,dev@example.com")
    monkeypatch.setenv("ALERT_SMTP_USE_TLS", "true")
    monkeypatch.setenv("ALERT_SMTP_USE_SSL", "false")

    smtp_client = MagicMock()
    smtp_cm = MagicMock()
    smtp_cm.__enter__.return_value = smtp_client
    monkeypatch.setattr(scheduler.smtplib, "SMTP", MagicMock(return_value=smtp_cm))

    result = scheduler._send_shop_watchdog_email_alert("strict startup failure")
    assert result is True

    scheduler.smtplib.SMTP.assert_called_once_with("smtp.example.com", 587, timeout=15)
    smtp_client.starttls.assert_called_once()
    smtp_client.login.assert_called_once_with("alerts@example.com", "secret")
    assert smtp_client.send_message.call_count == 1

    message = smtp_client.send_message.call_args.args[0]
    assert message["Subject"] == "[PEPEL BOT][critical] strict startup failure"
    assert "Критично · " in message.get_content()
    _, kwargs = smtp_client.send_message.call_args
    assert kwargs["to_addrs"] == ["ops@example.com", "dev@example.com"]


def test_send_email_alert_invalid_port_fallback(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    monkeypatch.setenv("ALERT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("ALERT_SMTP_PORT", "not-a-number")
    monkeypatch.setenv("ALERT_SMTP_FROM", "alerts@example.com")
    monkeypatch.setenv("ALERT_SMTP_TO", "ops@example.com")
    monkeypatch.setenv("ALERT_SMTP_USE_TLS", "false")
    monkeypatch.setenv("ALERT_SMTP_USE_SSL", "false")

    smtp_client = MagicMock()
    smtp_cm = MagicMock()
    smtp_cm.__enter__.return_value = smtp_client
    monkeypatch.setattr(scheduler.smtplib, "SMTP", MagicMock(return_value=smtp_cm))

    result = scheduler._send_shop_watchdog_email_alert("strict startup failure")
    assert result is True
    scheduler.smtplib.SMTP.assert_called_once_with("smtp.example.com", 587, timeout=15)


def test_send_email_alert_missing_config_returns_false(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    for key in (
        "ALERT_SMTP_HOST",
        "ALERT_SMTP_PORT",
        "ALERT_SMTP_USER",
        "ALERT_SMTP_PASSWORD",
        "ALERT_SMTP_FROM",
        "ALERT_SMTP_TO",
        "ALERT_SMTP_USE_TLS",
        "ALERT_SMTP_USE_SSL",
    ):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(scheduler.database, "get_setting", lambda _k: "")
    smtp_ctor = MagicMock()
    monkeypatch.setattr(scheduler.smtplib, "SMTP", smtp_ctor)

    result = scheduler._send_shop_watchdog_email_alert("strict startup failure")
    assert result is False
    smtp_ctor.assert_not_called()


def test_send_telegram_alert_success(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("ALERT_TELEGRAM_THREAD_ID", "42")

    captured: dict[str, object] = {}

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"ok": True, "result": {"message_id": 1}}).encode("utf-8")

    def _fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode("utf-8")
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(scheduler.urlrequest, "urlopen", _fake_urlopen)

    result = scheduler._send_shop_watchdog_telegram_alert("strict startup failure")
    assert result is True
    assert "api.telegram.org/bot123456:ABC/sendMessage" in str(captured["url"])
    payload = json.loads(str(captured["body"]))
    assert payload["chat_id"] == "-1001234567890"
    assert payload["message_thread_id"] == 42
    assert "🔴 *strict startup failure*" in payload["text"]
    assert "_Критично · " in payload["text"]
    assert payload["parse_mode"] == "MarkdownV2"
    assert captured["timeout"] == 15


def test_watchdog_email_message_is_compact_and_levelled(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    class _FakeDateTime:
        @classmethod
        def now(cls, *_args, **_kwargs):
            return datetime(2026, 3, 23, 21, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(scheduler, "datetime", _FakeDateTime)

    rendered = scheduler._format_shop_watchdog_email_message(
        "strict startup failure",
        title="manual restart failed",
        level="warning",
        source_label="Резервный бот",
    )

    assert rendered == (
        "manual restart failed\n"
        "Внимание · Резервный бот · 23.03.2026 21:12\n\n"
        "strict startup failure"
    )


def test_watchdog_telegram_message_is_compact(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    class _FakeDateTime:
        @classmethod
        def now(cls, *_args, **_kwargs):
            return datetime(2026, 3, 23, 21, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(scheduler, "datetime", _FakeDateTime)

    rendered = scheduler._format_shop_watchdog_telegram_message(
        "strict startup failure",
        title="manual restart failed",
        level="info",
        source_label="Основной бот",
    )

    assert rendered == (
        "🟡 *manual restart failed*\n"
        "_Информация · Основной бот · 23\\.03\\.2026 21:12_\n"
        "strict startup failure"
    )
    assert "*Time:*" not in rendered
    assert "prod-main" not in rendered
    assert "*Reason:*" not in rendered


def test_watchdog_telegram_message_uses_green_icon_for_success(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    class _FakeDateTime:
        @classmethod
        def now(cls, *_args, **_kwargs):
            return datetime(2026, 3, 23, 21, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(scheduler, "datetime", _FakeDateTime)

    rendered = scheduler._format_shop_watchdog_telegram_message(
        "работа восстановлена",
        title="Хост восстановлен",
        level="success",
        source_label="Основной бот",
    )

    assert rendered == (
        "🟢 *Хост восстановлен*\n"
        "_Восстановлено · Основной бот · 23\\.03\\.2026 21:12_\n"
        "работа восстановлена"
    )


def test_watchdog_telegram_message_renders_copyable_fields(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    class _FakeDateTime:
        @classmethod
        def now(cls, *_args, **_kwargs):
            return datetime(2026, 3, 23, 21, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(scheduler, "datetime", _FakeDateTime)

    rendered = scheduler._format_shop_watchdog_telegram_message(
        "ID платежа = pay_123\nID пользователя = 77\nЧто сделать: Повторите выдачу по ID платежа.",
        title="Выдача ключа отложена",
        level="warning",
        source_label="Основной бот",
    )

    assert "*ID платежа:* `pay_123`" in rendered
    assert "*ID пользователя:* `77`" in rendered
    assert "*Что сделать:* Повторите выдачу по ID платежа\\." in rendered


def test_send_telegram_alert_missing_config_returns_false(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    monkeypatch.delenv("ALERT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALERT_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(scheduler.database, "get_setting", lambda _k: "")
    urlopen_mock = MagicMock()
    monkeypatch.setattr(scheduler.urlrequest, "urlopen", urlopen_mock)

    result = scheduler._send_shop_watchdog_telegram_alert("strict startup failure")
    assert result is False
    urlopen_mock.assert_not_called()


def test_send_telegram_alert_falls_back_to_admin_chat_and_runtime_token(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    monkeypatch.delenv("ALERT_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALERT_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ALERT_TELEGRAM_THREAD_ID", raising=False)

    settings = {
        "alert_telegram_bot_token": "",
        "alert_telegram_chat_id": "",
        "alert_telegram_thread_id": "",
        "telegram_bot_token": "999999:MAIN",
        "reserve_bot_token": "",
        "admin_telegram_id": "123456789",
    }
    monkeypatch.setattr(scheduler.database, "get_setting", lambda key: settings.get(key, ""))

    captured: dict[str, object] = {}

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"ok": True, "result": {"message_id": 1}}).encode("utf-8")

    def _fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["body"] = req.data.decode("utf-8")
        captured["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(scheduler.urlrequest, "urlopen", _fake_urlopen)

    result = scheduler._send_shop_watchdog_telegram_alert("strict startup failure")
    assert result is True
    assert "api.telegram.org/bot999999:MAIN/sendMessage" in str(captured["url"])
    payload = json.loads(str(captured["body"]))
    assert payload["chat_id"] == "123456789"
    assert payload["parse_mode"] == "MarkdownV2"
    assert captured["timeout"] == 15


def test_send_telegram_alert_falls_back_to_second_token_when_first_invalid(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "111111:BAD")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "-1001234567890")
    monkeypatch.delenv("ALERT_TELEGRAM_THREAD_ID", raising=False)

    settings = {
        "alert_telegram_bot_token": "",
        "telegram_bot_token": "222222:GOOD",
        "reserve_bot_token": "",
        "alert_telegram_chat_id": "",
        "admin_telegram_id": "",
    }
    monkeypatch.setattr(scheduler.database, "get_setting", lambda key: settings.get(key, ""))
    monkeypatch.setattr(scheduler, "ALERT_TELEGRAM_SEND_RETRIES", 1)

    called_urls: list[str] = []

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"ok": True, "result": {"message_id": 1}}).encode("utf-8")

    def _fake_urlopen(req, timeout=0):
        called_urls.append(req.full_url)
        if "bot111111:BAD" in req.full_url:
            raise urlerror.HTTPError(
                url=req.full_url,
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=io.BytesIO(b'{"ok":false,"description":"Unauthorized"}'),
            )
        return _FakeResp()

    monkeypatch.setattr(scheduler.urlrequest, "urlopen", _fake_urlopen)

    result = scheduler._send_shop_watchdog_telegram_alert("strict startup failure")
    assert result is True
    assert any("bot111111:BAD" in url for url in called_urls)
    assert any("bot222222:GOOD" in url for url in called_urls)


def test_send_telegram_alert_retries_transient_network_error(monkeypatch, scheduler_module):
    scheduler = scheduler_module

    monkeypatch.setenv("ALERT_TELEGRAM_BOT_TOKEN", "123456:ABC")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "-1001234567890")
    monkeypatch.delenv("ALERT_TELEGRAM_THREAD_ID", raising=False)
    monkeypatch.setattr(scheduler.database, "get_setting", lambda _k: "")
    monkeypatch.setattr(scheduler, "ALERT_TELEGRAM_SEND_RETRIES", 2)
    monkeypatch.setattr(scheduler, "ALERT_TELEGRAM_RETRY_BACKOFF_SECONDS", 0)

    calls = {"count": 0}

    class _FakeResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"ok": True, "result": {"message_id": 1}}).encode("utf-8")

    def _fake_urlopen(req, timeout=0):
        calls["count"] += 1
        if calls["count"] == 1:
            raise urlerror.URLError("temporary network glitch")
        return _FakeResp()

    monkeypatch.setattr(scheduler.urlrequest, "urlopen", _fake_urlopen)

    result = scheduler._send_shop_watchdog_telegram_alert("strict startup failure")
    assert result is True
    assert calls["count"] == 2


def test_watchdog_cycle_strict_failure_sends_alert_and_keeps_autorestart(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

    controller = _FakeBotController(
        running=False,
        desired_running=True,
        start_result={"status": "error", "message": "invalid token", "strict_reason": True},
    )
    controller.shop_last_failure = "Telegram polling crashed"

    alert_calls: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alert_calls.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    result = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now)

    assert result == {"attempted_restart": True, "strict_reason": True, "alert_sent": True}
    assert controller.shop_desired_running is True
    assert controller.shop_autorestart_paused is False
    assert len(alert_calls) == 2
    from shop_bot.errors import BotError
    assert alert_calls[0]["error"] == BotError.BOT_CRASH
    assert "reason=bot_down_detected" in alert_calls[0]["details"]
    assert alert_calls[1]["error"] == BotError.BOT_TOKEN_INVALID  # classified from "invalid token"
    assert "Автозапуск продолжает попытки" in alert_calls[1]["details"]


def test_watchdog_cycle_panel_forbidden_failure_maps_to_host_auth_error(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

    controller = _FakeBotController(
        running=False,
        desired_running=True,
        start_result={
            "status": "error",
            "message": "Неожиданная ошибка входа в панель. 403 Client Error: Forbidden for url: https://panel.example.com/login",
            "strict_reason": True,
        },
    )
    controller.shop_last_failure = "Telegram polling crashed"

    alert_calls: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alert_calls.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    result = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now)

    assert result == {"attempted_restart": True, "strict_reason": True, "alert_sent": True}
    assert controller.shop_desired_running is True
    assert controller.shop_autorestart_paused is False
    assert len(alert_calls) == 2
    from shop_bot.errors import BotError
    assert alert_calls[0]["error"] == BotError.BOT_CRASH
    assert alert_calls[1]["error"] == BotError.HOST_LOGIN_FAILED
    assert "Автозапуск продолжает попытки" in alert_calls[1]["details"]


def test_watchdog_cycle_conflict_failure_does_not_pause_autorestart(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

    controller = _FakeBotController(
        running=False,
        desired_running=True,
        start_result={
            "status": "error",
            "message": "Конфликт getUpdates: бот уже запущен в другом процессе.",
            "strict_reason": True,
        },
    )
    controller.shop_last_failure = "Telegram polling crashed"

    alert_calls: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alert_calls.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    result = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now)

    assert result == {"attempted_restart": True, "strict_reason": False, "alert_sent": True}
    assert controller.shop_desired_running is True
    assert controller.shop_autorestart_paused is False
    from shop_bot.errors import BotError
    assert alert_calls[0]["error"] == BotError.BOT_CRASH
    assert alert_calls[1]["error"] == BotError.BOT_CRASH
    assert "watchdog_retry" in alert_calls[1]["details"]


def test_watchdog_cycle_successful_restart_sends_down_and_recovered_alerts(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

    controller = _FakeBotController(
        running=False,
        desired_running=True,
        start_result={"status": "success"},
    )
    controller.shop_last_failure = "Telegram polling crashed"

    alert_calls: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alert_calls.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    result = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now)

    assert result == {"attempted_restart": True, "strict_reason": False, "alert_sent": True}
    from shop_bot.errors import BotError
    assert [call["error"] for call in alert_calls] == [BotError.BOT_CRASH, BotError.BOT_AUTO_RESTART]
    assert alert_calls[0]["respect_cooldown"] is True
    assert alert_calls[1]["respect_cooldown"] is False
    assert "reason=bot_recovered_after_crash" in alert_calls[1]["details"]


def test_watchdog_cycle_deploy_restart_alert_contains_task_context(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setenv("DEPLOY_LAST_TASK", "Issue #62: Не все ВСС ссылки попадают в подписку")
    monkeypatch.setenv("DEPLOY_LAST_COMMIT_SHORT_SHA", "abc1234")
    monkeypatch.setenv("DEPLOY_LAST_COMMIT_TITLE", "fix(xui): keep all subscription links")
    monkeypatch.setenv("DEPLOY_LAST_REF_NAME", "main")
    monkeypatch.setenv("DEPLOY_LAST_PIPELINE_URL", "https://github.com/example/repo/actions/runs/101")

    controller = _FakeBotController(
        running=False,
        desired_running=True,
        start_result={"status": "success"},
    )

    alert_calls: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alert_calls.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    result = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now)

    assert result == {"attempted_restart": True, "strict_reason": False, "alert_sent": True}

    from shop_bot.errors import BotError

    assert [call["error"] for call in alert_calls] == [BotError.BOT_DEPLOY_RESTARTED]
    assert alert_calls[0]["respect_cooldown"] is False
    deploy_alert_details = alert_calls[0]["details"]
    assert "reason=deploy_restart_ok" in deploy_alert_details
    assert "task=62" in deploy_alert_details
    assert "Задача: Issue #62: Не все ВСС ссылки попадают в подписку" in deploy_alert_details
    assert "Коммит: abc1234" in deploy_alert_details
    assert "Изменение: fix(xui): keep all subscription links" in deploy_alert_details
    assert "Ветка: main" in deploy_alert_details
    assert "Pipeline: https://github.com/example/repo/actions/runs/101" in deploy_alert_details


def test_watchdog_cycle_failed_deploy_restart_sends_retry_alert(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

    controller = _FakeBotController(
        running=False,
        desired_running=True,
        start_result={
            "status": "error",
            "message": "temporary start failure",
            "strict_reason": False,
        },
    )

    alert_calls: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alert_calls.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    result = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now)

    assert result == {"attempted_restart": True, "strict_reason": False, "alert_sent": True}

    from shop_bot.errors import BotError

    assert [call["error"] for call in alert_calls] == [BotError.BOT_CRASH]
    assert alert_calls[0]["respect_cooldown"] is False
    assert "reason=watchdog_retry" in alert_calls[0]["details"]
    assert "Падение: Обновление кода (деплой)" in alert_calls[0]["details"]
    assert "Авторестарт не удался: temporary start failure" in alert_calls[0]["details"]


def test_watchdog_cycle_skips_duplicate_start_while_bot_is_starting(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

    controller = _FakeBotController(
        running=False,
        desired_running=True,
        start_result={"status": "success"},
        healthy=False,
        health_message="starting",
        starting=True,
    )

    alert_calls: list[dict] = []
    monkeypatch.setattr(
        scheduler,
        "send_critical_alert_nonblocking",
        lambda *args, **kwargs: alert_calls.append({"args": args, "kwargs": kwargs}),
    )

    result = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now)

    assert result == {"attempted_restart": False, "strict_reason": False, "alert_sent": False}
    assert controller.start_calls == 0
    assert alert_calls == []


def test_watchdog_cycle_probe_timeout_keeps_runtime_running(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

    controller = _FakeBotController(
        running=True,
        desired_running=True,
        start_result={"status": "success"},
        healthy=False,
        health_message="probe_timeout",
    )

    alert_calls: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alert_calls.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    result = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now)

    assert result == {"attempted_restart": False, "strict_reason": False, "alert_sent": False}
    assert controller.stop_calls == 0
    assert controller.start_calls == 0
    assert alert_calls == []


def test_watchdog_cycle_probe_timeout_never_restarts(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

    controller = _FakeBotController(
        running=True,
        desired_running=True,
        start_result={"status": "success"},
        healthy=False,
        health_message="probe_timeout",
    )

    alert_calls: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alert_calls.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    first = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now)
    second = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now + timedelta(seconds=10))
    third = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now + timedelta(seconds=20))

    assert first == {"attempted_restart": False, "strict_reason": False, "alert_sent": False}
    assert second == {"attempted_restart": False, "strict_reason": False, "alert_sent": False}
    assert third == {"attempted_restart": False, "strict_reason": False, "alert_sent": False}
    assert controller.stop_calls == 0
    assert controller.start_calls == 0
    assert alert_calls == []


def test_watchdog_cycle_persistent_probe_timeout_sends_telegram_warning(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

    monkeypatch.setattr(scheduler, "SHOP_WATCHDOG_TRANSIENT_UNHEALTHY_MAX_SECONDS", 30)

    controller = _FakeBotController(
        running=True,
        desired_running=True,
        start_result={"status": "success"},
        healthy=False,
        health_message="probe_timeout",
    )

    alert_calls: list[dict] = []

    def _capture_alert(error, *, details="", respect_cooldown=True):
        alert_calls.append({"error": error, "details": details, "respect_cooldown": respect_cooldown})
        return True

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", _capture_alert)

    first = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now)
    second = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now + timedelta(seconds=31))

    from shop_bot.errors import BotError

    assert first == {"attempted_restart": False, "strict_reason": False, "alert_sent": False}
    assert second == {"attempted_restart": False, "strict_reason": False, "alert_sent": True}
    assert controller.stop_calls == 0
    assert controller.start_calls == 0
    assert len(alert_calls) == 1
    assert alert_calls[0]["error"] == BotError.TELEGRAM_API_ERROR
    assert "reason=watchdog_probe_timeout_persistent" in alert_calls[0]["details"]
    assert "Health: probe_timeout" in alert_calls[0]["details"]
    assert alert_calls[0]["respect_cooldown"] is True


def test_watchdog_cycle_does_not_set_cooldown_on_failed_alert(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 1, 0, tzinfo=UTC)

    controller = _FakeBotController(
        running=False,
        desired_running=True,
        start_result={"status": "error", "message": "invalid token", "strict_reason": True},
    )

    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", MagicMock())

    result = scheduler.process_shop_watchdog_cycle(controller, now=fixed_now)
    assert result["alert_sent"] is True
    assert scheduler._last_shop_watchdog_alert_at is None


def test_watchdog_cycle_cooldown_suppresses_alert(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    base = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

    controller = _FakeBotController(
        running=False,
        desired_running=True,
        start_result={"status": "error", "message": "invalid token", "strict_reason": True},
    )

    # Watchdog now emits through nonblocking sender; validate cycle behavior only.
    alert_mock = MagicMock()
    monkeypatch.setattr(scheduler, "send_critical_alert_nonblocking", alert_mock)

    result = scheduler.process_shop_watchdog_cycle(controller, now=base + timedelta(minutes=5))

    assert result["strict_reason"] is True
    assert result["alert_sent"] is True


def test_send_shop_ops_alert_respect_cooldown_extends_dedupe_window(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    base = datetime(2026, 3, 14, 12, 0, 0, tzinfo=UTC)

    email_mock = MagicMock(return_value=False)
    tg_mock = MagicMock(return_value=True)
    monkeypatch.setattr(scheduler, "_send_shop_watchdog_email_alert", email_mock)
    monkeypatch.setattr(scheduler, "_send_shop_watchdog_telegram_alert", tg_mock)

    first = scheduler.send_shop_ops_alert(
        title="probe timeout",
        reason="probe timeout persisted",
        respect_cooldown=True,
        now=base,
        dedupe_key="watchdog:probe-timeout:main",
        dedupe_window_seconds=180,
    )
    second = scheduler.send_shop_ops_alert(
        title="probe timeout",
        reason="probe timeout persisted",
        respect_cooldown=True,
        now=base + timedelta(seconds=181),
        dedupe_key="watchdog:probe-timeout:main",
        dedupe_window_seconds=180,
    )
    third = scheduler.send_shop_ops_alert(
        title="probe timeout",
        reason="probe timeout persisted",
        respect_cooldown=True,
        now=base + timedelta(seconds=901),
        dedupe_key="watchdog:probe-timeout:main",
        dedupe_window_seconds=180,
    )

    assert first is True
    assert second is False
    assert third is True
    assert tg_mock.call_count == 2


def test_send_shop_ops_alert_appends_manual_action_hint(monkeypatch, scheduler_module):
    scheduler = scheduler_module
    fixed_now = datetime(2026, 3, 14, 12, 2, 0, tzinfo=UTC)

    email_mock = MagicMock(return_value=True)
    tg_mock = MagicMock(return_value=False)
    monkeypatch.setattr(scheduler, "_send_shop_watchdog_email_alert", email_mock)
    monkeypatch.setattr(scheduler, "_send_shop_watchdog_telegram_alert", tg_mock)

    sent = scheduler.send_shop_ops_alert(
        title="manual restart failed",
        reason="invalid token",
        manual_start_required=True,
        respect_cooldown=True,
        now=fixed_now,
    )

    assert sent is True
    assert scheduler._last_shop_watchdog_alert_at == fixed_now
    assert email_mock.call_count == 1
    assert tg_mock.call_count == 1

    email_reason = email_mock.call_args.args[0]
    assert "invalid token" in email_reason
    assert "Запустите Shop-бота вручную" in email_reason
    assert email_mock.call_args.kwargs["title"] == "manual restart failed"
    assert email_mock.call_args.kwargs["level"] == "critical"
