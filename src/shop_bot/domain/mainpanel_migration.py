from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from shop_bot.modules import xui_api

STATUS_WILL_MIGRATE = "will_migrate"
STATUS_ALREADY_MIGRATED = "already_migrated"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_SKIPPED_EXPIRED = "skipped_expired"
STATUS_SKIPPED_WRONG_TIER = "skipped_wrong_tier"
STATUS_SKIPPED_TARGET_HOST = "skipped_target_host"

_SUPER_HOST_MARKERS = ("super", "супер")
_STANDARD_HOST_MARKERS = ("standart", "standard", "стандарт")


def parse_bonus_days(raw_value: Any, *, default: int = 0) -> int:
    try:
        parsed = int(raw_value if raw_value is not None else default)
    except (TypeError, ValueError) as exc:
        raise ValueError("Подарочные дни должны быть целым числом") from exc
    if parsed < 0 or parsed > 365:
        raise ValueError("Подарочные дни должны быть в диапазоне 0..365")
    return parsed


def parse_key_expiry(raw_value: Any) -> datetime | None:
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return None
    try:
        parsed = datetime.fromisoformat(raw_text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone().replace(tzinfo=None)
    return parsed


def normalize_metadata(raw_metadata: Any) -> dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return dict(raw_metadata)
    if not isinstance(raw_metadata, str):
        return {}
    try:
        parsed = json.loads(raw_metadata)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def metadata_key_id(metadata: dict[str, Any]) -> int | None:
    raw_key_id = metadata.get("key_id") or metadata.get("vpn_key_id")
    try:
        parsed = int(str(raw_key_id).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _metadata_bool(raw_value: Any) -> bool | None:
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def _tier_from_text(raw_value: Any) -> str | None:
    normalized = str(raw_value or "").strip().lower()
    if not normalized:
        return None
    if any(marker in normalized for marker in _SUPER_HOST_MARKERS):
        return xui_api.SUBSCRIPTION_TIER_SUPER
    if any(marker in normalized for marker in _STANDARD_HOST_MARKERS):
        return xui_api.SUBSCRIPTION_TIER_STANDARD
    return None


def legacy_host_tier(host_name: Any, *, main_panel_host_name: str) -> str | None:
    normalized_host = str(host_name or "").strip()
    if not normalized_host or normalized_host == str(main_panel_host_name or "").strip():
        return None
    return _tier_from_text(normalized_host)


def _tier_from_metadata(metadata: dict[str, Any], *, main_panel_host_name: str) -> tuple[str | None, str | None]:
    explicit_tier = xui_api.normalize_subscription_tier(metadata.get("subscription_tier"))
    if explicit_tier:
        return explicit_tier, "transactions.metadata.subscription_tier"

    is_super = _metadata_bool(metadata.get("is_super_key_flow"))
    if is_super is True:
        return xui_api.SUBSCRIPTION_TIER_SUPER, "transactions.metadata.is_super_key_flow"
    if is_super is False:
        return xui_api.SUBSCRIPTION_TIER_STANDARD, "transactions.metadata.is_super_key_flow"

    plan_tier = _tier_from_text(metadata.get("plan_name") or metadata.get("tariff_name"))
    if plan_tier:
        return plan_tier, "transactions.metadata.plan_name"

    host_tier = legacy_host_tier(metadata.get("host_name"), main_panel_host_name=main_panel_host_name)
    if host_tier:
        return host_tier, "transactions.metadata.host_name"

    return None, None


def resolve_key_tier(
    key: dict[str, Any],
    transaction_rows: list[dict[str, Any]],
    *,
    main_panel_host_name: str,
) -> tuple[str | None, str | None, str | None]:
    explicit_tier = xui_api.normalize_subscription_tier(key.get("subscription_tier"))
    if explicit_tier:
        return explicit_tier, "vpn_keys.subscription_tier", None

    key_id = int(key.get("key_id") or 0)
    exact_rows: list[dict[str, Any]] = []
    fallback_rows: list[dict[str, Any]] = []
    for row in transaction_rows:
        metadata = normalize_metadata(row.get("metadata_dict") or row.get("metadata"))
        if metadata_key_id(metadata) == key_id:
            exact_rows.append(row)
        else:
            fallback_rows.append(row)

    for row in exact_rows + fallback_rows:
        tier, source = _tier_from_metadata(
            normalize_metadata(row.get("metadata_dict") or row.get("metadata")),
            main_panel_host_name=main_panel_host_name,
        )
        if tier:
            return tier, source, None

    host_tier = legacy_host_tier(key.get("host_name"), main_panel_host_name=main_panel_host_name)
    if host_tier:
        return host_tier, "vpn_keys.host_name", None

    return None, None, "unknown_tier"


def build_migration_item(
    key: dict[str, Any],
    *,
    transaction_rows: list[dict[str, Any]],
    target_tier: str,
    main_panel_host_name: str,
    bonus_days: int,
    incident_id: str | None,
    existing_migration: dict[str, Any] | None = None,
    replacement_key: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or datetime.now(UTC).replace(tzinfo=None)
    key_id = int(key.get("key_id") or 0)
    old_host = str(key.get("host_name") or "").strip()
    item = {
        "user_id": int(key.get("user_id") or 0),
        "key_id": key_id,
        "key_email": str(key.get("key_email") or "").strip(),
        "old_host": old_host,
        "new_host": main_panel_host_name,
        "bonus_days": bonus_days,
        "incident_id": incident_id or "",
        "status": STATUS_NEEDS_REVIEW,
        "_target_sub_id": str(key.get("panel_sub_id") or "").strip(),
        "_bot_source": str(key.get("bot_source") or "main").strip() or "main",
    }

    if (existing_migration and existing_migration.get("status") == "migrated") or replacement_key:
        item.update(
            {
                "status": STATUS_ALREADY_MIGRATED,
                "reason": "already_migrated",
                "new_key_id": (replacement_key or existing_migration or {}).get("key_id")
                or (existing_migration or {}).get("new_key_id"),
            }
        )
        return item

    expiry_date = parse_key_expiry(key.get("expiry_date"))
    if expiry_date is None:
        item.update({"status": STATUS_NEEDS_REVIEW, "reason": "invalid_expiry"})
        return item

    remaining_seconds = max((expiry_date - current_time).total_seconds(), 0)
    remaining_days = round(remaining_seconds / 86400, 1)
    new_expiry = expiry_date + timedelta(days=bonus_days)
    item.update(
        {
            "old_expiry": expiry_date.isoformat(),
            "remaining_days": remaining_days,
            "new_expiry": new_expiry.isoformat(),
            "target_expiry_ms": int(new_expiry.timestamp() * 1000),
        }
    )

    if expiry_date <= current_time:
        item.update({"status": STATUS_SKIPPED_EXPIRED, "reason": "expired"})
        return item

    resolved_tier, tier_source, tier_reason = resolve_key_tier(
        key,
        transaction_rows,
        main_panel_host_name=main_panel_host_name,
    )
    item.update({"resolved_tier": resolved_tier or "", "tier_source": tier_source or ""})
    if not resolved_tier:
        item.update({"status": STATUS_NEEDS_REVIEW, "reason": tier_reason or "unknown_tier"})
        return item

    if old_host == str(main_panel_host_name or "").strip():
        item.update({"status": STATUS_SKIPPED_TARGET_HOST, "reason": "already_on_mainpanel"})
        return item

    if resolved_tier != target_tier:
        item.update({"status": STATUS_SKIPPED_WRONG_TIER, "reason": "wrong_tier"})
        return item

    item.update({"status": STATUS_WILL_MIGRATE, "reason": "ready"})
    return item


def summarize_items(items: list[dict[str, Any]]) -> dict[str, int]:
    summary = {
        "found": len(items),
        "total": len(items),
        "will_migrate": 0,
        "migrated": 0,
        "already_migrated": 0,
        "skipped": 0,
        "skipped_expired": 0,
        "skipped_wrong_tier": 0,
        "needs_review": 0,
        "failed": 0,
        "errors": 0,
    }
    for item in items:
        status = str(item.get("status") or "")
        if status == STATUS_WILL_MIGRATE:
            summary["will_migrate"] += 1
        elif status == "migrated":
            summary["migrated"] += 1
        elif status == STATUS_ALREADY_MIGRATED:
            summary["already_migrated"] += 1
            summary["skipped"] += 1
        elif status == STATUS_SKIPPED_EXPIRED:
            summary["skipped_expired"] += 1
            summary["skipped"] += 1
        elif status == STATUS_SKIPPED_WRONG_TIER:
            summary["skipped_wrong_tier"] += 1
            summary["skipped"] += 1
        elif status == STATUS_NEEDS_REVIEW:
            summary["needs_review"] += 1
            summary["skipped"] += 1
        elif status in {STATUS_SKIPPED_TARGET_HOST, "skipped_claimed", "skipped_trial"}:
            summary["skipped"] += 1
        elif status in {"error", "failed"}:
            summary["failed"] += 1
            summary["errors"] += 1
    return summary
