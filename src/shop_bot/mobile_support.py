"""Authenticated native support bridge. Chatwoot credentials never reach clients.

The separate API inbox is opt-in. GET never creates contacts/conversations or
sends messages. Durable send intents prevent duplicate messages after timeouts.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

ORIGIN = "https://chatwoot.pepelbot.com"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I)
MAX_TEXT = 1000
LEASE_SECONDS = 120


class SupportUnavailable(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ChatwootBackend:
    def __init__(self, settings=None):
        if settings is None:
            from shop_bot.data_manager.database import get_setting
            settings = get_setting
        self.origin = str(settings("chatwoot_api_url") or "").rstrip("/")
        self.token = str(settings("chatwoot_api_token") or "")
        try:
            self.account = int(settings("chatwoot_account_id") or 0)
            self.inbox = int(settings("mobile_support_inbox_id") or 0)
        except (TypeError, ValueError):
            self.account = self.inbox = 0
        self.available = self.origin == ORIGIN and bool(self.token) and self.account > 0 and self.inbox > 0
        self.opener = build_opener(NoRedirect())

    def call(self, path, body=None):
        if not self.available or not path.startswith("/"):
            raise SupportUnavailable()
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(f"{self.origin}/api/v1/accounts/{self.account}{path}", data=payload,
                          headers={"api_access_token": self.token, "Accept": "application/json",
                                   "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise SupportUnavailable()
            return json.loads(raw)
        except (HTTPError, URLError, OSError, ValueError, RecursionError):
            # No upstream response bodies, headers or credentials in API errors.
            raise SupportUnavailable() from None

    def check_inbox(self):
        inbox = self.call(f"/inboxes/{self.inbox}")
        if inbox.get("channel_type") != "Channel::Api" or not inbox.get("lock_to_single_conversation"):
            raise SupportUnavailable()

    def find_contact(self, identifier):
        contacts = self.call("/contacts/search?" + urlencode({"q": identifier})).get("payload", [])
        matches = [c for c in contacts if c.get("identifier") == identifier]
        if len(matches) > 1:
            raise SupportUnavailable()
        return matches[0] if matches else None

    def create_contact(self, identifier, user_id, name):
        value = self.call("/contacts", {"identifier": identifier, "name": name[:100],
            "inbox_id": self.inbox, "source_id": identifier,
            "additional_attributes": {"pepel_mobile_telegram_id": str(user_id), "pepel_source": "android"}})
        return value.get("payload", {}).get("contact")

    def find_conversation(self, contact_id):
        value = self.call(f"/contacts/{contact_id}/conversations").get("payload", [])
        rows = [c for c in value if c.get("inbox_id") == self.inbox]
        if len(rows) > 1:
            # This channel is single-conversation; an operator must reconcile
            # a changed/merged mapping instead of guessing a customer's thread.
            raise SupportUnavailable()
        return rows[0] if rows else None

    def create_conversation(self, contact_id, identifier):
        return self.call("/conversations", {"inbox_id": self.inbox, "contact_id": contact_id,
            "source_id": identifier, "status": "open", "additional_attributes": {"pepel_source": "android"}})

    def conversation(self, conversation_id):
        return self.call(f"/conversations/{conversation_id}")

    def messages(self, conversation_id, before=None, after=None):
        query = {key: value for key, value in (("before", before), ("after", after)) if value is not None}
        suffix = "?" + urlencode(query) if query else ""
        return self.call(f"/conversations/{conversation_id}/messages" + suffix).get("payload", [])

    def send(self, conversation_id, client_id, text):
        return self.call(f"/conversations/{conversation_id}/messages", {
            "content": text, "message_type": "incoming", "private": False,
            "content_type": "text", "content_attributes": {"pepel_client_message_id": client_id},
            "source_id": "pepel-app:" + client_id})


def public_message(message):
    if message.get("private") is not False or message.get("message_type") not in (0, 1):
        return None
    try:
        message_id = int(message["id"])
        created = datetime.fromtimestamp(float(message["created_at"]), timezone.utc).isoformat().replace("+00:00", "Z")
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        return None
    if message_id <= 0:
        return None
    attributes = message.get("content_attributes") or {}
    client_id = attributes.get("pepel_client_message_id")
    if not isinstance(client_id, str) or not UUID_RE.fullmatch(client_id) or message["message_type"] != 0:
        client_id = None
    text = str(message.get("content") or "")[:20000]
    attachments = []
    for item in (message.get("attachments") or [])[:10]:
        url = str(item.get("data_url") or "")
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme == "https" and parsed.netloc == "chatwoot.pepelbot.com" and parsed.path.startswith("/rails/active_storage/"):
            attachments.append({"name": str(item.get("file_name") or "Вложение")[:150], "url": url,
                                "type": str(item.get("file_type") or "file")[:30]})
        elif item:
            text += "\n[Вложение недоступно в приложении]"
    if attributes.get("deleted"):
        text, attachments = "Сообщение удалено", []
    if not text.strip() and not attachments:
        return None
    return {"id": str(message_id), "client_message_id": client_id,
            "role": "customer" if message["message_type"] == 0 else "support",
            "text": text, "created_at": created, "attachments": attachments}


class SupportService:
    def __init__(self, store, error, backend=None):
        self.store, self.error = store, error
        self.backend = backend
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mobile_support_threads (
                    user_id INTEGER NOT NULL, inbox_id INTEGER NOT NULL,
                    contact_id INTEGER, conversation_id INTEGER,
                    stage TEXT NOT NULL DEFAULT 'new', lease_until INTEGER NOT NULL DEFAULT 0, lease_owner TEXT,
                    PRIMARY KEY(user_id, inbox_id)
                );
                CREATE TABLE IF NOT EXISTS mobile_support_sends (
                    user_id INTEGER NOT NULL, client_id TEXT NOT NULL, text_hash TEXT NOT NULL,
                    inbox_id INTEGER NOT NULL, conversation_id INTEGER NOT NULL,
                    after_id INTEGER NOT NULL, state TEXT NOT NULL, message_json TEXT,
                    created_at INTEGER NOT NULL, PRIMARY KEY(user_id, client_id)
                );
            """)

    def provider(self):
        return self.backend if self.backend is not None else ChatwootBackend()

    def thread(self, user_id, inbox):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM mobile_support_threads WHERE user_id=? AND inbox_id=?", (user_id, inbox)).fetchone()
        return dict(row) if row else None

    def validate(self, backend, row):
        value = backend.conversation(row["conversation_id"])
        sender = (value.get("meta") or {}).get("sender") or {}
        # Chatwoot's conversation serializer supplies the contact in meta.sender.
        if value.get("inbox_id") != backend.inbox or sender.get("id") != row["contact_id"]:
            raise SupportUnavailable()
        return value

    def ensure_thread(self, backend, user_id, name):
        row = self.thread(user_id, backend.inbox)
        if row and row["conversation_id"]:
            self.validate(backend, row)
            return row
        now = int(self.store.clock())
        owner = secrets.token_urlsafe(24)
        with self.store.connection() as db:
            db.execute("INSERT OR IGNORE INTO mobile_support_threads(user_id,inbox_id) VALUES(?,?)", (user_id, backend.inbox))
            updated = db.execute("UPDATE mobile_support_threads SET lease_until=?,lease_owner=? WHERE user_id=? AND inbox_id=? AND lease_until<=?",
                                 (now + LEASE_SECONDS, owner, user_id, backend.inbox, now)).rowcount
        if not updated:
            raise SupportUnavailable()
        row = self.thread(user_id, backend.inbox)
        identifier = f"pepel-app:{user_id}"
        try:
            backend.check_inbox()
            contact = backend.find_contact(identifier)
            if contact is None:
                if row["stage"] != "new":
                    raise SupportUnavailable()
                self._stage(user_id, backend.inbox, owner, "contact_started")
                contact = backend.create_contact(identifier, user_id, name)
            if not contact or contact.get("identifier") != identifier or type(contact.get("id")) is not int:
                raise SupportUnavailable()
            contact_id = contact["id"]
            if row["contact_id"] and row["contact_id"] != contact_id:
                raise SupportUnavailable()
            if row["stage"] not in ("conversation_started", "ready"):
                self._stage(user_id, backend.inbox, owner, "contact_ready", contact_id=contact_id)
            conversation = backend.find_conversation(contact_id)
            if conversation is None:
                if row["stage"] == "conversation_started":
                    raise SupportUnavailable()
                self._stage(user_id, backend.inbox, owner, "conversation_started", contact_id=contact_id)
                conversation = backend.create_conversation(contact_id, identifier)
            conversation_id = conversation.get("id") if conversation else None
            if type(conversation_id) is not int or conversation_id <= 0:
                raise SupportUnavailable()
            candidate = {"contact_id": contact_id, "conversation_id": conversation_id}
            self.validate(backend, candidate)
            with self.store.connection() as db:
                changed = db.execute("UPDATE mobile_support_threads SET contact_id=?,conversation_id=?,stage='ready',lease_until=0,lease_owner=NULL WHERE user_id=? AND inbox_id=? AND lease_owner=? AND lease_until>?",
                           (contact_id, conversation_id, user_id, backend.inbox, owner, int(self.store.clock()))).rowcount
                if not changed:
                    raise SupportUnavailable()
            return self.thread(user_id, backend.inbox)
        finally:
            # A failed remote mutation keeps its durable stage; future retries
            # perform lookup only, never blindly duplicate contact/conversation.
            with self.store.connection() as db:
                db.execute("UPDATE mobile_support_threads SET lease_until=0,lease_owner=NULL WHERE user_id=? AND inbox_id=? AND lease_owner=?", (user_id, backend.inbox, owner))

    def _stage(self, user_id, inbox, owner, stage, contact_id=None):
        with self.store.connection() as db:
            changed = db.execute("UPDATE mobile_support_threads SET stage=?,contact_id=COALESCE(?,contact_id) WHERE user_id=? AND inbox_id=? AND lease_owner=? AND lease_until>?",
                       (stage, contact_id, user_id, inbox, owner, int(self.store.clock()))).rowcount
            if not changed:
                raise SupportUnavailable()

    def _confirmed(self, user_id, message):
        value = public_message(message)
        if value and value["client_message_id"]:
            text_hash = hashlib.sha256(str(message.get("content") or "").encode()).hexdigest()
            with self.store.connection() as db:
                db.execute("UPDATE mobile_support_sends SET state='sent',message_json=? WHERE user_id=? AND client_id=? AND text_hash=?",
                           (json.dumps(value, ensure_ascii=False), user_id, value["client_message_id"], text_hash))
        return value

    def history(self, user_id, before=None):
        backend = self.provider()
        if not backend.available:
            return {"available": False, "reason": "support_not_configured", "conversation": None,
                    "messages": [], "has_more": False, "next_before": None}
        row = self.thread(user_id, backend.inbox)
        if not row or not row["conversation_id"]:
            return {"available": True, "conversation": None, "messages": [], "has_more": False, "next_before": None}
        conversation = self.validate(backend, row)
        messages = backend.messages(row["conversation_id"], before=before)
        if not isinstance(messages, list):
            raise SupportUnavailable()
        result = [value for item in messages if (value := self._confirmed(user_id, item)) is not None]
        ids = [int(m["id"]) for m in messages if type(m.get("id")) is int and m["id"] > 0]
        return {"available": True, "conversation": {"status": str(conversation.get("status") or "open")},
                "messages": sorted(result, key=lambda m: int(m["id"])), "has_more": len(messages) >= 20 and bool(ids),
                "next_before": str(min(ids)) if len(messages) >= 20 and ids else None}

    def send(self, user_id, name, data):
        client_id, text = data.get("client_message_id"), data.get("text")
        if not isinstance(client_id, str) or not UUID_RE.fullmatch(client_id):
            raise self.error("invalid_message_id")
        client_id = client_id.lower()
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or "\x00" in text:
            raise self.error("invalid_message_text")
        try:
            text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        except UnicodeEncodeError:
            raise self.error("invalid_message_text") from None
        backend = self.provider()
        if not backend.available:
            raise self.error("support_not_configured", 503)
        with self.store.connection() as db:
            intent = db.execute("SELECT * FROM mobile_support_sends WHERE user_id=? AND client_id=?", (user_id, client_id)).fetchone()
        if intent:
            if intent["text_hash"] != text_hash:
                raise self.error("idempotency_conflict", 409)
            if intent["state"] == "sent":
                return {"status": "sent", "message": json.loads(intent["message_json"])}
            row = self.thread(user_id, intent["inbox_id"])
            if not row or backend.inbox != intent["inbox_id"] or row["conversation_id"] != intent["conversation_id"]:
                raise SupportUnavailable()
            self.validate(backend, row)
            after = intent["after_id"]
            for _ in range(10):
                page = backend.messages(row["conversation_id"], after=after)
                for item in page:
                    value = self._confirmed(user_id, item)
                    if value and value["client_message_id"] == client_id and value["text"] == text:
                        return {"status": "sent", "message": value}
                if len(page) < 100:
                    break
                ids = [m["id"] for m in page if type(m.get("id")) is int]
                if not ids or max(ids) <= after:
                    break
                after = max(ids)
            return {"status": "pending", "message": None}
        row = self.ensure_thread(backend, user_id, name)
        recent = backend.messages(row["conversation_id"])
        after_id = max([m["id"] for m in recent if type(m.get("id")) is int] or [0])
        with self.store.connection() as db:
            inserted = db.execute("INSERT OR IGNORE INTO mobile_support_sends(user_id,client_id,text_hash,inbox_id,conversation_id,after_id,state,created_at) VALUES(?,?,?,?,?,?,'sending',?)",
                                  (user_id, client_id, text_hash, backend.inbox, row["conversation_id"], after_id, int(self.store.clock()))).rowcount
        if not inserted:
            return self.send(user_id, name, data)
        try:
            message = backend.send(row["conversation_id"], client_id, text)
            value = self._confirmed(user_id, message)
            if value and value["client_message_id"] == client_id and value["text"] == text:
                return {"status": "sent", "message": value}
        except SupportUnavailable:
            pass
        return {"status": "pending", "message": None}


def register_routes(api, store, require_auth, body, error, backend=None):
    from flask import g, jsonify, request
    service = SupportService(store, error, backend)

    @api.get("/support")
    @require_auth
    def mobile_support_history():
        before = request.args.get("before")
        if before is not None and (len(before) > 10 or not before.isascii() or not before.isdigit() or not 0 < int(before) <= 2147483647):
            raise error("invalid_cursor")
        try:
            return jsonify(service.history(g.mobile_session["user_id"], int(before) if before else None))
        except SupportUnavailable:
            raise error("support_unavailable", 503) from None

    @api.post("/support/messages")
    @require_auth
    def mobile_support_send():
        user_id = g.mobile_session["user_id"]
        store.rate_limit("support-send:" + str(user_id), 20)
        try:
            result = service.send(user_id, str(g.mobile_user.get("username") or g.mobile_session.get("display_name") or "Клиент ПЕПЕЛ ВПН"), body())
            return jsonify(result), 202 if result["status"] == "pending" else 200
        except SupportUnavailable:
            raise error("support_unavailable", 503) from None
    return service
