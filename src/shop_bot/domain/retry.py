"""Retry engine — domain layer.

Encapsulates the retry state-machine: candidate scanning, metadata
hydration, stuck-transaction detection, and exhaustion escalation.
All DB access goes through injected callables or the legacy module.

This module does NOT own the scheduler loop — it provides the logic
that the loop orchestrates.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

_TX_COL_TO_META: dict[str, str] = {
    "user_id": "user_id",
    "amount_rub": "price",
    "payment_method": "payment_method",
}

_INNER_META_FIELDS = (
    "months", "action", "key_id", "host_name", "plan_id", "plan_name",
)


def hydrate_retry_metadata(
    *,
    tx: dict[str, Any],
    metadata: dict[str, Any],
    payment_id: str,
    retry_source: str,
    normalize_fn: Callable[[Any], dict] | None = None,
) -> dict[str, Any]:
    """Merge transaction columns into metadata for retry processing.

    *normalize_fn* defaults to ``scheduler.normalize_payment_metadata``.
    """
    if normalize_fn is None:
        from shop_bot.data_manager.scheduler import normalize_payment_metadata
        normalize_fn = normalize_payment_metadata

    hydrated = normalize_fn(metadata)
    if not hydrated.get("external_payment_id"):
        hydrated["external_payment_id"] = payment_id
    hydrated["payment_confirmed"] = True
    hydrated["retry_source"] = str(retry_source or "pending_confirmed_retry")

    for col, meta_key in _TX_COL_TO_META.items():
        if hydrated.get(meta_key) in (None, "") and tx.get(col) not in (None, ""):
            hydrated[meta_key] = tx[col]

    inner_meta = tx.get("metadata")
    if isinstance(inner_meta, dict):
        for field in _INNER_META_FIELDS:
            if hydrated.get(field) in (None, "") and inner_meta.get(field) not in (None, ""):
                hydrated[field] = inner_meta[field]

    return hydrated


def stamp_trace(metadata: dict[str, Any]) -> str:
    """Assign a fresh trace_id to *metadata* in-place and return it."""
    trace_id = uuid.uuid4().hex[:12]
    metadata["trace_id"] = trace_id
    return trace_id


def classify_retry_exception(exc: BaseException) -> str:
    """Return a machine-readable tag for a retry-loop exception."""
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "connection" in name:
        return "connection"
    if "telegram" in name:
        return "telegram_api"
    return "unknown"


def should_mark_host_unavailable(exc: BaseException) -> bool:
    """Return True if *exc* means the host is likely unreachable this cycle."""
    tag = classify_retry_exception(exc)
    return tag in ("timeout", "connection")


class InMemoryProcessingLock:
    """Thread-safe set of payment IDs currently being processed.

    Prevents double-processing when multiple retry sources run
    concurrently in the same process.
    """

    __slots__ = ("_ids",)

    def __init__(self) -> None:
        self._ids: set[str] = set()

    def try_acquire(self, payment_id: str) -> bool:
        if payment_id in self._ids:
            return False
        self._ids.add(payment_id)
        return True

    def release(self, payment_id: str) -> None:
        self._ids.discard(payment_id)

    def __contains__(self, payment_id: str) -> bool:
        return payment_id in self._ids

    def __len__(self) -> int:
        return len(self._ids)


def build_retry_stats(
    *,
    checked: int = 0,
    recovered: int = 0,
    errors: int = 0,
    host_skipped: int = 0,
    payment_ids: list[str] | None = None,
    recovered_payment_ids: list[str] | None = None,
    scan_stats: dict[str, int] | None = None,
) -> dict[str, object]:
    """Build the stats dict returned by ``retry_pending_confirmed_transactions``."""
    ss = scan_stats or {}
    return {
        "checked": checked,
        "recovered": recovered,
        "errors": errors,
        "host_skipped": host_skipped,
        "payment_ids": payment_ids or [],
        "recovered_payment_ids": recovered_payment_ids or [],
        "scan_scanned": int(ss.get("scanned", 0)),
        "scan_accepted": int(ss.get("accepted", 0)),
        "scan_skipped_foreign": int(ss.get("skipped_foreign", 0)),
        "scan_skipped_incomplete": int(ss.get("skipped_incomplete", 0)),
    }
