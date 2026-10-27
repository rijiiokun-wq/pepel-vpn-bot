from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("aiogram")
pytest.importorskip("py3xui")

from shop_bot.bot import handlers


def test_trial_super_timeout_defaults_are_doubled():
    assert handlers.TRIAL_SUPER_ISSUE_TIMEOUT_SECONDS == 40.0
    assert handlers.TRIAL_ISSUE_QUICK_RETRY_ATTEMPT_TIMEOUT_SECONDS == 16.0


def test_trial_super_issuance_uses_single_fixed_target_expiry(monkeypatch):
    calls: list[dict] = []

    async def fake_create_or_update_key_on_host(**kwargs):
        calls.append(kwargs)
        return {
            "client_uuid": "uuid-trial",
            "email": kwargs["email"],
            "expiry_timestamp_ms": kwargs["target_expiry_ms"],
            "connection_string": "vless://example",
            "subscription_link": None,
            "sub_id": "sub-trial",
        }

    monkeypatch.setattr(handlers.xui_api, "create_or_update_key_on_host", fake_create_or_update_key_on_host)
    monkeypatch.setattr(handlers, "TRIAL_ISSUE_QUICK_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(handlers, "TRIAL_SUPER_ISSUE_TIMEOUT_SECONDS", 20.0)

    handlers._trial_super_issue_locks.clear()
    result, timed_out, in_progress = asyncio.run(
        handlers._issue_trial_super_with_quick_retries(
            host_name="SUPER2",
            user_id=12345,
            key_number=1,
            trial_days=2,
        )
    )

    assert timed_out is False
    assert in_progress is False
    assert result is not None
    assert len(calls) == 1
    assert calls[0]["email"] == "user12345-key1-trial-super@telegram.bot"
    assert calls[0]["days_to_add"] == 2
    assert calls[0]["target_expiry_ms"] == result["expiry_timestamp_ms"]
    assert calls[0]["max_attempts"] == 3


def test_trial_result_from_existing_key_restores_subscription_link():
    key = {
        "xui_client_uuid": "uuid-existing",
        "key_email": "user123-key7-trial-super@telegram.bot",
        "expiry_date": "2026-05-27 12:00:00",
        "issued_key_value": "https://pepelbot.ru/podpiska/sub-id",
        "host_name": "SUPER2",
        "panel_sub_id": "sub-id",
    }

    result = handlers._trial_result_from_existing_key(key)

    assert result["client_uuid"] == "uuid-existing"
    assert result["email"] == "user123-key7-trial-super@telegram.bot"
    assert result["subscription_link"] == "https://pepelbot.ru/podpiska/sub-id"
    assert result["connection_string"] is None
    assert result["sub_id"] == "sub-id"


def test_pick_issued_key_value_prefers_managed_subscription_url(monkeypatch):
    monkeypatch.setenv("MANAGED_SUBSCRIPTION_BASE_URL", "https://pepelbot.com/podpiska")

    assert (
        handlers._pick_issued_key_value(
            connection_string="vless://fallback",
            subscription_link="https://panel.example/sub/raw-sub",
            sub_id="managed-sub",
            subscription_tier="super",
        )
        == "https://pepelbot.com/podpiska/managed-sub"
    )


def test_pick_issued_key_value_falls_back_without_managed_env(monkeypatch):
    monkeypatch.delenv("MANAGED_SUBSCRIPTION_BASE_URL", raising=False)

    assert (
        handlers._pick_issued_key_value(
            connection_string="vless://fallback",
            subscription_link="https://panel.example/sub/raw-sub",
            sub_id="managed-sub",
            subscription_tier="standard",
        )
        == "https://panel.example/sub/raw-sub"
    )


def test_trial_super_delivery_uses_managed_url_instead_of_panel_cdn(monkeypatch):
    monkeypatch.setenv("MANAGED_SUBSCRIPTION_BASE_URL", "https://pepelbot.com/podpiska")

    issued_key_value, connection_string, subscription_link = handlers._trial_super_access_values_from_panel_result(
        {
            "connection_string": "vless://fallback",
            "subscription_link": "https://s83313.cdn.ngenix.net/pepelvpn/synthetic-sub-003",
            "sub_id": "synthetic-sub-003",
        }
    )

    assert issued_key_value == "https://pepelbot.com/podpiska/synthetic-sub-003"
    assert connection_string is None
    assert subscription_link == "https://pepelbot.com/podpiska/synthetic-sub-003"


def test_stored_key_access_values_restores_connection_string():
    connection_string, subscription_link = handlers._stored_key_access_values(
        {"issued_key_value": "vless://uuid@example:443#stored"}
    )

    assert connection_string == "vless://uuid@example:443#stored"
    assert subscription_link is None


def test_stored_key_access_values_falls_back_to_subscription_payload():
    connection_string, subscription_link = handlers._stored_key_access_values(
        {"subscription_payload": ["vless://main", "vless://backup"]}
    )

    assert connection_string == "vless://main\nvless://backup"
    assert subscription_link is None


def test_trial_super_key_number_parses_email():
    assert handlers._trial_super_key_number("user123-key7-trial-super@telegram.bot", 123, fallback=1) == 7
    assert handlers._trial_super_key_number("bad-email", 123, fallback=4) == 4


def test_trial_super_issuance_returns_in_progress_when_locked(monkeypatch):
    calls: list[dict] = []

    async def fake_create_or_update_key_on_host(**kwargs):
        calls.append(kwargs)
        return {"client_uuid": "uuid-trial"}

    async def _run():
        lock = await handlers._try_acquire_trial_super_issue_lock(777)
        try:
            return await handlers._issue_trial_super_with_quick_retries(
                host_name="SUPER2",
                user_id=777,
                key_number=1,
                trial_days=2,
            )
        finally:
            handlers._release_trial_super_issue_lock(777, lock)

    monkeypatch.setattr(handlers.xui_api, "create_or_update_key_on_host", fake_create_or_update_key_on_host)
    handlers._trial_super_issue_locks.clear()

    result, timed_out, in_progress = asyncio.run(_run())

    assert result is None
    assert timed_out is False
    assert in_progress is True
    assert calls == []


def test_trial_super_timeout_counts_are_pruned_and_capped(monkeypatch):
    now = 100.0

    def _monotonic() -> float:
        return now

    monkeypatch.setattr(handlers.time, "monotonic", _monotonic)
    monkeypatch.setattr(handlers, "_TRIAL_SUPER_TIMEOUT_COUNT_TTL_SECONDS", 10)
    monkeypatch.setattr(handlers, "_TRIAL_SUPER_TIMEOUT_COUNT_MAX_USERS", 3)
    handlers._trial_super_timeout_counts.clear()
    handlers._trial_super_timeout_counts[999] = (1, 80.0)

    for user_id, timestamp in (
        (1, 100.0),
        (2, 101.0),
        (3, 102.0),
        (4, 103.0),
    ):
        now = timestamp
        assert handlers._increment_trial_super_timeout_count(user_id) == 1

    assert 999 not in handlers._trial_super_timeout_counts
    assert 1 not in handlers._trial_super_timeout_counts
    assert set(handlers._trial_super_timeout_counts) == {2, 3, 4}

    now = 114.0
    assert handlers._consume_trial_super_timeout_count(4) == 0
    assert 4 not in handlers._trial_super_timeout_counts


def test_verify_trial_super_after_uncertain_issue_reuses_panel_result(monkeypatch):
    calls: list[dict] = []

    async def fake_get_existing_key_by_email_on_host(
        host_name, email, *, target_expiry_ms=None, subscription_tier=None
    ):
        calls.append(
            {
                "host_name": host_name,
                "email": email,
                "target_expiry_ms": target_expiry_ms,
                "subscription_tier": subscription_tier,
            }
        )
        return {
            "client_uuid": "uuid-existing",
            "email": email,
            "expiry_timestamp_ms": target_expiry_ms,
            "connection_string": "vless://existing",
            "subscription_link": None,
            "host_name": host_name,
            "sub_id": "sub-existing",
        }

    monkeypatch.setattr(handlers, "_existing_active_trial_super_key", lambda *_args: None)
    monkeypatch.setattr(handlers.xui_api, "get_existing_key_by_email_on_host", fake_get_existing_key_by_email_on_host)
    monkeypatch.setattr(handlers, "TRIAL_SUPER_VERIFY_ATTEMPTS", 2)
    monkeypatch.setattr(handlers, "TRIAL_SUPER_VERIFY_DELAY_SECONDS", 0.1)

    result = asyncio.run(
        handlers._verify_trial_super_after_uncertain_issue(
            host_name="SUPER2",
            user_id=12345,
            key_number=3,
            target_expiry_ms=1_800_000_000_000,
        )
    )

    assert result is not None
    assert result["client_uuid"] == "uuid-existing"
    assert calls == [
        {
            "host_name": "SUPER2",
            "email": "user12345-key3-trial-super@telegram.bot",
            "target_expiry_ms": 1_800_000_000_000,
            "subscription_tier": handlers.xui_api.SUBSCRIPTION_TIER_SUPER,
        }
    ]


def test_replace_message_edits_before_send_or_delete():
    events: list[str] = []

    class _Bot:
        async def send_message(self, **_kwargs):
            events.append("send")

    class _Message:
        bot = _Bot()
        chat = type("Chat", (), {"id": 123})()

        async def edit_text(self, text, **_kwargs):
            events.append(f"edit:{text}")

        async def delete(self):
            events.append("delete")

    asyncio.run(handlers._replace_message(_Message(), text="updated"))

    assert events == ["edit:updated"]
