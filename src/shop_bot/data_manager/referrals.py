"""Referral policy, integer-cent balances and transactional payout requests.

All methods are synchronous: async bot callers must use a worker thread. No
method sends money or Telegram messages. Legacy balance fields are mirrors;
the accounts and append-only ledger below are authoritative after migration.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from uuid import uuid4

from shop_bot.data_manager import database, referral_test_sessions

logger = logging.getLogger(__name__)
CENT = Decimal("0.01")
MAX_MONEY = Decimal("1000000000000")
SETTING_DEFAULTS = {
    "enable_referrals": "false",
    "referral_percentage": "10",
    "referral_discount": "5",
    "minimum_withdrawal": "100",
}


def _decimal(value, label: str) -> Decimal:
    try:
        number = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label}: укажите число.") from exc
    if not number.is_finite():
        raise ValueError(f"{label}: укажите конечное число.")
    return number


def money_cents(value) -> int:
    amount = _decimal(value, "Сумма")
    if abs(amount) > MAX_MONEY:
        raise ValueError("Сумма слишком велика.")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def validate_settings(mapping: dict) -> dict[str, str]:
    """Validate the four fields together; missing fields use safe defaults."""
    values = {**SETTING_DEFAULTS, **mapping}
    enabled = str(values["enable_referrals"]).strip().lower()
    if enabled not in {"true", "false"}:
        raise ValueError("Включение реферальной программы: допустимо true или false.")
    percentage = _decimal(values["referral_percentage"], "Вознаграждение")
    discount = _decimal(values["referral_discount"], "Скидка")
    minimum = _decimal(values["minimum_withdrawal"], "Минимум вывода")
    if not 0 <= percentage <= 100:
        raise ValueError("Вознаграждение должно быть от 0 до 100%.")
    if not 0 <= discount <= Decimal("99.99"):
        raise ValueError("Скидка должна быть от 0 до 99,99%: первая покупка остаётся платной.")
    if minimum <= 0 or minimum > MAX_MONEY or minimum != minimum.quantize(CENT):
        raise ValueError("Минимум вывода должен быть положительной суммой с точностью до копеек.")
    return {
        "enable_referrals": enabled,
        "referral_percentage": format(percentage.normalize(), "f"),
        "referral_discount": format(discount.normalize(), "f"),
        "minimum_withdrawal": format(minimum.normalize(), "f"),
    }


def _policy(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT key, value FROM bot_settings WHERE key IN (?, ?, ?, ?)", tuple(SETTING_DEFAULTS)
    ).fetchall()
    try:
        values = validate_settings(dict(rows))
    except ValueError:
        logger.warning("Referral policy is invalid; referral actions are disabled until settings are corrected.")
        values = {**SETTING_DEFAULTS, "referral_percentage": "0", "referral_discount": "0"}
    return {
        "enabled": values["enable_referrals"] == "true",
        "percentage": Decimal(values["referral_percentage"]),
        "discount": Decimal(values["referral_discount"]),
        "minimum": Decimal(values["minimum_withdrawal"]),
    }


def get_policy() -> dict:
    with closing(database._create_connection()) as conn:
        return _policy(conn)


def save_settings(mapping: dict) -> dict[str, str]:
    with closing(database._create_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current = dict(conn.execute(
            "SELECT key, value FROM bot_settings WHERE key IN (?, ?, ?, ?)", tuple(SETTING_DEFAULTS)
        ).fetchall())
        values = validate_settings({**current, **mapping})
        conn.executemany("INSERT OR REPLACE INTO bot_settings(key,value) VALUES (?,?)", values.items())
        if values["enable_referrals"] == "false":
            conn.execute("DELETE FROM referral_pending_invites")
    database.invalidate_settings_cache()
    return values


def initialize_schema(conn: sqlite3.Connection) -> None:
    """Idempotent migration; preserve legacy available and lifetime separately."""
    referral_test_sessions.initialize_schema(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referral_discount_invoices (
            id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            base_cents INTEGER NOT NULL CHECK(base_cents > 0),
            price_cents INTEGER NOT NULL CHECK(price_cents > 0),
            discount_cents INTEGER NOT NULL CHECK(discount_cents >= 0),
            discount_percent TEXT NOT NULL,
            referrer_id INTEGER,
            status TEXT NOT NULL CHECK(status IN ('full_price','reserved','issued','uncertain','consumed','cancelled')),
            payment_id TEXT,
            consumed_payment_id TEXT,
            reason TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # A consumed row continues to occupy the entitlement for the user's life.
    # Unknown provider outcomes occupy it too: time alone never releases it.
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_discount_entitlement
        ON referral_discount_invoices(user_id)
        WHERE discount_cents>0 AND status IN ('reserved','issued','uncertain','consumed')
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_discount_payment
        ON referral_discount_invoices(payment_id) WHERE payment_id IS NOT NULL
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referral_withdrawal_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            withdrawal_id INTEGER NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('paid','rejected')),
            source TEXT NOT NULL CHECK(source IN ('main','reserve')),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sending','sent','failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            total_attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            lease_token TEXT,
            lease_until TEXT,
            error TEXT NOT NULL DEFAULT '',
            message_id TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_referral_notification_queue
        ON referral_withdrawal_notifications(source,status,next_retry_at)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referral_pending_invites (
            user_id INTEGER PRIMARY KEY,
            referrer_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referral_accounts (
            user_id INTEGER PRIMARY KEY,
            balance_cents INTEGER NOT NULL CHECK(balance_cents >= 0),
            reserved_cents INTEGER NOT NULL DEFAULT 0 CHECK(reserved_cents >= 0),
            lifetime_cents INTEGER NOT NULL CHECK(lifetime_cents >= 0),
            legacy_opening_cents INTEGER NOT NULL,
            legacy_lifetime_cents INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referral_withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','rejected')),
            details TEXT NOT NULL,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            admin_id TEXT,
            note TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_pending_user
        ON referral_withdrawals(user_id) WHERE status='pending'
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS referral_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            reserved_delta_cents INTEGER NOT NULL DEFAULT 0,
            lifetime_delta_cents INTEGER NOT NULL DEFAULT 0,
            payment_id TEXT,
            withdrawal_id INTEGER,
            related_user_id INTEGER,
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_reward_payment
        ON referral_ledger(payment_id) WHERE kind='reward'
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_referral_ledger_user ON referral_ledger(user_id,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_referral_users_referrer ON users(referred_by)")
    for (user_id,) in conn.execute("""
        SELECT telegram_id FROM users WHERE telegram_id NOT IN (SELECT user_id FROM referral_accounts)
    """).fetchall():
        _ensure_account(conn, user_id)


def _ensure_account(conn: sqlite3.Connection, user_id: int) -> None:
    if conn.execute("SELECT 1 FROM referral_accounts WHERE user_id=?", (user_id,)).fetchone():
        return
    row = conn.execute(
        "SELECT referral_balance, referral_balance_all FROM users WHERE telegram_id=?", (user_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Пользователь не найден.")
    balance, lifetime = (money_cents(value or 0) for value in row)
    if balance < 0 or lifetime < 0:
        raise ValueError("Старый реферальный баланс отрицательный: требуется сверка до миграции.")
    conn.execute("""
        INSERT INTO referral_accounts(user_id,balance_cents,lifetime_cents,legacy_opening_cents,legacy_lifetime_cents)
        VALUES (?,?,?,?,?)
    """, (user_id, balance, lifetime, balance, lifetime))
    conn.execute("""
        INSERT INTO referral_ledger(user_id,kind,amount_cents,lifetime_delta_cents,note)
        VALUES (?,'opening',?,?,?)
    """, (user_id, balance, lifetime, "Начальный остаток из старой базы; история до миграции может быть неполной."))
    _sync_legacy(conn, user_id)


def _sync_legacy(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("""
        UPDATE users SET
          referral_balance=(SELECT balance_cents/100.0 FROM referral_accounts WHERE user_id=?),
          referral_balance_all=(SELECT lifetime_cents/100.0 FROM referral_accounts WHERE user_id=?)
        WHERE telegram_id=?
    """, (user_id, user_id, user_id))


def valid_referrer(conn: sqlite3.Connection, user_id: int, candidate) -> int | None:
    if not _policy(conn)["enabled"]:
        return None
    raw_candidate = str(candidate or "").strip()
    if not raw_candidate.isdecimal() or len(raw_candidate) > 19:
        return None
    referrer_id = int(raw_candidate)
    if referrer_id == int(user_id) or not 0 < referrer_id <= 9223372036854775807:
        return None
    return referrer_id if conn.execute(
        "SELECT 1 FROM users WHERE telegram_id=?", (referrer_id,)
    ).fetchone() else None


def capture_invite(user_id: int, candidate) -> int | None:
    """Preserve the first valid invite across gates without creating a user."""
    with closing(database._create_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _policy(conn)["enabled"]:
            conn.execute("DELETE FROM referral_pending_invites WHERE user_id=?", (user_id,))
            return None
        existing_user = conn.execute("SELECT 1 FROM users WHERE telegram_id=?", (user_id,)).fetchone()
        expected_test_referrer = referral_test_sessions.pending_referrer(conn, user_id) if existing_user else None
        if existing_user and expected_test_referrer is None:
            conn.execute("DELETE FROM referral_pending_invites WHERE user_id=?", (user_id,))
            return None
        # SQLite timestamps are UTC. Expired invitations cannot extend their
        # own TTL when someone follows another referral link later.
        conn.execute("DELETE FROM referral_pending_invites WHERE created_at<=datetime('now','-7 days')")
        referrer_id = valid_referrer(conn, user_id, candidate)
        if referrer_id is None or (existing_user and referrer_id != expected_test_referrer):
            return None
        conn.execute("""
            INSERT OR IGNORE INTO referral_pending_invites(user_id,referrer_id) VALUES (?,?)
        """, (user_id, referrer_id))
        return conn.execute(
            "SELECT referrer_id FROM referral_pending_invites WHERE user_id=?", (user_id,),
        ).fetchone()[0]


def consume_invite(conn: sqlite3.Connection, user_id: int, candidate) -> int | None:
    """Choose an invitation inside the caller's registration transaction."""
    row = conn.execute("""
        SELECT referrer_id FROM referral_pending_invites
        WHERE user_id=? AND created_at>datetime('now','-7 days')
    """, (user_id,)).fetchone()
    saved_referrer = valid_referrer(conn, user_id, row[0]) if row else None
    referrer_id = saved_referrer or valid_referrer(conn, user_id, candidate)
    conn.execute("DELETE FROM referral_pending_invites WHERE user_id=?", (user_id,))
    return referrer_id


def payment_reward(conn: sqlite3.Connection, user_id: int, amount_rub) -> tuple[int | None, int]:
    """Read persisted relationship and policy under the payment write lock."""
    policy = _policy(conn)
    row = conn.execute("SELECT referred_by FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    if not policy["enabled"] or row is None:
        return None, 0
    referrer_id = valid_referrer(conn, user_id, row[0])
    if not referrer_id:
        return None, 0
    reward = money_cents(_decimal(amount_rub, "Платёж") * policy["percentage"] / 100)
    return referrer_id, max(0, reward)


def credit_reward(conn: sqlite3.Connection, referrer_id: int, user_id: int, cents: int, payment_id: str) -> None:
    _ensure_account(conn, referrer_id)
    conn.execute("""
        INSERT INTO referral_ledger(user_id,kind,amount_cents,lifetime_delta_cents,payment_id,related_user_id)
        VALUES (?,'reward',?,?,?,?)
    """, (referrer_id, cents, cents, payment_id, user_id))
    conn.execute("""
        UPDATE referral_accounts SET balance_cents=balance_cents+?, lifetime_cents=lifetime_cents+?
        WHERE user_id=?
    """, (cents, cents, referrer_id))
    _sync_legacy(conn, referrer_id)


def _has_purchases(conn: sqlite3.Connection, user_id: int) -> bool:
    return conn.execute("""
            SELECT 1 FROM users WHERE telegram_id=? AND (
                total_spent>0 OR total_months>0
                OR EXISTS(SELECT 1 FROM payment_accounting_effects WHERE user_id=users.telegram_id)
                OR EXISTS(SELECT 1 FROM transactions WHERE user_id=users.telegram_id
                          AND (status='paid' OR confirmed_at IS NOT NULL
                               OR CASE WHEN json_valid(metadata)
                                  THEN COALESCE(json_extract(metadata,'$.payment_confirmed'),0) ELSE 0 END=1))
            )
        """, (user_id,)).fetchone() is not None


def has_purchases(user_id: int) -> bool:
    with closing(database._create_connection()) as conn:
        return _has_purchases(conn, user_id)


def _discount_quote(conn: sqlite3.Connection, user_id: int, base_cents: int, *, eligible: bool,
                    minimum_cents: int = 1) -> dict:
    policy = _policy(conn)
    row = conn.execute("SELECT referred_by FROM users WHERE telegram_id=?", (user_id,)).fetchone()
    referrer_id = valid_referrer(conn, user_id, row[0]) if row else None
    reason = "available"
    if not eligible:
        reason = "ineligible_plan"
    elif not policy["enabled"]:
        reason = "disabled"
    elif not referrer_id:
        reason = "no_referrer"
    elif policy["discount"] <= 0:
        reason = "zero_discount"
    elif _has_purchases(conn, user_id) and not referral_test_sessions.discount_eligible(conn, user_id):
        reason = "already_purchased"
    elif conn.execute("""
        SELECT 1 FROM referral_discount_invoices WHERE user_id=? AND discount_cents>0
        AND status IN ('reserved','issued','uncertain','consumed')
    """, (user_id,)).fetchone():
        reason = "already_claimed"
    # Never round a payable price to zero, even for a one-kopeck tariff.
    price_cents = max(minimum_cents, int((Decimal(base_cents) * (100 - policy["discount"]) / 100).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP,
    ))) if reason == "available" else base_cents
    return {"base_cents": base_cents, "price_cents": price_cents,
            "discount_cents": base_cents - price_cents, "referrer_id": referrer_id,
            "discount_percent": str(policy["discount"]), "reason": reason}


def preview_discount_price(user_id: int, base_price, *, eligible: bool = True) -> Decimal:
    """Read-only display quote; only prepare_discount_invoice reserves the price."""
    cents = money_cents(base_price)
    if cents <= 0:
        raise ValueError("Стоимость покупки должна быть положительной.")
    with closing(database._create_connection()) as conn:
        conn.execute("BEGIN")
        quote = _discount_quote(conn, user_id, cents, eligible=eligible)
        return Decimal(quote["price_cents"]) / 100


def _discount_invoice_row(row) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    result["price_rub"] = Decimal(result["price_cents"]) / 100
    result["claim_id"] = result["id"] if result["discount_cents"] > 0 else None
    return result


def prepare_discount_invoice(user_id: int, base_price, attempt_id: str, provider: str, *, eligible: bool = True,
                             minimum_price=Decimal("0.01")) -> dict:
    """Atomically reserve one discounted invoice across both bot runtimes.

    The immutable attempt stores its quoted price before any provider request.
    Other invoices use full price; unknown outcomes never free a claim. Callers
    must not re-send an uncertain provider request using a new attempt id.
    """
    cents, minimum_cents = money_cents(base_price), money_cents(minimum_price)
    attempt_id, provider = str(attempt_id or "").strip(), str(provider or "").strip()
    if cents <= 0 or not attempt_id or len(attempt_id) > 200 or not provider or len(provider) > 100:
        raise ValueError("Некорректная стоимость, идентификатор или провайдер счёта.")
    if minimum_cents <= 0 or cents < minimum_cents:
        raise ValueError("Стоимость тарифа ниже минимальной суммы платёжного провайдера.")
    with closing(database._create_connection()) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM referral_discount_invoices WHERE attempt_id=?", (attempt_id,),
        ).fetchone()
        if existing:
            if (existing["user_id"] != int(user_id) or existing["base_cents"] != cents
                    or existing["provider"] != provider or existing["price_cents"] < minimum_cents):
                raise ValueError("Повторная попытка создания счёта имеет другие параметры.")
            return _discount_invoice_row(existing)
        quote = _discount_quote(conn, user_id, cents, eligible=eligible, minimum_cents=minimum_cents)
        invoice_id = str(uuid4())
        conn.execute("""
            INSERT INTO referral_discount_invoices
            (id,attempt_id,user_id,provider,base_cents,price_cents,discount_cents,discount_percent,referrer_id,status,reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (invoice_id, attempt_id, int(user_id), provider, cents, quote["price_cents"], quote["discount_cents"],
              quote["discount_percent"], quote["referrer_id"],
              "reserved" if quote["discount_cents"] > 0 else "full_price", quote["reason"]))
        return _discount_invoice_row(conn.execute(
            "SELECT * FROM referral_discount_invoices WHERE id=?", (invoice_id,),
        ).fetchone())


def mark_discount_invoice(claim_id: str | None, payment_id: str | None = None, *, status: str = "issued", note: str = "") -> dict | None:
    """Record provider outcome; cancellation requires confirmed non-payability.

    Cancellation is safe only after provider-confirmed cancellation or proof
    that the request was not sent. A timeout/error/clock expiry is uncertain.
    """
    if not claim_id:
        return None
    note = str(note or "").strip()[:2000]
    if status not in {"issued", "uncertain", "cancelled"}:
        raise ValueError("Неизвестный статус реферального счёта.")
    if status == "cancelled" and not note:
        raise ValueError("Для отмены нужно подтверждение, что счёт больше нельзя оплатить.")
    normalized_payment_id = str(payment_id or "").strip() or None
    with closing(database._create_connection()) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM referral_discount_invoices WHERE id=?", (claim_id,)).fetchone()
        if row is None or row["discount_cents"] <= 0:
            raise ValueError("Реферальный счёт не найден.")
        if normalized_payment_id and row["payment_id"] and normalized_payment_id != row["payment_id"]:
            raise ValueError("К скидке уже привязан другой платёж.")
        if row["status"] == "consumed":
            return _discount_invoice_row(row)
        if row["status"] == "cancelled":
            if status != "cancelled":
                raise ValueError("Отменённый реферальный счёт нельзя использовать снова.")
            return _discount_invoice_row(row)
        conn.execute("""
            UPDATE referral_discount_invoices SET status=?,payment_id=COALESCE(?,payment_id),note=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (status, normalized_payment_id, note, claim_id))
        return _discount_invoice_row(conn.execute(
            "SELECT * FROM referral_discount_invoices WHERE id=?", (claim_id,),
        ).fetchone())


def consume_discount_invoice(conn: sqlite3.Connection, user_id: int, payment_id: str, amount_cents: int,
                             claim_id: str | None = None) -> None:
    """Run inside payment accounting, preserving the originally issued price."""
    # A provider callback may win the race against mark_discount_invoice.
    cursor = conn.execute("""
        SELECT id,user_id,price_cents,status,payment_id,consumed_payment_id
        FROM referral_discount_invoices WHERE discount_cents>0 AND (id=? OR payment_id=?)
    """, (claim_id, payment_id))
    rows = cursor.fetchall()
    if not rows:
        if claim_id:
            raise ValueError("Скидка платежа не найдена.")
        return
    if len(rows) != 1:
        raise ValueError("Платёж связан с несколькими скидками.")
    invoice_id, owner, price_cents, status, bound_payment, consumed_payment = rows[0]
    if owner != user_id or price_cents != amount_cents or (bound_payment and bound_payment != payment_id):
        raise ValueError("Параметры платежа не совпадают с реферальным счётом.")
    if status == "cancelled" or (status == "consumed" and consumed_payment != payment_id):
        raise ValueError("Скидка уже использована или счёт отменён.")
    conn.execute("""
        UPDATE referral_discount_invoices SET status='consumed',payment_id=?,consumed_payment_id=?,
            updated_at=CURRENT_TIMESTAMP WHERE id=?
    """, (payment_id, payment_id, invoice_id))


def list_discount_invoices(user_id: int | None = None, *, limit: int = 100, offset: int = 0) -> list[dict]:
    with closing(database._create_connection()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT * FROM referral_discount_invoices WHERE (? IS NULL OR user_id=?)
            ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?
        """, (user_id, user_id, min(500, max(1, int(limit))), max(0, int(offset)))).fetchall()
        return [_discount_invoice_row(row) for row in rows]


def get_payment_reward(payment_id: str) -> dict:
    with closing(database._create_connection()) as conn:
        row = conn.execute(
            "SELECT referrer_id, referral_reward FROM payment_accounting_effects WHERE payment_id=?", (payment_id,)
        ).fetchone()
    return {"referrer_id": row[0] if row else None, "reward": float(row[1] or 0) if row else 0.0,
            "reward_cents": money_cents(row[1] or 0) if row else 0}


def _withdrawal_row(row) -> dict | None:
    if row is None:
        return None
    result = dict(row)
    result["amount"] = result["amount_cents"] / 100
    return result


def _get_withdrawal(conn: sqlite3.Connection, request_id: int) -> dict | None:
    return _withdrawal_row(conn.execute("""
        SELECT w.*, u.username,n.id notification_id,n.status notification_status,n.error notification_error
        FROM referral_withdrawals w LEFT JOIN users u ON u.telegram_id=w.user_id
        LEFT JOIN referral_withdrawal_notifications n ON n.withdrawal_id=w.id
        WHERE w.id=?
    """, (request_id,)).fetchone())


def get_withdrawal(request_id: int) -> dict | None:
    with closing(database._create_connection()) as conn:
        conn.row_factory = sqlite3.Row
        return _get_withdrawal(conn, request_id)


def create_withdrawal(user_id: int, details: str, *, source: str = "main") -> dict:
    details = str(details or "").strip()
    if not 5 <= len(details) <= 2000:
        raise ValueError("Укажите реквизиты длиной от 5 до 2000 символов.")
    if source not in {"main", "reserve"}:
        raise ValueError("Неизвестный источник заявки.")
    with closing(database._create_connection()) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        policy = _policy(conn)
        if not policy["enabled"]:
            raise ValueError("Реферальная программа выключена. Новые заявки временно недоступны.")
        _ensure_account(conn, user_id)
        pending = conn.execute(
            "SELECT id FROM referral_withdrawals WHERE user_id=? AND status='pending'", (user_id,)
        ).fetchone()
        if pending:
            return {**_get_withdrawal(conn, pending[0]), "is_new": False}
        balance = conn.execute("SELECT balance_cents FROM referral_accounts WHERE user_id=?", (user_id,)).fetchone()[0]
        if balance < money_cents(policy["minimum"]):
            raise ValueError(f"Минимальная сумма вывода — {policy['minimum']:f} ₽.")
        cursor = conn.execute("""
            INSERT INTO referral_withdrawals(user_id,amount_cents,details,source) VALUES (?,?,?,?)
        """, (user_id, balance, details, source))
        request_id = cursor.lastrowid
        conn.execute("""
            UPDATE referral_accounts SET balance_cents=balance_cents-?,reserved_cents=reserved_cents+?
            WHERE user_id=?
        """, (balance, balance, user_id))
        conn.execute("""
            INSERT INTO referral_ledger(user_id,kind,amount_cents,reserved_delta_cents,withdrawal_id)
            VALUES (?,'reserve',?,?,?)
        """, (user_id, -balance, balance, request_id))
        _sync_legacy(conn, user_id)
        return {**_get_withdrawal(conn, request_id), "is_new": True}


def resolve_withdrawal(request_id: int, action: str, admin_id, note: str = "") -> dict:
    if action not in {"paid", "rejected"}:
        raise ValueError("Допустимые действия: paid или rejected.")
    actor = str(admin_id or "").strip()
    if not actor or len(actor) > 128:
        raise ValueError("Не указан администратор.")
    note = str(note or "").strip()
    if len(note) > 2000:
        raise ValueError("Комментарий не должен превышать 2000 символов.")
    with closing(database._create_connection()) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = _get_withdrawal(conn, request_id)
        if row is None:
            raise ValueError("Заявка на вывод не найдена.")
        if row["status"] == action:
            return {**row, "is_new": False}
        if row["status"] != "pending":
            raise ValueError("Заявка уже обработана с другим результатом.")
        user_id, cents = row["user_id"], row["amount_cents"]
        release = cents if action == "rejected" else 0
        result = conn.execute("""
            UPDATE referral_accounts SET balance_cents=balance_cents+?, reserved_cents=reserved_cents-?
            WHERE user_id=? AND reserved_cents>=?
        """, (release, cents, user_id, cents))
        if result.rowcount != 1:
            raise ValueError("Резерв заявки не совпадает с балансом. Требуется сверка.")
        conn.execute("""
            UPDATE referral_withdrawals SET status=?,resolved_at=CURRENT_TIMESTAMP,admin_id=?,note=? WHERE id=?
        """, (action, actor, note, request_id))
        conn.execute("""
            INSERT INTO referral_ledger(user_id,kind,amount_cents,reserved_delta_cents,withdrawal_id,note)
            VALUES (?,?,?,?,?,?)
        """, (user_id, "release" if release else "paid", release, -cents, request_id, note))
        conn.execute("""
            INSERT INTO referral_withdrawal_notifications(withdrawal_id,user_id,action,source)
            VALUES (?,?,?,?)
        """, (request_id, user_id, action, row["source"]))
        _sync_legacy(conn, user_id)
        return {**_get_withdrawal(conn, request_id), "is_new": True}


def list_withdrawals(status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
    if status is not None and status not in {"pending", "paid", "rejected"}:
        raise ValueError("Неизвестный статус заявки.")
    with closing(database._create_connection()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT w.*,u.username,n.id notification_id,n.status notification_status,n.error notification_error
            FROM referral_withdrawals w LEFT JOIN users u ON u.telegram_id=w.user_id
            LEFT JOIN referral_withdrawal_notifications n ON n.withdrawal_id=w.id
            WHERE (? IS NULL OR w.status=?) ORDER BY w.id DESC LIMIT ? OFFSET ?
        """, (status, status, min(500, max(1, int(limit))), max(0, int(offset)))).fetchall()
        return [_withdrawal_row(row) for row in rows]


_NOTIFICATION_SELECT = """
    SELECT n.*,w.amount_cents,w.note,u.username FROM referral_withdrawal_notifications n
    JOIN referral_withdrawals w ON w.id=n.withdrawal_id
    LEFT JOIN users u ON u.telegram_id=n.user_id
"""


def list_withdrawal_notifications(*, limit: int = 100, offset: int = 0) -> list[dict]:
    with closing(database._create_connection()) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(
            _NOTIFICATION_SELECT + " ORDER BY CASE n.status WHEN 'failed' THEN 0 WHEN 'pending' THEN 1 "
            "WHEN 'sending' THEN 2 ELSE 3 END,n.id DESC LIMIT ? OFFSET ?",
            (min(500, max(1, int(limit))), max(0, int(offset))),
        ).fetchall()]


def claim_withdrawal_notifications(source: str, limit: int = 10) -> list[dict]:
    """Lease a bounded batch for its originating bot; a crashed worker retries.

    Telegram has no send-message idempotency key, so delivery is at least once:
    a timeout or crash after delivery can produce a duplicate notification.
    No retry repeats the financial decision or its ledger entry.
    """
    if source not in {"main", "reserve"}:
        raise ValueError("Неизвестный источник уведомления.")
    with closing(database._create_connection()) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            UPDATE referral_withdrawal_notifications SET
                status=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,
                error='Истёк срок отправки; доставка предыдущей попытки не подтверждена.',
                lease_token=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP
            WHERE source=? AND status='sending' AND lease_until<=CURRENT_TIMESTAMP
        """, (source,))
        rows = conn.execute("""
            SELECT id FROM referral_withdrawal_notifications WHERE source=? AND status='pending'
                AND next_retry_at<=CURRENT_TIMESTAMP AND attempts<5 ORDER BY id LIMIT ?
        """, (source, min(100, max(1, int(limit))))).fetchall()
        results = []
        for (notification_id,) in rows:
            token = str(uuid4())
            conn.execute("""
                UPDATE referral_withdrawal_notifications SET status='sending',attempts=attempts+1,
                    total_attempts=total_attempts+1,lease_token=?,lease_until=datetime('now','+5 minutes'),
                    updated_at=CURRENT_TIMESTAMP WHERE id=?
            """, (token, notification_id))
            results.append(dict(conn.execute(
                _NOTIFICATION_SELECT + " WHERE n.id=?", (notification_id,),
            ).fetchone()))
        return results


def complete_withdrawal_notification(notification_id: int, *, success: bool, message_id=None,
                                     error: str = "", retryable: bool = True, lease_token: str | None = None,
                                     retry_after_seconds: int | None = None) -> dict:
    with closing(database._create_connection()) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM referral_withdrawal_notifications WHERE id=?", (notification_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Уведомление не найдено.")
        if row["status"] != "sending" or not lease_token or row["lease_token"] != lease_token:
            raise ValueError("Попытка отправки уже завершена или принадлежит другому обработчику.")
        if success:
            conn.execute("""
                UPDATE referral_withdrawal_notifications SET status='sent',message_id=?,error='',
                    sent_at=CURRENT_TIMESTAMP,lease_token=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?
            """, (str(message_id) if message_id is not None else None, notification_id))
        else:
            attempts = row["attempts"]
            status = "pending" if retryable and attempts < 5 else "failed"
            backoff = (30, 120, 600, 1800, 3600)[min(4, max(0, attempts - 1))]
            delay = max(backoff, min(86400, max(0, int(retry_after_seconds or 0))))
            conn.execute("""
                UPDATE referral_withdrawal_notifications SET status=?,error=?,
                    next_retry_at=datetime('now',?),lease_token=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (status, str(error or "Доставка не подтверждена.")[:1000], f"+{delay} seconds", notification_id))
        return dict(conn.execute(_NOTIFICATION_SELECT + " WHERE n.id=?", (notification_id,)).fetchone())


def retry_withdrawal_notification(notification_id: int) -> dict:
    """Explicitly retry failed delivery; never repeats financial effects."""
    with closing(database._create_connection()) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM referral_withdrawal_notifications WHERE id=?", (notification_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Уведомление не найдено.")
        if row[0] not in {"failed", "pending"}:
            raise ValueError("Уведомление уже отправлено или сейчас отправляется.")
        conn.execute("""
            UPDATE referral_withdrawal_notifications SET status='pending',attempts=0,
                next_retry_at=CURRENT_TIMESTAMP,lease_token=NULL,lease_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?
        """, (notification_id,))
        return dict(conn.execute(_NOTIFICATION_SELECT + " WHERE n.id=?", (notification_id,)).fetchone())


_USER_SELECT = """
    SELECT u.telegram_id,u.username,u.referred_by,u.registration_date,u.total_spent,inviter.username referrer_username,
      COALESCE(a.balance_cents,0) balance_cents, COALESCE(a.reserved_cents,0) reserved_cents,
      COALESCE(a.lifetime_cents,0) lifetime_cents,COALESCE(a.legacy_opening_cents,0) legacy_opening_cents,
      COALESCE(a.legacy_lifetime_cents,0) legacy_lifetime_cents,
      (SELECT COUNT(*) FROM users r WHERE r.referred_by=u.telegram_id) referral_count
    FROM users u LEFT JOIN referral_accounts a ON a.user_id=u.telegram_id
    LEFT JOIN users inviter ON inviter.telegram_id=u.referred_by
"""


def _user_row(row) -> dict:
    result = dict(row)
    for name in ("balance", "reserved", "lifetime", "legacy_opening", "legacy_lifetime"):
        result[name] = result[name + "_cents"] / 100
    result["count"] = result["referral_count"]
    result["legacy_lifetime_incomplete"] = result["legacy_lifetime_cents"] < result["legacy_opening_cents"]
    return result


def get_user_summary(user_id: int) -> dict:
    with closing(database._create_connection()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        row = conn.execute(_USER_SELECT + " WHERE u.telegram_id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError("Пользователь не найден.")
        result = _user_row(row)
        result["referrals"] = [_user_row(item) for item in conn.execute(
            _USER_SELECT + " WHERE u.referred_by=? ORDER BY u.registration_date DESC,u.telegram_id DESC", (user_id,)
        ).fetchall()]
        pending = conn.execute(
            "SELECT id FROM referral_withdrawals WHERE user_id=? AND status='pending'", (user_id,)
        ).fetchone()
        result["pending"] = _get_withdrawal(conn, pending[0]) if pending else None
        result["withdrawals"] = [_withdrawal_row(item) for item in conn.execute(
            "SELECT w.* FROM referral_withdrawals w WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,)
        ).fetchall()]
        result["ledger"] = [dict(item) for item in conn.execute(
            "SELECT * FROM referral_ledger WHERE user_id=? ORDER BY id DESC LIMIT 100", (user_id,)
        ).fetchall()]
        result["discount_invoices"] = [_discount_invoice_row(item) for item in conn.execute(
            "SELECT * FROM referral_discount_invoices WHERE user_id=? ORDER BY rowid DESC LIMIT 100", (user_id,),
        ).fetchall()]
        result["notifications"] = [dict(item) for item in conn.execute(
            _NOTIFICATION_SELECT + " WHERE n.user_id=? ORDER BY n.id DESC LIMIT 100", (user_id,),
        ).fetchall()]
        return result


def get_dashboard(
    query: str = "", limit: int = 100, offset: int = 0, *, reward_limit: int = 100, reward_offset: int = 0,
) -> dict:
    with closing(database._create_connection()) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        summary = dict(conn.execute("""
            SELECT COUNT(*) total_accounts,COALESCE(SUM(balance_cents),0) balance_cents,
                COALESCE(SUM(reserved_cents),0) reserved_cents,COALESCE(SUM(lifetime_cents),0) lifetime_cents,
                COALESCE(SUM(legacy_opening_cents),0) legacy_opening_cents,
                COALESCE(MAX(legacy_lifetime_cents<legacy_opening_cents),0) legacy_lifetime_incomplete
            FROM referral_accounts
        """).fetchone())
        summary.update(dict(conn.execute("""
            SELECT COALESCE(SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END),0) pending_count,
                   COALESCE(SUM(CASE WHEN status='paid' THEN amount_cents ELSE 0 END),0) paid_cents
            FROM referral_withdrawals
        """).fetchone()))
        summary["referred_users_count"] = conn.execute("SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL").fetchone()[0]
        for name in ("balance", "reserved", "lifetime", "paid", "legacy_opening"):
            summary[name] = summary[name + "_cents"] / 100
        clause = """ WHERE (u.referred_by IS NOT NULL OR COALESCE(a.balance_cents,0)>0
            OR COALESCE(a.reserved_cents,0)>0 OR COALESCE(a.lifetime_cents,0)>0
            OR EXISTS(SELECT 1 FROM users r WHERE r.referred_by=u.telegram_id))
            AND (?='' OR CAST(u.telegram_id AS TEXT) LIKE ? ESCAPE '\\' OR u.username LIKE ? ESCAPE '\\')
        """
        search = str(query or "").strip().lstrip("@")
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args = (search, pattern, pattern)
        summary["users_count"] = conn.execute(
            "SELECT COUNT(*) FROM (" + _USER_SELECT + clause + ")", ("", "%%", "%%"),  # noqa: S608 - static SQL.
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM (" + _USER_SELECT + clause + ")", args).fetchone()[0]  # noqa: S608 - static SQL; search is bound.
        users = [_user_row(row) for row in conn.execute(
            _USER_SELECT + clause + " ORDER BY balance_cents DESC,u.telegram_id DESC LIMIT ? OFFSET ?",
            (*args, min(500, max(1, int(limit))), max(0, int(offset))),
        ).fetchall()]
        rewards = [dict(row) for row in conn.execute("""
            SELECT p.*,b.username buyer_username,r.username referrer_username
            FROM payment_accounting_effects p JOIN users b ON b.telegram_id=p.user_id
            JOIN users r ON r.telegram_id=p.referrer_id
            WHERE p.referral_reward>0 AND p.user_id<>p.referrer_id
            ORDER BY p.applied_at DESC,p.payment_id DESC LIMIT ? OFFSET ?
        """, (min(500, max(1, int(reward_limit))), max(0, int(reward_offset)))).fetchall()]
        for row in rewards:
            row.update(reward=row["referral_reward"], reward_cents=money_cents(row["referral_reward"]),
                       amount_cents=money_cents(row["amount_rub"]))
        rewards_total = conn.execute("""
            SELECT COUNT(*) FROM payment_accounting_effects p JOIN users r ON r.telegram_id=p.referrer_id
            JOIN users b ON b.telegram_id=p.user_id WHERE p.referral_reward>0 AND p.user_id<>p.referrer_id
        """).fetchone()[0]
        return {"summary": summary, "users": users, "users_total": total, "rewards": rewards,
                "rewards_total": rewards_total}
