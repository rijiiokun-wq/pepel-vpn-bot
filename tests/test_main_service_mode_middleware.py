from __future__ import annotations

import asyncio
from types import SimpleNamespace

from shop_bot.bot import middlewares


class _FakeMessage:
    def __init__(self, chat_type: str = "private"):
        self.chat = SimpleNamespace(id=123, type=chat_type)
        self.message_id = 456
        self.answers: list[dict] = []

    async def answer(self, text: str, **kwargs):
        self.answers.append({"text": text, "kwargs": kwargs})


class _FakeCallback:
    def __init__(self, data: str = "same_button"):
        self.data = data
        self.from_user = SimpleNamespace(id=1)
        self.message = _FakeMessage()
        self.inline_message_id = None
        self.answers: list[dict] = []

    async def answer(self, text: str | None = None, **kwargs):
        self.answers.append({"text": text, "kwargs": kwargs})


def test_main_service_mode_blocks_private_message(monkeypatch):
    monkeypatch.setattr(middlewares, "BOT_SOURCE", "main")
    monkeypatch.setattr(middlewares, "Message", _FakeMessage)

    async def _fake_payload():
        return True, "service text", "https://t.me/reserve_bot"

    monkeypatch.setattr(middlewares, "_load_main_service_mode_payload", _fake_payload)

    called = {"value": False}

    async def _handler(event, data):
        called["value"] = True
        return "ok"

    event = _FakeMessage()
    middleware = middlewares.MainServiceModeMiddleware()
    asyncio.run(middleware(_handler, event, {"event_from_user": SimpleNamespace(id=1)}))

    assert called["value"] is False
    assert len(event.answers) == 1
    assert event.answers[0]["text"] == "service text"
    assert event.answers[0]["kwargs"]["reply_markup"] is not None


def test_main_service_mode_allows_handler_when_disabled(monkeypatch):
    monkeypatch.setattr(middlewares, "BOT_SOURCE", "main")
    monkeypatch.setattr(middlewares, "Message", _FakeMessage)

    async def _fake_payload():
        return False, "", ""

    monkeypatch.setattr(middlewares, "_load_main_service_mode_payload", _fake_payload)

    called = {"value": False}

    async def _handler(event, data):
        called["value"] = True
        return "ok"

    event = _FakeMessage()
    middleware = middlewares.MainServiceModeMiddleware()
    asyncio.run(middleware(_handler, event, {"event_from_user": SimpleNamespace(id=1)}))

    assert called["value"] is True
    assert event.answers == []


def test_callback_debounce_suppresses_duplicate_in_flight_press(monkeypatch):
    monkeypatch.setattr(middlewares, "CallbackQuery", _FakeCallback)
    monkeypatch.setattr(middlewares, "Message", _FakeMessage)

    async def _run():
        middleware = middlewares.CallbackDebounceMiddleware(ttl_seconds=30, max_entries=16)
        calls = {"count": 0}

        async def _handler(event, data):
            calls["count"] += 1
            await asyncio.sleep(0.05)
            return "ok"

        first_event = _FakeCallback()
        second_event = _FakeCallback()
        first = asyncio.create_task(middleware(_handler, first_event, {}))
        await asyncio.sleep(0)
        duplicate_result = await middleware(_handler, second_event, {})
        first_result = await first

        return calls["count"], duplicate_result, first_result, second_event.answers

    calls, duplicate_result, first_result, duplicate_answers = asyncio.run(_run())

    assert calls == 1
    assert duplicate_result is None
    assert first_result == "ok"
    assert duplicate_answers[0]["text"] == "⏳ Уже обрабатываю, подождите."
    assert duplicate_answers[0]["kwargs"]["cache_time"] == 1


def test_callback_debounce_releases_key_after_handler(monkeypatch):
    monkeypatch.setattr(middlewares, "CallbackQuery", _FakeCallback)
    monkeypatch.setattr(middlewares, "Message", _FakeMessage)

    async def _run():
        middleware = middlewares.CallbackDebounceMiddleware(ttl_seconds=30, max_entries=16)
        calls = {"count": 0}

        async def _handler(event, data):
            calls["count"] += 1
            return "ok"

        first_result = await middleware(_handler, _FakeCallback(), {})
        second_result = await middleware(_handler, _FakeCallback(), {})
        return calls["count"], first_result, second_result

    calls, first_result, second_result = asyncio.run(_run())

    assert calls == 2
    assert first_result == "ok"
    assert second_result == "ok"


def test_main_service_mode_skipped_for_reserve_runtime(monkeypatch):
    monkeypatch.setattr(middlewares, "BOT_SOURCE", "reserve")
    monkeypatch.setattr(middlewares, "Message", _FakeMessage)

    called = {"value": False}

    async def _handler(event, data):
        called["value"] = True
        return "ok"

    event = _FakeMessage()
    middleware = middlewares.MainServiceModeMiddleware()
    asyncio.run(middleware(_handler, event, {"event_from_user": SimpleNamespace(id=1)}))

    assert called["value"] is True
    assert event.answers == []
