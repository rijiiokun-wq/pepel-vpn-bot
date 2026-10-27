import pytest

pytest.importorskip("aiogram")

from shop_bot.bot import handlers


def test_build_support_text_returns_configured_text_verbatim_when_present():
    text = "Текст из веб-панели"

    assert handlers._build_support_text(text, support_contact_visible=False) == text
    assert handlers._build_support_text(text, support_contact_visible=True) == text


def test_build_support_text_falls_back_to_button_hint_when_hidden():
    assert handlers._build_support_text(None, support_contact_visible=False) == (
        "Для связи с поддержкой используйте кнопку ниже."  # noqa: RUF001
    )



def test_build_support_text_falls_back_to_button_hint_when_open():
    assert handlers._build_support_text(None, support_contact_visible=True) == (
        "Для связи с поддержкой используйте кнопку ниже."
    )
