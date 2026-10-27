from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from shop_bot.data_manager import database


def test_resolve_db_file_path_prefers_env_absolute(tmp_path):
    custom_db = tmp_path / "custom" / "shop.db"
    resolved = database._resolve_db_file_path(
        env_value=str(custom_db),
        data_candidate=tmp_path / "data" / "shop.db",
        default_candidate=tmp_path / "users.db",
    )
    assert resolved == custom_db


def test_resolve_db_file_path_resolves_relative_env_to_cwd(tmp_path):
    resolved = database._resolve_db_file_path(
        env_value="data/shop.db",
        data_candidate=tmp_path / "ignored_data" / "shop.db",
        default_candidate=tmp_path / "users.db",
        cwd=tmp_path,
    )
    assert resolved == (tmp_path / "data" / "shop.db").resolve()


def test_resolve_db_file_path_falls_back_to_data_candidate_when_exists(tmp_path):
    data_db = tmp_path / "data" / "shop.db"
    data_db.parent.mkdir(parents=True, exist_ok=True)
    data_db.touch()

    resolved = database._resolve_db_file_path(
        env_value="",
        data_candidate=data_db,
        default_candidate=tmp_path / "users.db",
    )
    assert resolved == data_db


def test_resolve_db_file_path_falls_back_to_default_when_data_missing(tmp_path):
    default_db = tmp_path / "users.db"
    resolved = database._resolve_db_file_path(
        env_value="",
        data_candidate=tmp_path / "data" / "shop.db",
        default_candidate=default_db,
    )
    assert resolved == default_db


def test_ensure_db_parent_dir_creates_parent(tmp_path):
    target_db = tmp_path / "nested" / "dir" / "shop.db"
    assert not target_db.parent.exists()
    database._ensure_db_parent_dir(target_db)
    assert target_db.parent.exists()
