"""Legacy code deprecation tracker.

Counts calls to legacy code paths during primary mode to confirm
zero legacy usage before code removal (TASK-9 gate).
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_call_counts: dict[str, int] = defaultdict(int)


def track_legacy_call(path: str) -> None:
    with _lock:
        _call_counts[path] += 1
    logger.debug("Legacy path called: %s (total: %d)", path, _call_counts[path])


def get_legacy_call_counts() -> dict[str, int]:
    with _lock:
        return dict(_call_counts)


def total_legacy_calls() -> int:
    with _lock:
        return sum(_call_counts.values())


def reset_legacy_counts() -> None:
    with _lock:
        _call_counts.clear()


def legacy_removal_safe() -> bool:
    return total_legacy_calls() == 0
