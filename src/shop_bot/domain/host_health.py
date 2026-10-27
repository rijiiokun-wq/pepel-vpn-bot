"""Host health probing and circuit-breaker logic — domain layer.

Provides domain-level helpers that wrap the existing per-host circuit
breaker in ``xui_api`` and the health classification from ``scheduler``.
When the Go backend takes over, these become the authoritative health
API surface.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from shop_bot.domain.protocols import CircuitState, HostStatus

logger = logging.getLogger(__name__)

_UNHEALTHY_STATUSES = frozenset({"degraded", "unavailable", "maintenance"})


def host_status_from_raw(raw: str | None) -> HostStatus:
    """Convert a raw DB / panel status string to a ``HostStatus`` enum."""
    norm = str(raw or "").strip().lower()
    if norm in {"degraded", "maintenance"}:
        return HostStatus.DEGRADED
    if norm in {"unavailable", "offline", "error"}:
        return HostStatus.UNAVAILABLE
    return HostStatus.HEALTHY


def is_unhealthy(status: str | None) -> bool:
    """Return ``True`` if the raw status string indicates an unhealthy host."""
    return str(status or "").strip().lower() in _UNHEALTHY_STATUSES


def check_host_for_autoretry(
    host_name: str | None,
    *,
    get_host_fn: Any | None = None,
) -> tuple[bool, str]:
    """Check whether *host_name* is unhealthy for the retry engine.

    Returns ``(is_unhealthy, effective_status)```.  When *get_host_fn* is
    supplied it is called to fetch the host row from the database;
    otherwise the legacy ``database.get_host`` is used.
    """
    name = str(host_name or "").strip()
    if not name:
        return False, "unknown"

    _get_host = get_host_fn
    if _get_host is None:
        from shop_bot.data_manager import database
        _get_host = database.get_host

    host = _get_host(name)
    if not host:
        return False, "missing"

    raw = str(host.get("raw_status") or host.get("status") or "").strip().lower()
    return is_unhealthy(raw), raw


class CircuitBreaker:
    """Per-host circuit breaker with half-open probe support.

    This is a domain-level wrapper over the existing ``xui_api`` circuit
    breaker state.  For now it delegates; after migration it will own the
    state entirely.
    """

    __slots__ = ("_cooldown", "_failure_threshold", "_lock", "_states")

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self._failure_threshold = max(1, failure_threshold)
        self._cooldown = max(1.0, cooldown_seconds)
        self._lock = threading.Lock()
        self._states: dict[str, dict[str, Any]] = {}

    def precheck(self, host_key: str) -> tuple[CircuitState, int]:
        """Return ``(state, remaining_seconds)`` for *host_key*.

        - ``CLOSED`` — host is available, remaining = 0.
        - ``OPEN`` — host is tripped, remaining > 0.
        - ``HALF_OPEN`` — cooldown expired, one probe allowed.
        """
        with self._lock:
            entry = self._states.get(host_key)
            if not entry:
                return CircuitState.CLOSED, 0

            opened_until = float(entry.get("opened_until", 0.0))
            if opened_until <= 0:
                return CircuitState.CLOSED, 0

            now = time.time()
            if now >= opened_until:
                entry["opened_until"] = 0.0
                entry["failures"] = 0
                return CircuitState.HALF_OPEN, 0

            remaining = max(1, int(opened_until - now))
            return CircuitState.OPEN, remaining

    def on_success(self, host_key: str) -> None:
        with self._lock:
            self._states.pop(host_key, None)

    def on_failure(self, host_key: str) -> bool:
        """Record a failure. Return ``True`` if the breaker just opened."""
        now = time.time()
        with self._lock:
            entry = self._states.setdefault(
                host_key, {"failures": 0, "opened_until": 0.0},
            )
            entry["failures"] = int(entry.get("failures", 0)) + 1
            if entry["failures"] >= self._failure_threshold:
                entry["opened_until"] = now + self._cooldown
                return True
        return False

    def reset(self) -> None:
        with self._lock:
            self._states.clear()
