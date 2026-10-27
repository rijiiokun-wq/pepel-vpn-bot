from __future__ import annotations

import sys
from pathlib import Path


SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_super_host_exact_name_is_recognized(monkeypatch):
    from shop_bot.bot import handlers

    hosts = {
        "SUPER@": {"host_name": "SUPER@", "host_url": "https://panel.example.test:2666/panel-path/"},
    }
    monkeypatch.setattr(handlers, "get_setting", lambda key: "SUPER@" if key == "super_key_host_name" else None)
    monkeypatch.setattr(handlers, "get_host", hosts.get)

    assert handlers._is_super_key_host_name("SUPER@") is True


def test_super_host_alias_with_same_url_is_recognized(monkeypatch):
    from shop_bot.bot import handlers

    hosts = {
        "SUPER@": {"host_name": "SUPER@", "host_url": "https://panel.example.test:2666/panel-path/"},
        "Super 2.0": {"host_name": "Super 2.0", "host_url": "https://panel.example.test:2666/panel-path"},
        "blitz_hysteria2": {"host_name": "blitz_hysteria2", "host_url": "https://blitz.example/api"},
    }

    monkeypatch.setattr(handlers, "get_setting", lambda key: "SUPER@" if key == "super_key_host_name" else None)
    monkeypatch.setattr(handlers, "get_host", hosts.get)

    assert handlers._is_super_key_host_name("Super 2.0") is True
    assert handlers._is_super_key_host_name("blitz_hysteria2") is False


def test_super_host_alias_returns_false_when_configured_host_missing(monkeypatch):
    from shop_bot.bot import handlers

    hosts = {"Super 2.0": {"host_name": "Super 2.0", "host_url": "https://panel.example.test:2666/panel-path/"}}
    monkeypatch.setattr(handlers, "get_setting", lambda key: "SUPER@" if key == "super_key_host_name" else None)
    monkeypatch.setattr(handlers, "get_host", hosts.get)

    assert handlers._is_super_key_host_name("Super 2.0") is False
