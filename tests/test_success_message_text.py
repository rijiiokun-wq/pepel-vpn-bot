from datetime import UTC, datetime

from shop_bot.config import (
    get_purchase_success_text,
    get_subscription_ready_notification_template,
    get_trial_success_text,
)

def test_subscription_notification_template_uses_current_quota_without_legacy_helper_key():
    template = get_subscription_ready_notification_template()

    assert "WHITELIST — 40 ГБ в месяц." in template
    assert "15 гигабайт/мес." not in template
    assert "вспомогательный ключ" not in template.lower()
    assert "hysteria2://" not in template
    assert "<pre>{key}</pre>" in template


def test_purchase_success_text_uses_subscription_copy_with_dynamic_fields():
    text = get_purchase_success_text(
        action="создан",
        key_number=39,
        expiry_date=datetime(2026, 7, 11, 14, 17, tzinfo=UTC),
        subscription_link="https://pepelbot.com/podpiska/demo",
        host_name="MainPanel",
        key_email="user442482830-key39@vpn.bot",
    )

    assert "Уважаемый пользователь, ваша новая подписка готова" in text
    assert "Ваш ключ" not in text
    assert "Он будет" not in text
    assert "Хост: <b>MainPanel</b>" in text
    assert "Срок до: <b>11.07.2026 14:17</b>" in text
    assert "Email: <code>user442482830-key39@vpn.bot</code>" in text
    assert "<pre>https://pepelbot.com/podpiska/demo</pre>" in text


def test_manual_migration_message_uses_purchase_and_renewal_template():
    from shop_bot.bot import handlers

    expiry_date = datetime(2026, 7, 11, 14, 17, tzinfo=UTC)
    actual = handlers._render_manual_migration_key_text(
        host_name="MainPanel",
        expiry_date=expiry_date,
        key_value="https://pepelbot.com/podpiska/manual",
        key_email="user442482830-key40@vpn.bot",
        key_id=40,
    )
    expected = get_purchase_success_text(
        action="выдан",
        key_number=40,
        expiry_date=expiry_date,
        connection_string="https://pepelbot.com/podpiska/manual",
        host_name="MainPanel",
        key_email="user442482830-key40@vpn.bot",
    )

    assert actual == expected


def test_trial_success_text_uses_test_subscription_copy_with_dynamic_fields():
    text = get_trial_success_text(
        expiry_date=datetime(2026, 6, 13, 14, 17, tzinfo=UTC),
        subscription_link="https://pepelbot.com/podpiska/trial",
        host_name="MainPanel",
        key_email="user442482830-key1-trial-super@telegram.bot",
    )

    assert "Уважаемый пользователь, ваша новая тестовая подписка готова" in text
    assert "Ваш ключ" not in text
    assert "Он будет" not in text
    assert "Хост: <b>MainPanel</b>" in text
    assert "Срок до: <b>13.06.2026 14:17</b>" in text
    assert "Email: <code>user442482830-key1-trial-super@telegram.bot</code>" in text
    assert "<pre>https://pepelbot.com/podpiska/trial</pre>" in text
