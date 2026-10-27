from __future__ import annotations

import sqlite3


def test_get_key_by_panel_sub_id_includes_expired_and_archived_rows(tmp_path, monkeypatch):
    from shop_bot.data_manager import database

    db_file = tmp_path / "shop.db"
    monkeypatch.setattr(database, "DB_FILE", db_file)

    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE vpn_keys (
                key_id INTEGER PRIMARY KEY,
                panel_sub_id TEXT,
                issued_key_value TEXT,
                subscription_payload TEXT,
                expiry_date TEXT,
                is_archived INTEGER DEFAULT 0
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO vpn_keys (
                key_id,
                panel_sub_id,
                issued_key_value,
                subscription_payload,
                expiry_date,
                is_archived
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (1, "expired-direct", None, None, "2020-01-01T00:00:00", 0),
                (
                    2,
                    None,
                    "https://example.test/podpiska/synthetic-sub-001",
                    None,
                    "2099-01-01T00:00:00",
                    1,
                ),
                (
                    3,
                    None,
                    "https://example.test/podpiska/literal%_legacy",
                    None,
                    "2099-01-01T00:00:00",
                    0,
                ),
            ],
        )

    direct = database.get_key_by_panel_sub_id("expired-direct")
    legacy = database.get_key_by_panel_sub_id("synthetic-sub-001")

    assert direct is not None
    assert direct["key_id"] == 1
    assert legacy is not None
    assert legacy["key_id"] == 2
    wildcard_literal = database.get_key_by_panel_sub_id("literal%_legacy")
    assert wildcard_literal is not None
    assert wildcard_literal["key_id"] == 3
    active_wildcard_literal = database.get_active_key_by_panel_sub_id("literal%_legacy")
    assert active_wildcard_literal is not None
    assert active_wildcard_literal["key_id"] == 3
    assert database.get_key_by_panel_sub_id("missing") is None
