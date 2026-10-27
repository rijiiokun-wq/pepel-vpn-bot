import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("aiogram")

from shop_bot.bot import handlers


class _FakeMessage:
    def __init__(self, user_id: int):
        self.chat = SimpleNamespace(id=user_id)
        self.answers: list[dict[str, object]] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append({"text": text, **kwargs})


async def _purchase_available() -> dict[str, object]:
    return {
        "standard_allowed": True,
        "super_allowed": True,
        "standard_message": "",
        "super_message": "",
        "common_message": "",
        "reason": "",
    }


def test_reserve_main_menu_keeps_admin_actions_when_main_is_unavailable(monkeypatch):
    admin_id = 42
    monkeypatch.setattr(handlers, "BOT_SOURCE", "reserve")
    monkeypatch.setattr(handlers, "ADMIN_ID", str(admin_id))
    monkeypatch.setattr(handlers, "get_setting", lambda _key: None)
    monkeypatch.setattr(handlers.referrals, "get_policy", lambda: {"enabled": False})
    monkeypatch.setattr(
        handlers,
        "_sync_load_main_menu_state",
        lambda user_id: (
            {"telegram_id": user_id, "trial_used": True, "trial_super_used": True},
            [],
            [],
            None,
        ),
    )
    monkeypatch.setattr(handlers, "_resolve_purchase_availability", _purchase_available)
    monkeypatch.setattr(handlers.keyboards, "is_super_key_enabled", lambda: True)

    message = _FakeMessage(admin_id)

    asyncio.run(handlers._show_main_menu_impl(message))

    assert message.answers
    assert "Главное меню" in message.answers[0]["text"]
    assert "Основной сервер временно недоступен" not in message.answers[0]["text"]

    reply_markup = message.answers[0]["reply_markup"]
    callback_data = [
        button.callback_data
        for row in reply_markup.inline_keyboard
        for button in row
    ]
    assert "start_broadcast" in callback_data


def test_main_menu_text_filter_accepts_home_button_variants():
    assert handlers._is_main_menu_request_text("🏠 Главное меню")
    assert handlers._is_main_menu_request_text("В меню")  # noqa: RUF001
    assert handlers._is_main_menu_request_text("/menu")
    assert not handlers._is_main_menu_request_text("Купить ключ")


def test_main_menu_incident_buttons_use_short_callback_data(monkeypatch):
    long_incident_id = "incident-" + ("x" * 120)
    monkeypatch.setattr(
        handlers,
        "_sync_load_main_menu_state",
        lambda user_id: (
            {"telegram_id": user_id, "trial_used": True, "trial_super_used": True},
            [],
            [{"incident_id": long_incident_id, "message": "Ключ обновлен"}],
            123,
        ),
    )
    monkeypatch.setattr(handlers, "_resolve_purchase_availability", _purchase_available)

    message = _FakeMessage(42)

    asyncio.run(handlers._show_main_menu_impl(message))

    callbacks = [
        button.callback_data
        for row in message.answers[0]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert callbacks == ["incident_open_key", "ack_incident"]
    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)
