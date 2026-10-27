import pytest

from datetime import datetime
from zoneinfo import ZoneInfo

pytest.importorskip("aiogram")

from shop_bot.bot import keyboards


MSK = ZoneInfo("Europe/Moscow")


def _button_texts(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_support_contact_visibility_is_always_enabled():
    assert keyboards.is_support_contact_visible(datetime(2026, 3, 26, 9, 59, tzinfo=MSK)) is True
    assert keyboards.is_support_contact_visible(datetime(2026, 3, 26, 10, 0, tzinfo=MSK)) is True
    assert keyboards.is_support_contact_visible(datetime(2026, 3, 26, 19, 59, tzinfo=MSK)) is True
    assert keyboards.is_support_contact_visible(datetime(2026, 3, 26, 20, 0, tzinfo=MSK)) is True


def test_support_keyboard_keeps_contact_button_when_support_is_closed():
    markup = keyboards.create_support_keyboard(
        "https://t.me/example_support",
        contact_visible=False,
    )

    assert _button_texts(markup) == ["🆘 Написать в поддержку", "⬅️ Назад в меню"]


def test_support_keyboard_shows_contact_button_when_support_is_open():
    markup = keyboards.create_support_keyboard(
        "https://t.me/example_support",
        contact_visible=True,
    )

    assert _button_texts(markup) == ["🆘 Написать в поддержку", "⬅️ Назад в меню"]
