import pytest

pytest.importorskip("aiogram")

from shop_bot.bot import handlers
from shop_bot.bot.middlewares import (
    _DEFAULT_MAIN_SERVICE_MESSAGE,
    _DEFAULT_MAIN_SERVICE_RESERVE_BOT_URL,
)


def test_main_purchase_redirect_uses_default_text(monkeypatch):
    monkeypatch.delenv("MAIN_SERVICE_MODE_MESSAGE", raising=False)
    monkeypatch.setattr(handlers, "get_setting", lambda _key: "")

    assert handlers._build_main_purchase_redirect_text() == _DEFAULT_MAIN_SERVICE_MESSAGE


def test_main_purchase_redirect_keyboard_uses_reserve_username(monkeypatch):
    settings = {
        "main_service_mode_reserve_bot_url": "",
        "reserve_bot_username": "@probelvpnbot",
    }
    monkeypatch.delenv("MAIN_SERVICE_MODE_RESERVE_BOT_URL", raising=False)
    monkeypatch.setattr(handlers, "get_setting", lambda key: settings.get(key, ""))

    markup = handlers._build_main_purchase_redirect_keyboard()
    button = markup.inline_keyboard[0][0]

    assert button.text == "🔁 Перейти в бота"
    assert button.url == "https://t.me/probelvpnbot"


def test_main_purchase_redirect_keyboard_falls_back_to_default_url(monkeypatch):
    monkeypatch.delenv("MAIN_SERVICE_MODE_RESERVE_BOT_URL", raising=False)
    monkeypatch.setattr(handlers, "get_setting", lambda _key: "")

    markup = handlers._build_main_purchase_redirect_keyboard()

    assert markup.inline_keyboard[0][0].url == _DEFAULT_MAIN_SERVICE_RESERVE_BOT_URL


def test_main_purchase_disabled_depends_on_runtime_and_toggle(monkeypatch):
    monkeypatch.setattr(handlers, "BOT_SOURCE", "main")
    monkeypatch.setattr(handlers, "get_setting", lambda _key: "false")
    assert handlers._main_payments_disabled()

    monkeypatch.setattr(handlers, "get_setting", lambda _key: "true")
    assert not handlers._main_payments_disabled()

    monkeypatch.setattr(handlers, "BOT_SOURCE", "reserve")
    monkeypatch.setattr(handlers, "get_setting", lambda _key: "false")
    assert not handlers._main_payments_disabled()
