"""Read-only LAVA profile payout lookup. No payout creation capability."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

INFO_URL = "https://api.lava.ru/business/payoff/info"
MAX_RESPONSE_BYTES = 262144
STATUSES = frozenset({"created", "pending", "success", "rejected"})


class PayoutAPIError(ValueError):
    """Safe diagnostic code; never retains provider body, signature or wallet."""
    def __init__(self, code, *, http_status=None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _uuid(value):
    if not isinstance(value, str):
        raise PayoutAPIError("invalid_identifier")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise PayoutAPIError("invalid_identifier") from None


def _order(value):
    if not isinstance(value, str) or not value or len(value) > 50:
        raise PayoutAPIError("invalid_order")
    if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise PayoutAPIError("invalid_order")
    return value


def _alias(data, *keys, optional=False):
    values = [data[key] for key in keys if key in data]
    if not values:
        if optional:
            return None
        raise PayoutAPIError("missing_field")
    if any(value != values[0] for value in values[1:]):
        raise PayoutAPIError("conflicting_fields")
    return values[0]


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        raise PayoutAPIError("invalid_amount")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0 or amount > Decimal(1000000000000):
            raise PayoutAPIError("invalid_amount")
        numerator, denominator = amount.as_integer_ratio()
        if (numerator * 100) % denominator:
            raise PayoutAPIError("invalid_amount")
        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise PayoutAPIError("invalid_amount") from None


def _json_bytes(data):
    # Matches the existing working v2 client / official SDK for the only
    # permitted request fields (ASCII UUID, merchant order ID, signature).
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def fetch_info(profile_id, secret, payoff_id=None, order_id=None, *, opener=None, timeout=12):
    """Read one known payout and return validated amounts as RUB strings.

    `amount_pay` is the profile debit including commission, as confirmed by
    the existing authenticated TRC20 lookup and LAVA's profile payout screen.
    No provider debit timestamp is documented: `debit_at` is deliberately None.
    """
    profile_id = _uuid(profile_id)
    payoff_id = _uuid(payoff_id) if payoff_id is not None else None
    order_id = _order(order_id) if order_id is not None else None
    if not payoff_id and not order_id:
        raise PayoutAPIError("missing_identifier")
    if not isinstance(secret, str) or not secret.strip():
        raise PayoutAPIError("missing_key")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 20:
        raise PayoutAPIError("invalid_timeout")
    payload = {"profileId": profile_id}
    if payoff_id is not None:
        payload["payoffId"] = payoff_id
    if order_id is not None:
        payload["orderId"] = order_id
    unsigned = _json_bytes(dict(sorted(payload.items())))
    signature = hmac.new(secret.encode(), unsigned, hashlib.sha256).hexdigest()
    body = _json_bytes({**payload, "signature": signature})
    request = urllib.request.Request(INFO_URL, data=body, method="POST", headers={
        "Accept": "application/json", "Content-Type": "application/json",
    })
    try:
        client = opener if opener is not None else urllib.request.build_opener(NoRedirect())
        with client.open(request, timeout=timeout) as response:
            if response.status != 200 or response.geturl() != INFO_URL:
                raise PayoutAPIError("unexpected_http_response")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise PayoutAPIError("response_too_large")
        envelope = json.loads(raw, parse_float=Decimal, object_pairs_hook=_object)
    except urllib.error.HTTPError as exc:
        raise PayoutAPIError("http_error", http_status=exc.code) from None
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        raise PayoutAPIError("network_error") from None
    except (ValueError, UnicodeDecodeError) as exc:
        if isinstance(exc, PayoutAPIError):
            raise
        raise PayoutAPIError("invalid_json") from None
    if (not isinstance(envelope, dict) or envelope.get("status") != 200
            or envelope.get("status_check") is not True
            or not isinstance(envelope.get("data"), dict)):
        raise PayoutAPIError("invalid_envelope")
    data = envelope["data"]
    actual_id = _uuid(_alias(data, "id", "payoff_id", "payoffId"))
    actual_order = _alias(data, "order_id", "orderId", optional=True)
    if actual_order is not None:
        actual_order = _order(actual_order)
    if (payoff_id and actual_id != payoff_id) or (order_id and actual_order != order_id):
        raise PayoutAPIError("identifier_mismatch")
    actual_profile = _alias(data, "profile_id", "profileId", optional=True)
    if actual_profile is not None and _uuid(actual_profile) != profile_id:
        raise PayoutAPIError("profile_mismatch")
    status = _alias(data, "status", "payoff_status")
    if not isinstance(status, str) or status not in STATUSES:
        raise PayoutAPIError("unknown_status")
    if data.get("currency", "RUB") != "RUB":
        raise PayoutAPIError("unexpected_currency")
    debit = _money(_alias(data, "amount_pay", "amountPay"))
    fee = _money(_alias(data, "commission"))
    received = _money(_alias(data, "amount_receive", "amountReceive"))
    if debit <= 0 or fee + received != debit:
        raise PayoutAPIError("inconsistent_amounts")
    service = _alias(data, "service")
    if not isinstance(service, str) or not service or len(service) > 64:
        raise PayoutAPIError("invalid_service")
    return {
        "payoff_id": actual_id, "order_id": actual_order,
        "status": status, "provider_status": status,
        "debit_rub": format(debit, ".2f"), "fee_rub": format(fee, ".2f"),
        "received_rub": format(received, ".2f"), "currency": "RUB",
        "service": service, "debit_at": None,
        "verified_at": datetime.now(UTC).isoformat(),
    }
