"""Payment metadata validation and confirmation gate.

Pure domain logic — no Telegram, no Flask, no direct DB calls.
All DB access goes through injected callables or the legacy module.
"""
from __future__ import annotations

import logging
from typing import Any

from shop_bot.domain.protocols import IssuanceAction, PaymentMeta

logger = logging.getLogger(__name__)

_REQUIRED_INT_FIELDS = ("user_id", "months", "key_id", "plan_id")
_REQUIRED_FLOAT_FIELDS = ("price",)
_REQUIRED_STR_FIELDS = ("action", "host_name")

# Post-payment precheck failures are treated as retryable so autoretry can
# complete key issuance once infrastructure/config issues are fixed.
FATAL_PRECHECK_REASONS: frozenset[str] = frozenset()


def parse_payment_metadata(
    raw: dict[str, Any],
    *,
    is_super_flow_fn: Any | None = None,
) -> PaymentMeta:
    """Parse raw metadata dict into a validated ``PaymentMeta``.

    Raises ``ValueError`` with a descriptive message on missing/invalid fields.
    The optional *is_super_flow_fn* callback resolves super-key purchase flows;
    when ``None`` the field defaults to ``False``.
    """
    try:
        payment_id = str(raw.get("external_payment_id") or "")
        user_id = int(raw["user_id"])
        months = int(raw["months"])
        price = float(raw["price"])
        action_raw = str(raw["action"]).strip().lower()
        key_id = int(raw["key_id"])
        host_name = str(raw["host_name"])
        plan_id = int(raw["plan_id"])
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"metadata_parse_failed:{type(exc).__name__}: {exc}") from exc

    try:
        action = IssuanceAction(action_raw)
    except ValueError:
        action = IssuanceAction.NEW

    is_super = False
    if callable(is_super_flow_fn):
        try:
            is_super = bool(is_super_flow_fn(
                host_name=host_name,
                is_super_key_flow=raw.get("is_super_key_flow"),
                plan_data={"plan_id": plan_id},
            ))
        except Exception:
            is_super = False

    return PaymentMeta(
        payment_id=payment_id,
        user_id=user_id,
        months=months,
        price=price,
        action=action,
        key_id=key_id,
        host_name=host_name,
        plan_id=plan_id,
        plan_name=str(raw.get("plan_name") or ""),
        customer_email=str(raw.get("customer_email") or ""),
        payment_method=str(raw.get("payment_method") or ""),
        payment_confirmed=_coerce_bool(raw.get("payment_confirmed")),
        is_super_flow=is_super,
        retry_source=raw.get("retry_source"),
        trace_id=raw.get("trace_id"),
    )


def check_payment_confirmed(
    meta: PaymentMeta,
    *,
    tx_status: str = "",
) -> bool:
    """Return ``True`` when the payment is confirmed for issuance.

    The gate passes when **either** the DB transaction status is ``paid``
    **or** the provider already flagged ``payment_confirmed=True``.
    """
    if tx_status == "paid":
        return True
    return meta.payment_confirmed


def is_fatal_precheck_reason(reason: str | None) -> bool:
    """Return ``True`` if *reason* means the transaction should not be retried."""
    return (reason or "") in FATAL_PRECHECK_REASONS


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False
