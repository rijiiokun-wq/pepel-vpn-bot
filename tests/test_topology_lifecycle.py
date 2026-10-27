from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


@pytest.fixture
def topology_db(tmp_path, monkeypatch):
    from shop_bot.data_manager import database

    db_file = tmp_path / "topology.db"
    monkeypatch.setattr(database, "DB_FILE", db_file)
    monkeypatch.setattr(database, "_WAL_INITIALIZED", False)
    database.initialize_db()
    return db_file


def test_migration_backfills_legacy_roles_and_states(tmp_path, monkeypatch):
    from shop_bot.data_manager import database

    db_file = tmp_path / "legacy.db"
    with sqlite3.connect(db_file) as conn:
        conn.execute(
            """
            CREATE TABLE xui_hosts (
                host_name TEXT NOT NULL,
                host_url TEXT NOT NULL,
                host_username TEXT NOT NULL,
                host_pass TEXT NOT NULL,
                host_inbound_id INTEGER NOT NULL,
                is_white_host INTEGER DEFAULT 0,
                delivery_state TEXT DEFAULT 'active'
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO xui_hosts
                (host_name, host_url, host_username, host_pass, host_inbound_id,
                 is_white_host, delivery_state)
            VALUES (?, 'https://panel.example', 'admin', 'pass', 1, ?, ?)
            """,
            (
                ("ordinary-active", 0, "active"),
                ("white-draft", 1, "draft"),
                ("ordinary-disabled", 0, "disabled"),
            ),
        )

    monkeypatch.setattr(database, "DB_FILE", db_file)
    monkeypatch.setattr(database, "_WAL_INITIALIZED", False)
    database.initialize_db()

    assert database.get_host("ordinary-active")["topology_role"] == "ordinary"
    assert database.get_host("ordinary-active")["topology_state"] == "delivery_active"
    assert database.get_host("white-draft")["topology_role"] == "whitelist"
    assert database.get_host("white-draft")["topology_state"] == "registered"
    assert database.get_host("ordinary-disabled")["topology_state"] == "disabled"


def test_transition_chain_is_audited_and_does_not_change_legacy_delivery(topology_db):
    from shop_bot.data_manager import database

    database.create_host(
        "estonia-2",
        "https://estonia-2.example",
        "admin",
        "pass",
        1,
        delivery_state="draft",
    )

    operation_id = "add-estonia-2"
    for expected, target in (
        ("registered", "provisioning"),
        ("provisioning", "backfilling"),
        ("backfilling", "verifying"),
        ("verifying", "delivery_active"),
    ):
        updated = database.transition_host_topology_state(
            "estonia-2",
            target,
            actor="test-suite",
            operation_id=operation_id,
            expected_state=expected,
            reason=f"advance to {target}",
        )
        assert updated["topology_state"] == target
        assert updated["delivery_state"] == "draft"

    with sqlite3.connect(topology_db) as conn:
        version = conn.execute("SELECT version FROM host_topology_state WHERE id = 1").fetchone()[0]
        events = conn.execute(
            """
            SELECT from_state, to_state, operation_id, actor
            FROM host_topology_events
            WHERE host_name = ? ORDER BY id
            """,
            ("estonia-2",),
        ).fetchall()

    assert version == 5
    assert events == [
        ("registered", "provisioning", operation_id, "test-suite"),
        ("provisioning", "backfilling", operation_id, "test-suite"),
        ("backfilling", "verifying", operation_id, "test-suite"),
        ("verifying", "delivery_active", operation_id, "test-suite"),
    ]


def test_invalid_and_stale_transitions_leave_state_unchanged(topology_db):
    from shop_bot.data_manager import database

    database.create_host(
        "safe-host",
        "https://safe.example",
        "admin",
        "pass",
        1,
        delivery_state="draft",
    )

    with pytest.raises(ValueError, match="registered -> delivery_active"):
        database.transition_host_topology_state(
            "safe-host",
            "delivery_active",
            actor="test-suite",
        )
    with pytest.raises(RuntimeError, match="expected disabled, got registered"):
        database.transition_host_topology_state(
            "safe-host",
            "provisioning",
            actor="test-suite",
            expected_state="disabled",
        )

    assert database.get_host("safe-host")["topology_state"] == "registered"
    with sqlite3.connect(topology_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM host_topology_events").fetchone()[0] == 0


def test_snapshot_contains_only_active_hosts_allowed_for_tier(topology_db):
    from shop_bot.data_manager import database

    database.create_host("ordinary", "https://ordinary.example", "admin", "pass", 1)
    database.create_host(
        "whitelist",
        "https://whitelist.example",
        "admin",
        "pass",
        1,
        is_white_host=True,
    )
    database.create_host("warming-up", "https://warming.example", "admin", "pass", 1)
    with sqlite3.connect(topology_db) as conn:
        conn.execute(
            "UPDATE xui_hosts SET topology_state = 'provisioning' WHERE host_name = 'warming-up'"
        )

    standard = database.get_host_topology_snapshot("standard")
    super_plan = database.get_host_topology_snapshot("super")

    assert [host["host_name"] for host in standard["hosts"]] == ["ordinary", "whitelist"]
    assert [host["host_name"] for host in super_plan["hosts"]] == ["ordinary", "whitelist"]
    assert standard["version"] == 1
