from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
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


class DummyMessage:
    def __init__(self):
        self.edits: list[tuple[str, dict]] = []
        self.deleted = False

    async def edit_text(self, text: str, **kwargs):
        self.edits.append((text, kwargs))

    async def delete(self):
        self.deleted = True


class DummyBot:
    def __init__(self):
        self.sent_messages: list[dict] = []
        self.processing_messages: list[DummyMessage] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append({"chat_id": chat_id, "text": text, "kwargs": kwargs})
        msg = DummyMessage()
        self.processing_messages.append(msg)
        return msg

    async def delete_message(self, chat_id, message_id):
        return None


class ProcessingNoticeFailsBot(DummyBot):
    def __init__(self):
        super().__init__()
        self._fail_next_send = True

    async def send_message(self, chat_id, text, **kwargs):
        if self._fail_next_send:
            self._fail_next_send = False
            raise RuntimeError
        return await super().send_message(chat_id, text, **kwargs)


class DummyBotController:
    def __init__(self, bot):
        self._bot = bot

    def get_bot_instance(self):
        return self._bot


def _build_base_metadata(*, payment_id: str, user_id: int = 50101) -> dict:
    return {
        "user_id": user_id,
        "months": 3,
        "price": 840.0,
        "action": "new",
        "key_id": 0,
        "host_name": "HOST_SMOKE",
        "plan_id": 1,
        "plan_name": "Суперключ 3 месяца",
        "customer_email": f"user{user_id}-key1@vpn.bot",
        "payment_method": "YooKassa",
        "external_payment_id": payment_id,
    }


def _read_transaction_row(db_file: Path, payment_id: str) -> dict:
    with sqlite3.connect(str(db_file)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payment_id, status, metadata FROM transactions WHERE payment_id = ?",
            (payment_id,),
        )
        row = cursor.fetchone()
    assert row is not None, f"transaction {payment_id} not found"
    payload = dict(row)
    payload["metadata"] = json.loads(payload["metadata"]) if payload["metadata"] else {}
    return payload


def _read_key_rows(db_file: Path) -> list[dict]:
    with sqlite3.connect(str(db_file)) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT user_id, host_name, subscription_tier, issued_key_value, subscription_payload
            FROM vpn_keys
            ORDER BY user_id
            """
        )
        return [dict(row) for row in cursor.fetchall()]


def test_super_purchase_flow_uses_explicit_tier_before_shared_host(monkeypatch):
    from shop_bot.bot import handlers

    main_panel = "MainPanel"
    monkeypatch.setattr(
        handlers,
        "get_setting",
        lambda key: main_panel if key == "super_key_host_name" else None,
    )
    monkeypatch.setattr(
        handlers,
        "get_host",
        lambda host_name: (
            {"host_name": main_panel, "host_url": "https://panel.example"} if host_name == main_panel else None
        ),
    )

    assert (
        handlers._is_super_purchase_flow(
            host_name=main_panel,
            is_super_key_flow=False,
            plan_data={"plan_id": 0},
        )
        is False
    )
    assert (
        handlers._is_super_purchase_flow(
            host_name=main_panel,
            plan_data={"plan_id": 1},
        )
        is False
    )
    assert (
        handlers._is_super_purchase_flow(
            host_name=main_panel,
            plan_data={"plan_id": 0},
        )
        is True
    )


def test_payment_issuance_routes_standard_and_super_on_same_main_panel(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database

    main_panel = "MainPanel"
    settings = {
        "standard_key_host_name": main_panel,
        "super_key_host_name": main_panel,
        "receipt_email": "example@example.com",
    }
    monkeypatch.setenv("MANAGED_SUBSCRIPTION_BASE_URL", "https://pepelbot.com/podpiska")
    monkeypatch.setattr(handlers, "get_setting", settings.get)

    async def _allowed_precheck(*_args, **_kwargs):
        return handlers.PreCheckResult(allowed=True)

    async def _return_none(**_kwargs):
        return None

    async def _noop_notify(*_args, **_kwargs):
        return None

    namespace_checks: list[tuple[str, str]] = []

    async def _empty_standard_namespace(host_name, email):
        namespace_checks.append((host_name, email))
        return True

    monkeypatch.setattr(handlers.xui_api, "standard_birth_namespace_is_empty", _empty_standard_namespace)
    standard_calls: list[dict] = []
    super_calls: list[dict] = []

    async def _fake_standard_issue(**kwargs):
        # The first panel write must follow the durable namespace claim.
        transaction = _read_transaction_row(_use_temp_db, "smoke-same-panel-standard-001")
        assert transaction["metadata"]["standard_birth_attempted"] is True
        assert transaction["metadata"]["issuance_target_email"] == kwargs["email"]
        assert transaction["metadata"]["issuance_whitelist_cycle_anchor_ms"] == kwargs["whitelist_cycle_anchor_ms"]
        standard_calls.append(kwargs)
        return {
            "client_uuid": "uuid-standard",
            "email": kwargs["email"],
            "expiry_timestamp_ms": kwargs["target_expiry_ms"],
            "connection_string": "vless://standard",
            "subscription_links": [
                "vless://standard@ordinary.example:443?type=tcp#FINLAND",
                "vless://standard@white.example:443?type=ws#WHITELIST1",
            ],
            "subscription_link": "https://sub/standard",
            "host_name": kwargs["host_name"],
            "sub_id": "synthetic-sub-002",
        }

    async def _fake_super_issue(**kwargs):
        super_calls.append(kwargs)
        return {
            "client_uuid": "uuid-super",
            "email": kwargs["email"],
            "expiry_timestamp_ms": kwargs["target_expiry_ms"],
            "connection_string": "vless://super",
            "subscription_links": [
                "vless://super@ordinary.example:443?type=tcp#FINLAND",
                "vless://super@white.example:443?type=ws#WHITELIST1",
            ],
            "subscription_link": "https://sub/super",
            "host_name": kwargs["host_name"],
            "sub_id": "sub-super",
        }

    monkeypatch.setattr(handlers, "_run_purchase_precheck_async", _allowed_precheck)
    monkeypatch.setattr(handlers, "_is_host_reachable", lambda _host_name: (True, ""))
    monkeypatch.setattr(handlers, "notify_admin_of_purchase", _noop_notify)
    monkeypatch.setattr(handlers, "notify_admin_of_issuance_failure", _noop_notify)
    monkeypatch.setattr(handlers.xui_api, "get_existing_key_by_email_on_host", _return_none)
    monkeypatch.setattr(handlers.xui_api, "find_existing_key_by_email_with_fallback", _return_none)
    monkeypatch.setattr(
        handlers.xui_api,
        "create_or_update_standard_subscription_on_hosts",
        _fake_standard_issue,
    )
    monkeypatch.setattr(handlers.xui_api, "create_or_update_key_on_host", _fake_super_issue)

    standard_metadata = _build_base_metadata(
        payment_id="smoke-same-panel-standard-001",
        user_id=50201,
    )
    standard_metadata.update(
        {
            "host_name": main_panel,
            "plan_id": 1,
            "plan_name": "Стандарт 3 месяца",
            "is_super_key_flow": False,
            "payment_confirmed": True,
        }
    )
    super_metadata = _build_base_metadata(
        payment_id="smoke-same-panel-super-001",
        user_id=50202,
    )
    super_metadata.update(
        {
            "host_name": main_panel,
            "plan_id": 0,
            "plan_name": "Супер 3 месяца",
            "is_super_key_flow": True,
            "payment_confirmed": True,
        }
    )

    for metadata in (standard_metadata, super_metadata):
        database.register_user_if_not_exists(metadata["user_id"], f"user_{metadata['user_id']}", None)
        database.create_pending_transaction(
            metadata["external_payment_id"],
            metadata["user_id"],
            float(metadata["price"]),
            metadata,
        )

    bot = DummyBot()
    asyncio.run(handlers.process_successful_payment(bot, standard_metadata))
    asyncio.run(handlers.process_successful_payment(bot, super_metadata))

    assert namespace_checks == [(main_panel, "user50201-key1@vpn.bot")]
    assert len(standard_calls) == 1
    assert standard_calls[0]["host_name"] == main_panel
    assert standard_calls[0]["email"].startswith("user50201-key1@")
    assert standard_calls[0]["max_attempts_per_host"] == handlers.PURCHASE_ISSUE_MAX_ATTEMPTS
    assert standard_calls[0]["panel_call_timeout_seconds"] == handlers.PURCHASE_ISSUE_PANEL_CALL_TIMEOUT_SECONDS
    assert len(super_calls) == 1
    assert super_calls[0]["host_name"] == main_panel
    assert super_calls[0]["email"].startswith("user50202-key1@")
    assert super_calls[0]["include_white_inbounds"] is True
    assert super_calls[0]["require_white_inbound"] is True
    assert super_calls[0]["max_attempts"] == handlers.PURCHASE_ISSUE_MAX_ATTEMPTS
    assert super_calls[0]["panel_call_timeout_seconds"] == handlers.PURCHASE_ISSUE_PANEL_CALL_TIMEOUT_SECONDS

    key_rows = _read_key_rows(_use_temp_db)
    assert [(row["user_id"], row["subscription_tier"]) for row in key_rows] == [
        (50201, "standard"),
        (50202, "super"),
    ]
    assert key_rows[0]["issued_key_value"] == "https://pepelbot.com/podpiska/synthetic-sub-002"
    assert key_rows[1]["issued_key_value"] == "https://pepelbot.com/podpiska/sub-super"
    assert key_rows[0]["subscription_payload"].splitlines() == [
        "vless://standard@ordinary.example:443?type=tcp#FINLAND",
        "vless://standard@white.example:443?type=ws#WHITELIST1",
    ]
    assert key_rows[1]["subscription_payload"].splitlines() == [
        "vless://super@ordinary.example:443?type=tcp#FINLAND",
        "vless://super@white.example:443?type=ws#WHITELIST1",
    ]

    final_messages = [
        message["text"]
        for message in bot.sent_messages
        if "Уважаемый пользователь, ваша новая подписка готова" in message["text"]
    ]
    assert len(final_messages) == 2
    assert "https://pepelbot.com/podpiska/synthetic-sub-002" in final_messages[0]
    assert "https://sub/standard" not in final_messages[0]
    assert "Хост: <b>MainPanel</b>" in final_messages[0]
    assert "Email: <code>user50201-key1@vpn.bot</code>" in final_messages[0]
    assert "Ваш ключ" not in final_messages[0]
    assert "Он будет" not in final_messages[0]
    assert "https://pepelbot.com/podpiska/sub-super" in final_messages[1]
    assert "https://sub/super" not in final_messages[1]
    assert "Хост: <b>MainPanel</b>" in final_messages[1]
    assert "Email: <code>user50202-key1@vpn.bot</code>" in final_messages[1]


def test_smoke_fail_sets_reason_logs_and_alert(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database

    user_id = 50101
    payment_id = "smoke-fail-001"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id)
    metadata["payment_confirmed"] = True

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    monkeypatch.setattr(
        handlers,
        "_run_purchase_precheck",
        lambda *args, **kwargs: handlers.PreCheckResult(allowed=True),
    )
    monkeypatch.setattr(handlers.xui_api, "get_existing_key_by_email_on_host", lambda **kwargs: None)

    async def _return_none(**kwargs):
        return None

    monkeypatch.setattr(handlers.xui_api, "find_existing_key_by_email_with_fallback", _return_none)
    monkeypatch.setattr(handlers.xui_api, "create_or_update_key_with_fallback", _return_none)

    failure_alerts: list[dict] = []

    async def _capture_failure_alert(_bot, **kwargs):
        failure_alerts.append(kwargs)

    async def _noop_notify_purchase(*args, **kwargs):
        return None

    monkeypatch.setattr(handlers, "notify_admin_of_issuance_failure", _capture_failure_alert)
    monkeypatch.setattr(handlers, "notify_admin_of_purchase", _noop_notify_purchase)

    bot = DummyBot()
    asyncio.run(handlers.process_successful_payment(bot, metadata))

    tx = _read_transaction_row(_use_temp_db, payment_id)
    assert tx["metadata"]["issuance_failed"] is True
    assert tx["metadata"]["issuance_failure_alert_sent"] is True
    assert tx["metadata"]["issuance_failure_reason"].startswith("key_creation_failed")
    assert tx["metadata"]["issuance_failure_logs"]["stage"] == "key_issue"
    assert len(failure_alerts) == 1
    assert failure_alerts[0]["reason"].startswith("key_creation_failed")


def test_issuance_failure_sends_retry_notice_without_processing_message(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database

    user_id = 50109
    payment_id = "smoke-fail-user-notice-001"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id)
    metadata["payment_confirmed"] = True

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    async def _allowed_precheck(*args, **kwargs):
        return handlers.PreCheckResult(allowed=True)

    async def _return_none(**kwargs):
        return None

    async def _noop_notify(*args, **kwargs):
        return None

    monkeypatch.setattr(handlers, "_run_purchase_precheck_async", _allowed_precheck)
    monkeypatch.setattr(handlers.xui_api, "get_existing_key_by_email_on_host", _return_none)
    monkeypatch.setattr(handlers.xui_api, "find_existing_key_by_email_with_fallback", _return_none)
    monkeypatch.setattr(handlers.xui_api, "create_or_update_key_with_fallback", _return_none)
    monkeypatch.setattr(handlers, "notify_admin_of_issuance_failure", _noop_notify)
    monkeypatch.setattr(handlers, "notify_admin_of_purchase", _noop_notify)

    bot = ProcessingNoticeFailsBot()
    asyncio.run(handlers.process_successful_payment(bot, metadata))

    assert len(bot.sent_messages) == 1
    assert "платёж сохранён" in bot.sent_messages[0]["text"].lower()
    assert "автоматически" in bot.sent_messages[0]["text"]

    tx = _read_transaction_row(_use_temp_db, payment_id)
    assert tx["metadata"]["issuance_failure_reason"].startswith("key_creation_failed")


def test_precheck_blocked_stays_retryable_for_autoretry(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database

    user_id = 50107
    payment_id = "smoke-precheck-retryable-001"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id)
    metadata["payment_confirmed"] = True

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    async def _blocked_precheck(*args, **kwargs):
        return handlers.PreCheckResult(
            allowed=False,
            reason="blitz_disabled",
            user_message="maintenance",
        )

    monkeypatch.setattr(handlers, "_run_purchase_precheck_async", _blocked_precheck)

    failure_alerts: list[dict] = []

    async def _capture_failure_alert(_bot, **kwargs):
        failure_alerts.append(kwargs)

    monkeypatch.setattr(handlers, "notify_admin_of_issuance_failure", _capture_failure_alert)

    bot = DummyBot()
    asyncio.run(handlers.process_successful_payment(bot, metadata))

    tx = database.get_transaction_by_payment_id(payment_id)
    assert isinstance(tx, dict)
    assert tx["status"] == "pending"
    assert tx["issuance_state"] == database.ISSUANCE_STATE_RETRYABLE_FAILED
    assert tx["last_error"] == "precheck_blocked:blitz_disabled"

    tx_metadata = json.loads(tx["metadata"]) if tx.get("metadata") else {}
    assert tx_metadata.get("issuance_failure_reason") == "precheck_blocked:blitz_disabled"
    assert tx_metadata.get("issuance_fatal") is not True
    assert len(failure_alerts) == 1
    assert failure_alerts[0]["reason"] == "precheck_blocked:blitz_disabled"


def test_host_maintenance_issuance_failure_alert_is_suppressed(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database

    user_id = 50108
    payment_id = "smoke-host-maintenance-suppressed-001"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id)
    metadata.update(
        {
            "host_name": "StandartVLESS",
            "payment_confirmed": True,
            "issuance_failure_reason": "precheck_blocked:host_maintenance",
        }
    )

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    critical_alerts: list[dict] = []
    monkeypatch.setattr(handlers, "_get_ops_alert_chat_ids", lambda: ["100500"])
    monkeypatch.setattr(
        handlers,
        "send_critical_alert_nonblocking",
        lambda *args, **kwargs: critical_alerts.append({"args": args, "kwargs": kwargs}),
    )

    bot = DummyBot()
    asyncio.run(
        handlers.notify_admin_of_issuance_failure(
            bot,
            user_id=user_id,
            payment_id=payment_id,
            payment_method="PayPear",
            host_name="StandartVLESS",
            plan_id=1,
            plan_name="1 месяц",
            months=1,
            amount_rub=840.0,
            action="extend",
            key_email="user50108-key1@vpn.bot",
            reason="precheck_blocked:host_maintenance",
        )
    )

    assert critical_alerts == []
    assert bot.sent_messages == []

    tx = database.get_transaction_by_payment_id(payment_id)
    tx_metadata = json.loads(tx["metadata"]) if tx and tx.get("metadata") else {}
    assert tx_metadata.get("payment_issue_critical_suppressed") is True
    assert tx_metadata.get("payment_issue_critical_suppressed_reason") == "precheck_blocked:host_maintenance"
    assert tx_metadata.get("payment_issue_critical_suppressed_at")


def test_smoke_auto_retry_marks_paid_and_counts_recovered(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database, scheduler

    user_id = 50102
    payment_id = "smoke-retry-001"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id)
    metadata["payment_confirmed"] = True

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    monkeypatch.setattr(scheduler, "YOOKASSA_RECONCILE_MIN_AGE_SECONDS", 0)
    captured_retry_sources: list[str] = []

    async def _fake_process(_bot, tx_metadata):
        captured_retry_sources.append(str(tx_metadata.get("retry_source") or ""))
        database.mark_transaction_paid(tx_metadata["external_payment_id"], payment_method="YooKassa")

    monkeypatch.setattr(handlers, "process_successful_payment", _fake_process)

    stats = asyncio.run(scheduler.retry_pending_confirmed_transactions(DummyBotController(DummyBot())))

    tx = _read_transaction_row(_use_temp_db, payment_id)
    assert captured_retry_sources == ["pending_confirmed_retry"]
    assert stats["checked"] == 1
    assert stats["recovered"] == 1
    assert stats["errors"] == 0
    assert tx["status"] == "paid"
    assert tx["metadata"]["retry_source"] == "pending_confirmed_retry"


def test_smoke_recovered_alert_sent_after_success(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database

    user_id = 50103
    payment_id = "smoke-recovered-001"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id)
    metadata["payment_confirmed"] = True
    metadata["issuance_failed"] = True
    metadata["issuance_failure_alert_sent"] = True
    metadata["issuance_failure_reason"] = "key_creation_failed"
    metadata["issuance_failure_first_at"] = "2026-03-23T12:00:00"
    metadata["retry_source"] = "pending_confirmed_retry"

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    monkeypatch.setattr(
        handlers,
        "_run_purchase_precheck",
        lambda *args, **kwargs: handlers.PreCheckResult(allowed=True),
    )

    async def _existing_key(**kwargs):
        email = kwargs["email"]
        return {
            "client_uuid": "uuid-smoke-ok",
            "email": email,
            "host_name": "HOST_SMOKE",
            "connection_string": "vless://smoke",
        }

    monkeypatch.setattr(handlers.xui_api, "get_existing_key_by_email_on_host", _existing_key)
    monkeypatch.setattr(handlers.xui_api, "find_existing_key_by_email_with_fallback", _existing_key)

    async def _noop_notify_purchase(*args, **kwargs):
        return None

    monkeypatch.setattr(handlers, "notify_admin_of_purchase", _noop_notify_purchase)
    monkeypatch.setattr(handlers.keyboards, "create_key_info_keyboard", lambda *args, **kwargs: None)
    monkeypatch.setattr(handlers.blitz_api, "is_blitz_enabled", lambda: False)

    recovered_alerts: list[dict] = []

    async def _capture_recovered(_bot, *, metadata, retry_source=None):
        recovered_alerts.append(
            {
                "payment_id": metadata.get("external_payment_id"),
                "retry_source": retry_source,
                "key_id": metadata.get("key_id"),
            }
        )

    monkeypatch.setattr(handlers, "notify_admin_of_issuance_recovered", _capture_recovered)

    bot = DummyBot()
    asyncio.run(handlers.process_successful_payment(bot, metadata))

    tx = _read_transaction_row(_use_temp_db, payment_id)
    assert tx["status"] == "paid"
    assert recovered_alerts == []


def test_paid_not_issued_transaction_still_issues_key(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database

    user_id = 50105
    payment_id = "smoke-paid-awaiting-001"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id)
    metadata["payment_confirmed"] = True
    metadata["is_super_key_flow"] = True

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)
    with sqlite3.connect(str(_use_temp_db)) as conn:
        conn.execute(
            "UPDATE transactions SET status = 'paid', issuance_state = ? WHERE payment_id = ?",
            (database.ISSUANCE_STATE_AWAITING_PAYMENT, payment_id),
        )
        conn.commit()

    monkeypatch.setattr(
        handlers,
        "_run_purchase_precheck",
        lambda *args, **kwargs: handlers.PreCheckResult(allowed=True),
    )
    monkeypatch.setattr(handlers, "_is_host_reachable", lambda _host: (True, ""))

    async def _no_existing_key(**kwargs):
        return None

    async def _issue_on_host(**kwargs):
        return {
            "client_uuid": "uuid-paid-awaiting",
            "email": kwargs["email"],
            "host_name": kwargs["host_name"],
            "connection_string": "vless://paid-awaiting",
            "expiry_timestamp_ms": kwargs["target_expiry_ms"],
        }

    monkeypatch.setattr(handlers.xui_api, "get_existing_key_by_email_on_host", _no_existing_key)
    monkeypatch.setattr(handlers.xui_api, "create_or_update_key_on_host", _issue_on_host)
    monkeypatch.setattr(handlers.keyboards, "create_key_info_keyboard", lambda *args, **kwargs: None)
    monkeypatch.setattr(handlers.blitz_api, "is_blitz_enabled", lambda: False)

    async def _noop_notify_purchase(*args, **kwargs):
        return None

    monkeypatch.setattr(handlers, "notify_admin_of_purchase", _noop_notify_purchase)

    bot = DummyBot()
    asyncio.run(handlers.process_successful_payment(bot, metadata))

    tx = database.get_transaction_by_payment_id(payment_id)
    keys = database.get_user_keys(user_id)

    assert tx is not None
    assert tx["status"] == "paid"
    assert tx["issuance_state"] == database.ISSUANCE_STATE_ISSUED
    assert database.is_transaction_fully_issued(payment_id) is True
    assert len(keys) == 1
    assert keys[0]["key_email"] == f"user{user_id}-key1@vpn.bot"


def test_fully_issued_transaction_is_not_reissued(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database

    user_id = 50106
    payment_id = "smoke-issued-skip-001"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id)
    metadata["payment_confirmed"] = True
    metadata["is_super_key_flow"] = True

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)
    database.mark_transaction_paid(payment_id, payment_method="YooKassa")

    async def _unexpected_issue(**kwargs):
        raise AssertionError

    monkeypatch.setattr(handlers.xui_api, "create_or_update_key_on_host", _unexpected_issue)

    bot = DummyBot()
    asyncio.run(handlers.process_successful_payment(bot, metadata))

    assert bot.sent_messages == []
    assert database.is_transaction_fully_issued(payment_id) is True


def test_payment_handler_lock_blocks_parallel_dispatch(monkeypatch):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database

    user_id = 50108
    payment_id = "smoke-parallel-issuance-lock-001"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id)
    metadata["payment_confirmed"] = True

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 840.0, metadata)

    entered_locked_handler: list[str] = []

    async def _run_parallel_dispatch() -> None:
        locked_handler_started = asyncio.Event()
        release_locked_handler = asyncio.Event()

        async def _hold_locked_handler(_bot, tx_metadata, *, issuance_lock_token):
            assert issuance_lock_token
            entered_locked_handler.append(str(tx_metadata["external_payment_id"]))
            locked_handler_started.set()
            await release_locked_handler.wait()

        monkeypatch.setattr(handlers, "_process_successful_payment_locked", _hold_locked_handler)

        bot = DummyBot()
        first_dispatch = asyncio.create_task(handlers.process_successful_payment(bot, metadata))
        await asyncio.wait_for(locked_handler_started.wait(), timeout=5)
        await handlers.process_successful_payment(bot, metadata)
        release_locked_handler.set()
        await first_dispatch

    asyncio.run(_run_parallel_dispatch())

    tx = database.get_transaction_by_payment_id(payment_id)
    assert entered_locked_handler == [payment_id]
    assert isinstance(tx, dict)
    assert tx.get("locked_by") is None
    assert tx.get("locked_at") is None


def test_missing_extend_key_issues_replacement_key(monkeypatch, _use_temp_db):
    from shop_bot.bot import handlers
    from shop_bot.data_manager import database

    user_id = 50104
    payment_id = "smoke-missing-extend-001"
    metadata = _build_base_metadata(payment_id=payment_id, user_id=user_id)
    metadata.update(
        {
            "payment_confirmed": True,
            "action": "extend",
            "key_id": 999,
            "months": 1,
            "price": 300.0,
            "plan_id": 0,
            "plan_name": "Суперключ 1 месяц",
            "is_super_key_flow": True,
        }
    )

    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.create_pending_transaction(payment_id, user_id, 300.0, metadata)

    monkeypatch.setattr(
        handlers,
        "_run_purchase_precheck",
        lambda *args, **kwargs: handlers.PreCheckResult(allowed=True),
    )
    monkeypatch.setattr(handlers, "_is_host_reachable", lambda _host: (True, ""))

    async def _no_existing_key(**kwargs):
        return None

    async def _issue_on_host(**kwargs):
        return {
            "client_uuid": "uuid-replacement",
            "email": kwargs["email"],
            "host_name": kwargs["host_name"],
            "connection_string": "vless://replacement",
            "expiry_timestamp_ms": kwargs["target_expiry_ms"],
        }

    monkeypatch.setattr(handlers.xui_api, "get_existing_key_by_email_on_host", _no_existing_key)
    monkeypatch.setattr(handlers.xui_api, "create_or_update_key_on_host", _issue_on_host)
    monkeypatch.setattr(handlers.keyboards, "create_key_info_keyboard", lambda *args, **kwargs: None)
    monkeypatch.setattr(handlers.blitz_api, "is_blitz_enabled", lambda: False)

    failure_alerts: list[dict] = []

    async def _capture_failure_alert(_bot, **kwargs):
        failure_alerts.append(kwargs)

    async def _noop_notify_purchase(*args, **kwargs):
        return None

    monkeypatch.setattr(handlers, "notify_admin_of_issuance_failure", _capture_failure_alert)
    monkeypatch.setattr(handlers, "notify_admin_of_purchase", _noop_notify_purchase)

    bot = DummyBot()
    asyncio.run(handlers.process_successful_payment(bot, metadata))

    tx = _read_transaction_row(_use_temp_db, payment_id)
    keys = database.get_user_keys(user_id)

    assert tx["status"] == "paid"
    assert tx["metadata"]["action"] == "new"
    assert tx["metadata"]["original_action"] == "extend"
    assert tx["metadata"]["missing_extend_key_id"] == 999
    assert len(keys) == 1
    assert keys[0]["key_email"] == f"user{user_id}-key1@vpn.bot"
    assert failure_alerts == []
    assert any("выдали новый ключ" in message["text"] for message in bot.sent_messages)
