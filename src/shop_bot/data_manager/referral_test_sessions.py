"""Explicit, short-lived referral rehearsals without deleting payment history.

There is deliberately no bot command or web endpoint for activation. An operator
must call ``activate`` from a trusted maintenance script after authorization.
Normal registration and factual purchase-history checks remain unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing

from shop_bot.data_manager import database


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referral_test_sessions (
            user_id INTEGER PRIMARY KEY,
            expected_referrer_id INTEGER NOT NULL CHECK(expected_referrer_id<>user_id),
            activation_id TEXT NOT NULL UNIQUE,
            previous_referrer_id INTEGER,
            baseline_total_spent REAL NOT NULL,
            baseline_total_months INTEGER NOT NULL,
            baseline_accounted_payments TEXT NOT NULL CHECK(json_valid(baseline_accounted_payments)),
            baseline_confirmed_payments TEXT NOT NULL CHECK(json_valid(baseline_confirmed_payments)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            bound_at TEXT,
            note TEXT NOT NULL DEFAULT ''
        )
    """)


def _confirmed_payment_ids(conn: sqlite3.Connection, user_id: int) -> set[str]:
    return {row[0] for row in conn.execute("""
        SELECT payment_id FROM transactions WHERE user_id=? AND (
            status='paid' OR confirmed_at IS NOT NULL OR CASE WHEN json_valid(metadata)
                THEN COALESCE(json_extract(metadata,'$.payment_confirmed'),0) ELSE 0 END=1
        )
    """, (user_id,))}


def _accounted_payment_ids(conn: sqlite3.Connection, user_id: int) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT payment_id FROM payment_accounting_effects WHERE user_id=?", (user_id,),
    )}


def _session(conn: sqlite3.Connection, user_id: int, *, active: bool = True) -> dict | None:
    cursor = conn.execute("""
        SELECT * FROM referral_test_sessions WHERE user_id=? AND (?=0 OR expires_at>CURRENT_TIMESTAMP)
    """, (user_id, int(active)))
    row = cursor.fetchone()
    return dict(zip((column[0] for column in cursor.description), row, strict=True)) if row else None


def _summary(session: dict, *, is_new: bool) -> dict:
    return {
        "user_id": session["user_id"], "expected_referrer_id": session["expected_referrer_id"],
        "activation_id": session["activation_id"], "previous_referrer_id": session["previous_referrer_id"],
        "created_at": session["created_at"], "expires_at": session["expires_at"],
        "bound_at": session["bound_at"], "is_new": is_new,
        "baseline_accounted_count": len(json.loads(session["baseline_accounted_payments"])),
        "baseline_confirmed_count": len(json.loads(session["baseline_confirmed_payments"])),
    }


def activate(user_id: int, expected_referrer_id: int, *, activation_id: str, note: str = "") -> dict:
    """Arm one authorized seven-day rehearsal; retain all money and audit rows.

    The activation id makes a maintenance-script retry harmless. A different id
    cannot silently reset an existing session and grant another first discount.
    Only the incoming relationship and an unconsumed invite are cleared.
    """
    if (type(user_id) is not int or type(expected_referrer_id) is not int
            or not 0 < user_id <= 9223372036854775807
            or not 0 < expected_referrer_id <= 9223372036854775807 or user_id == expected_referrer_id):
        raise ValueError("Нужны два разных корректных Telegram ID.")
    if not isinstance(activation_id, str) or not activation_id.strip() or len(activation_id) > 200:
        raise ValueError("Нужен уникальный идентификатор тестового сброса длиной до 200 символов.")
    if not isinstance(note, str) or len(note) > 1000:
        raise ValueError("Примечание должно быть не длиннее 1000 символов.")
    activation_id = activation_id.strip()
    with closing(database._create_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _session(conn, user_id, active=False)
        if existing:
            if (existing["activation_id"] != activation_id
                    or existing["expected_referrer_id"] != expected_referrer_id):
                raise ValueError("Для пользователя уже сохранён тестовый сеанс; повторный сброс запрещён.")
            return _summary(existing, is_new=False)
        if conn.execute(
            "SELECT 1 FROM referral_test_sessions WHERE activation_id=?", (activation_id,),
        ).fetchone():
            raise ValueError("Идентификатор тестового сброса уже занят.")
        user = conn.execute(
            "SELECT total_spent,total_months,referred_by FROM users WHERE telegram_id=?", (user_id,),
        ).fetchone()
        if user is None or not conn.execute(
            "SELECT 1 FROM users WHERE telegram_id=?", (expected_referrer_id,),
        ).fetchone():
            raise ValueError("Оба участника теста должны существовать.")
        if conn.execute(
            "SELECT 1 FROM referral_withdrawals WHERE user_id=? AND status='pending'", (user_id,),
        ).fetchone():
            raise ValueError("Сначала завершите открытую заявку пользователя на вывод.")
        if conn.execute("""
            SELECT 1 FROM referral_discount_invoices WHERE user_id=? AND discount_cents>0
            AND status IN ('reserved','issued','uncertain','consumed')
        """, (user_id,)).fetchone():
            raise ValueError("Нельзя сбросить действующий или уже использованный скидочный счёт.")
        if conn.execute("""
            SELECT 1 FROM transactions t WHERE user_id=? AND (
                status='processing' OR issuance_state IN ('confirmed','issuing','retryable_failed')
                OR ((status='paid' OR confirmed_at IS NOT NULL OR CASE WHEN json_valid(metadata)
                    THEN COALESCE(json_extract(metadata,'$.payment_confirmed'),0) ELSE 0 END=1)
                    AND COALESCE(issuance_state,'')<>'issued'
                    AND NOT EXISTS(SELECT 1 FROM payment_accounting_effects p WHERE p.payment_id=t.payment_id))
            )
        """, (user_id,)).fetchone():
            raise ValueError("Сначала завершите обработку подтверждённых платежей пользователя.")
        accounted = json.dumps(sorted(_accounted_payment_ids(conn, user_id)), ensure_ascii=True)
        confirmed = json.dumps(sorted(_confirmed_payment_ids(conn, user_id)), ensure_ascii=True)
        conn.execute("""
            INSERT INTO referral_test_sessions(user_id,expected_referrer_id,activation_id,previous_referrer_id,
                baseline_total_spent,baseline_total_months,baseline_accounted_payments,
                baseline_confirmed_payments,expires_at,note)
            VALUES (?,?,?,?,?,?,?,?,datetime('now','+7 days'),?)
        """, (user_id, expected_referrer_id, activation_id, user[2], user[0] or 0, user[1] or 0,
              accounted, confirmed, note))
        conn.execute("UPDATE users SET referred_by=NULL WHERE telegram_id=?", (user_id,))
        conn.execute("DELETE FROM referral_pending_invites WHERE user_id=?", (user_id,))
        return _summary(_session(conn, user_id), is_new=True)


def _purchases_unchanged(conn: sqlite3.Connection, session: dict) -> bool:
    user_id = session["user_id"]
    stats = conn.execute(
        "SELECT COALESCE(total_spent,0),COALESCE(total_months,0) FROM users WHERE telegram_id=?", (user_id,),
    ).fetchone()
    if stats is None or tuple(stats) != (session["baseline_total_spent"], session["baseline_total_months"]):
        return False
    try:
        accounted = json.loads(session["baseline_accounted_payments"])
        confirmed = json.loads(session["baseline_confirmed_payments"])
        if (not isinstance(accounted, list) or not isinstance(confirmed, list)
                or not all(isinstance(value, str) for value in accounted + confirmed)):
            return False
        # Exact sets also fail closed if old purchase history is removed or
        # altered. In particular, a pre-reset unpaid invoice paid later is new.
        return (_accounted_payment_ids(conn, user_id) == set(accounted)
                and _confirmed_payment_ids(conn, user_id) == set(confirmed))
    except (TypeError, ValueError):
        return False


def pending_referrer(conn: sqlite3.Connection, user_id: int) -> int | None:
    """Return the sole permitted invite while an existing user awaits a link."""
    session = _session(conn, user_id)
    if session is None or session["bound_at"] is not None or not _purchases_unchanged(conn, session):
        return None
    user = conn.execute("SELECT referred_by FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    if user is None or user[0] is not None:
        return None
    return session["expected_referrer_id"]


def bind_existing(conn: sqlite3.Connection, user_id: int, referrer_id: int | None) -> bool:
    """Bind once inside registration's existing BEGIN IMMEDIATE transaction."""
    if not referrer_id or pending_referrer(conn, user_id) != referrer_id:
        return False
    updated = conn.execute(
        "UPDATE users SET referred_by=? WHERE telegram_id=? AND referred_by IS NULL", (referrer_id, user_id),
    )
    if updated.rowcount != 1:
        return False
    conn.execute(
        "UPDATE referral_test_sessions SET bound_at=CURRENT_TIMESTAMP WHERE user_id=? AND bound_at IS NULL",
        (user_id,),
    )
    return True


def discount_eligible(conn: sqlite3.Connection, user_id: int) -> bool:
    """Pricing-only exception; factual purchase history is never reset."""
    session = _session(conn, user_id)
    if session is None or session["bound_at"] is None or not _purchases_unchanged(conn, session):
        return False
    user = conn.execute("SELECT referred_by FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    return user is not None and user[0] == session["expected_referrer_id"]
