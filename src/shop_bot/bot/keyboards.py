import logging
from datetime import datetime
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.config import BOT_SOURCE
from shop_bot.data_manager.database import get_host, get_setting
from shop_bot.super_promotion import (
    is_promotional_plan,
    is_super_offer_available, promotion_button_text,
)

logger = logging.getLogger(__name__)


BUTTON_STYLE_PRIMARY = "primary"
BUTTON_STYLE_SUCCESS = "success"
BUTTON_STYLE_DANGER = "danger"


def _button_extra(*, style: str | None = None, icon_custom_emoji_id: str | None = None) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if style:
        extra["style"] = style
    if icon_custom_emoji_id:
        extra["icon_custom_emoji_id"] = icon_custom_emoji_id
    return extra


def is_support_contact_visible(now: datetime | None = None) -> bool:
    """Support contact must stay visible even outside helpdesk working hours."""
    _ = now
    return True


main_reply_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏠 Главное меню", **_button_extra(style=BUTTON_STYLE_PRIMARY))]],
    resize_keyboard=True
)

def create_main_menu_keyboard(
    user_keys: list,
    trial_standard_available: bool,
    trial_super_available: bool,
    is_admin: bool,
    *,
    show_standard_purchase: bool = True,
    show_super_purchase: bool | None = None,
    show_referral_program: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    show_super_key = is_super_key_enabled() if show_super_purchase is None else bool(show_super_purchase)
    # Trial buttons stay above purchase buttons, but disappear once the matching trial is used.
    show_trial_std = trial_standard_available
    show_trial_super = trial_super_available

    if show_trial_std:
        builder.button(
            text="🎁 Стандартный: Wi-Fi + WHITELIST 5 ГБ — 2 дня",
            callback_data="get_trial_standard",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    if show_trial_super:
        builder.button(
            text="🎁 Суперключ (обход белых списков) — 2 дня бесплатно",
            callback_data="get_trial_super",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )

    if show_standard_purchase:
        builder.button(
            text="➕ Стандартный ключ (5Гб/мес белые списки)",
            callback_data="buy_new_key_main",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    if show_super_key:
        builder.button(
            text="🔑 Суперключ (40ГБ/мес белые списки)",
            callback_data="buy_super_key_main",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )

    builder.button(text="👤 Мой профиль", callback_data="show_profile")
    builder.button(text=f"🔑 Мои ключи ({len(user_keys)})", callback_data="manage_keys")
    builder.button(
        text="❓ Как использовать",
        callback_data="howto_vless",
        **_button_extra(style=BUTTON_STYLE_PRIMARY),
    )
    builder.button(
        text="🆘 Поддержка",
        callback_data="show_help",
        **_button_extra(style=BUTTON_STYLE_DANGER),
    )
    if show_referral_program:
        builder.button(text="🤝 Реферальная программа", callback_data="show_referral_program")
    if is_admin:
        builder.button(
            text="📢 Рассылка",
            callback_data="start_broadcast",
            **_button_extra(style=BUTTON_STYLE_PRIMARY),
        )

    layout = [
        1 if show_trial_std else 0,
        1 if show_trial_super else 0,
        1 if show_standard_purchase else 0,
        1 if show_super_key else 0,
        2,
        2,
        1 if show_referral_program else 0,
        1 if is_admin else 0,
    ]
    actual_layout = [size for size in layout if size > 0]
    builder.adjust(*actual_layout)

    return builder.as_markup()


def is_super_key_enabled() -> bool:
    super_key_host_name = (get_setting("super_key_host_name") or "").strip()
    if not super_key_host_name:
        return False
    return get_host(super_key_host_name) is not None

def create_broadcast_options_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➕ Добавить кнопку",
        callback_data="broadcast_add_button",
        **_button_extra(style=BUTTON_STYLE_PRIMARY),
    )
    builder.button(
        text="➡️ Пропустить",
        callback_data="broadcast_skip_button",
        **_button_extra(style=BUTTON_STYLE_PRIMARY),
    )
    builder.button(
        text="❌ Отмена",
        callback_data="cancel_broadcast",
        **_button_extra(style=BUTTON_STYLE_DANGER),
    )
    builder.adjust(2, 1)
    return builder.as_markup()

def create_broadcast_confirmation_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Отправить всем",
        callback_data="confirm_broadcast",
        **_button_extra(style=BUTTON_STYLE_SUCCESS),
    )
    builder.button(
        text="❌ Отмена",
        callback_data="cancel_broadcast",
        **_button_extra(style=BUTTON_STYLE_DANGER),
    )
    builder.adjust(2)
    return builder.as_markup()

def create_broadcast_cancel_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="❌ Отмена",
        callback_data="cancel_broadcast",
        **_button_extra(style=BUTTON_STYLE_DANGER),
    )
    return builder.as_markup()

def create_about_keyboard(channel_url: str | None, terms_url: str | None, privacy_url: str | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if channel_url:
        builder.button(text="📰 Наш канал", url=channel_url)
    if terms_url:
        builder.button(text="📄 Условия использования", url=terms_url)
    if privacy_url:
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def create_support_keyboard(
    support_user: str,
    *,
    contact_visible: bool | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    _ = contact_visible
    if support_user:
        builder.button(
            text="🆘 Написать в поддержку",
            url=support_user,
            **_button_extra(style=BUTTON_STYLE_DANGER),
        )
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def create_promo_campaign_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🎁🔥 Акция Супер 2+1 — 1100 ₽", callback_data="promo:super",
                   **_button_extra(style=BUTTON_STYLE_PRIMARY))
    builder.button(text="🎁🔥 Акция Стандарт 6+1 — 500 ₽", callback_data="promo:standard",
                   **_button_extra(style=BUTTON_STYLE_PRIMARY))
    builder.adjust(1)
    return builder.as_markup()


def create_promo_action_keyboard(tier: str) -> InlineKeyboardMarkup:
    if tier not in {"standard", "super"}:
        raise ValueError("Invalid promotion tier")
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Продлить ключ", callback_data=f"promo:{tier}:keys:0",
                   **_button_extra(style=BUTTON_STYLE_SUCCESS))
    builder.button(text=("🔑" if tier == "super" else "➕") + " Купить новый ключ",
                   callback_data=f"promo:{tier}:new", **_button_extra(style=BUTTON_STYLE_SUCCESS))
    builder.button(text="⬅️ К акциям", callback_data="promo:offers")
    builder.adjust(1)
    return builder.as_markup()


def create_promo_keys_keyboard(keys: list[dict], tier: str, page: int = 0) -> InlineKeyboardMarkup:
    if tier not in {"standard", "super"}:
        raise ValueError("Invalid promotion tier")
    page_size = 8
    last_page = max(0, (len(keys) - 1) // page_size)
    page = min(max(0, page), last_page)
    builder = InlineKeyboardBuilder()
    for key in keys[page * page_size:(page + 1) * page_size]:
        try:
            expiry = datetime.fromisoformat(str(key['expiry_date'])).strftime('%d.%m.%Y')
        except (KeyError, TypeError, ValueError):
            expiry = "дата не указана"
        number = key.get('promo_display_number', key['key_id'])
        builder.button(text=f"🔑 Ключ №{number} · до {expiry}",
                       callback_data=f"promo:{tier}:renew:{key['key_id']}")
    if page > 0:
        builder.button(text="⬅️ Предыдущие", callback_data=f"promo:{tier}:keys:{page - 1}")
    if page < last_page:
        builder.button(text="Следующие ➡️", callback_data=f"promo:{tier}:keys:{page + 1}")
    builder.button(text=("🔑" if tier == "super" else "➕") + " Купить новый ключ",
                   callback_data=f"promo:{tier}:new", **_button_extra(style=BUTTON_STYLE_SUCCESS))
    builder.button(text="⬅️ Назад", callback_data=f"promo:{tier}")
    builder.adjust(1)
    return builder.as_markup()


def create_plans_keyboard(
    plans: list[dict],
    action: str,
    host_name: str,
    key_id: int = 0,
    back_callback: str = "manage_keys",
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for plan in sorted(plans, key=lambda offer: not is_promotional_plan(offer)):
        callback_data = f"buy_{host_name}_{plan['plan_id']}_{action}_{key_id}"
        builder.button(
            text=promotion_button_text(plan),
            callback_data=callback_data,
            **_button_extra(style=BUTTON_STYLE_PRIMARY if is_promotional_plan(plan) else BUTTON_STYLE_SUCCESS),
        )
    builder.button(text="⬅️ Назад", callback_data=back_callback)
    builder.adjust(1)
    return builder.as_markup()

def create_superkey_plans_keyboard(
    super_plans: list[dict],
    back_callback: str = "manage_keys",
) -> InlineKeyboardMarkup:
    """
    Superkey plans are static business offers and do not depend on DB plan IDs.
    callback_data uses short code to keep payload compact and reliable.
    """
    builder = InlineKeyboardBuilder()
    for plan in sorted(super_plans, key=lambda offer: not is_promotional_plan(offer)):
        if not is_super_offer_available(plan):
            continue
        builder.button(
            text=plan["button_text"],
            callback_data=f"superbuy_{plan['code']}",
            **(_button_extra(style=BUTTON_STYLE_PRIMARY) if is_promotional_plan(plan) else {}),
        )
    builder.button(text="⬅️ НАЗАД", callback_data=back_callback)
    builder.adjust(1)
    return builder.as_markup()

def create_skip_email_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➡️ Продолжить без почты",
        callback_data="skip_email",
        **_button_extra(style=BUTTON_STYLE_PRIMARY),
    )
    builder.button(text="⬅️ Назад к тарифам", callback_data="back_to_plans")
    builder.adjust(1)
    return builder.as_markup()

def create_payment_method_keyboard(payment_methods: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    main_payments_enabled = str(get_setting("yookassa_payments_enabled") or "").strip().lower() == "true"
    if str(BOT_SOURCE).strip().lower() == "main" and not main_payments_enabled:
        builder.button(text="⬅️ Назад", callback_data="back_to_email_prompt")
        builder.adjust(1)
        return builder.as_markup()

    if payment_methods and payment_methods.get("lava"):
        builder.button(
            text="🏦 Оплатить через СБП",
            callback_data="pay_lava_sbp",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
        builder.button(
            text="💳 Оплатить картой",
            callback_data="pay_lava_card",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    elif payment_methods and payment_methods.get("paypear"):
        builder.button(
            text="🏦 Оплатить через СБП",
            callback_data="pay_paypear_sbp",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
        builder.button(
            text="💳 Оплатить картой",
            callback_data="pay_paypear_card",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    elif payment_methods and payment_methods.get("yookassa"):
        if get_setting("sbp_enabled"):
            builder.button(
                text="🏦 СБП / Банковская карта",
                callback_data="pay_yookassa",
                **_button_extra(style=BUTTON_STYLE_SUCCESS),
            )
        else:
            builder.button(
                text="🏦 Банковская карта",
                callback_data="pay_yookassa",
                **_button_extra(style=BUTTON_STYLE_SUCCESS),
            )
    if payment_methods and payment_methods.get("heleket"):
        builder.button(
            text="💎 Криптовалюта",
            callback_data="pay_heleket",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    if payment_methods and payment_methods.get("cryptobot"):
        builder.button(
            text="🤖 CryptoBot",
            callback_data="pay_cryptobot",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    if payment_methods and payment_methods.get("tonconnect"):
        callback_data_ton = "pay_tonconnect"
        logger.info(f"Creating TON button with callback_data: '{callback_data_ton}'")
        builder.button(
            text="🪙 TON Connect",
            callback_data=callback_data_ton,
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )

    builder.button(text="⬅️ Назад", callback_data="back_to_email_prompt")
    builder.adjust(1)
    return builder.as_markup()

def create_ton_connect_keyboard(connect_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🚀 Открыть кошелек",
        url=connect_url,
        **_button_extra(style=BUTTON_STYLE_PRIMARY),
    )
    return builder.as_markup()

def create_payment_keyboard(payment_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="Перейти к оплате",
        url=payment_url,
        **_button_extra(style=BUTTON_STYLE_SUCCESS),
    )
    return builder.as_markup()

def create_keys_management_keyboard(
    keys: list,
    show_super_key: bool | None = None,
    *,
    show_standard_purchase: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if show_super_key is None:
        show_super_key = is_super_key_enabled()
    if keys:
        for i, key in enumerate(keys):
            expiry_date = datetime.fromisoformat(key['expiry_date'])
            is_archived = bool(key.get('is_archived'))
            if is_archived:
                status_icon = "❌"
            elif expiry_date > datetime.now():
                status_icon = "✅"
            else:
                status_icon = "❌"
            host_name = key.get('host_name', 'Неизвестный хост')
            gift = int(key.get('gift_days') or 0)
            gift_label = f" +{gift}д" if gift > 0 else ""
            button_text = f"{status_icon} Ключ #{i+1} ({host_name}) (до {expiry_date.strftime('%d.%m.%Y')}){gift_label}"
            # Archived keys stay visible in the list, but should not open details.
            callback_data = "archived_key_noop" if is_archived else f"show_key_{key['key_id']}"
            builder.button(text=button_text, callback_data=callback_data)
    if show_standard_purchase:
        builder.button(
            text="➕ Стандартный ключ (5Гб/мес белые списки)",
            callback_data="buy_new_key_keys",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    if show_super_key:
        builder.button(
            text="🔑 Суперключ (40ГБ/мес белые списки)",
            callback_data="buy_super_key_keys",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def create_key_info_keyboard(
    key_id: int,
    *,
    show_blitz_migration: bool = False,
    show_refresh: bool = False,
    show_qr: bool = True,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if show_blitz_migration:
        builder.button(
            text="⚡ Перейти на Hysteria 2",
            callback_data=f"migrate_blitz_{key_id}",
            **_button_extra(style=BUTTON_STYLE_PRIMARY),
        )
    if show_refresh:
        builder.button(
            text="🔄 Продлить ключ",
            callback_data=f"extend_key_{key_id}",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    if show_qr:
        builder.button(
            text="📱 Показать QR-код",
            callback_data=f"show_qr_{key_id}",
            **_button_extra(style=BUTTON_STYLE_PRIMARY),
        )
    builder.button(text="⬅️ Назад к списку ключей", callback_data="manage_keys")
    builder.adjust(1)
    return builder.as_markup()

def create_support_error_keyboard() -> InlineKeyboardMarkup:
    """Keyboard shown when a Blitz/key error occurs — directs user to support."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🆘 Написать в поддержку",
        callback_data="show_help",
        **_button_extra(style=BUTTON_STYLE_DANGER),
    )
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_back_to_keys_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад к списку ключей", callback_data="manage_keys")
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_trial_retry_keyboard(retry_callback: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔁 Попробовать ещё раз",
        callback_data=retry_callback,
        **_button_extra(style=BUTTON_STYLE_SUCCESS),
    )
    builder.button(
        text="🆘 Поддержка",
        callback_data="show_help",
        **_button_extra(style=BUTTON_STYLE_DANGER),
    )
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def create_howto_vless_keyboard(
    happ_android_url: str,
    happ_ios_url: str,
    happ_windows_url: str,
    happ_macos_url: str,
    karing_android_url: str,
    karing_ios_url: str,
    karing_windows_url: str,
    karing_macos_url: str,
    hiddify_android_url: str,
    hiddify_ios_url: str,
    hiddify_windows_url: str,
    hiddify_macos_url: str,
    v2raytun_android_url: str,
    v2raytun_ios_url: str,
    v2raytun_windows_url: str,
    incy_android_url: str,
    incy_ios_url: str,
    incy_windows_url: str,
    incy_macos_url: str,
) -> InlineKeyboardMarkup:
    """
    "How to use" points to download links for supported apps.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="Скачать Happ для iOS", url=happ_ios_url)
    builder.button(text="Скачать Happ для Android", url=happ_android_url)
    builder.button(text="Скачать Happ для Windows", url=happ_windows_url)
    builder.button(text="Скачать Happ для macOS", url=happ_macos_url)
    builder.button(text="Скачать Karing для iOS", url=karing_ios_url)
    builder.button(text="Скачать Karing для Android", url=karing_android_url)
    builder.button(text="Скачать Karing для Windows", url=karing_windows_url)
    builder.button(text="Скачать Karing для macOS", url=karing_macos_url)
    builder.button(text="Скачать Hiddify для iOS", url=hiddify_ios_url)
    builder.button(text="Скачать Hiddify для Android", url=hiddify_android_url)
    builder.button(text="Скачать Hiddify для Windows", url=hiddify_windows_url)
    builder.button(text="Скачать Hiddify для macOS", url=hiddify_macos_url)
    builder.button(text="Скачать v2RayTun для iOS", url=v2raytun_ios_url)
    builder.button(text="Скачать v2RayTun для Android", url=v2raytun_android_url)
    builder.button(text="Скачать v2RayTun для Windows", url=v2raytun_windows_url)
    builder.button(text="Скачать INCY для iOS", url=incy_ios_url)
    builder.button(text="Скачать INCY для Android", url=incy_android_url)
    builder.button(text="Скачать INCY для Windows", url=incy_windows_url)
    builder.button(text="Скачать INCY для macOS", url=incy_macos_url)
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()

def create_howto_vless_keyboard_key(
    happ_android_url: str,
    happ_ios_url: str,
    happ_windows_url: str,
    happ_macos_url: str,
    karing_android_url: str,
    karing_ios_url: str,
    karing_windows_url: str,
    karing_macos_url: str,
    hiddify_android_url: str,
    hiddify_ios_url: str,
    hiddify_windows_url: str,
    hiddify_macos_url: str,
    v2raytun_android_url: str,
    v2raytun_ios_url: str,
    v2raytun_windows_url: str,
    incy_android_url: str,
    incy_ios_url: str,
    incy_windows_url: str,
    incy_macos_url: str,
    key_id: int,
) -> InlineKeyboardMarkup:
    """
    Key-specific "Инструкция" points to download links for supported apps.
    """
    builder = InlineKeyboardBuilder()
    builder.button(text="Скачать Happ для iOS", url=happ_ios_url)
    builder.button(text="Скачать Happ для Android", url=happ_android_url)
    builder.button(text="Скачать Happ для Windows", url=happ_windows_url)
    builder.button(text="Скачать Happ для macOS", url=happ_macos_url)
    builder.button(text="Скачать Karing для iOS", url=karing_ios_url)
    builder.button(text="Скачать Karing для Android", url=karing_android_url)
    builder.button(text="Скачать Karing для Windows", url=karing_windows_url)
    builder.button(text="Скачать Karing для macOS", url=karing_macos_url)
    builder.button(text="Скачать Hiddify для iOS", url=hiddify_ios_url)
    builder.button(text="Скачать Hiddify для Android", url=hiddify_android_url)
    builder.button(text="Скачать Hiddify для Windows", url=hiddify_windows_url)
    builder.button(text="Скачать Hiddify для macOS", url=hiddify_macos_url)
    builder.button(text="Скачать v2RayTun для iOS", url=v2raytun_ios_url)
    builder.button(text="Скачать v2RayTun для Android", url=v2raytun_android_url)
    builder.button(text="Скачать v2RayTun для Windows", url=v2raytun_windows_url)
    builder.button(text="Скачать INCY для iOS", url=incy_ios_url)
    builder.button(text="Скачать INCY для Android", url=incy_android_url)
    builder.button(text="Скачать INCY для Windows", url=incy_windows_url)
    builder.button(text="Скачать INCY для macOS", url=incy_macos_url)
    builder.button(text="⬅️ Назад к ключу", callback_data=f"show_key_{key_id}")
    builder.adjust(1)
    return builder.as_markup()

def create_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    return builder.as_markup()

def create_welcome_keyboard(channel_url: str | None, is_subscription_forced: bool = False, terms_url: str | None = None, privacy_url: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    if channel_url and terms_url and privacy_url and is_subscription_forced:
        builder.button(text="📢 Перейти в канал", url=channel_url)
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(
            text="✅ Я подписался",
            callback_data="check_subscription_and_agree",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    elif channel_url and terms_url and privacy_url:
        builder.button(text="📢 Наш канал (не обязательно)", url=channel_url)
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(
            text="✅ Принимаю условия",
            callback_data="check_subscription_and_agree",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    elif terms_url and privacy_url:
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(
            text="✅ Принимаю условия",
            callback_data="check_subscription_and_agree",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    elif terms_url:
        builder.button(text="📄 Условия использования", url=terms_url)
        builder.button(
            text="✅ Принимаю условия",
            callback_data="check_subscription_and_agree",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    elif privacy_url:
        builder.button(text="🔒 Политика конфиденциальности", url=privacy_url)
        builder.button(
            text="✅ Принимаю условия",
            callback_data="check_subscription_and_agree",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    else:
        builder.button(text="📢 Наш канал (не обязательно)", url=channel_url)
        builder.button(
            text="✅ Я подписался",
            callback_data="check_subscription_and_agree",
            **_button_extra(style=BUTTON_STYLE_SUCCESS),
        )
    builder.adjust(1)
    return builder.as_markup()

def get_main_menu_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="🏠 В главное меню",
        callback_data="show_main_menu",
        **_button_extra(style=BUTTON_STYLE_PRIMARY),
    )

def get_buy_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text="💳 Купить подписку",
        callback_data="buy_vpn",
        **_button_extra(style=BUTTON_STYLE_SUCCESS),
    )
