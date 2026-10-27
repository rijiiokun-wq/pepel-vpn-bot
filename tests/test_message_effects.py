import asyncio

import pytest

pytest.importorskip("aiogram")

from shop_bot.bot import message_effects


class DummyBot:
    def __init__(self):
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return kwargs


def test_send_effect_message_passes_message_effect_id(monkeypatch):
    monkeypatch.setattr(
        message_effects,
        "get_setting",
        lambda key: "5104841245755180586" if key == message_effects.WELCOME_MESSAGE_EFFECT_ID_SETTING else None,
    )
    bot = DummyBot()

    asyncio.run(
        message_effects.send_effect_message(
            bot,
            chat_id=123,
            text="hello",
            effect_setting_key=message_effects.WELCOME_MESSAGE_EFFECT_ID_SETTING,
        )
    )

    assert bot.calls[0]["message_effect_id"] == "5104841245755180586"


def test_send_effect_message_uses_default_effect_when_setting_empty(monkeypatch):
    monkeypatch.setattr(message_effects, "get_setting", lambda _key: "")
    bot = DummyBot()

    asyncio.run(
        message_effects.send_effect_message(
            bot,
            chat_id=123,
            text="hello",
            effect_setting_key=message_effects.WELCOME_MESSAGE_EFFECT_ID_SETTING,
        )
    )

    assert bot.calls[0]["message_effect_id"] == message_effects.DEFAULT_SCREENSHOT_EFFECT_ID


def test_send_effect_message_retries_without_effect_if_telegram_rejects(monkeypatch):
    class EffectRejected(Exception):
        pass

    class FailingOnceBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise EffectRejected("Bad Request: invalid message_effect_id")
            return kwargs

    monkeypatch.setattr(message_effects, "get_setting", lambda _key: "")
    monkeypatch.setattr(message_effects, "TelegramBadRequest", EffectRejected)
    bot = FailingOnceBot()

    asyncio.run(
        message_effects.send_effect_message(
            bot,
            chat_id=123,
            text="hello",
            effect_setting_key=message_effects.WELCOME_MESSAGE_EFFECT_ID_SETTING,
        )
    )

    assert len(bot.calls) == 2
    assert "message_effect_id" in bot.calls[0]
    assert "message_effect_id" not in bot.calls[1]


def test_send_effect_message_retries_default_when_custom_effect_rejected(monkeypatch):
    class EffectRejected(Exception):
        pass

    class FailingCustomBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs.get("message_effect_id") == "custom-stale-effect":
                raise EffectRejected("Bad Request: invalid message_effect_id")
            return kwargs

    monkeypatch.setattr(message_effects, "get_setting", lambda _key: "custom-stale-effect")
    monkeypatch.setattr(message_effects, "TelegramBadRequest", EffectRejected)
    bot = FailingCustomBot()

    asyncio.run(
        message_effects.send_effect_message(
            bot,
            chat_id=123,
            text="hello",
            effect_setting_key=message_effects.WELCOME_MESSAGE_EFFECT_ID_SETTING,
        )
    )

    assert len(bot.calls) == 2
    assert bot.calls[0]["message_effect_id"] == "custom-stale-effect"
    assert bot.calls[1]["message_effect_id"] == message_effects.DEFAULT_SCREENSHOT_EFFECT_ID


def test_send_effect_message_defaults_to_html_parse_mode(monkeypatch):
    monkeypatch.setattr(message_effects, "get_setting", lambda _key: "")
    bot = DummyBot()

    asyncio.run(
        message_effects.send_effect_message(
            bot,
            chat_id=123,
            text="<b>hello</b>",
        )
    )

    assert bot.calls[0]["parse_mode"] == "HTML"


def test_send_effect_message_keeps_explicit_parse_mode(monkeypatch):
    monkeypatch.setattr(message_effects, "get_setting", lambda _key: "")
    bot = DummyBot()

    asyncio.run(
        message_effects.send_effect_message(
            bot,
            chat_id=123,
            text="*hello*",
            parse_mode="Markdown",
        )
    )

    assert bot.calls[0]["parse_mode"] == "Markdown"
