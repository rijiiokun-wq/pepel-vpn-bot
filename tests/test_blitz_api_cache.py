from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_get_auth_context_prunes_expired_entries_and_respects_cap(monkeypatch):
    from shop_bot.modules import blitz_api

    blitz_api._AUTH_CACHE.clear()
    monkeypatch.setattr(blitz_api, "BLITZ_AUTH_CACHE_MAX_SIZE", 3)
    monkeypatch.setattr(blitz_api, "BLITZ_AUTH_CACHE_TTL_SECONDS", 10)

    now_ref = {"value": 1_000.0}
    monkeypatch.setattr(blitz_api.time, "monotonic", lambda: now_ref["value"])

    blitz_api._AUTH_CACHE["https://expired.example|old"] = (
        {"headers": {"Authorization": "expired"}, "cookies": None},
        900.0,
    )

    for idx in range(5):
        cfg = {
            "url": f"https://node-{idx}.example",
            "api_token": f"token-{idx}",
            "username": "",
            "password": "",
        }
        asyncio.run(blitz_api._get_auth_context(cfg))

    assert "https://expired.example|old" not in blitz_api._AUTH_CACHE
    assert "https://node-0.example|token-0" not in blitz_api._AUTH_CACHE
    assert "https://node-4.example|token-4" in blitz_api._AUTH_CACHE
    assert len(blitz_api._AUTH_CACHE) == 3


def test_get_auth_context_reuses_cached_entry_within_ttl(monkeypatch):
    from shop_bot.modules import blitz_api

    blitz_api._AUTH_CACHE.clear()
    monkeypatch.setattr(blitz_api, "BLITZ_AUTH_CACHE_MAX_SIZE", 8)
    monkeypatch.setattr(blitz_api, "BLITZ_AUTH_CACHE_TTL_SECONDS", 30)

    now_ref = {"value": 2_000.0}
    monkeypatch.setattr(blitz_api.time, "monotonic", lambda: now_ref["value"])

    cfg = {
        "url": "https://single-node.example",
        "api_token": "single-token",
        "username": "",
        "password": "",
    }

    first = asyncio.run(blitz_api._get_auth_context(cfg))
    now_ref["value"] += 5
    second = asyncio.run(blitz_api._get_auth_context(cfg))

    assert first == second
    assert len(blitz_api._AUTH_CACHE) == 1
