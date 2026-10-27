"""Resume only proved missing components of a claimed paid Standard birth.

The caller owns the payment lock and persists the journal before any POST.
Existing components are read-only; every uncertain add is resolved by reads.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping

from requests.exceptions import RequestException

from shop_bot.modules import xui_api as x

logger = logging.getLogger(__name__)
_PUBLIC = (22, 23, 24, 25)
_LABELS = {
    22: "ROMANIA Hysteria2",
    23: "ROMANIA2 Hysteria2",
    24: "NEW3-IP10 VLESS TCP REALITY Vision-Mozilla-FF",
    25: "WHITELIST2",
}
_ERRORS = (
    RequestException,
    RuntimeError,
    TimeoutError,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
)


class RecoveryBlocked(ValueError):
    pass


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise RecoveryBlocked(reason)


def _uuid(row: Mapping) -> str:
    return x._normalized_client_uuid(row.get("uuid") or row.get("id"))


def _clients(raw: Mapping) -> list:
    rows = x._raw_settings_mapping(raw.get("settings")).get("clients")
    _require(
        isinstance(rows, list) and all(isinstance(r, Mapping) for r in rows),
        "client_inventory_invalid",
    )
    return rows


def _owned(rows: list, email: str, uid: str) -> list:
    return [
        r
        for r in rows
        if _uuid(r) == uid or x._subscription_email_variant(email, r.get("email", ""))
    ]


def _transport(raw: Mapping) -> str:
    fields = {
        key: raw.get(key)
        for key in (
            "enable",
            "listen",
            "port",
            "protocol",
            "tag",
            "nodeId",
            "remark",
            "sniffing",
            "streamSettings",
        )
    }
    fields["settings"] = {
        key: value
        for key, value in x._raw_settings_mapping(raw.get("settings")).items()
        if key != "clients"
    }
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _identity(client: Mapping) -> dict:
    result = {
        key: client.get(key)
        for key in (
            "email",
            "subId",
            "enable",
            "expiryTime",
            "totalGB",
            "reset",
            "auth",
            "password",
            "limitIp",
            "tgId",
            "security",
        )
    }
    result.update(id=_uuid(client), flow=client.get("flow") or "")
    return result


def _credential(client: Mapping) -> str:
    values = [client.get(key) for key in ("auth", "password")]
    _require(
        all(value is None or isinstance(value, str) for value in values),
        "credential_type_invalid",
    )
    populated = {value for value in values if value}
    _require(len(populated) <= 1, "credential_conflict")
    return next(iter(populated), "")


def _central_policy_matches(row: Mapping, client: Mapping, iid: int) -> None:
    # The central record is authoritative. A stale projected inbound must not
    # authorize a new component after policy, identity or credential changes.
    _require(
        row.get("enable") is True and client.get("enable") is True,
        "central_enable_invalid",
    )
    for field in ("expiryTime", "totalGB", "reset", "limitIp", "tgId"):
        left, right = row.get(field), client.get(field)
        _require(
            type(left) is int and type(right) is int and left >= 0 and left == right,
            "central_policy_mismatch",
        )
    for field in ("flow", "security"):
        left, right = row.get(field), client.get(field)
        left = "" if left is None else left
        right = "" if right is None else right
        _require(
            isinstance(left, str) and isinstance(right, str) and left == right,
            "central_transport_credential_mismatch",
        )
    credential = _credential(client)
    _require(
        _credential(row) == credential and (iid not in (22, 23) or bool(credential)),
        "central_credential_mismatch",
    )


def _native_target(api, master: Mapping):
    target = x._resolve_whitelist2_native_child_target(api, master, timeout_seconds=4)
    if target is not None:
        return target
    # Some registered nodes intentionally have no separate shop host record.
    # Use only the fresh authenticated panel registration, never caller input.
    nodes = x._api_extract_obj(
        x._api_get_json(api, "panel/api/nodes/list", skip_check=True, timeout=4)
    )
    _require(
        isinstance(nodes, list) and all(isinstance(n, Mapping) for n in nodes),
        "node_inventory_invalid",
    )
    matches = [n for n in nodes if n.get("id") == master.get("nodeId")]
    _require(
        len(matches) == 1 and matches[0].get("enable") is True,
        "node_registration_invalid",
    )
    node = matches[0]
    token = node.get("apiToken")
    _require(
        isinstance(token, str)
        and len(token.strip()) >= 16
        and not any(
            part in token.lower() for part in ("redacted", "masked", "hidden", "...")
        )
        and not set(token) <= set("*xX• "),
        "node_token_unavailable",
    )
    address, port = node.get("address"), node.get("port", 443)
    _require(
        isinstance(address, str)
        and address
        and type(port) is int
        and 0 < port <= 65535,
        "node_endpoint_invalid",
    )
    address = "[" + address + "]" if ":" in address else address
    url = f"{node.get('scheme') or 'https'}://{address}:{port}{x._normalize_node_base_path(node.get('basePath'))}"
    mode = node.get("tlsVerifyMode") or "verify"
    _require(
        x._whitelist2_native_endpoint(url) is not None and mode in {"verify", "skip"},
        "node_tls_policy_unsupported",
    )
    return url + "panel/api/inbounds/list", token.strip(), mode == "verify"


def _native(api, master: Mapping) -> Mapping:
    target = _native_target(api, master)
    _require(target is not None, "native_target_unavailable")
    url, token, verify = target
    rows = x._api_extract_obj(
        x._whitelist2_read_json_once(
            url, api_token=token, tls_verify=verify, timeout_seconds=4
        )
    )
    _require(
        isinstance(rows, list) and all(isinstance(r, Mapping) for r in rows),
        "native_inventory_invalid",
    )
    matches = [r for r in rows if r.get("tag") == master.get("tag")]
    _require(len(matches) == 1, "native_inbound_not_unique")
    child = matches[0]
    for field in (
        "enable",
        "listen",
        "port",
        "protocol",
        "tag",
        "streamSettings",
        "sniffing",
    ):
        left, right = master.get(field), child.get(field)
        if field in {"streamSettings", "sniffing"}:
            left = json.loads(left) if isinstance(left, str) and left else left
            right = json.loads(right) if isinstance(right, str) and right else right
        _require(left == right, "native_transport_mismatch")
    return child


def _snapshot(api, primary_id: int, email: str, expiry: int, anchor: int) -> dict:
    """Complete owned namespace + native public components, no writes."""
    inventory = x._api_extract_obj(
        x._api_get_json(api, "panel/api/inbounds/list", skip_check=True, timeout=4)
    )
    _require(
        isinstance(inventory, list)
        and all(isinstance(r, Mapping) and type(r.get("id")) is int for r in inventory),
        "master_inventory_invalid",
    )
    _require(
        len({r["id"] for r in inventory}) == len(inventory),
        "master_inventory_duplicate",
    )
    raws = {r["id"]: r for r in inventory}
    for iid in (primary_id, *_PUBLIC):
        raws[iid] = x._get_inbound_raw_by_id(
            api, iid, allow_list_fallback=False, request_timeout=4
        )
        _require(raws[iid].get("id") == iid, "master_inbound_mismatch")
    primary_proof = x._pending_standard_component_proof(
        raws[primary_id],
        email=email,
        client_uuid=None,
        expiry_ms=expiry,
        total_bytes=0,
        reset_days=0,
        primary=True,
    )
    _require(len(primary_proof) == 1, "primary_identity_missing_or_ambiguous")
    primary = primary_proof[0][0]
    uid = _uuid(primary)
    _require(str(x.uuid.UUID(uid)) == uid.lower(), "primary_uuid_invalid")
    children = {iid: _native(api, raws[iid]) for iid in _PUBLIC}
    now = int(time.time() * 1000)
    adopted = anchor
    white = _owned(_clients(raws[25]), email, uid)
    if (
        len(white) == 1
        and white[0].get("expiryTime")
        != x._next_whitelist_reset_deadline_ms(anchor, None, expiry, now)[0]
    ):
        # The old LAVA path clamped its future wall-clock anchor at birth. Adopt
        # only that exact initial native cycle, never a later quota reset.
        deadline = white[0].get("expiryTime")
        created = primary.get("created_at")
        start = deadline - x.WHITELIST_RESET_PERIOD_MS if type(deadline) is int else 0
        _require(
            type(created) is int
            and 0 < start <= now
            and now - start < x.MILLISECONDS_PER_DAY
            and abs(start - created) <= 120_000
            and 0 < anchor - start <= 6 * 3_600_000
            and deadline < expiry
            and white[0].get("reset") == 30,
            "cycle_anchor_not_proven",
        )
        adopted = start
    _require(0 < adopted <= now, "cycle_anchor_future")
    white_expiry, _ = x._next_whitelist_reset_deadline_ms(adopted, None, expiry, now)
    white_reset = 0 if white_expiry >= expiry else 30
    expected, existing, missing = {}, {}, []
    for iid, raw in raws.items():
        runtime = x._raw_inbound_runtime_namespace(raw, inbound_id=iid)
        alias = (
            email
            if iid == primary_id
            else x._secondary_inbound_email(email, runtime, host_name="MainPanel")
        )
        expected[iid] = alias
        found = _owned(_clients(raw), email, uid)
        if not found and iid not in _PUBLIC:
            continue
        _require(
            not found
            or (
                len(found) == 1
                and found[0].get("email") == alias
                and _uuid(found[0]) == uid
            ),
            "component_identity_collision",
        )
        total, deadline, reset = (
            (x.STANDARD_WHITELIST_TRAFFIC_BYTES, white_expiry, white_reset)
            if iid == 25
            else (0, expiry, 0)
        )
        proof = x._pending_standard_component_proof(
            raw,
            email=email,
            client_uuid=uid,
            expiry_ms=deadline,
            total_bytes=total,
            reset_days=reset,
            primary=iid == primary_id,
        )
        if found:
            _require(len(proof) == 1, "component_policy_invalid")
            existing[str(iid)] = {
                "client": _identity(proof[0][0]),
                "up": proof[0][1]["up"],
                "down": proof[0][1]["down"],
            }
        else:
            stats = raw.get("clientStats")
            _require(
                isinstance(stats, list)
                and not any(
                    x._subscription_email_variant(email, r.get("email", ""))
                    for r in stats
                ),
                "orphan_component_stats",
            )
            missing.append(iid)
        if iid in _PUBLIC:
            _require(
                raw.get("enable") is True and x._inbound_label(runtime) == _LABELS[iid],
                "public_topology_changed",
            )
            if iid == 24:
                _require(
                    runtime.protocol == "vless"
                    and x._stream_settings_value(runtime, "network", "") == "tcp"
                    and x._stream_settings_value(runtime, "security", "") == "reality"
                    and runtime.stream_settings.reality_settings.get(
                        "settings", {}
                    ).get("fingerprint")
                    == "firefox",
                    "reality_topology_changed",
                )
            elif iid == 25:
                endpoint = x._xhttp_external_proxy_endpoint(runtime)
                _require(
                    raw.get("tag") == "whitelist2-clearway-xhttp"
                    and raw.get("nodeId") == 14
                    and endpoint is not None
                    and x.subscription_policy.clearway_client_profile(
                        25, "WHITELIST2", endpoint[0]
                    ),
                    "white_topology_changed",
                )
            else:
                protocol = raw.get("protocol")
                network = x._stream_settings_value(runtime, "network", "")
                hysteria = x._as_mapping(
                    x._stream_settings_value(runtime, "hysteria_settings", {})
                )
                canonical = protocol == "hysteria2" and network == "hysteria2"
                legacy = (
                    protocol == "hysteria"
                    and network == "hysteria"
                    and type(hysteria.get("version")) is int
                    and hysteria["version"] == 2
                )
                _require(
                    (canonical or legacy)
                    and x._stream_settings_value(runtime, "security", "") == "tls"
                    and (
                        "version" not in hysteria
                        or (
                            type(hysteria["version"]) is int
                            and hysteria["version"] == 2
                        )
                    ),
                    "hysteria_topology_changed",
                )
            if found:
                if iid in (22, 23):
                    _require(
                        bool(
                            str(
                                x._client_config_value(
                                    found[0], ("auth", "password"), ""
                                )
                                or ""
                            ).strip()
                        ),
                        "hysteria_credential_missing",
                    )
                else:
                    _require(
                        (found[0].get("flow") or "")
                        == ("xtls-rprx-vision" if iid == 24 else ""),
                        "public_client_flow_invalid",
                    )
            child = children[iid]
            child_found = _owned(_clients(child), email, uid)
            _require(len(child_found) == len(found), "native_component_pending")
            if found:
                child_proof = x._pending_standard_component_proof(
                    child,
                    email=email,
                    client_uuid=uid,
                    expiry_ms=deadline,
                    total_bytes=total,
                    reset_days=reset,
                )
                _require(
                    len(child_proof) == 1
                    and _identity(child_proof[0][0]) == _identity(found[0]),
                    "native_identity_mismatch",
                )
            else:
                stats = child.get("clientStats")
                _require(
                    isinstance(stats, list)
                    and not any(
                        x._subscription_email_variant(email, r.get("email", ""))
                        for r in stats
                    ),
                    "native_orphan_stats",
                )
    central = x._api_extract_obj(
        x._api_get_json(api, "panel/api/clients/list", skip_check=True, timeout=4)
    )
    central = central.get("clients") if isinstance(central, Mapping) else central
    _require(
        isinstance(central, list) and all(isinstance(r, Mapping) for r in central),
        "central_inventory_invalid",
    )
    central_owned = _owned(central, email, uid)
    _require(
        len({r.get("email") for r in central_owned}) == len(central_owned),
        "central_duplicate",
    )
    aliases = {row["client"]["email"]: int(iid) for iid, row in existing.items()}
    _require(
        {r.get("email") for r in central_owned} == set(aliases),
        "central_component_pending_or_collision",
    )
    for row in central_owned:
        iid = aliases[row["email"]]
        client = existing[str(iid)]["client"]
        _require(
            _uuid(row) == uid
            and row.get("subId") == client["subId"]
            and isinstance(row.get("inboundIds"), list)
            and all(type(value) is int for value in row["inboundIds"])
            and row["inboundIds"] == [iid],
            "central_identity_mismatch",
        )
        _central_policy_matches(row, client, iid)
    return {
        "uuid": uid,
        "primary_sid": x._extract_client_sub_id(primary),
        "anchor_ms": adopted,
        "expiry_ms": expiry,
        "email": email,
        "primary_id": primary_id,
        "existing": existing,
        "missing": missing,
        "expected": expected,
        "raws": raws,
        "primary": primary,
        "white_expiry": white_expiry,
        "white_reset": white_reset,
        "transport": {str(iid): _transport(raw) for iid, raw in raws.items()},
    }


def _plan(snapshot: dict, saved: Mapping | None) -> dict:
    identity = {
        k: snapshot[k]
        for k in (
            "uuid",
            "primary_sid",
            "anchor_ms",
            "expiry_ms",
            "email",
            "primary_id",
        )
    }
    if saved:
        _require(
            isinstance(saved, Mapping) and saved.get("version") == 1, "journal_invalid"
        )
        _require(
            saved.get("identity") == identity
            and saved.get("transport") == snapshot["transport"],
            "journal_identity_or_transport_drift",
        )
        plan = copy.deepcopy(dict(saved))
        for iid, before in plan["existing"].items():
            now = snapshot["existing"].get(iid)
            _require(
                now is not None
                and now["client"] == before["client"]
                and now["up"] >= before["up"]
                and now["down"] >= before["down"],
                "existing_component_changed",
            )
        for iid, intent in plan["components"].items():
            now = snapshot["existing"].get(iid)
            if now:
                _require(
                    all(
                        now["client"].get(k) == v
                        for k, v in intent["client"].items()
                        if k in now["client"]
                    ),
                    "planned_component_mismatch",
                )
        _require(
            set(snapshot["missing"]) <= {int(iid) for iid in plan["components"]},
            "unplanned_missing_component",
        )
        return plan
    plan = {
        "version": 1,
        "identity": identity,
        "transport": snapshot["transport"],
        "existing": snapshot["existing"],
        "components": {},
    }
    for iid in snapshot["missing"]:
        _require(iid in _PUBLIC, "repair_scope_invalid")
        raw = snapshot["raws"][iid]
        runtime = x._raw_inbound_runtime_namespace(raw, inbound_id=iid)
        client = {
            "id": snapshot["uuid"],
            "email": snapshot["expected"][iid],
            "subId": x._generate_sub_id(),
            "enable": True,
            "expiryTime": snapshot["white_expiry"]
            if iid == 25
            else snapshot["expiry_ms"],
            "totalGB": x.STANDARD_WHITELIST_TRAFFIC_BYTES if iid == 25 else 0,
            "reset": snapshot["white_reset"] if iid == 25 else 0,
            "flow": "xtls-rprx-vision" if iid == 24 else "",
            "limitIp": snapshot["primary"].get("limitIp", 0),
            "tgId": snapshot["primary"].get("tgId", 0),
        }
        if x._is_hysteria_subscription_inbound(runtime):
            secret = x._generate_hysteria_secret()
            client.update(auth=secret, password=secret, security="auto")
        plan["components"][str(iid)] = {
            "client": client,
            "attempts": 0,
            "last_attempt_at_ms": 0,
        }
    return plan


def _resume_once(
    host_name: str,
    email: str,
    *,
    target_expiry_ms: int,
    whitelist_cycle_anchor_ms: int,
    journal: Mapping | None,
    persist: Callable[[dict], bool],
) -> dict | None:
    component = 0
    stage = "preflight"
    try:
        _require(
            host_name == "MainPanel"
            and re.fullmatch(r"user\d+-key[1-9]\d*@vpn\.bot", email) is not None
            and type(target_expiry_ms) is int
            and target_expiry_ms > int(time.time() * 1000),
            "scope_invalid",
        )
        _require(not x.get_key_by_email(email), "db_key_already_exists")
        host = x.get_host(host_name)
        _require(
            host is not None and x.is_host_delivery_active(host), "host_unavailable"
        )
        token = x._resolve_host_api_token(host)
        _require(bool(token), "token_unavailable")
        api = x._create_api(
            host["host_url"], host["host_username"], host["host_pass"], token
        )
        x._set_api_request_retries(api, 1)
        primary_id = x._coerce_panel_int(host.get("host_inbound_id"))
        _require(primary_id > 0 and primary_id not in _PUBLIC, "primary_scope_invalid")
        anchor = (
            journal["identity"]["anchor_ms"] if journal else whitelist_cycle_anchor_ms
        )
        snap = _snapshot(api, primary_id, email, target_expiry_ms, anchor)
        plan = _plan(snap, journal)
        _require(persist(copy.deepcopy(plan)), "journal_save_failed")
        for iid in sorted(snap["missing"]):
            component, stage = iid, "absence_proof"
            intent = plan["components"][str(iid)]
            # Allow a later retry only with the SAME durable payload and fresh
            # native+central absence, observed twice across a native sync poll.
            if int(time.time() * 1000) - intent["last_attempt_at_ms"] < 60_000:
                return None
            time.sleep(5)
            fresh = _snapshot(
                api, primary_id, email, target_expiry_ms, plan["identity"]["anchor_ms"]
            )
            _plan(fresh, plan)
            if iid not in fresh["missing"]:
                continue
            intent["attempts"] += 1
            intent["last_attempt_at_ms"] = int(time.time() * 1000)
            _require(persist(copy.deepcopy(plan)), "attempt_save_failed")
            stage = "add_client"
            try:
                x._api_post_json_once_raw(
                    api,
                    "panel/api/clients/add",
                    {"client": copy.deepcopy(intent["client"]), "inboundIds": [iid]},
                )
            except _ERRORS:
                pass  # An error may follow a committed write; never infer absence.
            stage = "readback"
            for attempt in range(3):
                time.sleep(5)
                try:
                    observed = _snapshot(
                        api,
                        primary_id,
                        email,
                        target_expiry_ms,
                        plan["identity"]["anchor_ms"],
                    )
                    _plan(observed, plan)
                except _ERRORS:
                    if attempt == 2:
                        raise
                    continue
                if iid not in observed["missing"]:
                    break
            else:
                return None
            if iid in observed["missing"]:
                return None
        stage = "final_probe"
        result = x._probe_pending_standard_subscription_once(
            host_name,
            email,
            target_expiry_ms=target_expiry_ms,
            whitelist_cycle_anchor_ms=plan["identity"]["anchor_ms"],
        )
        if result:
            result["recovered_cycle_anchor_ms"] = plan["identity"]["anchor_ms"]
        return result
    except _ERRORS as exc:
        reason = str(exc) if isinstance(exc, RecoveryBlocked) else type(exc).__name__
        logger.warning(
            "Standard partial recovery pending stage=%s component=%s reason=%s",
            stage,
            component,
            reason,
        )
        return None


async def resume_pending_standard_birth(
    host_name: str,
    email: str,
    *,
    target_expiry_ms: int,
    whitelist_cycle_anchor_ms: int,
    journal: Mapping | None,
    persist: Callable[[dict], bool],
) -> dict | None:
    # Keep the gate and caller's payment-lock heartbeat alive until every
    # synchronous request has settled. Cancelling to_thread does not stop it.
    worker = asyncio.create_task(
        x._run_blocking_once_to_completion(
            _resume_once,
            host_name,
            email,
            target_expiry_ms=target_expiry_ms,
            whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
            journal=journal,
            persist=persist,
        )
    )
    cancelled = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            # Retrieve and propagate below; cancellation has precedence.
            break
    if cancelled:
        if not worker.cancelled():
            worker.exception()
        raise asyncio.CancelledError
    return worker.result()
