"""Alert dispatch, dedupe, and formatting — domain layer.

This module provides a thin domain facade over the existing alert
infrastructure in ``errors.py`` and ``scheduler.py``.  It does NOT
duplicate their implementations — it delegates, wrapping them with a
stable interface that the future Go backend can replicate.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

_DEDUPE_DEFAULT_WINDOW = 120
_DEDUPE_MAX_ENTRIES = 500


class AlertDeduplicator:
    """Bounded in-memory deduplicate window for alert keys.

    Prevents the same alert from being dispatched multiple times within
    *window_seconds*.  The internal store is capped at *max_entries* to
    avoid unbounded memory growth.
    """

    __slots__ = ("_max", "_store", "_window")

    def __init__(
        self,
        *,
        window_seconds: int = _DEDUPE_DEFAULT_WINDOW,
        max_entries: int = _DEDUPE_MAX_ENTRIES,
    ) -> None:
        self._store: OrderedDict[str, float] = OrderedDict()
        self._window = max(1, window_seconds)
        self._max = max(10, max_entries)

    def is_duplicate(self, key: str) -> bool:
        """Return ``True`` if *key* was seen within the dedupe window."""
        self._gc()
        prev = self._store.get(key)
        return prev is not None and (time.monotonic() - prev) < self._window

    def record(self, key: str) -> None:
        """Mark *key* as dispatched now."""
        self._store[key] = time.monotonic()
        self._store.move_to_end(key)
        while len(self._store) > self._max:
            self._store.popitem(last=False)

    def _gc(self) -> None:
        cutoff = time.monotonic() - self._window
        while self._store:
            oldest_key, oldest_ts = next(iter(self._store.items()))
            if oldest_ts < cutoff:
                self._store.pop(oldest_key, None)
            else:
                break

    def clear(self) -> None:
        self._store.clear()


def build_dedupe_key(
    error_tag: str,
    details: str = "",
    *,
    max_len: int = 512,
) -> str:
    """Build a deterministic dedupe key from error tag + details."""
    normalized = " ".join(str(details).split())
    return f"{error_tag}|{normalized}"[:max_len]


def send_alert(
    error: Any,
    *,
    details: str = "",
    respect_cooldown: bool = True,
) -> bool:
    """Dispatch a critical alert through the existing infrastructure.

    This is a domain-level wrapper: call it from domain code instead of
    importing ``errors.send_critical_alert`` directly.  When the Go
    backend takes over, this function will be replaced with an HTTP call
    to the alert service.
    """
    from shop_bot.errors import send_critical_alert

    return send_critical_alert(error, details=details, respect_cooldown=respect_cooldown)


def send_alert_nonblocking(
    error: Any,
    *,
    details: str = "",
    respect_cooldown: bool = True,
) -> None:
    """Fire-and-forget domain alert (delegates to bounded queue)."""
    from shop_bot.errors import send_critical_alert_nonblocking

    send_critical_alert_nonblocking(error, details=details, respect_cooldown=respect_cooldown)


def classify_error_message(message: str) -> Any:
    """Classify a raw error string into the corresponding BotError."""
    from shop_bot.errors import classify_error

    return classify_error(message)


# ── Alert Debouncer ──────────────────────────────────────────────────

_DEFAULT_HOST_DEBOUNCE_SECONDS = 60
_DEFAULT_PRIMARY_DEBOUNCE_SECONDS = 0

_ALERT_FIELD_KEYS = (
    "payment_id", "external_payment_id", "user_id", "host",
    "host_name", "key_id", "plan_name", "amount", "price",
)


class _HostAlertState:
    __slots__ = ("down_alert_sent", "first_unhealthy_at", "last_status")

    def __init__(self) -> None:
        self.first_unhealthy_at: float = 0.0
        self.down_alert_sent: bool = False
        self.last_status: str = "unknown"


class AlertDebouncer:
    """Debounce host down/recovery alerts to reduce noise.

    - Regular hosts: suppress CRITICAL until unhealthy for *default_debounce* seconds.
    - Primary hosts: immediate CRITICAL (debounce = *primary_debounce*).
    - Recovery alert only when a down alert was actually sent.
    - ``host_recovered_auto_retry`` only when errors > 0 or recovered > 0.
    """

    __slots__ = ("_default_debounce", "_primary_debounce", "_primary_hosts", "_states")

    def __init__(
        self,
        *,
        default_debounce: int = _DEFAULT_HOST_DEBOUNCE_SECONDS,
        primary_debounce: int = _DEFAULT_PRIMARY_DEBOUNCE_SECONDS,
        primary_hosts: set[str] | None = None,
    ) -> None:
        self._default_debounce = max(0, default_debounce)
        self._primary_debounce = max(0, primary_debounce)
        self._primary_hosts: set[str] = {h.lower() for h in (primary_hosts or set())}
        self._states: dict[str, _HostAlertState] = {}

    def should_alert_down(self, host: str) -> bool:
        """Return ``True`` if a down alert should be sent (debounce elapsed)."""
        key = host.lower()
        state = self._states.setdefault(key, _HostAlertState())

        if state.last_status != "down":
            state.first_unhealthy_at = time.monotonic()
            state.last_status = "down"
            state.down_alert_sent = False

        debounce = (
            self._primary_debounce if key in self._primary_hosts
            else self._default_debounce
        )

        elapsed = time.monotonic() - state.first_unhealthy_at
        if elapsed >= debounce and not state.down_alert_sent:
            state.down_alert_sent = True
            return True
        return False

    def should_alert_recovery(self, host: str) -> bool:
        """Return ``True`` only if a down alert was previously sent."""
        key = host.lower()
        state = self._states.get(key)
        if state and state.down_alert_sent:
            state.last_status = "healthy"
            state.down_alert_sent = False
            return True
        if state:
            state.last_status = "healthy"
        return False

    @staticmethod
    def should_alert_retry_result(errors: int, recovered: int) -> bool:
        """Only alert on retry result when there was actual activity."""
        return errors > 0 or recovered > 0

    def clear(self) -> None:
        self._states.clear()


def format_alert_fields(metadata: dict[str, Any]) -> str:
    """Format metadata into copy-pasteable alert fields."""
    lines: list[str] = []
    for key in _ALERT_FIELD_KEYS:
        val = metadata.get(key)
        if val is not None and str(val).strip():
            lines.append(f"{key} = {val}")
    return "\n".join(lines)
