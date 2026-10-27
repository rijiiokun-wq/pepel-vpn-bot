"""Unit tests for shop_bot.domain.retry — retry engine helpers."""
from shop_bot.domain.retry import (
    InMemoryProcessingLock,
    build_retry_stats,
    classify_retry_exception,
    hydrate_retry_metadata,
    should_mark_host_unavailable,
    stamp_trace,
)


class TestHydrateRetryMetadata:
    def test_fills_external_payment_id(self):
        result = hydrate_retry_metadata(
            tx={"user_id": 42},
            metadata={},
            payment_id="pay-1",
            retry_source="test",
            normalize_fn=lambda m: dict(m),
        )
        assert result["external_payment_id"] == "pay-1"
        assert result["payment_confirmed"] is True
        assert result["retry_source"] == "test"

    def test_copies_tx_columns(self):
        result = hydrate_retry_metadata(
            tx={"user_id": 99, "amount_rub": 500, "payment_method": "crypto"},
            metadata={},
            payment_id="p2",
            retry_source="s",
            normalize_fn=lambda m: dict(m),
        )
        assert result["user_id"] == 99
        assert result["price"] == 500
        assert result["payment_method"] == "crypto"

    def test_inner_metadata_fills_gaps(self):
        result = hydrate_retry_metadata(
            tx={"metadata": {"months": 3, "action": "extend", "host_name": "H"}},
            metadata={},
            payment_id="p3",
            retry_source="s",
            normalize_fn=lambda m: dict(m),
        )
        assert result["months"] == 3
        assert result["action"] == "extend"
        assert result["host_name"] == "H"

    def test_does_not_overwrite_existing(self):
        result = hydrate_retry_metadata(
            tx={"user_id": 99},
            metadata={"user_id": 42},
            payment_id="p4",
            retry_source="s",
            normalize_fn=lambda m: dict(m),
        )
        assert result["user_id"] == 42


class TestStampTrace:
    def test_assigns_trace_id(self):
        meta: dict = {}
        tid = stamp_trace(meta)
        assert len(tid) == 12
        assert meta["trace_id"] == tid


class TestClassifyRetryException:
    def test_timeout(self):
        assert classify_retry_exception(TimeoutError()) == "timeout"

    def test_connection(self):
        assert classify_retry_exception(ConnectionError()) == "connection"

    def test_unknown(self):
        assert classify_retry_exception(RuntimeError()) == "unknown"


class TestShouldMarkHostUnavailable:
    def test_timeout_true(self):
        assert should_mark_host_unavailable(TimeoutError()) is True

    def test_connection_true(self):
        assert should_mark_host_unavailable(ConnectionError()) is True

    def test_runtime_false(self):
        assert should_mark_host_unavailable(RuntimeError()) is False


class TestInMemoryProcessingLock:
    def test_acquire_and_release(self):
        lock = InMemoryProcessingLock()
        assert lock.try_acquire("p1") is True
        assert lock.try_acquire("p1") is False
        assert "p1" in lock
        lock.release("p1")
        assert "p1" not in lock
        assert lock.try_acquire("p1") is True

    def test_len(self):
        lock = InMemoryProcessingLock()
        lock.try_acquire("a")
        lock.try_acquire("b")
        assert len(lock) == 2


class TestBuildRetryStats:
    def test_defaults(self):
        s = build_retry_stats()
        assert s["checked"] == 0
        assert s["recovered"] == 0
        assert s["scan_scanned"] == 0

    def test_with_scan_stats(self):
        s = build_retry_stats(
            checked=5, recovered=2,
            scan_stats={"scanned": 10, "accepted": 5, "skipped_foreign": 3, "skipped_incomplete": 2},
        )
        assert s["checked"] == 5
        assert s["scan_scanned"] == 10
        assert s["scan_skipped_foreign"] == 3


class TestClassifyRetryExceptionEdgeCases:
    def test_oserror_is_unknown(self):
        assert classify_retry_exception(OSError("Network unreachable")) == "unknown"

    def test_connection_reset_is_connection(self):
        assert classify_retry_exception(ConnectionResetError("reset")) == "connection"

    def test_builtin_timeout_error(self):
        assert classify_retry_exception(TimeoutError("async timeout")) == "timeout"

    def test_value_error_unknown(self):
        assert classify_retry_exception(ValueError("bad data")) == "unknown"


class TestInMemoryProcessingLockEdgeCases:
    def test_release_nonexistent_is_safe(self):
        lock = InMemoryProcessingLock()
        lock.release("nonexistent")

    def test_concurrent_ids_independent(self):
        lock = InMemoryProcessingLock()
        assert lock.try_acquire("p1") is True
        assert lock.try_acquire("p2") is True
        lock.release("p1")
        assert lock.try_acquire("p1") is True
        assert lock.try_acquire("p2") is False

    def test_clear_all(self):
        lock = InMemoryProcessingLock()
        lock.try_acquire("a")
        lock.try_acquire("b")
        lock._ids.clear()
        assert len(lock) == 0
        assert lock.try_acquire("a") is True


class TestHydrateRetryMetadataEdgeCases:
    def test_none_tx_metadata_field_skipped(self):
        result = hydrate_retry_metadata(
            tx={"metadata": None},
            metadata={},
            payment_id="p5",
            retry_source="s",
            normalize_fn=lambda m: dict(m),
        )
        assert result["external_payment_id"] == "p5"

    def test_normalize_fn_applied(self):
        def add_marker(m):
            m["normalized"] = True
            return m
        result = hydrate_retry_metadata(
            tx={}, metadata={},
            payment_id="p6", retry_source="s",
            normalize_fn=add_marker,
        )
        assert result.get("normalized") is True
