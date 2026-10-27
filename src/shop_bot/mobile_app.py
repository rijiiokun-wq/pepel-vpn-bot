"""PEPELVPN native-app device authorization; separate database, no entitlements.

The Telegram challenge and the device polling secret are independent. Only the
hashes of challenges, polling secrets and bearer tokens are persisted. Telegram
identity is supplied exclusively by the existing authenticated aiogram update.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

REQUEST_TTL = 300
SESSION_TTL = 30 * 24 * 3600
PREFIX = "/mobile-api/v1"
BOT_USERNAME = "probelvpnbot"
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{24}$")


def enabled():
    return os.environ.get("MOBILE_APP_ENABLED", "").lower() in {"1", "true", "yes"}


class MobileError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def clean_device_name(value):
    if not isinstance(value, str):
        return "Android"
    return "".join(c for c in value if c.isalnum() or c in " ._()-")[:60].strip() or "Android"


def is_active(key, now=None):
    if not key or int(key.get("is_archived") or 0):
        return False
    try:
        expires = datetime.fromisoformat(str(key.get("expiry_date") or "").replace("Z", "+00:00"))
        if expires.tzinfo is None:
            # The bot stores naive UTC, as its SQLite CURRENT_TIMESTAMP checks do.
            expires = expires.replace(tzinfo=timezone.utc)
        return expires.timestamp() > (time.time() if now is None else now)
    except (ValueError, TypeError, OverflowError):
        return False


def expiry_iso(key):
    expires = datetime.fromisoformat(str(key["expiry_date"]).replace("Z", "+00:00"))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _whitelist_cache_db_path():
    """Mirror the bot's path resolver without importing its database initialization."""
    configured = os.environ.get("SHOP_DB_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    data_path = Path("/app/data/shop.db")
    return data_path if data_path.exists() else Path("/app/project/users.db")


def _whitelist_public_status(row, now):
    """Only a valid, owned cache row can assert a balance or unlimited traffic."""
    used, total, reset_ms = (row[name] for name in ("used_bytes", "total_bytes", "reset_timestamp_ms"))
    if any(type(value) is not int or not 0 <= value < 2 ** 63 for value in (used, total, reset_ms)):
        return None
    try:
        updated = datetime.fromisoformat(str(row["updated_at"] or "").replace("Z", "+00:00"))
        if updated.tzinfo is None:
            # SQLite CURRENT_TIMESTAMP is UTC, even though its text has no offset.
            updated = updated.replace(tzinfo=timezone.utc)
        updated = updated.astimezone(timezone.utc)
        timestamp = updated.timestamp()
        if timestamp < 0 or timestamp > now:
            return None
        reset = datetime.fromtimestamp(reset_ms / 1000, timezone.utc) if reset_ms else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None
    unlimited = total == 0
    return {
        "limit_bytes": None if unlimited else total,
        "used_bytes": used,
        "remaining_bytes": None if unlimited else max(0, total - used),
        "unlimited": unlimited,
        "updated_at": updated.isoformat().replace("+00:00", "Z"),
        "reset_at": reset.isoformat().replace("+00:00", "Z") if reset and not unlimited else None,
        "stale": now - timestamp > 20 * 60,
    }


def load_whitelist_traffic(user_id, keys, *, db_path=None, now=None):
    """Read existing per-subscription aggregate counters; never query or mutate a panel.

    White aliases share this balance. Summing per-node/alias counters would count
    the same usage repeatedly. A missing or stale mapping must not invent quota.
    """
    current = time.time() if now is None else now
    try:
        ids = sorted({int(key["key_id"]) for key in keys
                      if int(key.get("user_id") or 0) == int(user_id) and is_active(key, current)
                      and 0 < int(key["key_id"]) < 2 ** 63})
        if not ids:
            return {}
        path = Path(db_path) if db_path is not None else _whitelist_cache_db_path()
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            result = {}
            for offset in range(0, len(ids), 100):
                batch = ids[offset:offset + 100]
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(f"""
                    SELECT w.key_id, w.used_bytes, w.total_bytes, w.reset_timestamp_ms,
                           w.updated_at, v.expiry_date, v.is_archived
                    FROM whitelist_traffic_status w
                    JOIN vpn_keys v ON v.key_id = w.key_id AND v.user_id = w.user_id
                        AND v.key_email = w.key_email AND v.host_name = w.host_name
                    WHERE v.user_id = ? AND v.key_id IN ({placeholders})
                        AND COALESCE(v.is_archived, 0) = 0
                """, (int(user_id), *batch)).fetchall()
                for row in rows:
                    if is_active(dict(row), current):
                        status = _whitelist_public_status(row, current)
                        if status is not None:
                            result[row["key_id"]] = status
            return result
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError):
        # A cache outage must not block login, subscription selection or config.
        return {}


class AuthStore:
    def __init__(self, path, clock=time.time):
        self.path = str(path)
        self.clock = clock
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        # Both Flask runtimes can initialize the same fresh file at startup.
        # SQLite's first WAL transition can return BUSY immediately even with
        # busy_timeout. Retry only lock contention, never other schema errors.
        deadline = time.monotonic() + 5.0
        attempt = 0
        while True:
            try:
                self._initialize_schema()
                break
            except sqlite3.OperationalError as error:
                code = getattr(error, "sqlite_errorcode", 0) & 0xFF
                remaining = deadline - time.monotonic()
                if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or remaining <= 0:
                    raise
                time.sleep(min(0.02 * (2 ** min(attempt, 4)), 0.2, remaining))
                attempt += 1

    def _initialize_schema(self):
        with self.connection(timeout=0.2) as db:
            if str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS auth_requests (
                    request_id TEXT PRIMARY KEY, poll_hash TEXT NOT NULL,
                    challenge_hash TEXT NOT NULL UNIQUE, device_name TEXT NOT NULL,
                    confirmation_code TEXT NOT NULL, expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
                    user_id INTEGER, display_name TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
                    device_name TEXT NOT NULL, display_name TEXT NOT NULL,
                    created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS subscription_aliases (
                    alias TEXT PRIMARY KEY, user_id INTEGER NOT NULL, key_id INTEGER NOT NULL,
                    UNIQUE(user_id, key_id)
                );
                CREATE TABLE IF NOT EXISTS rate_limits (
                    scope TEXT PRIMARY KEY, window INTEGER NOT NULL, hits INTEGER NOT NULL
                );
            """)

    @contextmanager
    def connection(self, write=False, timeout=5):
        db = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def rate_limit(self, scope, limit, seconds=60):
        now = int(self.clock())
        window = now // seconds
        with self.connection(write=True) as db:
            db.execute("DELETE FROM rate_limits WHERE window < ?", (window - 2,))
            row = db.execute("SELECT window,hits FROM rate_limits WHERE scope=?", (scope,)).fetchone()
            count = row["hits"] if row and row["window"] == window else 0
            if count >= limit:
                raise MobileError("rate_limited", 429)
            db.execute("INSERT OR REPLACE INTO rate_limits VALUES (?,?,?)", (scope, window, count + 1))

    def start(self, device_name, ip="unknown"):
        self.rate_limit("start-ip:" + digest(ip), 8)
        self.rate_limit("start-global", 120)
        now = int(self.clock())
        request_id = secrets.token_urlsafe(18)
        poll_secret, challenge = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(1000000):06d}"
        with self.connection(write=True) as db:
            db.execute("DELETE FROM auth_requests WHERE expires_at < ?", (now - 3600,))
            db.execute("DELETE FROM sessions WHERE expires_at < ? OR revoked=1", (now,))
            if db.execute("SELECT COUNT(*) FROM auth_requests WHERE expires_at>?", (now,)).fetchone()[0] >= 1200:
                raise MobileError("rate_limited", 429)
            db.execute("""INSERT INTO auth_requests
                (request_id,poll_hash,challenge_hash,device_name,confirmation_code,expires_at,created_at)
                VALUES (?,?,?,?,?,?,?)""", (request_id, digest(poll_secret), digest(challenge),
                    clean_device_name(device_name), code, now + REQUEST_TTL, now))
        return {"request_id": request_id, "request_secret": poll_secret,
                "telegram_url": f"https://t.me/{BOT_USERNAME}?start=app_{challenge}",
                "expires_in": REQUEST_TTL, "confirmation_code": code, "poll_interval": 3}

    def claim(self, challenge, user_id, display_name):
        if not TOKEN_RE.fullmatch(challenge):
            raise MobileError("invalid_request", 404)
        now = int(self.clock())
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM auth_requests WHERE challenge_hash=?", (digest(challenge),)).fetchone()
            if not row or row["expires_at"] <= now or row["state"] not in {"pending", "claimed"}:
                raise MobileError("invalid_request", 404)
            if row["user_id"] is not None and row["user_id"] != int(user_id):
                raise MobileError("invalid_request", 404)
            db.execute("UPDATE auth_requests SET state='claimed',user_id=?,display_name=? WHERE request_id=?",
                       (int(user_id), str(display_name)[:100], row["request_id"]))
            return {"request_id": row["request_id"], "device_name": row["device_name"],
                    "confirmation_code": row["confirmation_code"]}

    def confirm(self, request_id, user_id, approve=True):
        with self.connection(write=True) as db:
            changed = db.execute("""UPDATE auth_requests SET state=? WHERE request_id=?
                AND user_id=? AND state='claimed' AND expires_at>?""",
                ("approved" if approve else "denied", request_id, int(user_id), int(self.clock()))).rowcount
            if not changed:
                raise MobileError("invalid_request", 404)

    def poll(self, request_id, secret, ip="unknown"):
        self.rate_limit("poll-ip:" + digest(ip), 120)
        if not isinstance(request_id, str) or not isinstance(secret, str):
            raise MobileError("invalid_request", 404)
        if not ID_RE.fullmatch(request_id) or not TOKEN_RE.fullmatch(secret):
            raise MobileError("invalid_request", 404)
        now = int(self.clock())
        with self.connection(write=True) as db:
            row = db.execute("SELECT * FROM auth_requests WHERE request_id=?", (request_id,)).fetchone()
            if not row or not hmac.compare_digest(row["poll_hash"], digest(secret)):
                raise MobileError("invalid_request", 404)
            if row["expires_at"] <= now:
                raise MobileError("expired_request", 410)
            if row["state"] == "denied":
                raise MobileError("access_denied", 403)
            if row["state"] == "consumed":
                raise MobileError("consumed_request", 410)
            if row["state"] != "approved":
                return None
            token = secrets.token_urlsafe(32)
            db.execute("UPDATE auth_requests SET state='consumed' WHERE request_id=?", (request_id,))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,0)",
                       (digest(token), row["user_id"], row["device_name"], row["display_name"] or "",
                        now, now + SESSION_TTL))
            # Bound active devices per account; oldest sessions are revoked.
            db.execute("""UPDATE sessions SET revoked=1 WHERE user_id=? AND token_hash NOT IN
                (SELECT token_hash FROM sessions WHERE user_id=? AND revoked=0 ORDER BY created_at DESC,rowid DESC LIMIT 10)""",
                       (row["user_id"], row["user_id"]))
            return {"access_token": token, "expires_in": SESSION_TTL, "token_type": "Bearer"}

    def authenticate(self, token):
        if not TOKEN_RE.fullmatch(str(token)):
            raise MobileError("unauthorized", 401)
        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE token_hash=? AND revoked=0 AND expires_at>?",
                             (digest(token), int(self.clock()))).fetchone()
        if not row:
            raise MobileError("unauthorized", 401)
        return dict(row)

    def logout(self, token):
        with self.connection(write=True) as db:
            db.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?", (digest(token),))

    def revoke_user(self, user_id):
        with self.connection(write=True) as db:
            count = db.execute("UPDATE sessions SET revoked=1 WHERE user_id=? AND revoked=0", (int(user_id),)).rowcount
            db.execute("UPDATE auth_requests SET state='denied' WHERE user_id=? AND state IN ('claimed','approved')", (int(user_id),))
            return count

    def alias_for(self, user_id, key_id):
        with self.connection(write=True) as db:
            db.execute("INSERT OR IGNORE INTO subscription_aliases VALUES (?,?,?)",
                       (secrets.token_urlsafe(18), int(user_id), int(key_id)))
            return db.execute("SELECT alias FROM subscription_aliases WHERE user_id=? AND key_id=?",
                              (int(user_id), int(key_id))).fetchone()[0]

    def key_for_alias(self, user_id, alias):
        if not ID_RE.fullmatch(alias):
            return None
        with self.connection() as db:
            row = db.execute("SELECT key_id FROM subscription_aliases WHERE user_id=? AND alias=?",
                             (int(user_id), alias)).fetchone()
            return row[0] if row else None


_stores = {}


def get_store():
    path = os.environ.get("MOBILE_APP_AUTH_DB", "/app/data/mobile-auth.sqlite3")
    if path not in _stores:
        _stores[path] = AuthStore(path)
    return _stores[path]


def register_api(app, get_user, get_user_keys, get_key_by_id, config_loader, store=None,
                 renewal_backend=None, support_backend=None):
    """Register routes using existing read helpers and existing delivery adapter."""
    from flask import Blueprint, g, jsonify, make_response, request
    store = store or get_store()
    api = Blueprint("pepel_mobile", __name__, url_prefix=PREFIX)

    @api.errorhandler(MobileError)
    def mobile_error(error):
        response = jsonify(error=error.code)
        response.status_code = error.status
        if error.status == 429:
            response.headers["Retry-After"] = "60"
        return response

    @api.errorhandler(sqlite3.Error)
    def database_error(_error):
        return jsonify(error="temporarily_unavailable"), 503

    @api.after_request
    def no_cache(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.before_request
    def bounded_input():
        if request.content_length and request.content_length > 8192:
            raise MobileError("request_too_large", 413)

    def body():
        if not request.is_json:
            raise MobileError("json_required", 415)
        # The stream limit also handles chunked bodies without Content-Length.
        raw = request.stream.read(8193)
        if len(raw) > 8192:
            raise MobileError("request_too_large", 413)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise MobileError("invalid_json") from None
        if not isinstance(value, dict):
            raise MobileError("invalid_json")
        return value

    def client_ip():
        # Opt in only for observed local proxy addresses. nginx must overwrite
        # X-Real-IP with $remote_addr. Never accept a client-supplied XFF list.
        remote = request.remote_addr or "unknown"
        trusted = {part.strip() for part in os.environ.get("MOBILE_APP_TRUSTED_PROXIES", "").split(",") if part.strip()}
        if remote in trusted:
            try:
                return str(ipaddress.ip_address(request.headers.get("X-Real-IP", "")))
            except ValueError:
                pass
        return remote

    def require_auth(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                raise MobileError("unauthorized", 401)
            g.mobile_token = header[7:]
            g.mobile_session = store.authenticate(g.mobile_token)
            g.mobile_user = get_user(g.mobile_session["user_id"])
            if not g.mobile_user or int(g.mobile_user.get("is_banned") or 0):
                store.logout(g.mobile_token)
                raise MobileError("unauthorized", 401)
            store.rate_limit("api-user:" + str(g.mobile_session["user_id"]), 120)
            return fn(*args, **kwargs)
        return wrapped

    @api.post("/auth/start")
    def start():
        data = body()
        return jsonify(store.start(data.get("device_name"), client_ip()))

    @api.post("/auth/poll")
    def poll():
        data = body()
        result = store.poll(data.get("request_id"), data.get("request_secret"), client_ip())
        return (jsonify(result), 200) if result else (jsonify(status="pending"), 202)

    @api.get("/me")
    @require_auth
    def me():
        return jsonify(telegram_id=g.mobile_session["user_id"],
                       display_name=str(g.mobile_user.get("username") or g.mobile_session["display_name"] or "Клиент PEPELVPN"))

    @api.get("/subscriptions")
    @require_auth
    def subscriptions():
        user_id = g.mobile_session["user_id"]
        result = []
        keys = []
        for key in get_user_keys(user_id):
            if int(key.get("user_id") or 0) != user_id or int(key.get("is_archived") or 0):
                continue
            try:
                expiry_iso(key)
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
            keys.append(key)
        traffic = load_whitelist_traffic(user_id, keys, now=store.clock())
        for key in keys:
            tier = str(key.get("subscription_tier") or "").lower()
            title = {"standard": "PEPELVPN Standard", "super": "PEPELVPN Super"}.get(tier, "PEPELVPN")
            result.append({"id": store.alias_for(user_id, key["key_id"]), "title": title,
                           "expires_at": expiry_iso(key), "active": is_active(key, store.clock()),
                           "whitelist_traffic": traffic.get(int(key["key_id"]))})
        return jsonify(subscriptions=result)

    @api.get("/subscriptions/<alias>/config")
    @require_auth
    def config(alias):
        user_id = g.mobile_session["user_id"]
        key_id = store.key_for_alias(user_id, alias)
        key = get_key_by_id(key_id) if key_id else None
        if not key or int(key.get("user_id") or 0) != user_id or not is_active(key, store.clock()):
            raise MobileError("subscription_not_found", 404)
        response = make_response(config_loader(key))
        if response.status_code != 200:
            # Do not forward upstream errors or hidden URLs to the mobile UI.
            raise MobileError("subscription_unavailable", 503)
        response.headers["Content-Type"] = "text/plain; charset=utf-8"
        response.headers.pop("Set-Cookie", None)
        return response

    @api.post("/auth/logout")
    @require_auth
    def logout():
        store.logout(g.mobile_token)
        return jsonify(status="logged_out")

    if __package__:
        from .mobile_renewal import register_routes as register_renewal_routes
        from .mobile_support import register_routes as register_support_routes
    else:
        from mobile_renewal import register_routes as register_renewal_routes
        from mobile_support import register_routes as register_support_routes
    register_renewal_routes(api, store, require_auth, body, get_key_by_id, MobileError, renewal_backend)
    register_support_routes(api, store, require_auth, body, MobileError, support_backend)
    app.register_blueprint(api)


async def handle_start(message, args, get_user):
    """True means the existing /start handler must return immediately."""
    if not enabled() or not str(args or "").startswith("app_"):
        return False
    import asyncio
    from html import escape
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    if str(message.chat.type) != "private" or not message.from_user:
        return True
    if os.environ.get("BOT_SOURCE", "main") != "reserve":
        await message.answer("Вход в PEPELVPN доступен через @probelvpnbot.")
        return True
    user = await asyncio.to_thread(get_user, message.from_user.id)
    if not user or int(user.get("is_banned") or 0):
        await message.answer("Для входа нужен аккаунт PEPELVPN. Откройте меню бота командой /start, затем повторите вход в приложении.")
        return True
    try:
        store = await asyncio.to_thread(get_store)
        claimed = await asyncio.to_thread(store.claim, args[4:], message.from_user.id, message.from_user.full_name)
    except MobileError:
        await message.answer("Запрос входа недействителен или истёк. Создайте новый запрос в приложении.")
        return True
    except sqlite3.Error:
        await message.answer("Вход временно недоступен. Попробуйте позже.")
        return True
    request_id = claimed["request_id"]
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Подтвердить вход", callback_data="mapp_ok:" + request_id),
        InlineKeyboardButton(text="Отклонить", callback_data="mapp_no:" + request_id),
    ]])
    await message.answer(
        "<b>Вход в приложение PEPELVPN</b>\n\n"
        f"Устройство: {escape(claimed['device_name'])}\nКод: <b>{claimed['confirmation_code']}</b>\n\n"
        "Сверьте код с приложением. Подтверждайте, только если вы сами начали вход на своём Android. "
        "Не подтверждайте ссылки от других людей: это даст им доступ к вашей подписке.",
        parse_mode="HTML", reply_markup=markup)
    return True


def register_bot_handlers(router, get_user):
    import asyncio
    from aiogram import F
    from aiogram.filters import Command

    @router.callback_query(F.data.startswith("mapp_"))
    async def confirm_mobile(callback):
        if not enabled() or os.environ.get("BOT_SOURCE", "main") != "reserve":
            await callback.answer("Вход недоступен", show_alert=True)
            return
        if not callback.message or str(callback.message.chat.type) != "private":
            await callback.answer("Откройте личный чат с ботом", show_alert=True)
            return
        user = await asyncio.to_thread(get_user, callback.from_user.id)
        if not user or int(user.get("is_banned") or 0):
            await callback.answer("Вход недоступен", show_alert=True)
            return
        action, _, request_id = str(callback.data).partition(":")
        if action not in {"mapp_ok", "mapp_no"} or not ID_RE.fullmatch(request_id):
            await callback.answer("Неверный запрос", show_alert=True)
            return
        try:
            store = await asyncio.to_thread(get_store)
            await asyncio.to_thread(store.confirm, request_id, callback.from_user.id, action == "mapp_ok")
        except (MobileError, sqlite3.Error):
            await callback.answer("Запрос истёк или уже использован", show_alert=True)
            return
        await callback.answer("Вход подтверждён" if action == "mapp_ok" else "Вход отклонён")
        await callback.message.edit_text(
            "Вход подтверждён. Вернитесь в приложение PEPELVPN.\n"
            "Чтобы завершить все сеансы приложения, отправьте /app_logout."
            if action == "mapp_ok" else "Вход в приложение отклонён.")

    @router.message(Command("app_logout"))
    async def logout_mobile(message):
        if not enabled() or os.environ.get("BOT_SOURCE", "main") != "reserve":
            return
        if str(message.chat.type) != "private" or not message.from_user:
            return
        try:
            store = await asyncio.to_thread(get_store)
            await asyncio.to_thread(store.revoke_user, message.from_user.id)
        except sqlite3.Error:
            await message.answer("Не удалось завершить сеансы. Повторите позже.")
            return
        await message.answer("Все сеансы приложения PEPELVPN завершены. VPN-ключи и покупки сохранены.")
