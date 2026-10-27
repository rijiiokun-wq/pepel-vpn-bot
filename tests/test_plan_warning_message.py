from shop_bot.bot import handlers


def test_standard_plan_message_states_current_whitelist_quota():
    message = handlers.STANDARD_KEY_NETWORK_WARNING
    assert "<b>Стандарт:</b> WHITELIST — 5 ГБ в месяц." in message
    assert "Остальные серверы — безлимитные." in message
    assert "может работать нестабильно" not in message
