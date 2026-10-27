"""Runtime metrics collection for issuance pipeline observability.

Exposes counters that can be read via /internal/runtime/status or
scraped by a monitoring system.
"""
from __future__ import annotations

import os
import threading
from collections import defaultdict

_lock = threading.Lock()
_counters: dict[str, int] = defaultdict(int)


def inc(name: str, amount: int = 1, **labels: str) -> None:
    key = _build_key(name, labels)
    with _lock:
        _counters[key] += amount


def get(name: str, **labels: str) -> int:
    key = _build_key(name, labels)
    with _lock:
        return _counters.get(key, 0)


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def reset() -> None:
    with _lock:
        _counters.clear()


def runtime_status() -> dict:
    from shop_bot.domain.adapters import get_shadow_stats
    from shop_bot.domain.deprecation import get_legacy_call_counts, total_legacy_calls

    canary_percent, config_warnings = _read_canary_percent()

    return {
        "mode": os.environ.get("ISSUANCE_BACKEND_MODE", "legacy"),
        "canary_percent": canary_percent,
        "kill_switch": os.environ.get("ISSUANCE_BACKEND_KILL_SWITCH", "0") == "1",
        "config_warnings": config_warnings,
        "shadow": get_shadow_stats(),
        "legacy_calls": get_legacy_call_counts(),
        "legacy_calls_total": total_legacy_calls(),
        "counters": snapshot(),
    }


def _read_canary_percent() -> tuple[int, list[dict[str, object]]]:
    raw_value = os.environ.get("ISSUANCE_CANARY_PERCENT", "0")
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        return 0, [
            {
                "field": "ISSUANCE_CANARY_PERCENT",
                "reason": "invalid_integer",
                "normalized_to": 0,
            }
        ]

    if parsed_value < 0:
        return 0, [
            {
                "field": "ISSUANCE_CANARY_PERCENT",
                "reason": "below_min",
                "normalized_to": 0,
            }
        ]
    if parsed_value > 100:
        return 100, [
            {
                "field": "ISSUANCE_CANARY_PERCENT",
                "reason": "above_max",
                "normalized_to": 100,
            }
        ]

    return parsed_value, []


def _build_key(name: str, labels: dict[str, str]) -> str:
    if not labels:
        return name
    pairs = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    return f"{name}{{{pairs}}}"
