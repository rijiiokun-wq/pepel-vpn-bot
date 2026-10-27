import asyncio
import hashlib
import hmac

from shop_bot.modules import platega_api


def _signature(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def test_verify_webhook_signature_accepts_plain_hex():
    payload = b'{"payment_id":"abc","status":"succeeded"}'
    secret = "webhook-secret"
    signature = _signature(payload, secret)

    assert platega_api.verify_webhook_signature(payload, signature, secret) is True


def test_verify_webhook_signature_accepts_sha256_prefix():
    payload = b'{"payment_id":"abc","status":"succeeded"}'
    secret = "webhook-secret"
    signature = f"sha256={_signature(payload, secret)}"

    assert platega_api.verify_webhook_signature(payload, signature, secret) is True


def test_verify_webhook_signature_rejects_invalid_signature():
    payload = b'{"payment_id":"abc","status":"succeeded"}'
    secret = "webhook-secret"

    assert platega_api.verify_webhook_signature(payload, "deadbeef", secret) is False


def test_map_platega_status_normalization():
    assert platega_api.map_platega_status("succeeded") == "succeeded"
    assert platega_api.map_platega_status("FAILED") == "canceled"
    assert platega_api.map_platega_status("in_progress") == "pending"


def test_create_payment_reuses_cached_checkout(monkeypatch):
    class FakeService:
        calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def create_payment(self, **_kwargs):
            FakeService.calls += 1
            return {
                "payment_id": "txn_123",
                "payment_url": "https://pay.example/txn_123",
            }

    monkeypatch.setattr(platega_api, "PlategaService", FakeService)
    platega_api._CHECKOUT_CACHE.clear()

    first = asyncio.run(
        platega_api.create_payment(
            amount_rub=199.0,
            order_id="order-1",
            description="Test payment",
            metadata={"checkout_ttl_seconds": 300},
        )
    )
    second = asyncio.run(
        platega_api.create_payment(
            amount_rub=199.0,
            order_id="order-1",
            description="Test payment",
            metadata={"checkout_ttl_seconds": 300},
        )
    )

    assert FakeService.calls == 1
    assert first == second


def test_create_payment_retries_with_fallback_method_on_wrong_input(monkeypatch):
    used_methods: list[int] = []

    class FakeService:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def create_payment(self, **kwargs):
            method = int(kwargs["payment_method"])
            used_methods.append(method)
            if method == 10:
                raise platega_api.PlategaAPIError(400, "Error 400: Wrong input parameters")
            return {
                "payment_id": "txn_fallback",
                "payment_url": "https://pay.example/txn_fallback",
            }

    monkeypatch.setattr(platega_api, "PlategaService", FakeService)
    platega_api._CHECKOUT_CACHE.clear()

    result = asyncio.run(
        platega_api.create_payment(
            amount_rub=100.0,
            order_id="order-fallback",
            description="Fallback test",
            metadata={
                "platega_payment_method": 10,
                "platega_payment_method_fallbacks": [2],
            },
        )
    )

    assert result["payment_id"] == "txn_fallback"
    assert used_methods == [10, 2]


def test_create_payment_card_variant_prefers_cardru_method_before_configured_legacy(monkeypatch):
    used_methods: list[int] = []

    class FakeService:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def create_payment(self, **kwargs):
            method = int(kwargs["payment_method"])
            used_methods.append(method)
            return {
                "payment_id": "txn_card_fallback",
                "payment_url": "https://pay.example/txn_card_fallback",
            }

    monkeypatch.setattr(platega_api, "PlategaService", FakeService)
    platega_api._CHECKOUT_CACHE.clear()

    result = asyncio.run(
        platega_api.create_payment(
            amount_rub=100.0,
            order_id="order-card-compat",
            description="Card compatibility fallback",
            metadata={
                "platega_payment_method": 11,
                "platega_payment_variant": "card",
                "platega_payment_method_fallbacks": [2],
            },
        )
    )

    assert result["payment_id"] == "txn_card_fallback"
    assert used_methods == [10]


def test_create_payment_retries_on_unavailable_requisites_error(monkeypatch):
    used_methods: list[int] = []

    class FakeService:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def create_payment(self, **kwargs):
            method = int(kwargs["payment_method"])
            used_methods.append(method)
            if method == 10:
                raise platega_api.PlategaAPIError(400, "No available requisites for selected payment method")
            return {
                "payment_id": "txn_requisites_fallback",
                "payment_url": "https://pay.example/txn_requisites_fallback",
            }

    monkeypatch.setattr(platega_api, "PlategaService", FakeService)
    platega_api._CHECKOUT_CACHE.clear()

    result = asyncio.run(
        platega_api.create_payment(
            amount_rub=100.0,
            order_id="order-no-requisites",
            description="Card requisites fallback",
            metadata={
                "platega_payment_method": 10,
                "platega_payment_variant": "card",
                "platega_payment_method_fallbacks": [2],
            },
        )
    )

    assert result["payment_id"] == "txn_requisites_fallback"
    assert used_methods == [10, 12]


def test_create_payment_uses_configured_card_candidates_before_defaults(monkeypatch):
    used_methods: list[int] = []

    class FakeService:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def create_payment(self, **kwargs):
            method = int(kwargs["payment_method"])
            used_methods.append(method)
            return {
                "payment_id": "txn_card_candidates",
                "payment_url": "https://pay.example/txn_card_candidates",
            }

    monkeypatch.setattr(platega_api, "PlategaService", FakeService)
    platega_api._CHECKOUT_CACHE.clear()

    result = asyncio.run(
        platega_api.create_payment(
            amount_rub=100.0,
            order_id="order-card-candidates",
            description="Card candidates",
            metadata={
                "platega_payment_method": 12,
                "platega_payment_variant": "card",
                "platega_payment_method_candidates": "12,10",
            },
        )
    )

    assert result["payment_id"] == "txn_card_candidates"
    assert used_methods == [12]


def test_create_payment_appends_default_card_candidates_to_legacy_setting(monkeypatch):
    used_methods: list[int] = []

    class FakeService:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def create_payment(self, **kwargs):
            method = int(kwargs["payment_method"])
            used_methods.append(method)
            if method == 10:
                raise platega_api.PlategaAPIError(400, "No available requisites for selected payment method")
            return {
                "payment_id": "txn_card_legacy_setting",
                "payment_url": "https://pay.example/txn_card_legacy_setting",
            }

    monkeypatch.setattr(platega_api, "PlategaService", FakeService)
    platega_api._CHECKOUT_CACHE.clear()

    result = asyncio.run(
        platega_api.create_payment(
            amount_rub=100.0,
            order_id="order-card-legacy-setting",
            description="Card legacy setting",
            metadata={
                "platega_payment_method": 10,
                "platega_payment_variant": "card",
                "platega_payment_method_candidates": [10],
            },
        )
    )

    assert result["payment_id"] == "txn_card_legacy_setting"
    assert used_methods == [10, 12]


def test_create_payment_tries_card_acquiring_after_primary_card_candidates(monkeypatch):
    used_methods: list[int] = []

    class FakeService:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def create_payment(self, **kwargs):
            method = int(kwargs["payment_method"])
            used_methods.append(method)
            if method in {10, 12}:
                raise platega_api.PlategaAPIError(400, "No available requisites for selected payment method")
            return {
                "payment_id": "txn_card_acquiring",
                "payment_url": "https://pay.example/txn_card_acquiring",
            }

    monkeypatch.setattr(platega_api, "PlategaService", FakeService)
    platega_api._CHECKOUT_CACHE.clear()

    result = asyncio.run(
        platega_api.create_payment(
            amount_rub=100.0,
            order_id="order-card-acquiring",
            description="Card acquiring fallback",
            metadata={
                "platega_payment_method": 10,
                "platega_payment_variant": "card",
                "platega_payment_method_candidates": [10],
            },
        )
    )

    assert result["payment_id"] == "txn_card_acquiring"
    assert used_methods == [10, 12, 11]
