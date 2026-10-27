"""Read-only profile balance; never creates invoices, payouts or refunds."""

import copy
import hashlib
import hmac
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

_LOCK = threading.Lock()
_CACHE = {}
_MSK = timezone(timedelta(hours=3))
_URL = "https://api.lava.ru/business/profile/balance"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_balance(profile_id, secret, *, opener=None):
    """Follow the official SDK GET contract; never expose the signed URL."""
    payload = {"profileId": str(uuid.UUID(profile_id))}
    body = json.dumps(payload, separators=(",", ":")).encode()
    payload["signature"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        _URL + "?" + urllib.parse.urlencode(payload),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    client = opener or urllib.request.build_opener(_NoRedirect())
    with client.open(request, timeout=5) as response:
        raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("Invalid balance response")
        envelope = json.loads(raw)
    if not isinstance(envelope, dict) or envelope.get("status") != 200 or envelope.get("status_check") is not True:
        raise ValueError("Balance request failed")
    data = envelope.get("data") or {}
    if not isinstance(data, dict):
        raise TypeError("Invalid balance data")
    amounts = {}
    for name in ("balance", "active_balance", "freeze_balance"):
        value = data.get(name)
        if value is None or isinstance(value, bool):
            raise ValueError("Balance field missing")
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal("0.01")):
            raise ValueError("Invalid balance value")
        amounts[name] = format(amount, ".2f")
    if Decimal(amounts["balance"]) != Decimal(amounts["active_balance"]) + Decimal(amounts["freeze_balance"]):
        raise ValueError("Balance components do not reconcile")
    return amounts


def get_profile_balance(*, environ=None, fetcher=None, monotonic=None):
    """Cache per credential, retain clearly stale values for at most 15 minutes."""
    env = os.environ if environ is None else environ
    profile_id = str(env.get("LAVA_FINANCE_PROFILE_ID") or "").strip()
    secret = str(env.get("LAVA_FINANCE_PROFILE_SECRET_KEY") or "").strip()
    empty = {"configured": False, "status": "not_configured", "stale": False,
             "balance": None, "active_balance": None, "freeze_balance": None,
             "checked_at": None}
    if not profile_id or not secret:
        return empty
    clock = monotonic or time.monotonic
    fetch = fetcher or _fetch_balance
    identity = hashlib.sha256((profile_id + "\0" + secret).encode()).hexdigest()
    with _LOCK:
        now = clock()
        cached = _CACHE.get(identity)
        if cached and now < cached["refresh_after"]:
            return copy.deepcopy(cached["view"])
        try:
            data = fetch(profile_id, secret)
            view = {**empty, **data, "configured": True, "status": "ok",
                    "checked_at": datetime.now(_MSK).strftime("%d.%m.%Y %H:%M:%S МСК")}
            entry = {"view": view, "success_at": now, "refresh_after": now + 60}
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, TypeError, ValueError, InvalidOperation):
            if cached and cached.get("success_at") is not None and now - cached["success_at"] <= 900:
                view = {**cached["view"], "status": "unavailable", "stale": True}
                entry = {**cached, "view": view, "refresh_after": now + 15}
            else:
                view = {**empty, "configured": True, "status": "unavailable"}
                entry = {"view": view, "success_at": None, "refresh_after": now + 15}
        # Old credentials and their cached balances never remain accessible.
        _CACHE.clear()
        _CACHE[identity] = entry
        return copy.deepcopy(view)
