"""Reversible retirement of subscription nodes; never deletes panel/client data.

Only explicitly retired IDs are excluded. Newly verified nodes keep the usual
MainPanel discovery behavior. The legacy primary inbound may remain an internal
identity anchor, but its link is never published while retired.
"""

import json
import re
from urllib.parse import unquote, urlparse

from shop_bot.data_manager import database

SETTING_KEY = "subscription_retired_nodes"
MIGRATION_SETTING_KEY = "subscription_inbound_migrations"


def _migration_policies() -> dict:
    raw = database.get_setting(MIGRATION_SETTING_KEY)
    if not raw:
        return {}
    policies = json.loads(raw)
    if not isinstance(policies, dict):
        raise ValueError("invalid_subscription_inbound_migrations")
    return policies


def inbound_migrations(host_name: str) -> list[dict]:
    """Explicit replacements only; malformed activated policy fails closed."""
    # This rollout is scoped to MainPanel. A corrupt or partial rollout setting
    # must not affect independent panels, even while they issue new clients.
    if str(host_name or "").strip() != "MainPanel":
        return []
    records = _migration_policies().get("MainPanel", [])
    if not isinstance(records, list):
        raise ValueError("invalid_subscription_inbound_migrations")
    old_ids, new_ids = set(), set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("invalid_subscription_inbound_migration")
        old_id = record.get("old_inbound_id")
        new_id = record.get("new_inbound_id")
        if (
            type(old_id) is not int or type(new_id) is not int
            or min(old_id, new_id) <= 0 or old_id == new_id
            or old_id in old_ids or new_id in new_ids
            or not str(record.get("old_label") or "").strip()
            or not str(record.get("new_label") or "").strip()
            or type(record.get("staged", False)) is not bool
        ):
            raise ValueError("invalid_subscription_inbound_migration")
        old_ids.add(old_id)
        new_ids.add(new_id)
    if old_ids & new_ids:
        raise ValueError("chained_subscription_inbound_migration")
    return records


def replaced_inbound_ids(host_name: str) -> frozenset[int]:
    return frozenset(item["old_inbound_id"] for item in inbound_migrations(host_name) if not item.get("staged", False))


def migration_excluded_inbound_ids(host_name: str) -> frozenset[int]:
    """Hide the new target during staging, then the old source after activation."""
    return frozenset(
        item["new_inbound_id"] if item.get("staged", False) else item["old_inbound_id"]
        for item in inbound_migrations(host_name)
    )


def migration_for_target(host_name: str, inbound_id: int, label: str) -> dict | None:
    for record in inbound_migrations(host_name):
        if record["new_inbound_id"] != int(inbound_id):
            continue
        if str(label or "").strip() != record["new_label"]:
            raise ValueError("subscription_migration_target_label_mismatch")
        return record
    return None


def client_identity_suffix(host_name: str, inbound_id: int, label: str) -> str:
    record = migration_for_target(host_name, inbound_id, label)
    if record:
        return f"{record['old_label']}-{record['old_inbound_id']}"
    return f"{label}-{inbound_id}"


def clearway_client_profile(inbound_id: int, label: str, address: str) -> dict | None:
    """The complete endpoint identity scopes transport overrides, not a name match."""
    if (
        str(label or "").strip() != "WHITELIST2"
        or not str(address or "").strip()
    ):
        return None
    matches = []
    for record in inbound_migrations("MainPanel"):
        if (
            record["new_inbound_id"] == int(inbound_id)
            and record["new_label"] == str(label or "").strip()
            and record.get("client_profile") == "clearway"
        ):
            if record.get("client_address") != str(address or "").strip():
                raise ValueError("clearway_client_address_mismatch")
            matches.append(record)
    if len(matches) > 1:
        raise ValueError("ambiguous_clearway_client_profile")
    return matches[0] if matches else None


def get_policy(host_name: str) -> dict:
    raw = database.get_setting(SETTING_KEY)
    if not raw:
        return {}
    try:
        policies = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(policies, dict):
        return {}
    policy = policies.get(str(host_name or "").strip(), {})
    return policy if isinstance(policy, dict) else {}


def retired_inbound_ids(host_name: str) -> frozenset[int]:
    values = get_policy(host_name).get("inbound_ids", [])
    return frozenset(int(value) for value in values if str(value).isdigit())


def _label_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def filter_subscription_payload(
    host_name: str, payload: str | None, tier: str | None = None, *, expected_labels=None
) -> str | None:
    _ = tier  # Traffic quotas now distinguish paid tiers; node publication is shared.
    if not payload:
        return None
    policy = get_policy(host_name)
    migrations = inbound_migrations(host_name)
    if not policy and not migrations:
        return payload
    retired_labels = {_label_key(str(label)) for label in policy.get("labels", [])}
    retired_addresses = {str(address).strip().casefold() for address in policy.get("addresses", [])}
    for migration in migrations:
        staged = migration.get("staged", False)
        retired_labels.add(_label_key(migration["new_label"] if staged else migration["old_label"]))
        if staged and migration.get("client_address"):
            retired_addresses.add(str(migration["client_address"]).strip().casefold())
    expected = {_label_key(str(label)) for label in expected_labels} if expected_labels is not None else None
    retained = []
    for line in payload.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = urlparse(line)
            address = str(parsed.hostname or "").casefold()
        except ValueError:
            continue
        label = _label_key(unquote(parsed.fragment))
        if address in retired_addresses or label in retired_labels:
            continue
        if expected is not None and label not in expected:
            continue
        retained.append(line)
    return "\n".join(retained) or None
