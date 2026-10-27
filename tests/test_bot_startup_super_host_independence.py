from __future__ import annotations

import concurrent.futures
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


class _LoopStub:
    def is_running(self) -> bool:
        return True


class _TimeoutFuture:
    def result(self, timeout=None):
        raise concurrent.futures.TimeoutError


class _RaisingFuture:
    def __init__(self, exc: Exception):
        self._exc = exc

    def result(self, timeout=None):
        raise self._exc


class _DispatcherStub:
    def __init__(self):
        middleware_chain = MagicMock()
        middleware_chain.middleware = MagicMock()
        self.update = MagicMock()
        self.message = middleware_chain
        self.callback_query = middleware_chain
        self.include_router = MagicMock()


@pytest.fixture
def bot_controller_module(monkeypatch):
    from shop_bot import bot_controller

    def _run_coroutine_threadsafe(coro, _loop):
        coro.close()
        return _TimeoutFuture()

    monkeypatch.setattr(bot_controller, "BOT_SOURCE", "main")
    monkeypatch.setattr(bot_controller, "Bot", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(bot_controller, "Dispatcher", _DispatcherStub)
    monkeypatch.setattr(bot_controller, "BanMiddleware", MagicMock(return_value=object()))
    monkeypatch.setattr(bot_controller, "get_user_router", MagicMock(return_value=object()))
    monkeypatch.setattr(
        bot_controller.asyncio,
        "run_coroutine_threadsafe",
        _run_coroutine_threadsafe,
    )
    return bot_controller


def test_start_shop_bot_does_not_depend_on_super_host_availability(monkeypatch, bot_controller_module):
    bot_controller = bot_controller_module

    settings = {
        "telegram_bot_token": "123:ABC",
        "telegram_bot_username": "shop_bot",
        "admin_telegram_id": "1",
        "super_key_host_name": "SUPER",
        "yookassa_shop_id": "",
        "yookassa_secret_key": "",
        "platega_api_key": "",
        "platega_shop_id": "",
        "cryptobot_token": "",
        "heleket_merchant_id": "",
        "heleket_api_key": "",
        "ton_wallet_address": "",
        "tonapi_key": "",
    }
    monkeypatch.setattr(bot_controller.database, "get_setting", lambda key: settings.get(key, ""))

    controller = bot_controller.BotController()
    controller.set_loop(_LoopStub())

    result = controller.start_shop_bot()

    assert result["status"] == "success"
    assert controller.shop_is_running is True
    assert controller.shop_last_failure is None


@pytest.mark.parametrize(
    ("toggle_value", "expected_enabled"),
    [
        ("false", False),
        ("true", True),
    ],
)
def test_start_shop_bot_respects_main_payments_admin_toggle(
    monkeypatch,
    bot_controller_module,
    toggle_value,
    expected_enabled,
):
    bot_controller = bot_controller_module

    settings = {
        "telegram_bot_token": "123:ABC",
        "telegram_bot_username": "shop_bot",
        "admin_telegram_id": "1",
        "super_key_host_name": "SUPER",
        "yookassa_shop_id": "shop-id",
        "yookassa_secret_key": "secret",
        "yookassa_payments_enabled": toggle_value,
        "paypear_shop_id": "paypear-shop",
        "paypear_secret_key": "paypear-secret",
        "platega_api_key": "",
        "platega_shop_id": "",
        "cryptobot_token": "cryptobot-token",
        "heleket_merchant_id": "",
        "heleket_api_key": "",
        "ton_wallet_address": "",
        "tonapi_key": "",
    }
    monkeypatch.setattr(bot_controller.database, "get_setting", lambda key: settings.get(key, ""))

    controller = bot_controller.BotController()
    controller.set_loop(_LoopStub())

    result = controller.start_shop_bot()

    assert result["status"] == "success"
    assert bot_controller.handlers.PAYMENT_METHODS["yookassa"] is expected_enabled
    assert bot_controller.handlers.PAYMENT_METHODS["paypear"] is expected_enabled
    assert bot_controller.handlers.PAYMENT_METHODS["cryptobot"] is expected_enabled


def test_startup_probe_conflict_allows_manual_force_start(monkeypatch, bot_controller_module):
    bot_controller = bot_controller_module

    class _DummyConflictError(Exception):
        pass

    def _run_coroutine_threadsafe(coro, _loop):
        coro.close()
        return _RaisingFuture(_DummyConflictError("conflict"))

    monkeypatch.setattr(bot_controller, "TelegramConflictError", _DummyConflictError)
    monkeypatch.setattr(bot_controller.asyncio, "run_coroutine_threadsafe", _run_coroutine_threadsafe)

    controller = bot_controller.BotController()
    controller.set_loop(_LoopStub())
    controller._loop_thread_id = -1

    probe_error = controller._run_startup_telegram_probe(MagicMock(), ignore_conflict=True)

    assert probe_error is None


def test_startup_probe_conflict_blocks_non_force_start(monkeypatch, bot_controller_module):
    bot_controller = bot_controller_module

    class _DummyConflictError(Exception):
        pass

    def _run_coroutine_threadsafe(coro, _loop):
        coro.close()
        return _RaisingFuture(_DummyConflictError("conflict"))

    monkeypatch.setattr(bot_controller, "TelegramConflictError", _DummyConflictError)
    monkeypatch.setattr(bot_controller.asyncio, "run_coroutine_threadsafe", _run_coroutine_threadsafe)

    controller = bot_controller.BotController()
    controller.set_loop(_LoopStub())
    controller._loop_thread_id = -1

    probe_error = controller._run_startup_telegram_probe(MagicMock(), ignore_conflict=False)

    assert probe_error is not None
    assert probe_error["status"] == "error"
    assert probe_error["strict_reason"] is False
    assert "Конфликт getUpdates" in probe_error["message"]


def test_startup_probe_skips_threadsafe_call_from_event_loop_thread(monkeypatch, bot_controller_module):
    bot_controller = bot_controller_module

    def _fail_if_called(*_args, **_kwargs):
        pytest.fail("run_coroutine_threadsafe must not be used from loop thread")

    monkeypatch.setattr(bot_controller.asyncio, "run_coroutine_threadsafe", _fail_if_called)

    controller = bot_controller.BotController()
    controller.set_loop(_LoopStub())
    controller._loop_thread_id = threading.get_ident()

    assert controller._run_startup_telegram_probe(MagicMock(), ignore_conflict=False) is None


def test_start_shop_bot_rejects_duplicate_start_while_starting(bot_controller_module):
    bot_controller = bot_controller_module

    controller = bot_controller.BotController()
    controller.set_loop(_LoopStub())
    controller._shop_is_starting = True

    result = controller.start_shop_bot()

    assert result["status"] == "error"
    assert "запускается" in result["message"]


def test_start_shop_bot_manual_uses_force_conflict_mode(monkeypatch, bot_controller_module):
    bot_controller = bot_controller_module

    settings = {
        "telegram_bot_token": "123:ABC",
        "telegram_bot_username": "shop_bot",
        "admin_telegram_id": "1",
        "super_key_host_name": "SUPER",
        "yookassa_shop_id": "",
        "yookassa_secret_key": "",
        "platega_api_key": "",
        "platega_shop_id": "",
        "cryptobot_token": "",
        "heleket_merchant_id": "",
        "heleket_api_key": "",
        "ton_wallet_address": "",
        "tonapi_key": "",
    }
    monkeypatch.setattr(bot_controller.database, "get_setting", lambda key: settings.get(key, ""))

    captured: list[bool] = []

    def _capture_probe(self, bot, *, ignore_conflict=False):
        captured.append(ignore_conflict)
        return None

    monkeypatch.setattr(bot_controller.BotController, "_run_startup_telegram_probe", _capture_probe)

    controller = bot_controller.BotController()
    controller.set_loop(_LoopStub())

    result = controller.start_shop_bot()

    assert result["status"] == "success"
    assert captured == [True]
