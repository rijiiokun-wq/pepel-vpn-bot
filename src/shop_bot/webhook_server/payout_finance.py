"""Accounting-only signed LAVA profile payout notification queue."""
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from flask import request
from shop_bot.data_manager import finance_payouts
from shop_bot.modules.lava_api import verify_webhook_request

ENDPOINT = "/webhooks/lava/payout-finance"
MAX_EVENT_BYTES = 65536


class ProfileMismatch(ValueError):
    pass


def _object(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("Duplicate key")
        data[key] = value
    return data


def _identifier(data, names):
    values = [data[k] for k in names if k in data and data[k] is not None]
    if any(value != values[0] for value in values[1:]):
        raise ValueError("Conflicting identifiers")
    return values[0] if values else None


def _uuid(value):
    if not isinstance(value, str):
        raise TypeError("UUID expected")
    return str(uuid.UUID(value))


def read_configuration(history_path):
    path = Path(history_path).with_name("payout-webhook-secret.json")
    if path.stat().st_size > 4096:
        raise ValueError("Invalid configuration")
    config = json.loads(path.read_text(), object_pairs_hook=_object)
    if not isinstance(config, dict):
        raise TypeError("Invalid configuration")
    declared = config.get("profile_id")
    env_ids = [value for value in (
        os.environ.get("LAVA_FINANCE_PROFILE_ID", "").strip(),
        os.environ.get("LAVA_PROFILE_ID", "").strip(),
    ) if value]
    if declared is not None:
        env_ids.append(declared)
    ids = [_uuid(value) for value in env_ids]
    if not ids or len(set(ids)) != 1:
        raise ValueError("Invalid profile configuration")
    secret = config.get("additional_key")
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("Missing profile additional key")
    return {"profile_id": ids[0], "additional_key": secret}


def capture(data, history_path, profile_id, *, payload_digest, observed_at=None):
    """Caller has verified the profile signature; enqueue IDs only."""
    profile = _identifier(data, ("profile_id", "profileId"))
    if profile is not None:
        try:
            if _uuid(profile) != profile_id:
                raise ProfileMismatch("Wrong profile")
        except (ValueError, TypeError):
            raise ProfileMismatch("Wrong profile") from None
    payoff_id = _identifier(data, ("payoff_id", "payoffId"))
    order_id = _identifier(data, ("order_id", "orderId"))
    if payoff_id is not None:
        payoff_id = _uuid(payoff_id)
    if order_id is not None and (not isinstance(order_id, str) or not order_id or len(order_id) > 50
            or order_id != order_id.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in order_id)):
        raise ValueError("Invalid order")
    if not payoff_id and not order_id:
        raise ValueError("Missing payout identifier")
    # A generic `id`, payment invoice_id, amounts and notification timestamps
    # are deliberately not treated as authoritative payout/accounting data.
    received = observed_at or datetime.now(UTC).isoformat()
    finance_payouts.enqueue(history_path, payoff_id, order_id, payload_digest, received)


def register_payout_finance(flask_app, *, history_path, secret_reader=None):
    def receive():
        if request.content_length is not None and request.content_length > MAX_EVENT_BYTES:
            return "Too large", 413
        raw = request.stream.read(MAX_EVENT_BYTES + 1)
        if not raw or len(raw) > MAX_EVENT_BYTES:
            return "Bad request", 400
        path = history_path()
        if not path:
            return "Not configured", 503
        try:
            config = (secret_reader or read_configuration)(path)
            profile_id = _uuid(config["profile_id"])
            secret = config["additional_key"]
            if not isinstance(secret, str) or not secret.strip():
                raise ValueError("Missing key")
        except (OSError, ValueError, TypeError, KeyError):
            return "Not configured", 503
        # Reject contradictory signature headers rather than silently falling
        # back from a deliberately incorrect current header to a legacy one.
        if request.headers.get("Signature") and request.headers.get("Authorization"):
            current = verify_webhook_request(raw, {"Signature": request.headers["Signature"]}, secret=secret)
            legacy = verify_webhook_request(raw, {"Authorization": request.headers["Authorization"]}, secret=secret, allow_legacy=True)
            valid = current and legacy
        else:
            valid = verify_webhook_request(raw, request.headers, secret=secret, allow_legacy=True)
        if not valid:
            return "Forbidden", 403
        digest = hashlib.sha256(raw).hexdigest()
        observed_at = datetime.now(UTC).isoformat()
        try:
            data = json.loads(raw, object_pairs_hook=_object)
            if not isinstance(data, dict):
                raise TypeError("Object expected")
            capture(data, path, profile_id, payload_digest=digest, observed_at=observed_at)
        except ProfileMismatch:
            return "Invalid event", 400
        except (ValueError, TypeError, KeyError):
            # An authentic but unsupported notification may represent a real
            # debit. Preserve only its digest and require reconciliation.
            try:
                finance_payouts.record_unrecognized(path, digest, observed_at)
            except (OSError, sqlite3.Error):
                return "Retry later", 503
            return "Invalid event", 400
        except (OSError, sqlite3.Error):
            return "Retry later", 503
        return "OK", 200

    flask_app.add_url_rule(ENDPOINT, "payout_finance_webhook", receive, methods=["POST"])
