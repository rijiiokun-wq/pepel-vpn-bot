"""Unit tests for shop_bot.domain.adapters — IssuanceRouter."""
import asyncio
import logging
import sqlite3

import pytest

from shop_bot.domain.adapters import (
    AdapterResult,
    IssuanceBackendMode,
    IssuanceRouter,
    _backend_auth_headers,
    _backend_event_id,
    _shadow_counters,
    get_shadow_stats,
)


class _FakeAdapter:
    """Minimal adapter for unit tests."""

    def __init__(self, result: AdapterResult | None = None, *, raise_exc: Exception | None = None):
        self._result = result or AdapterResult(status="issued")
        self._raise_exc = raise_exc
        self.call_count = 0
        self.last_metadata: dict | None = None

    async def process_payment(self, metadata: dict) -> AdapterResult:
        self.call_count += 1
        self.last_metadata = metadata
        if self._raise_exc:
            raise self._raise_exc
        return self._result


def test_backend_auth_headers_empty_token() -> None:
    assert _backend_auth_headers("") is None


def test_backend_auth_headers_sets_shared_token() -> None:
    assert _backend_auth_headers("secret") == {"X-Issuance-Backend-Token": "secret"}


def test_backend_event_id_uses_explicit_idempotency_key() -> None:
    assert _backend_event_id({"idempotency_key": "evt-1", "external_payment_id": "pay-1"}) == "evt-1"


def test_backend_event_id_falls_back_to_stable_payment_id() -> None:
    assert (
        _backend_event_id({"external_payment_id": "pay-1", "bot_source": "reserve"})
        == "payment-confirm:reserve:pay-1"
    )


def test_backend_event_id_returns_none_without_payment_id() -> None:
    assert _backend_event_id({"bot_source": "reserve"}) is None


@pytest.fixture()
def legacy():
    return _FakeAdapter(AdapterResult(status="issued"))


@pytest.fixture()
def backend():
    return _FakeAdapter(AdapterResult(status="issued"))


@pytest.fixture()
def meta():
    return {"external_payment_id": "pay-001", "user_id": 42}


class TestIssuanceRouterLegacyMode:
    def test_legacy_only_calls_legacy(self, monkeypatch, legacy, backend, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.LEGACY)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert legacy.call_count == 1
        assert backend.call_count == 0


class TestIssuanceRouterKillSwitch:
    def test_kill_switch_forces_legacy(self, monkeypatch, legacy, backend, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.PRIMARY)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "1")
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert legacy.call_count == 1
        assert backend.call_count == 0


class TestIssuanceRouterShadowMode:
    def test_shadow_returns_legacy_result(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.SHADOW)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="retry_scheduled"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert legacy.call_count == 1
        assert backend.call_count == 1

    def test_shadow_ignores_backend_error(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.SHADOW)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(raise_exc=RuntimeError("boom"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert legacy.call_count == 1


class TestIssuanceRouterCanaryMode:
    def test_canary_deterministic_routing(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.CANARY)
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "100")
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="issued"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert backend.call_count == 1
        assert legacy.call_count == 0

    def test_canary_0_percent_goes_legacy(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.CANARY)
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "0")
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="issued"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert legacy.call_count == 1
        assert backend.call_count == 0

    def test_canary_fallback_on_error(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.CANARY)
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "100")
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(raise_exc=RuntimeError("down"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert legacy.call_count == 1

    def test_canary_fallback_on_backend_stub_result(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.CANARY)
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "100")
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="retry_scheduled", reason="stub: pending"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert backend.call_count == 1
        assert legacy.call_count == 1


class TestIssuanceRouterPrimaryMode:
    def test_primary_calls_backend(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.PRIMARY)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="issued"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert backend.call_count == 1
        assert legacy.call_count == 0

    def test_primary_falls_back_on_error(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.PRIMARY)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(raise_exc=ConnectionError("timeout"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert legacy.call_count == 1

    def test_primary_falls_back_on_backend_stub_result(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.PRIMARY)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="retry_scheduled", reason="stub: pending"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert backend.call_count == 1
        assert legacy.call_count == 1


class TestIssuanceRouterNoBackend:
    def test_shadow_without_backend_returns_legacy(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.SHADOW)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        router = IssuanceRouter(legacy=legacy, backend=None)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert legacy.call_count == 1


class TestIssuanceRouterUnknownMode:
    def test_unknown_mode_falls_back_to_legacy(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", "nonexistent_mode")
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="issued"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert legacy.call_count == 1
        assert backend.call_count == 0


class TestIssuanceRouterShadowDiffLogging:
    def test_shadow_logs_status_diff(self, monkeypatch, meta, caplog):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.SHADOW)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="retry_scheduled"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        with caplog.at_level(logging.WARNING, logger="shop_bot.domain.adapters"):
            asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert any("Shadow DIFF" in r.message for r in caplog.records)


class TestIssuanceRouterShadowDiffPersistence:
    @pytest.fixture
    def temp_db(self, tmp_path, monkeypatch):
        from shop_bot.data_manager import database

        db_file = tmp_path / "test_users.db"
        monkeypatch.setattr(database, "DB_FILE", db_file)
        monkeypatch.setattr(database, "_WAL_INITIALIZED", False)
        database.initialize_db()
        return db_file

    def test_shadow_diff_persists_row(self, monkeypatch, meta, temp_db):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.SHADOW)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued", reason="legacy-ok"))
        backend = _FakeAdapter(AdapterResult(status="retry_scheduled", reason="backend-retry"))
        router = IssuanceRouter(legacy=legacy, backend=backend)

        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))

        assert result.status == "issued"
        with sqlite3.connect(temp_db) as conn:
            row = conn.execute(
                """
                SELECT payment_id, legacy_status, shadow_status, legacy_reason, shadow_reason, legacy_ms, shadow_ms
                FROM shadow_diffs
                WHERE payment_id = ?
                """,
                (meta["external_payment_id"],),
            ).fetchone()

        assert row is not None
        assert row[:5] == (
            "pay-001",
            "issued",
            "retry_scheduled",
            "legacy-ok",
            "backend-retry",
        )
        assert row[5] >= 0
        assert row[6] >= 0

    def test_shadow_diff_persistence_failure_logs_warning(self, monkeypatch, meta, caplog):
        from shop_bot.data_manager import database

        def fail_record_shadow_diff(**_kwargs):
            raise sqlite3.OperationalError

        monkeypatch.setattr(database, "record_shadow_diff", fail_record_shadow_diff)
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.SHADOW)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="retry_scheduled"))
        router = IssuanceRouter(legacy=legacy, backend=backend)

        with caplog.at_level(logging.WARNING, logger="shop_bot.domain.adapters"):
            result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))

        assert result.status == "issued"
        assert any(
            "Could not persist shadow diff" in record.message and "pay-001" in record.message
            for record in caplog.records
        )


class TestShadowComparison:
    @pytest.fixture(autouse=True)
    def _reset_counters(self):
        for k in _shadow_counters:
            _shadow_counters[k] = 0

    def test_shadow_match_increments_counter(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.SHADOW)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="issued"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert _shadow_counters["match"] == 1
        assert _shadow_counters["diff"] == 0

    def test_shadow_diff_increments_counter(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.SHADOW)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="retry_scheduled"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert _shadow_counters["diff"] == 1
        assert _shadow_counters["match"] == 0

    def test_shadow_error_does_not_affect_user(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.SHADOW)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(raise_exc=RuntimeError("backend down"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert _shadow_counters["error"] == 1

    def test_shadow_always_returns_legacy(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.SHADOW)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued", key_id=42))
        backend = _FakeAdapter(AdapterResult(status="fatal_failed"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert result.key_id == 42

    def test_get_shadow_stats(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.SHADOW)
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(AdapterResult(status="issued"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        stats = get_shadow_stats()
        assert stats["total"] == 1
        assert stats["match"] == 1
        assert stats["match_rate"] == 100.0


class TestIssuanceRouterCanaryDeterminism:
    def test_same_payment_id_routes_to_same_backend(self, monkeypatch):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.CANARY)
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "50")
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        fixed_meta = {"external_payment_id": "stable-pay-id-42", "user_id": 1}
        results = []
        for _ in range(5):
            legacy = _FakeAdapter(AdapterResult(status="legacy"))
            backend = _FakeAdapter(AdapterResult(status="backend"))
            router = IssuanceRouter(legacy=legacy, backend=backend)
            result = asyncio.get_event_loop().run_until_complete(
                router.process_payment(fixed_meta),
            )
            results.append(result.status)
        assert len(set(results)) == 1, f"Non-deterministic routing: {results}"


class TestCanaryFallback:
    def test_canary_fallback_on_backend_error(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.CANARY)
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "100")
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="issued"))
        backend = _FakeAdapter(raise_exc=RuntimeError("backend crash"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "issued"
        assert legacy.call_count == 1

    def test_kill_switch_overrides_canary(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.CANARY)
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "100")
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "1")
        legacy = _FakeAdapter(AdapterResult(status="legacy_result"))
        backend = _FakeAdapter(AdapterResult(status="backend_result"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "legacy_result"
        assert backend.call_count == 0

    def test_canary_0_percent_all_legacy(self, monkeypatch, meta):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.CANARY)
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "0")
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        legacy = _FakeAdapter(AdapterResult(status="legacy"))
        backend = _FakeAdapter(AdapterResult(status="backend"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        result = asyncio.get_event_loop().run_until_complete(router.process_payment(meta))
        assert result.status == "legacy"

    def test_canary_isolates_bot_source(self, monkeypatch):
        monkeypatch.setenv("ISSUANCE_BACKEND_MODE", IssuanceBackendMode.CANARY)
        monkeypatch.setenv("ISSUANCE_CANARY_PERCENT", "50")
        monkeypatch.setenv("ISSUANCE_BACKEND_KILL_SWITCH", "0")
        meta_main = {"external_payment_id": "pay-main", "user_id": 1, "bot_source": "main"}
        meta_reserve = {"external_payment_id": "pay-main", "user_id": 1, "bot_source": "reserve"}
        legacy = _FakeAdapter(AdapterResult(status="legacy"))
        backend = _FakeAdapter(AdapterResult(status="backend"))
        router = IssuanceRouter(legacy=legacy, backend=backend)
        r1 = asyncio.get_event_loop().run_until_complete(router.process_payment(meta_main))
        r2 = asyncio.get_event_loop().run_until_complete(router.process_payment(meta_reserve))
        assert r1.status in ("legacy", "backend")
        assert r2.status in ("legacy", "backend")
