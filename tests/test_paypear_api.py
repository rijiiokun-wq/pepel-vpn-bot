import asyncio
import hashlib
import hmac
import json

from shop_bot.modules import paypear_api


def _signature(payload: dict, secret: str) -> str:
    unsigned = {key: value for key, value in payload.items() if key != "signature"}
    body = json.dumps(unsigned, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_verify_webhook_signature_accepts_body_signature():
    payload = {
        "event": "payment.confirmed",
        "object": {
            "id": "pay_001",
            "status": "CONFIRMED",
        },
    }
    secret = "webhook-secret"
    payload["signature"] = _signature(payload, secret)
    raw_body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    assert paypear_api.verify_webhook_signature(raw_body, payload["signature"], secret) is True


def test_verify_webhook_signature_rejects_invalid_signature():
    payload = {
        "event": "payment.confirmed",
        "object": {"id": "pay_001", "status": "CONFIRMED"},
    }
    raw_body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")

    assert paypear_api.verify_webhook_signature(raw_body, "deadbeef", "secret") is False


def test_map_paypear_status_normalization():
    assert paypear_api.map_paypear_status("CONFIRMED") == "succeeded"
    assert paypear_api.map_paypear_status("COMPLETED") == "succeeded"
    assert paypear_api.map_paypear_status("canceled") == "canceled"
    assert paypear_api.map_paypear_status("DECLINED") == "canceled"
    assert paypear_api.map_paypear_status("process") == "pending"


def test_normalize_payment_method_preserves_paypear_enum_values():
    assert paypear_api._normalize_payment_method("p2p_card") == "P2P_CARD"
    assert paypear_api._normalize_payment_method("basic_card_hpp") == "BASIC_CARD_HPP"
    assert paypear_api._normalize_payment_method("not a method") == paypear_api.PAYMENT_METHOD_SBP


def test_legacy_card_payment_method_candidates_order():
    assert paypear_api._legacy_payment_method_candidates("basic_card") == [
        "card",
        "basic_card_hpp",
        "p2p_card",
        "bank_card",
    ]
    assert paypear_api._legacy_payment_method_candidates("sbp") == ["sbp"]


def test_resolve_api_base_url_uses_modern_default_for_legacy_db_value(monkeypatch):
    settings = {
        "paypear_api_base_url": "https://api.paypear.ru/v1",
        "paypear_force_legacy_api_base_url": "false",
    }
    def get_test_setting(key):
        return settings.get(key)

    monkeypatch.setattr(paypear_api, "get_setting", get_test_setting)
    monkeypatch.delenv("PAYPEAR_API_BASE_URL", raising=False)
    monkeypatch.delenv("PAYPEAR_FORCE_LEGACY_API_BASE_URL", raising=False)

    assert paypear_api._resolve_api_base_url() == paypear_api.PAYPEAR_API_BASE_URL


def test_resolve_api_base_url_keeps_legacy_when_force_flag_enabled(monkeypatch):
    settings = {
        "paypear_api_base_url": "https://api.paypear.ru/v1",
    }
    def get_test_setting(key):
        return settings.get(key)

    monkeypatch.setattr(paypear_api, "get_setting", get_test_setting)
    monkeypatch.delenv("PAYPEAR_API_BASE_URL", raising=False)
    monkeypatch.setenv("PAYPEAR_FORCE_LEGACY_API_BASE_URL", "true")

    assert paypear_api._resolve_api_base_url() == "https://api.paypear.ru/v1"


def test_resolve_webhook_url_from_domain(monkeypatch):
    settings = {
        "domain": "pepelbot.ru",
    }

    def get_test_setting(key):
        return settings.get(key)

    monkeypatch.setattr(paypear_api, "get_setting", get_test_setting)
    monkeypatch.delenv("PAYPEAR_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("PAYPEAR_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)

    assert paypear_api._resolve_webhook_url() == "https://pepelbot.ru/paypear-webhook"


def test_create_payment_reuses_cached_checkout(monkeypatch):
    class FakeService:
        calls = 0

        async def create_payment(self, **_kwargs):
            FakeService.calls += 1
            return {
                "payment_id": "pp_txn_123",
                "payment_url": "https://paypear.example/pay/pp_txn_123",
            }

    monkeypatch.setattr(paypear_api, "PayPearService", FakeService)
    paypear_api._CHECKOUT_CACHE.clear()

    first = asyncio.run(
        paypear_api.create_payment(
            amount_rub=199.0,
            order_id="order-1",
            description="Test payment",
            metadata={"checkout_ttl_seconds": 300},
            payment_method_type=paypear_api.PAYMENT_METHOD_SBP,
        )
    )
    second = asyncio.run(
        paypear_api.create_payment(
            amount_rub=199.0,
            order_id="order-1",
            description="Test payment",
            metadata={"checkout_ttl_seconds": 300},
            payment_method_type=paypear_api.PAYMENT_METHOD_CARD,
        )
    )

    assert FakeService.calls == 1
    assert first == second


def test_service_create_payment_uses_current_paypear_contract(monkeypatch):
    captured = {}
    settings = {
        "paypear_shop_id": "shop-id",
        "paypear_secret_key": "secret-token",
        "paypear_api_base_url": "https://engine.paypear.example/api/v1",
        "paypear_webhook_url": "https://pepelbot.ru/paypear-webhook",
    }

    def get_test_setting(key):
        return settings.get(key)

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def text(self):
            return json.dumps(
                {
                "id": "pay_123",
                "redirect_url": "https://checkout.paypear.example/pay_123",
                "status": "CREATED",
                }
            )

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def post(self, url, *, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(paypear_api.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(paypear_api, "get_setting", get_test_setting)

    result = asyncio.run(
        paypear_api.PayPearService().create_payment(
            amount=199.0,
            order_id="order-123",
            description="VPN",
            payment_method_type="sbp",
            currency="RUB",
            return_url="https://t.me/example_bot",
            metadata={"user_id": 42},
        )
    )

    assert captured["url"] == "https://engine.paypear.example/api/v1/payments"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["headers"]["X-Shop-Id"] == "shop-id"
    assert captured["headers"]["Idempotence-Key"] == "order-123"
    assert captured["json"]["referenceId"] == "order-123"
    assert captured["json"]["paymentType"] == "DEPOSIT"
    assert captured["json"]["paymentMethod"] == "SBP"
    assert captured["json"]["amount"] == 199.0
    assert captured["json"]["returnUrl"] == "https://t.me/example_bot"
    assert captured["json"]["webhookUrl"] == "https://pepelbot.ru/paypear-webhook"
    assert "metadata" not in captured["json"]
    assert result == {
        "payment_id": "pay_123",
        "payment_url": "https://checkout.paypear.example/pay_123",
    }


def test_service_create_payment_falls_back_to_legacy_contract(monkeypatch):
    calls: list[dict] = []
    settings = {
        "paypear_shop_id": "shop-id",
        "paypear_secret_key": "secret-token",
        "paypear_api_base_url": "https://engine.paypear.example/api/v1",
    }

    def get_test_setting(key):
        return settings.get(key)

    class FakeResponse:
        def __init__(self, status: int, payload: dict):
            self.status = status
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def text(self):
            return json.dumps(self._payload)

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def post(self, url, *, json, headers):
            calls.append({"url": url, "json": json, "headers": headers})
            if url.endswith("/payments"):
                return FakeResponse(401, {"error": {"message": "Invalid credentials"}})
            return FakeResponse(
                200,
                {
                    "success": True,
                    "result": {
                        "id": "legacy_pay_123",
                        "payment_url": "https://legacy.paypear.example/legacy_pay_123",
                    },
                },
            )

    monkeypatch.setattr(paypear_api.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(paypear_api, "get_setting", get_test_setting)

    result = asyncio.run(
        paypear_api.PayPearService().create_payment(
            amount=199.0,
            order_id="order-legacy-123",
            description="VPN",
            payment_method_type="sbp",
            currency="RUB",
            return_url="https://t.me/example_bot",
            metadata={"user_id": 42},
        )
    )

    assert len(calls) == 2
    assert calls[0]["url"] == "https://engine.paypear.example/api/v1/payments"
    assert calls[0]["headers"]["Authorization"] == "Bearer secret-token"
    assert calls[0]["headers"]["Idempotence-Key"] == "order-legacy-123"
    assert calls[1]["url"] == "https://api.paypear.ru/v1/payment/"
    assert calls[1]["headers"]["Authorization"].startswith("Basic ")
    assert calls[1]["headers"]["Idempotence-Key"] == "order-legacy-123"
    assert result == {
        "payment_id": "legacy_pay_123",
        "payment_url": "https://legacy.paypear.example/legacy_pay_123",
    }


def test_service_legacy_fallback_uses_first_card_candidate(monkeypatch):
    calls: list[dict] = []
    settings = {
        "paypear_shop_id": "shop-id",
        "paypear_secret_key": "secret-token",
        "paypear_api_base_url": "https://engine.paypear.example/api/v1",
    }

    def get_test_setting(key):
        return settings.get(key)

    class FakeResponse:
        def __init__(self, status: int, payload: dict):
            self.status = status
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def text(self):
            return json.dumps(self._payload)

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def post(self, url, *, json, headers):
            calls.append({"url": url, "json": json, "headers": headers})
            if url.endswith("/payments"):
                return FakeResponse(401, {"error": {"message": "Invalid credentials"}})
            return FakeResponse(
                200,
                {
                    "success": True,
                    "result": {
                        "id": "legacy_pay_card_123",
                        "payment_url": "https://legacy.paypear.example/legacy_pay_card_123",
                    },
                },
            )

    monkeypatch.setattr(paypear_api.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(paypear_api, "get_setting", get_test_setting)

    result = asyncio.run(
        paypear_api.PayPearService().create_payment(
            amount=199.0,
            order_id="order-legacy-card-123",
            description="VPN",
            payment_method_type=paypear_api.PAYMENT_METHOD_CARD,
            currency="RUB",
            return_url="https://t.me/example_bot",
            metadata={"user_id": 42},
        )
    )

    assert len(calls) == 2
    assert calls[1]["json"]["payment_method_data"]["type"] == "card"
    assert result["payment_id"] == "legacy_pay_card_123"


def test_service_legacy_fallback_retries_card_method_not_found(monkeypatch):
    calls: list[dict] = []
    settings = {
        "paypear_shop_id": "shop-id",
        "paypear_secret_key": "secret-token",
        "paypear_api_base_url": "https://engine.paypear.example/api/v1",
    }

    def get_test_setting(key):
        return settings.get(key)

    class FakeResponse:
        def __init__(self, status: int, payload: dict):
            self.status = status
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def text(self):
            return json.dumps(self._payload)

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def post(self, url, *, json, headers):
            calls.append({"url": url, "json": json, "headers": headers})
            if url.endswith("/payments"):
                return FakeResponse(401, {"error": {"message": "Invalid credentials"}})

            legacy_method = str(json.get("payment_method_data", {}).get("type") or "")
            if legacy_method in {"card", "basic_card_hpp", "p2p_card"}:
                return FakeResponse(400, {"message": "'payment method type is not found'"})

            return FakeResponse(
                200,
                {
                    "success": True,
                    "result": {
                        "id": "legacy_pay_card_321",
                        "payment_url": "https://legacy.paypear.example/legacy_pay_card_321",
                    },
                },
            )

    monkeypatch.setattr(paypear_api.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(paypear_api, "get_setting", get_test_setting)

    result = asyncio.run(
        paypear_api.PayPearService().create_payment(
            amount=399.0,
            order_id="order-legacy-card-321",
            description="VPN",
            payment_method_type=paypear_api.PAYMENT_METHOD_CARD,
            currency="RUB",
            return_url="https://t.me/example_bot",
            metadata={"user_id": 42},
        )
    )

    legacy_calls = [item for item in calls if item["url"].endswith("/payment/")]
    assert [item["json"]["payment_method_data"]["type"] for item in legacy_calls] == [
        "card",
        "basic_card_hpp",
        "p2p_card",
        "bank_card",
    ]
    assert result["payment_id"] == "legacy_pay_card_321"


def test_service_check_status_uses_current_paypear_contract(monkeypatch):
    captured = {}
    settings = {
        "paypear_shop_id": "shop-id",
        "paypear_secret_key": "secret-token",
        "paypear_api_base_url": "https://engine.paypear.example/api/v1",
    }

    def get_test_setting(key):
        return settings.get(key)

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def text(self):
            return json.dumps({"id": "pay_123", "status": "CONFIRMED"})

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def get(self, url, *, headers):
            captured["url"] = url
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(paypear_api.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(paypear_api, "get_setting", get_test_setting)

    result = asyncio.run(paypear_api.PayPearService().check_status("pay_123"))

    assert captured["url"] == "https://engine.paypear.example/api/v1/payments/pay_123"
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert result == {
        "status": "succeeded",
        "provider_status": "CONFIRMED",
    }


def test_service_check_status_accepts_paypear_state_field(monkeypatch):
    settings = {
        "paypear_shop_id": "shop-id",
        "paypear_secret_key": "secret-token",
        "paypear_api_base_url": "https://engine.paypear.example/api/v1",
    }

    def get_test_setting(key):
        return settings.get(key)

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def text(self):
            return json.dumps({"result": {"id": "pay_123", "state": "COMPLETED"}})

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def get(self, _url, *, headers):
            return FakeResponse()

    monkeypatch.setattr(paypear_api.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(paypear_api, "get_setting", get_test_setting)

    result = asyncio.run(paypear_api.PayPearService().check_status("pay_123"))

    assert result == {
        "status": "succeeded",
        "provider_status": "COMPLETED",
    }
