from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from shop_bot.data_manager import database


def _setup_temp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "test_users.db"
    monkeypatch.setattr(database, "DB_FILE", db_file)
    database.initialize_db()
    return db_file


def test_add_new_key_idempotent_for_same_user_email(monkeypatch, tmp_path):
    db_file = _setup_temp_db(monkeypatch, tmp_path)

    key_id_1 = database.add_new_key(
        user_id=100,
        host_name="SUPER",
        xui_client_uuid="uuid-1",
        key_email="user100-key1@vpn.bot",
        expiry_timestamp_ms=1_800_000_000_000,
    )
    assert key_id_1 is not None

    key_id_2 = database.add_new_key(
        user_id=100,
        host_name="SUPER",
        xui_client_uuid="uuid-2",
        key_email="user100-key1@vpn.bot",
        expiry_timestamp_ms=1_800_000_100_000,
    )
    assert key_id_2 == key_id_1

    with sqlite3.connect(db_file) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM vpn_keys WHERE key_email = ?", ("user100-key1@vpn.bot",))
        assert int(cursor.fetchone()[0] or 0) == 1


def test_add_new_key_duplicate_email_for_other_user_fails(monkeypatch, tmp_path):
    _setup_temp_db(monkeypatch, tmp_path)

    key_id_1 = database.add_new_key(
        user_id=101,
        host_name="SUPER",
        xui_client_uuid="uuid-a",
        key_email="shared-key@vpn.bot",
        expiry_timestamp_ms=1_800_000_000_000,
    )
    assert key_id_1 is not None

    key_id_2 = database.add_new_key(
        user_id=202,
        host_name="SUPER",
        xui_client_uuid="uuid-b",
        key_email="shared-key@vpn.bot",
        expiry_timestamp_ms=1_800_000_000_000,
    )
    assert key_id_2 is None


def test_add_new_key_stores_active_panel_sub_id(monkeypatch, tmp_path):
    _setup_temp_db(monkeypatch, tmp_path)

    key_id = database.add_new_key(
        user_id=100,
        host_name="StandartVLESS",
        xui_client_uuid="uuid-1",
        key_email="user100-key1@vpn.bot",
        expiry_timestamp_ms=1_800_000_000_000,
        panel_sub_id="sub-123",
    )

    assert key_id is not None
    key = database.get_active_key_by_panel_sub_id("sub-123")
    assert key is not None
    assert key["key_id"] == key_id
    assert key["host_name"] == "StandartVLESS"


def test_add_new_key_stores_subscription_payload(monkeypatch, tmp_path):
    _setup_temp_db(monkeypatch, tmp_path)

    key_id = database.add_new_key(
        user_id=100,
        host_name="StandartVLESS",
        xui_client_uuid="uuid-1",
        key_email="user100-key1@vpn.bot",
        expiry_timestamp_ms=1_800_000_000_000,
        panel_sub_id="sub-123",
        subscription_payload="vless://uuid@cdn.example:443?type=xhttp&security=tls#cached",
    )

    assert key_id is not None
    key = database.get_active_key_by_panel_sub_id("sub-123")
    assert key is not None
    assert key["subscription_payload"] == "vless://uuid@cdn.example:443?type=xhttp&security=tls#cached"
