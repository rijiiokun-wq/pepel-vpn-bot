import pytest

pytest.importorskip("aiogram")

from shop_bot.bot import keyboards


def _callback_data_list(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def _button_dicts(markup) -> list[dict]:
    return [
        button.model_dump(exclude_none=True)
        for row in markup.inline_keyboard
        for button in row
    ]


def test_keyboard_shows_paypear_for_reserve_matrix(monkeypatch):
    monkeypatch.setattr(keyboards, "BOT_SOURCE", "reserve")
    monkeypatch.setattr(keyboards, "get_setting", lambda _key: "false")
    markup = keyboards.create_payment_method_keyboard(
        payment_methods={"paypear": True, "yookassa": False},
    )
    callbacks = _callback_data_list(markup)

    assert "pay_paypear_sbp" in callbacks
    assert "pay_paypear_card" in callbacks
    assert "pay_yookassa" not in callbacks


def test_payment_method_keyboard_styles_success_cta(monkeypatch):
    monkeypatch.setattr(keyboards, "BOT_SOURCE", "main")
    monkeypatch.setattr(keyboards, "get_setting", lambda _key: "true")
    markup = keyboards.create_payment_method_keyboard(
        payment_methods={"paypear": False, "yookassa": True, "cryptobot": True},
    )
    buttons = _button_dicts(markup)

    pay_buttons = [button for button in buttons if button.get("callback_data") in {"pay_yookassa", "pay_cryptobot"}]
    back_button = next(button for button in buttons if button.get("callback_data") == "back_to_email_prompt")

    assert pay_buttons
    assert all(button.get("style") == keyboards.BUTTON_STYLE_SUCCESS for button in pay_buttons)
    assert "style" not in back_button


def test_keyboard_shows_yookassa_for_main_matrix(monkeypatch):
    monkeypatch.setattr(keyboards, "BOT_SOURCE", "main")
    monkeypatch.setattr(
        keyboards,
        "get_setting",
        lambda key: "true" if key == "yookassa_payments_enabled" else "false",
    )
    markup = keyboards.create_payment_method_keyboard(
        payment_methods={"paypear": False, "yookassa": True},
    )
    callbacks = _callback_data_list(markup)

    assert "pay_yookassa" in callbacks
    assert "pay_paypear_sbp" not in callbacks


def test_keyboard_hides_all_main_payments_when_admin_toggle_disabled(monkeypatch):
    monkeypatch.setattr(keyboards, "BOT_SOURCE", "main")
    monkeypatch.setattr(keyboards, "get_setting", lambda _key: "false")
    markup = keyboards.create_payment_method_keyboard(
        payment_methods={
            "paypear": True,
            "yookassa": True,
            "cryptobot": True,
            "heleket": True,
            "tonconnect": True,
        },
    )
    callbacks = _callback_data_list(markup)

    assert "pay_yookassa" not in callbacks
    assert "pay_paypear_sbp" not in callbacks
    assert "pay_paypear_card" not in callbacks
    assert "pay_cryptobot" not in callbacks
    assert "pay_heleket" not in callbacks
    assert "pay_tonconnect" not in callbacks
    assert callbacks == ["back_to_email_prompt"]
