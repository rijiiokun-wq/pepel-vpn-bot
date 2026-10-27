"""Narrow authorization and payload parsing for referral withdrawal decisions."""

import logging
import re
import sqlite3
from contextlib import closing

from shop_bot.data_manager import database

logger = logging.getLogger(__name__)
REVIEWER_SETTING = "referral_withdrawal_reviewer_ids"
_ID_PATTERN = re.compile(r"[1-9][0-9]{0,18}")
_CALLBACK_PATTERN = re.compile(r"referral_withdraw_(paid|rejected):([1-9][0-9]{0,18})")
_MAX_SQLITE_INTEGER = 9223372036854775807


def parse_callback(data: object) -> tuple[str, int] | None:
    if not isinstance(data, str):
        return None
    match = _CALLBACK_PATTERN.fullmatch(data)
    if not match:
        return None
    request_id = int(match[2])
    if request_id > _MAX_SQLITE_INTEGER:
        return None
    return match[1], request_id


def reviewer_ids() -> set[int]:
    """Only an absent explicit setting permits the legacy single-admin fallback.

    Notification recipients are deliberately not an authorization source. An empty,
    null or malformed explicit ACL denies access, including to the legacy admin.
    """
    try:
        with closing(database._create_connection()) as conn:
            settings = dict(conn.execute(
                "SELECT key,value FROM bot_settings WHERE key IN (?,?)",
                (REVIEWER_SETTING, "admin_telegram_id"),
            ))
    except sqlite3.Error:
        logger.warning("Withdrawal reviewer authorization could not be read; access denied.")
        return set()
    value = settings[REVIEWER_SETTING] if REVIEWER_SETTING in settings else settings.get("admin_telegram_id")
    if not isinstance(value, str) or not value.strip():
        return set()
    if REVIEWER_SETTING not in settings and not _ID_PATTERN.fullmatch(value.strip()):
        return set()
    parts = [part.strip() for part in value.split(",")]
    if any(not _ID_PATTERN.fullmatch(part) for part in parts):
        return set()
    values = {int(part) for part in parts}
    return values if all(value <= _MAX_SQLITE_INTEGER for value in values) else set()


def is_reviewer(user_id: int) -> bool:
    return isinstance(user_id, int) and not isinstance(user_id, bool) and user_id in reviewer_ids()
