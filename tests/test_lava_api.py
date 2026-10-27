from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from shop_bot.modules import lava_api


@pytest.fixture(autouse=True)
def prevent_http_requests(monkeypatch):
    def unexpected_session(*args, **kwargs):
        pytest.fail("Lava regression tests must not open a live HTTP session")

    monkeypatch.setattr(lava_api.aiohttp, "ClientSession", unexpected_session)


@pytest.fixture
def lava_client():
    return lava_api.LavaClient(
        lava_api.LavaConfig(
            shop_id=str(uuid.UUID(int=1)),
            secret_key="offline-api-signing",
            webhook_secret="offline-webhook-signing",
            api_base_url="https://api.example.test",
        )
    )


@pytest.fixture
def mocked_http(monkeypatch, prevent_http_requests, lava_client):
    response = MagicMock()
    response.status = 200
    response.text = AsyncMock(
        return_value=json.dumps(
            {
                "status_check": True,
                "data": {
                    "id": str(uuid.UUID(int=2)),
                    "shop_id": lava_client.config.shop_id,
                    "amount": "125.50",
                    "url": "https://payments.example.test/invoice/offline",
                },
            }
        )
    )
    response.__aenter__.return_value = response
    session = MagicMock()
    session.__aenter__.return_value = session
    session.post.return_value = response
    monkeypatch.setattr(lava_api.aiohttp, "ClientSession", lambda **kwargs: session)
    return session, response


def test_sign_raw_body_matches_rfc4231_hmac_sha256_vector():
    # RFC 4231 test case 2 is an independent, public signing oracle.
    expected = "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    assert lava_api.sign_raw_body(b"what do ya want for nothing?", "Jefe") == expected


def test_current_webhook_signature_authenticates_exact_wire_bytes():
    raw_body = '{"amount":150,"description":"Офлайн","order_id":"offline-order"}'.encode()
    signature = hmac.digest(b"offline-webhook-signing", raw_body, "sha256").hex()
    headers = {"sIgNaTuRe": "SHA256=" + signature.upper()}

    assert lava_api.verify_webhook_request(raw_body, headers, secret="offline-webhook-signing")
    assert not lava_api.verify_webhook_request(raw_body + b" ", headers, secret="offline-webhook-signing")
    assert not lava_api.verify_webhook_request(
        raw_body.replace(b"150", b"151"), headers, secret="offline-webhook-signing"
    )
    assert not lava_api.verify_webhook_request(raw_body, headers, secret="offline-wrong-signing")
    assert not lava_api.verify_webhook_request(
        raw_body, {"Signature": "sha256=invalid"}, secret="offline-webhook-signing"
    )


def test_legacy_webhook_canonicalization_requires_explicit_opt_in():
    raw_body = b'{ "order_id": "offline-order", "metadata": {"z":1,"a":2}, "amount": 150 }'
    canonical_body = b'{"amount":150,"metadata":{"z":1,"a":2},"order_id":"offline-order"}'
    signature = hmac.digest(b"offline-webhook-signing", canonical_body, "sha256").hex()
    headers = {"Authorization": signature}

    assert not lava_api.verify_webhook_request(raw_body, headers, secret="offline-webhook-signing")
    assert lava_api.verify_webhook_request(raw_body, headers, allow_legacy=True, secret="offline-webhook-signing")
    assert not lava_api.verify_webhook_request(
        raw_body.replace(b'{"z":1,"a":2}', b'{"a":2,"z":1}'),
        headers,
        allow_legacy=True,
        secret="offline-webhook-signing",
    )


@pytest.mark.parametrize(
    ("amount", "reason"),
    [(True, "monetary number"), ("NaN", "finite"), ("10.001", "two decimal places")],
)
def test_create_invoice_rejects_invalid_money_before_http(lava_client, amount, reason):
    with pytest.raises(lava_api.LavaValidationError, match=reason):
        asyncio.run(lava_client.create_invoice(amount=amount, order_id="offline-order"))


def test_create_invoice_sends_the_exact_bytes_it_signs(lava_client, mocked_http):
    session, _response = mocked_http
    metadata = {"label": "Офлайн", "number": 2}

    result = asyncio.run(
        lava_client.create_invoice(amount=Decimal("125.50"), order_id="offline-order", custom_fields=metadata)
    )

    session.post.assert_called_once()
    request = session.post.call_args.kwargs
    raw_body = request["data"]
    assert isinstance(raw_body, bytes)
    assert request["headers"]["Signature"] == hmac.digest(b"offline-api-signing", raw_body, "sha256").hex()
    assert request["headers"]["Content-Type"] == "application/json"
    payload = json.loads(raw_body)
    assert payload["sum"] == 125.5
    assert payload["orderId"] == "offline-order"
    assert payload["shopId"] == lava_client.config.shop_id
    assert json.loads(payload["customFields"]) == metadata
    assert "Офлайн".encode() in raw_body
    assert result["payment_id"] == str(uuid.UUID(int=2))
    assert result["order_id"] == "offline-order"


def test_create_invoice_rejects_payment_from_another_shop(lava_client, mocked_http):
    session, response = mocked_http
    envelope = json.loads(response.text.return_value)
    envelope["data"]["shop_id"] = str(uuid.UUID(int=3))
    response.text.return_value = json.dumps(envelope)

    with pytest.raises(lava_api.LavaProtocolError, match="shop_id"):
        asyncio.run(lava_client.create_invoice(amount=Decimal("125.50"), order_id="offline-order"))

    session.post.assert_called_once()
