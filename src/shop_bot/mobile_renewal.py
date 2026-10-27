"""Native renewal checkout: durable intent, server quotes, existing LAVA fulfillment.

No entitlement is issued here. Provider callbacks and the existing reserve
scheduler remain the only payment confirmation/fulfillment path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlsplit

QUOTE_TTL = 600
UNDELIVERED_EXPIRY_GRACE = 600
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
OPAQUE_RE = re.compile(r"^[A-Za-z0-9_-]{24}$")


class DefiniteCheckoutFailure(Exception):
    """Only failures known to precede invoice creation may allow a fresh intent."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def target_fingerprint(key):
    # Expiry may legitimately change due to a separate, confirmed renewal.
    fields = ("user_id", "key_id", "host_name", "key_email", "xui_client_uuid",
              "panel_sub_id", "subscription_tier")
    value = {name: key.get(name) for name in fields}
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def strict_target_matches(metadata, key, user_id):
    if metadata.get("mobile_strict_renewal") is not True:
        return True
    return bool(key and int(key.get("user_id") or 0) == int(user_id)
                and not key.get("is_archived")
                and metadata.get("mobile_target_fingerprint") == target_fingerprint(key))


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def expiry_timestamp(key):
    value = datetime.fromisoformat(str(key["expiry_date"]).replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


class ProductionBackend:
    """Lazy adapters keep registration independent from bot SDK initialization."""

    def catalog(self, user, key):
        from shop_bot.bot import handlers as h
        from shop_bot.modules import lava_api
        from shop_bot.super_promotion import is_super_offer_available, promotion_metadata
        from shop_bot.data_manager.host_visibility import is_customer_host_visible

        config = lava_api.LavaConfig.from_env()  # Validation only; no provider I/O.
        if not config.webhook_url or not is_customer_host_visible(key.get("host_name")):
            return [], None, config.invoice_ttl_minutes * 60
        tier = str(key.get("subscription_tier") or "").strip().lower()
        if tier not in {"standard", "super"}:
            tier = "super" if h._is_super_key_host_name(key.get("host_name")) else "standard"
        if tier == "super":
            plans = [{"id": "super:" + p["code"], "plan_id": 0, "title": p["name"],
                      "months": int(p["months"]), "price": p["price"], "is_super": True,
                      **promotion_metadata(p)}
                     for p in h.SUPER_KEY_PLANS if is_super_offer_available(p)]
        else:
            plans = [{"id": "standard:" + str(p["plan_id"]), "plan_id": int(p["plan_id"]),
                      "title": p["plan_name"], "months": int(p["months"]),
                      "price": p["price"], "is_super": False, **promotion_metadata(p)}
                     for p in h.get_plans_for_host(h._get_standard_key_host_name())]
        for plan in plans:
            amount = h._apply_first_purchase_discount(Decimal(str(plan.pop("price"))), user, plan_data=plan)
            plan["amount_minor"] = int(amount * 100)
        plans = [p for p in plans if 0 < p["months"] <= 120 and 100 <= p["amount_minor"] <= 200000000]
        mode = "replace_trial" if tier == "super" and h._is_trial_super_key_email(key.get("key_email")) else "same_key"
        return plans, mode, config.invoice_ttl_minutes * 60

    def precheck(self, key, offer):
        from shop_bot.bot import handlers as h
        result = h._run_purchase_precheck(key.get("host_name"), is_super_key_flow=offer["is_super"], plan_data=offer)
        return bool(result.allowed)

    def create(self, order_id, offer, method):
        from shop_bot.modules import lava_api
        return asyncio.run(lava_api.create_payment(amount_rub=Decimal(offer["amount_minor"]) / 100,
                           order_id=order_id, description="Пепел ВПН", payment_service=method))

    def lookup(self, order_id, amount_minor):
        from shop_bot.modules import lava_api
        return asyncio.run(lava_api.check_status(order_id=order_id, expected_amount=Decimal(amount_minor) / 100))

    def transaction(self, payment_id):
        from shop_bot.data_manager.database import get_transaction_by_payment_id
        return get_transaction_by_payment_id(payment_id)

    def attach(self, invoice_id, metadata, amount_minor):
        from shop_bot.data_manager.database import create_pending_transaction
        return create_pending_transaction(invoice_id, metadata["user_id"], amount_minor / 100,
                                          metadata, bot_source="reserve")


class RenewalService:
    def __init__(self, store, get_key, error, backend=None):
        self.store, self.get_key, self.error = store, get_key, error
        self.backend = backend or ProductionBackend()
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mobile_renewal_quotes (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, alias TEXT NOT NULL,
                    target_hash TEXT NOT NULL, offer_json TEXT NOT NULL, mode TEXT NOT NULL,
                    expires_at INTEGER NOT NULL, invoice_ttl INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mobile_checkout_intents (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, alias TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL, request_hash TEXT NOT NULL,
                    order_id TEXT NOT NULL UNIQUE, metadata_json TEXT NOT NULL,
                    amount_minor INTEGER NOT NULL, method TEXT NOT NULL,
                    state TEXT NOT NULL, invoice_id TEXT, payment_url TEXT,
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                    checked_at INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(user_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS mobile_checkout_owner ON mobile_checkout_intents(user_id, alias);
            """)

    def owned_key(self, user_id, alias):
        key_id = self.store.key_for_alias(user_id, alias)
        key = self.get_key(key_id) if key_id else None
        if not key or int(key.get("user_id") or 0) != user_id or key.get("is_archived"):
            raise self.error("subscription_not_found", 404)
        try:
            expiry_timestamp(key)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise self.error("subscription_unavailable", 409) from None
        return key

    def offers(self, user_id, user, alias):
        key = self.owned_key(user_id, alias)
        now = int(self.store.clock())
        expires = expiry_timestamp(key)
        try:
            offers, mode, ttl = self.backend.catalog(user, key)
        except Exception:
            raise self.error("provider_unavailable", 503) from None
        tier = str(key.get("subscription_tier") or "").lower()
        title = {"standard": "PEPELVPN Standard", "super": "PEPELVPN Super"}.get(tier, "PEPELVPN")
        result = {"eligible": bool(offers), "reason": None if offers else "renewal_unavailable",
                  "subscription": {"id": alias, "title": title, "expires_at": iso(expires), "active": expires > now},
                  "renewal_mode": mode or "same_key", "notice": None, "offers": [],
                  "methods": [{"id": "sbp", "title": "СБП"}, {"id": "card", "title": "Банковская карта"}] if offers else [],
                  "return_hint": "После оплаты вернитесь в приложение"}
        if mode == "replace_trial":
            result["notice"] = "После оплаты вместо пробного Суперключа появится новый платный ключ."
        with self.store.connection(write=True) as db:
            db.execute("DELETE FROM mobile_renewal_quotes WHERE expires_at < ?", (now - 86400,))
            for offer in offers:
                quote_id = secrets.token_urlsafe(18)
                db.execute("INSERT INTO mobile_renewal_quotes VALUES (?,?,?,?,?,?,?,?)",
                           (quote_id, user_id, alias, target_fingerprint(key), canonical(offer), mode, now + QUOTE_TTL, ttl))
                # Reuse the bot's calendar-month semantics, without importing its DB.
                import calendar
                anchor = datetime.fromtimestamp(now if mode == "replace_trial" else max(now, expires), timezone.utc)
                month = anchor.month - 1 + offer["months"]
                year = anchor.year + month // 12
                projected = anchor.replace(year=year, month=month % 12 + 1,
                                           day=min(anchor.day, calendar.monthrange(year, month % 12 + 1)[1]))
                result["offers"].append({"id": offer["id"], "title": offer["title"],
                    "duration_months": offer["months"], "amount_minor": offer["amount_minor"], "currency": "RUB",
                    "quote_id": quote_id, "quote_expires_at": iso(now + QUOTE_TTL),
                    "projected_expires_at": projected.isoformat().replace("+00:00", "Z")})
        result["pending_payment"] = self.pending_payment(user_id, alias)
        return result

    def pending_payment(self, user_id, alias, *, reconcile=False):
        with self.store.connection() as db:
            row = db.execute("SELECT id FROM mobile_checkout_intents WHERE user_id=? AND alias=? AND (state IN ('creating','pending','paid_processing') OR (state='failed' AND invoice_id IS NOT NULL)) ORDER BY created_at LIMIT 1",
                             (user_id, alias)).fetchone()
        if row:
            value, _status = self.payment(user_id, row["id"], reconcile=reconcile)
            if value["payment"]["status"] in {"creating", "pending", "paid_processing"}:
                return value["payment"]
        return None

    def _read(self, payment_id, user_id):
        if not isinstance(payment_id, str) or not OPAQUE_RE.fullmatch(payment_id):
            raise self.error("payment_not_found", 404)
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM mobile_checkout_intents WHERE id=? AND user_id=?", (payment_id, user_id)).fetchone()
        if not row:
            raise self.error("payment_not_found", 404)
        return dict(row)

    def checkout(self, user_id, user, alias, data):
        if set(data) != {"offer_id", "quote_id", "method", "idempotency_key"}:
            raise self.error("invalid_checkout")
        if any(not isinstance(data.get(name), str) for name in data):
            raise self.error("invalid_checkout")
        if (data["method"] not in {"sbp", "card"} or not IDEMPOTENCY_RE.fullmatch(data["idempotency_key"])
                or not OPAQUE_RE.fullmatch(data["quote_id"]) or len(data["offer_id"]) > 100):
            raise self.error("invalid_checkout")
        key = self.owned_key(user_id, alias)
        request_hash = hashlib.sha256(canonical({"alias": alias, **data}).encode()).hexdigest()
        with self.store.connection() as db:
            prior = db.execute("SELECT * FROM mobile_checkout_intents WHERE user_id=? AND idempotency_key=?",
                               (user_id, data["idempotency_key"])).fetchone()
        if prior:
            if prior["request_hash"] != request_hash:
                raise self.error("idempotency_conflict", 409)
            return self.payment(user_id, prior["id"])
        # Reconcile terminal state already recorded by webhook/scheduler before
        # considering a new intent; this performs no provider request.
        self.pending_payment(user_id, alias, reconcile=True)
        now = int(self.store.clock())
        with self.store.connection() as db:
            quote = db.execute("SELECT * FROM mobile_renewal_quotes WHERE id=? AND user_id=? AND alias=?",
                               (data["quote_id"], user_id, alias)).fetchone()
        if not quote or quote["expires_at"] <= now:
            raise self.error("quote_expired", 409)
        offer = json.loads(quote["offer_json"])
        if offer["id"] != data["offer_id"]:
            raise self.error("quote_changed", 409)
        if quote["target_hash"] != target_fingerprint(key):
            raise self.error("target_changed", 409)
        try:
            current, mode, _ttl = self.backend.catalog(user, key)
            selected = next((p for p in current if p["id"] == offer["id"]), None)
            if selected != offer or mode != quote["mode"]:
                raise self.error("quote_changed", 409)
            if not self.backend.precheck(key, offer):
                raise self.error("renewal_unavailable", 503)
        except self.error:
            raise
        except Exception:
            raise self.error("provider_unavailable", 503) from None
        # Recheck after preflight, which can take time or race with a target change.
        key = self.owned_key(user_id, alias)
        if quote["target_hash"] != target_fingerprint(key):
            raise self.error("target_changed", 409)
        now = int(self.store.clock())
        if quote["expires_at"] <= now:
            raise self.error("quote_expired", 409)
        payment_id = secrets.token_urlsafe(18)
        order_id = "mobile_" + payment_id
        metadata = {"user_id": user_id, "months": offer["months"], "price": offer["amount_minor"] / 100,
                    "action": "extend", "key_id": key["key_id"], "host_name": key["host_name"],
                    "plan_id": offer["plan_id"], "plan_name": offer["title"], "customer_email": None,
                    "payment_method": "LAVA", "lava_order_id": order_id, "lava_payment_service": data["method"],
                    "lava_payment_variant": data["method"], "bot_source": "reserve",
                    "is_super_key_flow": offer["is_super"], "mobile_strict_renewal": True,
                    "mobile_target_fingerprint": quote["target_hash"], "mobile_renewal_mode": quote["mode"],
                    "mobile_payment_id": payment_id}
        for field in ("promo_code", "paid_months", "bonus_months"):
            if field in offer:
                metadata[field] = offer[field]
        with self.store.connection(write=True) as db:
            # Serialize both API runtimes before any provider I/O.
            prior = db.execute("SELECT * FROM mobile_checkout_intents WHERE user_id=? AND idempotency_key=?",
                               (user_id, data["idempotency_key"])).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise self.error("idempotency_conflict", 409)
                payment_id = prior["id"]
            else:
                pending = db.execute("SELECT id FROM mobile_checkout_intents WHERE user_id=? AND alias=? AND (state IN ('creating','pending','paid_processing') OR (state='failed' AND invoice_id IS NOT NULL)) LIMIT 1",
                                     (user_id, alias)).fetchone()
                if pending:
                    raise self.error("pending_checkout_exists", 409)
                db.execute("""INSERT INTO mobile_checkout_intents
                    (id,user_id,alias,idempotency_key,request_hash,order_id,metadata_json,amount_minor,method,state,created_at,expires_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'creating',?,?)""",
                    (payment_id, user_id, alias, data["idempotency_key"], request_hash, order_id,
                     canonical(metadata), offer["amount_minor"], data["method"], now, now + quote["invoice_ttl"]))
        if prior:
            return self.payment(user_id, payment_id)
        try:
            created = self.backend.create(order_id, offer, data["method"])
            self._record_invoice(payment_id, user_id, created)
        except DefiniteCheckoutFailure:
            with self.store.connection(write=True) as db:
                db.execute("UPDATE mobile_checkout_intents SET state='failed' WHERE id=? AND state='creating'", (payment_id,))
        except Exception:
            # Intent survives ambiguous network/provider failure. Never call create
            # again for it; subsequent reads reconcile the SAME order id only.
            pass
        return self.payment(user_id, payment_id, reconcile=False)

    def _record_invoice(self, payment_id, user_id, result):
        invoice_id = str(result.get("payment_id") or result.get("invoice_id") or "").strip()
        payment_url = str(result.get("payment_url") or "").strip()
        parsed = urlsplit(payment_url)
        if not invoice_id or len(invoice_id) > 128:
            raise ValueError("invalid invoice")
        if payment_url and (parsed.scheme != "https" or parsed.hostname != "lava.ru"
                            or not parsed.path.startswith("/pay/") or parsed.username or parsed.password
                            or parsed.port not in (None, 443)):
            raise ValueError("invalid checkout URL")
        row = self._read(payment_id, user_id)
        if result.get("order_id") and result["order_id"] != row["order_id"]:
            raise ValueError("invoice order mismatch")
        with self.store.connection(write=True) as db:
            provider_state = {"succeeded": "paid_processing", "canceled": "cancelled"}.get(result.get("status"), "pending")
            db.execute("UPDATE mobile_checkout_intents SET invoice_id=?,payment_url=?,state=? WHERE id=? AND state='creating'",
                       (invoice_id, payment_url or None, provider_state, payment_id))

    def _attach_transaction(self, row):
        metadata = json.loads(row["metadata_json"])
        metadata.update(external_payment_id=row["invoice_id"], lava_invoice_id=row["invoice_id"])
        tx = self.backend.transaction(row["invoice_id"])
        if not tx:
            self.backend.attach(row["invoice_id"], metadata, row["amount_minor"])
            tx = self.backend.transaction(row["invoice_id"])
        if not tx:
            return None
        stored = tx.get("metadata")
        if isinstance(stored, str):
            stored = json.loads(stored)
        if (not isinstance(stored, dict) or int(tx.get("user_id") or 0) != row["user_id"]
                or stored.get("lava_order_id") != row["order_id"]
                or stored.get("mobile_payment_id") != row["id"]
                or str(stored.get("bot_source")) != "reserve"
                or Decimal(str(tx.get("amount_rub"))) != Decimal(row["amount_minor"]) / 100):
            raise ValueError("transaction integrity mismatch")
        return tx

    def payment(self, user_id, payment_id, *, reconcile=True):
        row = self._read(payment_id, user_id)
        now = int(self.store.clock())
        # A read may repair a missing pending record but never confirms payment
        # or issues a key. The existing webhook/scheduler verifies provider state.
        if row["state"] == "creating" and reconcile and now - row["created_at"] >= 20 and now - row["checked_at"] >= 60:
            with self.store.connection(write=True) as db:
                claimed = db.execute("UPDATE mobile_checkout_intents SET checked_at=? WHERE id=? AND checked_at=?",
                                     (now, payment_id, row["checked_at"])).rowcount
            if claimed:
                try:
                    result = self.backend.lookup(row["order_id"], row["amount_minor"])
                    self._record_invoice(payment_id, user_id, result)
                except Exception:
                    pass
            row = self._read(payment_id, user_id)
            if (claimed and row["state"] == "creating" and not row["invoice_id"] and not row["payment_url"]
                    and now > row["expires_at"] + UNDELIVERED_EXPIRY_GRACE):
                # No payable URL was ever released, no known invoice/payment is
                # discarded, and a fresh same-order lookup has just been attempted.
                # This also recovers a crash between intent INSERT and provider I/O.
                with self.store.connection(write=True) as db:
                    db.execute("UPDATE mobile_checkout_intents SET state='failed' WHERE id=? AND state='creating' AND invoice_id IS NULL AND payment_url IS NULL",
                               (payment_id,))
                row = self._read(payment_id, user_id)
        updated_alias = None
        attached = False
        needs_support = False
        if row["invoice_id"]:
            try:
                tx = self._attach_transaction(row)
                attached = bool(tx)
                if tx:
                    state = row["state"]
                    if tx.get("status") == "paid" and tx.get("issuance_state") == "issued":
                        state = "fulfilled"
                        meta = tx.get("metadata")
                        meta = json.loads(meta) if isinstance(meta, str) else meta
                        key = self.get_key(int(meta.get("key_id") or 0))
                        if key and int(key.get("user_id") or 0) == user_id and not key.get("is_archived"):
                            updated_alias = self.store.alias_for(user_id, key["key_id"])
                    elif tx.get("status") == "canceled":
                        state = "cancelled"
                    elif tx.get("issuance_state") in {"fatal_failed", "exhausted"}:
                        # Issuance failure is not payment failure. Keep this paid
                        # liability unresolved so the customer cannot pay twice.
                        state = "paid_processing"
                        needs_support = True
                    elif tx.get("status") in {"paid", "processing"} or tx.get("issuance_state") in {"confirmed", "issuing", "retryable_failed"}:
                        state = "paid_processing"
                    with self.store.connection(write=True) as db:
                        db.execute("UPDATE mobile_checkout_intents SET state=? WHERE id=?", (state, payment_id))
                    row["state"] = state
            except Exception:
                # Never expose a payable URL before durable pending metadata exists.
                attached = False
        target_matches = False
        try:
            key = self.owned_key(user_id, row["alias"])
            target_matches = target_fingerprint(key) == json.loads(row["metadata_json"])["mobile_target_fingerprint"]
        except self.error:
            pass
        status = row["state"] if attached or (row["state"] == "failed" and not row["invoice_id"]) else "creating"
        payment = {"id": row["id"], "status": status,
                   "payment_url": row["payment_url"] if attached and target_matches and row["state"] == "pending" else None,
                   "expires_at": iso(row["expires_at"]), "subscription_id": row["alias"],
                   "updated_subscription_id": updated_alias,
                   "notice": ("Оплата получена, но продление не завершено. Обратитесь в поддержку; повторно оплачивать не нужно."
                              if needs_support else "Выбранный ключ изменился. Обратитесь в поддержку перед оплатой."
                              if not target_matches and status == "pending" else None)}
        return {"payment": payment}, (202 if payment["status"] == "creating" else 200)


def register_routes(api, store, require_auth, body, get_key, error, backend=None):
    from flask import g, jsonify
    service = RenewalService(store, get_key, error, backend)

    @api.get("/subscriptions/<alias>/renewal")
    @require_auth
    def renewal(alias):
        store.rate_limit("renewal-offers:" + str(g.mobile_session["user_id"]), 20)
        return jsonify(service.offers(g.mobile_session["user_id"], g.mobile_user, alias))

    @api.post("/subscriptions/<alias>/checkout")
    @require_auth
    def checkout(alias):
        store.rate_limit("renewal-checkout:" + str(g.mobile_session["user_id"]), 8)
        value, status = service.checkout(g.mobile_session["user_id"], g.mobile_user, alias, body())
        return jsonify(value), status

    @api.get("/payments/<payment_id>")
    @require_auth
    def payment(payment_id):
        value, status = service.payment(g.mobile_session["user_id"], payment_id)
        return jsonify(value), status

    return service
