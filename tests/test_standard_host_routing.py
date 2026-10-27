from __future__ import annotations

import sys
from pathlib import Path


SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_standard_host_name_uses_default_when_setting_empty(monkeypatch):
    from shop_bot.bot import handlers

    monkeypatch.setattr(
        handlers,
        "get_setting",
        lambda key: "" if key == "standard_key_host_name" else None,
    )

    assert handlers._get_standard_key_host_name() == "StandartVLESS"


def test_standard_host_name_uses_setting_override(monkeypatch):
    from shop_bot.bot import handlers

    monkeypatch.setattr(
        handlers,
        "get_setting",
        lambda key: "StandartVLESS-2" if key == "standard_key_host_name" else None,
    )

    assert handlers._get_standard_key_host_name() == "StandartVLESS-2"


def test_purchase_availability_uses_standard_host(monkeypatch):
    from shop_bot.bot import handlers

    def _fake_get_setting(key: str):
        if key == "maintenance_mode":
            return "false"
        if key == "maintenance_message":
            return ""
        if key == "standard_key_host_name":
            return "StandartVLESS"
        return None

    monkeypatch.setattr(handlers, "get_setting", _fake_get_setting)
    monkeypatch.setattr(
        handlers,
        "get_host",
        lambda host_name: (
            {"host_name": "StandartVLESS", "status": "healthy", "is_maintenance": False}
            if host_name == "StandartVLESS"
            else None
        ),
    )
    monkeypatch.setattr(
        handlers,
        "_run_purchase_precheck",
        lambda *_args, **_kwargs: handlers.PreCheckResult(allowed=True),
    )
    monkeypatch.setattr(
        handlers,
        "get_plans_for_host",
        lambda host_name: [{"plan_id": 1}] if host_name == "StandartVLESS" else [],
    )
    monkeypatch.setattr(handlers, "_get_super_key_host_name", lambda: None)
    monkeypatch.setattr(handlers, "_get_super_key_host", lambda: None)

    result = handlers._resolve_purchase_availability_sync()

    assert result["standard_allowed"] is True


def test_purchase_availability_blocks_when_standard_host_missing(monkeypatch):
    from shop_bot.bot import handlers

    def _fake_get_setting(key: str):
        if key == "maintenance_mode":
            return "false"
        if key == "maintenance_message":
            return ""
        if key == "standard_key_host_name":
            return "StandartVLESS"
        return None

    monkeypatch.setattr(handlers, "get_setting", _fake_get_setting)
    monkeypatch.setattr(handlers, "get_host", lambda _host_name: None)
    monkeypatch.setattr(handlers, "_get_super_key_host_name", lambda: None)
    monkeypatch.setattr(handlers, "_get_super_key_host", lambda: None)

    result = handlers._resolve_purchase_availability_sync()

    assert result["standard_allowed"] is False
    assert "StandartVLESS" in str(result["standard_message"])
