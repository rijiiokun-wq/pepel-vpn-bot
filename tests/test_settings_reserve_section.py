from pathlib import Path


def _read_settings_template() -> str:
    template_path = (
        Path(__file__).resolve().parents[1] / "src" / "shop_bot" / "webhook_server" / "templates" / "settings.html"
    )
    return template_path.read_text(encoding="utf-8")


def test_settings_template_contains_reserve_bot_section_and_controls():
    template = _read_settings_template()

    required_snippets = [
        "Резервный бот",
        "settings-group settings-group-management",
        'name="reserve_bot_token"',
        'name="reserve_bot_username"',
        "Runtime резервного бота",
        "settings-runtime-subsection",
        "Сохранить изменения",
        "url_for('start_shop_bot_route')",
        "url_for('stop_shop_bot_route')",
        "runtime_bot_source == 'reserve'",
    ]

    for snippet in required_snippets:
        assert snippet in template

    reserve_section_anchor = template.find("Резервный бот")
    assert reserve_section_anchor >= 0
    reserve_section_window = template[reserve_section_anchor : reserve_section_anchor + 3200]
    assert (
        "settings-group settings-group-management"
        in template[max(0, reserve_section_anchor - 200) : reserve_section_anchor]
    )
    assert "Runtime резервного бота" in reserve_section_window
    assert 'name="paypear_secret_key"' not in reserve_section_window
    assert 'name="paypear_shop_id"' not in reserve_section_window
    assert 'name="paypear_webhook_secret"' not in reserve_section_window
    assert 'name="paypear_webhook_url"' not in reserve_section_window


def test_settings_template_places_paypear_inside_payment_systems():
    template = _read_settings_template()

    required_snippets = [
        "Платёжные системы",
        "<h3>PayPear</h3>",
        'name="paypear_secret_key"',
        'name="paypear_shop_id"',
        'name="paypear_webhook_secret"',
        'name="paypear_webhook_url"',
        'name="paypear_api_base_url"',
        "https://engine.paypear.io/api/v1",
    ]

    for snippet in required_snippets:
        assert snippet in template

    payments_anchor = template.find("<h2>Платёжные системы</h2>")
    assert payments_anchor >= 0
    payments_window = template[payments_anchor : payments_anchor + 3200]
    assert "<h3>PayPear</h3>" in payments_window


def test_settings_template_contains_main_bot_temporary_controls():
    template = _read_settings_template()

    required_snippets = [
        'name="yookassa_payments_enabled"',
        "Разрешить оплату в основном боте",
        'name="main_service_mode_enabled"',
        "Включить заглушку основного бота",
        'name="main_service_mode_message"',
        'name="main_service_mode_reserve_bot_url"',
    ]

    for snippet in required_snippets:
        assert snippet in template


def test_settings_template_groups_settings_by_domain():
    template = _read_settings_template()

    required_snippets = [
        'data-settings-group="management"',
        'data-settings-group="delivery"',
        'data-settings-group="sales"',
        'data-settings-group="support"',
        'data-settings-group="showcase"',
        'data-settings-group="operations"',
        "Панель и боты",
        "Хосты и выдача",
        "Продажи",
        "Контент и вход",
        "Контроль",
        "URL подписки для клиентов:",
        "Выдача подписок",
        "Хост для выдачи подписки Супер:",
        "Тексты и ссылки",
        "Первый вход и инструкции",
        "Требовать подписку на канал перед началом работы",
        "Telegram message effects",
        'name="welcome_message_effect_id"',
        'name="onboarding_success_message_effect_id"',
        'name="trial_success_message_effect_id"',
        'name="purchase_success_message_effect_id"',
    ]

    for snippet in required_snippets:
        assert snippet in template

    super_key_anchor = template.find("Выдача подписок")
    assert super_key_anchor >= 0
    super_key_window = template[max(0, super_key_anchor - 200) : super_key_anchor]
    assert "settings-group settings-group-delivery" in super_key_window

    onboarding_anchor = template.find("Первый вход и инструкции")
    assert onboarding_anchor >= 0
    onboarding_window = template[max(0, onboarding_anchor - 120) : onboarding_anchor]
    assert "settings-group settings-group-showcase" in onboarding_window


def test_settings_template_uses_dialogs_for_host_actions():
    template = _read_settings_template()

    required_snippets = [
        'data-dialog-open="add-host-dialog"',
        'id="add-host-dialog"',
        "settings-dialog host-settings-dialog",
        "update_host_config_route",
        'name="host_subscription_base_url"',
        'name="host_username"',
        'name="host_pass"',
        'name="host_inbound_id"',
        "Сохранить параметры",
        "Read-only: node_id",
        "data-token-source",
        'name="host_api_token_mode"',
        'name="host_api_token_env_choice"',
        'name="host_api_token_direct"',
        'name="is_white_host"',
        "White host (Стандарт — 5 ГБ/мес., Супер — 40 ГБ/мес.)",
        "Жизненный цикл хоста",
        "Проверить хост",
        "Активировать",
        "Отключить",
        "Dry-run 50",
        "Backfill 50",
        "data-host-sync-time",
        "data-host-sync-error",
        "Добавить тариф",
    ]

    for snippet in required_snippets:
        assert snippet in template

    host_card_window = template[
        template.find('data-host-card="{{ host.host_name }}"') : template.find("host-settings-dialog")
    ]
    assert "Добавить новый тариф" not in host_card_window
    assert "host-token-form" not in host_card_window
    assert 'value="{{ host.host_pass }}"' not in template
