from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_register_user_preserves_real_username_from_fallback(monkeypatch, tmp_path):
    from shop_bot.data_manager import database

    db_file = tmp_path / "users.db"
    monkeypatch.setattr(database, "DB_FILE", db_file)
    monkeypatch.setattr(database, "_WAL_INITIALIZED", False)
    database.initialize_db()

    user_id = 123456789
    database.register_user_if_not_exists(user_id, "real_user", None)
    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)

    user = database.get_user(user_id)
    assert user is not None
    assert user["username"] == "real_user"


def test_register_user_replaces_fallback_with_real_username(monkeypatch, tmp_path):
    from shop_bot.data_manager import database

    db_file = tmp_path / "users.db"
    monkeypatch.setattr(database, "DB_FILE", db_file)
    monkeypatch.setattr(database, "_WAL_INITIALIZED", False)
    database.initialize_db()

    user_id = 123456789
    database.register_user_if_not_exists(user_id, f"user_{user_id}", None)
    database.register_user_if_not_exists(user_id, "real_user", None)

    user = database.get_user(user_id)
    assert user is not None
    assert user["username"] == "real_user"
