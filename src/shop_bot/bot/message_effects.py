import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from shop_bot.data_manager.database import get_setting

logger = logging.getLogger(__name__)

WELCOME_MESSAGE_EFFECT_ID_SETTING = "welcome_message_effect_id"
ONBOARDING_SUCCESS_MESSAGE_EFFECT_ID_SETTING = "onboarding_success_message_effect_id"
TRIAL_SUCCESS_MESSAGE_EFFECT_ID_SETTING = "trial_success_message_effect_id"
PURCHASE_SUCCESS_MESSAGE_EFFECT_ID_SETTING = "purchase_success_message_effect_id"

# Temporary default effect (spark/fire-like) matching the sample the user requested.
# Can be overridden per event in settings UI.
DEFAULT_SCREENSHOT_EFFECT_ID = "5104841245755180586"
DEFAULT_EFFECT_IDS_BY_SETTING = {
    WELCOME_MESSAGE_EFFECT_ID_SETTING: DEFAULT_SCREENSHOT_EFFECT_ID,
    ONBOARDING_SUCCESS_MESSAGE_EFFECT_ID_SETTING: DEFAULT_SCREENSHOT_EFFECT_ID,
    TRIAL_SUCCESS_MESSAGE_EFFECT_ID_SETTING: DEFAULT_SCREENSHOT_EFFECT_ID,
    PURCHASE_SUCCESS_MESSAGE_EFFECT_ID_SETTING: DEFAULT_SCREENSHOT_EFFECT_ID,
}


def get_message_effect_id(setting_key: str | None) -> str | None:
    if not setting_key:
        return None
    raw_value = str(get_setting(setting_key) or "").strip()
    if raw_value:
        return raw_value
    return DEFAULT_EFFECT_IDS_BY_SETTING.get(setting_key)


def _default_message_effect_id(setting_key: str | None) -> str | None:
    if not setting_key:
        return None
    return DEFAULT_EFFECT_IDS_BY_SETTING.get(setting_key)


async def send_effect_message(
    bot: Bot,
    *,
    chat_id: int,
    text: str,
    effect_setting_key: str | None = None,
    **kwargs,
):
    payload = dict(kwargs)
    payload.setdefault("parse_mode", "HTML")
    message_effect_id = get_message_effect_id(effect_setting_key)
    if message_effect_id:
        payload["message_effect_id"] = message_effect_id
    try:
        return await bot.send_message(chat_id=chat_id, text=text, **payload)
    except TelegramBadRequest as exc:
        error_text = str(exc).lower()
        if payload.get("message_effect_id") and ("effect" in error_text or "message_effect_id" in error_text):
            rejected_effect_id = str(payload.pop("message_effect_id"))
            default_effect_id = _default_message_effect_id(effect_setting_key)
            if default_effect_id and default_effect_id != rejected_effect_id:
                logger.warning(
                    "Telegram rejected message_effect_id=%s for setting=%s; retrying default effect.",
                    rejected_effect_id,
                    effect_setting_key,
                )
                payload["message_effect_id"] = default_effect_id
                try:
                    return await bot.send_message(chat_id=chat_id, text=text, **payload)
                except TelegramBadRequest as default_exc:
                    default_error_text = str(default_exc).lower()
                    if "effect" not in default_error_text and "message_effect_id" not in default_error_text:
                        raise
                    logger.warning(
                        "Telegram rejected default message_effect_id=%s; retrying without effect.",
                        default_effect_id,
                    )
                    payload.pop("message_effect_id", None)
            else:
                logger.warning(
                    "Telegram rejected message_effect_id=%s for setting=%s; retrying without effect.",
                    rejected_effect_id,
                    effect_setting_key,
                )
            return await bot.send_message(chat_id=chat_id, text=text, **payload)
        raise
