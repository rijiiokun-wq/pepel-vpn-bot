"""Customer-facing host visibility policy.

The setting is intentionally an allowlist and affects presentation only.  Raw
key/host rows stay available to billing, renewal, support, and rollback flows.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Collection, Iterable, Mapping
from typing import TypeVar, cast

from shop_bot.data_manager.database import get_setting

logger = logging.getLogger(__name__)

CUSTOMER_VISIBLE_HOST_NAMES_SETTING = "customer_visible_host_names"

_UNSET = object()
_Item = TypeVar("_Item")


def _normalize_host_name(value: object) -> str:
    return str(value or "").strip().casefold()


def parse_customer_visible_host_names(value: object) -> frozenset[str] | None:
    """Parse the customer-visible host allowlist.

    ``None`` means that filtering is disabled.  Missing, blank, or malformed
    values deliberately fail open so removing the setting is a complete and
    immediate rollback.  JSON is canonical; comma/newline-separated text is
    accepted for operational convenience.
    """

    if value is None:
        return None
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            logger.warning(
                "Non-string %s setting entries; customer host filtering is disabled.",
                CUSTOMER_VISIBLE_HOST_NAMES_SETTING,
            )
            return None
        candidates = value
        raw_value = json.dumps(value)
    else:
        raw_value = str(value).strip()
        if not raw_value:
            return None

    looks_like_json = raw_value.startswith(("[", "{", '"')) or raw_value in {"null", "true", "false"}
    if not isinstance(value, list) and looks_like_json:
        try:
            parsed = json.loads(raw_value)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Invalid %s setting; customer host filtering is disabled.",
                CUSTOMER_VISIBLE_HOST_NAMES_SETTING,
            )
            return None
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            logger.warning(
                "Non-string %s setting entries; customer host filtering is disabled.",
                CUSTOMER_VISIBLE_HOST_NAMES_SETTING,
            )
            return None
        candidates = parsed
    elif not isinstance(value, list):
        candidates = re.split(r"[,\n]", raw_value)

    unique_names: dict[str, str] = {}
    for candidate in candidates:
        display_name = str(candidate or "").strip()
        normalized_name = _normalize_host_name(display_name)
        if normalized_name and normalized_name not in unique_names:
            unique_names[normalized_name] = display_name
    return frozenset(unique_names.values())


def get_customer_visible_host_names() -> frozenset[str] | None:
    """Return configured visible hosts, or ``None`` when all are visible."""

    return parse_customer_visible_host_names(get_setting(CUSTOMER_VISIBLE_HOST_NAMES_SETTING))


def _resolve_visible_host_names(
    visible_host_names: Collection[str] | None | object,
) -> Collection[str] | None:
    if visible_host_names is _UNSET:
        return get_customer_visible_host_names()
    if isinstance(visible_host_names, str):
        return parse_customer_visible_host_names(visible_host_names)
    return cast("Collection[str] | None", visible_host_names)


def is_customer_host_visible(
    host_name: object,
    visible_host_names: Collection[str] | None | object = _UNSET,
) -> bool:
    """Return whether a host may appear on customer-facing surfaces."""

    resolved_names = _resolve_visible_host_names(visible_host_names)
    if resolved_names is None:
        return True
    normalized_host_name = _normalize_host_name(host_name)
    if not normalized_host_name:
        return False
    return normalized_host_name in {_normalize_host_name(name) for name in resolved_names}


def _item_host_name(item: object) -> object:
    if isinstance(item, Mapping):
        return item.get("host_name")
    return item


def _filter_customer_visible_items(
    items: Iterable[_Item],
    visible_host_names: Collection[str] | None | object,
) -> list[_Item]:
    resolved_names = _resolve_visible_host_names(visible_host_names)
    if resolved_names is None:
        return list(items)
    normalized_names = {_normalize_host_name(name) for name in resolved_names}
    return [
        item
        for item in items
        if _normalize_host_name(_item_host_name(item)) in normalized_names
    ]


def filter_customer_visible_keys(
    keys: Iterable[_Item],
    visible_host_names: Collection[str] | None | object = _UNSET,
) -> list[_Item]:
    """Filter key rows for customer presentation without changing their state."""

    return _filter_customer_visible_items(keys, visible_host_names)


def filter_customer_visible_hosts(
    hosts: Iterable[_Item],
    visible_host_names: Collection[str] | None | object = _UNSET,
) -> list[_Item]:
    """Filter host rows/options for customer-facing selectors."""

    return _filter_customer_visible_items(hosts, visible_host_names)
