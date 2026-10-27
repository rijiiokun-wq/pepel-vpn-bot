import logging
import os
from html import escape as html_escape

logger = logging.getLogger(__name__)

_raw_bot_source = (os.getenv("BOT_SOURCE", "main") or "main").strip().lower()
if _raw_bot_source not in {"main", "reserve"}:
    logger.warning("Unknown BOT_SOURCE=%r. Falling back to 'main'.", _raw_bot_source)
    BOT_SOURCE = "main"
else:
    BOT_SOURCE = _raw_bot_source


def is_truthy(value: str | None) -> bool:
    """Check whether a string setting represents a truthy value."""
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


CHOOSE_PLAN_MESSAGE = "Выберите подходящий тариф:"
CHOOSE_PAYMENT_METHOD_MESSAGE = "Выберите удобный способ оплаты:"
VPN_INACTIVE_TEXT = "❌ <b>Статус VPN:</b> Неактивен (срок истек)"
VPN_NO_DATA_TEXT = "ℹ️ <b>Статус VPN:</b> У вас пока нет активных ключей."


def get_profile_text(username, total_spent, total_months, vpn_status_text, whitelist_status_text=None):
    whitelist_block = f"\n\n{whitelist_status_text}" if whitelist_status_text else ""
    return (
        f"👤 <b>Профиль:</b> {username}\n\n"
        f"💰 <b>Потрачено всего:</b> {total_spent:.0f} RUB\n"
        f"📅 <b>Приобретено месяцев:</b> {total_months}\n\n"
        f"{vpn_status_text}"
        f"{whitelist_block}"
    )


def get_vpn_active_text(days_left, hours_left):
    return f"✅ <b>Статус VPN:</b> Активен\n⏳ <b>Осталось:</b> {days_left} д. {hours_left} ч."


def get_key_info_text(
    key_number,
    expiry_date,
    created_date,
    connection_string: str | None = None,
    subscription_link: str | None = None,
    gift_days: int = 0,
):
    expiry_formatted = expiry_date.strftime("%d.%m.%Y в %H:%M")
    created_formatted = created_date.strftime("%d.%m.%Y в %H:%M")
    gift_line = f"\n<b>🎁 Компенсация:</b> +{gift_days} дн." if gift_days > 0 else ""

    if subscription_link:
        return (
            f"<b>🔑 Информация о ключе #{key_number}</b>\n\n"
            f"<b>➕ Приобретён:</b> {created_formatted}\n"
            f"<b>⏳ Действителен до:</b> {expiry_formatted}{gift_line}\n\n"
            f"<b>Ваша ссылка на подписку:</b>\n"
            f"<code>{subscription_link}</code>\n\n"
            f"Скопируйте ссылку и добавьте её в настройках подписки вашего VPN-клиента."
        )

    return (
        f"<b>🔑 Информация о ключе #{key_number}</b>\n\n"
        f"<b>➕ Приобретён:</b> {created_formatted}\n"
        f"<b>⏳ Действителен до:</b> {expiry_formatted}{gift_line}\n\n"
        f"<code>{connection_string or ''}</code>"
    )


def get_purchase_success_text(
    action: str,
    key_number: int,
    expiry_date,
    connection_string: str | None = None,
    subscription_link: str | None = None,
    host_name: str | None = None,
    key_email: str | None = None,
    whitelist_limit_gb: int | None = 40,
):
    _ = action, key_number
    return _render_subscription_ready_notification(
        template=get_subscription_ready_notification_template(whitelist_limit_gb),
        expiry_date=expiry_date,
        access_value=subscription_link or connection_string,
        host_name=host_name,
        key_email=key_email,
    )


def get_trial_success_text(
    expiry_date,
    connection_string: str | None = None,
    subscription_link: str | None = None,
    host_name: str | None = None,
    key_email: str | None = None,
    whitelist_limit_gb: int | None = None,
):
    return _format_subscription_ready_text(
        intro_text=(
            "Уважаемый пользователь, ваша новая тестовая подписка готова."
        ),
        expiry_date=expiry_date,
        access_value=subscription_link or connection_string,
        host_name=host_name,
        key_email=key_email,
        whitelist_limit_gb=whitelist_limit_gb,
    )


def _format_subscription_ready_text(
    *,
    intro_text: str,
    expiry_date,
    access_value: str | None,
    host_name: str | None,
    key_email: str | None,
    prefix_text: str | None = None,
    bold_subscription_text: bool = True,
    whitelist_limit_gb: int | None = None,
) -> str:
    expiry_formatted = expiry_date.strftime("%d.%m.%Y %H:%M")
    host_label = html_escape(str(host_name or "—"))
    email_label = html_escape(str(key_email or "—"))
    access_label = html_escape(str(access_value or ""))

    body_start = "🔑 <b>" if bold_subscription_text else "🔑 "
    body_end = "</b>" if bold_subscription_text else ""
    whitelist_text = (
        f"WHITELIST — {whitelist_limit_gb} ГБ на пробный срок.\n\n"
        if whitelist_limit_gb else ""
    )
    body_text = (
        f"{body_start}{intro_text}\n\n"
        f"{whitelist_text}"
        "Пожалуйста, для корректной работы VPN обновите подписку, вставив новую ссылку в свое приложение HAPP. "
        "Для этого скопируйте ссылку, перейдите в НАРР нажмите «+» в правом верхнем углу, затем нажмите "
        f"добавить из буфера обмена.{body_end}\n\n"
        "Информация\n"
        f"Хост: <b>{host_label}</b>\n"
        f"Срок до: <b>{expiry_formatted}</b>\n"
        f"Email: <code>{email_label}</code>\n\n"
        f"<pre>{access_label}</pre>"
    )
    if prefix_text:
        return f"{prefix_text}\n\n{body_text}"
    return body_text


def get_whitelist_policy_text(whitelist_limit_gb: int | None) -> str:
    if whitelist_limit_gb is None:
        return ""
    if whitelist_limit_gb == 0:
        return "WHITELIST — безлимитный."
    return (
        f"WHITELIST — {whitelist_limit_gb} ГБ в месяц.\n"
        "Остальные серверы — безлимитные."
    )


def get_subscription_ready_notification_template(
    whitelist_limit_gb: int | None = 40, *, dynamic_quota: bool = False
) -> str:
    whitelist_text = "{whitelist_policy}" if dynamic_quota else get_whitelist_policy_text(whitelist_limit_gb)
    if whitelist_text:
        whitelist_text += "\n\n"
    return (
        "🔑 Уважаемый пользователь, ваша новая подписка готова.\n\n"
        f"{whitelist_text}"
        "Пожалуйста, для корректной работы VPN обновите подписку, вставив новую ссылку в свое приложение HAPP. "
        "Для этого скопируйте ссылку, перейдите в НАРР нажмите «+» в правом верхнем углу, затем нажмите "
        "добавить из буфера обмена.\n\n"
        "Информация\n"
        "Хост: <b>{host}</b>\n"
        "Срок до: <b>{expiry}</b>\n"
        "Email: <code>{email}</code>\n\n"
        "<pre>{key}</pre>"
    )


def _render_subscription_ready_notification(
    *,
    template: str,
    expiry_date,
    access_value: str | None,
    host_name: str | None,
    key_email: str | None,
) -> str:
    values = {
        "{host}": html_escape(str(host_name or "—")),
        "{expiry}": expiry_date.strftime("%d.%m.%Y %H:%M"),
        "{email}": html_escape(str(key_email or "—")),
        "{key}": html_escape(str(access_value or "")),
    }
    rendered = template
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    return rendered
