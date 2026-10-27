"""
Tests for host resilience, pre-check, migration, and incident notifications.

Requires Python 3.10+ (project targets 3.12 via Docker).
Run inside Docker or with a compatible Python.

Covers:
- G1: Error classification (degraded/fatal)
- G2: Pre-check purchase at various host statuses
- G3: Remaining time calculation (remaining + bonus)
- G4: Beta bonus (+2 days for active beta)
- G5: Bot survives when one host is dead (degraded scenario)
- G6: "Buy" shows maintenance message on unavailable/maintenance host
- G7: Incident notification shown exactly once
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure the src and scripts packages are importable
SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path, monkeypatch):
    """Redirect all database operations to a temporary SQLite DB."""
    db_file = tmp_path / "test_users.db"
    monkeypatch.setattr("shop_bot.data_manager.database.DB_FILE", db_file)
    monkeypatch.setattr("shop_bot.data_manager.database._WAL_INITIALIZED", False)

    from shop_bot.data_manager import database
    database.initialize_db()
    yield db_file


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def _insert_host(
    db_file,
    name,
    status="healthy",
    is_maintenance=0,
    last_check_at=None,
    delivery_state="active",
):
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute(
            """INSERT INTO xui_hosts
               (host_name, host_url, host_username, host_pass, host_inbound_id,
                status, is_maintenance, last_check_at, delivery_state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                f"http://{name}:8080",
                "admin",
                "pass",
                1,
                status,
                is_maintenance,
                last_check_at,
                delivery_state,
            ),
        )
        conn.commit()


def test_create_host_persists_api_token(_use_temp_db):
    from shop_bot.data_manager.database import create_host, get_host

    create_host(
        name="token_host",
        url="https://token-host.example",
        user="admin",
        passwd="pass",
        inbound=1,
        subscription_base_url="https://sub.example",
        api_token_env="XUI_API_TOKEN_HOST",
    )

    host = get_host("token_host")

    assert host is not None
    assert host["host_api_token_env"] == "XUI_API_TOKEN_HOST"


def test_create_host_persists_node_sync_defaults(_use_temp_db):
    from shop_bot.data_manager.database import create_host, get_host

    create_host(
        name="white_host",
        url="https://white-host.example",
        user="admin",
        passwd="pass",
        inbound=11,
        is_white_host=True,
    )

    host = get_host("white_host")

    assert host is not None
    assert int(host["is_white_host"]) == 1
    assert host["node_sync_status"] == "pending"
    assert host["node_sync_error"] is None
    assert host["node_id"] is None
    assert host["node_inbound_id"] is None
    assert host["node_synced_at"] is None


def test_update_host_api_token_ref_persists_and_clears_value(_use_temp_db):
    from shop_bot.data_manager.database import (
        create_host,
        get_host,
        update_host_api_token_ref,
    )

    create_host(
        name="token_host",
        url="https://token-host.example",
        user="admin",
        passwd="pass",
        inbound=1,
    )

    assert update_host_api_token_ref("token_host", "env:XUI_API_TOKEN_HOST") is True
    assert get_host("token_host")["host_api_token_env"] == "env:XUI_API_TOKEN_HOST"

    assert update_host_api_token_ref("token_host", "") is True
    assert get_host("token_host")["host_api_token_env"] is None

    assert update_host_api_token_ref("missing_host", "env:XUI_API_TOKEN_HOST") is False


def test_update_host_node_sync_state_persists_completed_and_failure_metadata(_use_temp_db):
    from shop_bot.data_manager.database import (
        create_host,
        get_host,
        update_host_node_sync_state,
    )

    create_host(
        name="sync_host",
        url="https://sync-host.example",
        user="admin",
        passwd="pass",
        inbound=1,
    )

    assert update_host_node_sync_state(
        "sync_host",
        status="completed",
        error=None,
        node_id=51,
        node_inbound_id=72,
    ) is True
    completed_host = get_host("sync_host")
    assert completed_host["node_sync_status"] == "completed"
    assert completed_host["node_id"] == 51
    assert completed_host["node_inbound_id"] == 72
    assert completed_host["node_synced_at"]

    assert update_host_node_sync_state(
        "sync_host",
        status="failed",
        error="sync exploded",
    ) is True
    failed_host = get_host("sync_host")
    assert failed_host["node_sync_status"] == "failed"
    assert failed_host["node_sync_error"] == "sync exploded"


def test_update_host_urls_persists_and_clears_optional_subscription(_use_temp_db):
    from shop_bot.data_manager.database import create_host, get_host, update_host_urls

    create_host(
        name="url_host",
        url="https://old-panel.example",
        user="admin",
        passwd="pass",
        inbound=1,
        subscription_base_url="https://old-sub.example",
    )

    assert update_host_urls("url_host", "https://new-panel.example", "https://new-sub.example") is True
    assert get_host("url_host")["host_url"] == "https://new-panel.example"
    assert get_host("url_host")["host_subscription_base_url"] == "https://new-sub.example"

    assert update_host_urls("url_host", "https://new-panel.example", "") is True
    assert get_host("url_host")["host_subscription_base_url"] is None

    assert update_host_urls("missing_host", "https://missing.example", None) is False


def test_update_host_config_persists_editable_fields_without_touching_node_state(_use_temp_db):
    from shop_bot.data_manager.database import (
        create_host,
        get_host,
        update_host_config,
        update_host_node_sync_state,
    )

    create_host(
        name="config_host",
        url="https://old-panel.example",
        user="old-admin",
        passwd="old-pass",
        inbound=11,
        subscription_base_url="https://old-sub.example",
    )
    assert update_host_node_sync_state(
        "config_host",
        status="completed",
        error=None,
        node_id=51,
        node_inbound_id=72,
    ) is True

    assert update_host_config(
        "config_host",
        host_url="https://new-panel.example/path/",
        host_username="new-admin",
        host_pass="new-pass",
        host_inbound_id=77,
        subscription_base_url=None,
        is_white_host=True,
    ) is True

    host = get_host("config_host")
    assert host["host_url"] == "https://new-panel.example/path/"
    assert host["host_username"] == "new-admin"
    assert host["host_pass"] == "new-pass"
    assert host["host_inbound_id"] == 77
    assert host["host_subscription_base_url"] is None
    assert host["is_white_host"] == 1
    assert host["node_id"] == 51
    assert host["node_inbound_id"] == 72

    assert update_host_config(
        "missing_host",
        host_url="https://missing.example",
        host_username="admin",
        host_pass="pass",
        host_inbound_id=1,
        subscription_base_url=None,
        is_white_host=False,
    ) is False


def _insert_key(
    db_file,
    user_id,
    host_name,
    expiry_hours=24 * 30,
    is_archived=0,
    gift_days=0,
    key_email: str | None = None,
):
    expiry = datetime.now() + timedelta(hours=expiry_hours)
    email = key_email or f"user{user_id}@{host_name}"
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute(
            """INSERT INTO vpn_keys
               (user_id, host_name, xui_client_uuid, key_email, expiry_date,
                is_archived, gift_days)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                host_name,
                f"uuid-{user_id}-{host_name}",
                email,
                expiry.isoformat(),
                is_archived,
                gift_days,
            ),
        )
        conn.commit()


def _insert_user(db_file, telegram_id, trial_super_used=0, has_active_beta=0):
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute(
            "INSERT INTO users (telegram_id, username, trial_super_used, has_active_beta) VALUES (?, ?, ?, ?)",
            (telegram_id, f"user_{telegram_id}", trial_super_used, has_active_beta),
        )
        conn.commit()


# ──────────────────────────────────────────────────────────────
# G2: Pre-check purchase at various host statuses
# ──────────────────────────────────────────────────────────────

class TestPurchasePreCheck:
    def test_healthy_host_allowed(self, _use_temp_db):
        _insert_host(_use_temp_db, "host_ok", status="healthy")
        from shop_bot.modules.purchase_precheck import check_purchase
        result = check_purchase("host_ok")
        assert result.allowed is True

    def test_unavailable_host_blocked(self, _use_temp_db, monkeypatch):
        _insert_host(_use_temp_db, "host_down", status="unavailable")
        monkeypatch.setattr(
            "shop_bot.modules.xui_api.login_to_host",
            lambda **_kwargs: (None, None),
        )
        from shop_bot.modules.purchase_precheck import check_purchase
        result = check_purchase("host_down")
        assert result.allowed is False
        assert result.reason == "host_unavailable"

    def test_maintenance_host_blocked(self, _use_temp_db):
        _insert_host(_use_temp_db, "host_maint", status="healthy", is_maintenance=1)
        from shop_bot.modules.purchase_precheck import check_purchase
        result = check_purchase("host_maint")
        assert result.allowed is False
        assert result.reason == "host_maintenance"

    @pytest.mark.parametrize("delivery_state", ["draft", "disabled"])
    def test_non_active_delivery_host_blocked(self, _use_temp_db, delivery_state):
        _insert_host(
            _use_temp_db,
            f"host_{delivery_state}",
            status="healthy",
            delivery_state=delivery_state,
        )

        from shop_bot.modules.purchase_precheck import check_purchase

        result = check_purchase(f"host_{delivery_state}")

        assert result.allowed is False
        assert result.reason == "host_delivery_disabled"

    def test_global_maintenance_blocks_all(self, _use_temp_db):
        _insert_host(_use_temp_db, "host_ok", status="healthy")
        from shop_bot.data_manager.database import update_setting
        update_setting("maintenance_mode", "true")
        from shop_bot.modules.purchase_precheck import check_purchase
        result = check_purchase("host_ok")
        assert result.allowed is False
        assert result.reason == "global_maintenance"

    def test_nonexistent_host_blocked(self, _use_temp_db):
        from shop_bot.modules.purchase_precheck import check_purchase
        result = check_purchase("ghost_host")
        assert result.allowed is False
        assert result.reason == "host_not_found"

    def test_degraded_host_does_not_block_purchase(self, _use_temp_db, monkeypatch):
        _insert_host(_use_temp_db, "host_deg", status="degraded")

        def _must_not_be_called(**_kwargs):
            raise AssertionError("degraded host should not trigger revalidation in pre-check")

        monkeypatch.setattr("shop_bot.modules.xui_api.login_to_host", _must_not_be_called)
        from shop_bot.modules.purchase_precheck import check_purchase
        result = check_purchase("host_deg")
        assert result.allowed is True

    def test_degraded_host_keeps_status_without_revalidation(self, _use_temp_db, monkeypatch):
        _insert_host(_use_temp_db, "host_deg_recovered", status="degraded")

        def _must_not_be_called(**_kwargs):
            raise AssertionError("degraded host should not trigger revalidation in pre-check")

        monkeypatch.setattr("shop_bot.modules.xui_api.login_to_host", _must_not_be_called)

        from shop_bot.modules.purchase_precheck import check_purchase

        result = check_purchase("host_deg_recovered")
        assert result.allowed is True

        with sqlite3.connect(str(_use_temp_db)) as conn:
            status = conn.execute(
                "SELECT status FROM xui_hosts WHERE host_name = ?",
                ("host_deg_recovered",),
            ).fetchone()[0]
        assert status == "degraded"

    def test_unavailable_host_recovers_after_successful_revalidation(self, _use_temp_db, monkeypatch):
        _insert_host(_use_temp_db, "host_unavailable_recovered", status="unavailable")
        monkeypatch.setattr(
            "shop_bot.modules.purchase_precheck.PRECHECK_REVALIDATE_UNAVAILABLE_HOSTS",
            True,
        )
        monkeypatch.setattr(
            "shop_bot.modules.xui_api.login_to_host",
            lambda **_kwargs: (object(), type("Inbound", (), {"id": 1})()),
        )

        from shop_bot.modules.purchase_precheck import check_purchase

        result = check_purchase("host_unavailable_recovered")
        assert result.allowed is True

    def test_super_precheck_not_configured(self, _use_temp_db):
        from shop_bot.data_manager.database import update_setting
        update_setting("super_key_host_name", "")
        from shop_bot.modules.purchase_precheck import check_super_purchase
        result = check_super_purchase()
        assert result.allowed is False
        assert result.reason == "super_not_configured"

    def test_super_precheck_ok(self, _use_temp_db):
        _insert_host(_use_temp_db, "SUPER_HOST", status="healthy")
        from shop_bot.data_manager.database import update_setting
        update_setting("super_key_host_name", "SUPER_HOST")
        from shop_bot.modules.purchase_precheck import check_super_purchase
        result = check_super_purchase()
        assert result.allowed is True

    def test_precheck_exception_returns_maintenance_block(self, _use_temp_db, monkeypatch):
        from shop_bot.modules import purchase_precheck

        monkeypatch.setattr(
            purchase_precheck,
            "get_maintenance_message",
            lambda: "Покупка временно недоступна, ведутся технические работы. Попробуйте позже.",
        )

        def _boom(_host_name):
            raise RuntimeError("db temporarily unavailable")

        monkeypatch.setattr(purchase_precheck, "get_host", _boom)

        result = purchase_precheck.check_purchase("host_boom")
        assert result.allowed is False
        assert result.reason == "precheck_exception"
        assert "Покупка временно недоступна" in str(result.user_message or "")


# ──────────────────────────────────────────────────────────────
# G1: Host status filtering in load balancing
# ──────────────────────────────────────────────────────────────

class TestHostFiltering:
    @patch("shop_bot.modules.xui_api.get_clients_count_from_panel", return_value=10)
    def test_unavailable_excluded_from_load_balancing(self, mock_count, _use_temp_db):
        _insert_host(_use_temp_db, "alive", status="healthy")
        _insert_host(_use_temp_db, "dead", status="unavailable")
        from shop_bot.data_manager.database import create_plan
        create_plan("alive", "1 месяц", 1, 100)
        create_plan("dead", "1 месяц", 1, 100)
        from shop_bot.data_manager.database import select_least_loaded_host
        result = select_least_loaded_host()
        assert result is not None
        assert result["host_name"] == "alive"

    @patch("shop_bot.modules.xui_api.get_clients_count_from_panel", return_value=10)
    def test_maintenance_excluded_from_load_balancing(self, mock_count, _use_temp_db):
        _insert_host(_use_temp_db, "live_host", status="healthy")
        _insert_host(_use_temp_db, "maint_host", status="healthy", is_maintenance=1)
        from shop_bot.data_manager.database import create_plan
        create_plan("live_host", "1 месяц", 1, 100)
        create_plan("maint_host", "1 месяц", 1, 100)
        from shop_bot.data_manager.database import select_least_loaded_host
        result = select_least_loaded_host()
        assert result is not None
        assert result["host_name"] == "live_host"

    @patch("shop_bot.modules.xui_api.get_clients_count_from_panel", return_value=10)
    def test_all_hosts_down_returns_none(self, mock_count, _use_temp_db):
        _insert_host(_use_temp_db, "down1", status="unavailable")
        _insert_host(_use_temp_db, "down2", status="unavailable")
        from shop_bot.data_manager.database import select_least_loaded_host
        result = select_least_loaded_host()
        assert result is None

    @patch("shop_bot.modules.xui_api.get_clients_count_from_panel")
    def test_hosts_without_plans_are_excluded_from_load_balancing(self, mock_count, _use_temp_db):
        _insert_host(_use_temp_db, "no_plans_host", status="healthy")
        _insert_host(_use_temp_db, "planned_host", status="healthy")

        from shop_bot.data_manager.database import create_plan, select_least_loaded_host

        create_plan("planned_host", "1 месяц", 1, 100)

        def _fake_load(host_name: str) -> int:
            return {"no_plans_host": 1, "planned_host": 5}[host_name]

        mock_count.side_effect = _fake_load

        result = select_least_loaded_host()
        assert result is not None
        assert result["host_name"] == "planned_host"

    @patch("shop_bot.modules.xui_api.get_clients_count_from_panel")
    def test_super_hosts_are_excluded_from_standard_load_balancing(self, mock_count, _use_temp_db):
        _insert_host(_use_temp_db, "Super 2.0", status="healthy")
        _insert_host(_use_temp_db, "planned_host", status="healthy")

        from shop_bot.data_manager.database import create_plan, select_least_loaded_host

        create_plan("Super 2.0", "1 месяц", 1, 300)
        create_plan("planned_host", "1 месяц", 1, 100)

        def _fake_load(host_name: str) -> int:
            return {"Super 2.0": 1, "planned_host": 5}[host_name]

        mock_count.side_effect = _fake_load

        result = select_least_loaded_host()
        assert result is not None
        assert result["host_name"] == "planned_host"

    def test_white_hosts_are_excluded_from_standard_load_balancing(self, _use_temp_db):
        from shop_bot.data_manager.database import create_host, create_plan, select_least_loaded_host

        create_host(
            name="white_host",
            url="https://white.example",
            user="admin",
            passwd="pass",
            inbound=1,
            is_white_host=True,
        )
        create_host(
            name="planned_host",
            url="https://planned.example",
            user="admin",
            passwd="pass",
            inbound=1,
            is_white_host=False,
        )
        create_plan("white_host", "1 месяц", 1, 300)
        create_plan("planned_host", "1 месяц", 1, 100)

        result = select_least_loaded_host()

        assert result is not None
        assert result["host_name"] == "planned_host"

    def test_white_hosts_are_excluded_from_standard_candidates(self, _use_temp_db):
        from shop_bot.data_manager.database import create_host, get_eligible_standard_hosts

        create_host(
            name="white_host",
            url="https://white.example",
            user="admin",
            passwd="pass",
            inbound=1,
            is_white_host=True,
        )
        create_host(
            name="regular_host",
            url="https://regular.example",
            user="admin",
            passwd="pass",
            inbound=1,
            is_white_host=False,
        )

        hosts = get_eligible_standard_hosts(require_plans=False)

        assert [host["host_name"] for host in hosts] == ["regular_host"]

    def test_load_balancing_prefers_cached_panel_counts_without_live_calls(self, _use_temp_db, monkeypatch):
        _insert_host(_use_temp_db, "cache_h1", status="healthy")
        _insert_host(_use_temp_db, "cache_h2", status="healthy")

        from shop_bot.data_manager.database import create_plan, select_least_loaded_host
        from shop_bot.modules import xui_api

        create_plan("cache_h1", "1 месяц", 1, 100)
        create_plan("cache_h2", "1 месяц", 1, 100)

        xui_api.update_host_health_snapshot("cache_h1", status="healthy", clients_count=25)
        xui_api.update_host_health_snapshot("cache_h2", status="healthy", clients_count=4)

        def _must_not_be_called(_host_name: str) -> int:
            raise AssertionError("select_least_loaded_host must not call panel API directly")

        monkeypatch.setattr(xui_api, "get_clients_count_from_panel", _must_not_be_called)

        result = select_least_loaded_host()
        assert result is not None
        assert result["host_name"] == "cache_h2"


# ──────────────────────────────────────────────────────────────
# G3-G4: Migration bonus calculation
# ──────────────────────────────────────────────────────────────



# ──────────────────────────────────────────────────────────────
# G5: Idempotent migration (no duplicate keys)
# ──────────────────────────────────────────────────────────────



# ──────────────────────────────────────────────────────────────
# G7: Incident notification shown exactly once
# ──────────────────────────────────────────────────────────────

class TestIncidentNotification:
    def test_unseen_incident_shown_then_hidden(self, _use_temp_db):
        from shop_bot.data_manager.database import (
            add_migrated_key,
            create_incident,
            get_latest_migrated_key_id_for_user_incident,
            get_unseen_incidents_for_user,
            log_migration,
            mark_incident_seen,
        )

        _insert_user(_use_temp_db, 5005)
        create_incident("INC-001", "Test incident message")
        new_key_id = add_migrated_key(
            user_id=5005,
            host_name="new",
            xui_client_uuid="uuid-5005-new",
            key_email="user5005-new@vpn.bot",
            expiry_timestamp_ms=1_780_310_400_000,
            migrated_from_key_id=999,
            gift_days=3,
            incident_id="INC-001",
            issued_key_value="vless://test",
        )

        log_migration(
            incident_id="INC-001",
            user_id=5005,
            old_key_id=999,
            new_key_id=new_key_id,
            old_host="old",
            new_host="new",
            gift_days=3,
            remaining_days=10.0,
            status="gifted",
        )

        unseen = get_unseen_incidents_for_user(5005)
        assert len(unseen) == 1
        assert unseen[0]["incident_id"] == "INC-001"
        assert get_latest_migrated_key_id_for_user_incident(5005, "INC-001") == new_key_id

        mark_incident_seen(5005, "INC-001")

        unseen_after = get_unseen_incidents_for_user(5005)
        assert len(unseen_after) == 0

    def test_unrelated_user_sees_nothing(self, _use_temp_db):
        from shop_bot.data_manager.database import (
            create_incident,
            get_unseen_incidents_for_user,
            log_migration,
        )

        _insert_user(_use_temp_db, 6006)
        _insert_user(_use_temp_db, 7007)
        create_incident("INC-002", "Only for user 6006")
        log_migration(
            incident_id="INC-002",
            user_id=6006,
            old_key_id=888,
            new_key_id=889,
            old_host="old",
            new_host="new",
            gift_days=3,
            remaining_days=10.0,
            status="migrated",
        )

        assert len(get_unseen_incidents_for_user(6006)) == 1
        assert len(get_unseen_incidents_for_user(7007)) == 0


# ──────────────────────────────────────────────────────────────
# Host health DB functions
# ──────────────────────────────────────────────────────────────

class TestHostHealth:
    def test_update_host_status_healthy(self, _use_temp_db):
        _insert_host(_use_temp_db, "h1", status="unknown")
        from shop_bot.data_manager.database import get_host, update_host_status
        update_host_status("h1", "healthy")
        host = get_host("h1")
        assert host["status"] == "healthy"
        assert host["last_error"] is None

    def test_update_host_status_degraded(self, _use_temp_db):
        _insert_host(_use_temp_db, "h2", status="healthy")
        from shop_bot.data_manager.database import get_host, update_host_status
        update_host_status("h2", "degraded", "SSL error")
        host = get_host("h2")
        assert host["status"] == "degraded"
        assert "SSL" in host["last_error"]
        assert host["raw_status"] == "degraded"

    def test_set_host_maintenance(self, _use_temp_db):
        _insert_host(_use_temp_db, "h3")
        from shop_bot.data_manager.database import is_host_available, set_host_maintenance
        assert is_host_available("h3") is True
        set_host_maintenance("h3", True)
        assert is_host_available("h3") is False
        set_host_maintenance("h3", False)
        assert is_host_available("h3") is True

    def test_stale_healthy_host_is_exposed_as_degraded_in_hosts_with_plans(self, _use_temp_db):
        _insert_host(_use_temp_db, "stale_h", status="healthy")
        stale_check = (datetime.now() - timedelta(minutes=20)).isoformat()
        with sqlite3.connect(str(_use_temp_db)) as conn:
            conn.execute(
                "UPDATE xui_hosts SET last_check_at = ? WHERE host_name = ?",
                (stale_check, "stale_h"),
            )
            conn.commit()

        from shop_bot.data_manager.database import get_hosts_with_plans

        hosts = get_hosts_with_plans()
        stale_host = next(item for item in hosts if item["host_name"] == "stale_h")
        assert stale_host["raw_status"] == "healthy"
        assert stale_host["status"] == "degraded"
        assert stale_host["is_status_stale"] is True
        assert "Нет свежей проверки хоста" in str(stale_host["last_error"])

    def test_stale_healthy_host_remains_available_for_purchase_eligibility(self, _use_temp_db):
        _insert_host(_use_temp_db, "stale_h2", status="healthy")
        stale_check = (datetime.now() - timedelta(minutes=20)).isoformat()
        with sqlite3.connect(str(_use_temp_db)) as conn:
            conn.execute(
                "UPDATE xui_hosts SET last_check_at = ? WHERE host_name = ?",
                (stale_check, "stale_h2"),
            )
            conn.commit()

        from shop_bot.data_manager.database import is_host_available

        assert is_host_available("stale_h2") is True


# ──────────────────────────────────────────────────────────────
# Exact formula: remaining seconds (not ceil days) + gift
# ──────────────────────────────────────────────────────────────



# ──────────────────────────────────────────────────────────────
# include_expired flag
# ──────────────────────────────────────────────────────────────





# ──────────────────────────────────────────────────────────────
# has_active_beta DB field
# ──────────────────────────────────────────────────────────────

class TestActiveBetaField:
    def test_set_and_check_active_beta(self, _use_temp_db):
        _insert_user(_use_temp_db, 10001)
        from shop_bot.data_manager.database import (
            set_user_active_beta,
            user_has_active_beta,
        )
        assert user_has_active_beta(10001) is False
        set_user_active_beta(10001, True)
        assert user_has_active_beta(10001) is True
        set_user_active_beta(10001, False)
        assert user_has_active_beta(10001) is False


class TestKeyNumbering:
    def test_get_next_key_number_uses_max_suffix_not_count(self, _use_temp_db):
        _insert_user(_use_temp_db, 11001)
        with sqlite3.connect(str(_use_temp_db)) as conn:
            conn.execute(
                """INSERT INTO vpn_keys
                   (user_id, host_name, xui_client_uuid, key_email, expiry_date)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    11001,
                    "legacy_host",
                    "uuid-11001",
                    "user11001-key2@vpn.bot",
                    (datetime.now() + timedelta(days=10)).isoformat(),
                ),
            )
            conn.commit()

        from shop_bot.data_manager.database import get_next_key_number
        assert get_next_key_number(11001) == 3




class TestYooKassaPendingReconcile:
    def test_mark_transaction_canceled_from_pending(self, _use_temp_db):
        from shop_bot.data_manager.database import (
            create_pending_transaction,
            mark_transaction_canceled,
        )

        payment_id = "test-yk-cancel-001"
        tx_id = create_pending_transaction(
            payment_id=payment_id,
            user_id=555001,
            amount_rub=100.0,
            metadata={"payment_method": "YooKassa"},
        )
        assert tx_id > 0

        changed = mark_transaction_canceled(payment_id, payment_method="YooKassa")
        assert changed is True

        with sqlite3.connect(str(_use_temp_db)) as conn:
            row = conn.execute(
                "SELECT status, payment_method FROM transactions WHERE payment_id = ?",
                (payment_id,),
            ).fetchone()
            assert row is not None
            assert row[0] == "canceled"
            assert row[1] == "YooKassa"

    def test_get_pending_yookassa_transactions_filters_by_age_and_provider(self, _use_temp_db):
        from shop_bot.data_manager.database import (
            create_pending_transaction,
            get_pending_yookassa_transactions,
        )

        old_yk = "test-yk-old-001"
        old_other = "test-other-old-001"
        fresh_yk = "test-yk-fresh-001"

        create_pending_transaction(
            payment_id=old_yk,
            user_id=555002,
            amount_rub=300.0,
            metadata={"payment_method": "YooKassa", "cms_name": "yookassa_sdk_python"},
        )
        create_pending_transaction(
            payment_id=old_other,
            user_id=555003,
            amount_rub=300.0,
            metadata={"payment_method": "CryptoBot"},
        )
        create_pending_transaction(
            payment_id=fresh_yk,
            user_id=555004,
            amount_rub=300.0,
            metadata={"payment_method": "YooKassa"},
        )

        with sqlite3.connect(str(_use_temp_db)) as conn:
            # Make two rows old enough for min_age_seconds filter.
            conn.execute(
                "UPDATE transactions SET created_date = datetime('now', '-10 minutes') WHERE payment_id IN (?, ?)",
                (old_yk, old_other),
            )
            conn.commit()

        rows = get_pending_yookassa_transactions(limit=50, min_age_seconds=120)
        payment_ids = {row["payment_id"] for row in rows}

        assert old_yk in payment_ids
        assert old_other not in payment_ids
        assert fresh_yk not in payment_ids




class TestBlitzExpiryParsing:
    def test_expiration_days_uses_account_creation_as_anchor(self, _use_temp_db):
        from shop_bot.data_manager.scheduler import _parse_blitz_expiry

        now = datetime(2026, 3, 17, 12, 0, 0)
        local_created_at = datetime(2026, 2, 15, 16, 6, 52)
        user_data = {
            "account_creation_date": "2026-02-15",
            "expiration_days": 30,
        }

        expiry, source = _parse_blitz_expiry(
            user_data,
            now,
            local_created_at=local_created_at,
        )

        assert source == "absolute_from_creation"
        assert expiry == datetime(2026, 3, 17, 16, 6, 52)

    def test_expiration_days_without_creation_date_is_ignored(self, _use_temp_db):
        from shop_bot.data_manager.scheduler import _parse_blitz_expiry

        now = datetime(2026, 3, 17, 12, 0, 0)
        user_data = {"expiration_days": 30}

        expiry, source = _parse_blitz_expiry(user_data, now)

        assert expiry is None
        assert source is None

    def test_remaining_days_still_supported(self, _use_temp_db):
        from shop_bot.data_manager.scheduler import _parse_blitz_expiry

        now = datetime(2026, 3, 17, 12, 0, 0)
        user_data = {"remaining_days": 5}

        expiry, source = _parse_blitz_expiry(user_data, now)

        assert source == "relative_days"
        assert expiry == now + timedelta(days=5)
