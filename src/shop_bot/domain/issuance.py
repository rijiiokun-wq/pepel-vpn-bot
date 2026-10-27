"""Key issuance orchestration — domain layer.

Contains helpers for the issuance state machine that are independent of
Telegram UI.  The full ``process_successful_payment`` flow still lives
in ``handlers.py``; this module provides:

* ``mark_issuance_failure`` — metadata bookkeeping for failed attempts.
* ``extract_panel_snapshot`` — pull panel client coordinates from metadata.
* ``resolve_paid_at`` — canonical paid-at timestamp from metadata.
* ``pick_issuance_action`` — resolve action enum from raw string.
* ``generate_new_email`` — deterministic email generator for new keys.

After migration Stage B the full process_payment will live here.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from shop_bot.domain.protocols import IssuanceAction

logger = logging.getLogger(__name__)


def mark_issuance_failure(
    *,
    payment_id: str | None,
    metadata: dict[str, Any] | None,
    reason: str,
    failure_logs: dict[str, Any] | None = None,
    fatal: bool = False,
) -> dict[str, Any]:
    """Update *metadata* dict with issuance failure bookkeeping fields.

    Returns the updated metadata dict (mutated in place for efficiency).
    Does NOT touch the database — the caller is responsible for persisting.
    """
    # Retained for the domain API while persistence remains caller-owned.
    _ = payment_id
    if metadata is None:
        metadata = {}
    now_iso = datetime.now().isoformat()
    previous_count = int(metadata.get("issuance_failure_count") or 0)

    metadata["issuance_failed"] = True
    metadata["issuance_failure_reason"] = str(reason or "unknown")
    metadata["issuance_failure_last_at"] = now_iso
    if not metadata.get("issuance_failure_first_at"):
        metadata["issuance_failure_first_at"] = now_iso
    metadata["issuance_failure_count"] = previous_count + 1
    metadata["issuance_failure_alert_sent"] = True
    if fatal:
        metadata["issuance_fatal"] = True
    metadata.pop("autoretry_exhausted_alert_sent", None)
    metadata.pop("autoretry_exhausted_alert_at", None)
    if not metadata.get("retry_source"):
        metadata["retry_source"] = "issuance_autoretry"

    if failure_logs:
        sanitized = {
            str(k): str(v)[:500]
            for k, v in failure_logs.items()
            if k and v is not None
        }
        if sanitized:
            metadata["issuance_failure_logs"] = sanitized

    return metadata


def extract_panel_snapshot(metadata: dict[str, Any] | None) -> dict[str, str] | None:
    """Extract panel client coordinates from transaction metadata.

    Returns ``{"host_name": ..., "email": ..., "client_uuid": ...}`` or
    ``None`` if the snapshot is incomplete.
    """
    if not isinstance(metadata, dict):
        return None

    host_name = str(
        metadata.get("issued_panel_host")
        or metadata.get("panel_host_name")
        or "",
    ).strip()
    email = str(
        metadata.get("issued_panel_email")
        or metadata.get("panel_email")
        or "",
    ).strip()
    client_uuid = str(
        metadata.get("issued_panel_uuid")
        or metadata.get("panel_client_uuid")
        or "",
    ).strip()

    if not host_name or not email or not client_uuid:
        return None

    return {
        "host_name": host_name,
        "email": email,
        "client_uuid": client_uuid,
    }


def resolve_paid_at(metadata: dict[str, Any]) -> datetime:
    """Return the canonical paid-at timestamp from metadata.

    Falls back to ``datetime.now()`` when the metadata field is missing
    or unparseable.
    """
    raw = metadata.get("paid_at")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            pass
    return datetime.now()


def pick_issuance_action(raw: str | None) -> IssuanceAction:
    """Normalize a raw action string into ``IssuanceAction``."""
    norm = str(raw or "").strip().lower()
    try:
        return IssuanceAction(norm)
    except ValueError:
        return IssuanceAction.NEW


def generate_new_email(user_id: int, key_number: int) -> str:
    """Deterministic email for a new VPN key."""
    return f"user{user_id}-key{key_number}@vpn.bot"


def generate_fallback_email(user_id: int, key_number: int, offset: int) -> str:
    """Deterministic fallback email for alternate new-key attempts."""
    return f"user{user_id}-key{key_number + offset}@vpn.bot"
