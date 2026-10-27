import pytest

pytest.importorskip("aiogram")

from shop_bot.bot import keyboards


def _row_callbacks(markup) -> list[list[str]]:
    return [
        [button.callback_data for button in row if button.callback_data]
        for row in markup.inline_keyboard
    ]


def _row_dicts(markup) -> list[list[dict]]:
    return [
        [button.model_dump(exclude_none=True) for button in row]
        for row in markup.inline_keyboard
    ]


def test_main_menu_replaces_referral_button_with_howto(monkeypatch):
    monkeypatch.setattr(keyboards, "is_super_key_enabled", lambda: False)

    markup = keyboards.create_main_menu_keyboard(
        user_keys=[{"id": 1}, {"id": 2}],
        trial_standard_available=False,
        trial_super_available=False,
        is_admin=False,
    )

    rows = _row_callbacks(markup)
    callbacks = [callback for row in rows for callback in row]

    assert "show_referral_program" not in callbacks
    assert rows == [
        ["buy_new_key_main"],
        ["show_profile", "manage_keys"],
        ["howto_vless", "show_help"],
    ]


def test_main_menu_applies_native_telegram_styles(monkeypatch):
    monkeypatch.setattr(keyboards, "is_super_key_enabled", lambda: True)

    markup = keyboards.create_main_menu_keyboard(
        user_keys=[],
        trial_standard_available=True,
        trial_super_available=True,
        is_admin=True,
    )

    rows = _row_dicts(markup)
    reply_button = keyboards.main_reply_keyboard.keyboard[0][0].model_dump(exclude_none=True)

    assert reply_button["style"] == keyboards.BUTTON_STYLE_PRIMARY
    assert rows[0][0]["style"] == keyboards.BUTTON_STYLE_SUCCESS
    assert rows[1][0]["style"] == keyboards.BUTTON_STYLE_SUCCESS
    assert rows[2][0]["style"] == keyboards.BUTTON_STYLE_SUCCESS
    assert rows[3][0]["style"] == keyboards.BUTTON_STYLE_SUCCESS
    assert rows[5][0]["style"] == keyboards.BUTTON_STYLE_PRIMARY
    assert rows[5][1]["style"] == keyboards.BUTTON_STYLE_DANGER
    assert rows[6][0]["style"] == keyboards.BUTTON_STYLE_PRIMARY


def test_main_menu_hides_purchase_buttons_when_unavailable(monkeypatch):
    monkeypatch.setattr(keyboards, "is_super_key_enabled", lambda: True)

    markup = keyboards.create_main_menu_keyboard(
        user_keys=[],
        trial_standard_available=False,
        trial_super_available=False,
        is_admin=False,
        show_standard_purchase=False,
        show_super_purchase=False,
    )
    callbacks = [cb for row in _row_callbacks(markup) for cb in row]

    assert "buy_new_key_main" not in callbacks
    assert "buy_super_key_main" not in callbacks


def test_key_info_keyboard_uses_extend_wording_for_refresh_action():
    markup = keyboards.create_key_info_keyboard(
        key_id=42,
        show_refresh=True,
        show_qr=False,
    )

    button_texts = [button.text for row in markup.inline_keyboard for button in row]

    assert "🔄 Продлить ключ" in button_texts
    assert "🔄 Обновить ключ" not in button_texts
