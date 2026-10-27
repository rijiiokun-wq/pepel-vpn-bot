from __future__ import annotations

from collections.abc import Mapping

VALID_BOT_SOURCES = frozenset({"main", "reserve"})

REQUIRED_PAYMENT_METADATA_FIELDS = (
    "user_id",
    "months",
    "price",
    "action",
    "key_id",
    "host_name",
    "plan_id",
)


def normalize_bot_source(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in VALID_BOT_SOURCES:
        return normalized
    return None


def normalize_payment_metadata(raw_metadata: object) -> dict:
    if isinstance(raw_metadata, dict):
        return dict(raw_metadata)
    if isinstance(raw_metadata, Mapping):
        return dict(raw_metadata.items())
    return {}


def merge_payment_metadata(preferred_metadata: object, fallback_metadata: object) -> dict:
    merged = normalize_payment_metadata(preferred_metadata)
    fallback = normalize_payment_metadata(fallback_metadata)
    for key, value in fallback.items():
        if key not in merged or merged.get(key) in (None, ""):
            merged[key] = value
    return merged


def missing_required_payment_metadata_fields(metadata: object) -> list[str]:
    normalized_metadata = normalize_payment_metadata(metadata)
    return [
        field
        for field in REQUIRED_PAYMENT_METADATA_FIELDS
        if normalized_metadata.get(field) in (None, "")
    ]


def metadata_bot_source_matches_runtime(metadata: object, runtime_bot_source: object) -> bool:
    metadata_source = normalize_bot_source(normalize_payment_metadata(metadata).get("bot_source"))
    runtime_source = normalize_bot_source(runtime_bot_source)
    if not metadata_source or not runtime_source:
        return True
    return metadata_source == runtime_source
