from __future__ import annotations

import asyncio
from types import SimpleNamespace

from shop_bot.bot import handlers


class _FakeConnector:
    connected = True
    account = SimpleNamespace(address="test-address")

    def __init__(self) -> None:
        self.sent_payloads: list[dict] = []

    async def send_transaction(self, payload: dict) -> None:
        self.sent_payloads.append(payload)


def test_ton_listener_does_not_clear_newer_session_on_old_finally():
    user_id = 9001
    old_token = object()
    new_token = object()
    new_connector = _FakeConnector()
    new_task = object()

    handlers._listener_tokens[user_id] = new_token
    handlers._user_connectors[user_id] = new_connector
    handlers._listener_tasks[user_id] = new_task

    asyncio.run(handlers._listener_task(_FakeConnector(), user_id, {"old": True}, old_token))

    assert handlers._listener_tokens[user_id] is new_token
    assert handlers._user_connectors[user_id] is new_connector
    assert handlers._listener_tasks[user_id] is new_task

    handlers._listener_tokens.pop(user_id, None)
    handlers._user_connectors.pop(user_id, None)
    handlers._listener_tasks.pop(user_id, None)


def test_ton_listener_clears_current_session_on_finally():
    user_id = 9002
    token = object()

    handlers._listener_tokens[user_id] = token
    handlers._user_connectors[user_id] = _FakeConnector()
    handlers._listener_tasks[user_id] = object()

    asyncio.run(handlers._listener_task(_FakeConnector(), user_id, {"current": True}, token))

    assert user_id not in handlers._listener_tokens
    assert user_id not in handlers._user_connectors
    assert user_id not in handlers._listener_tasks
