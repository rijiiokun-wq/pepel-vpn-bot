from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_extend_key_display_name_marks_trial_origin():
    from shop_bot.bot import handlers

    assert (
        handlers._resolve_extend_key_display_name(
            original_email="user42-key1-trial-super@telegram.bot"
        )
        == "Выданный после триала"
    )
    assert (
        handlers._resolve_extend_key_display_name(
            original_email="user42-key1@vpn.bot"
        )
        == "Выданный после продления"
    )


def test_resolve_extend_email_target_returns_none_for_canonical_email():
    from shop_bot.bot import handlers

    assert (
        handlers._resolve_extend_email_target(
            user_id=42,
            key_id=7,
            original_email="user42-key7@vpn.bot",
        )
        is None
    )


def test_resolve_extend_email_target_preserves_trial_panel_identity():
    from shop_bot.bot import handlers

    assert handlers._resolve_extend_email_target(
        user_id=42,
        key_id=7,
        original_email="user42-key1-trial-super@telegram.bot",
    ) is None


def test_resolve_available_extend_email_uses_reserve_suffix_on_collision(monkeypatch: pytest.MonkeyPatch):
    from shop_bot.bot import handlers

    def _fake_get_key_by_email(email: str):
        if email == "user55-key9@vpn.bot":
            return {"key_id": 1001}
        return None

    monkeypatch.setattr(handlers, "get_key_by_email", _fake_get_key_by_email)

    assert (
        handlers._resolve_available_extend_email(
            user_id=55,
            key_id=9,
            current_key_id=9,
        )
        == "user55-key9-r1@vpn.bot"
    )


def test_resolve_available_extend_email_keeps_canonical_when_same_key(monkeypatch: pytest.MonkeyPatch):
    from shop_bot.bot import handlers

    def _fake_get_key_by_email(email: str):
        if email == "user55-key9@vpn.bot":
            return {"key_id": 9}
        return None

    monkeypatch.setattr(handlers, "get_key_by_email", _fake_get_key_by_email)

    assert (
        handlers._resolve_available_extend_email(
            user_id=55,
            key_id=9,
            current_key_id=9,
        )
        == "user55-key9@vpn.bot"
    )
