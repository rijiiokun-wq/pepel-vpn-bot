from __future__ import annotations

import re
import sqlite3

from shop_bot.data_manager import database

USERNAME_PATTERN = r"^[A-Za-z0-9_]{1,64}$"


class TrialResetError(RuntimeError):
    def __init__(self, code: str, *, details: str | None = None) -> None:
        super().__init__(code if details is None else f"{code}:{details}")
        self.code = code
        self.details = details


def normalize_username(raw_username: str) -> str:
    username = str(raw_username or "").strip().lstrip("@")
    if not re.fullmatch(USERNAME_PATTERN, username):
        raise TrialResetError("invalid_username")
    return username


def _find_user_by_username(username: str) -> list[dict]:
    normalized = username.lower()
    candidates = (normalized, f"@{normalized}")
    with database._create_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                telegram_id,
                username,
                COALESCE(trial_used, 0) AS trial_used,
                COALESCE(trial_super_used, 0) AS trial_super_used
            FROM users
            WHERE LOWER(TRIM(COALESCE(username, ''))) IN (?, ?)
            ORDER BY telegram_id
            """,
            candidates,
        )
        return [dict(row) for row in cursor.fetchall()]


def reset_user_trials_by_username(raw_username: str) -> dict:
    username = normalize_username(raw_username)
    matches = _find_user_by_username(username)
    if not matches:
        raise TrialResetError("user_not_found")
    if len(matches) > 1:
        ids = ",".join(str(user["telegram_id"]) for user in matches)
        raise TrialResetError("ambiguous_username", details=ids)

    user = matches[0]
    user_id = int(user["telegram_id"])
    before_trial = int(user["trial_used"] or 0)
    before_trial_super = int(user["trial_super_used"] or 0)

    with database._create_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET trial_used = 0, trial_super_used = 0 WHERE telegram_id = ?",
            (user_id,),
        )
        conn.commit()
        if cursor.rowcount != 1:
            raise TrialResetError("db_update_failed")

    return {
        "telegram_id": user_id,
        "username": user["username"],
        "trial_used_before": before_trial,
        "trial_super_used_before": before_trial_super,
        "trial_used_after": 0,
        "trial_super_used_after": 0,
        "changed": bool(before_trial or before_trial_super),
    }
