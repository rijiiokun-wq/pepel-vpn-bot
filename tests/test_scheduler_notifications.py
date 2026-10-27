from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_users.db"
    monkeypatch.setattr("shop_bot.data_manager.database.DB_FILE", db_file)
    monkeypatch.setattr("shop_bot.data_manager.database._WAL_INITIALIZED", False)

    from shop_bot.data_manager import database

    database.initialize_db()
    yield db_file


def _create_user_with_key(user_id: int, *, hours_left: int = 2, bot_source: str | None = None) -> tuple[int, str]:
    from shop_bot.data_manager import database

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    expiry = datetime.now() + timedelta(hours=hours_left)
    key_id = database.add_new_key(
        user_id=user_id,
        host_name="host_test",
        xui_client_uuid=f"uuid-{user_id}",
        key_email=f"user{user_id}@telegram.bot",
        expiry_timestamp_ms=int(expiry.timestamp() * 1000),
        bot_source=bot_source,
    )
    assert key_id is not None
    key_data = database.get_key_by_id(key_id)
    assert key_data is not None
    return key_id, key_data["expiry_date"]


def test_notification_sent_once_and_persists_across_restart(monkeypatch):
    from shop_bot.data_manager import scheduler

    key_id, _ = _create_user_with_key(10001, hours_left=2)
    calls = []

    async def fake_send(_bot, user_id, sent_key_id, _time_left, _expiry):
        calls.append((user_id, sent_key_id))
        return True

    monkeypatch.setattr(scheduler, "send_subscription_notification", fake_send)
    asyncio.run(scheduler.check_expiring_subscriptions(bot=object()))
    assert calls == [(10001, key_id)]

    scheduler_reloaded = importlib.reload(scheduler)

    async def fake_send_after_restart(_bot, user_id, sent_key_id, _time_left, _expiry):
        calls.append((user_id, sent_key_id))
        return True

    monkeypatch.setattr(scheduler_reloaded, "send_subscription_notification", fake_send_after_restart)
    asyncio.run(scheduler_reloaded.check_expiring_subscriptions(bot=object()))
    assert calls == [(10001, key_id)]


def test_notification_is_sent_again_after_expiry_change(monkeypatch):
    from shop_bot.data_manager import database, scheduler

    key_id, _ = _create_user_with_key(10002, hours_left=2)
    calls = []

    async def fake_send(_bot, user_id, sent_key_id, _time_left, _expiry):
        calls.append((user_id, sent_key_id))
        return True

    monkeypatch.setattr(scheduler, "send_subscription_notification", fake_send)

    asyncio.run(scheduler.check_expiring_subscriptions(bot=object()))
    asyncio.run(scheduler.check_expiring_subscriptions(bot=object()))
    assert calls == [(10002, key_id)]

    new_expiry = datetime.now() + timedelta(hours=6)
    database.update_key_info(
        key_id=key_id,
        new_xui_uuid="uuid-updated",
        new_expiry_ms=int(new_expiry.timestamp() * 1000),
    )

    asyncio.run(scheduler.check_expiring_subscriptions(bot=object()))
    asyncio.run(scheduler.check_expiring_subscriptions(bot=object()))
    assert calls == [(10002, key_id), (10002, key_id)]


def test_reserve_runtime_notifies_only_reserve_keys(monkeypatch):
    from shop_bot.data_manager import scheduler

    main_key_id, _ = _create_user_with_key(10003, hours_left=2, bot_source="main")
    reserve_key_id, _ = _create_user_with_key(10004, hours_left=2, bot_source="reserve")
    calls = []

    async def fake_send(_bot, user_id, sent_key_id, _time_left, _expiry):
        calls.append((user_id, sent_key_id))
        return True

    monkeypatch.setattr(scheduler, "BOT_SOURCE", "reserve")
    monkeypatch.setattr(scheduler, "send_subscription_notification", fake_send)

    asyncio.run(scheduler.check_expiring_subscriptions(bot=object()))

    assert calls == [(10004, reserve_key_id)]
    assert main_key_id != reserve_key_id


def test_whitelist_cache_matches_secondary_client_by_shared_uuid(monkeypatch):
    from shop_bot.data_manager import scheduler

    captured_rows = []
    key = {
        "key_id": 42,
        "user_id": 12345,
        "key_email": "user12345-key2@vpn.bot",
        "xui_client_uuid": "aaaaaaaa-0000-4000-8000-000000000001",
        "panel_sub_id": "primary-sub-id",
        "host_name": "MainPanel",
        "bot_source": "reserve",
    }
    whitelist_stat = {
        "inbound_id": 1,
        "inbound_label": "WHITELIST1",
        "email": "user12345-key2-whitelist1-1@vpn.bot",
        "client_uuid": "AAAAAAAA-0000-4000-8000-000000000001",
        "sub_id": "different-whitelist-sub-id",
        "used_bytes": 2 * 1024**3,
        "total_bytes": 40 * 1024**3,
        "reset_days": 30,
        "reset_timestamp_ms": 1_786_312_558_594,
        "panel_enable": True,
        "exhausted": False,
    }

    monkeypatch.setattr(scheduler, "BOT_SOURCE", "reserve")
    monkeypatch.setattr(
        scheduler.database,
        "get_setting",
        lambda name: "MainPanel" if name == "super_key_host_name" else None,
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_host",
        lambda _name: {"host_inbound_id": 7},
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_active_whitelist_limit_keys",
        lambda **_kwargs: [key],
    )
    monkeypatch.setattr(
        scheduler.database,
        "get_whitelist_traffic_statuses_by_key_ids",
        lambda _key_ids: {},
    )
    monkeypatch.setattr(
        scheduler.database,
        "upsert_whitelist_traffic_status",
        lambda rows: captured_rows.extend(rows) or len(rows),
    )
    monkeypatch.setattr(
        scheduler.xui_api,
        "get_whitelist_traffic_snapshot",
        lambda _host_name: {whitelist_stat["email"]: whitelist_stat},
    )

    result = scheduler._refresh_whitelist_limit_statuses()

    assert result["updated"] == 1
    assert captured_rows == [
        {
            "key_id": 42,
            "user_id": 12345,
            "key_email": "user12345-key2@vpn.bot",
            "host_name": "MainPanel",
            "bot_source": "reserve",
            "used_bytes": 2 * 1024**3,
            "total_bytes": 40 * 1024**3,
            "reset_days": 30,
            "reset_timestamp_ms": 1_786_312_558_594,
            "panel_enable": True,
            "exhausted": False,
        }
    ]


def test_whitelist_monitor_runs_cycle_guard_without_notification_keys(monkeypatch):
    from shop_bot.data_manager import scheduler

    snapshot_calls = []
    monkeypatch.setattr(scheduler, "BOT_SOURCE", "main")
    monkeypatch.setattr(scheduler.database, "get_setting", lambda _name: "MainPanel")
    monkeypatch.setattr(
        scheduler.database,
        "get_active_whitelist_limit_keys",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        scheduler.xui_api,
        "get_whitelist_traffic_snapshot",
        lambda _host: snapshot_calls.append(True) or {},
    )
    monkeypatch.setattr(
        scheduler.xui_api,
        "reconcile_active_whitelist_reset_cycles",
        lambda _host, **_kwargs: {
            "checked": 1,
            "already_correct": 0,
            "updated": 1,
            "needs_review": 0,
            "failed": 0,
        },
    )

    result = scheduler._refresh_whitelist_limit_statuses()

    assert len(snapshot_calls) == 2
    assert result["keys"] == 0
    assert result["cycle_guard"]["updated"] == 1
