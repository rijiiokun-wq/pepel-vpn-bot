import asyncio
import base64
import binascii
import copy
import hashlib
import json
import logging
import math
import os
import re
import secrets
import socket
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, MutableMapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

import requests
from py3xui import Api, Client, Inbound
from requests.exceptions import RequestException

from shop_bot.data_manager import subscription_policy
from shop_bot.data_manager.database import (
    add_mainpanel_inbound_sync_item,
    clear_key_subscription_payload,
    create_mainpanel_inbound_sync_run,
    get_active_keys_for_host,
    get_all_hosts,
    get_host,
    get_key_by_email,
    get_mainpanel_inbound_snapshot,
    get_mainpanel_inbound_sync_run,
    get_setting,
    is_host_delivery_active,
    update_host_inbound_id,
    update_mainpanel_inbound_sync_run,
    upsert_mainpanel_inbound_snapshot,
)

logger = logging.getLogger(__name__)


def _read_positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(str(os.environ.get(name, str(default))).strip() or str(default)))
    except (TypeError, ValueError):
        return default


# XUI API connection timeout (seconds)
XUI_API_TIMEOUT = _read_positive_int_env("XUI_API_TIMEOUT_SECONDS", 20)
# Lock to prevent concurrent socket.setdefaulttimeout() race condition
_socket_timeout_lock = threading.Lock()
XUI_API_LOGIN_RETRIES = 2
XUI_API_RETRY_DELAY_SECONDS = 1.0
BYTES_PER_GB = 1024 * 1024 * 1024
MULTI_NODE_LIMITED_TRAFFIC_BYTES = 40 * BYTES_PER_GB
STANDARD_WHITELIST_TRAFFIC_BYTES = 5 * BYTES_PER_GB
TRIAL_SUPER_WHITELIST_TRAFFIC_BYTES = 2 * BYTES_PER_GB
MULTI_NODE_LIMIT_RESET_DAYS = 30
MANUAL_UNLIMITED_EXPIRY_MIN_MS = 4_070_908_800_000
WHITELIST_CYCLE_REPAIR_BATCH_MAX = 50
MILLISECONDS_PER_DAY = 24 * 60 * 60 * 1000
WHITELIST_RESET_PERIOD_MS = MULTI_NODE_LIMIT_RESET_DAYS * MILLISECONDS_PER_DAY
WHITELIST_RESET_MATCH_TOLERANCE_MS = 10 * 60 * 1000
SUPPORTED_SUBSCRIPTION_INBOUND_PROTOCOLS = frozenset({"vless", "hysteria", "hysteria2"})
SUBSCRIPTION_TIER_STANDARD = "standard"
SUBSCRIPTION_TIER_SUPER = "super"
SUBSCRIPTION_TIER_DISPLAY_NAMES = {
    SUBSCRIPTION_TIER_STANDARD: "STANDART",
    SUBSCRIPTION_TIER_SUPER: "SUPER",
}
PANEL_CALL_TIMEOUT_SECONDS = max(
    XUI_API_TIMEOUT + 5,
    _read_positive_int_env("PANEL_CALL_TIMEOUT_SECONDS", 45),
)
PANEL_CALL_RETRIES = max(
    1,
    int(str(os.environ.get("PANEL_CALL_RETRIES", "2")).strip() or "2"),
)
PANEL_CLIENT_UPSERT_ALLOW_FULL_INBOUND_FALLBACK = str(
    os.environ.get("PANEL_CLIENT_UPSERT_ALLOW_FULL_INBOUND_FALLBACK", "0")
).strip().lower() in {"1", "true", "yes", "on"}
SUBSCRIPTION_LINKS_MIN_ATTEMPTS = max(
    1,
    int(str(os.environ.get("SUBSCRIPTION_LINKS_MIN_ATTEMPTS", "4")).strip() or "4"),
)
SUBSCRIPTION_LINKS_RETRY_DELAY_SECONDS = max(
    0.05,
    float(str(os.environ.get("SUBSCRIPTION_LINKS_RETRY_DELAY_SECONDS", "0.35")).strip() or "0.35"),
)
NODE_SYNC_SUBSCRIPTION_VERIFY_LIMIT = max(
    0,
    int(str(os.environ.get("NODE_SYNC_SUBSCRIPTION_VERIFY_LIMIT", "3")).strip() or "3"),
)
NODE_SYNC_PROGRESS_EVERY = max(
    1,
    int(str(os.environ.get("NODE_SYNC_PROGRESS_EVERY", "25")).strip() or "25"),
)
NODE_SYNC_CLIENT_LIMIT = max(
    0,
    int(str(os.environ.get("NODE_SYNC_CLIENT_LIMIT", "50")).strip() or "50"),
)
_ENV_VAR_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
_HOST_ENV_SUFFIX_RE = re.compile(r"[^A-Z0-9]+")


def _normalize_api_token(api_token: str | None) -> str:
    return str(api_token or "").strip()


def _resolve_api_token_from_env(api_token_env: str | None) -> str | None:
    env_name = str(api_token_env or "").strip()
    if not env_name:
        return None
    if not _ENV_VAR_NAME_RE.fullmatch(env_name):
        logger.error("Ignoring invalid XUI API token env variable name: %s", env_name)
        return None
    token = os.environ.get(env_name, "").strip()
    if not token:
        logger.warning("XUI API token env variable '%s' is configured but empty.", env_name)
        return None
    return token


def _normalize_host_env_suffix(host_name: str | None) -> str:
    return _HOST_ENV_SUFFIX_RE.sub("_", str(host_name or "").strip().upper()).strip("_")


def get_auto_host_api_token_env_name(host_data: dict | None) -> str | None:
    if not host_data:
        return None
    suffix = _normalize_host_env_suffix(host_data.get("host_name"))
    if not suffix:
        return None
    return f"XUI_API_TOKEN_{suffix}"


def _resolve_host_api_token(host_data: dict | None) -> str | None:
    if not host_data:
        return None
    raw_value = str(host_data.get("host_api_token_env") or host_data.get("host_api_token") or "").strip()
    if not raw_value:
        auto_env_name = get_auto_host_api_token_env_name(host_data)
        if auto_env_name and os.environ.get(auto_env_name):
            return _resolve_api_token_from_env(auto_env_name)
        return None

    normalized_value = raw_value.lower()
    if normalized_value.startswith("env:"):
        env_name = raw_value[len("env:") :].strip()
        if not env_name:
            return None
        return _resolve_api_token_from_env(env_name)

    if _ENV_VAR_NAME_RE.fullmatch(raw_value):
        token_from_env = _resolve_api_token_from_env(raw_value)
        if token_from_env:
            return token_from_env
        logger.warning(
            "XUI API token value '%s' matches env naming but env variable is missing; using it as direct token.",
            raw_value,
        )
        return raw_value

    # Backward-compatible mode: allow storing a direct API token value in DB.
    return raw_value


def _with_bearer_header(headers: dict[str, str] | None, api_token: str | None) -> dict[str, str]:
    merged = dict(headers or {})
    token = _normalize_api_token(api_token)
    if token and "Authorization" not in merged:
        merged["Authorization"] = f"Bearer {token}"
    return merged


def _attach_api_token(api: Api, api_token: str | None) -> Api:
    """
    Add 3x-ui 3.x Bearer token auth to py3xui without changing the SDK globally.
    """
    token = _normalize_api_token(api_token)
    if not token:
        return api

    # Keep the token available to the one-shot atomic repair transport.  That
    # path intentionally bypasses py3xui's retry wrapper, so it cannot inherit
    # the Authorization header from the closures installed below.
    api._pepel_api_token = token

    for api_part in (api.client, api.inbound, api.database, api.server):
        original_request = api_part._request_with_retry
        original_get = api_part._get
        original_post = api_part._post

        def _request_with_token(method, url, headers, *args, _original=original_request, **kwargs):
            kwargs.setdefault("timeout", XUI_API_TIMEOUT)
            return _original(method, url, _with_bearer_header(headers, token), *args, **kwargs)

        def _get_with_token_auth(url, headers, *args, _original=original_get, **kwargs):
            kwargs.setdefault("is_login", True)
            return _original(url, headers, *args, **kwargs)

        def _post_with_token_auth(url, headers, data, *args, _original=original_post, **kwargs):
            kwargs.setdefault("is_login", True)
            return _original(url, headers, data, *args, **kwargs)

        api_part._request_with_retry = _request_with_token
        api_part._get = _get_with_token_auth
        api_part._post = _post_with_token_auth
    return api


def _set_api_request_retries(api: Api, retries: int) -> None:
    safe_retries = max(1, int(retries))
    for api_part in (api.client, api.inbound, api.database, api.server):
        api_part.max_retries = safe_retries


def _create_api(host_url: str, username: str, password: str, api_token: str | None = None) -> Api:
    api = Api(host=host_url, username=username, password=password)
    return _attach_api_token(api, api_token)


def _authenticate_api(api: Api, api_token: str | None = None) -> str:
    """
    Authenticate panel session.

    For 3x-ui 3.0.2+ we prefer Bearer token mode for `/panel/api/*` and skip
    `/login` (which may require CSRF). Fallback cookie login remains for hosts
    without API token.
    """
    if _normalize_api_token(api_token):
        return "bearer"
    api.login()
    return "cookie"


PANEL_CALL_CONCURRENCY = max(
    2,
    int(str(os.environ.get("PANEL_CALL_CONCURRENCY", "8")).strip() or "8"),
)
PANEL_CALL_QUEUE_TIMEOUT_SECONDS = max(
    0.2,
    float(str(os.environ.get("PANEL_CALL_QUEUE_TIMEOUT_SECONDS", "1.5")).strip() or "1.5"),
)
HOST_HEALTH_CACHE_MAX_AGE_SECONDS = max(
    30,
    int(str(os.environ.get("HOST_HEALTH_CACHE_MAX_AGE_SECONDS", "180")).strip() or "180"),
)
HOST_HEALTH_CACHE_MAX_ENTRIES = max(
    32,
    int(str(os.environ.get("HOST_HEALTH_CACHE_MAX_ENTRIES", "512")).strip() or "512"),
)
HOST_HEALTH_CACHE_GC_MAX_AGE_SECONDS = max(
    HOST_HEALTH_CACHE_MAX_AGE_SECONDS,
    int(str(os.environ.get("HOST_HEALTH_CACHE_GC_MAX_AGE_SECONDS", "3600")).strip() or "3600"),
)
HOST_CIRCUIT_BREAKER_FAILURE_THRESHOLD = max(
    1,
    int(str(os.environ.get("HOST_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "3")).strip() or "3"),
)
HOST_CIRCUIT_BREAKER_COOLDOWN_SECONDS = max(
    10,
    int(str(os.environ.get("HOST_CIRCUIT_BREAKER_COOLDOWN_SECONDS", "120")).strip() or "120"),
)
HOST_CIRCUIT_BREAKER_MAX_ENTRIES = max(
    32,
    int(str(os.environ.get("HOST_CIRCUIT_BREAKER_MAX_ENTRIES", "512")).strip() or "512"),
)
HOST_CIRCUIT_BREAKER_STATE_TTL_SECONDS = max(
    HOST_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    int(str(os.environ.get("HOST_CIRCUIT_BREAKER_STATE_TTL_SECONDS", "900")).strip() or "900"),
)


@dataclass(frozen=True, slots=True)
class HostLoginResult:
    api: Api | None
    inbound: Inbound | Any | None
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = 1
    recovered_on_retry: bool = False


@dataclass(frozen=True, slots=True)
class HostHealthSnapshot:
    host_name: str
    status: str
    clients_count: int | None
    checked_at: float
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class PanelClientSummary:
    id: str | None
    email: str
    expiry_time: int | None


@dataclass(frozen=True, slots=True)
class PanelClientScan:
    clients_by_email: dict[str, PanelClientSummary]
    orphan_total: int
    orphan_sample: list[str]


class PanelInboundLookupError(RuntimeError):
    def __init__(self, code: str, inbound_id: int | str, detail: str | None = None) -> None:
        self.code = code
        self.inbound_id = str(inbound_id).strip()
        self.detail = str(detail or "").strip()
        message = f"{self.code}:{self.inbound_id}"
        if self.detail:
            message = f"{message}:{self.detail}"
        super().__init__(message)


_host_health_cache_lock = threading.Lock()
_host_health_cache: dict[str, HostHealthSnapshot] = {}
_host_circuit_breaker_lock = threading.Lock()
_host_circuit_breaker_state: dict[str, dict[str, float | int]] = {}
_panel_call_gate_lock = threading.Lock()
_panel_call_gate: asyncio.Semaphore | None = None
_panel_call_gate_loop_id: int | None = None
_HOST_CIRCUIT_BREAKER_RETRYABLE_CODES = {
    "host_timeout",
    "host_connection_refused",
    "host_connection_reset",
    "host_dns_failed",
    "host_ssl_error",
    "host_invalid_http_response",
}


def _prune_host_health_cache_locked(now_ts: float) -> None:
    stale_cutoff = now_ts - float(HOST_HEALTH_CACHE_GC_MAX_AGE_SECONDS)
    stale_keys = [
        host_name for host_name, snapshot in _host_health_cache.items() if float(snapshot.checked_at) < stale_cutoff
    ]
    for host_name in stale_keys:
        _host_health_cache.pop(host_name, None)

    overflow = len(_host_health_cache) - HOST_HEALTH_CACHE_MAX_ENTRIES
    if overflow <= 0:
        return

    sorted_keys = sorted(_host_health_cache, key=lambda key: _host_health_cache[key].checked_at)
    for host_name in sorted_keys[:overflow]:
        _host_health_cache.pop(host_name, None)


def _prune_host_circuit_breaker_locked(now_ts: float) -> None:
    stale_cutoff = now_ts - float(HOST_CIRCUIT_BREAKER_STATE_TTL_SECONDS)
    stale_keys: list[str] = []
    for host_name, state in _host_circuit_breaker_state.items():
        opened_until = float(state.get("opened_until", 0.0) or 0.0)
        updated_at = float(state.get("updated_at", opened_until) or 0.0)

        # Keep active cooldown entries; prune only inactive/stale states.
        if opened_until > now_ts:
            continue
        if updated_at <= 0 or updated_at < stale_cutoff:
            stale_keys.append(host_name)

    for host_name in stale_keys:
        _host_circuit_breaker_state.pop(host_name, None)

    overflow = len(_host_circuit_breaker_state) - HOST_CIRCUIT_BREAKER_MAX_ENTRIES
    if overflow <= 0:
        return

    sorted_keys = sorted(
        _host_circuit_breaker_state,
        key=lambda key: float(
            _host_circuit_breaker_state[key].get(
                "updated_at",
                _host_circuit_breaker_state[key].get("opened_until", 0.0),
            )
            or 0.0
        ),
    )
    for host_name in sorted_keys[:overflow]:
        _host_circuit_breaker_state.pop(host_name, None)


def _get_panel_call_gate() -> asyncio.Semaphore:
    global _panel_call_gate
    global _panel_call_gate_loop_id

    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    with _panel_call_gate_lock:
        if _panel_call_gate is None or _panel_call_gate_loop_id != loop_id:
            _panel_call_gate = asyncio.Semaphore(PANEL_CALL_CONCURRENCY)
            _panel_call_gate_loop_id = loop_id
        return _panel_call_gate


def _set_future_result_if_pending(future: asyncio.Future, result) -> None:
    if not future.done():
        future.set_result(result)


def _set_future_exception_if_pending(future: asyncio.Future, exc: BaseException) -> None:
    if not future.done():
        future.set_exception(exc)


async def _run_blocking_call_in_daemon_thread(func, *args, call_timeout: float, **kwargs):
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def _notify_future(callback, *callback_args) -> None:
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(callback, future, *callback_args)
        except RuntimeError:
            return

    def _runner() -> None:
        try:
            result = func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - propagate SDK/thread errors back to the awaiting caller.
            _notify_future(_set_future_exception_if_pending, exc)
        else:
            _notify_future(_set_future_result_if_pending, result)

    thread = threading.Thread(
        target=_runner,
        name=f"panel-call-{getattr(func, '__name__', 'unknown')}",
        daemon=True,
    )
    thread.start()
    return await asyncio.wait_for(future, timeout=call_timeout)


async def _run_blocking_with_timeout(
    func,
    *args,
    retries: int | None = None,
    error_context: str = "",
    **kwargs,
):
    wrapper_timeout = kwargs.pop("timeout", None)
    safe_timeout = float(wrapper_timeout or PANEL_CALL_TIMEOUT_SECONDS)
    attempts = max(1, int(retries if retries is not None else PANEL_CALL_RETRIES))
    last_error: Exception | None = None
    gate = _get_panel_call_gate()

    for attempt in range(1, attempts + 1):
        acquired = False
        try:
            await asyncio.wait_for(
                gate.acquire(),
                timeout=PANEL_CALL_QUEUE_TIMEOUT_SECONDS,
            )
            acquired = True
            return await _run_blocking_call_in_daemon_thread(func, *args, call_timeout=safe_timeout, **kwargs)
        except TimeoutError:
            if not acquired:
                last_error = TimeoutError(f"panel_call_queue_timeout:{PANEL_CALL_QUEUE_TIMEOUT_SECONDS}s")
                break
            last_error = TimeoutError(f"panel_call_timeout:{safe_timeout}s")
        except Exception as exc:  # noqa: BLE001 - preserve retry behavior for SDK/network failures.
            last_error = exc
        finally:
            if acquired:
                gate.release()

        if attempt >= attempts:
            break
        logger.warning(
            "Blocking panel call failed (%s, attempt %s/%s): %s",
            error_context or getattr(func, "__name__", "unknown"),
            attempt,
            attempts,
            last_error,
        )
        await asyncio.sleep(min(1.0 * attempt, 2.0))

    if last_error:
        raise last_error
    raise RuntimeError("Blocking panel call failed without explicit exception")


async def _run_blocking_once_to_completion(func, *args, **kwargs):
    """Run one bounded-I/O workflow without abandoning its worker thread."""
    gate = _get_panel_call_gate()
    acquired = False
    try:
        await asyncio.wait_for(
            gate.acquire(),
            timeout=PANEL_CALL_QUEUE_TIMEOUT_SECONDS,
        )
        acquired = True
        return await asyncio.to_thread(func, *args, **kwargs)
    except TimeoutError as exc:
        if not acquired:
            raise TimeoutError(
                f"panel_call_queue_timeout:{PANEL_CALL_QUEUE_TIMEOUT_SECONDS}s"
            ) from exc
        raise
    finally:
        if acquired:
            gate.release()


def _normalize_host_circuit_key(host_url: str) -> str:
    return str(host_url or "").strip().lower()


def reset_host_circuit_breaker_state() -> None:
    with _host_circuit_breaker_lock:
        _host_circuit_breaker_state.clear()


def _host_circuit_breaker_precheck(host_url: str) -> tuple[bool, int]:
    key = _normalize_host_circuit_key(host_url)
    if not key:
        return False, 0

    now_ts = time.time()
    with _host_circuit_breaker_lock:
        _prune_host_circuit_breaker_locked(now_ts)
        state = _host_circuit_breaker_state.get(key)
        if not state:
            return False, 0

        opened_until = float(state.get("opened_until", 0.0) or 0.0)
        if opened_until <= 0:
            return False, 0
        if opened_until > 0 and opened_until <= now_ts:
            _host_circuit_breaker_state.pop(key, None)
            return False, 0

        remaining_seconds = max(1, int(opened_until - now_ts))
        return True, remaining_seconds


def _host_circuit_breaker_on_success(host_url: str) -> None:
    key = _normalize_host_circuit_key(host_url)
    if not key:
        return
    with _host_circuit_breaker_lock:
        _host_circuit_breaker_state.pop(key, None)


def _host_circuit_breaker_on_failure(host_url: str, error_code: str | None) -> bool:
    normalized_code = str(error_code or "").strip().lower()
    if normalized_code not in _HOST_CIRCUIT_BREAKER_RETRYABLE_CODES:
        return False

    key = _normalize_host_circuit_key(host_url)
    if not key:
        return False

    now_ts = time.time()
    opened_now = False
    with _host_circuit_breaker_lock:
        state = _host_circuit_breaker_state.setdefault(
            key,
            {"failures": 0, "opened_until": 0.0, "updated_at": now_ts},
        )
        failures = int(state.get("failures", 0) or 0) + 1
        state["failures"] = failures
        state["updated_at"] = now_ts

        if failures >= HOST_CIRCUIT_BREAKER_FAILURE_THRESHOLD:
            opened_until = now_ts + float(HOST_CIRCUIT_BREAKER_COOLDOWN_SECONDS)
            state["opened_until"] = opened_until
            opened_now = True
        _prune_host_circuit_breaker_locked(now_ts)

    if opened_now:
        logger.warning(
            "XUI host circuit breaker opened for '%s' after %s failures. Cooldown: %ss.",
            host_url,
            int(failures),
            HOST_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        )
    return opened_now


def update_host_health_snapshot(
    host_name: str,
    *,
    status: str,
    clients_count: int | None,
    error_code: str | None = None,
    error_message: str | None = None,
    checked_at: float | None = None,
) -> None:
    normalized_name = str(host_name or "").strip()
    if not normalized_name:
        return

    normalized_status = str(status or "unknown").strip().lower() or "unknown"
    normalized_clients_count: int | None = None
    if clients_count is not None:
        try:
            normalized_clients_count = max(0, int(clients_count))
        except (TypeError, ValueError):
            normalized_clients_count = None

    now_ts = time.time()

    snapshot = HostHealthSnapshot(
        host_name=normalized_name,
        status=normalized_status,
        clients_count=normalized_clients_count,
        checked_at=float(checked_at) if checked_at is not None else now_ts,
        error_code=str(error_code or "").strip() or None,
        error_message=str(error_message or "").strip() or None,
    )
    with _host_health_cache_lock:
        _host_health_cache[normalized_name] = snapshot
        _prune_host_health_cache_locked(now_ts)


def get_host_health_snapshot(
    host_name: str,
    *,
    max_age_seconds: int | None = None,
) -> HostHealthSnapshot | None:
    normalized_name = str(host_name or "").strip()
    if not normalized_name:
        return None

    with _host_health_cache_lock:
        snapshot = _host_health_cache.get(normalized_name)

    if not snapshot:
        return None

    if max_age_seconds is None:
        max_age_seconds = HOST_HEALTH_CACHE_MAX_AGE_SECONDS

    safe_max_age = max(1, int(max_age_seconds))
    if (time.time() - snapshot.checked_at) > safe_max_age:
        with _host_health_cache_lock:
            if _host_health_cache.get(normalized_name) is snapshot:
                _host_health_cache.pop(normalized_name, None)
        return None
    return snapshot


def get_cached_host_clients_count(
    host_name: str,
    *,
    max_age_seconds: int | None = None,
) -> int | None:
    snapshot = get_host_health_snapshot(host_name, max_age_seconds=max_age_seconds)
    if not snapshot:
        return None
    if snapshot.status in {"unavailable", "maintenance"}:
        return None
    if snapshot.clients_count is None:
        return None
    return int(snapshot.clients_count)


def _extract_client_sub_id(client: Any) -> str | None:
    for attr in ("sub_id", "subId", "subid"):
        value = getattr(client, attr, None)
        if value:
            return str(value)

    candidate_dicts: list[dict] = []
    if isinstance(client, dict):
        candidate_dicts.append(client)
    dump_method = getattr(client, "model_dump", None) or getattr(client, "dict", None)
    if callable(dump_method):
        try:
            as_dict = dump_method()
            if isinstance(as_dict, dict):
                candidate_dicts.append(as_dict)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.debug("Failed to serialize client %s for subId extraction: %s", client, exc)

    for item in candidate_dicts:
        for key in ("subId", "sub_id", "subid"):
            value = item.get(key)
            if value:
                return str(value)

    return None


def _generate_sub_id() -> str:
    """Generate deterministic-safe sub_id string for 3xui clients."""
    return str(uuid.uuid4()).replace("-", "")[:16]


def _ensure_client_sub_id(client: Any, preferred_sub_id: str | None = None) -> str:
    """
    Ensure client has sub_id populated.

    3xui subscription links are built as `<base_url>/<sub_id>`, so we guarantee
    this field exists before panel update/add calls.
    """
    preferred = str(preferred_sub_id or "").strip()
    existing = (_extract_client_sub_id(client) or "").strip()
    sub_id = preferred or existing or _generate_sub_id()
    for attr in ("sub_id", "subId", "subid"):
        with suppress(AttributeError, TypeError, ValueError):
            setattr(client, attr, sub_id)
    return sub_id


def _set_client_total_gb(
    client: Any,
    total_gb_bytes: int | None,
    *,
    traffic_reset_days: int | None = None,
) -> None:
    if total_gb_bytes is None:
        return
    safe_total = max(0, int(total_gb_bytes))
    for attr in ("total_gb", "totalGB"):
        with suppress(AttributeError, TypeError, ValueError):
            setattr(client, attr, safe_total)
    with suppress(AttributeError, TypeError, ValueError):
        client.reset = (
            0
            if safe_total == 0
            else (
                MULTI_NODE_LIMIT_RESET_DAYS
                if traffic_reset_days is None
                else max(0, int(traffic_reset_days))
            )
        )


def _datetime_value_ms(value: Any) -> int:
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    normalized = str(value or "").strip().replace("Z", "+00:00")
    if not normalized:
        return 0
    try:
        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return 0


def _key_has_unlimited_whitelist_override(key: Mapping[str, Any] | None) -> bool:
    """Return only the explicit per-key exception marker."""
    row = key if isinstance(key, Mapping) else {}
    marker = str(row.get("whitelist_traffic_unlimited_override") or "").strip().lower()
    return marker in {"1", "true", "yes", "on"}


def _key_uses_unlimited_whitelist(
    key: Mapping[str, Any] | None,
    *,
    paid_expiry_ms: int | None = None,
) -> bool:
    """Combine the explicit marker with the already-supported 2099 convention."""
    row = key if isinstance(key, Mapping) else {}
    if _key_has_unlimited_whitelist_override(row):
        return True
    effective_expiry_ms = (
        int(paid_expiry_ms)
        if paid_expiry_ms is not None
        else _datetime_value_ms(row.get("expiry_date"))
    )
    return effective_expiry_ms >= MANUAL_UNLIMITED_EXPIRY_MIN_MS


def _paid_expiry_ms_for_client(
    email: str,
    *,
    target_expiry_ms: int | None,
    days_to_add: int,
    now_ms: int,
) -> int:
    if target_expiry_ms is not None:
        return int(target_expiry_ms)

    current_expiry_ms = 0
    try:
        key = get_key_by_email(str(email or "").strip()) or {}
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError) as exc:
        logger.debug("Could not load local paid expiry for email=%s: %s", email, exc)
        key = {}
    current_expiry_ms = _datetime_value_ms(key.get("expiry_date"))
    base_ms = max(int(now_ms), current_expiry_ms)
    return base_ms + max(0, int(days_to_add or 0)) * MILLISECONDS_PER_DAY


def _whitelist_cycle_anchor_ms_for_client(
    email: str,
    *,
    requested_anchor_ms: int | None,
    now_ms: int,
) -> int:
    if requested_anchor_ms is not None and int(requested_anchor_ms) > 0:
        return int(requested_anchor_ms)
    try:
        key = get_key_by_email(str(email or "").strip()) or {}
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError) as exc:
        logger.debug("Could not load WHITELIST cycle anchor for email=%s: %s", email, exc)
        key = {}
    stored_anchor_ms = _datetime_value_ms(
        key.get("whitelist_cycle_anchor_at") or key.get("created_date")
    )
    if stored_anchor_ms > 0:
        return stored_anchor_ms
    return int(now_ms)


def _next_whitelist_reset_deadline_ms(
    anchor_ms: int,
    current_reset_ms: int | None,
    paid_expiry_ms: int | None,
    now_ms: int,
    *,
    current_reset_days: int | None = None,
) -> tuple[int, bool]:
    """Return the next WHITELIST cycle deadline and whether a prior deadline was missed.

    The 3x-ui ``client_traffics.expiry_time`` field doubles as the automatic
    traffic-reset deadline when ``reset > 0``.  Paid access is therefore kept
    separately in ``vpn_keys.expiry_date`` and must never be used as the cycle
    deadline for a multi-cycle subscription.
    """
    safe_now_ms = max(0, int(now_ms or 0))
    safe_anchor_ms = int(anchor_ms or 0)
    if safe_anchor_ms <= 0 or safe_anchor_ms > safe_now_ms:
        safe_anchor_ms = safe_now_ms
    safe_paid_expiry_ms = max(0, int(paid_expiry_ms or 0))
    safe_current_reset_ms = max(0, int(current_reset_ms or 0))

    elapsed_ms = max(0, safe_now_ms - safe_anchor_ms)
    completed_periods = elapsed_ms // WHITELIST_RESET_PERIOD_MS
    next_anchor_boundary_ms = (
        safe_anchor_ms + (completed_periods + 1) * WHITELIST_RESET_PERIOD_MS
    )
    calculated_reset_ms = next_anchor_boundary_ms
    if safe_paid_expiry_ms > 0:
        calculated_reset_ms = min(calculated_reset_ms, safe_paid_expiry_ms)

    if current_reset_days is not None and int(current_reset_days) == 0:
        # A terminal reset=0 deadline certifies every anchor boundary up to the
        # paid terminal point.  Continuous renewals keep that marker and must
        # not grant another full limit merely because processing completed just
        # after the old paid expiry.  A true lapsed renewal reanchors at/after
        # the old deadline and starts a fresh counter cycle.
        if (
            safe_current_reset_ms > 0
            and safe_current_reset_ms <= safe_now_ms
            and safe_current_reset_ms <= safe_anchor_ms
        ):
            return calculated_reset_ms, True
        if safe_current_reset_ms > safe_now_ms:
            return calculated_reset_ms, False
        periods_at_or_after_terminal = max(
            1,
            math.ceil(
                (safe_current_reset_ms - safe_anchor_ms)
                / WHITELIST_RESET_PERIOD_MS
            ),
        )
        boundary_at_or_after_terminal_ms = (
            safe_anchor_ms
            + periods_at_or_after_terminal * WHITELIST_RESET_PERIOD_MS
        )
        return (
            calculated_reset_ms,
            safe_now_ms >= boundary_at_or_after_terminal_ms,
        )

    if safe_current_reset_ms <= 0:
        return calculated_reset_ms, completed_periods >= 1
    if safe_current_reset_ms <= safe_now_ms:
        return calculated_reset_ms, True

    # After the final automatic reset, 3x-ui can advance the deadline beyond
    # paid expiry.  Only that overshoot plus exact anchor alignment proves the
    # reset already happened; the runtime guard then changes policy to reset=0
    # without clearing counters twice.
    if safe_paid_expiry_ms > 0 and safe_current_reset_ms > safe_paid_expiry_ms:
        periods_from_anchor = round(
            (safe_current_reset_ms - safe_anchor_ms) / WHITELIST_RESET_PERIOD_MS
        )
        aligned_boundary_ms = (
            safe_anchor_ms + periods_from_anchor * WHITELIST_RESET_PERIOD_MS
        )
        if (
            periods_from_anchor >= 1
            and abs(safe_current_reset_ms - aligned_boundary_ms)
            <= WHITELIST_RESET_MATCH_TOLERANCE_MS
            and safe_now_ms + WHITELIST_RESET_MATCH_TOLERANCE_MS
            >= safe_current_reset_ms - WHITELIST_RESET_PERIOD_MS
        ):
            return calculated_reset_ms, False

    # A future deadline exactly on the strict next grid boundary is the normal
    # state after a successful 3x-ui auto reset.  This is stronger evidence
    # than merely being aligned to any later boundary (which can also be a
    # legacy multi-month paid expiry).
    if (
        abs(safe_current_reset_ms - next_anchor_boundary_ms)
        <= WHITELIST_RESET_MATCH_TOLERANCE_MS
    ):
        return calculated_reset_ms, False

    return calculated_reset_ms, completed_periods >= 1


def _raw_whitelist_client_cycle_state(
    raw_inbound: Mapping[str, Any] | None,
    *,
    email: str,
    client_uuid: str | None,
) -> dict[str, Any]:
    if not raw_inbound:
        return {}
    normalized_email = _normalized_client_email(email)
    normalized_uuid = _normalized_client_uuid(client_uuid)
    try:
        clients = [
            client
            for client in _iter_client_dicts_from_settings(raw_inbound.get("settings"))
            if _normalized_client_email(client.get("email")) == normalized_email
        ]
    except (TypeError, ValueError):
        clients = []
    if len(clients) > 1:
        return {"collision": True}
    client = clients[0] if clients else {}
    client_value_uuid = _normalized_client_uuid(client.get("id") or client.get("uuid"))
    if normalized_uuid and client_value_uuid and normalized_uuid != client_value_uuid:
        return {"collision": True}

    stats = [
        stat
        for stat in (raw_inbound.get("clientStats") or [])
        if isinstance(stat, Mapping) and _normalized_client_email(stat.get("email")) == normalized_email
    ]
    if len(stats) > 1:
        return {"collision": True}
    stat = stats[0] if stats else {}
    settings_expiry_ms = _coerce_panel_int(
        client.get("expiryTime", client.get("expiry_time"))
    )
    settings_reset_days = _coerce_panel_int(client.get("reset"))
    settings_total_bytes = _coerce_panel_int(
        client.get("totalGB", client.get("total_gb", client.get("total")))
    )
    settings_enable = _coerce_panel_bool(client.get("enable"), default=True)
    traffic_expiry_ms = _coerce_panel_int(
        stat.get("expiryTime", stat.get("expiry_time"))
    )
    traffic_reset_days = _coerce_panel_int(stat.get("reset"))
    traffic_total_bytes = _coerce_panel_int(stat.get("total"))
    traffic_enable = _coerce_panel_bool(stat.get("enable"), default=True)
    traffic_up_bytes = _coerce_panel_int(stat.get("up"))
    traffic_down_bytes = _coerce_panel_int(stat.get("down"))
    reset_days = max(traffic_reset_days, settings_reset_days)
    current_reset_ms = traffic_expiry_ms or settings_expiry_ms
    total_bytes = max(traffic_total_bytes, settings_total_bytes)
    used_bytes = traffic_up_bytes + traffic_down_bytes
    return {
        "collision": False,
        "client": client,
        "stat": stat,
        "sub_id": str(_extract_client_sub_id(client) or "").strip(),
        "current_reset_ms": current_reset_ms,
        "reset_days": reset_days,
        "used_bytes": used_bytes,
        "total_bytes": total_bytes,
        "enabled": traffic_enable if stat else settings_enable,
        "settings_expiry_ms": settings_expiry_ms,
        "settings_reset_days": settings_reset_days,
        "settings_total_bytes": settings_total_bytes,
        "settings_enable": settings_enable,
        "traffic_expiry_ms": traffic_expiry_ms,
        "traffic_reset_days": traffic_reset_days,
        "traffic_total_bytes": traffic_total_bytes,
        "traffic_enable": traffic_enable,
        "traffic_up_bytes": traffic_up_bytes,
        "traffic_down_bytes": traffic_down_bytes,
    }


def _load_whitelist_client_cycle_state(
    api: Api,
    *,
    inbound_id: int,
    email: str,
    client_uuid: str | None,
) -> dict[str, Any]:
    try:
        raw_inbound = _get_inbound_raw_by_id(api, inbound_id, allow_list_fallback=True)
    except (
        RequestException,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ) as exc:
        logger.warning(
            "Could not load WHITELIST cycle state: inbound_id=%s email=%s error=%s",
            inbound_id,
            email,
            exc,
        )
        return {}
    return _raw_whitelist_client_cycle_state(
        raw_inbound,
        email=email,
        client_uuid=client_uuid,
    )


def _should_reset_limited_whitelist_traffic(
    cycle_state: Mapping[str, Any],
    *,
    reset_limited_traffic: bool,
    reset_exhausted_limited_traffic: bool,
    cycle_overdue: bool,
) -> bool:
    total_bytes = max(0, _coerce_panel_int(cycle_state.get("total_bytes")))
    used_bytes = max(0, _coerce_panel_int(cycle_state.get("used_bytes")))
    exhausted = total_bytes > 0 and used_bytes >= total_bytes
    return bool(
        reset_limited_traffic
        or cycle_overdue
        or (reset_exhausted_limited_traffic and exhausted)
    )


def _reset_client_traffic_on_panel(api: Api, *, email: str) -> bool:
    safe_email = str(email or "").strip()
    if not safe_email:
        return False
    endpoint = f"panel/api/clients/resetTraffic/{quote(safe_email, safe='')}"
    try:
        payload = _api_post_json(api, endpoint, {})
    except (
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        logger.exception("Failed to reset client traffic counters: email=%s", safe_email)
        return False

    if isinstance(payload, Mapping) and payload.get("success") is False:
        logger.error("Panel refused client traffic reset: email=%s payload=%s", safe_email, payload)
        return False
    return True


def _finish_client_upsert(
    api: Api,
    *,
    email: str,
    client_uuid: str | None,
    expiry_ms: int | None,
    total_gb_bytes: int | None,
    reset_client_traffic: bool,
) -> tuple[str | None, int | None]:
    if reset_client_traffic and int(total_gb_bytes or 0) > 0:
        if not _reset_client_traffic_on_panel(api, email=email):
            return None, None
    return client_uuid, expiry_ms


_MISSING = object()
_CLIENT_CONFIG_FIELD_ALIASES: tuple[tuple[str, ...], ...] = (
    ("auth",),
    ("security",),
    ("flow",),
    ("method",),
    ("password",),
    ("reset",),
    ("limit_ip", "limitIp"),
    ("tg_id", "tgId"),
    ("comment",),
)


def _get_client_field(client: Any, aliases: tuple[str, ...]) -> Any:
    if client is None:
        return _MISSING
    for attr in aliases:
        with suppress(AttributeError, TypeError, ValueError):
            return getattr(client, attr)
    if isinstance(client, dict):
        for key in aliases:
            if key in client:
                return client[key]
    dump_method = getattr(client, "model_dump", None) or getattr(client, "dict", None)
    if callable(dump_method):
        with suppress(AttributeError, TypeError, ValueError):
            client_dict = dump_method()
            if isinstance(client_dict, dict):
                for key in aliases:
                    if key in client_dict:
                        return client_dict[key]
    return _MISSING


def _copy_client_config_fields(target_client: Any, source_client: Any | None) -> None:
    for aliases in _CLIENT_CONFIG_FIELD_ALIASES:
        value = _get_client_field(source_client, aliases)
        if value is _MISSING:
            continue
        for attr in aliases:
            try:
                setattr(target_client, attr, value)
            except (AttributeError, TypeError, ValueError):
                with suppress(AttributeError, TypeError, ValueError):
                    object.__setattr__(target_client, attr, value)


def _client_config_value(source_client: Any | None, aliases: tuple[str, ...], default: Any) -> Any:
    value = _get_client_field(source_client, aliases)
    if value is _MISSING:
        return default
    return value


def _generate_hysteria_secret() -> str:
    return secrets.token_urlsafe(18).rstrip("=")


def _inbound_supports_vision_flow(inbound: Inbound) -> bool:
    network = str(_stream_settings_value(inbound, "network", "tcp") or "").strip().lower()
    security = str(_stream_settings_value(inbound, "security", "") or "").strip().lower()
    if network != "tcp":
        return False
    return security in {"", "reality", "tls"}


def _inbound_disallows_vision_flow(inbound: Inbound) -> bool:
    label = re.sub(r"[^a-z0-9]+", "", _inbound_label(inbound).lower())
    return label == "estonia2"


def _client_flow_for_inbound(inbound: Inbound, source_client: Any | None) -> str:
    if _inbound_disallows_vision_flow(inbound) or not _inbound_supports_vision_flow(inbound):
        return ""
    source_flow = str(_client_config_value(source_client, ("flow",), "") or "").strip()
    return source_flow or "xtls-rprx-vision"


def _host_email_suffix(host_name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(host_name or "").strip().lower()).strip("-")
    return slug or "host"


def build_host_specific_email(base_email: str, host_name: str) -> str:
    email = str(base_email or "").strip()
    suffix = _host_email_suffix(host_name)
    local, sep, domain = email.partition("@")
    if not sep:
        return f"{email}-{suffix}"
    return f"{local}-{suffix}@{domain}"


def _inbound_label(inbound: Inbound) -> str:
    return str(
        getattr(inbound, "remark", "") or getattr(inbound, "tag", "") or f"inbound-{getattr(inbound, 'id', 0)}"
    ).strip()


def _coerce_positive_months(months: int | str | None) -> int:
    try:
        return max(1, int(months or 1))
    except (TypeError, ValueError):
        return 1


def _secondary_inbound_email(base_email: str, inbound: Inbound, *, host_name: str = "") -> str:
    label = _inbound_label(inbound)
    inbound_id = int(getattr(inbound, "id", 0) or 0)
    suffix = subscription_policy.client_identity_suffix(host_name, inbound_id, label)
    return build_host_specific_email(base_email, suffix)


def _inbound_protocol(inbound: Inbound) -> str:
    return str(getattr(inbound, "protocol", "") or "").strip().lower()


def _is_hysteria_subscription_inbound(inbound: Inbound) -> bool:
    protocol = _inbound_protocol(inbound)
    network = str(_stream_settings_value(inbound, "network", "") or "").strip().lower()
    return protocol in {"hysteria", "hysteria2"} or network in {"hysteria", "hysteria2"}


def _is_supported_subscription_inbound(inbound: Inbound) -> bool:
    return bool(
        getattr(inbound, "enable", False)
        and (
            _inbound_protocol(inbound) in SUPPORTED_SUBSCRIPTION_INBOUND_PROTOCOLS
            or _is_hysteria_subscription_inbound(inbound)
        )
        and int(getattr(inbound, "id", 0) or 0) > 0
    )


STANDARD_SUBSCRIPTION_REMARK = "Standard"
WHITE_SUBSCRIPTION_REMARK = "White (без Wi-Fi)"
DEFAULT_XHTTP_EXTERNAL_PROXY_PATH = "/api/user"
DEFAULT_XHTTP_EXTERNAL_PROXY_MODE = "packet-up"
DEFAULT_XHTTP_EXTERNAL_PROXY_ALPN = "h2"
DEFAULT_XHTTP_EXTERNAL_PROXY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
DEFAULT_XHTTP_EXTERNAL_PROXY_EXTRA = {
    "xPaddingHeader": "Authorization",
    "xPaddingKey": "token",
    "xPaddingMethod": "tokenish",
    "uplinkHTTPMethod": "GET",
    "xPaddingObfsMode": True,
    "xPaddingPlacement": "queryInHeader",
}
NODE_INBOUND_IMPORT_ATTEMPTS = 15
NODE_INBOUND_IMPORT_DELAY_SECONDS = 4.0
SUBSCRIPTION_INBOUND_MODE_ALL = "all_inbounds"
SUBSCRIPTION_INBOUND_MODE_ALL_EXCEPT_WHITELIST = "all_except_whitelist"
MAINPANEL_INBOUND_CLASS_ORDINARY = "ordinary"
MAINPANEL_INBOUND_CLASS_WHITELIST = "whitelist"
MAINPANEL_INBOUND_CLASS_UNSUPPORTED = "unsupported"


def _is_white_inbound_name(inbound: Inbound) -> bool:
    label = " ".join(
        value
        for value in (
            str(getattr(inbound, "remark", "") or "").strip(),
            str(getattr(inbound, "tag", "") or "").strip(),
        )
        if value
    )
    return bool(re.search(r"(^|[^a-z0-9])white", label, re.IGNORECASE))


def _looks_like_white_bypass_label(value: str) -> bool:
    return bool(
        re.search(
            r"(^|[^a-z0-9])white(?:[\W_]*list|\d*)?([^a-z0-9]|$)|whitelist|бел",
            str(value or ""),
            re.IGNORECASE,
        )
    )


def _friendly_xhttp_subscription_remark(inbound: Inbound, current_remark: str) -> str:
    decoded_remark = unquote(str(current_remark or "").strip())
    inbound_label = _inbound_label(inbound)
    if (
        _looks_like_white_bypass_label(decoded_remark)
        or _looks_like_white_bypass_label(inbound_label)
        or _is_white_inbound_name(inbound)
    ):
        return WHITE_SUBSCRIPTION_REMARK
    return STANDARD_SUBSCRIPTION_REMARK


def _friendly_subscription_remark(current_remark: str) -> str:
    decoded_remark = unquote(str(current_remark or "").strip())
    if _looks_like_white_bypass_label(decoded_remark):
        return WHITE_SUBSCRIPTION_REMARK
    return STANDARD_SUBSCRIPTION_REMARK


def _replace_subscription_remark(link: str, remark: str) -> str:
    parsed = urlparse(str(link or "").strip())
    if parsed.scheme.lower() not in {"vless", "hysteria2", "hy2"}:
        return link
    return parsed._replace(fragment=quote(remark, safe="")).geturl()


def _replace_subscription_hostname(link: str, hostname: str) -> str:
    parsed = urlparse(str(link or "").strip())
    normalized_host = str(hostname or "").strip()
    if parsed.scheme.lower() != "vless" or not parsed.hostname or not normalized_host:
        return link

    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    port_suffix = f":{parsed.port}" if parsed.port else ""
    return parsed._replace(netloc=f"{userinfo}{normalized_host}{port_suffix}").geturl()


def normalize_subscription_tier(raw_value: Any) -> str | None:
    normalized = str(raw_value or "").strip().lower()
    if normalized in {"standard", "standart", "std"}:
        return SUBSCRIPTION_TIER_STANDARD
    if normalized == "super":
        return SUBSCRIPTION_TIER_SUPER
    return None


def subscription_display_name_for_tier(subscription_tier: Any) -> str | None:
    normalized_tier = normalize_subscription_tier(subscription_tier)
    if not normalized_tier:
        return None
    return SUBSCRIPTION_TIER_DISPLAY_NAMES.get(normalized_tier)


def _apply_subscription_display_name_to_links(links: Iterable[str], display_name: str | None) -> list[str]:
    safe_display_name = str(display_name or "").strip()
    if not safe_display_name:
        return [str(link) for link in links]
    return [_replace_subscription_remark(str(link), safe_display_name) for link in links]


def apply_subscription_display_name(raw_payload: Any, display_name: str | None) -> str | None:
    normalized_payload = normalize_subscription_payload(raw_payload)
    if not normalized_payload:
        return None
    renamed_links = _apply_subscription_display_name_to_links(
        normalized_payload.splitlines(),
        display_name,
    )
    return normalize_subscription_payload(renamed_links)


def _safe_inbound_id(inbound: Inbound) -> int:
    try:
        return max(0, int(getattr(inbound, "id", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _safe_inbound_node_id(inbound: Inbound) -> int:
    for attr_name in ("node_id", "nodeId"):
        try:
            node_id = int(getattr(inbound, attr_name, 0) or 0)
        except (TypeError, ValueError):
            node_id = 0
        if node_id > 0:
            return node_id
    return 0


def _node_public_address_maps(api: Api) -> tuple[dict[int, str], dict[str, str]]:
    by_id: dict[int, str] = {}
    by_name: dict[str, str] = {}
    try:
        nodes = _list_nodes_raw(api)
    except (
        RequestException,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ) as exc:
        logger.debug("Node public address map unavailable, using host URL fallback: %s", exc)
        return by_id, by_name
    for node in nodes:
        node_id = _coerce_panel_int(node.get("id"))
        address = str(node.get("address") or "").strip()
        name = str(node.get("name") or "").strip().lower()
        if node_id > 0 and address:
            by_id[node_id] = address
        if name and address:
            by_name[name] = address
    return by_id, by_name


def _subscription_endpoint_hostname(
    inbound: Inbound,
    host_url: str,
    *,
    node_address_by_id: Mapping[int, str] | None = None,
    node_address_by_name: Mapping[str, str] | None = None,
) -> str | None:
    node_id = _safe_inbound_node_id(inbound)
    if node_id > 0:
        address = str((node_address_by_id or {}).get(node_id) or "").strip()
        if address:
            return address

    inbound_label = _inbound_label(inbound).strip().lower()
    if inbound_label:
        address = str((node_address_by_name or {}).get(inbound_label) or "").strip()
        if address:
            return address

    return str(urlparse(str(host_url or "").strip()).hostname or "").strip() or None


def _host_is_white(host_data: Mapping[str, Any] | None) -> bool:
    try:
        return bool(int((host_data or {}).get("is_white_host") or 0))
    except (TypeError, ValueError):
        return False


def should_include_white_subscription_inbounds(
    host_data: Mapping[str, Any] | None,
    *,
    host_name: str | None = None,
) -> bool:
    normalized_host_name = str(host_name or (host_data or {}).get("host_name") or "").strip()
    configured_panel_hosts = {
        str(get_setting("super_key_host_name") or "").strip(),
        str(get_setting("standard_key_host_name") or "").strip(),
    }
    if normalized_host_name and normalized_host_name in configured_panel_hosts:
        return True
    return _host_is_white(host_data)


def _white_node_inbound_ids_for_panel(host_name: str) -> set[int]:
    target_host_name = str(host_name or "").strip()
    super_host_name = str(get_setting("super_key_host_name") or "").strip()
    standard_host_name = str(get_setting("standard_key_host_name") or "").strip()
    panel_host_names = {name for name in (super_host_name, standard_host_name) if name}
    if not panel_host_names or target_host_name not in panel_host_names:
        return set()

    inbound_ids: set[int] = set()
    for host in get_all_hosts():
        if not _host_is_white(host):
            continue
        if not is_host_delivery_active(host):
            continue
        try:
            node_inbound_id = int(host.get("node_inbound_id") or 0)
        except (TypeError, ValueError):
            continue
        if node_inbound_id > 0:
            inbound_ids.add(node_inbound_id)
    return inbound_ids


def _multi_node_total_gb_bytes(
    inbound: Inbound,
    *,
    months: int = 1,
    white_node_inbound_ids: set[int] | None = None,
    white_traffic_limit_bytes: int | None = None,
) -> int:
    del months
    inbound_id = _safe_inbound_id(inbound)
    is_configured_white_node = inbound_id in (white_node_inbound_ids or set())
    if is_configured_white_node or _is_white_inbound_name(inbound):
        # 3x-ui expects bytes in totalGB; white nodes use a fixed per-client cap.
        if white_traffic_limit_bytes is not None:
            return max(0, int(white_traffic_limit_bytes))
        return MULTI_NODE_LIMITED_TRAFFIC_BYTES
    return 0


def _is_white_subscription_inbound(inbound: Inbound, white_node_inbound_ids: set[int] | None = None) -> bool:
    inbound_id = _safe_inbound_id(inbound)
    if inbound_id in (white_node_inbound_ids or set()):
        return True
    if _looks_like_white_bypass_label(_inbound_label(inbound)):
        return True
    return _xhttp_external_proxy_endpoint(inbound) is not None


def _subscription_inbound_mode(*, include_white_inbounds: bool) -> str:
    if include_white_inbounds:
        return SUBSCRIPTION_INBOUND_MODE_ALL
    return SUBSCRIPTION_INBOUND_MODE_ALL_EXCEPT_WHITELIST


def _subscription_inbound_candidates(
    api: Api,
    raw_inbounds: list[dict[str, Any]] | None = None,
) -> list[Inbound]:
    resolved_raw_inbounds = raw_inbounds if raw_inbounds is not None else _list_inbounds_raw(api)
    if resolved_raw_inbounds:
        candidates: list[Inbound] = []
        for raw_inbound in resolved_raw_inbounds:
            inbound_id = _coerce_panel_int(raw_inbound.get("id"))
            if inbound_id <= 0:
                continue
            try:
                detailed_raw = _get_inbound_raw_by_id(api, inbound_id, allow_list_fallback=False)
            except (
                RequestException,
                RuntimeError,
                TimeoutError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
            ) as exc:
                logger.warning("Failed to load detailed subscription inbound id=%s: %s", inbound_id, exc)
                detailed_raw = None
            if isinstance(detailed_raw, Mapping):
                candidates.append(_raw_inbound_runtime_namespace(detailed_raw, inbound_id=inbound_id))
                continue
            try:
                detailed_inbound = api.inbound.get_by_id(inbound_id)
            except (
                RequestException,
                RuntimeError,
                TimeoutError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
            ):
                detailed_inbound = None
            candidates.append(detailed_inbound or _raw_inbound_runtime_namespace(raw_inbound, inbound_id=inbound_id))
        return sorted(
            candidates,
            key=lambda item: int(getattr(item, "id", 0) or 0),
        )

    return sorted(
        api.inbound.get_list() or [],
        key=lambda item: int(getattr(item, "id", 0) or 0),
    )


def _subscription_inbound_matches_mode(
    inbound: Inbound,
    *,
    mode: str,
    white_node_inbound_ids: set[int] | None = None,
) -> bool:
    if mode == SUBSCRIPTION_INBOUND_MODE_ALL:
        return True
    if mode == SUBSCRIPTION_INBOUND_MODE_ALL_EXCEPT_WHITELIST:
        return not _is_white_subscription_inbound(inbound, white_node_inbound_ids)
    raise ValueError(f"Unsupported subscription inbound mode: {mode}")


def _select_subscription_inbounds(
    api: Api,
    primary_inbound: Inbound,
    *,
    mode: str,
    white_node_inbound_ids: set[int] | None = None,
    raw_inbounds: list[dict[str, Any]] | None = None,
    host_name: str = "",
    require_migration_targets: bool = False,
) -> list[Inbound]:
    primary_id = _safe_inbound_id(primary_inbound)
    ordered_inbounds: list[Inbound] = []
    seen_ids: set[int] = set()
    if (
        primary_id > 0
        and _is_supported_subscription_inbound(primary_inbound)
        and _subscription_inbound_matches_mode(
            primary_inbound,
            mode=mode,
            white_node_inbound_ids=white_node_inbound_ids,
        )
    ):
        ordered_inbounds.append(primary_inbound)
        seen_ids.add(primary_id)
    extra_inbounds = _subscription_inbound_candidates(api, raw_inbounds=raw_inbounds)
    for inbound in extra_inbounds:
        inbound_id = int(getattr(inbound, "id", 0) or 0)
        if inbound_id in seen_ids or not _is_supported_subscription_inbound(inbound):
            continue
        if not _subscription_inbound_matches_mode(
            inbound,
            mode=mode,
            white_node_inbound_ids=white_node_inbound_ids,
        ):
            continue
        ordered_inbounds.append(inbound)
        seen_ids.add(inbound_id)
    replaced_ids = subscription_policy.migration_excluded_inbound_ids(host_name)
    if primary_id in replaced_ids:
        raise ValueError("cannot_replace_primary_subscription_inbound")
    selected = [item for item in ordered_inbounds if _safe_inbound_id(item) not in replaced_ids]
    if require_migration_targets and mode == SUBSCRIPTION_INBOUND_MODE_ALL:
        for migration in subscription_policy.inbound_migrations(host_name):
            if migration.get("staged", False):
                continue
            targets = [item for item in selected if _safe_inbound_id(item) == migration["new_inbound_id"]]
            if len(targets) != 1:
                raise ValueError("subscription_migration_target_unavailable")
            subscription_policy.migration_for_target(
                host_name, _safe_inbound_id(targets[0]), _inbound_label(targets[0]),
            )
    return selected


def _subscription_inbounds(
    api: Api,
    primary_inbound: Inbound,
    *,
    include_white_inbounds: bool = True,
    white_node_inbound_ids: set[int] | None = None,
    raw_inbounds: list[dict[str, Any]] | None = None,
    host_name: str = "",
    require_migration_targets: bool = False,
) -> list[Inbound]:
    return _select_subscription_inbounds(
        api,
        primary_inbound,
        mode=_subscription_inbound_mode(include_white_inbounds=include_white_inbounds),
        white_node_inbound_ids=white_node_inbound_ids,
        raw_inbounds=raw_inbounds,
        host_name=host_name,
        require_migration_targets=require_migration_targets,
    )


def _delete_matching_subscription_client_from_inbound(
    api: Api,
    inbound: Inbound,
    *,
    client_uuid: str,
    sub_id: str,
    email: str,
) -> tuple[int, bool]:
    inbound_id = _safe_inbound_id(inbound)
    if inbound_id <= 0:
        return 0, False

    inbound_to_check = api.inbound.get_by_id(inbound_id)
    clients = list(getattr(getattr(inbound_to_check, "settings", None), "clients", None) or [])
    removed = 0
    for client in clients:
        candidate_uuid = str(_client_config_value(client, ("id", "uuid"), "") or "").strip()
        candidate_email = str(_client_config_value(client, ("email",), "") or "").strip()
        candidate_sub_id = str(_extract_client_sub_id(client) or "").strip()
        if not (
            (client_uuid and candidate_uuid == client_uuid)
            or (sub_id and candidate_sub_id == sub_id)
            or (email and candidate_email == email)
        ):
            continue
        if not candidate_email:
            logger.error(
                "Cannot detach matching subscription client without email: inbound_id=%s uuid=%s",
                inbound_id,
                candidate_uuid,
            )
            return removed, False
        if not _detach_client_from_inbound_by_email(api, inbound_id, candidate_email):
            logger.error(
                "Failed to detach excluded white subscription client: inbound_id=%s uuid=%s email=%s",
                inbound_id,
                candidate_uuid,
                candidate_email,
            )
            return removed, False
        removed += 1
    return removed, True


def _client_matches_subscription_identity(
    client: Any,
    *,
    client_uuids: set[str],
    sub_ids: set[str],
    emails: set[str],
) -> bool:
    candidate_uuid = str(_client_config_value(client, ("id", "uuid"), "") or "").strip()
    candidate_email = str(_client_config_value(client, ("email",), "") or "").strip()
    candidate_sub_id = str(_extract_client_sub_id(client) or "").strip()
    return (
        bool(candidate_uuid and candidate_uuid in client_uuids)
        or bool(candidate_sub_id and candidate_sub_id in sub_ids)
        or bool(candidate_email and candidate_email in emails)
    )


def _standard_key_identity_sets(
    keys: list[Mapping[str, Any]],
    white_inbounds: list[Inbound],
    *,
    host_name: str = "",
) -> tuple[set[str], set[str], set[str]]:
    client_uuids: set[str] = set()
    sub_ids: set[str] = set()
    emails: set[str] = set()
    for key in keys:
        client_uuid = str(key.get("xui_client_uuid") or "").strip()
        if client_uuid:
            client_uuids.add(client_uuid)
        sub_id = str(key.get("panel_sub_id") or "").strip()
        if sub_id:
            sub_ids.add(sub_id)
        base_email = str(key.get("key_email") or "").strip()
        if not base_email:
            continue
        emails.add(base_email)
        for inbound in white_inbounds:
            emails.add(_secondary_inbound_email(base_email, inbound, host_name=host_name))
    return client_uuids, sub_ids, emails


def cleanup_standard_key_white_clients_on_host(
    *,
    host_name: str,
    host_data: Mapping[str, Any],
    keys: list[Mapping[str, Any]],
    execute: bool = False,
) -> dict[str, Any]:
    # Kept as a compatibility entry point for older maintenance callers.
    # Standard subscriptions now include capped WHITELIST access, so deleting
    # their bindings would undo the entitlement during a later reconcile.
    del host_data
    return {
        "host_name": host_name,
        "execute": execute,
        "keys_checked": len(keys),
        "white_inbounds_checked": 0,
        "matched_clients": 0,
        "deleted_clients": 0,
        "failed_deletes": 0,
        "inbounds": [],
        "status": "skipped",
        "reason": "standard_whitelist_entitled",
    }


def _mainpanel_whitelist_label(value: str) -> bool:
    return "whitelist" in str(value or "").lower()


def classify_mainpanel_subscription_inbound(inbound: Any) -> str:
    if not _is_supported_subscription_inbound(inbound):
        return MAINPANEL_INBOUND_CLASS_UNSUPPORTED
    if _mainpanel_whitelist_label(getattr(inbound, "remark", "")) or _mainpanel_whitelist_label(
        getattr(inbound, "tag", "")
    ):
        return MAINPANEL_INBOUND_CLASS_WHITELIST
    return MAINPANEL_INBOUND_CLASS_ORDINARY


def get_whitelist_traffic_snapshot(
    host_name: str,
    *,
    target_emails: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return WHITELIST traffic rows from 3x-ui clientStats keyed by normalized email."""
    normalized_host_name = str(host_name or "").strip()
    if not normalized_host_name:
        return {}
    host_data = get_host(normalized_host_name)
    if not host_data:
        return {}

    target_set = {
        _normalized_client_email(email)
        for email in (target_emails or [])
        if _normalized_client_email(email)
    }

    api_token = _resolve_host_api_token(host_data)
    api = _create_api(
        str(host_data["host_url"]),
        str(host_data["host_username"]),
        str(host_data["host_pass"]),
        api_token,
    )
    _authenticate_api(api, api_token)

    snapshot: dict[str, dict[str, Any]] = {}
    for listed_raw_inbound in _list_inbounds_raw(api):
        raw_inbound = listed_raw_inbound
        inbound_id = _coerce_panel_int(raw_inbound.get("id"))
        if inbound_id <= 0 or inbound_id in subscription_policy.migration_excluded_inbound_ids(normalized_host_name):
            continue
        runtime_inbound = _raw_inbound_runtime_namespace(raw_inbound, inbound_id=inbound_id)
        if classify_mainpanel_subscription_inbound(runtime_inbound) != MAINPANEL_INBOUND_CLASS_WHITELIST:
            continue
        if raw_inbound.get("settings") is None and not raw_inbound.get("clientStats"):
            try:
                detailed_raw = _get_inbound_raw_by_id(api, inbound_id)
                if isinstance(detailed_raw, Mapping):
                    raw_inbound = detailed_raw
            except (
                RequestException,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
            ):
                logger.exception("Failed to load detailed WHITELIST inbound: host=%s inbound_id=%s", host_name, inbound_id)
                continue

        try:
            clients_by_email: dict[str, list[dict[str, Any]]] = {}
            for client in _iter_client_dicts_from_settings(raw_inbound.get("settings")):
                client_email = _normalized_client_email(client.get("email"))
                if client_email:
                    clients_by_email.setdefault(client_email, []).append(client)
        except (TypeError, ValueError):
            clients_by_email = {}

        stats_by_email: dict[str, list[Mapping[str, Any]]] = {}
        for stat in raw_inbound.get("clientStats") or []:
            if not isinstance(stat, Mapping):
                continue
            stat_email = _normalized_client_email(stat.get("email"))
            if stat_email:
                stats_by_email.setdefault(stat_email, []).append(stat)

        for email in set(clients_by_email) | set(stats_by_email):
            if target_set and email not in target_set:
                continue
            matching_clients = clients_by_email.get(email, [])
            matching_stats = stats_by_email.get(email, [])
            identity_collision = len(matching_clients) != 1 or len(matching_stats) != 1
            client = matching_clients[0] if len(matching_clients) == 1 else {}
            stat = matching_stats[0] if len(matching_stats) == 1 else {}
            traffic_up_bytes = _coerce_panel_int(stat.get("up"))
            traffic_down_bytes = _coerce_panel_int(stat.get("down"))
            used_bytes = traffic_up_bytes + traffic_down_bytes
            settings_total_bytes = _coerce_panel_int(
                client.get("totalGB", client.get("total_gb", client.get("total")))
            )
            traffic_total_bytes = _coerce_panel_int(stat.get("total"))
            total_bytes = max(
                traffic_total_bytes,
                settings_total_bytes,
            )
            settings_reset_days = _coerce_panel_int(client.get("reset"))
            traffic_reset_days = _coerce_panel_int(stat.get("reset"))
            reset_days = traffic_reset_days if stat else settings_reset_days
            # 3x-ui keeps two different expiry timestamps for auto-renewing
            # clients: clientStats.expiryTime is the next traffic reset, while
            # settings.clients[].expiryTime is the subscription expiry.  Using
            # the later value makes the admin UI display the subscription end
            # date as the next WHITELIST reset.
            settings_expiry_ms = _coerce_panel_int(
                client.get("expiryTime", client.get("expiry_time"))
            )
            traffic_expiry_ms = _coerce_panel_int(
                stat.get("expiryTime", stat.get("expiry_time"))
            )
            cycle_deadline_ms = traffic_expiry_ms or settings_expiry_ms
            reset_timestamp_ms = cycle_deadline_ms if reset_days > 0 else 0
            if (
                reset_timestamp_ms <= 0
                and settings_reset_days > 0
                and settings_expiry_ms > int(time.time() * 1000)
            ):
                # Display the future reset configured on the inbound even when
                # an exhausted traffic row still carries the terminal reset=0.
                # Keep reset_days/cycle_deadline_ms untouched for reconciliation.
                reset_timestamp_ms = settings_expiry_ms
            settings_enable = _coerce_panel_bool(client.get("enable"), default=True)
            traffic_enable = _coerce_panel_bool(stat.get("enable"), default=True)
            panel_enable = traffic_enable if stat else settings_enable
            client_uuid = str(
                client.get("id")
                or client.get("uuid")
                or stat.get("uuid")
                or ""
            ).strip()
            sub_id = str(_extract_client_sub_id(client) or stat.get("subId") or stat.get("sub_id") or "").strip()
            snapshot_key = email
            if snapshot_key in snapshot:
                snapshot_key = f"{email}#{inbound_id}"
                collision_index = 2
                while snapshot_key in snapshot:
                    snapshot_key = f"{email}#{inbound_id}:{collision_index}"
                    collision_index += 1
            snapshot[snapshot_key] = {
                "email": email,
                "client_uuid": client_uuid,
                "sub_id": sub_id,
                "inbound_id": inbound_id,
                "inbound_label": _inbound_label(runtime_inbound),
                "identity_collision": identity_collision,
                "used_bytes": used_bytes,
                "total_bytes": total_bytes,
                "reset_days": reset_days,
                "reset_timestamp_ms": reset_timestamp_ms,
                "cycle_deadline_ms": cycle_deadline_ms,
                "panel_enable": panel_enable,
                "settings_expiry_ms": settings_expiry_ms,
                "settings_reset_days": settings_reset_days,
                "settings_total_bytes": settings_total_bytes,
                "settings_enable": settings_enable,
                "traffic_expiry_ms": traffic_expiry_ms,
                "traffic_reset_days": traffic_reset_days,
                "traffic_total_bytes": traffic_total_bytes,
                "traffic_enable": traffic_enable,
                "traffic_up_bytes": traffic_up_bytes,
                "traffic_down_bytes": traffic_down_bytes,
                "exhausted": bool(total_bytes > 0 and used_bytes >= total_bytes),
            }

    return snapshot


def _cycle_repair_machine_reason(value: Any, *, default: str) -> str:
    raw = str(value or default).strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_]{0,79}", raw):
        return raw
    return default


def _strict_panel_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _exact_whitelist_cycle_repair_state(
    raw_inbound: Mapping[str, Any],
    *,
    email: str,
    client_uuid: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Extract the two independent CAS representations for one exact client."""
    normalized_email = _normalized_client_email(email)
    normalized_uuid = _normalized_client_uuid(client_uuid)
    try:
        raw_clients = list(_iter_client_dicts_from_settings(raw_inbound.get("settings")))
    except (TypeError, ValueError):
        return None, "invalid_inbound_settings"

    candidates = [
        client
        for client in raw_clients
        if _normalized_client_email(client.get("email")) == normalized_email
        or _normalized_client_uuid(client.get("id") or client.get("uuid")) == normalized_uuid
    ]
    if len(candidates) != 1:
        return None, "settings_identity_not_unique"
    client = candidates[0]
    raw_email = str(client.get("email") or "")
    raw_uuid = str(client.get("id") or client.get("uuid") or "")
    raw_sub_id = str(_extract_client_sub_id(client) or "")
    if (
        not raw_email
        or _normalized_client_email(raw_email) != normalized_email
        or not raw_uuid
        or _normalized_client_uuid(raw_uuid) != normalized_uuid
        or not raw_sub_id
        or raw_sub_id.strip() != raw_sub_id
    ):
        return None, "settings_identity_mismatch"

    matching_stats = [
        stat
        for stat in (raw_inbound.get("clientStats") or [])
        if isinstance(stat, Mapping)
        and _normalized_client_email(stat.get("email")) == normalized_email
    ]
    if len(matching_stats) != 1:
        return None, "traffic_identity_not_unique"
    stat = matching_stats[0]

    settings_expiry = _strict_panel_int(client.get("expiryTime"))
    settings_reset = _strict_panel_int(client.get("reset"))
    settings_total = _strict_panel_int(client.get("totalGB"))
    settings_enable = client.get("enable")
    traffic_expiry = _strict_panel_int(stat.get("expiryTime"))
    traffic_reset = _strict_panel_int(stat.get("reset"))
    traffic_total = _strict_panel_int(stat.get("total"))
    traffic_enable = stat.get("enable")
    traffic_up = _strict_panel_int(stat.get("up"))
    traffic_down = _strict_panel_int(stat.get("down"))
    if (
        settings_expiry is None
        or settings_reset is None
        or settings_total is None
        or not isinstance(settings_enable, bool)
    ):
        return None, "settings_state_incomplete"
    if (
        traffic_expiry is None
        or traffic_reset is None
        or traffic_total is None
        or not isinstance(traffic_enable, bool)
        or traffic_up is None
        or traffic_down is None
    ):
        return None, "traffic_state_incomplete"
    if min(settings_total, traffic_total, traffic_up, traffic_down) < 0:
        return None, "unexpected_traffic_state"
    durable_limit = max(settings_total, traffic_total)
    if durable_limit not in {STANDARD_WHITELIST_TRAFFIC_BYTES, MULTI_NODE_LIMITED_TRAFFIC_BYTES} or (
        settings_total not in {0, durable_limit} or traffic_total not in {0, durable_limit}
    ):
        return None, "unexpected_traffic_limit"
    if settings_reset not in {0, MULTI_NODE_LIMIT_RESET_DAYS} or traffic_reset not in {
        0,
        MULTI_NODE_LIMIT_RESET_DAYS,
    }:
        return None, "unexpected_reset_policy"

    return (
        {
            "email": raw_email,
            "uuid": raw_uuid,
            "subId": raw_sub_id,
            "settings": {
                "expiryTime": settings_expiry,
                "reset": settings_reset,
                "total": settings_total,
                "enable": settings_enable,
            },
            "traffic": {
                "expiryTime": traffic_expiry,
                "reset": traffic_reset,
                "total": traffic_total,
                "enable": traffic_enable,
                "up": traffic_up,
                "down": traffic_down,
            },
        },
        None,
    )


def _cycle_repair_representation_matches_target(
    representation: Mapping[str, Any],
    target: Mapping[str, Any],
) -> bool:
    return all(
        int(representation.get(field) or 0) == int(target.get(field) or 0)
        for field in ("expiryTime", "reset", "total")
    )


def _cycle_repair_readback_results(
    raw_inbound: Mapping[str, Any],
    prepared: list[dict[str, Any]],
    *,
    receipt_confirmed: bool,
    ambiguous_post: bool,
) -> tuple[list[dict[str, Any]], bool]:
    results: list[dict[str, Any]] = []
    all_verified = True
    for prepared_item in prepared:
        request_item = prepared_item["request"]
        state, reason = _exact_whitelist_cycle_repair_state(
            raw_inbound,
            email=request_item["email"],
            client_uuid=request_item["uuid"],
        )
        target = request_item["target"]
        reset_traffic = bool(request_item["resetTraffic"])
        expected_traffic = request_item["expectedTraffic"]
        expected_sub_id = str(request_item["subId"])
        sub_id_changed = bool(state and str(state["subId"]) != expected_sub_id)
        traffic_counters_decreased = bool(
            state
            and not reset_traffic
            and (
                int(state["traffic"]["up"]) < int(expected_traffic["up"])
                or int(state["traffic"]["down"]) < int(expected_traffic["down"])
            )
        )
        reset_outcome_ambiguous = bool(ambiguous_post and reset_traffic)
        verified = bool(
            state
            and _cycle_repair_representation_matches_target(state["settings"], target)
            and _cycle_repair_representation_matches_target(state["traffic"], target)
            and not sub_id_changed
            and (
                not reset_traffic
                or (bool(state["settings"]["enable"]) and bool(state["traffic"]["enable"]))
            )
            and not traffic_counters_decreased
            # A readback at the requested target cannot prove whether an
            # outcome-ambiguous destructive reset was committed exactly once.
            # Keep the batch failed so the controller stops instead of
            # treating an unconfirmed receipt as a safe success.
            and not reset_outcome_ambiguous
        )
        if not verified:
            all_verified = False
            if reset_outcome_ambiguous:
                failure_reason = "post_outcome_ambiguous"
            elif traffic_counters_decreased:
                failure_reason = "traffic_counters_decreased"
            else:
                failure_reason = reason or (
                    "post_outcome_ambiguous" if ambiguous_post else "post_write_verify_failed"
                )
            results.append(
                {
                    "index": prepared_item["index"],
                    "status": "failed",
                    "reason": failure_reason,
                    # A failed/ambiguous reset POST must only be resumed with
                    # resetTraffic=false.  Never issue a second reset request.
                    "reset_performed": reset_traffic,
                    "reset_outcome_ambiguous": reset_outcome_ambiguous,
                    "safe_retry_reset_traffic": False,
                    "receipt_confirmed": receipt_confirmed,
                    "verified_reset_ms": int(state["traffic"]["expiryTime"]) if state else 0,
                    "verified_reset_days": int(state["traffic"]["reset"]) if state else -1,
                }
            )
            continue
        results.append(
            {
                "index": prepared_item["index"],
                "status": "updated",
                "reason": "deadline_repaired",
                "verified_reset_ms": int(target["expiryTime"]),
                "verified_reset_days": int(target["reset"]),
                "reset_performed": reset_traffic,
                "reset_suppressed": bool(
                    prepared_item["requested_reset"] and not request_item["resetTraffic"]
                ),
                "receipt_confirmed": receipt_confirmed,
            }
        )
    return results, all_verified


def _remote_cycle_repair_rejection_is_exact(payload: Any, expected_count: int) -> bool:
    """Recognize the panel's exact no-op remote rejection receipt."""
    if not isinstance(payload, Mapping) or payload.get("success") is not False:
        return False
    message = str(payload.get("msg") or "").strip()
    reason = "client traffic cycle repair batch rejected: remote_inbound_unsupported"
    if message not in {reason, f"Something went wrong ({reason})"}:
        return False
    receipt = _api_extract_obj(payload)
    if not isinstance(receipt, Mapping) or _strict_panel_int(receipt.get("updated")) != 0:
        return False
    if receipt.get("needRestart") is not False:
        return False
    items = receipt.get("items")
    if not isinstance(items, list) or len(items) != expected_count:
        return False
    indexes = []
    for item in items:
        if not isinstance(item, Mapping):
            return False
        index = _strict_panel_int(item.get("index"))
        if (index is None or item.get("status") != "not_applied" or item.get("reason") != "batch_rejected"
                or item.get("trafficReset") is not False or item.get("reenabled") is not False):
            return False
        indexes.append(index)
    return sorted(indexes) == list(range(expected_count))


def _repair_remote_whitelist_cycle_metadata(
    api: Api,
    *,
    host_name: str,
    inbound_id: int,
    prepared: list[dict[str, Any]],
    existing_results: Mapping[int, Mapping[str, Any]],
    requested_count: int,
    before_post_callback: Any | None,
) -> dict[str, Any]:
    """Repair remote deadline metadata without resetting or rebinding a client.

    Remote inbounds cannot use the panel's local atomic reset endpoint.  This
    fallback is limited to an explicit rejection of that endpoint and to
    metadata-only requests.  A client with shared bindings is never eligible.
    """
    results = {index: dict(result) for index, result in existing_results.items()}

    def _result(status: str, reason: str, index: int | None = None) -> dict[str, Any]:
        if status == "failed" and index is None:
            index = next((int(item["index"]) for item in prepared if int(item["index"]) not in results), 0)
        if index is not None:
            results[index] = {
                "index": index, "status": status, "reason": reason,
                "reset_performed": False, "safe_retry_reset_traffic": False,
            }
        return {
            "status": status, "reason": reason,
            "items": [results.get(index, {
                "index": index, "status": "skipped", "reason": "batch_stopped",
                "reset_performed": False,
            }) for index in range(requested_count)],
        }

    if any(item["requested_reset"] or item["request"]["resetTraffic"] for item in prepared):
        return _result("failed", "remote_traffic_reset_unsupported")

    def _central_client(email: str) -> dict[str, Any]:
        listed = _api_extract_obj(_api_get_json(
            api, "panel/api/clients/list", skip_check=True, timeout=5.0,
        ))
        if isinstance(listed, Mapping):
            listed = listed.get("clients")
        if not isinstance(listed, list):
            raise TypeError("central_client_snapshot_unavailable")
        matches = [dict(row) for row in listed if isinstance(row, Mapping)
                   and _normalized_client_email(row.get("email")) == _normalized_client_email(email)]
        if len(matches) != 1:
            raise ValueError("central_client_identity_not_unique")
        return matches[0]

    def _read_guarded(item: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        request = item["request"]
        target = request["target"]
        raw = _get_inbound_raw_by_id(
            api, inbound_id, allow_list_fallback=False, request_timeout=5.0,
        )
        runtime = _raw_inbound_runtime_namespace(raw, inbound_id=inbound_id)
        if (classify_mainpanel_subscription_inbound(runtime) != MAINPANEL_INBOUND_CLASS_WHITELIST
                or _safe_inbound_node_id(runtime) <= 0):
            raise ValueError("remote_whitelist_inbound_required")
        state, reason = _exact_whitelist_cycle_repair_state(
            raw, email=request["email"], client_uuid=request["uuid"],
        )
        if state is None:
            raise ValueError(reason or "client_state_invalid")
        if state["subId"] != request["subId"]:
            raise ValueError("stale_sub_id_precondition")
        now_ms = int(time.time() * 1000)
        if min(int(target["expiryTime"]), int(state["settings"]["expiryTime"]),
               int(state["traffic"]["expiryTime"])) <= now_ms:
            raise ValueError("cycle_deadline_elapsed")
        for representation, expected_field in (("settings", "expectedSettings"), ("traffic", "expectedTraffic")):
            expected = request[expected_field]
            actual = state[representation]
            if any(actual[field] != expected[field] for field in ("expiryTime", "reset", "total", "enable")):
                raise ValueError("stale_cycle_metadata_precondition")
            if int(actual["total"]) != int(target["total"]):
                raise ValueError("unexpected_traffic_limit")
        if (state["traffic"]["up"] < request["expectedTraffic"]["up"]
                or state["traffic"]["down"] < request["expectedTraffic"]["down"]):
            raise ValueError("traffic_counters_decreased")
        central = _central_client(request["email"])
        if (_normalized_client_uuid(central.get("uuid") or central.get("id")) != request["uuid"]
                or str(central.get("subId") or "") != request["subId"]
                or central.get("inboundIds") != [inbound_id]):
            raise ValueError("central_client_identity_or_bindings_mismatch")
        if any(central.get(field) != state["settings"][state_field] for field, state_field in (
            ("expiryTime", "expiryTime"), ("reset", "reset"), ("totalGB", "total"), ("enable", "enable"),
        )):
            raise ValueError("central_client_state_mismatch")
        return raw, state, central

    # Preflight every item before applying any part of this non-atomic fallback.
    try:
        for item in prepared:
            _read_guarded(item)
    except (OSError, RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return _result("failed", _cycle_repair_machine_reason(str(exc), default="remote_metadata_preflight_failed"))

    for item in prepared:
        index = int(item["index"])
        request = item["request"]
        target = request["target"]
        try:
            before_raw, before_state, central_before = _read_guarded(item)
            changed = copy.deepcopy(central_before)
            changed.update({"id": request["uuid"], "expiryTime": target["expiryTime"], "reset": target["reset"]})
            changed.pop("traffic", None)
            endpoint = f"panel/api/clients/update/{quote(request['email'], safe='')}?{urlencode({'inboundIds': str(inbound_id)})}"
            if before_post_callback is not None:
                try:
                    before_post_callback(copy.deepcopy({
                        "schema": 1, "captured_at_ms": int(time.time() * 1000), "host_name": host_name,
                        "inbound_id": inbound_id, "endpoint": endpoint, "items": [request],
                        "central_client_before": central_before, "metadata_only": True,
                    }))
                except Exception:  # noqa: BLE001 - a failed private journal must block the metadata POST.
                    return _result("failed", "before_post_journal_failed", index)
            if min(int(target["expiryTime"]), int(before_state["settings"]["expiryTime"]),
                   int(before_state["traffic"]["expiryTime"])) <= int(time.time() * 1000):
                return _result("failed", "cycle_deadline_elapsed", index)
            payload = _api_post_json_once_raw(api, endpoint, changed)
            if not isinstance(payload, Mapping) or payload.get("success") is not True:
                return _result("failed", "remote_metadata_update_rejected", index)
        except (OSError, RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as exc:
            # Never replay an outcome-ambiguous client update.
            return _result("failed", _cycle_repair_machine_reason(str(exc), default="remote_metadata_update_failed"), index)

        verified = False
        failure_reason = "remote_metadata_readback_failed"
        for _attempt in range(3):
            time.sleep(5.0)
            try:
                after_raw = _get_inbound_raw_by_id(
                    api, inbound_id, allow_list_fallback=False, request_timeout=5.0,
                )
                after_state, reason = _exact_whitelist_cycle_repair_state(
                    after_raw, email=request["email"], client_uuid=request["uuid"],
                )
                central_after = _central_client(request["email"])
                preserved_before = {key: value for key, value in central_before.items()
                                    if key not in {"expiryTime", "reset", "traffic", "updatedAt", "updated_at"}}
                preserved_after = {key: value for key, value in central_after.items()
                                   if key not in {"expiryTime", "reset", "traffic", "updatedAt", "updated_at"}}
                if after_raw.get("streamSettings", after_raw.get("stream_settings")) != before_raw.get(
                    "streamSettings", before_raw.get("stream_settings"),
                ):
                    return _result("failed", "stream_settings_changed", index)
                if (after_state and (after_state["traffic"]["up"] < before_state["traffic"]["up"]
                                     or after_state["traffic"]["down"] < before_state["traffic"]["down"])):
                    return _result("failed", "traffic_counters_decreased", index)
                if preserved_before != preserved_after:
                    failure_reason = "central_client_fields_changed"
                    continue
                verified = bool(
                    after_state and after_state["subId"] == request["subId"]
                    and _cycle_repair_representation_matches_target(after_state["settings"], target)
                    and _cycle_repair_representation_matches_target(after_state["traffic"], target)
                    and after_state["settings"]["enable"] == before_state["settings"]["enable"]
                    and after_state["traffic"]["enable"] == before_state["traffic"]["enable"]
                    and central_after.get("expiryTime") == target["expiryTime"]
                    and central_after.get("reset") == target["reset"]
                )
                if verified:
                    break
                failure_reason = reason or "remote_metadata_pending"
            except (OSError, RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
                continue
        if not verified:
            return _result("failed", failure_reason, index)
        results[index] = {
            "index": index, "status": "updated", "reason": "remote_deadline_metadata_repaired",
            "verified_reset_ms": int(target["expiryTime"]), "verified_reset_days": int(target["reset"]),
            "reset_performed": False, "receipt_confirmed": True,
        }
    return _result("updated", "deadline_repaired")


def repair_whitelist_reset_cycles_on_host(
    host_name: str,
    *,
    inbound_id: int,
    repairs: Iterable[Mapping[str, Any]],
    before_post_callback: Any | None = None,
) -> dict[str, Any]:
    """Atomically repair up to 50 exact clients in one WHITELIST inbound.

    The direct inbound read is the CAS snapshot used by the POST.  Remote
    inbounds can repair deadline/reset metadata through an exact client-only
    fallback after an explicit unsupported rejection, without clearing counters.
    A transport-ambiguous POST is read back once and never retried.
    """

    normalized_host = str(host_name or "").strip()
    safe_inbound_id = _coerce_panel_int(inbound_id)
    raw_requested = list(repairs)
    if any(not isinstance(item, Mapping) for item in raw_requested):
        return {"status": "failed", "reason": "invalid_arguments", "items": []}
    requested = [dict(item) for item in raw_requested]
    if (
        not normalized_host
        or safe_inbound_id <= 0
        or not requested
        or len(requested) > WHITELIST_CYCLE_REPAIR_BATCH_MAX
    ):
        return {"status": "failed", "reason": "invalid_arguments", "items": []}

    try:
        scan = scan_mainpanel_subscription_inbounds(normalized_host)
    except (
        OSError,
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        logger.warning("WHITELIST cycle repair panel scan failed host=%s", normalized_host)
        return {"status": "failed", "reason": "panel_scan_failed", "items": []}
    api: Api = scan["api"]
    matching_records = [
        record
        for record in scan.get("inbounds", [])
        if _coerce_panel_int(record.get("inbound_id")) == safe_inbound_id
    ]
    if len(matching_records) != 1:
        return {"status": "skipped", "reason": "inbound_identity_mismatch", "items": []}
    if matching_records[0].get("classification") != MAINPANEL_INBOUND_CLASS_WHITELIST:
        return {"status": "skipped", "reason": "inbound_not_whitelist", "items": []}

    # This is deliberately the last panel read before the journal and POST.
    try:
        before_raw = _get_inbound_raw_by_id(api, safe_inbound_id, allow_list_fallback=False)
    except (
        OSError,
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        logger.warning(
            "WHITELIST cycle repair inbound read failed host=%s inbound_id=%s",
            normalized_host,
            safe_inbound_id,
        )
        return {"status": "failed", "reason": "inbound_read_failed", "items": []}
    runtime_inbound = _raw_inbound_runtime_namespace(before_raw, inbound_id=safe_inbound_id)
    if classify_mainpanel_subscription_inbound(runtime_inbound) != MAINPANEL_INBOUND_CLASS_WHITELIST:
        return {"status": "skipped", "reason": "inbound_not_whitelist", "items": []}
    before_stream_settings = copy.deepcopy(
        before_raw.get("streamSettings", before_raw.get("stream_settings"))
    )

    prepared: list[dict[str, Any]] = []
    result_by_index: dict[int, dict[str, Any]] = {}
    seen_emails: set[str] = set()
    seen_uuids: set[str] = set()
    for index, requested_item in enumerate(requested):
        normalized_email = _normalized_client_email(requested_item.get("email"))
        normalized_uuid = _normalized_client_uuid(requested_item.get("expected_client_uuid"))
        raw_expected_sub_id = requested_item.get("expected_sub_id")
        expected_sub_id = raw_expected_sub_id if isinstance(raw_expected_sub_id, str) else ""
        try:
            uuid_is_canonical = str(uuid.UUID(normalized_uuid)) == normalized_uuid
        except (ValueError, TypeError, AttributeError):
            uuid_is_canonical = False
        raw_target_reset_ms = requested_item.get("target_reset_ms")
        raw_target_reset_days = requested_item.get("target_reset_days")
        target_reset_ms = _coerce_panel_int(raw_target_reset_ms)
        target_reset_days = _coerce_panel_int(raw_target_reset_days)
        raw_requested_reset = requested_item.get("reset_traffic")
        requested_reset = bool(raw_requested_reset)
        if (
            not normalized_email
            or not normalized_uuid
            or not uuid_is_canonical
            or not expected_sub_id
            or expected_sub_id.strip() != expected_sub_id
            or isinstance(raw_target_reset_ms, bool)
            or not isinstance(raw_target_reset_ms, int)
            or isinstance(raw_target_reset_days, bool)
            or not isinstance(raw_target_reset_days, int)
            or target_reset_ms <= 0
            or target_reset_days not in {0, MULTI_NODE_LIMIT_RESET_DAYS}
            or not isinstance(raw_requested_reset, bool)
            or normalized_email in seen_emails
            or normalized_uuid in seen_uuids
        ):
            return {"status": "failed", "reason": "invalid_repair_item", "items": []}
        seen_emails.add(normalized_email)
        seen_uuids.add(normalized_uuid)
        state, reason = _exact_whitelist_cycle_repair_state(
            before_raw,
            email=normalized_email,
            client_uuid=normalized_uuid,
        )
        if state is None:
            return {
                "status": "skipped",
                "reason": reason or "client_state_invalid",
                "items": [
                    {
                        "index": index,
                        "status": "skipped",
                        "reason": reason or "client_state_invalid",
                        "reset_performed": False,
                    }
                ],
            }
        if str(state["subId"]) != expected_sub_id:
            return {
                "status": "failed",
                "reason": "stale_sub_id_precondition",
                "items": [
                    {
                        "index": index,
                        "status": "failed",
                        "reason": "stale_sub_id_precondition",
                        "reset_performed": False,
                    }
                ],
            }
        target = {
            "expiryTime": target_reset_ms,
            "reset": target_reset_days,
            "total": max(int(state["settings"]["total"]), int(state["traffic"]["total"])),
        }
        settings_at_target = _cycle_repair_representation_matches_target(
            state["settings"], target
        )
        traffic_at_target = _cycle_repair_representation_matches_target(
            state["traffic"], target
        )
        if settings_at_target and traffic_at_target:
            result_by_index[index] = {
                "index": index,
                "status": "already_correct",
                "reason": "deadline_already_correct",
                "verified_reset_ms": target_reset_ms,
                "verified_reset_days": target_reset_days,
                "reset_performed": False,
                "reset_suppressed": requested_reset,
            }
            continue

        # If either durable representation already has the target cycle, a
        # previous/legacy reset may have happened.  Heal drift without ever
        # clearing counters a second time.
        effective_reset = requested_reset and not (settings_at_target or traffic_at_target)
        request_item = {
            "inboundId": safe_inbound_id,
            "email": state["email"],
            "uuid": state["uuid"],
            "subId": state["subId"],
            "expectedSettings": dict(state["settings"]),
            "expectedTraffic": dict(state["traffic"]),
            "target": target,
            "resetTraffic": effective_reset,
        }
        prepared.append(
            {
                "index": index,
                "request": request_item,
                "requested_reset": requested_reset,
            }
        )

    if not prepared:
        ordered = [result_by_index[index] for index in range(len(requested))]
        return {"status": "already_correct", "reason": "deadline_already_correct", "items": ordered}

    journal_payload = {
        "schema": 1,
        "captured_at_ms": int(time.time() * 1000),
        "host_name": normalized_host,
        "inbound_id": safe_inbound_id,
        "endpoint": f"panel/api/inbounds/{safe_inbound_id}/repairClientTrafficCycles",
        "items": [dict(item["request"]) for item in prepared],
    }
    if before_post_callback is not None:
        try:
            before_post_callback(copy.deepcopy(journal_payload))
        except Exception:  # noqa: BLE001 - callback failures must block the POST without leaking details.
            logger.warning(
                "WHITELIST cycle repair journal failed host=%s inbound_id=%s",
                normalized_host,
                safe_inbound_id,
            )
            return {"status": "failed", "reason": "before_post_journal_failed", "items": []}

    endpoint = f"panel/api/inbounds/{safe_inbound_id}/repairClientTrafficCycles"
    ambiguous_post = False
    try:
        payload = _api_post_json_once_raw(
            api,
            endpoint,
            {"items": [item["request"] for item in prepared]},
        )
    except (
        OSError,
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        # No retry: the server may have committed before the connection failed.
        ambiguous_post = True
        payload = None

    if not ambiguous_post:
        obj = _api_extract_obj(payload)
        if not isinstance(payload, Mapping) or payload.get("success") is not True:
            item_results = obj.get("items") if isinstance(obj, Mapping) else None
            rejected_reason = "repair_rejected"
            rejected_index = 0
            if isinstance(item_results, list):
                for raw_result in item_results:
                    if not isinstance(raw_result, Mapping):
                        continue
                    if str(raw_result.get("status") or "") == "rejected":
                        rejected_index = _coerce_panel_int(raw_result.get("index"))
                        rejected_reason = _cycle_repair_machine_reason(
                            raw_result.get("reason"), default="repair_rejected"
                        )
                        break
            if _remote_cycle_repair_rejection_is_exact(payload, len(prepared)):
                return _repair_remote_whitelist_cycle_metadata(
                    api, host_name=normalized_host, inbound_id=safe_inbound_id,
                    prepared=prepared, existing_results=result_by_index,
                    requested_count=len(requested), before_post_callback=before_post_callback,
                )
            original_index = prepared[min(max(rejected_index, 0), len(prepared) - 1)]["index"]
            return {
                "status": "failed",
                "reason": rejected_reason,
                "items": [
                    {
                        "index": original_index,
                        "status": "failed",
                        "reason": rejected_reason,
                        "reset_performed": False,
                    }
                ],
            }
        if not isinstance(obj, Mapping) or _coerce_panel_int(obj.get("updated")) != len(prepared):
            ambiguous_post = True
        else:
            receipt_items = obj.get("items")
            if not isinstance(receipt_items, list) or len(receipt_items) != len(prepared):
                ambiguous_post = True
            else:
                by_index = {
                    _coerce_panel_int(item.get("index")): item
                    for item in receipt_items
                    if isinstance(item, Mapping)
                }
                for endpoint_index, prepared_item in enumerate(prepared):
                    receipt = by_index.get(endpoint_index)
                    if (
                        not isinstance(receipt, Mapping)
                        or str(receipt.get("status") or "") != "updated"
                        or str(receipt.get("reason") or "") != "applied"
                        or bool(receipt.get("trafficReset"))
                        != bool(prepared_item["request"]["resetTraffic"])
                    ):
                        ambiguous_post = True
                        break

    try:
        after_raw = _get_inbound_raw_by_id(api, safe_inbound_id, allow_list_fallback=False)
    except (
        OSError,
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        failed_items = [
            {
                "index": item["index"],
                "status": "failed",
                "reason": "post_outcome_ambiguous" if ambiguous_post else "post_write_read_failed",
                "reset_performed": bool(item["request"]["resetTraffic"]),
                "reset_outcome_ambiguous": bool(
                    ambiguous_post and item["request"]["resetTraffic"]
                ),
                "safe_retry_reset_traffic": False,
                "receipt_confirmed": not ambiguous_post,
            }
            for item in prepared
        ]
        return {
            "status": "failed",
            "reason": failed_items[0]["reason"],
            "items": failed_items,
        }
    after_stream_settings = after_raw.get("streamSettings", after_raw.get("stream_settings"))
    if after_stream_settings != before_stream_settings:
        for item in prepared:
            result_by_index[item["index"]] = {
                "index": item["index"],
                "status": "failed",
                "reason": "stream_settings_changed",
                "reset_performed": bool(item["request"]["resetTraffic"]),
                "reset_outcome_ambiguous": bool(
                    ambiguous_post and item["request"]["resetTraffic"]
                ),
                "safe_retry_reset_traffic": False,
                "receipt_confirmed": not ambiguous_post,
            }
        return {
            "status": "failed",
            "reason": "stream_settings_changed",
            "items": [result_by_index[index] for index in range(len(requested))],
        }

    readback, all_verified = _cycle_repair_readback_results(
        after_raw,
        prepared,
        receipt_confirmed=not ambiguous_post,
        ambiguous_post=ambiguous_post,
    )
    for item in readback:
        result_by_index[int(item["index"])] = item
    ordered_results = [
        result_by_index.get(
            index,
            {
                "index": index,
                "status": "failed",
                "reason": "missing_result",
                "reset_performed": False,
            },
        )
        for index in range(len(requested))
    ]
    if not all_verified:
        first_failure = next(
            (
                item
                for item in ordered_results
                if str(item.get("status") or "") == "failed"
            ),
            None,
        )
        failure_reason = (
            str(first_failure.get("reason") or "post_write_verify_failed")
            if isinstance(first_failure, Mapping)
            else "post_write_verify_failed"
        )
        return {"status": "failed", "reason": failure_reason, "items": ordered_results}
    return {"status": "updated", "reason": "deadline_repaired", "items": ordered_results}


def repair_whitelist_reset_cycle_on_host(
    host_name: str,
    *,
    inbound_id: int,
    email: str,
    expected_client_uuid: str,
    expected_sub_id: str,
    target_reset_ms: int,
    target_reset_days: int,
    reset_traffic: bool,
    before_post_callback: Any | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for one atomic cycle-repair item."""
    batch_result = repair_whitelist_reset_cycles_on_host(
        host_name,
        inbound_id=inbound_id,
        repairs=[
            {
                "email": email,
                "expected_client_uuid": expected_client_uuid,
                "expected_sub_id": expected_sub_id,
                "target_reset_ms": target_reset_ms,
                "target_reset_days": target_reset_days,
                "reset_traffic": reset_traffic,
            }
        ],
        before_post_callback=before_post_callback,
    )
    items = batch_result.get("items")
    if isinstance(items, list) and items and isinstance(items[0], Mapping):
        return {key: value for key, value in items[0].items() if key != "index"}
    return {
        "status": str(batch_result.get("status") or "failed"),
        "reason": str(batch_result.get("reason") or "repair_failed"),
        "reset_performed": False,
    }


def reconcile_active_whitelist_reset_cycles(
    host_name: str,
    *,
    keys: Iterable[Mapping[str, Any]] | None = None,
    snapshot: Mapping[str, Mapping[str, Any]] | None = None,
    now_ms: int | None = None,
) -> dict[str, int]:
    """Heal active paid WHITELIST deadlines without touching inbound config.

    3x-ui advances an auto-renewing traffic deadline by the configured reset
    period.  The final automatic advance can pass the paid entitlement, so the
    runtime monitor must cap that last deadline at paid expiry and switch the
    reset policy to zero.  Only exact, unambiguous tariff-capped client rows are
    eligible; every mutation is delegated to the guarded client-level helper.
    """
    normalized_host = str(host_name or "").strip()
    report = {
        "checked": 0,
        "already_correct": 0,
        "updated": 0,
        "needs_review": 0,
        "failed": 0,
    }
    if not normalized_host:
        report["failed"] = 1
        return report

    safe_now_ms = int(time.time() * 1000) if now_ms is None else max(0, int(now_ms))
    active_keys = list(keys) if keys is not None else get_active_keys_for_host(normalized_host)
    traffic_snapshot = (
        dict(snapshot)
        if snapshot is not None
        else get_whitelist_traffic_snapshot(normalized_host)
    )
    stats_by_uuid: dict[str, list[dict[str, Any]]] = {}
    for stat in traffic_snapshot.values():
        if not isinstance(stat, Mapping):
            continue
        if _coerce_panel_int(stat.get("inbound_id")) in subscription_policy.migration_excluded_inbound_ids(normalized_host):
            continue
        client_uuid = _normalized_client_uuid(stat.get("client_uuid"))
        if client_uuid:
            stats_by_uuid.setdefault(client_uuid, []).append(dict(stat))

    for key in active_keys:
        if str(key.get("host_name") or "").strip() != normalized_host:
            continue
        if _mainpanel_resolve_key_tier(key) not in {SUBSCRIPTION_TIER_STANDARD, SUBSCRIPTION_TIER_SUPER}:
            continue
        if standard_trial_is_ordinary_only(key):
            continue
        if whitelist_traffic_limit_bytes_for_key(key) not in {
            0, STANDARD_WHITELIST_TRAFFIC_BYTES, MULTI_NODE_LIMITED_TRAFFIC_BYTES
        }:
            continue
        try:
            if bool(int(key.get("is_archived") or 0)):
                continue
        except (TypeError, ValueError):
            report["needs_review"] += 1
            continue

        paid_expiry_ms = _mainpanel_key_expiry_ms(key) or 0
        anchor_ms = _datetime_value_ms(
            key.get("whitelist_cycle_anchor_at") or key.get("created_date")
        )
        client_uuid = _normalized_client_uuid(key.get("xui_client_uuid"))
        if (
            paid_expiry_ms <= safe_now_ms
            or anchor_ms <= 0
            or anchor_ms >= paid_expiry_ms
            or not client_uuid
        ):
            continue

        matches = stats_by_uuid.get(client_uuid, [])
        if not matches:
            report["checked"] += 1
            report["needs_review"] += 1
            continue
        inbound_ids = [_coerce_panel_int(stat.get("inbound_id")) for stat in matches]
        if (
            any(inbound_id <= 0 for inbound_id in inbound_ids)
            or len(set(inbound_ids)) != len(inbound_ids)
        ):
            report["checked"] += len(matches)
            report["needs_review"] += 1
            continue

        base_email = str(key.get("key_email") or "").strip()
        unlimited_whitelist = _key_uses_unlimited_whitelist(
            key,
            paid_expiry_ms=paid_expiry_ms,
        )
        for stat in matches:
            report["checked"] += 1
            component_email = str(stat.get("email") or "").strip()
            raw_sub_id = stat.get("sub_id")
            expected_sub_id = raw_sub_id if isinstance(raw_sub_id, str) else ""
            if (
                not component_email
                or not expected_sub_id
                or expected_sub_id.strip() != expected_sub_id
                or not _subscription_email_variant(base_email, component_email)
            ):
                report["needs_review"] += 1
                continue

            reset_days = _coerce_panel_int(stat.get("reset_days"))
            total_bytes = max(0, _coerce_panel_int(stat.get("total_bytes")))
            if unlimited_whitelist:
                if (
                    total_bytes == 0
                    and reset_days == 0
                    and _coerce_panel_bool(stat.get("panel_enable"), default=True)
                ):
                    report["already_correct"] += 1
                else:
                    report["needs_review"] += 1
                continue
            if total_bytes != whitelist_traffic_limit_bytes_for_key(key):
                report["needs_review"] += 1
                continue
            if reset_days not in {0, MULTI_NODE_LIMIT_RESET_DAYS}:
                report["needs_review"] += 1
                continue
            used_bytes = max(0, _coerce_panel_int(stat.get("used_bytes")))
            if (
                not _coerce_panel_bool(stat.get("panel_enable"), default=True)
                and used_bytes < total_bytes
            ):
                report["needs_review"] += 1
                continue

            current_reset_ms = _coerce_panel_int(
                stat.get("cycle_deadline_ms", stat.get("reset_timestamp_ms"))
            )
            target_reset_ms, reset_due = _next_whitelist_reset_deadline_ms(
                anchor_ms,
                current_reset_ms,
                paid_expiry_ms,
                safe_now_ms,
                current_reset_days=reset_days,
            )
            target_reset_days = (
                0
                if target_reset_ms >= paid_expiry_ms
                else MULTI_NODE_LIMIT_RESET_DAYS
            )
            if (
                abs(current_reset_ms - target_reset_ms)
                <= WHITELIST_RESET_MATCH_TOLERANCE_MS
                and reset_days == target_reset_days
                and not reset_due
            ):
                report["already_correct"] += 1
                continue

            # The always-on runtime guard is deliberately terminal-only.  It
            # must never turn deploy into an unbounded legacy traffic-reset
            # backfill; overdue/intermediate repairs require the protected
            # dry-run, backup, canary, and batched maintenance command.
            if reset_due or target_reset_days != 0:
                report["needs_review"] += 1
                continue

            result = repair_whitelist_reset_cycle_on_host(
                normalized_host,
                inbound_id=_coerce_panel_int(stat.get("inbound_id")),
                email=component_email,
                expected_client_uuid=client_uuid,
                expected_sub_id=expected_sub_id,
                target_reset_ms=target_reset_ms,
                target_reset_days=target_reset_days,
                reset_traffic=reset_due,
            )
            status = str(result.get("status") or "failed")
            if status == "updated":
                report["updated"] += 1
            elif status == "already_correct":
                report["already_correct"] += 1
            elif status == "skipped":
                report["needs_review"] += 1
            else:
                report["failed"] += 1

    return report


def _mainpanel_inbound_settings_hash(raw_inbound: Mapping[str, Any]) -> str:
    stable_payload = {
        "id": raw_inbound.get("id"),
        "remark": raw_inbound.get("remark"),
        "tag": raw_inbound.get("tag"),
        "protocol": raw_inbound.get("protocol"),
        "enable": raw_inbound.get("enable"),
        "port": raw_inbound.get("port"),
        "streamSettings": raw_inbound.get("streamSettings", raw_inbound.get("stream_settings")),
    }
    encoded = json.dumps(stable_payload, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mainpanel_inbound_record(
    raw_inbound: Mapping[str, Any],
    *,
    primary_inbound_id: int,
    previous_snapshot: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any] | None:
    inbound_id = _coerce_panel_int(raw_inbound.get("id"))
    if inbound_id <= 0:
        return None
    runtime_inbound = _raw_inbound_runtime_namespace(raw_inbound, inbound_id=inbound_id)
    classification = classify_mainpanel_subscription_inbound(runtime_inbound)
    return {
        "inbound_id": inbound_id,
        "id": inbound_id,
        "remark": str(raw_inbound.get("remark") or ""),
        "tag": str(raw_inbound.get("tag") or raw_inbound.get("remark") or ""),
        "protocol": str(raw_inbound.get("protocol") or ""),
        "enable": _coerce_panel_bool(raw_inbound.get("enable"), default=True),
        "classification": classification,
        "label": _inbound_label(runtime_inbound),
        "is_primary": inbound_id == primary_inbound_id,
        "is_new": inbound_id not in previous_snapshot,
        "settings_hash": _mainpanel_inbound_settings_hash(raw_inbound),
        "runtime": runtime_inbound,
    }


def scan_mainpanel_subscription_inbounds(host_name: str) -> dict[str, Any]:
    normalized_host = str(host_name or "").strip()
    if not normalized_host:
        raise ValueError("mainpanel_host_required")
    host_data = get_host(normalized_host)
    if not host_data:
        raise ValueError(f"mainpanel_host_not_found:{normalized_host}")

    api, primary_inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=_resolve_host_api_token(host_data),
        prefer_raw_inbound_lookup=True,
    )
    if not api or not primary_inbound:
        raise ManagedSubscriptionError("mainpanel_inbound_scan_login_failed")

    primary_inbound_id = _safe_inbound_id(primary_inbound)
    previous_snapshot = get_mainpanel_inbound_snapshot(normalized_host)
    raw_inbounds = _list_inbounds_raw(api)
    if not raw_inbounds and primary_inbound_id > 0:
        raw_inbounds = [
            {
                "id": primary_inbound_id,
                "remark": getattr(primary_inbound, "remark", ""),
                "tag": getattr(primary_inbound, "tag", ""),
                "protocol": getattr(primary_inbound, "protocol", ""),
                "enable": getattr(primary_inbound, "enable", True),
                "port": getattr(primary_inbound, "port", 0),
                "streamSettings": getattr(primary_inbound, "stream_settings", None),
            }
        ]

    records: list[dict[str, Any]] = []
    for raw_inbound in raw_inbounds:
        record = _mainpanel_inbound_record(
            raw_inbound,
            primary_inbound_id=primary_inbound_id,
            previous_snapshot=previous_snapshot,
        )
        if record:
            records.append(record)
    records.sort(key=lambda item: int(item["inbound_id"]))
    return {
        "host_name": normalized_host,
        "host_data": host_data,
        "api": api,
        "primary_inbound": primary_inbound,
        "primary_inbound_id": primary_inbound_id,
        "raw_inbounds": raw_inbounds,
        "inbounds": records,
    }


def _mainpanel_raw_inbound_with_clients(
    api: Api,
    raw_inbound: Mapping[str, Any] | None,
    *,
    inbound_id: int,
) -> Mapping[str, Any] | None:
    if raw_inbound and raw_inbound.get("settings") is not None:
        return raw_inbound
    try:
        detailed = _get_inbound_raw_by_id(api, inbound_id, allow_list_fallback=True)
    except (
        RequestException,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ) as exc:
        logger.warning("MainPanel inbound sync could not load clients for inbound_id=%s: %s", inbound_id, exc)
        return raw_inbound
    return detailed if isinstance(detailed, Mapping) else raw_inbound


def _mainpanel_find_matching_raw_client(
    raw_inbound: Mapping[str, Any] | None,
    *,
    email: str | None = None,
    client_uuid: str | None = None,
    sub_id: str | None = None,
) -> dict[str, Any] | None:
    if not raw_inbound:
        return None
    target_email = _normalized_client_email(email)
    target_uuid = _normalized_client_uuid(client_uuid)
    target_sub_id = str(sub_id or "").strip()
    try:
        raw_clients = list(_iter_client_dicts_from_settings(raw_inbound.get("settings")))
    except (TypeError, ValueError):
        return None
    for raw_client in raw_clients:
        raw_email = _normalized_client_email(raw_client.get("email"))
        raw_uuid = _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid"))
        raw_sub_id = str(_extract_client_sub_id(raw_client) or "").strip()
        if (
            (target_uuid and raw_uuid == target_uuid)
            or (target_sub_id and raw_sub_id == target_sub_id)
            or (target_email and raw_email == target_email)
        ):
            return raw_client
    return None


def _mainpanel_key_expiry_ms(key: Mapping[str, Any]) -> int | None:
    raw_expiry = key.get("expiry_date")
    if not raw_expiry:
        return None
    if isinstance(raw_expiry, datetime):
        expiry_dt = raw_expiry
    else:
        normalized = str(raw_expiry).strip().replace("Z", "+00:00")
        try:
            expiry_dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return int(expiry_dt.timestamp() * 1000)


def _mainpanel_resolve_key_tier(key: Mapping[str, Any]) -> str | None:
    explicit_tier = normalize_subscription_tier(key.get("subscription_tier"))
    if explicit_tier:
        return explicit_tier
    email = str(key.get("key_email") or "").strip().lower()
    if email.endswith("-trial-super@telegram.bot"):
        return SUBSCRIPTION_TIER_SUPER
    return None


def _mainpanel_target_email(base_email: str, inbound: Any, *, primary_inbound_id: int, host_name: str = "") -> str:
    if _safe_inbound_id(inbound) == primary_inbound_id:
        return str(base_email or "").strip()
    return _secondary_inbound_email(base_email, inbound, host_name=host_name)


def _mainpanel_tier_can_receive_inbound(tier: str, classification: str) -> bool:
    if classification == MAINPANEL_INBOUND_CLASS_ORDINARY:
        return tier in {SUBSCRIPTION_TIER_STANDARD, SUBSCRIPTION_TIER_SUPER}
    if classification == MAINPANEL_INBOUND_CLASS_WHITELIST:
        return tier in {SUBSCRIPTION_TIER_STANDARD, SUBSCRIPTION_TIER_SUPER}
    return False


def standard_trial_is_ordinary_only(key: Mapping[str, Any] | None) -> bool:
    """Compatibility predicate: Standard trials now include capped WHITELIST.

    The quota resolver returns 5 GiB for Standard; the existing cycle resolver
    caps the client deadline at the unchanged trial expiry with reset=0.
    """
    return False


def whitelist_traffic_limit_bytes_for_key(key: Mapping[str, Any] | None) -> int:
    """Resolve client-level WHITELIST quota while preserving explicit exceptions."""
    row = key or {}
    if _key_uses_unlimited_whitelist(row, paid_expiry_ms=_mainpanel_key_expiry_ms(row) or 0):
        return 0
    email = str(row.get("key_email") or "").strip().lower()
    if email.endswith("-trial-super@telegram.bot"):
        return TRIAL_SUPER_WHITELIST_TRAFFIC_BYTES
    if _mainpanel_resolve_key_tier(row) == SUBSCRIPTION_TIER_STANDARD:
        return STANDARD_WHITELIST_TRAFFIC_BYTES
    return MULTI_NODE_LIMITED_TRAFFIC_BYTES


def _mainpanel_inbound_total_gb_bytes(
    classification: str,
    *,
    unlimited_whitelist: bool = False,
    subscription_tier: Any = None,
    is_trial_super: bool = False,
) -> int:
    if classification == MAINPANEL_INBOUND_CLASS_WHITELIST:
        if unlimited_whitelist:
            return 0
        if is_trial_super:
            return TRIAL_SUPER_WHITELIST_TRAFFIC_BYTES
        if normalize_subscription_tier(subscription_tier) == SUBSCRIPTION_TIER_STANDARD:
            return STANDARD_WHITELIST_TRAFFIC_BYTES
        return MULTI_NODE_LIMITED_TRAFFIC_BYTES
    return 0


def _mainpanel_existing_client_repair_reason(
    existing_client: Mapping[str, Any] | None,
    *,
    inbound: Any,
    expected_expiry_ms: int | None,
    expected_total_gb_bytes: int,
    expected_reset_days: int | None = None,
    require_exact_expiry: bool = False,
    require_enabled: bool = True,
) -> str | None:
    if not existing_client:
        return "missing"
    if require_enabled and not _coerce_panel_bool(existing_client.get("enable"), default=True):
        return "disabled"
    expiry_ms = _coerce_panel_int(
        existing_client.get("expiryTime", existing_client.get("expiry_time"))
    )
    now_ms = int(time.time() * 1000)
    if expiry_ms and expiry_ms < now_ms:
        return "expired"
    if expected_expiry_ms and expiry_ms:
        expiry_delta_ms = abs(expiry_ms - int(expected_expiry_ms))
        if require_exact_expiry and expiry_delta_ms > WHITELIST_RESET_MATCH_TOLERANCE_MS:
            return "wrong_cycle_deadline"
        if not require_exact_expiry and expiry_ms + 600000 < int(expected_expiry_ms):
            return "stale_expiry"
    total_gb = _coerce_panel_int(existing_client.get("totalGB", existing_client.get("total_gb")))
    if total_gb != max(0, int(expected_total_gb_bytes or 0)):
        return "wrong_total_gb"
    if expected_reset_days is not None:
        actual_reset_days = _coerce_panel_int(existing_client.get("reset"))
        if actual_reset_days != int(expected_reset_days):
            return "wrong_reset_policy"
    if _is_hysteria_subscription_inbound(inbound):
        if not str(existing_client.get("auth") or existing_client.get("password") or "").strip():
            return "missing_auth"
        security = str(existing_client.get("security") or "").strip().lower()
        if security not in {"", "auto"}:
            return "non_auto_security"
    return None


def _mainpanel_item(
    *,
    key: Mapping[str, Any],
    inbound_record: Mapping[str, Any] | None,
    tier: str | None,
    status: str,
    action: str,
    error: str = "",
) -> dict[str, Any]:
    return {
        "key_id": key.get("key_id"),
        "user_id": key.get("user_id"),
        "email": key.get("key_email"),
        "inbound_id": inbound_record.get("inbound_id") if inbound_record else None,
        "inbound_label": inbound_record.get("label") if inbound_record else "",
        "classification": inbound_record.get("classification") if inbound_record else "",
        "tier": tier or "",
        "status": status,
        "action": action,
        "error": error,
    }


def _mainpanel_report_from_run(run_id: str, fallback: Mapping[str, Any] | None = None) -> dict[str, Any]:
    persisted = get_mainpanel_inbound_sync_run(run_id, include_items=True) or {}
    result = dict(fallback or {})
    result.update(persisted)
    if "items" in result and "details" not in result:
        result["details"] = result["items"]
    return result


def sync_existing_mainpanel_clients_to_inbounds(
    host_name: str,
    *,
    dry_run: bool = True,
    batch_limit: int = 100,
    mode: str | None = None,
    persist_snapshot: bool | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    normalized_host = str(host_name or "").strip()
    if not normalized_host:
        raise ValueError("mainpanel_host_required")
    try:
        safe_batch_limit = max(0, int(batch_limit or 0))
    except (TypeError, ValueError):
        safe_batch_limit = 100
    run_mode = str(mode or ("dry_run" if dry_run else "manual")).strip() or "manual"
    run_id = create_mainpanel_inbound_sync_run(
        host_name=normalized_host,
        mode=run_mode,
        batch_limit=safe_batch_limit,
        run_id=run_id,
    )

    counters = {
        "processed": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "needs_review": 0,
    }
    details: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "run_id": run_id,
        "host_name": normalized_host,
        "dry_run": dry_run,
        "mode": run_mode,
        "status": "running",
        "inbounds": [],
        "details": details,
        "new_inbounds": 0,
        "total": 0,
        **counters,
    }

    def _record(item: dict[str, Any], *, counter: str | None = None) -> None:
        details.append(item)
        add_mainpanel_inbound_sync_item(run_id, item)
        if counter:
            counters[counter] += 1

    try:
        scan = scan_mainpanel_subscription_inbounds(normalized_host)
        api: Api = scan["api"]
        primary_inbound_id = int(scan["primary_inbound_id"] or 0)
        raw_inbounds = list(scan["raw_inbounds"] or [])
        raw_by_id = {_coerce_panel_int(raw.get("id")): raw for raw in raw_inbounds}
        inbound_records = [
            record
            for record in scan["inbounds"]
            if record["classification"] in {MAINPANEL_INBOUND_CLASS_ORDINARY, MAINPANEL_INBOUND_CLASS_WHITELIST}
            and record["inbound_id"] not in subscription_policy.migration_excluded_inbound_ids(normalized_host)
        ]
        report["inbounds"] = [
            {key: value for key, value in record.items() if key != "runtime"} for record in scan["inbounds"]
        ]
        report["new_inbounds"] = sum(1 for record in scan["inbounds"] if record.get("is_new"))
        if persist_snapshot is None:
            persist_snapshot = not dry_run
        if persist_snapshot:
            upsert_mainpanel_inbound_snapshot(normalized_host, report["inbounds"])

        active_keys = get_active_keys_for_host(normalized_host)
        if safe_batch_limit:
            active_keys = active_keys[:safe_batch_limit]
        report["total"] = len(active_keys)

        for key in active_keys:
            counters["processed"] += 1
            tier = _mainpanel_resolve_key_tier(key)
            if not tier:
                _record(
                    _mainpanel_item(
                        key=key,
                        inbound_record=None,
                        tier=None,
                        status="needs_review",
                        action="resolve_tier",
                        error="subscription_tier_missing",
                    ),
                    counter="needs_review",
                )
                update_mainpanel_inbound_sync_run(run_id, **counters)
                continue

            base_email = str(key.get("key_email") or "").strip()
            client_uuid = str(key.get("xui_client_uuid") or "").strip()
            expiry_ms = _mainpanel_key_expiry_ms(key)
            primary_raw = _mainpanel_raw_inbound_with_clients(
                api,
                raw_by_id.get(primary_inbound_id),
                inbound_id=primary_inbound_id,
            )
            source_client = _mainpanel_find_matching_raw_client(
                primary_raw,
                email=base_email,
                client_uuid=client_uuid,
                sub_id=key.get("panel_sub_id"),
            )
            sub_id = str(key.get("panel_sub_id") or _extract_client_sub_id(source_client) or "").strip()
            if not base_email or not client_uuid or not sub_id or not expiry_ms:
                _record(
                    _mainpanel_item(
                        key=key,
                        inbound_record=None,
                        tier=tier,
                        status="needs_review",
                        action="resolve_identity",
                        error="client_uuid_sub_id_or_expiry_missing",
                    ),
                    counter="needs_review",
                )
                update_mainpanel_inbound_sync_run(run_id, **counters)
                continue

            key_updated = False
            for inbound_record in inbound_records:
                classification = str(inbound_record["classification"])
                inbound = inbound_record["runtime"]
                inbound_id = int(inbound_record["inbound_id"])
                if (
                    not _mainpanel_tier_can_receive_inbound(tier, classification)
                    or (classification == MAINPANEL_INBOUND_CLASS_WHITELIST
                        and standard_trial_is_ordinary_only(key))
                ):
                    _record(
                        _mainpanel_item(
                            key=key,
                            inbound_record=inbound_record,
                            tier=tier,
                            status="skipped",
                            action="tier_excluded",
                        ),
                        counter="skipped",
                    )
                    continue

                raw_with_clients = _mainpanel_raw_inbound_with_clients(
                    api,
                    raw_by_id.get(inbound_id),
                    inbound_id=inbound_id,
                )
                target_email = _mainpanel_target_email(base_email, inbound, primary_inbound_id=primary_inbound_id, host_name=host_name)
                existing_client = _mainpanel_find_matching_raw_client(
                    raw_with_clients,
                    email=target_email,
                    client_uuid=client_uuid,
                    sub_id=sub_id,
                )
                unlimited_whitelist = _key_uses_unlimited_whitelist(
                    key,
                    paid_expiry_ms=int(expiry_ms),
                )
                total_gb_bytes = _mainpanel_inbound_total_gb_bytes(
                    classification,
                    unlimited_whitelist=unlimited_whitelist,
                    subscription_tier=tier,
                    is_trial_super=base_email.lower().endswith("-trial-super@telegram.bot"),
                )
                client_target_expiry_ms = int(expiry_ms)
                reset_client_traffic = False
                traffic_reset_days: int | None = None
                if (
                    classification == MAINPANEL_INBOUND_CLASS_WHITELIST
                    and unlimited_whitelist
                ):
                    traffic_reset_days = 0
                elif classification == MAINPANEL_INBOUND_CLASS_WHITELIST:
                    cycle_state = _raw_whitelist_client_cycle_state(
                        raw_with_clients,
                        email=target_email,
                        client_uuid=client_uuid,
                    )
                    if cycle_state.get("collision"):
                        _record(
                            _mainpanel_item(
                                key=key,
                                inbound_record=inbound_record,
                                tier=tier,
                                status="needs_review",
                                action="repair_client" if existing_client else "upsert_client",
                                error="whitelist_identity_collision",
                            ),
                            counter="needs_review",
                        )
                        continue
                    cycle_anchor_ms = _datetime_value_ms(
                        key.get("whitelist_cycle_anchor_at") or key.get("created_date")
                    ) or int(time.time() * 1000)
                    client_target_expiry_ms, cycle_overdue = _next_whitelist_reset_deadline_ms(
                        cycle_anchor_ms,
                        cycle_state.get("current_reset_ms"),
                        expiry_ms,
                        int(time.time() * 1000),
                        current_reset_days=cycle_state.get("reset_days"),
                    )
                    reset_client_traffic = cycle_overdue
                    traffic_reset_days = (
                        0
                        if client_target_expiry_ms >= int(expiry_ms)
                        else MULTI_NODE_LIMIT_RESET_DAYS
                    )
                repair_reason = _mainpanel_existing_client_repair_reason(
                    existing_client,
                    inbound=inbound,
                    expected_expiry_ms=client_target_expiry_ms,
                    expected_total_gb_bytes=total_gb_bytes,
                    expected_reset_days=traffic_reset_days,
                    require_exact_expiry=(
                        classification == MAINPANEL_INBOUND_CLASS_WHITELIST
                    ),
                    require_enabled=(
                        classification != MAINPANEL_INBOUND_CLASS_WHITELIST
                        or reset_client_traffic
                        or unlimited_whitelist
                    ),
                )
                if existing_client and not repair_reason:
                    if not dry_run and inbound_record.get("is_new"):
                        key_updated = True
                    _record(
                        _mainpanel_item(
                            key=key,
                            inbound_record=inbound_record,
                            tier=tier,
                            status="skipped",
                            action="already_synced",
                        ),
                        counter="skipped",
                    )
                    continue

                item_status = "would_update" if dry_run else "updated"
                item_action = "repair_client" if existing_client else "upsert_client"
                if dry_run:
                    _record(
                        _mainpanel_item(
                            key=key,
                            inbound_record=inbound_record,
                            tier=tier,
                            status=item_status,
                            action=item_action,
                            error=repair_reason or "",
                        ),
                        counter="updated",
                    )
                    continue

                target_sub_id = (
                    sub_id
                    if inbound_id == primary_inbound_id
                    else str(_extract_client_sub_id(existing_client) or "").strip() or _generate_sub_id()
                )
                result_uuid, result_expiry_ms = update_or_create_client_on_panel(
                    api=api,
                    inbound_id=inbound_id,
                    email=target_email,
                    days_to_add=0,
                    target_expiry_ms=client_target_expiry_ms,
                    target_sub_id=target_sub_id,
                    target_client_uuid=client_uuid,
                    total_gb_bytes=total_gb_bytes,
                    traffic_reset_days=traffic_reset_days,
                    source_client=source_client,
                    reset_client_traffic=reset_client_traffic,
                    preserve_existing_enable=(
                        classification == MAINPANEL_INBOUND_CLASS_WHITELIST
                        and not reset_client_traffic
                        and not unlimited_whitelist
                    ),
                )
                if result_uuid != client_uuid or int(result_expiry_ms or 0) != client_target_expiry_ms:
                    _record(
                        _mainpanel_item(
                            key=key,
                            inbound_record=inbound_record,
                            tier=tier,
                            status="failed",
                            action=item_action,
                            error="panel_upsert_failed",
                        ),
                        counter="failed",
                    )
                    continue

                key_updated = True
                _record(
                    _mainpanel_item(
                        key=key,
                        inbound_record=inbound_record,
                        tier=tier,
                        status=item_status,
                        action=item_action,
                        error=repair_reason or "",
                    ),
                    counter="updated",
                )

            if key_updated:
                clear_key_subscription_payload(int(key.get("key_id") or 0))
            update_mainpanel_inbound_sync_run(run_id, **counters)

        status = "completed" if counters["failed"] == 0 else "completed_with_errors"
        report.update(counters)
        report["status"] = status
        report["would_update"] = counters["updated"] if dry_run else 0
        update_mainpanel_inbound_sync_run(run_id, status=status, completed=True, **counters)
        return _mainpanel_report_from_run(run_id, report)
    except Exception as exc:
        counters["failed"] += 1
        report.update(counters)
        report["status"] = "failed"
        report["error"] = str(exc)
        update_mainpanel_inbound_sync_run(
            run_id,
            status="failed",
            error=str(exc),
            completed=True,
            **counters,
        )
        logger.exception("MainPanel inbound sync failed for host '%s'", normalized_host)
        return _mainpanel_report_from_run(run_id, report)


def _build_subscription_link(base_url: str, sub_id: str) -> str:
    return f"{base_url.rstrip('/')}/{str(sub_id).strip('/')}"


def _is_forbidden_subscription_base_url(base_url: str) -> bool:
    parsed = urlparse(str(base_url or "").strip())
    hostname = str(parsed.hostname or "").lower()
    normalized_path = parsed.path.rstrip("/")
    return (
        hostname in {"pepelbot.ru", "www.pepelbot.ru", "pepelbot.com", "www.pepelbot.com"}
        or normalized_path.endswith("/podpiska")
        or "/podpiska/" in normalized_path
    )


def _resolve_subscription_base_url(host_data: Mapping[str, Any] | None) -> str:
    base_url = str((host_data or {}).get("host_subscription_base_url") or "").strip()
    if _is_forbidden_subscription_base_url(base_url):
        logger.warning(
            "Ignoring forbidden subscription base URL for host '%s': %s",
            (host_data or {}).get("host_name") or "unknown",
            base_url,
        )
        return ""
    return base_url


def _is_subscription_link_candidate(value: str) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False
    lower = normalized.lower()
    return lower.startswith(("vless://", "vmess://", "trojan://", "hy2://", "hysteria2://", "http://", "https://"))


def _decode_base64_subscription_payload(value: str) -> str | None:
    compact = "".join(str(value or "").split())
    if "://" in compact or len(compact) < 16:
        return None

    padded = compact + ("=" * (-len(compact) % 4))
    decoders = (
        lambda payload: base64.b64decode(payload, validate=True),
        base64.urlsafe_b64decode,
    )
    for decoder in decoders:
        try:
            decoded = decoder(padded).decode("utf-8", errors="strict").strip()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded and decoded != value and "://" in decoded:
            return decoded
    return None


def _normalize_subscription_links(raw_payload: Any) -> list[str]:
    queue: list[Any] = [raw_payload]
    visited_collections: set[int] = set()
    collected: list[str] = []

    while queue:
        current = queue.pop(0)
        if current is None:
            continue

        if isinstance(current, str):
            text = str(current).strip()
            if not text:
                continue
            decoded_payload = _decode_base64_subscription_payload(text)
            if decoded_payload:
                queue.append(decoded_payload)
                continue
            if "\n" in text:
                for line in text.splitlines():
                    queue.append(line)
                continue
            if "," in text and "://" in text:
                for chunk in text.split(","):
                    queue.append(chunk)
                continue
            if _is_subscription_link_candidate(text):
                collected.append(text)
            continue

        if isinstance(current, dict):
            marker = id(current)
            if marker in visited_collections:
                continue
            visited_collections.add(marker)
            preferred_keys = (
                "obj",
                "links",
                "sub_links",
                "subLinks",
                "subscriptions",
                "subscription_links",
                "subscriptionLinks",
                "url",
                "uri",
            )
            for key in preferred_keys:
                if key in current:
                    queue.append(current.get(key))
            for value in current.values():
                if isinstance(value, (str, list, tuple, set, dict)):
                    queue.append(value)
            continue

        if isinstance(current, (list, tuple, set)):
            marker = id(current)
            if marker in visited_collections:
                continue
            visited_collections.add(marker)
            queue.extend(list(current))
            continue

    deduped: list[str] = []
    seen: set[str] = set()
    for link in collected:
        normalized = link.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def normalize_subscription_payload(raw_payload: Any) -> str | None:
    links = _normalize_subscription_links(raw_payload)
    if not links:
        return None
    return "\n".join(_patch_known_subscription_link_defaults(link) for link in links)


def _patch_known_subscription_link_defaults(link: str) -> str:
    parsed = urlparse(str(link or "").strip())
    if parsed.scheme.lower() != "vless" or not parsed.hostname:
        return link

    fragment = unquote(str(parsed.fragment or "")).strip().lower()
    if not _looks_like_white_bypass_label(fragment):
        return link

    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if str(query.get("type") or "").strip().lower() != "ws":
        return link
    if query.get("path"):
        return link
    if str(parsed.hostname or "").strip().lower() != "s83313.cdn.ngenix.net":
        return link

    query["path"] = "/api/user"
    order = ("encryption", "type", "path", "host", "security", "sni", "fp", "alpn", "flow")
    items = [(key, query[key]) for key in order if key in query]
    used = {key for key, _ in items}
    items.extend((key, value) for key, value in query.items() if key not in used)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(items), parsed.fragment))


def managed_subscription_links_from_direct_payload(
    raw_payload: Any,
    *,
    subscription_tier: Any,
    expected_labels: Iterable[str] | None = None,
) -> list[str]:
    tier = normalize_subscription_tier(subscription_tier)
    if not tier:
        return []

    expected = tuple(
        str(label or "").strip() for label in (expected_labels or ()) if str(label or "").strip()
    )
    if not expected:
        return []

    def _label_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    selected_by_label: dict[str, str] = {}
    for link in _normalize_subscription_links(raw_payload):
        parsed = urlparse(link)
        if parsed.scheme.lower() not in {"vless", "hysteria2", "hy2"}:
            continue

        fragment = unquote(str(parsed.fragment or "")).strip()
        fragment_key = _label_key(fragment)
        fragment_is_white = _looks_like_white_bypass_label(fragment)
        label = None
        for candidate in expected:
            candidate_is_white = _looks_like_white_bypass_label(candidate)
            if fragment_is_white and not candidate_is_white:
                continue
            candidate_key = _label_key(candidate)
            if candidate_key and candidate_key in fragment_key:
                label = candidate
                break
        if label is None and fragment_is_white:
            white_candidates = [candidate for candidate in expected if _looks_like_white_bypass_label(candidate)]
            if len(white_candidates) == 1:
                label = white_candidates[0]

        if label not in expected or label in selected_by_label:
            continue
        selected_by_label[label] = _replace_subscription_remark(link, label)

    if any(label not in selected_by_label for label in expected):
        return []
    return [selected_by_label[label] for label in expected]


def mainpanel_expected_subscription_labels(host_name: str, subscription_tier: Any) -> list[str]:
    tier = normalize_subscription_tier(subscription_tier)
    normalized_host = str(host_name or "").strip()
    if not tier or not normalized_host:
        return []

    def _label_from_record(record: Mapping[str, Any]) -> str:
        label = str(record.get("label") or record.get("remark") or record.get("tag") or "").strip()
        if label:
            return label
        inbound_id = _coerce_panel_int(record.get("inbound_id") or record.get("id"))
        return f"inbound-{inbound_id}" if inbound_id > 0 else ""

    def _labels_from_records(records: Iterable[Mapping[str, Any]]) -> list[str]:
        labels: list[str] = []
        seen: set[str] = set()
        retired_ids = (subscription_policy.retired_inbound_ids(normalized_host)
                       | subscription_policy.migration_excluded_inbound_ids(normalized_host))
        for record in sorted(records, key=lambda item: _coerce_panel_int(item.get("inbound_id") or item.get("id"))):
            if _coerce_panel_int(record.get("inbound_id") or record.get("id")) in retired_ids:
                continue
            if not _coerce_panel_bool(record.get("enable"), default=True):
                continue
            classification = str(record.get("classification") or "")
            if not _mainpanel_tier_can_receive_inbound(tier, classification):
                continue
            label = _label_from_record(record)
            if not label or label in seen:
                continue
            seen.add(label)
            labels.append(label)
        return labels

    snapshot = get_mainpanel_inbound_snapshot(normalized_host)
    labels = _labels_from_records(snapshot.values())
    if labels:
        return labels

    scan = scan_mainpanel_subscription_inbounds(normalized_host)
    return _labels_from_records(scan.get("inbounds") or [])


def subscription_payload_from_result(
    result: Mapping[str, Any] | None,
    *,
    subscription_tier: Any = None,
) -> str | None:
    if not result:
        return None
    del subscription_tier
    payload = normalize_subscription_payload(result.get("subscription_links"))
    if payload:
        return payload
    payload = normalize_subscription_payload(result.get("connection_string"))
    if not payload:
        return None
    patched_payload = normalize_subscription_payload(patch_subscription_links_for_inbounds(payload.splitlines(), []))
    return patched_payload


def _get_subscription_links(api: Api, sub_id: str) -> list[str]:
    normalized_sub_id = str(sub_id or "").strip()
    if not normalized_sub_id:
        return []

    endpoint = f"panel/api/inbounds/getSubLinks/{normalized_sub_id}"
    try:
        response = api.inbound._get(
            api.inbound._url(endpoint),
            {"Accept": "application/json"},
            skip_check=True,
        )
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        response = api.inbound._get(
            api.inbound._url(endpoint),
            {"Accept": "application/json"},
        )
    try:
        payload = response.json()
    except ValueError:
        raw_text = str(getattr(response, "text", "") or "").strip()
        if not raw_text:
            content = getattr(response, "content", b"")
            if isinstance(content, bytes):
                raw_text = content.decode("utf-8", errors="replace").strip()
        return _normalize_subscription_links(raw_text)
    links = payload.get("obj") if isinstance(payload, dict) else payload
    return _normalize_subscription_links(links)


def _get_subscription_links_with_retry(
    api: Api,
    sub_id: str,
    *,
    expected_count: int = 1,
    attempts: int | None = None,
    delay_seconds: float | None = None,
) -> list[str]:
    safe_expected = max(1, int(expected_count or 1))
    retries = max(1, int(attempts or SUBSCRIPTION_LINKS_MIN_ATTEMPTS))
    retry_delay = max(0.0, float(delay_seconds or SUBSCRIPTION_LINKS_RETRY_DELAY_SECONDS))

    best_links: list[str] = []
    for attempt in range(1, retries + 1):
        links = _get_subscription_links(api, sub_id)
        if len(links) >= len(best_links):
            best_links = links
        if len(links) >= safe_expected:
            return links
        if attempt < retries:
            logger.warning(
                "Subscription links for subId '%s' are incomplete (%s/%s, attempt %s/%s). Retrying...",
                sub_id,
                len(links),
                safe_expected,
                attempt,
                retries,
            )
            time.sleep(retry_delay)
    return best_links


def _build_subscription_links_from_panel_clients(
    api: Api,
    host_data: Mapping[str, Any],
    sub_id: str,
    inbounds: list[Inbound],
    raw_inbounds: list[dict[str, Any]] | None = None,
    node_address_by_id: Mapping[int, str] | None = None,
    node_address_by_name: Mapping[str, str] | None = None,
    remark_mode: str = "friendly",
    client_uuid: str | None = None,
    base_email: str | None = None,
) -> list[str]:
    normalized_sub_id = str(sub_id or "").strip()
    normalized_uuid = _normalized_client_uuid(client_uuid)
    normalized_base_email = _normalized_client_email(base_email)
    if not (normalized_sub_id or normalized_uuid or normalized_base_email) or not inbounds:
        return []

    resolved_raw_inbounds = raw_inbounds if raw_inbounds is not None else _list_inbounds_raw(api)
    if not resolved_raw_inbounds:
        return []

    raw_inbound_by_id = {
        _coerce_panel_int(raw_inbound.get("id")): raw_inbound
        for raw_inbound in resolved_raw_inbounds
        if _coerce_panel_int(raw_inbound.get("id")) > 0
    }

    def _raw_inbound_with_clients(inbound_id: int, raw_inbound: dict[str, Any] | None) -> dict[str, Any] | None:
        try:
            detailed_raw = _get_inbound_raw_by_id(api, inbound_id, allow_list_fallback=False)
        except (
            RequestException,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ) as exc:
            logger.warning(
                "Failed to load detailed inbound for managed subscription fallback: inbound_id=%s error=%s",
                inbound_id,
                exc,
            )
            return raw_inbound
        if isinstance(detailed_raw, dict) and detailed_raw.get("settings") is not None:
            return detailed_raw
        return raw_inbound

    def _raw_client_match_score(
        raw_client: Mapping[str, Any],
        *,
        expected_email: str,
        allow_disabled: bool = False,
    ) -> int:
        if not allow_disabled and not _coerce_panel_bool(raw_client.get("enable"), default=True):
            return -1
        raw_uuid = _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid"))
        if not raw_uuid:
            return -1
        raw_email = _normalized_client_email(raw_client.get("email"))
        raw_sub_id = str(_extract_client_sub_id(raw_client) or "").strip()
        normalized_expected_email = _normalized_client_email(expected_email)
        if normalized_expected_email and raw_email == normalized_expected_email:
            return 100
        if normalized_sub_id and raw_sub_id == normalized_sub_id:
            return 80
        if normalized_uuid and raw_uuid == normalized_uuid:
            return 60
        return -1

    links: list[str] = []
    link_inbounds: list[Inbound] = []
    for inbound_index, inbound in enumerate(inbounds):
        inbound_id = _safe_inbound_id(inbound)
        raw_inbound = _raw_inbound_with_clients(inbound_id, raw_inbound_by_id.get(inbound_id))
        if not raw_inbound:
            continue
        runtime_inbound = inbound
        if raw_inbound.get("streamSettings") is not None or raw_inbound.get("stream_settings") is not None:
            runtime_inbound = _raw_inbound_runtime_namespace(raw_inbound, inbound_id=inbound_id)
        try:
            raw_clients = list(_iter_client_dicts_from_settings(raw_inbound.get("settings")))
        except (TypeError, ValueError):
            logger.warning(
                "Failed to parse inbound clients for managed subscription fallback: inbound_id=%s", inbound_id
            )
            continue
        expected_email = ""
        if normalized_base_email:
            base_email_value = str(base_email or "").strip()
            base_email_is_whitelist = _looks_like_white_bypass_label(base_email_value)
            inbound_is_whitelist = _looks_like_white_bypass_label(_inbound_label(runtime_inbound))
            expected_email = (
                base_email_value
                if (base_email_is_whitelist and inbound_is_whitelist)
                or (not base_email_is_whitelist and inbound_index == 0)
                else _secondary_inbound_email(base_email_value, runtime_inbound, host_name=str(host_data.get("host_name") or ""))
            )
        best_client: Mapping[str, Any] | None = None
        best_score = -1
        allow_disabled_client = _looks_like_white_bypass_label(_inbound_label(runtime_inbound))
        for raw_client in raw_clients:
            score = _raw_client_match_score(
                raw_client,
                expected_email=expected_email,
                allow_disabled=allow_disabled_client,
            )
            if score > best_score:
                best_score = score
                best_client = raw_client
            if score >= 100:
                break
        if not best_client:
            continue
        if best_score < 100 and expected_email:
            logger.warning(
                "Managed subscription fallback matched inbound_id=%s by subId/uuid instead of expected email '%s'. "
                "Matched email='%s'.",
                inbound_id,
                expected_email,
                str(best_client.get("email") or ""),
            )
        client_uuid = str(best_client.get("id") or best_client.get("uuid") or "").strip()
        link = get_connection_string(
            runtime_inbound,
            client_uuid,
            str(host_data.get("host_url") or ""),
            remark=_inbound_label(runtime_inbound),
            node_address_by_id=node_address_by_id,
            node_address_by_name=node_address_by_name,
            client=best_client,
        )
        if link:
            links.append(link)
            link_inbounds.append(runtime_inbound)
    return patch_subscription_links_for_inbounds(
        links,
        link_inbounds,
        node_address_by_id=node_address_by_id,
        node_address_by_name=node_address_by_name,
        remark_mode=remark_mode,
    )


class ManagedSubscriptionError(RuntimeError):
    """Raised when managed subscription data cannot be retrieved safely."""


def get_managed_subscription_links(
    host_data: dict,
    sub_id: str,
    *,
    include_white_inbounds: bool | None = None,
    subscription_tier: Any = None,
    remark_mode: str = "friendly",
    client_uuid: str | None = None,
    base_email: str | None = None,
) -> list[str]:
    normalized_sub_id = str(sub_id or "").strip()
    if not normalized_sub_id:
        return []

    api, inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=_resolve_host_api_token(host_data),
        prefer_raw_inbound_lookup=True,
    )
    if not api or not inbound:
        raise ManagedSubscriptionError("managed_subscription_login_failed")

    try:
        del subscription_tier
        node_address_by_id, node_address_by_name = _node_public_address_maps(api)

        def _finalize_links(links: list[str]) -> list[str]:
            return links

        white_node_inbound_ids = _white_node_inbound_ids_for_panel(
            str(host_data.get("host_name") or ""),
        )
        include_white = (
            should_include_white_subscription_inbounds(host_data)
            if include_white_inbounds is None
            else bool(include_white_inbounds)
        )
        raw_inbounds = _list_inbounds_raw(api)
        inbounds = _subscription_inbounds(
            api,
            inbound,
            include_white_inbounds=include_white,
            white_node_inbound_ids=white_node_inbound_ids,
            raw_inbounds=raw_inbounds or None,
            host_name=str(host_data.get("host_name") or ""),
            require_migration_targets=include_white,
        )
        retired_ids = subscription_policy.retired_inbound_ids(str(host_data.get("host_name") or ""))
        inbounds = [item for item in inbounds if _safe_inbound_id(item) not in retired_ids]
        try:
            raw_links = _get_subscription_links(api, normalized_sub_id)
        except (RequestException, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
            raw_links = []
        filtered_links = _filter_subscription_links_for_inbounds(raw_links, inbounds)
        patched_links = patch_subscription_links_for_inbounds(
            filtered_links,
            inbounds,
            node_address_by_id=node_address_by_id,
            node_address_by_name=node_address_by_name,
            remark_mode=remark_mode,
        )
        if len(patched_links) >= len(inbounds):
            return _finalize_links(patched_links)

        fallback_links = _build_subscription_links_from_panel_clients(
            api,
            host_data,
            normalized_sub_id,
            inbounds,
            raw_inbounds=raw_inbounds,
            node_address_by_id=node_address_by_id,
            node_address_by_name=node_address_by_name,
            remark_mode=remark_mode,
            client_uuid=client_uuid,
            base_email=base_email,
        )
        if fallback_links:
            return _finalize_links(fallback_links)

        raw_links = _get_subscription_links_with_retry(
            api,
            normalized_sub_id,
            expected_count=len(inbounds),
        )
        filtered_links = _filter_subscription_links_for_inbounds(raw_links, inbounds)
        return _finalize_links(
            patch_subscription_links_for_inbounds(
                filtered_links,
                inbounds,
                node_address_by_id=node_address_by_id,
                node_address_by_name=node_address_by_name,
                remark_mode=remark_mode,
            )
        )
    except (
        RequestException,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ) as exc:
        raise ManagedSubscriptionError("managed_subscription_links_unavailable") from exc


def _api_extract_obj(payload: Any) -> Any:
    if isinstance(payload, dict) and "obj" in payload:
        return payload.get("obj")
    return payload


def _api_get_json(api: Api, endpoint: str, *, skip_check: bool = False, timeout: float | None = None) -> Any:
    kwargs = {"skip_check": True} if skip_check else {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    try:
        response = api.inbound._get(
            api.inbound._url(endpoint),
            {"Accept": "application/json"},
            **kwargs,
        )
    except TypeError as exc:
        if not skip_check or "unexpected keyword" not in str(exc):
            raise
        response = api.inbound._get(
            api.inbound._url(endpoint),
            {"Accept": "application/json"},
            **({"timeout": timeout} if timeout is not None else {}),
        )
    return response.json()


def _api_post_json(api: Api, endpoint: str, data: dict[str, Any]) -> Any:
    response = api.inbound._post(
        api.inbound._url(endpoint),
        {"Accept": "application/json"},
        data,
    )
    return response.json()


def _api_post_json_once_raw(api: Api, endpoint: str, data: dict[str, Any]) -> Any:
    """POST once without py3xui retry or success-envelope validation.

    The atomic traffic-cycle repair endpoint uses compare-and-swap semantics.
    Retrying a transport-ambiguous request could reset counters twice, while
    treating an HTTP 200 ``success:false`` envelope as an exception would hide
    the endpoint's deterministic rejection reason.  Remote metadata-only
    client updates also use this transport so a write is never replayed.
    """

    inbound_api = api.inbound
    api_token = _normalize_api_token(getattr(api, "_pepel_api_token", None))
    if not api_token and not inbound_api.session:
        raise ValueError("atomic_repair_authentication_missing")

    verify: bool | str
    if not inbound_api.use_tls_verify:
        verify = False
    elif inbound_api.custom_certificate_path:
        verify = inbound_api.custom_certificate_path
    else:
        verify = True

    response = requests.post(
        inbound_api._url(endpoint),
        allow_redirects=False,
        cookies=inbound_api.cookies,
        headers=_with_bearer_header({"Accept": "application/json"}, api_token),
        json=data,
        timeout=XUI_API_TIMEOUT,
        verify=verify,
    )
    response.raise_for_status()
    return response.json()


def _client_to_api_payload(client: Any) -> dict[str, Any]:
    if isinstance(client, Mapping):
        payload = dict(client)
    elif hasattr(client, "model_dump"):
        payload = client.model_dump(by_alias=True, exclude_none=True)
    elif hasattr(client, "dict"):
        payload = client.dict(by_alias=True, exclude_none=True)
    else:
        payload = {}

    if not isinstance(payload, dict):
        payload = {}

    aliases = {
        "expiry_time": "expiryTime",
        "total_gb": "totalGB",
        "sub_id": "subId",
        "limit_ip": "limitIp",
        "tg_id": "tgId",
        "inbound_id": "inboundId",
        "created_at": "created_at",
        "updated_at": "updated_at",
    }
    for source, alias in aliases.items():
        value = getattr(client, source, None)
        if value is not None and alias not in payload:
            payload[alias] = value

    for field in (
        "id",
        "email",
        "enable",
        "password",
        "flow",
        "method",
        "comment",
        "reset",
        "security",
        "auth",
        "group",
    ):
        value = getattr(client, field, None)
        if value is not None and field not in payload:
            payload[field] = value

    for int_field in (
        "expiryTime",
        "totalGB",
        "limitIp",
        "tgId",
        "inboundId",
        "reset",
        "created_at",
        "updated_at",
    ):
        if int_field in payload:
            try:
                payload[int_field] = int(payload[int_field] or 0)
            except (TypeError, ValueError):
                payload[int_field] = 0

    return {key: value for key, value in payload.items() if value is not None}


def _api_post_client_add(api: Api, inbound_id: int, client: Any) -> Any:
    payload = {
        "client": _client_to_api_payload(client),
        "inboundIds": [int(inbound_id)],
    }
    return _api_post_json(api, "panel/api/clients/add", payload)


def _api_post_client_update(api: Api, current_email: str, inbound_id: int, client: Any) -> Any:
    encoded_email = quote(str(current_email or "").strip(), safe="")
    endpoint = f"panel/api/clients/update/{encoded_email}?{urlencode({'inboundIds': str(int(inbound_id))})}"
    changed = _client_to_api_payload(client)
    expected_uuid = _normalized_client_uuid(changed.get("uuid") or changed.get("id"))
    # The centralized API owns additional fields not represented by py3xui's
    # legacy Client model.  Keep its exact row, applying client config changes
    # only, and send the protocol UUID in id (the list endpoint's id is numeric).
    listed = _api_extract_obj(_api_get_json(api, "panel/api/clients/list", skip_check=True))
    if isinstance(listed, Mapping):
        listed = listed.get("clients")
    if not isinstance(listed, list):
        raise TypeError("central_client_snapshot_unavailable")
    matches = [
        dict(row) for row in listed
        if isinstance(row, Mapping)
        and _normalized_client_email(row.get("email")) == _normalized_client_email(current_email)
    ]
    if len(matches) != 1:
        raise ValueError("central_client_identity_not_unique")
    preserved = matches[0]
    central_uuid = _normalized_client_uuid(preserved.get("uuid") or preserved.get("id"))
    bindings = {_coerce_panel_int(value) for value in (preserved.get("inboundIds") or [])}
    if not expected_uuid or central_uuid != expected_uuid or int(inbound_id) not in bindings:
        raise ValueError("central_client_identity_mismatch")
    for field in (
        "email", "enable", "expiryTime", "totalGB", "reset", "subId", "flow",
        "password", "limitIp", "tgId", "comment", "method", "auth", "security",
    ):
        if field in changed:
            preserved[field] = changed[field]
    preserved["id"] = central_uuid
    preserved.pop("traffic", None)
    return _api_post_json(api, endpoint, _client_to_api_payload(preserved))


def _api_post_inbound_client_update(
    api: Api,
    *,
    inbound_id: int,
    expected_email: str,
    expected_client_uuid: str,
    client: Mapping[str, Any],
) -> Any:
    """Update one exact client through 3x-ui's inbound-scoped endpoint."""
    safe_inbound_id = _coerce_panel_int(inbound_id)
    normalized_email = _normalized_client_email(expected_email)
    normalized_uuid = _normalized_client_uuid(expected_client_uuid)
    client_payload = _compact_raw_client_mapping(client)
    payload_email = _normalized_client_email(client_payload.get("email"))
    payload_uuid = _normalized_client_uuid(
        client_payload.get("id") or client_payload.get("uuid")
    )
    if (
        safe_inbound_id <= 0
        or not normalized_email
        or not normalized_uuid
        or payload_email != normalized_email
        or payload_uuid != normalized_uuid
    ):
        raise ValueError("inbound_client_identity_mismatch")

    endpoint = f"panel/api/inbounds/updateClient/{quote(normalized_uuid, safe='')}"
    return _api_post_json(
        api,
        endpoint,
        {
            "id": safe_inbound_id,
            "settings": json.dumps(
                {"clients": [client_payload]},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    )


def _api_post_client_detach(api: Api, current_email: str, inbound_id: int) -> Any:
    encoded_email = quote(str(current_email or "").strip(), safe="")
    return _api_post_json(api, f"panel/api/clients/{encoded_email}/detach", {"inboundIds": [int(inbound_id)]})


def _api_post_client_delete(api: Api, current_email: str, *, keep_traffic: bool = False) -> Any:
    encoded_email = quote(str(current_email or "").strip(), safe="")
    keep_traffic_value = 1 if keep_traffic else 0
    return _api_post_json(
        api,
        f"panel/api/clients/del/{encoded_email}?keepTraffic={keep_traffic_value}",
        {},
    )


def _api_payload_success(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return True
    if "success" not in payload:
        return True
    return bool(payload.get("success"))


def _is_duplicate_email_error(error: BaseException) -> bool:
    normalized = str(error or "").strip().lower()
    return "duplicate email" in normalized or "email already in use" in normalized


def _detach_client_from_inbound_by_email(api: Api, inbound_id: int, email: str) -> bool:
    normalized_email = str(email or "").strip()
    if not normalized_email:
        return False
    try:
        payload = _api_post_client_detach(api, normalized_email, int(inbound_id))
    except (
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        logger.exception(
            "clients.detach failed for inbound_id=%s email=%s.",
            inbound_id,
            normalized_email,
        )
        return False
    if not _api_payload_success(payload):
        logger.warning(
            "clients.detach rejected for inbound_id=%s email=%s payload=%s.",
            inbound_id,
            normalized_email,
            payload,
        )
        return False
    return True


def _delete_detached_client_by_email(api: Api, email: str) -> bool:
    normalized_email = str(email or "").strip()
    if not normalized_email:
        return False
    try:
        payload = _api_post_client_delete(api, normalized_email)
    except (
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ) as exc:
        if _response_status_code(exc) == 404:
            return True
        logger.exception("clients.delete failed for detached email=%s.", normalized_email)
        return False
    if not _api_payload_success(payload):
        logger.warning("clients.delete rejected for detached email=%s payload=%s.", normalized_email, payload)
        return False
    return True


def _normalize_node_base_path(raw_path: str | None) -> str:
    path = str(raw_path or "").strip()
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    if not path.endswith("/"):
        path = path + "/"
    return path


def _host_to_node_payload(host_name: str, host_data: dict, *, api_token: str) -> dict[str, Any]:
    host_url = str(host_data.get("host_url") or "").strip()
    parsed = urlparse(host_url)
    scheme = str(parsed.scheme or "https").strip().lower() or "https"
    address = str(parsed.hostname or "").strip()
    if not address:
        raise ValueError(  # noqa: TRY003
            f"Host '{host_name}' has invalid URL for node registration: {host_url}"
        )
    port = int(parsed.port or (443 if scheme == "https" else 80))

    return {
        "name": str(host_name).strip(),
        "remark": "",
        "scheme": scheme,
        "address": address,
        "port": port,
        "basePath": _normalize_node_base_path(parsed.path),
        "apiToken": api_token,
        "enable": True,
    }


def _find_existing_node(nodes: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any] | None:
    target_name = str(payload.get("name") or "").strip().lower()
    target_address = str(payload.get("address") or "").strip().lower()
    target_port = int(payload.get("port") or 0)
    target_base_path = _normalize_node_base_path(payload.get("basePath"))
    for node in nodes:
        node_name = str(node.get("name") or "").strip().lower()
        node_address = str(node.get("address") or "").strip().lower()
        node_port = int(node.get("port") or 0)
        node_base_path = _normalize_node_base_path(node.get("basePath"))
        if target_name and node_name == target_name:
            return node
        if node_address == target_address and node_port == target_port and node_base_path == target_base_path:
            return node
    return None


def _list_nodes_raw(api: Api) -> list[dict[str, Any]]:
    payload = _api_get_json(api, "panel/api/nodes/list")
    nodes = _api_extract_obj(payload)
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def _resolve_or_create_node(api: Api, *, host_name: str, host_data: dict, node_api_token: str) -> int:
    node_payload = _host_to_node_payload(host_name, host_data, api_token=node_api_token)
    existing_node = _find_existing_node(_list_nodes_raw(api), node_payload)
    if existing_node:
        node_id = int(existing_node.get("id") or 0)
        if node_id <= 0:
            raise RuntimeError(  # noqa: TRY003
                f"Node for host '{host_name}' has invalid ID in panel response."
            )
        _api_post_json(api, f"panel/api/nodes/update/{node_id}", node_payload)
        return node_id

    created_payload = _api_post_json(api, "panel/api/nodes/add", node_payload)
    created_obj = _api_extract_obj(created_payload)
    if isinstance(created_obj, dict) and int(created_obj.get("id") or 0) > 0:
        return int(created_obj["id"])

    existing_after_create = _find_existing_node(_list_nodes_raw(api), node_payload)
    if existing_after_create and int(existing_after_create.get("id") or 0) > 0:
        return int(existing_after_create["id"])

    raise RuntimeError(f"Failed to resolve/create node for host '{host_name}'.")  # noqa: TRY003


def _probe_node(api: Api, node_id: int) -> bool:
    try:
        payload = _api_post_json(api, f"panel/api/nodes/probe/{int(node_id)}", {})
    except (
        AttributeError,
        KeyError,
        RequestException,
        RuntimeError,
        TypeError,
        ValueError,
    ):
        return False

    if isinstance(payload, dict) and payload.get("success") is False:
        return False
    obj = _api_extract_obj(payload)
    if isinstance(obj, dict):
        status = str(obj.get("status") or "").strip().lower()
        if status:
            return status == "online"
        if "ok" in obj:
            return bool(obj.get("ok"))
    return True


def _extract_inbound_list(payload: Any) -> list[dict[str, Any]]:
    inbounds = _api_extract_obj(payload)
    if isinstance(inbounds, dict):
        for key in ("inbounds", "rows", "items", "list", "data"):
            nested = inbounds.get(key)
            if isinstance(nested, list):
                inbounds = nested
                break
    if not isinstance(inbounds, list):
        return []
    return [inbound for inbound in inbounds if isinstance(inbound, dict)]


def _list_inbounds_raw(api: Api) -> list[dict[str, Any]]:
    endpoints = (
        ("panel/api/inbounds/options", True),
        ("panel/api/inbounds/list", False),
        ("panel/api/inbounds/list/slim", False),
    )
    for endpoint, skip_check in endpoints:
        try:
            inbounds = _extract_inbound_list(_api_get_json(api, endpoint, skip_check=skip_check))
        except (RequestException, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
            inbounds = []
        if inbounds:
            return inbounds
    return []


def _list_lightweight_inbounds_raw(api: Api) -> list[dict[str, Any]]:
    for endpoint in ("panel/api/inbounds/options", "panel/api/inbounds/list/slim"):
        try:
            inbounds = _extract_inbound_list(_api_get_json(api, endpoint, skip_check=True))
        except (RequestException, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
            inbounds = []
        if inbounds:
            return inbounds
    return []


def _is_panel_inbound_not_found_error(error: BaseException) -> bool:
    if isinstance(error, PanelInboundLookupError):
        return error.code == "panel_inbound_not_found"
    return str(error).startswith("panel_inbound_not_found:")


def is_panel_inbound_not_found_error(error: BaseException) -> bool:
    return _is_panel_inbound_not_found_error(error)


def _panel_lookup_failure(inbound_id: int | str, detail: object | None = None) -> PanelInboundLookupError:
    return PanelInboundLookupError("panel_raw_lookup_failed", inbound_id, str(detail or ""))


def _panel_inbound_not_found(inbound_id: int | str, detail: object | None = None) -> PanelInboundLookupError:
    return PanelInboundLookupError("panel_inbound_not_found", inbound_id, str(detail or ""))


def _get_inbound_raw_by_id(
    api: Api,
    inbound_id: int | str,
    *,
    allow_list_fallback: bool = False,
    allow_lightweight_fallback: bool = False,
    request_timeout: float | None = None,
) -> dict[str, Any]:
    normalized_inbound_id = str(inbound_id).strip()
    if not normalized_inbound_id:
        raise ValueError("panel_inbound_id_empty")

    try:
        payload = _api_get_json(
            api,
            f"panel/api/inbounds/get/{normalized_inbound_id}",
            skip_check=True,
            **({"timeout": request_timeout} if request_timeout is not None else {}),
        )
    except (RequestException, RuntimeError, ValueError, TypeError, KeyError) as exc:
        inbound = None
        direct_error: PanelInboundLookupError | None = _panel_lookup_failure(normalized_inbound_id, exc)
    else:
        if isinstance(payload, dict) and payload.get("success") is False:
            inbound = None
            direct_error = _panel_inbound_not_found(
                normalized_inbound_id,
                payload.get("msg") or payload.get("message"),
            )
        else:
            inbound = _api_extract_obj(payload)
            direct_error = None

    if isinstance(inbound, dict):
        inbound_response_id = str(inbound.get("id") or "").strip()
        if inbound_response_id == normalized_inbound_id:
            return inbound

    if allow_lightweight_fallback:
        for raw_inbound in _list_lightweight_inbounds_raw(api):
            if str(raw_inbound.get("id") or "").strip() == normalized_inbound_id:
                return raw_inbound

    if not allow_list_fallback:
        if direct_error is not None:
            raise direct_error
        raise _panel_lookup_failure(normalized_inbound_id, "unexpected_inbound_payload")

    for raw_inbound in _list_inbounds_raw(api):
        if str(raw_inbound.get("id") or "").strip() == normalized_inbound_id:
            return raw_inbound

    if direct_error is not None:
        raise direct_error
    raise _panel_inbound_not_found(normalized_inbound_id)


_JSON_CLIENTS_KEY_RE = re.compile(r'"clients"\s*:')


def _iter_client_dicts_from_settings(settings: Any) -> Iterable[dict[str, Any]]:
    if isinstance(settings, Mapping):
        clients = settings.get("clients")
        if not isinstance(clients, list):
            raise TypeError("panel_clients_missing")
        for client in clients:
            if isinstance(client, dict):
                yield client
        return

    if not isinstance(settings, str) or not settings.strip():
        raise ValueError("panel_inbound_settings_missing")

    match = _JSON_CLIENTS_KEY_RE.search(settings)
    if match is None:
        raise ValueError("panel_clients_missing")

    index = match.end()
    while index < len(settings) and settings[index].isspace():
        index += 1
    if index >= len(settings) or settings[index] != "[":
        raise ValueError("panel_clients_malformed")

    index += 1
    decoder = json.JSONDecoder()
    while True:
        while index < len(settings) and settings[index].isspace():
            index += 1
        if index >= len(settings):
            raise ValueError("panel_clients_unclosed")
        if settings[index] == "]":
            return
        if settings[index] == ",":
            index += 1
            continue

        try:
            client, index = decoder.raw_decode(settings, index)
        except json.JSONDecodeError as exc:
            raise ValueError("panel_clients_malformed") from exc
        if isinstance(client, dict):
            yield client

        while index < len(settings) and settings[index].isspace():
            index += 1
        if index < len(settings) and settings[index] == ",":
            index += 1
            continue
        if index < len(settings) and settings[index] == "]":
            return
        if index >= len(settings):
            raise ValueError("panel_clients_unclosed")
        raise ValueError("panel_clients_malformed")


def _client_summary_from_mapping(client: Mapping[str, Any]) -> PanelClientSummary | None:
    email = str(client.get("email") or "").strip()
    if not email:
        return None

    raw_client_id = client.get("uuid") or client.get("id")
    client_id = str(raw_client_id).strip() if raw_client_id is not None else ""
    raw_expiry_time = client.get("expiryTime", client.get("expiry_time"))
    expiry_time: int | None
    if raw_expiry_time in (None, ""):
        expiry_time = None
    else:
        try:
            expiry_time = int(raw_expiry_time)
        except (TypeError, ValueError):
            expiry_time = None

    return PanelClientSummary(
        id=client_id or None,
        email=email,
        expiry_time=expiry_time,
    )


def _coerce_panel_int(value: Any, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_panel_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_panel_bool(value: Any, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _client_model_from_raw_mapping(client: Mapping[str, Any], *, inbound_id: int) -> Client | None:
    email = str(client.get("email") or "").strip()
    if not email:
        return None
    raw_client_id = client.get("uuid") or client.get("id")
    client_id = str(raw_client_id).strip() if raw_client_id is not None else None
    return Client(
        id=client_id,
        uuid=str(client.get("uuid") or "").strip() or None,
        email=email,
        enable=_coerce_panel_bool(client.get("enable"), default=True),
        password=str(client.get("password") or ""),
        inboundId=inbound_id,
        up=_coerce_panel_int(client.get("up")),
        down=_coerce_panel_int(client.get("down")),
        expiryTime=_coerce_panel_int(client.get("expiryTime", client.get("expiry_time"))),
        total=_coerce_panel_int(client.get("total")),
        reset=_coerce_panel_optional_int(client.get("reset")),
        flow=str(client.get("flow") or ""),
        method=str(client.get("method") or ""),
        limitIp=_coerce_panel_int(client.get("limitIp", client.get("limit_ip"))),
        subId=str(client.get("subId", client.get("sub_id")) or ""),
        comment=str(client.get("comment") or ""),
        tgId=_coerce_panel_int(client.get("tgId", client.get("tg_id"))),
        totalGB=_coerce_panel_int(client.get("totalGB", client.get("total_gb"))),
    )


def _raw_stream_settings_namespace(raw_stream_settings: Any) -> SimpleNamespace:
    mapping = _as_mapping(raw_stream_settings)
    reality_settings = _as_mapping(mapping.get("realitySettings", mapping.get("reality_settings")))
    ws_settings = _as_mapping(mapping.get("wsSettings", mapping.get("ws_settings")))
    xhttp_settings = _as_mapping(mapping.get("xhttpSettings", mapping.get("xhttp_settings")))
    hysteria_settings = _as_mapping(mapping.get("hysteriaSettings", mapping.get("hysteria_settings")))
    tls_settings = _as_mapping(mapping.get("tlsSettings", mapping.get("tls_settings")))
    finalmask = _as_mapping(mapping.get("finalmask", mapping.get("finalMask")))
    external_proxy = mapping.get("externalProxy", mapping.get("external_proxy", []))
    return SimpleNamespace(
        network=mapping.get("network", ""),
        security=mapping.get("security", ""),
        reality_settings=reality_settings,
        realitySettings=reality_settings,
        ws_settings=ws_settings,
        wsSettings=ws_settings,
        xhttp_settings=xhttp_settings,
        xhttpSettings=xhttp_settings,
        hysteria_settings=hysteria_settings,
        hysteriaSettings=hysteria_settings,
        tls_settings=tls_settings,
        tlsSettings=tls_settings,
        finalmask=finalmask,
        finalMask=finalmask,
        external_proxy=external_proxy,
        externalProxy=external_proxy,
    )


def _raw_inbound_for_client_upsert(
    raw_inbound: Mapping[str, Any],
    *,
    inbound_id: int,
    email: str,
    target_client_uuid: str | None,
) -> SimpleNamespace:
    if "settings" not in raw_inbound:
        raise ValueError("panel_inbound_settings_missing")

    target_email = _normalized_client_email(email)
    target_uuid = _normalized_client_uuid(target_client_uuid)
    matched_clients: list[Client] = []
    for raw_client in _iter_client_dicts_from_settings(raw_inbound.get("settings")):
        raw_email = _normalized_client_email(raw_client.get("email"))
        raw_uuid = _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid"))
        if (target_uuid and raw_uuid == target_uuid) or (target_email and raw_email == target_email):
            client = _client_model_from_raw_mapping(raw_client, inbound_id=inbound_id)
            if client is not None:
                matched_clients.append(client)

    return SimpleNamespace(
        id=inbound_id,
        node_id=_coerce_panel_int(raw_inbound.get("nodeId") or raw_inbound.get("node_id")),
        port=_coerce_panel_int(raw_inbound.get("port")),
        protocol=str(raw_inbound.get("protocol") or ""),
        enable=_coerce_panel_bool(raw_inbound.get("enable"), default=True),
        remark=str(raw_inbound.get("remark") or ""),
        tag=str(raw_inbound.get("tag") or raw_inbound.get("remark") or ""),
        settings=SimpleNamespace(clients=matched_clients),
        stream_settings=_raw_stream_settings_namespace(
            raw_inbound.get("streamSettings", raw_inbound.get("stream_settings"))
        ),
    )


def _raw_inbound_runtime_namespace(raw_inbound: Mapping[str, Any], *, inbound_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=inbound_id,
        node_id=_coerce_panel_int(raw_inbound.get("nodeId") or raw_inbound.get("node_id")),
        port=_coerce_panel_int(raw_inbound.get("port")),
        protocol=str(raw_inbound.get("protocol") or ""),
        enable=_coerce_panel_bool(raw_inbound.get("enable"), default=True),
        remark=str(raw_inbound.get("remark") or ""),
        tag=str(raw_inbound.get("tag") or raw_inbound.get("remark") or ""),
        settings=SimpleNamespace(clients=[]),
        stream_settings=_raw_stream_settings_namespace(
            raw_inbound.get("streamSettings", raw_inbound.get("stream_settings"))
        ),
    )


def _minimal_inbound_runtime_namespace(inbound_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=inbound_id,
        node_id=0,
        port=0,
        protocol="",
        enable=True,
        remark="",
        tag="",
        settings=SimpleNamespace(clients=[]),
        stream_settings=SimpleNamespace(
            network="",
            security="",
            reality_settings={},
            realitySettings={},
            ws_settings={},
            wsSettings={},
            xhttp_settings={},
            xhttpSettings={},
            hysteria_settings={},
            hysteriaSettings={},
            tls_settings={},
            tlsSettings={},
            external_proxy=[],
            externalProxy=[],
        ),
    )


def _raise_raw_inbound_lookup_failed():
    raise RuntimeError("raw_inbound_lookup_failed")


def _load_inbound_for_client_upsert(
    api: Api,
    inbound_id: int,
    *,
    email: str,
    target_client_uuid: str | None,
) -> tuple[Any | None, bool]:
    raw_supported = all(hasattr(api.inbound, attr) for attr in ("_get", "_url"))
    try:
        raw_inbound = _get_inbound_raw_by_id(api, inbound_id)
        return (
            _raw_inbound_for_client_upsert(
                raw_inbound,
                inbound_id=inbound_id,
                email=email,
                target_client_uuid=target_client_uuid,
            ),
            False,
        )
    except (
        RequestException,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ) as exc:
        if raw_supported and not PANEL_CLIENT_UPSERT_ALLOW_FULL_INBOUND_FALLBACK:
            logger.warning(
                "Raw client upsert inbound load failed for inbound_id=%s; refusing full inbound fallback: %s",
                inbound_id,
                exc,
            )
            raise
        logger.warning(
            "Raw client upsert inbound load unavailable for inbound_id=%s; using legacy full inbound fallback: %s",
            inbound_id,
            exc,
        )
        return api.inbound.get_by_id(inbound_id), True


def _response_status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    try:
        return int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return None


_CLIENT_RAW_FIELD_ALIASES: dict[str, str] = {
    "expiry_time": "expiryTime",
    "total_gb": "totalGB",
    "sub_id": "subId",
    "limit_ip": "limitIp",
    "tg_id": "tgId",
    "inbound_id": "inboundId",
}

_CLIENT_RAW_RUNTIME_DROP_FIELDS: frozenset[str] = frozenset(
    {
        "created_at",
        "createdAt",
        "updated_at",
        "updatedAt",
        "inbound_id",
        "inboundId",
        "up",
        "down",
        "total",
    }
)

_STREAM_SETTINGS_PRESERVE_KEYS: tuple[str, ...] = (
    "network",
    "security",
    "tcpSettings",
    "kcpSettings",
    "wsSettings",
    "httpSettings",
    "grpcSettings",
    "xhttpSettings",
    "tlsSettings",
    "xtlsSettings",
    "realitySettings",
    "externalProxy",
)


def _json_panel_field(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))
    value_mapping = _as_mapping(value)
    if value_mapping:
        return json.dumps(value_mapping, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _raw_inbound_field_as_json(raw_inbound: Mapping[str, Any], *field_names: str) -> str:
    for field_name in field_names:
        if field_name in raw_inbound:
            return _json_panel_field(raw_inbound.get(field_name))
    return ""


def _raw_settings_mapping(raw_settings: Any) -> dict[str, Any]:
    settings = _as_mapping(raw_settings)
    clients = settings.get("clients")
    if not isinstance(clients, list):
        raise TypeError("panel_inbound_settings_clients_missing")
    return settings


def _client_raw_mapping(client: Any) -> dict[str, Any]:
    raw_client = _as_mapping(client)
    for snake_key, camel_key in _CLIENT_RAW_FIELD_ALIASES.items():
        if snake_key in raw_client:
            raw_client[camel_key] = raw_client.pop(snake_key)
    if not raw_client.get("id") and raw_client.get("uuid"):
        raw_client["id"] = raw_client["uuid"]
    return {key: value for key, value in raw_client.items() if value is not None}


def _compact_raw_client_mapping(client: Any) -> dict[str, Any]:
    raw_client = _client_raw_mapping(client)
    return {key: value for key, value in raw_client.items() if key not in _CLIENT_RAW_RUNTIME_DROP_FIELDS}


def _raw_inbound_update_payload(raw_inbound: Mapping[str, Any], clients: list[dict[str, Any]]) -> dict[str, Any]:
    settings = _raw_settings_mapping(raw_inbound.get("settings"))
    settings["clients"] = [_compact_raw_client_mapping(client) for client in clients]
    return {
        "remark": str(raw_inbound.get("remark") or ""),
        "enable": _coerce_panel_bool(raw_inbound.get("enable"), default=True),
        "listen": str(raw_inbound.get("listen") or ""),
        "port": _coerce_panel_int(raw_inbound.get("port")),
        "protocol": str(raw_inbound.get("protocol") or ""),
        "expiryTime": _coerce_panel_int(raw_inbound.get("expiryTime", raw_inbound.get("expiry_time"))),
        "settings": json.dumps(settings, ensure_ascii=False, separators=(",", ":")),
        "streamSettings": _raw_inbound_field_as_json(raw_inbound, "streamSettings", "stream_settings"),
        "sniffing": _raw_inbound_field_as_json(raw_inbound, "sniffing"),
    }


def _verify_raw_stream_settings_preserved(
    api: Api,
    inbound_id: int,
    expected_stream_settings: Any,
    *,
    endpoint_name: str,
    email: str,
) -> bool:
    expected = _as_mapping(expected_stream_settings)
    if not expected:
        return True
    try:
        observed_raw = _get_inbound_raw_by_id(api, inbound_id)
    except (
        RequestException,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        logger.exception(
            "%s raw fallback could not verify streamSettings preservation: inbound_id=%s email=%s.",
            endpoint_name,
            inbound_id,
            email,
        )
        return False
    observed = _as_mapping(observed_raw.get("streamSettings", observed_raw.get("stream_settings")))
    for key in _STREAM_SETTINGS_PRESERVE_KEYS:
        if key in expected and observed.get(key) != expected[key]:
            logger.error(
                "%s raw fallback changed streamSettings.%s for inbound_id=%s email=%s; refusing success.",
                endpoint_name,
                key,
                inbound_id,
                email,
            )
            return False
    return True


def _update_inbound_clients_preserving_raw_stream_settings(
    *,
    api: Api,
    inbound_id: int,
    email: str,
    target_client_uuid: str | None,
    client: Any,
    endpoint_name: str,
    replace_existing: bool,
) -> bool:
    if re.search(r"-trial-standard(?:-[a-z0-9-]+)?@vpn\.bot$", str(email or "").strip().lower()):
        logger.warning("Standard trial requires client-only API; full inbound fallback refused.")
        return False
    try:
        raw_inbound = _get_inbound_raw_by_id(api, inbound_id)
        settings = _raw_settings_mapping(raw_inbound.get("settings"))
        raw_clients = [dict(raw_client) for raw_client in settings.get("clients", []) if isinstance(raw_client, dict)]
    except (
        RequestException,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        logger.exception(
            "%s raw fallback failed: could not load full raw inbound_id=%s for email=%s.",
            endpoint_name,
            inbound_id,
            email,
        )
        return False

    raw_client = _client_raw_mapping(client)
    client_uuid = str(target_client_uuid or raw_client.get("id") or raw_client.get("uuid") or "").strip()
    client_index, duplicate_indexes = _select_client_indexes_for_upsert(
        raw_clients,
        email=email,
        target_client_uuid=client_uuid,
    )
    if replace_existing and client_index == -1:
        logger.warning(
            "%s raw fallback failed: client not found in raw inbound_id=%s email=%s uuid=%s.",
            endpoint_name,
            inbound_id,
            email,
            client_uuid,
        )
        return False
    if duplicate_indexes:
        duplicate_index_set = set(duplicate_indexes)
        raw_clients = [
            raw_existing_client
            for index, raw_existing_client in enumerate(raw_clients)
            if index not in duplicate_index_set
        ]
        client_index, _ = _select_client_indexes_for_upsert(
            raw_clients,
            email=email,
            target_client_uuid=client_uuid,
        )

    if client_index == -1:
        raw_clients.append(raw_client)
    else:
        raw_clients[client_index] = raw_client

    expected_stream_settings = raw_inbound.get("streamSettings", raw_inbound.get("stream_settings"))
    payload = _raw_inbound_update_payload(raw_inbound, raw_clients)
    payload_size_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    if payload_size_bytes >= 900_000:
        logger.warning(
            "%s raw fallback payload still large: inbound_id=%s email=%s clients=%s bytes=%s.",
            endpoint_name,
            inbound_id,
            email,
            len(raw_clients),
            payload_size_bytes,
        )
    try:
        response = _api_post_json(api, f"panel/api/inbounds/update/{inbound_id}", payload)
    except (
        RequestException,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        logger.exception(
            "%s raw fallback failed: update request failed for inbound_id=%s email=%s.",
            endpoint_name,
            inbound_id,
            email,
        )
        return False
    if isinstance(response, Mapping) and response.get("success") is False:
        logger.error(
            "%s raw fallback failed: panel rejected inbound.update for inbound_id=%s email=%s msg=%s.",
            endpoint_name,
            inbound_id,
            email,
            response.get("msg") or response.get("message") or "",
        )
        return False
    return _verify_raw_stream_settings_preserved(
        api,
        inbound_id,
        expected_stream_settings,
        endpoint_name=endpoint_name,
        email=email,
    )


def _recover_duplicate_email_update_via_raw_inbound(
    *,
    api: Api,
    inbound_id: int,
    email: str,
    client_uuid: str,
    expiry_ms: int,
    client: Any,
    endpoint_name: str,
) -> tuple[str | None, int | None]:
    if re.search(r"-trial-standard(?:-[a-z0-9-]+)?@vpn\.bot$", str(email or "").strip().lower()):
        logger.warning("Standard trial requires client-only API; full inbound fallback refused.")
        return (None, None)
    safe_inbound_id = _coerce_panel_int(inbound_id)
    expected_uuid = _normalized_client_uuid(client_uuid)
    requested_email = str(email or "").strip()
    requested_email_norm = _normalized_client_email(requested_email)
    if safe_inbound_id <= 0 or not expected_uuid or not requested_email_norm:
        return None, None

    try:
        raw_inbound = _get_inbound_raw_by_id(api, safe_inbound_id)
        settings = _raw_settings_mapping(raw_inbound.get("settings"))
        raw_clients = [dict(raw_client) for raw_client in settings.get("clients", []) if isinstance(raw_client, dict)]
    except (
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        logger.exception(
            "%s duplicate-email recovery failed: could not load raw inbound_id=%s email=%s.",
            endpoint_name,
            safe_inbound_id,
            requested_email,
        )
        return None, None

    uuid_matches = [
        index
        for index, raw_client in enumerate(raw_clients)
        if _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid")) == expected_uuid
    ]
    if len(uuid_matches) != 1:
        logger.warning(
            "%s duplicate-email recovery refused ambiguous UUID match: inbound_id=%s email=%s uuid=%s matches=%s.",
            endpoint_name,
            safe_inbound_id,
            requested_email,
            expected_uuid,
            len(uuid_matches),
        )
        return None, None

    foreign_email_matches = [
        raw_client
        for raw_client in raw_clients
        if _normalized_client_email(raw_client.get("email")) == requested_email_norm
        and _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid")) != expected_uuid
    ]
    if foreign_email_matches:
        logger.error(
            "%s duplicate-email recovery refused foreign email owner: inbound_id=%s email=%s uuid=%s.",
            endpoint_name,
            safe_inbound_id,
            requested_email,
            expected_uuid,
        )
        return None, None

    client_index = uuid_matches[0]
    existing_raw_client = raw_clients[client_index]
    existing_email = str(existing_raw_client.get("email") or "").strip()
    if not existing_email:
        logger.error(
            "%s duplicate-email recovery refused client without existing email: inbound_id=%s uuid=%s.",
            endpoint_name,
            safe_inbound_id,
            expected_uuid,
        )
        return None, None

    client_payload = dict(existing_raw_client)
    client_payload.update(_client_raw_mapping(client))
    client_payload["id"] = expected_uuid
    if "uuid" in client_payload:
        client_payload["uuid"] = expected_uuid
    client_payload["email"] = (
        existing_email
        if _normalized_client_email(existing_email) != requested_email_norm
        else requested_email
    )
    client_payload["expiryTime"] = int(expiry_ms)
    client_payload["inboundId"] = safe_inbound_id
    if not str(_extract_client_sub_id(client_payload) or "").strip():
        client_payload["subId"] = _generate_sub_id()
    raw_clients[client_index] = client_payload

    expected_stream_settings = raw_inbound.get("streamSettings", raw_inbound.get("stream_settings"))
    payload = _raw_inbound_update_payload(raw_inbound, raw_clients)
    try:
        response = _api_post_json(api, f"panel/api/inbounds/update/{safe_inbound_id}", payload)
    except (
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        logger.exception(
            "%s duplicate-email recovery failed: raw inbound.update rejected inbound_id=%s email=%s.",
            endpoint_name,
            safe_inbound_id,
            requested_email,
        )
        return None, None
    if isinstance(response, Mapping) and response.get("success") is False:
        logger.error(
            "%s duplicate-email recovery failed: panel rejected inbound.update inbound_id=%s email=%s msg=%s.",
            endpoint_name,
            safe_inbound_id,
            requested_email,
            response.get("msg") or response.get("message") or "",
        )
        return None, None
    if not _verify_raw_stream_settings_preserved(
        api,
        safe_inbound_id,
        expected_stream_settings,
        endpoint_name=endpoint_name,
        email=requested_email,
    ):
        return None, None

    try:
        verified_raw = _get_inbound_raw_by_id(api, safe_inbound_id)
        verified_clients = [
            dict(raw_client)
            for raw_client in _iter_client_dicts_from_settings(verified_raw.get("settings"))
            if isinstance(raw_client, dict)
            and _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid")) == expected_uuid
        ]
    except (
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        logger.exception(
            "%s duplicate-email recovery failed: read-back failed inbound_id=%s email=%s.",
            endpoint_name,
            safe_inbound_id,
            requested_email,
        )
        return None, None
    if len(verified_clients) != 1:
        logger.error(
            "%s duplicate-email recovery failed: read-back UUID matches=%s inbound_id=%s email=%s uuid=%s.",
            endpoint_name,
            len(verified_clients),
            safe_inbound_id,
            requested_email,
            expected_uuid,
        )
        return None, None
    verified_expiry_ms = _coerce_panel_int(
        verified_clients[0].get("expiryTime", verified_clients[0].get("expiry_time"))
    )
    if verified_expiry_ms != int(expiry_ms):
        logger.error(
            "%s duplicate-email recovery failed: read-back expiry mismatch inbound_id=%s email=%s uuid=%s got=%s want=%s.",
            endpoint_name,
            safe_inbound_id,
            requested_email,
            expected_uuid,
            verified_expiry_ms,
            int(expiry_ms),
        )
        return None, None
    verified_email = str(verified_clients[0].get("email") or "").strip()
    if _normalized_client_email(verified_email) != requested_email_norm:
        logger.warning(
            "%s duplicate-email recovery kept existing panel email inbound_id=%s requested_email=%s panel_email=%s uuid=%s.",
            endpoint_name,
            safe_inbound_id,
            requested_email,
            verified_email,
            expected_uuid,
        )
    return expected_uuid, int(expiry_ms)


def scan_inbound_clients_minimal(
    api: Api,
    inbound_id: int | str,
    local_key_emails: Iterable[str],
    orphan_sample_limit: int = 20,
    *,
    match_subscription_variants: bool = False,
) -> PanelClientScan:
    local_email_by_normalized = {
        _normalized_client_email(email): str(email or "").strip()
        for email in local_key_emails
        if _normalized_client_email(email)
    }
    raw_inbound = _get_inbound_raw_by_id(api, inbound_id)
    if "settings" not in raw_inbound:
        raise ValueError("panel_inbound_settings_missing")

    clients_by_email: dict[str, PanelClientSummary] = {}
    orphan_total = 0
    orphan_sample: list[str] = []
    sample_limit = max(0, int(orphan_sample_limit))
    exact_matches: set[str] = set()

    for raw_client in _iter_client_dicts_from_settings(raw_inbound.get("settings")):
        client = _client_summary_from_mapping(raw_client)
        if client is None:
            continue
        normalized_panel_email = _normalized_client_email(client.email)
        local_email = local_email_by_normalized.get(normalized_panel_email)
        is_exact_match = bool(local_email)
        if not local_email and match_subscription_variants:
            local_part, separator, domain = normalized_panel_email.rpartition("@")
            while separator and "-" in local_part:
                local_part = local_part.rsplit("-", 1)[0]
                local_email = local_email_by_normalized.get(f"{local_part}@{domain}")
                if local_email:
                    break
        if local_email:
            # A base subscription email may be absent from the panel while its
            # per-inbound variants are present. Key the result by the DB email
            # so the scheduler treats the subscription as present. If an exact
            # client also exists, prefer it as the authoritative expiry source.
            if local_email not in exact_matches:
                clients_by_email[local_email] = client
            if is_exact_match:
                exact_matches.add(local_email)
            continue
        orphan_total += 1
        if len(orphan_sample) < sample_limit:
            orphan_sample.append(client.email)

    return PanelClientScan(
        clients_by_email=clients_by_email,
        orphan_total=orphan_total,
        orphan_sample=orphan_sample,
    )


def scan_subscription_inbounds_clients_minimal(
    api: Api,
    primary_inbound: Inbound,
    host_name: str,
    local_key_emails: Iterable[str],
    orphan_sample_limit: int = 20,
    *,
    include_white_inbounds: bool = True,
) -> PanelClientScan:
    raw_inbounds = _list_inbounds_raw(api)
    active_inbounds = _subscription_inbounds(
        api,
        primary_inbound,
        include_white_inbounds=include_white_inbounds,
        white_node_inbound_ids=_white_node_inbound_ids_for_panel(host_name),
        raw_inbounds=raw_inbounds or None,
        host_name=host_name,
    )
    if not active_inbounds:
        active_inbounds = [primary_inbound]

    clients_by_email: dict[str, PanelClientSummary] = {}
    orphan_total = 0
    orphan_sample: list[str] = []
    sample_limit = max(0, int(orphan_sample_limit))
    for inbound in active_inbounds:
        inbound_id = _safe_inbound_id(inbound)
        if inbound_id <= 0:
            continue
        scan = scan_inbound_clients_minimal(
            api,
            inbound_id,
            local_key_emails,
            orphan_sample_limit=max(0, sample_limit - len(orphan_sample)),
            match_subscription_variants=True,
        )
        for local_email, client in scan.clients_by_email.items():
            existing_client = clients_by_email.get(local_email)
            client_is_exact = _normalized_client_email(client.email) == _normalized_client_email(local_email)
            existing_is_exact = bool(
                existing_client
                and _normalized_client_email(existing_client.email) == _normalized_client_email(local_email)
            )
            if existing_client is None or client_is_exact or not existing_is_exact:
                clients_by_email[local_email] = client
        orphan_total += scan.orphan_total
        if len(orphan_sample) < sample_limit:
            orphan_sample.extend(scan.orphan_sample[: sample_limit - len(orphan_sample)])

    return PanelClientScan(
        clients_by_email=clients_by_email,
        orphan_total=orphan_total,
        orphan_sample=orphan_sample,
    )


def count_inbound_clients_minimal(api: Api, inbound_id: int | str) -> int:
    raw_inbound = _get_inbound_raw_by_id(api, inbound_id)
    if "settings" not in raw_inbound:
        raise ValueError("panel_inbound_settings_missing")
    return sum(1 for _client in _iter_client_dicts_from_settings(raw_inbound.get("settings")))


def find_inbound_client_minimal(
    api: Api,
    inbound_id: int | str,
    *,
    email: str | None = None,
    client_uuid: str | None = None,
) -> Client | None:
    raw_inbound = _get_inbound_raw_by_id(api, inbound_id)
    if "settings" not in raw_inbound:
        raise ValueError("panel_inbound_settings_missing")

    normalized_email = _normalized_client_email(email)
    normalized_uuid = _normalized_client_uuid(client_uuid)
    for raw_client in _iter_client_dicts_from_settings(raw_inbound.get("settings")):
        raw_email = _normalized_client_email(raw_client.get("email"))
        raw_uuid = _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid"))
        if (normalized_uuid and raw_uuid == normalized_uuid) or (normalized_email and raw_email == normalized_email):
            return _client_model_from_raw_mapping(
                raw_client,
                inbound_id=_coerce_panel_int(inbound_id),
            )
    return None


def filter_available_subscription_base_emails_on_host(
    host_name: str,
    base_emails: Iterable[str],
    *,
    include_white_inbounds: bool = True,
    require_white_inbound: bool = False,
) -> tuple[list[str], dict[str, list[str]]]:
    """Return base emails whose planned subscription clients do not exist on the panel."""

    def _matches_subscription_base_email(raw_email: str, base_email: str) -> bool:
        if not raw_email or not base_email:
            return False
        if raw_email == base_email:
            return True
        base_local, sep, base_domain = base_email.partition("@")
        if not sep or not base_local or not base_domain:
            return False
        # Legacy multi-inbound clients used suffixed emails, e.g.
        # user1-key2-whitelist1-1@vpn.bot. Treat them as occupying
        # user1-key2@vpn.bot so purchases do not retry a colliding base.
        return raw_email.startswith(f"{base_local}-") and raw_email.endswith(f"@{base_domain}")

    unique_base_emails: list[str] = []
    seen_base_emails: set[str] = set()
    for raw_email in base_emails:
        email = str(raw_email or "").strip()
        normalized_email = _normalized_client_email(email)
        if not normalized_email or normalized_email in seen_base_emails:
            continue
        unique_base_emails.append(email)
        seen_base_emails.add(normalized_email)
    if not unique_base_emails:
        return [], {}

    host_data = get_host(host_name)
    if not host_data:
        logger.warning("Cannot preflight panel email conflicts: host '%s' not found.", host_name)
        return unique_base_emails, {}

    api, primary_inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=_resolve_host_api_token(host_data),
        prefer_raw_inbound_lookup=True,
    )
    if not api or not primary_inbound:
        logger.warning("Cannot preflight panel email conflicts: host '%s' login failed.", host_name)
        return unique_base_emails, {}

    white_node_inbound_ids = _white_node_inbound_ids_for_panel(host_name)
    primary_inbound_id = _safe_inbound_id(primary_inbound)
    if _host_is_white(host_data) and primary_inbound_id > 0:
        white_node_inbound_ids.add(primary_inbound_id)

    raw_inbounds = _list_inbounds_raw(api)
    active_inbounds = _subscription_inbounds(
        api,
        primary_inbound,
        include_white_inbounds=include_white_inbounds,
        white_node_inbound_ids=white_node_inbound_ids,
        raw_inbounds=raw_inbounds or None,
        host_name=host_name,
        require_migration_targets=bool(include_white_inbounds or require_white_inbound),
    )
    if require_white_inbound and not any(
        _is_white_subscription_inbound(inbound, white_node_inbound_ids) for inbound in active_inbounds
    ):
        logger.warning("Cannot preflight panel email conflicts: host '%s' has no whitelist inbound.", host_name)
        return unique_base_emails, {}
    if not active_inbounds:
        logger.warning("Cannot preflight panel email conflicts: host '%s' has no subscription inbounds.", host_name)
        return unique_base_emails, {}

    planned_by_base: dict[str, list[str]] = {}
    planned_norms: set[str] = set()
    for base_email in unique_base_emails:
        planned: list[str] = []
        for index, inbound in enumerate(active_inbounds):
            planned_email = base_email if index == 0 else _secondary_inbound_email(base_email, inbound, host_name=host_name)
            planned.append(planned_email)
            planned_norms.add(_normalized_client_email(planned_email))
        planned_by_base[base_email] = planned
    base_norm_by_email = {
        base_email: _normalized_client_email(base_email)
        for base_email in unique_base_emails
    }

    raw_inbound_by_id: dict[int, Mapping[str, Any]] = {}
    for raw_inbound in raw_inbounds:
        inbound_id = _coerce_panel_int(raw_inbound.get("id"))
        if inbound_id > 0:
            raw_inbound_by_id[inbound_id] = raw_inbound

    scan_inbound_ids = set(raw_inbound_by_id)
    scan_inbound_ids.update(_safe_inbound_id(inbound) for inbound in active_inbounds)
    existing_norms: set[str] = set()
    existing_base_conflicts: dict[str, set[str]] = {}

    def _record_existing_email(raw_email_value: Any) -> None:
        raw_email = _normalized_client_email(raw_email_value)
        if not raw_email:
            return
        if raw_email in planned_norms:
            existing_norms.add(raw_email)
        for base_email, base_norm in base_norm_by_email.items():
            if _matches_subscription_base_email(raw_email, base_norm):
                existing_base_conflicts.setdefault(base_email, set()).add(raw_email)

    for inbound_id in sorted(inbound_id for inbound_id in scan_inbound_ids if inbound_id > 0):
        raw_inbound = raw_inbound_by_id.get(inbound_id)
        if not raw_inbound or raw_inbound.get("settings") is None:
            try:
                raw_inbound = _get_inbound_raw_by_id(api, inbound_id, allow_list_fallback=True)
            except (
                RequestException,
                RuntimeError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
            ) as exc:
                logger.warning(
                    "Panel email conflict preflight skipped inbound_id=%s on host '%s': %s",
                    inbound_id,
                    host_name,
                    exc,
                )
                continue
        try:
            raw_clients = _iter_client_dicts_from_settings(raw_inbound.get("settings"))
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Panel email conflict preflight could not parse inbound_id=%s on host '%s': %s",
                inbound_id,
                host_name,
                exc,
            )
            continue
        for raw_client in raw_clients:
            _record_existing_email(raw_client.get("email"))
        for raw_stat in raw_inbound.get("clientStats") or []:
            if isinstance(raw_stat, Mapping):
                _record_existing_email(raw_stat.get("email"))
        if planned_norms and existing_norms.issuperset(planned_norms):
            break

    conflicts_by_base: dict[str, list[str]] = {}
    for base_email, planned_emails in planned_by_base.items():
        conflicts = [
            planned_email
            for planned_email in planned_emails
            if _normalized_client_email(planned_email) in existing_norms
        ]
        seen_conflicts = {_normalized_client_email(conflict) for conflict in conflicts}
        for raw_email in sorted(existing_base_conflicts.get(base_email, set())):
            if raw_email and raw_email not in seen_conflicts:
                conflicts.append(raw_email)
                seen_conflicts.add(raw_email)
        if conflicts:
            conflicts_by_base[base_email] = conflicts
    available = [base_email for base_email in unique_base_emails if base_email not in conflicts_by_base]
    return available, conflicts_by_base


def _is_port_exists_error(exc: Exception, port: int) -> bool:
    error_text = str(exc).lower()
    return "port" in error_text and "exist" in error_text and str(int(port)) in error_text


def _build_node_inbound_tag(host_name: str, port: int) -> str:
    host_slug = _host_email_suffix(host_name).replace("-", "_")
    return f"node_{host_slug}_inbound_{int(port)}"


def _find_node_inbound(
    *,
    inbounds: list[dict[str, Any]],
    node_id: int,
    port: int,
    protocol: str,
    tag: str,
    labels: set[str] | None = None,
    allow_port_fallback: bool = False,
) -> dict[str, Any] | None:
    normalized_protocol = str(protocol or "").strip().lower()
    normalized_tag = str(tag or "").strip().lower()
    normalized_labels = {str(label or "").strip().lower() for label in (labels or set()) if str(label or "").strip()}
    node_protocol_matches: list[dict[str, Any]] = []
    for inbound in inbounds:
        current_node_id = inbound.get("nodeId")
        if current_node_id is None:
            continue
        try:
            if int(current_node_id) != int(node_id):
                continue
        except (TypeError, ValueError):
            continue
        if str(inbound.get("protocol") or "").strip().lower() != normalized_protocol:
            continue
        node_protocol_matches.append(inbound)

    for inbound in node_protocol_matches:
        if normalized_tag and str(inbound.get("tag") or "").strip().lower() == normalized_tag:
            return inbound

    for inbound in node_protocol_matches:
        inbound_labels = {
            str(inbound.get("remark") or "").strip().lower(),
            str(inbound.get("tag") or "").strip().lower(),
        }
        if normalized_labels.intersection(inbound_labels):
            return inbound

    if allow_port_fallback:
        for inbound in node_protocol_matches:
            try:
                inbound_port = int(inbound.get("port") or 0)
            except (TypeError, ValueError):
                inbound_port = 0
            if inbound_port == int(port):
                return inbound

    if normalized_labels:
        for inbound in inbounds:
            if str(inbound.get("protocol") or "").strip().lower() != normalized_protocol:
                continue
            try:
                inbound_port = int(inbound.get("port") or 0)
            except (TypeError, ValueError):
                inbound_port = 0
            if inbound_port != int(port):
                continue
            inbound_labels = {
                str(inbound.get("remark") or "").strip().lower(),
                str(inbound.get("tag") or "").strip().lower(),
            }
            if normalized_labels.intersection(inbound_labels):
                return inbound

    if allow_port_fallback:
        for inbound in inbounds:
            if str(inbound.get("protocol") or "").strip().lower() != normalized_protocol:
                continue
            try:
                inbound_port = int(inbound.get("port") or 0)
            except (TypeError, ValueError):
                inbound_port = 0
            if inbound_port == int(port):
                return inbound
    return None


def _resolve_node_inbound_candidate(
    *,
    api: Api,
    node_id: int,
    port: int,
    protocol: str,
    tag: str,
    labels: set[str],
    allow_port_fallback: bool,
) -> dict[str, Any] | None:
    return _find_node_inbound(
        inbounds=_list_inbounds_raw(api),
        node_id=node_id,
        port=port,
        protocol=protocol,
        tag=tag,
        labels=labels,
        allow_port_fallback=allow_port_fallback,
    )


def _wait_for_node_inbound_import(
    *,
    api: Api,
    node_id: int,
    port: int,
    protocol: str,
    tag: str,
    labels: set[str],
    allow_port_fallback: bool,
) -> dict[str, Any] | None:
    for attempt in range(NODE_INBOUND_IMPORT_ATTEMPTS):
        resolved = _resolve_node_inbound_candidate(
            api=api,
            node_id=node_id,
            port=port,
            protocol=protocol,
            tag=tag,
            labels=labels,
            allow_port_fallback=allow_port_fallback,
        )
        if resolved:
            return resolved
        if attempt < NODE_INBOUND_IMPORT_ATTEMPTS - 1:
            time.sleep(NODE_INBOUND_IMPORT_DELAY_SECONDS)
    return None


def _configured_white_host_inbound(host_name: str, host_data: Mapping[str, Any]) -> Inbound:
    api, inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=_resolve_host_api_token(dict(host_data)),
    )
    if not api or not inbound:
        message = f"White host '{host_name}' configured inbound is unavailable."
        raise RuntimeError(message)
    if not _looks_like_white_bypass_label(_inbound_label(inbound)):
        message = f"White host '{host_name}' configured inbound '{_inbound_label(inbound)}' is not marked as white."
        raise RuntimeError(message)
    return inbound


def _resolve_node_sync_template_inbound(
    host_name: str,
    host_data: Mapping[str, Any],
) -> tuple[Inbound | None, bool]:
    api, configured_inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=_resolve_host_api_token(dict(host_data)),
    )
    candidate_inbounds = [configured_inbound] if configured_inbound else []
    if api:
        with suppress(
            AttributeError,
            KeyError,
            RequestException,
            RuntimeError,
            TypeError,
            ValueError,
        ):
            candidate_inbounds.extend(list(api.inbound.get_list() or []))

    for inbound in candidate_inbounds:
        if inbound and _looks_like_white_bypass_label(_inbound_label(inbound)):
            return inbound, True

    if _host_is_white(host_data):
        message = f"White host '{host_name}' has no inbound marked as white."
        raise RuntimeError(message)

    return None, False


def validate_host_catalog_entry(host_name: str) -> dict[str, Any]:
    target_host_name = str(host_name or "").strip()
    if not target_host_name:
        raise ValueError("Host name is required for validation.")  # noqa: TRY003

    host_data = get_host(target_host_name)
    if not host_data:
        raise ValueError(f"Host '{target_host_name}' was not found in DB.")  # noqa: TRY003

    result = login_to_host_detailed(
        host_url=str(host_data["host_url"]),
        username=str(host_data["host_username"]),
        password=str(host_data["host_pass"]),
        inbound_id=int(host_data["host_inbound_id"]),
        api_token=_resolve_host_api_token(host_data),
        retries=1,
    )
    configured_inbound = result.inbound
    resolved_inbound = configured_inbound
    is_white_host = _host_is_white(host_data)

    if is_white_host:
        resolved_inbound, _ = _resolve_node_sync_template_inbound(
            target_host_name,
            host_data,
        )

    if not result.api:
        message = result.error_message or "Host panel login failed."
        raise RuntimeError(message)
    if not resolved_inbound:
        message = result.error_message or "Configured inbound was not found."
        raise RuntimeError(message)

    resolved_inbound_id = int(getattr(resolved_inbound, "id", 0) or 0)
    configured_inbound_id = int(host_data.get("host_inbound_id") or 0)
    if resolved_inbound_id <= 0:
        raise RuntimeError("Resolved inbound has invalid ID.")  # noqa: TRY003

    if (
        is_white_host
        and resolved_inbound_id != configured_inbound_id
        and not update_host_inbound_id(target_host_name, resolved_inbound_id)
    ):
        raise RuntimeError(  # noqa: TRY003
            f"Failed to persist white inbound ID '{resolved_inbound_id}' for host '{target_host_name}'."
        )

    return {
        "host_name": target_host_name,
        "inbound_id": resolved_inbound_id,
        "inbound_remark": _inbound_label(resolved_inbound),
        "is_white_host": is_white_host,
        "configured_inbound_id": configured_inbound_id,
        "changed_inbound_id": resolved_inbound_id != configured_inbound_id,
    }


def _create_or_resolve_node_inbound(
    *,
    api: Api,
    primary_inbound: Inbound,
    node_id: int,
    host_name: str,
    template_inbound: Inbound | None = None,
    preserve_template_remark: bool = False,
) -> int:
    source_inbound = template_inbound or primary_inbound
    source_payload = source_inbound.to_json()
    source_port = int(source_payload.get("port") or 0)
    source_protocol = str(source_payload.get("protocol") or "").strip()
    source_remark = str(source_payload.get("remark") or "").strip()
    preserve_template_remark = preserve_template_remark or _looks_like_white_bypass_label(source_remark)
    inbound_remark = source_remark if preserve_template_remark and source_remark else str(host_name).strip()
    source_settings_raw = str(source_payload.get("settings") or "")
    try:
        source_settings = json.loads(source_settings_raw) if source_settings_raw else {}
    except json.JSONDecodeError:
        source_settings = {}
    if isinstance(source_settings, dict):
        source_settings["clients"] = []
    else:
        source_settings = {"clients": []}

    source_tag = str(source_payload.get("tag") or getattr(source_inbound, "tag", "") or "").strip()
    inbound_tag = (
        source_tag if preserve_template_remark and source_tag else _build_node_inbound_tag(host_name, source_port)
    )
    lookup_labels = {inbound_remark, source_remark} if not source_tag else {source_tag}
    existing = _resolve_node_inbound_candidate(
        api=api,
        node_id=node_id,
        port=source_port,
        protocol=source_protocol,
        tag=inbound_tag,
        labels=lookup_labels,
        allow_port_fallback=preserve_template_remark and not source_tag,
    )
    if existing and int(existing.get("id") or 0) > 0:
        return int(existing["id"])

    if preserve_template_remark:
        stale = None
        if source_tag:
            stale = _resolve_node_inbound_candidate(
                api=api,
                node_id=node_id,
                port=source_port,
                protocol=source_protocol,
                tag="",
                labels=set(),
                allow_port_fallback=True,
            )
        stale_id = int((stale or {}).get("id") or 0)
        stale_tag = str((stale or {}).get("tag") or "").strip()
        if stale_id > 0 and stale_tag != source_tag:
            _api_post_json(api, f"panel/api/inbounds/del/{stale_id}", {})

        imported = _wait_for_node_inbound_import(
            api=api,
            node_id=node_id,
            port=source_port,
            protocol=source_protocol,
            tag=inbound_tag,
            labels=lookup_labels,
            allow_port_fallback=not source_tag,
        )
        if imported and int(imported.get("id") or 0) > 0:
            return int(imported["id"])

    inbound_payload = {
        "enable": bool(source_payload.get("enable", True)) and not preserve_template_remark,
        "port": source_port,
        "protocol": source_protocol,
        "settings": json.dumps(source_settings, ensure_ascii=False),
        "streamSettings": source_payload.get("streamSettings") or "",
        "sniffing": source_payload.get("sniffing") or "",
        "remark": inbound_remark,
        "tag": inbound_tag,
        "nodeId": int(node_id),
    }
    try:
        _api_post_json(api, "panel/api/inbounds/add", inbound_payload)
    except (RequestException, RuntimeError, ValueError) as exc:
        if not _is_port_exists_error(exc, source_port):
            raise
        resolved = _resolve_node_inbound_candidate(
            api=api,
            node_id=node_id,
            port=source_port,
            protocol=source_protocol,
            tag=inbound_tag,
            labels=lookup_labels,
            allow_port_fallback=True,
        )
        if resolved and int(resolved.get("id") or 0) > 0:
            return int(resolved["id"])
        raise

    resolved = _resolve_node_inbound_candidate(
        api=api,
        node_id=node_id,
        port=source_port,
        protocol=source_protocol,
        tag=inbound_tag,
        labels=lookup_labels,
        allow_port_fallback=False,
    )
    if resolved and int(resolved.get("id") or 0) > 0:
        return int(resolved["id"])

    raise RuntimeError(f"Failed to create/find node inbound for host '{host_name}'.")  # noqa: TRY003


def _is_active_panel_client(client: Any, *, now_ms: int) -> bool:
    if not bool(getattr(client, "enable", False)):
        return False
    expiry_ms = int(getattr(client, "expiry_time", 0) or 0)
    return expiry_ms > now_ms


def _mainpanel_source_client_eligibility(
    client: Any,
    local_key: Mapping[str, Any],
    *,
    now_ms: int,
) -> tuple[bool, int]:
    source_expiry_ms = int(getattr(client, "expiry_time", 0) or 0)
    paid_expiry_ms = _mainpanel_key_expiry_ms(local_key) or source_expiry_ms
    if not local_key:
        return _is_active_panel_client(client, now_ms=now_ms), paid_expiry_ms

    try:
        archived = bool(int(local_key.get("is_archived") or 0))
    except (TypeError, ValueError):
        archived = True
    if archived or paid_expiry_ms <= now_ms:
        return False, paid_expiry_ms

    active_paid_key = _mainpanel_resolve_key_tier(local_key) in {
        SUBSCRIPTION_TIER_STANDARD, SUBSCRIPTION_TIER_SUPER
    }
    eligible = _is_active_panel_client(client, now_ms=now_ms) or active_paid_key
    return eligible, paid_expiry_ms


def _months_from_remaining_expiry(expiry_ms: int, *, now_ms: int) -> int:
    if expiry_ms <= now_ms:
        return 1
    remaining_ms = max(0, int(expiry_ms) - int(now_ms))
    remaining_days = remaining_ms / (24 * 60 * 60 * 1000)
    return max(1, math.ceil(remaining_days / 30))


def _white_host_total_gb_bytes(
    expiry_ms: int,
    *,
    now_ms: int,
    is_white_host: bool,
    unlimited_whitelist: bool = False,
    local_key: Mapping[str, Any] | None = None,
) -> int:
    del expiry_ms, now_ms
    if not is_white_host:
        return 0
    if unlimited_whitelist:
        return 0
    # Node copies use the key tariff; ordinary nodes stay unlimited.
    return whitelist_traffic_limit_bytes_for_key(local_key)


def sync_new_host_into_main_panel(
    host_name: str,
    *,
    subscription_verify_limit: int | None = None,
    progress_every: int | None = None,
    progress_callback: Any | None = None,
    client_limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    target_host_name = str(host_name or "").strip()
    if not target_host_name:
        raise ValueError("Host name is required for node sync.")  # noqa: TRY003

    target_host = get_host(target_host_name)
    if not target_host:
        raise ValueError(f"Host '{target_host_name}' was not found in DB.")  # noqa: TRY003

    super_host_name = str(get_setting("super_key_host_name") or "").strip()
    if not super_host_name:
        raise ValueError("Setting 'super_key_host_name' is not configured.")  # noqa: TRY003
    if super_host_name == target_host_name:
        raise ValueError("Main super host cannot be synced as an additional node.")  # noqa: TRY003

    main_host = get_host(super_host_name)
    if not main_host:
        raise ValueError(  # noqa: TRY003
            f"Configured super host '{super_host_name}' was not found in DB."
        )

    main_api_token = _resolve_host_api_token(main_host)
    if not _normalize_api_token(main_api_token):
        raise ValueError(  # noqa: TRY003
            f"Main host '{super_host_name}' requires XUI API token for node sync workflow."
        )
    node_api_token = _resolve_host_api_token(target_host)
    if not _normalize_api_token(node_api_token):
        raise ValueError(  # noqa: TRY003
            f"Host '{target_host_name}' requires XUI API token for node registration."
        )

    template_inbound, is_white_host = _resolve_node_sync_template_inbound(
        target_host_name,
        target_host,
    )
    if is_white_host and template_inbound:
        template_inbound_id = int(getattr(template_inbound, "id", 0) or 0)
        configured_inbound_id = int(target_host.get("host_inbound_id") or 0)
        if template_inbound_id > 0 and template_inbound_id != configured_inbound_id:
            if not update_host_inbound_id(target_host_name, template_inbound_id):
                raise RuntimeError(  # noqa: TRY003
                    f"Failed to persist white inbound ID '{template_inbound_id}' for host '{target_host_name}'."
                )
            target_host["host_inbound_id"] = template_inbound_id

    api, primary_inbound = login_to_host(
        host_url=main_host["host_url"],
        username=main_host["host_username"],
        password=main_host["host_pass"],
        inbound_id=main_host["host_inbound_id"],
        api_token=main_api_token,
    )
    if not api or not primary_inbound:
        raise RuntimeError(f"Failed to authenticate on main host '{super_host_name}'.")  # noqa: TRY003

    node_id = _resolve_or_create_node(
        api,
        host_name=target_host_name,
        host_data=target_host,
        node_api_token=node_api_token,
    )
    node_probe_ok = _probe_node(api, node_id)
    node_inbound_id = _create_or_resolve_node_inbound(
        api=api,
        primary_inbound=primary_inbound,
        node_id=node_id,
        host_name=target_host_name,
        template_inbound=template_inbound,
        preserve_template_remark=is_white_host,
    )

    node_inbound = api.inbound.get_by_id(node_inbound_id)
    if not node_inbound:
        raise RuntimeError(  # noqa: TRY003
            f"Node inbound '{node_inbound_id}' was not found after creation."
        )

    primary_refreshed = api.inbound.get_by_id(primary_inbound.id) or primary_inbound
    source_clients = list(getattr(primary_refreshed.settings, "clients", []) or [])

    now_ms = int(time.time() * 1000)
    old_node_inbound_id = int(target_host.get("node_inbound_id") or 0)
    copied = 0
    processed = 0
    skipped = 0
    verified_sub_ids = 0
    verify_limit = (
        NODE_SYNC_SUBSCRIPTION_VERIFY_LIMIT
        if subscription_verify_limit is None
        else max(0, int(subscription_verify_limit))
    )
    progress_step = NODE_SYNC_PROGRESS_EVERY if progress_every is None else max(1, int(progress_every))
    max_clients_to_copy = None if client_limit is None else max(0, int(client_limit))
    synced_clients: list[dict[str, str]] = []

    def _emit_progress_if_needed() -> None:
        if progress_callback and processed % progress_step == 0:
            progress_callback({"processed": processed, "copied": copied, "skipped": skipped})

    for client in source_clients:
        if max_clients_to_copy is not None and copied >= max_clients_to_copy:
            break
        processed += 1
        source_email = str(getattr(client, "email", "") or "").strip()
        source_uuid = str(getattr(client, "id", "") or "").strip()
        source_sub_id = (_extract_client_sub_id(client) or "").strip()
        if not source_email or not source_uuid or not source_sub_id:
            skipped += 1
            _emit_progress_if_needed()
            continue

        try:
            local_key = get_key_by_email(source_email) or {}
        except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError) as exc:
            logger.debug("Node sync could not load paid expiry for email=%s: %s", source_email, exc)
            local_key = {}
        eligible, paid_expiry_ms = _mainpanel_source_client_eligibility(
            client,
            local_key,
            now_ms=now_ms,
        )
        if is_white_host and standard_trial_is_ordinary_only(local_key):
            eligible = False
        if not eligible:
            skipped += 1
            _emit_progress_if_needed()
            continue

        target_email = build_host_specific_email(
            source_email,
            f"{target_host_name}-{node_inbound_id}",
        )
        # New-host behavior is deliberately changed only for explicitly marked
        # keys.  Legacy 2099 clients retain their pre-patch node-sync behavior.
        unlimited_whitelist = _key_has_unlimited_whitelist_override(local_key)
        total_gb_bytes = _white_host_total_gb_bytes(
            paid_expiry_ms,
            now_ms=now_ms,
            is_white_host=is_white_host,
            unlimited_whitelist=unlimited_whitelist,
            local_key=local_key,
        )
        target_node_expiry_ms = paid_expiry_ms
        target_traffic_reset_days: int | None = None
        if is_white_host and unlimited_whitelist:
            target_traffic_reset_days = 0
        elif is_white_host:
            cycle_anchor_ms = _datetime_value_ms(
                local_key.get("whitelist_cycle_anchor_at") or local_key.get("created_date")
            ) or now_ms
            target_node_expiry_ms, _cycle_overdue = _next_whitelist_reset_deadline_ms(
                cycle_anchor_ms,
                None,
                paid_expiry_ms,
                now_ms,
                current_reset_days=None,
            )
            target_traffic_reset_days = (
                0
                if target_node_expiry_ms >= paid_expiry_ms
                else MULTI_NODE_LIMIT_RESET_DAYS
            )
        if not dry_run:
            result_uuid, result_expiry = update_or_create_client_on_panel(
                api=api,
                inbound_id=node_inbound_id,
                email=target_email,
                days_to_add=0,
                target_expiry_ms=target_node_expiry_ms,
                target_sub_id=source_sub_id,
                target_client_uuid=source_uuid,
                total_gb_bytes=total_gb_bytes,
                traffic_reset_days=target_traffic_reset_days,
                source_client=client,
            )
            if result_uuid != source_uuid or int(result_expiry or 0) != target_node_expiry_ms:
                raise RuntimeError(  # noqa: TRY003
                    f"Failed to sync client '{source_email}' to node inbound '{node_inbound_id}'."
                )
            synced_clients.append(
                {
                    "email": source_email,
                    "uuid": source_uuid,
                    "sub_id": source_sub_id,
                }
            )
        copied += 1

        if not dry_run and verified_sub_ids < verify_limit:
            links = _get_subscription_links(api, source_sub_id)
            if not links:
                raise RuntimeError(  # noqa: TRY003
                    f"Subscription verification returned empty links for subId '{source_sub_id}'."
                )
            verified_sub_ids += 1

        _emit_progress_if_needed()

    stale_node_removed_clients = 0
    if not dry_run and old_node_inbound_id > 0 and old_node_inbound_id != node_inbound_id:
        stale_inbound = api.inbound.get_by_id(old_node_inbound_id)
        if stale_inbound:
            for synced_client in synced_clients:
                stale_email = build_host_specific_email(
                    synced_client["email"],
                    f"{target_host_name}-{old_node_inbound_id}",
                )
                removed_count, cleanup_ok = _delete_matching_subscription_client_from_inbound(
                    api,
                    stale_inbound,
                    client_uuid=synced_client["uuid"],
                    sub_id=synced_client["sub_id"],
                    email=stale_email,
                )
                stale_node_removed_clients += removed_count
                if not cleanup_ok:
                    message = (
                        f"Failed to clean stale node inbound '{old_node_inbound_id}' for host '{target_host_name}'."
                    )
                    raise RuntimeError(message)

    return {
        "host_name": target_host_name,
        "main_host_name": super_host_name,
        "node_id": node_id,
        "node_inbound_id": node_inbound_id,
        "node_probe_ok": node_probe_ok,
        "copied_clients": copied,
        "skipped_clients": skipped,
        "verified_sub_ids": verified_sub_ids,
        "client_limit": max_clients_to_copy,
        "dry_run": dry_run,
        "is_white_host": is_white_host,
        "stale_node_inbound_id": old_node_inbound_id if old_node_inbound_id != node_inbound_id else 0,
        "stale_node_removed_clients": stale_node_removed_clients,
    }


def _get_client_sub_id_from_panel(
    api: Api,
    inbound_id: int,
    *,
    email: str | None = None,
    client_uuid: str | None = None,
) -> str | None:
    try:
        raw_supported = all(hasattr(api.inbound, attr) for attr in ("_get", "_url"))
        try:
            raw_inbound = _get_inbound_raw_by_id(api, inbound_id)
            for raw_client in _iter_client_dicts_from_settings(raw_inbound.get("settings")):
                raw_uuid = _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid"))
                raw_email = _normalized_client_email(raw_client.get("email"))
                if (client_uuid and raw_uuid == _normalized_client_uuid(client_uuid)) or (
                    email and raw_email == _normalized_client_email(email)
                ):
                    return _extract_client_sub_id(raw_client)
        except (
            RequestException,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ) as exc:
            if raw_supported and not PANEL_CLIENT_UPSERT_ALLOW_FULL_INBOUND_FALLBACK:
                logger.warning(
                    "Raw subId lookup failed for inbound_id=%s; refusing full inbound fallback: %s",
                    inbound_id,
                    exc,
                )
                return None
            logger.warning(
                "Raw subId lookup unavailable for inbound_id=%s; using legacy full inbound fallback: %s",
                inbound_id,
                exc,
            )
        else:
            return None

        inbound = api.inbound.get_by_id(inbound_id)
        if not inbound or not inbound.settings:
            return None

        clients = inbound.settings.clients or []
        if not clients:
            return None

        matched_client = None
        if client_uuid:
            matched_client = next(
                (c for c in clients if str(getattr(c, "id", "")) == str(client_uuid)),
                None,
            )
        if not matched_client and email:
            matched_client = next(
                (c for c in clients if str(getattr(c, "email", "")) == str(email)),
                None,
            )
        if not matched_client:
            return None

        return _extract_client_sub_id(matched_client)
    except Exception as e:
        logger.warning(f"Failed to extract subId from panel for inbound {inbound_id}: {e}")
        return None


def _find_client_in_inbound_by_email(inbound: Inbound, email: str):
    target_email = str(email or "").strip()
    if not target_email or not inbound or not getattr(inbound, "settings", None):
        return None
    clients = inbound.settings.clients or []
    for client in clients:
        if str(getattr(client, "email", "")).strip() == target_email:
            return client
    return None


def _find_client_in_inbound_by_uuid(inbound: Inbound, client_uuid: str):
    target_uuid = str(client_uuid or "").strip()
    if not target_uuid or not inbound or not getattr(inbound, "settings", None):
        return None
    clients = inbound.settings.clients or []
    for client in clients:
        if str(getattr(client, "id", "")).strip() == target_uuid:
            return client
    return None


def _build_key_result_from_client(
    *,
    api: Api,
    inbound: Inbound,
    host_name: str,
    host_data: dict,
    email: str,
    client,
    target_expiry_ms: int | None = None,
    subscription_tier: Any = None,
) -> dict | None:
    client_uuid = str(getattr(client, "id", "") or "").strip()
    if not client_uuid:
        return None

    current_expiry_ms = int(getattr(client, "expiry_time", 0) or 0)
    if (
        normalize_subscription_tier(subscription_tier) in {SUBSCRIPTION_TIER_STANDARD, SUBSCRIPTION_TIER_SUPER}
        and target_expiry_ms is not None
        and _is_white_subscription_inbound(inbound)
        and int(_client_config_value(client, ("total_gb", "totalGB"), 0) or 0) > 0
        and int(_client_config_value(client, ("reset",), 0) or 0) > 0
    ):
        logger.warning(
            "Refusing to reconcile paid expiry from a WHITELIST traffic-cycle client: host=%s email=%s",
            host_name,
            email,
        )
        return None
    desired_expiry_ms = int(target_expiry_ms) if target_expiry_ms else current_expiry_ms
    new_expiry_ms = max(current_expiry_ms, desired_expiry_ms)

    # Never shorten existing active access during reconciliation.
    if new_expiry_ms > current_expiry_ms:
        result_uuid, result_expiry = update_or_create_client_on_panel(
            api=api,
            inbound_id=inbound.id,
            email=email,
            days_to_add=0,
            target_expiry_ms=new_expiry_ms,
            target_sub_id=_extract_client_sub_id(client),
            target_client_uuid=client_uuid,
            source_client=client,
        )
        if result_uuid != client_uuid or int(result_expiry or 0) != new_expiry_ms:
            logger.error(
                "Failed to extend existing panel client during reconciliation: host='%s' email='%s'.",
                host_name,
                email,
            )
            return None

    connection_string = get_connection_string(
        inbound,
        client_uuid,
        host_data["host_url"],
        remark=host_name,
        client=client,
    )
    if not connection_string:
        logger.error(
            "Empty connection string for '%s' on host '%s'. Check Reality/TLS settings on panel.",
            email,
            host_name,
        )
        return None

    subscription_link = None
    sub_id = None
    subscription_base_url = _resolve_subscription_base_url(host_data)
    if subscription_base_url:
        sub_id = _get_client_sub_id_from_panel(
            api,
            inbound.id,
            email=email,
            client_uuid=client_uuid,
        )
        if sub_id:
            subscription_link = _build_subscription_link(subscription_base_url, sub_id)

    return {
        "client_uuid": client_uuid,
        "email": email,
        "expiry_timestamp_ms": new_expiry_ms,
        "connection_string": connection_string,
        "subscription_link": subscription_link,
        "host_name": host_name,
        "sub_id": sub_id,
    }


def get_clients_count_from_panel(host_name: str) -> int:
    """
    Get total number of clients from XUI panel (for load balancing).
    Counts all client records: online, offline, and expired.
    Used to select the least loaded host (minimum total clients).

    Args:
        host_name: Name of the host to check

    Returns:
        Total number of clients on the panel, or 999999 on error (to avoid selecting failed host)
    """
    from shop_bot.data_manager.database import get_host

    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Cannot get clients count: Host '{host_name}' not found in database")
        update_host_health_snapshot(
            host_name,
            status="unavailable",
            clients_count=None,
            error_code="host_not_found",
            error_message=f"Host '{host_name}' not found in database",
        )
        return 999999

    original_timeout = socket.getdefaulttimeout()
    try:
        # Set socket timeout to prevent hanging
        socket.setdefaulttimeout(XUI_API_TIMEOUT)

        api = _create_api(
            host_data["host_url"],
            host_data["host_username"],
            host_data["host_pass"],
            _resolve_host_api_token(host_data),
        )
        _authenticate_api(api, _resolve_host_api_token(host_data))

        total_count = count_inbound_clients_minimal(api, host_data["host_inbound_id"])

        logger.info(f"Host '{host_name}' has {total_count} total clients on panel")
        update_host_health_snapshot(
            host_name,
            status="healthy",
            clients_count=total_count,
        )
        return total_count

    except TimeoutError:
        logger.exception(
            "Connection timeout (%ss) for host '%s'. Check if the server is reachable.",
            XUI_API_TIMEOUT,
            host_name,
        )
        update_host_health_snapshot(
            host_name,
            status="unavailable",
            clients_count=None,
            error_code="host_timeout",
            error_message=f"Connection timeout ({XUI_API_TIMEOUT}s)",
        )
        return 999999
    except Exception as e:
        logger.error(f"Failed to get clients count from panel for host '{host_name}': {e}")
        update_host_health_snapshot(
            host_name,
            status="degraded",
            clients_count=None,
            error_code="host_clients_count_failed",
            error_message=str(e),
        )
        return 999999
    finally:
        # Restore original timeout
        socket.setdefaulttimeout(original_timeout)


def _classify_login_exception(exc: Exception) -> tuple[str, str, bool]:
    error_msg = str(exc).strip() or exc.__class__.__name__
    normalized = error_msg.lower()

    if isinstance(exc, socket.timeout) or "timed out" in normalized:
        return (
            "host_timeout",
            f"Connection timeout ({XUI_API_TIMEOUT}s) while connecting to the panel: {error_msg}",
            True,
        )

    if "connection refused" in normalized or "[errno 111]" in normalized:
        return "host_connection_refused", f"Panel refused the connection: {error_msg}", True

    if "connection reset" in normalized or "remote end closed connection" in normalized:
        return "host_connection_reset", f"Panel connection was reset unexpectedly: {error_msg}", True

    if (
        "name or service not known" in normalized
        or "temporary failure in name resolution" in normalized
        or "nodename nor servname provided" in normalized
    ):
        return "host_dns_failed", f"DNS resolution failed for the panel host: {error_msg}", False

    if "certificate verify failed" in normalized or "ssl:" in normalized or "wrong version number" in normalized:
        return "host_ssl_error", f"SSL error while connecting to the panel: {error_msg}", False

    if "unknownprotocol" in error_msg or "http/0.0" in error_msg:
        return (
            "host_invalid_http_response",
            "Panel returned an invalid HTTP response. Check scheme, proxy, or base path.",
            False,
        )

    if (
        "unauthorized" in normalized
        or "invalid username" in normalized
        or "invalid password" in normalized
        or ("login" in normalized and "failed" in normalized)
    ):
        return "host_login_failed", f"Panel authentication failed: {error_msg}", False

    return "host_login_exception", error_msg, False


def login_to_host_detailed(
    host_url: str,
    username: str,
    password: str,
    inbound_id: int,
    *,
    api_token: str | None = None,
    retries: int = XUI_API_LOGIN_RETRIES,
    retry_delay_seconds: float = XUI_API_RETRY_DELAY_SECONDS,
    prefer_raw_inbound_lookup: bool = False,
    fallback_to_lightweight_inbound_lookup: bool = False,
    fallback_to_full_inbound_lookup: bool = True,
    allow_minimal_inbound_on_lookup_failure: bool = False,
) -> HostLoginResult:
    """
    Login to XUI host and retrieve target inbound with retry/classification.

    Network-flap errors are retried a few times before the host is treated as
    unhealthy. Authentication and config errors fail fast.
    """
    attempts_total = max(1, int(retries) + 1)
    last_error_code: str | None = None
    last_error_message: str | None = None

    circuit_open, circuit_remaining = _host_circuit_breaker_precheck(host_url)
    if circuit_open:
        error_message = f"Circuit breaker is open for host '{host_url}'. Skip panel calls for {circuit_remaining}s."
        return HostLoginResult(
            api=None,
            inbound=None,
            error_code="host_circuit_open",
            error_message=error_message,
            attempts=0,
        )

    for attempt in range(1, attempts_total + 1):
        try:
            with _socket_timeout_lock:
                original_timeout = socket.getdefaulttimeout()
                socket.setdefaulttimeout(XUI_API_TIMEOUT)
                try:
                    api = _create_api(host_url, username, password, api_token)
                    _authenticate_api(api, api_token)
                finally:
                    socket.setdefaulttimeout(original_timeout)
            target_inbound: Inbound | Any | None = None
            raw_lookup_failed = False
            if prefer_raw_inbound_lookup:
                try:
                    target_raw = _get_inbound_raw_by_id(
                        api,
                        inbound_id,
                        allow_lightweight_fallback=fallback_to_lightweight_inbound_lookup,
                    )
                    target_inbound = _raw_inbound_runtime_namespace(
                        target_raw,
                        inbound_id=inbound_id,
                    )
                except (
                    RequestException,
                    TimeoutError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                ) as exc:
                    if _is_panel_inbound_not_found_error(exc):
                        error_message = f"Inbound with ID '{inbound_id}' not found on host '{host_url}'"
                        logger.warning("%s: %s", error_message, exc)
                        _host_circuit_breaker_on_success(host_url)
                        return HostLoginResult(
                            api=api,
                            inbound=None,
                            error_code="host_inbound_missing",
                            error_message=error_message,
                            attempts=attempt,
                            recovered_on_retry=attempt > 1,
                        )
                    raw_error_code, raw_error_message, raw_retryable = _classify_login_exception(exc)
                    if raw_error_code in _HOST_CIRCUIT_BREAKER_RETRYABLE_CODES or raw_retryable:
                        circuit_opened_now = _host_circuit_breaker_on_failure(host_url, raw_error_code)
                        if circuit_opened_now:
                            raw_error_code = "host_circuit_open"
                            raw_error_message = (
                                f"Circuit breaker opened for host '{host_url}' after repeated failures. "
                                f"Cooldown: {HOST_CIRCUIT_BREAKER_COOLDOWN_SECONDS}s."
                            )
                        logger.warning(
                            "Raw inbound lookup failed for host '%s' with network/proxy error: %s",
                            host_url,
                            raw_error_message,
                        )
                        return HostLoginResult(
                            api=None,
                            inbound=None,
                            error_code=raw_error_code,
                            error_message=raw_error_message,
                            attempts=attempt,
                        )
                    logger.warning(
                        "Raw inbound lookup failed for inbound_id=%s; falling back to inbound list lookup: %s",
                        inbound_id,
                        exc,
                    )
                    raw_lookup_failed = True
                    target_inbound = None

            if target_inbound is None:
                if (
                    raw_lookup_failed
                    and allow_minimal_inbound_on_lookup_failure
                    and not fallback_to_full_inbound_lookup
                ):
                    target_inbound = _minimal_inbound_runtime_namespace(int(inbound_id))
                elif not fallback_to_full_inbound_lookup:
                    _raise_raw_inbound_lookup_failed()

            if target_inbound is None:
                try:
                    inbounds: list[Inbound] = api.inbound.get_list() or []
                except (
                    RequestException,
                    TimeoutError,
                    RuntimeError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                ):
                    if not raw_lookup_failed or not allow_minimal_inbound_on_lookup_failure:
                        raise
                    logger.warning(
                        "Inbound list lookup also failed for inbound_id=%s; using minimal runtime inbound.",
                        inbound_id,
                        exc_info=True,
                    )
                    target_inbound = _minimal_inbound_runtime_namespace(int(inbound_id))
                else:
                    target_inbound = next(
                        (inbound for inbound in inbounds if _safe_inbound_id(inbound) == int(inbound_id)),
                        None,
                    )

            if target_inbound is None:
                error_message = f"Inbound with ID '{inbound_id}' not found on host '{host_url}'"
                logger.error(error_message)
                _host_circuit_breaker_on_success(host_url)
                return HostLoginResult(
                    api=api,
                    inbound=None,
                    error_code="host_inbound_missing",
                    error_message=error_message,
                    attempts=attempt,
                    recovered_on_retry=attempt > 1,
                )

            if attempt > 1:
                logger.warning(
                    "Recovered XUI panel login for '%s' after %s attempt(s).",
                    host_url,
                    attempt,
                )
            _host_circuit_breaker_on_success(host_url)
            return HostLoginResult(
                api=api,
                inbound=target_inbound,
                attempts=attempt,
                recovered_on_retry=attempt > 1,
            )
        except Exception as exc:
            error_code, error_message, retryable = _classify_login_exception(exc)
            circuit_opened_now = _host_circuit_breaker_on_failure(host_url, error_code)
            last_error_code = error_code
            last_error_message = error_message

            if retryable and attempt < attempts_total and not circuit_opened_now:
                logger.warning(
                    "Transient XUI panel failure for '%s' (attempt %s/%s): %s",
                    host_url,
                    attempt,
                    attempts_total,
                    error_message,
                )
                time.sleep(max(0.0, retry_delay_seconds) * attempt)
                continue

            if circuit_opened_now:
                error_code = "host_circuit_open"
                error_message = (
                    f"Circuit breaker opened for host '{host_url}' after repeated failures. "
                    f"Cooldown: {HOST_CIRCUIT_BREAKER_COOLDOWN_SECONDS}s."
                )

            if error_code == "host_invalid_http_response":
                logger.error(
                    f"Invalid HTTP response from '{host_url}'. "
                    f"Common fixes: 1) Use https:// instead of http://, "
                    f"2) Remove trailing path (use 'https://ip:port' not 'https://ip:port/path/'), "
                    f"3) Check if server is behind a misconfigured proxy. Error: {error_message}"
                )
            elif error_code in {
                "host_timeout",
                "host_connection_refused",
                "host_connection_reset",
                "host_dns_failed",
                "host_ssl_error",
                "host_login_failed",
            }:
                logger.error(
                    "XUI panel login failed for '%s' after %s attempt(s): %s",
                    host_url,
                    attempt,
                    error_message,
                )
            else:
                logger.error(
                    "Login or inbound retrieval failed for host '%s': %s",
                    host_url,
                    exc,
                    exc_info=True,
                )

            return HostLoginResult(
                api=None,
                inbound=None,
                error_code=error_code,
                error_message=error_message,
                attempts=attempt,
            )

    return HostLoginResult(
        api=None,
        inbound=None,
        error_code=last_error_code or "host_login_exception",
        error_message=last_error_message or "Unknown XUI host login failure.",
        attempts=attempts_total,
    )


def login_to_host(
    host_url: str,
    username: str,
    password: str,
    inbound_id: int,
    api_token: str | None = None,
    prefer_raw_inbound_lookup: bool = False,
    fallback_to_lightweight_inbound_lookup: bool = False,
    fallback_to_full_inbound_lookup: bool = True,
    allow_minimal_inbound_on_lookup_failure: bool = False,
) -> tuple[Api | None, Inbound | None]:
    """
    Backward-compatible wrapper around detailed XUI login.
    """
    result = login_to_host_detailed(
        host_url=host_url,
        username=username,
        password=password,
        inbound_id=inbound_id,
        api_token=api_token,
        prefer_raw_inbound_lookup=prefer_raw_inbound_lookup,
        fallback_to_lightweight_inbound_lookup=fallback_to_lightweight_inbound_lookup,
        fallback_to_full_inbound_lookup=fallback_to_full_inbound_lookup,
        allow_minimal_inbound_on_lookup_failure=allow_minimal_inbound_on_lookup_failure,
    )
    return result.api, result.inbound


def _mapping_value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


def _as_mapping(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return {}
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError:
            return {}
        if isinstance(decoded, dict):
            return decoded
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        with suppress(TypeError, ValueError):
            dumped = model_dump(by_alias=True, exclude_none=True)
            if isinstance(dumped, Mapping):
                return dict(dumped)
        with suppress(TypeError, ValueError):
            dumped = model_dump()
            if isinstance(dumped, Mapping):
                return dict(dumped)
    legacy_dict = getattr(value, "dict", None)
    if callable(legacy_dict):
        with suppress(TypeError, ValueError):
            dumped = legacy_dict(by_alias=True, exclude_none=True)
            if isinstance(dumped, Mapping):
                return dict(dumped)
        with suppress(TypeError, ValueError):
            dumped = legacy_dict()
            if isinstance(dumped, Mapping):
                return dict(dumped)
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {}


def _as_list(value: Any) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return []
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError:
            return []
        if isinstance(decoded, list):
            return decoded
    return []


def _mapping_first(source: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = _mapping_value(source, key, _MISSING)
        if value is not _MISSING:
            return value
    return default


def _stream_settings_value(inbound: Inbound, key: str, default: Any = None) -> Any:
    stream_settings = getattr(inbound, "stream_settings", None) or getattr(inbound, "streamSettings", None)
    if not stream_settings:
        return default
    settings_mapping = _as_mapping(stream_settings)
    settings_source = settings_mapping or stream_settings
    aliases = {
        "external_proxy": "externalProxy",
        "reality_settings": "realitySettings",
        "ws_settings": "wsSettings",
        "xhttp_settings": "xhttpSettings",
        "hysteria_settings": "hysteriaSettings",
        "tls_settings": "tlsSettings",
    }
    value = _mapping_value(settings_source, key, _MISSING)
    if value is not _MISSING:
        return value
    alias = aliases.get(key)
    if alias:
        return _mapping_value(settings_source, alias, default)
    return default


def _first_external_proxy(inbound: Inbound) -> Any:
    external_proxy = _stream_settings_value(inbound, "external_proxy", [])
    proxy_items = _as_list(external_proxy)
    if not proxy_items:
        return {}
    proxy_mapping = _as_mapping(proxy_items[0])
    return proxy_mapping or proxy_items[0]


def _normalize_query_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _normalize_external_proxy_port(value: Any) -> str:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return ""
    return str(port) if port > 0 else ""


def _xhttp_settings_with_external_proxy_defaults(xhttp_settings: dict) -> dict:
    settings = copy.deepcopy(_as_mapping(xhttp_settings))
    if not _normalize_query_value(settings.get("path")):
        settings["path"] = DEFAULT_XHTTP_EXTERNAL_PROXY_PATH
    if not _normalize_query_value(settings.get("mode")):
        settings["mode"] = DEFAULT_XHTTP_EXTERNAL_PROXY_MODE

    extra = copy.deepcopy(_as_mapping(settings.get("extra")))
    for key, value in DEFAULT_XHTTP_EXTERNAL_PROXY_EXTRA.items():
        if not _normalize_query_value(extra.get(key)):
            extra[key] = value
    settings["extra"] = extra
    return settings


def _xhttp_external_proxy_client_alpn(_external_proxy: Any) -> str:
    return DEFAULT_XHTTP_EXTERNAL_PROXY_ALPN


def _stream_settings_patch_key(settings: Mapping[str, Any], camel_key: str, snake_key: str) -> str:
    if camel_key in settings:
        return camel_key
    if snake_key in settings:
        return snake_key
    return camel_key


def _xhttp_external_proxy_stream_settings_patch(inbound: Inbound) -> tuple[dict[str, Any], bool] | None:
    if str(_stream_settings_value(inbound, "network", "") or "").strip().lower() != "xhttp":
        return None

    external_proxy_items = _as_list(_stream_settings_value(inbound, "external_proxy", []))
    if not external_proxy_items:
        return None

    stream_settings = getattr(inbound, "stream_settings", None) or getattr(inbound, "streamSettings", None)
    patched_settings = copy.deepcopy(_as_mapping(stream_settings))
    if not patched_settings:
        patched_settings = {"network": "xhttp", "security": "none"}

    original_settings = copy.deepcopy(patched_settings)
    xhttp_key = _stream_settings_patch_key(patched_settings, "xhttpSettings", "xhttp_settings")
    external_proxy_key = _stream_settings_patch_key(patched_settings, "externalProxy", "external_proxy")

    patched_settings["network"] = "xhttp"
    if not _normalize_query_value(patched_settings.get("security")):
        patched_settings["security"] = "none"

    patched_settings[xhttp_key] = _xhttp_settings_with_external_proxy_defaults(
        _mapping_first(patched_settings, "xhttpSettings", "xhttp_settings", default={})
    )

    patched_external_proxy_items = copy.deepcopy(external_proxy_items)
    first_proxy = _as_mapping(patched_external_proxy_items[0])
    if first_proxy:
        first_proxy["alpn"] = _xhttp_external_proxy_client_alpn(first_proxy)
        patched_external_proxy_items[0] = first_proxy
    patched_settings[external_proxy_key] = patched_external_proxy_items

    return patched_settings, patched_settings != original_settings


def _apply_stream_settings_patch(inbound: Inbound, patched_settings: dict[str, Any]) -> None:
    if hasattr(inbound, "stream_settings"):
        original_stream_settings = inbound.stream_settings
        if isinstance(original_stream_settings, str):
            inbound.stream_settings = json.dumps(patched_settings, ensure_ascii=False, separators=(",", ":"))
            return
        if isinstance(original_stream_settings, dict):
            original_stream_settings.clear()
            original_stream_settings.update(patched_settings)
            return
        if hasattr(original_stream_settings, "__dict__"):
            for key, value in patched_settings.items():
                attr_name = {
                    "externalProxy": "external_proxy",
                    "xhttpSettings": "xhttp_settings",
                }.get(key, key)
                setattr(original_stream_settings, attr_name, value)
            return
        inbound.stream_settings = patched_settings
        return

    if hasattr(inbound, "streamSettings"):
        original_stream_settings = inbound.streamSettings
        if isinstance(original_stream_settings, str):
            inbound.streamSettings = json.dumps(patched_settings, ensure_ascii=False, separators=(",", ":"))
        elif isinstance(original_stream_settings, dict):
            original_stream_settings.clear()
            original_stream_settings.update(patched_settings)
        else:
            inbound.streamSettings = patched_settings


def _xhttp_extra_payload(xhttp_settings: dict) -> dict:
    extra = _as_mapping(xhttp_settings.get("extra")).copy()
    for target_key, source_keys in (
        ("path", ("path",)),
        ("mode", ("mode",)),
        ("xPaddingHeader", ("xPaddingHeader", "x_padding_header")),
        ("xPaddingKey", ("xPaddingKey", "x_padding_key")),
        ("xPaddingMethod", ("xPaddingMethod", "x_padding_method")),
        ("xPaddingObfsMode", ("xPaddingObfsMode", "x_padding_obfs_mode")),
        ("xPaddingPlacement", ("xPaddingPlacement", "x_padding_placement")),
        ("uplinkHTTPMethod", ("uplinkHTTPMethod", "uplink_http_method")),
    ):
        value = _mapping_first(xhttp_settings, *source_keys, default=_MISSING)
        if value is not _MISSING and target_key not in extra:
            extra[target_key] = value
    headers = _as_mapping(extra.get("headers")).copy()
    if not _normalize_query_value(headers.get("User-Agent")):
        headers["User-Agent"] = DEFAULT_XHTTP_EXTERNAL_PROXY_USER_AGENT
    extra["headers"] = headers
    return extra


def _double_url_encoded_compact_json(value: dict) -> str:
    json_payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return quote(json_payload, safe="")


def _build_xhttp_external_proxy_connection_string(
    inbound: Inbound,
    user_uuid: str,
    remark: str,
) -> str | None:
    if str(_stream_settings_value(inbound, "network", "") or "").strip().lower() != "xhttp":
        return None

    external_proxy = _first_external_proxy(inbound)
    dest = str(_mapping_first(external_proxy, "dest") or "").strip()
    port = _normalize_external_proxy_port(_mapping_first(external_proxy, "port"))
    if not dest or not port:
        return None

    force_tls = str(_mapping_first(external_proxy, "forceTls", "force_tls") or "").strip().lower()
    proxy_security = str(_mapping_first(external_proxy, "security") or "").strip().lower()
    security = (
        "tls"
        if "tls" in {force_tls, proxy_security}
        else (str(_stream_settings_value(inbound, "security", "") or "").strip().lower() or "none")
    )

    configured_xhttp = _as_mapping(_stream_settings_value(inbound, "xhttp_settings", {}))
    clearway = subscription_policy.clearway_client_profile(_safe_inbound_id(inbound), _inbound_label(inbound), dest)
    if clearway:
        settings = copy.deepcopy(configured_xhttp)
        extra = _as_mapping(settings.get("extra"))
        if (
            security != "tls" or port != "443" or not str(settings.get("path") or "").strip()
            or settings.get("mode") != "packet-up"
            or extra.get("uplinkHTTPMethod") != "GET" or extra.get("uplinkDataPlacement") not in {"body", "auto"}
            or any(str(key).startswith(("xPadding", "seq", "session")) for key in extra)
        ):
            raise ValueError("invalid_clearway_client_transport")
        # The origin accepts both legacy body links and header-based uploads.
        # Publish the carrier-compatible client transport independently.
        extra.pop("serverMaxHeaderBytes", None)
        extra.update({"uplinkDataPlacement": "header", "uplinkChunkSize": 4096,
                      "scMaxEachPostBytes": 32768})
        values = {
            "encryption": "none", "security": "tls", "type": "xhttp", "mode": "packet-up",
            "path": settings["path"], "host": dest, "sni": dest, "fp": "chrome",
            "alpn": "h2,http/1.1", "extra": json.dumps(extra, separators=(",", ":")),
        }
        query = urlencode(values, quote_via=quote)
        return f"vless://{user_uuid}@{dest}:443?{query}#{quote(str(remark or '').strip(), safe='')}"

    xhttp_settings = _xhttp_settings_with_external_proxy_defaults(configured_xhttp)
    query_values: dict[str, str] = {
        "encryption": "none",
        "type": "xhttp",
    }
    for key, value in {
        "path": _mapping_first(xhttp_settings, "path"),
        "host": _mapping_first(external_proxy, "host", default=dest) or dest,
        "mode": _mapping_first(xhttp_settings, "mode"),
        "sni": _mapping_first(external_proxy, "sni", default=dest) or dest,
        "fp": _mapping_first(external_proxy, "fp", "fingerprint"),
        "alpn": _xhttp_external_proxy_client_alpn(external_proxy),
    }.items():
        normalized = _normalize_query_value(value)
        if normalized:
            query_values[key] = normalized

    extra = _xhttp_extra_payload(xhttp_settings)
    if extra:
        query_values["extra"] = _double_url_encoded_compact_json(extra)

    query_values["security"] = security
    query_order = ("encryption", "type", "path", "host", "mode", "extra", "security", "sni", "fp", "alpn")
    query = urlencode([(key, query_values[key]) for key in query_order if key in query_values])
    fragment = quote(str(remark or "").strip(), safe="")
    return f"vless://{user_uuid}@{dest}:{port}?{query}#{fragment}"


def _build_ws_external_proxy_connection_string(
    inbound: Inbound,
    user_uuid: str,
    remark: str,
) -> str | None:
    if str(_stream_settings_value(inbound, "network", "") or "").strip().lower() != "ws":
        return None

    external_proxy = _first_external_proxy(inbound)
    dest = str(_mapping_first(external_proxy, "dest") or "").strip()
    port = _normalize_external_proxy_port(_mapping_first(external_proxy, "port"))
    if not dest or not port:
        return None

    force_tls = str(_mapping_first(external_proxy, "forceTls", "force_tls") or "").strip().lower()
    proxy_security = str(_mapping_first(external_proxy, "security") or "").strip().lower()
    security = (
        "tls"
        if "tls" in {force_tls, proxy_security}
        else (str(_stream_settings_value(inbound, "security", "") or "").strip().lower() or "none")
    )

    ws_settings = _as_mapping(_stream_settings_value(inbound, "ws_settings", {}))
    query_values: dict[str, str] = {
        "encryption": "none",
        "type": "ws",
    }
    for key, value in {
        "path": _mapping_first(ws_settings, "path")
        or _mapping_first(external_proxy, "path"),
        "host": _mapping_first(external_proxy, "host", default=dest) or dest,
        "security": security,
        "sni": _mapping_first(external_proxy, "sni", default=dest) or dest,
        "fp": _mapping_first(external_proxy, "fp", "fingerprint"),
        "alpn": _mapping_first(external_proxy, "alpn"),
    }.items():
        normalized = _normalize_query_value(value)
        if normalized:
            query_values[key] = normalized

    query_order = ("encryption", "type", "path", "host", "security", "sni", "fp", "alpn")
    query = urlencode([(key, query_values[key]) for key in query_order if key in query_values])
    fragment = quote(str(remark or "").strip(), safe="")
    return f"vless://{user_uuid}@{dest}:{port}?{query}#{fragment}"


def _build_external_proxy_connection_string(
    inbound: Inbound,
    user_uuid: str,
    remark: str,
) -> str | None:
    network = str(_stream_settings_value(inbound, "network", "") or "").strip().lower()
    if network == "xhttp":
        return _build_xhttp_external_proxy_connection_string(inbound, user_uuid, remark)
    if network == "ws":
        return _build_ws_external_proxy_connection_string(inbound, user_uuid, remark)
    return None


def _xhttp_external_proxy_endpoint(inbound: Inbound) -> tuple[str, int] | None:
    if str(_stream_settings_value(inbound, "network", "") or "").strip().lower() != "xhttp":
        return None
    external_proxy = _first_external_proxy(inbound)
    dest = str(_mapping_first(external_proxy, "dest") or "").strip().lower()
    try:
        port = int(_mapping_first(external_proxy, "port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not dest or port <= 0:
        return None
    return dest, port


def _external_proxy_endpoint(inbound: Inbound) -> tuple[str, int] | None:
    external_proxy = _first_external_proxy(inbound)
    dest = str(_mapping_first(external_proxy, "dest") or "").strip().lower()
    try:
        port = int(_mapping_first(external_proxy, "port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not dest or port <= 0:
        return None
    return dest, port


def _inbound_port(inbound: Inbound) -> int | None:
    try:
        port = int(getattr(inbound, "port", 0) or 0)
    except (TypeError, ValueError):
        return None
    return port if port > 0 else None


def _build_hysteria2_connection_string(
    inbound: Inbound,
    client: Any,
    host_url: str,
    remark: str,
    *,
    node_address_by_id: Mapping[int, str] | None = None,
    node_address_by_name: Mapping[str, str] | None = None,
) -> str | None:
    if not _is_hysteria_subscription_inbound(inbound):
        return None

    auth = str(_client_config_value(client, ("auth", "password"), "") or "").strip()
    if not auth:
        return None

    port = _inbound_port(inbound)
    if not port:
        return None

    tls_settings = _as_mapping(_stream_settings_value(inbound, "tls_settings", {}))
    server_name = str(_mapping_first(tls_settings, "serverName", "server_name") or "").strip()
    server_host = _subscription_endpoint_hostname(
        inbound,
        host_url,
        node_address_by_id=node_address_by_id,
        node_address_by_name=node_address_by_name,
    )
    if not server_host:
        server_host = server_name
    if not server_host:
        return None

    query_values: dict[str, str] = {}
    security = str(_stream_settings_value(inbound, "security", "") or "").strip().lower()
    if security == "tls" or tls_settings:
        query_values["security"] = "tls"
        if server_name:
            query_values["sni"] = server_name

    alpn = _mapping_first(tls_settings, "alpn", default=None)
    if isinstance(alpn, (list, tuple)) and alpn:
        query_values["alpn"] = str(alpn[0] or "").strip()
    elif alpn:
        query_values["alpn"] = str(alpn).strip()

    finalmask = _as_mapping(_stream_settings_value(inbound, "finalmask", {}))
    udp_masks = _as_list(_mapping_first(finalmask, "udp", default=[]))
    if udp_masks:
        salamander_passwords: list[str] = []
        for item in udp_masks:
            mask = _as_mapping(item)
            if str(_mapping_first(mask, "type") or "").strip().lower() != "salamander":
                continue
            mask_settings = _as_mapping(_mapping_first(mask, "settings", default={}))
            password = str(_mapping_first(mask_settings, "password") or "").strip()
            if password:
                salamander_passwords.append(password)
        if len(salamander_passwords) != 1:
            return None
        query_values["obfs"] = "salamander"
        query_values["obfs-password"] = salamander_passwords[0]

    query_order = ("alpn", "obfs", "obfs-password", "security", "sni")
    query = urlencode(
        [(key, normalized) for key in query_order if (normalized := _normalize_query_value(query_values.get(key)))]
    )
    fragment = quote(str(remark or "").strip(), safe="")
    auth_part = quote(auth, safe="")
    suffix = f"?{query}" if query else ""
    return f"hysteria2://{auth_part}@{server_host}:{port}{suffix}#{fragment}"


def _matches_xhttp_external_proxy_link(inbound: Inbound, hostname: str, port: int) -> bool:
    endpoint = _xhttp_external_proxy_endpoint(inbound)
    if endpoint == (hostname.lower(), port):
        return True
    return port == _inbound_port(inbound)


def _matches_external_proxy_link(inbound: Inbound, hostname: str, port: int, query_type: str) -> bool:
    network = str(_stream_settings_value(inbound, "network", "") or "").strip().lower()
    if network != str(query_type or "").strip().lower():
        return False
    endpoint = _external_proxy_endpoint(inbound)
    if endpoint == (str(hostname or "").strip().lower(), int(port)):
        return True
    return port == _inbound_port(inbound)


def _matches_subscription_link_inbound(
    inbound: Inbound,
    hostname: str,
    port: int,
    query_type: str,
) -> bool:
    inbound_network = str(_stream_settings_value(inbound, "network", "tcp") or "").strip().lower()
    if _external_proxy_endpoint(inbound) is not None:
        return _matches_external_proxy_link(inbound, hostname, port, query_type)
    if query_type == "xhttp":
        if inbound_network != "xhttp":
            return False
        if _xhttp_external_proxy_endpoint(inbound) is not None:
            return _matches_xhttp_external_proxy_link(inbound, hostname, port)
        return port == _inbound_port(inbound)
    if inbound_network == "xhttp":
        return False
    return port == _inbound_port(inbound)


def _filter_subscription_links_for_inbounds(links: list[str], inbounds: list[Inbound]) -> list[str]:
    filtered_links: list[str] = []
    for link in links:
        parsed = urlparse(str(link or "").strip())
        if parsed.scheme.lower() != "vless" or not parsed.hostname:
            filtered_links.append(link)
            continue
        try:
            link_port = int(parsed.port or 0)
        except ValueError:
            continue
        query_type = str(parse_qs(parsed.query).get("type", [""])[0] or "").strip().lower()
        if any(
            _matches_subscription_link_inbound(inbound, parsed.hostname, link_port, query_type) for inbound in inbounds
        ):
            filtered_links.append(link)
    return filtered_links


def _subscription_link_remark_for_mode(
    *,
    inbound: Inbound | None,
    current_fragment: str,
    remark_mode: str,
) -> str:
    if remark_mode == "inbound_label" and inbound is not None:
        return _inbound_label(inbound) or unquote(str(current_fragment or "").strip())
    return _friendly_subscription_remark(current_fragment)


def _matched_subscription_inbound(
    inbounds: list[Inbound],
    hostname: str,
    port: int,
    query_type: str,
) -> Inbound | None:
    for inbound in inbounds:
        if _matches_subscription_link_inbound(inbound, hostname, port, query_type):
            return inbound
    return None


def patch_subscription_links_for_inbounds(
    links: list[str],
    inbounds: list[Inbound],
    *,
    node_address_by_id: Mapping[int, str] | None = None,
    node_address_by_name: Mapping[str, str] | None = None,
    remark_mode: str = "friendly",
) -> list[str]:
    external_proxy_inbounds = [inbound for inbound in inbounds if _external_proxy_endpoint(inbound) is not None]

    patched_links: list[str] = []
    for index, link in enumerate(links):
        parsed = urlparse(str(link or "").strip())
        if parsed.scheme.lower() != "vless" or not parsed.hostname:
            patched_links.append(link)
            continue
        indexed_inbound = inbounds[index] if remark_mode == "inbound_label" and index < len(inbounds) else None
        query_type = str(parse_qs(parsed.query).get("type", [""])[0] or "").strip().lower()
        if query_type not in {"ws", "xhttp"}:
            try:
                link_port = int(parsed.port or 0)
            except ValueError:
                patched_links.append(link)
                continue
            matched_inbound = indexed_inbound or _matched_subscription_inbound(
                inbounds, parsed.hostname, link_port, query_type
            )
            patched_link = link
            replacement_hostname = (
                _subscription_endpoint_hostname(
                    matched_inbound,
                    "",
                    node_address_by_id=node_address_by_id,
                    node_address_by_name=node_address_by_name,
                )
                if matched_inbound is not None
                else None
            )
            if replacement_hostname and replacement_hostname.lower() != str(parsed.hostname or "").strip().lower():
                patched_link = _replace_subscription_hostname(link, replacement_hostname)
            patched_links.append(
                _replace_subscription_remark(
                    patched_link,
                    _subscription_link_remark_for_mode(
                        inbound=matched_inbound,
                        current_fragment=parsed.fragment,
                        remark_mode=remark_mode,
                    ),
                )
            )
            continue

        try:
            link_port = int(parsed.port or 0)
        except ValueError:
            patched_links.append(link)
            continue

        replacement = None
        for inbound in external_proxy_inbounds:
            if not _matches_external_proxy_link(inbound, parsed.hostname, link_port, query_type):
                continue
            user_uuid = str(parsed.username or "").strip()
            if not user_uuid:
                break
            remark = (
                _inbound_label(inbound)
                if remark_mode == "inbound_label"
                else _friendly_xhttp_subscription_remark(inbound, parsed.fragment)
            )
            replacement = _build_external_proxy_connection_string(
                inbound,
                user_uuid,
                remark,
            )
            break
        matched_inbound = indexed_inbound or _matched_subscription_inbound(
            inbounds, parsed.hostname, link_port, query_type
        )
        patched_links.append(
            replacement
            or _replace_subscription_remark(
                link,
                _subscription_link_remark_for_mode(
                    inbound=matched_inbound,
                    current_fragment=parsed.fragment,
                    remark_mode=remark_mode,
                ),
            )
        )
    return patched_links


def _subscription_endpoint_mismatches(
    links: list[str],
    inbounds: list[Inbound],
    *,
    node_address_by_id: Mapping[int, str] | None = None,
    node_address_by_name: Mapping[str, str] | None = None,
) -> list[str]:
    links_by_label: dict[str, list[str]] = {}
    for link in links:
        parsed = urlparse(str(link or "").strip())
        label = unquote(str(parsed.fragment or "").strip())
        if label:
            links_by_label.setdefault(label, []).append(str(parsed.hostname or "").strip().lower())

    mismatches: list[str] = []
    for inbound in inbounds:
        if _safe_inbound_node_id(inbound) <= 0:
            continue
        label = _inbound_label(inbound)
        network = str(_stream_settings_value(inbound, "network", "") or "").strip().lower()
        # WS/XHTTP clients connect to externalProxy, while the node address
        # identifies the origin. Match the endpoint used by their renderers.
        proxy_endpoint = _external_proxy_endpoint(inbound) if network in {"ws", "xhttp"} else None
        expected = str(
            proxy_endpoint[0] if proxy_endpoint else _subscription_endpoint_hostname(
                inbound,
                "",
                node_address_by_id=node_address_by_id,
                node_address_by_name=node_address_by_name,
            )
            or ""
        ).strip().lower()
        actual = links_by_label.get(label, [])
        if not expected or len(actual) != 1 or actual[0] != expected:
            mismatches.append(label)
    return mismatches


def get_connection_string(
    inbound: Inbound,
    user_uuid: str,
    host_url: str,
    remark: str,
    *,
    node_address_by_id: Mapping[int, str] | None = None,
    node_address_by_name: Mapping[str, str] | None = None,
    client: Any | None = None,
) -> str | None:
    if not inbound:
        return None

    hysteria_connection_string = _build_hysteria2_connection_string(
        inbound,
        client,
        host_url,
        remark,
        node_address_by_id=node_address_by_id,
        node_address_by_name=node_address_by_name,
    )
    if hysteria_connection_string:
        return hysteria_connection_string

    external_proxy_connection_string = _build_external_proxy_connection_string(inbound, user_uuid, remark)
    if external_proxy_connection_string:
        return external_proxy_connection_string

    settings = inbound.stream_settings.reality_settings.get("settings")
    if not settings:
        return None

    public_key = settings.get("publicKey")
    fp = settings.get("fingerprint")
    server_names = inbound.stream_settings.reality_settings.get("serverNames")
    short_ids = inbound.stream_settings.reality_settings.get("shortIds")
    port = inbound.port

    if not all([public_key, server_names, short_ids]):
        return None

    short_id = short_ids[0]
    endpoint_hostname = _subscription_endpoint_hostname(
        inbound,
        host_url,
        node_address_by_id=node_address_by_id,
        node_address_by_name=node_address_by_name,
    )
    if not endpoint_hostname:
        return None

    query_values: dict[str, str] = {
        "encryption": "none",
        "type": "tcp",
        "security": "reality",
        "pbk": str(public_key),
        "fp": str(fp or ""),
        "sni": str(server_names[0]),
        "sid": str(short_id),
    }
    spider_x = _mapping_first(settings, "spiderX", "spider_x")
    if spider_x:
        query_values["spx"] = str(spider_x)
    flow = "" if _inbound_disallows_vision_flow(inbound) else str(
        _client_config_value(client, ("flow",), "") or ""
    ).strip()
    if flow:
        query_values["flow"] = flow

    query_order = ("encryption", "type", "security", "pbk", "fp", "sni", "sid", "spx", "flow")
    query = urlencode(
        [(key, normalized) for key in query_order if (normalized := _normalize_query_value(query_values.get(key)))]
    )
    fragment = quote(str(remark or "").strip(), safe="")
    return f"vless://{user_uuid}@{endpoint_hostname}:{port}?{query}#{fragment}"


def _normalized_client_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _normalized_client_uuid(value: Any) -> str:
    return str(value or "").strip()


def _record_panel_issue_failure(
    failure_context: MutableMapping[str, str] | None,
    reason: str,
) -> None:
    if failure_context is not None and not failure_context.get("reason"):
        failure_context["reason"] = reason


def _client_email_value(client: Any) -> str:
    return str(_client_config_value(client, ("email",), "") or "").strip()


def _client_uuid_value(client: Any) -> str:
    return str(_client_config_value(client, ("id", "uuid"), "") or "").strip()


def _select_client_indexes_for_upsert(
    clients: list[Any],
    *,
    email: str,
    target_client_uuid: str | None,
) -> tuple[int, list[int]]:
    target_email = _normalized_client_email(email)
    target_uuid = _normalized_client_uuid(target_client_uuid)

    email_matches: list[int] = []
    uuid_matches: list[int] = []
    for index, client in enumerate(clients):
        client_email = _normalized_client_email(_client_email_value(client))
        client_uuid = _normalized_client_uuid(_client_uuid_value(client))
        if target_email and client_email == target_email:
            email_matches.append(index)
        if target_uuid and client_uuid == target_uuid:
            uuid_matches.append(index)

    if uuid_matches:
        canonical_index = uuid_matches[0]
    elif email_matches:
        canonical_index = email_matches[0]
    else:
        return -1, []

    duplicate_indexes = sorted(
        {index for index in [*email_matches, *uuid_matches] if index != canonical_index},
        reverse=True,
    )
    return canonical_index, duplicate_indexes


def _client_identity_conflicts_for_upsert(
    clients: Iterable[Any],
    *,
    email: str,
    target_client_uuid: str | None,
) -> bool:
    target_email = _normalized_client_email(email)
    target_uuid = _normalized_client_uuid(target_client_uuid)
    if not target_email or not target_uuid:
        return False
    for client in clients:
        if _normalized_client_email(_client_email_value(client)) != target_email:
            continue
        candidate_uuid = _normalized_client_uuid(_client_uuid_value(client))
        if candidate_uuid and candidate_uuid != target_uuid:
            return True
    return False


def _remove_duplicate_clients_on_panel(
    api: Api,
    inbound_id: int,
    *,
    clients: list[Any],
    duplicate_indexes: list[int],
) -> bool:
    for duplicate_index in duplicate_indexes:
        duplicate_client = clients[duplicate_index]
        duplicate_email = _client_email_value(duplicate_client)
        duplicate_uuid = _normalized_client_uuid(_client_uuid_value(duplicate_client))
        if not duplicate_email:
            logger.error(
                "Duplicate client without email detected on inbound_id=%s uuid=%s; refusing unsafe upsert.",
                inbound_id,
                duplicate_uuid,
            )
            return False
        if not _detach_client_from_inbound_by_email(api, inbound_id, duplicate_email):
            logger.error(
                "Failed to detach duplicate client inbound_id=%s duplicate_uuid=%s duplicate_email=%s",
                inbound_id,
                duplicate_uuid,
                duplicate_email,
            )
            return False
    return True


def _hysteria_raw_client_value(
    *,
    existing_client: Mapping[str, Any] | None,
    source_client: Any | None,
    key: str,
    aliases: tuple[str, ...] = (),
    default: Any = "",
) -> Any:
    existing = dict(existing_client or {})
    for candidate_key in (key, *aliases):
        value = existing.get(candidate_key)
        if value not in (None, ""):
            return value
    value = _client_config_value(source_client, (key, *aliases), _MISSING)
    if value is not _MISSING and value not in (None, ""):
        return value
    return default


def _build_hysteria_raw_client(
    *,
    inbound_id: int,
    email: str,
    client_uuid: str,
    expiry_ms: int,
    sub_id: str,
    total_gb_bytes: int | None,
    traffic_reset_days: int | None,
    existing_client: Mapping[str, Any] | None,
    source_client: Any | None,
    preserve_existing_enable: bool,
    reset_client_traffic: bool,
) -> dict[str, Any]:
    raw_client = dict(existing_client or {})
    auth = str(
        _hysteria_raw_client_value(
            existing_client=existing_client,
            source_client=source_client,
            key="auth",
            aliases=("password",),
            default="",
        )
        or ""
    ).strip()
    if not auth:
        auth = _generate_hysteria_secret()
    password = str(
        _hysteria_raw_client_value(
            existing_client=existing_client,
            source_client=source_client,
            key="password",
            default=auth,
        )
        or auth
    ).strip()
    if not password:
        password = auth

    raw_client.update(
        {
            "id": client_uuid,
            "email": email,
            "enable": (
                _coerce_panel_bool((existing_client or {}).get("enable"), default=True)
                if existing_client
                and preserve_existing_enable
                and not reset_client_traffic
                else True
            ),
            "expiryTime": int(expiry_ms),
            "subId": sub_id,
            "auth": auth,
            "password": password,
            "security": str(
                _hysteria_raw_client_value(
                    existing_client=existing_client,
                    source_client=source_client,
                    key="security",
                    default="auto",
                )
                or "auto"
            ),
            "inboundId": inbound_id,
            "limitIp": _coerce_panel_int(
                _hysteria_raw_client_value(
                    existing_client=existing_client,
                    source_client=source_client,
                    key="limitIp",
                    aliases=("limit_ip",),
                    default=0,
                )
            ),
            "tgId": _coerce_panel_int(
                _hysteria_raw_client_value(
                    existing_client=existing_client,
                    source_client=source_client,
                    key="tgId",
                    aliases=("tg_id",),
                    default=0,
                )
            ),
            "comment": str(
                _hysteria_raw_client_value(
                    existing_client=existing_client,
                    source_client=source_client,
                    key="comment",
                    default="",
                )
                or ""
            ),
            "reset": _coerce_panel_optional_int(
                _hysteria_raw_client_value(
                    existing_client=existing_client,
                    source_client=source_client,
                    key="reset",
                    default=0,
                )
            )
            or 0,
            "totalGB": max(0, int(total_gb_bytes or 0)),
        }
    )
    raw_client["reset"] = (
        0
        if raw_client["totalGB"] == 0
        else (
            MULTI_NODE_LIMIT_RESET_DAYS
            if traffic_reset_days is None
            else max(0, int(traffic_reset_days))
        )
    )
    return {key: value for key, value in raw_client.items() if value is not None}


def _update_or_create_hysteria_client_on_panel(
    api: Api,
    inbound_id: int,
    email: str,
    days_to_add: int,
    *,
    target_expiry_ms: int | None,
    target_sub_id: str | None,
    target_client_uuid: str | None,
    total_gb_bytes: int | None,
    traffic_reset_days: int | None,
    source_client: Any | None,
    result_metadata: MutableMapping[str, Any] | None,
    reset_client_traffic: bool = False,
    preserve_existing_enable: bool = False,
) -> tuple[str | None, int | None]:
    try:
        raw_inbound = _get_inbound_raw_by_id(api, inbound_id)
        settings = _raw_settings_mapping(raw_inbound.get("settings"))
        raw_clients = [dict(raw_client) for raw_client in settings.get("clients", []) if isinstance(raw_client, dict)]
    except (
        RequestException,
        RuntimeError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        logger.exception(
            "Failed to load raw Hysteria inbound for client upsert: inbound_id=%s email=%s",
            inbound_id,
            email,
        )
        return None, None

    target_uuid = str(target_client_uuid or "").strip()
    if _client_identity_conflicts_for_upsert(
        raw_clients,
        email=email,
        target_client_uuid=target_uuid,
    ):
        logger.error(
            "Refusing Hysteria client upsert after email/UUID identity conflict: "
            "inbound_id=%s email=%s",
            inbound_id,
            email,
        )
        return None, None
    client_index, duplicate_indexes = _select_client_indexes_for_upsert(
        raw_clients,
        email=email,
        target_client_uuid=target_uuid,
    )
    if duplicate_indexes:
        dedupe_ok = _remove_duplicate_clients_on_panel(
            api,
            inbound_id,
            clients=raw_clients,
            duplicate_indexes=duplicate_indexes,
        )
        if not dedupe_ok:
            logger.error(
                "Refusing Hysteria client upsert after duplicate cleanup failure: inbound_id=%s email=%s",
                inbound_id,
                email,
            )
            return None, None
        raw_clients = [
            client
            for index, client in enumerate(raw_clients)
            if index not in set(duplicate_indexes)
        ]
        client_index, _ = _select_client_indexes_for_upsert(
            raw_clients,
            email=email,
            target_client_uuid=target_uuid,
        )
    existing_client = raw_clients[client_index] if client_index != -1 else None
    existing_email = str((existing_client or {}).get("email") or "").strip()
    client_uuid = target_uuid or str(
        (existing_client or {}).get("id") or (existing_client or {}).get("uuid") or ""
    ).strip()
    if not client_uuid:
        client_uuid = str(uuid.uuid4())

    if target_expiry_ms is not None:
        new_expiry_ms = int(target_expiry_ms)
    else:
        existing_expiry_ms = _coerce_panel_int(
            (existing_client or {}).get("expiryTime", (existing_client or {}).get("expiry_time"))
        )
        now_ms = int(time.time() * 1000)
        if existing_client and existing_expiry_ms > now_ms:
            new_expiry_ms = existing_expiry_ms + int(timedelta(days=days_to_add).total_seconds() * 1000)
        else:
            new_expiry_ms = now_ms + int(timedelta(days=days_to_add).total_seconds() * 1000)

    sub_id = str(
        target_sub_id
        or (existing_client or {}).get("subId")
        or (existing_client or {}).get("sub_id")
        or ""
    ).strip()
    if not sub_id:
        sub_id = _generate_sub_id()
    if result_metadata is not None:
        result_metadata["sub_id"] = sub_id

    reset_was_requested = bool(reset_client_traffic)
    if (
        existing_client
        and reset_was_requested
        and int(total_gb_bytes or 0) > 0
    ):
        if not _reset_client_traffic_on_panel(
            api,
            email=existing_email or email,
        ):
            return None, None
        # The reset is intentionally completed before moving the deadline. If
        # the following update fails, the still-overdue deadline makes the next
        # retry safe and deterministic.
        reset_client_traffic = False

    raw_client = _build_hysteria_raw_client(
        inbound_id=inbound_id,
        email=email,
        client_uuid=client_uuid,
        expiry_ms=new_expiry_ms,
        sub_id=sub_id,
        total_gb_bytes=total_gb_bytes,
        traffic_reset_days=traffic_reset_days,
        existing_client=existing_client,
        source_client=source_client,
        preserve_existing_enable=preserve_existing_enable,
        reset_client_traffic=reset_was_requested,
    )
    try:
        if client_index != -1:
            clients_payload = _api_post_client_update(api, existing_email or email, inbound_id, raw_client)
            action = "Updated"
        else:
            clients_payload = _api_post_client_add(api, inbound_id, raw_client)
            action = "Added"
        if not _api_payload_success(clients_payload):
            logger.warning(
                "clients.%s rejected for Hysteria inbound_id=%s email=%s payload=%s.",
                "update" if client_index != -1 else "add",
                inbound_id,
                email,
                clients_payload,
            )
            return None, None
        logger.info(
            "%s Hysteria client via clients API for inbound_id=%s email=%s.",
            action,
            inbound_id,
            email,
        )
        return _finish_client_upsert(
            api,
            email=email,
            client_uuid=client_uuid,
            expiry_ms=new_expiry_ms,
            total_gb_bytes=total_gb_bytes,
            reset_client_traffic=reset_client_traffic,
        )
    except (
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ) as clients_api_error:
        if _response_status_code(clients_api_error) != 404:
            logger.exception(
                "clients API failed for Hysteria inbound_id=%s email=%s; refusing raw inbound.update fallback.",
                inbound_id,
                email,
            )
            return None, None
        logger.error(
            "Client-only Hysteria endpoint is unavailable for inbound_id=%s; "
            "normal issuance refuses full inbound replacement. "
            "Upgrade the panel or use a separately reviewed repair procedure.",
            inbound_id,
        )
        return None, None


@dataclass(frozen=True)
class SubscriptionClientIdentity:
    inbound_id: int
    email: str
    client_uuid: str
    sub_id: str
    existing: bool
    snapshot: Mapping[str, Any]


@dataclass(frozen=True)
class SubscriptionIdentityPlan:
    client_uuid: str
    primary_sub_id: str
    bindings: Mapping[int, SubscriptionClientIdentity]


def _plan_subscription_client_identities(
    api: Api,
    active_inbounds: list[Inbound],
    *,
    all_inbounds: list[Inbound],
    base_email: str,
    target_client_uuid: str | None,
    target_sub_id: str | None,
    stored_key: Mapping[str, Any],
    host_name: str,
) -> SubscriptionIdentityPlan:
    """Read every binding before writes; ordinary issuance never migrates identities.

    A public subscription has one stable logical ID, while centralized 3x-ui
    rows use distinct component IDs. Existing IDs are preserved. Missing or
    ambiguous evidence requires an explicit repair instead of a guessed upsert.
    """
    identity_inputs = (base_email, target_client_uuid, target_sub_id,
                       stored_key.get("xui_client_uuid"), stored_key.get("panel_sub_id"))
    if any(value is not None and not isinstance(value, str) for value in identity_inputs):
        raise ValueError("subscription_identity_invalid_request")
    requested_email = _normalized_client_email(base_email)
    if not requested_email or not active_inbounds:
        raise ValueError("subscription_identity_invalid_request")
    primary_id = _safe_inbound_id(active_inbounds[0])
    expected_emails = {
        _safe_inbound_id(inbound): (
            str(base_email).strip() if index == 0 else
            _secondary_inbound_email(base_email, inbound, host_name=host_name)
        )
        for index, inbound in enumerate(active_inbounds)
    }
    if (
        primary_id <= 0 or len(expected_emails) != len(active_inbounds)
        or len({_normalized_client_email(value) for value in expected_emails.values()}) != len(expected_emails)
    ):
        raise ValueError("subscription_identity_invalid_request")

    scan_ids = {_safe_inbound_id(inbound) for inbound in all_inbounds} | set(expected_emails)
    if any(inbound_id <= 0 for inbound_id in scan_ids):
        raise ValueError("subscription_identity_invalid_request")
    snapshots: dict[int, Mapping[str, Any]] = {}
    rows_by_id: dict[int, list[Mapping[str, Any]]] = {}
    for inbound_id in sorted(scan_ids):
        try:
            raw = _get_inbound_raw_by_id(api, inbound_id, allow_list_fallback=False)
        except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError("subscription_identity_unavailable") from exc
        if not isinstance(raw, Mapping) or _coerce_panel_int(raw.get("id")) != inbound_id:
            raise ValueError("subscription_identity_unavailable")
        settings = raw.get("settings")
        try:
            settings = json.loads(settings) if isinstance(settings, str) else settings
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("subscription_identity_malformed") from exc
        if not isinstance(settings, Mapping) or not isinstance(settings.get("clients"), list):
            raise ValueError("subscription_identity_malformed")
        rows = settings["clients"]
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("email"), str):
                raise ValueError("subscription_identity_malformed")
            uuid_aliases = [row[field] for field in ("id", "uuid") if row.get(field) is not None]
            sub_aliases = [row[field] for field in ("subId", "sub_id", "subid") if row.get(field) is not None]
            if (not _normalized_client_email(row["email"]) or not uuid_aliases
                    or any(not isinstance(value, str) for value in [*uuid_aliases, *sub_aliases])
                    or len({value.strip() for value in uuid_aliases}) != 1
                    or not uuid_aliases[0].strip()
                    or len({value.strip() for value in sub_aliases}) > 1):
                raise ValueError("subscription_identity_malformed")
        snapshots[inbound_id] = copy.deepcopy(raw)
        rows_by_id[inbound_id] = copy.deepcopy(rows)

    requested_uuid = _normalized_client_uuid(target_client_uuid)
    stored_uuid = _normalized_client_uuid(stored_key.get("xui_client_uuid"))
    requested_sub_id = str(target_sub_id or "").strip()
    stored_sub_id = str(stored_key.get("panel_sub_id") or "").strip()
    if ((requested_uuid and stored_uuid and requested_uuid != stored_uuid)
            or (requested_sub_id and stored_sub_id and requested_sub_id != stored_sub_id)):
        raise ValueError("subscription_identity_conflict")
    requested_uuid = requested_uuid or stored_uuid
    requested_sub_id = requested_sub_id or stored_sub_id
    email_matches = {
        inbound_id: [row for row in rows_by_id[inbound_id]
                     if _normalized_client_email(row.get("email")) == _normalized_client_email(email)]
        for inbound_id, email in expected_emails.items()
    }
    if any(len(matches) > 1 for matches in email_matches.values()):
        raise ValueError("subscription_identity_ambiguous")
    existing_uuids = {
        _normalized_client_uuid(row.get("uuid") or row.get("id"))
        for matches in email_matches.values() for row in matches
    }
    if len(existing_uuids) > 1 or (requested_uuid and existing_uuids and existing_uuids != {requested_uuid}):
        raise ValueError("subscription_identity_conflict")
    client_uuid = requested_uuid or next(iter(existing_uuids), "") or str(uuid.uuid4())
    existing: dict[int, Mapping[str, Any] | None] = {}
    for inbound_id, email in expected_emails.items():
        related = [row for row in rows_by_id[inbound_id]
                   if (_normalized_client_email(row.get("email")) == _normalized_client_email(email)
                       or _normalized_client_uuid(row.get("uuid") or row.get("id")) == client_uuid)]
        if len(related) > 1:
            raise ValueError("subscription_identity_ambiguous")
        row = related[0] if related else None
        if row is not None and (
            _normalized_client_email(row.get("email")) != _normalized_client_email(email)
            or _normalized_client_uuid(row.get("uuid") or row.get("id")) != client_uuid
        ):
            raise ValueError("subscription_identity_repair_required")
        if row is not None and not str(_extract_client_sub_id(row) or "").strip():
            raise ValueError("subscription_identity_missing_sub_id")
        existing[inbound_id] = row

    primary_row = existing[primary_id]
    observed_public_id = str(_extract_client_sub_id(primary_row) or "").strip() if primary_row else ""
    if requested_sub_id and observed_public_id and requested_sub_id != observed_public_id:
        raise ValueError("subscription_identity_conflict")
    if primary_row is None and any(existing.values()) and not requested_sub_id:
        raise ValueError("subscription_identity_public_id_unavailable")
    public_sub_id = observed_public_id or requested_sub_id
    used_sub_ids = {str(_extract_client_sub_id(row) or "").strip()
                    for rows in rows_by_id.values() for row in rows}
    used_sub_ids.discard("")

    def new_sub_id() -> str:
        for _ in range(8):
            value = str(_generate_sub_id() or "").strip()
            if value and value not in used_sub_ids:
                used_sub_ids.add(value)
                return value
        raise ValueError("subscription_identity_generation_failed")

    if not public_sub_id:
        public_sub_id = new_sub_id()
    used_sub_ids.add(public_sub_id)
    bindings: dict[int, SubscriptionClientIdentity] = {}
    planned_sub_ids: set[str] = set()
    for inbound_id, email in expected_emails.items():
        row = existing[inbound_id]
        sub_id = (public_sub_id if inbound_id == primary_id else
                  str(_extract_client_sub_id(row) or "").strip() if row is not None else new_sub_id())
        if sub_id in planned_sub_ids:
            raise ValueError("subscription_identity_repair_required")
        planned_sub_ids.add(sub_id)
        for other_id, rows in rows_by_id.items():
            for other in rows:
                if str(_extract_client_sub_id(other) or "").strip() != sub_id:
                    continue
                if not (other_id == inbound_id and other is row):
                    raise ValueError("subscription_identity_repair_required")
        bindings[inbound_id] = SubscriptionClientIdentity(
            inbound_id, email, client_uuid, sub_id, row is not None, snapshots[inbound_id],
        )
    # An ordinary-only request must not silently detach a previously entitled
    # binding or delete an alias. Such topology changes require explicit repair.
    for inbound_id in scan_ids - set(expected_emails):
        for row in rows_by_id[inbound_id]:
            if (_normalized_client_uuid(row.get("uuid") or row.get("id")) == client_uuid
                    or _normalized_client_email(row.get("email")) == requested_email
                    or str(_extract_client_sub_id(row) or "").strip() in planned_sub_ids):
                raise ValueError("subscription_identity_repair_required")
    return SubscriptionIdentityPlan(client_uuid, public_sub_id, bindings)


def _reconcile_subscription_clients_before_upsert(
    api: Api,
    active_subscription_inbounds: list[Inbound],
    *,
    base_email: str,
    target_client_uuid: str | None,
    target_sub_id: str | None,
    raw_inbounds: list[dict[str, Any]] | None,
    host_name: str = "",
) -> bool:
    """Detach legacy secondary rows that would make 3x-ui reject primary update as duplicate subId."""
    if len(active_subscription_inbounds) <= 1:
        return True

    base_email_value = str(base_email or "").strip()
    base_email_norm = _normalized_client_email(base_email_value)
    target_uuid = _normalized_client_uuid(target_client_uuid)
    primary_sub_id = str(target_sub_id or "").strip()
    if not (base_email_norm or target_uuid or primary_sub_id):
        return True

    raw_inbound_by_id = {
        _coerce_panel_int(raw_inbound.get("id")): raw_inbound
        for raw_inbound in (raw_inbounds or [])
        if _coerce_panel_int(raw_inbound.get("id")) > 0
    }

    def _raw_inbound_with_clients(inbound_id: int) -> Mapping[str, Any] | None:
        raw_inbound = raw_inbound_by_id.get(inbound_id)
        if raw_inbound and raw_inbound.get("settings") is not None:
            return raw_inbound
        try:
            return _get_inbound_raw_by_id(api, inbound_id)
        except (
            RequestException,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ) as exc:
            logger.warning(
                "Subscription preflight skipped inbound_id=%s for email=%s: %s",
                inbound_id,
                base_email_value,
                exc,
            )
            return None

    def _iter_raw_clients(raw_inbound: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        if not raw_inbound:
            return []
        try:
            return [dict(client) for client in _iter_client_dicts_from_settings(raw_inbound.get("settings"))]
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Subscription preflight could not parse inbound clients for email=%s: %s",
                base_email_value,
                exc,
            )
            return []

    primary_inbound_id = _safe_inbound_id(active_subscription_inbounds[0])
    primary_raw_clients = _iter_raw_clients(_raw_inbound_with_clients(primary_inbound_id))
    for raw_client in primary_raw_clients:
        raw_email = _normalized_client_email(raw_client.get("email"))
        raw_uuid = _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid"))
        raw_sub_id = str(_extract_client_sub_id(raw_client) or "").strip()
        if (
            (base_email_norm and raw_email == base_email_norm)
            or (target_uuid and raw_uuid == target_uuid)
            or (primary_sub_id and raw_sub_id == primary_sub_id)
        ):
            if not target_uuid and raw_uuid:
                target_uuid = raw_uuid
            if not primary_sub_id and raw_sub_id:
                primary_sub_id = raw_sub_id
            break

    if not (target_uuid or primary_sub_id):
        return True

    detached_count = 0
    deleted_detached_count = 0
    for inbound in active_subscription_inbounds[1:]:
        inbound_id = _safe_inbound_id(inbound)
        if inbound_id <= 0:
            continue
        expected_email = _secondary_inbound_email(base_email_value, inbound, host_name=host_name)
        expected_email_norm = _normalized_client_email(expected_email)
        for raw_client in _iter_raw_clients(_raw_inbound_with_clients(inbound_id)):
            raw_email_value = str(raw_client.get("email") or "").strip()
            raw_email = _normalized_client_email(raw_email_value)
            raw_uuid = _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid"))
            raw_sub_id = str(_extract_client_sub_id(raw_client) or "").strip()
            related = (
                (base_email_norm and raw_email == base_email_norm)
                or (expected_email_norm and raw_email == expected_email_norm)
                or (target_uuid and raw_uuid == target_uuid)
                or (primary_sub_id and raw_sub_id == primary_sub_id)
            )
            if not related:
                continue
            expected_secondary_ok = raw_email == expected_email_norm and not (
                primary_sub_id and raw_sub_id == primary_sub_id
            )
            if expected_secondary_ok:
                continue
            if not raw_email_value:
                logger.error(
                    "Subscription preflight found related client without email inbound_id=%s uuid=%s sub_id=%s.",
                    inbound_id,
                    raw_uuid,
                    raw_sub_id,
                )
                return False
            if not _detach_client_from_inbound_by_email(api, inbound_id, raw_email_value):
                logger.error(
                    "Subscription preflight failed to detach legacy client inbound_id=%s email=%s uuid=%s sub_id=%s.",
                    inbound_id,
                    raw_email_value,
                    raw_uuid,
                    raw_sub_id,
                )
                return False
            detached_count += 1
            should_delete_detached_client = raw_email != base_email_norm and (
                (target_uuid and raw_uuid == target_uuid)
                or (primary_sub_id and raw_sub_id == primary_sub_id)
            )
            if should_delete_detached_client:
                if not _delete_detached_client_by_email(api, raw_email_value):
                    logger.error(
                        "Subscription preflight failed to delete detached legacy client email=%s uuid=%s sub_id=%s.",
                        raw_email_value,
                        raw_uuid,
                        raw_sub_id,
                    )
                    return False
                deleted_detached_count += 1

    if detached_count:
        logger.warning(
            "Subscription preflight detached %s legacy secondary client(s), deleted %s detached row(s) "
            "for email=%s uuid=%s sub_id=%s.",
            detached_count,
            deleted_detached_count,
            base_email_value,
            target_uuid,
            primary_sub_id,
        )
    return True


def update_or_create_client_on_panel(
    api: Api,
    inbound_id: int,
    email: str,
    days_to_add: int,
    *,
    target_expiry_ms: int | None = None,
    target_sub_id: str | None = None,
    target_client_uuid: str | None = None,
    total_gb_bytes: int | None = None,
    traffic_reset_days: int | None = None,
    reset_client_traffic: bool = False,
    preserve_existing_enable: bool = False,
    source_client: Any | None = None,
    result_metadata: MutableMapping[str, Any] | None = None,
) -> tuple[str | None, int | None]:
    try:
        inbound_to_modify, _ = _load_inbound_for_client_upsert(
            api,
            inbound_id,
            email=email,
            target_client_uuid=target_client_uuid,
        )
        if not inbound_to_modify:
            logger.error("Could not find inbound for client upsert: inbound_id=%s", inbound_id)
            return None, None

        if _is_hysteria_subscription_inbound(inbound_to_modify):
            return _update_or_create_hysteria_client_on_panel(
                api,
                inbound_id,
                email,
                days_to_add,
                target_expiry_ms=target_expiry_ms,
                target_sub_id=target_sub_id,
                target_client_uuid=target_client_uuid,
                total_gb_bytes=total_gb_bytes,
                traffic_reset_days=traffic_reset_days,
                source_client=source_client,
                result_metadata=result_metadata,
                reset_client_traffic=reset_client_traffic,
                preserve_existing_enable=preserve_existing_enable,
            )

        if inbound_to_modify.settings.clients is None:
            inbound_to_modify.settings.clients = []

        if _client_identity_conflicts_for_upsert(
            inbound_to_modify.settings.clients,
            email=email,
            target_client_uuid=target_client_uuid,
        ):
            logger.error(
                "Refusing client upsert after email/UUID identity conflict: "
                "inbound_id=%s email=%s",
                inbound_id,
                email,
            )
            return None, None

        client_index, duplicate_indexes = _select_client_indexes_for_upsert(
            inbound_to_modify.settings.clients,
            email=email,
            target_client_uuid=target_client_uuid,
        )
        if duplicate_indexes:
            dedupe_ok = _remove_duplicate_clients_on_panel(
                api,
                inbound_id,
                clients=inbound_to_modify.settings.clients,
                duplicate_indexes=duplicate_indexes,
            )
            if not dedupe_ok:
                logger.error(
                    "Refusing client upsert after duplicate cleanup failure: inbound_id=%s email=%s",
                    inbound_id,
                    email,
                )
                return None, None
            duplicate_index_set = set(duplicate_indexes)
            inbound_to_modify.settings.clients = [
                client
                for index, client in enumerate(inbound_to_modify.settings.clients)
                if index not in duplicate_index_set
            ]
            client_index, _ = _select_client_indexes_for_upsert(
                inbound_to_modify.settings.clients,
                email=email,
                target_client_uuid=target_client_uuid,
            )

        if target_expiry_ms is not None:
            new_expiry_ms = int(target_expiry_ms)
        else:
            if client_index != -1:
                existing_client = inbound_to_modify.settings.clients[client_index]
                if existing_client.expiry_time > int(datetime.now().timestamp() * 1000):
                    current_expiry_dt = datetime.fromtimestamp(existing_client.expiry_time / 1000)
                    new_expiry_dt = current_expiry_dt + timedelta(days=days_to_add)
                else:
                    new_expiry_dt = datetime.now() + timedelta(days=days_to_add)
            else:
                new_expiry_dt = datetime.now() + timedelta(days=days_to_add)

            new_expiry_ms = int(new_expiry_dt.timestamp() * 1000)

        reset_was_requested = bool(reset_client_traffic)
        if (
            client_index != -1
            and reset_was_requested
            and int(total_gb_bytes or 0) > 0
        ):
            existing_email_for_reset = str(
                getattr(inbound_to_modify.settings.clients[client_index], "email", "")
                or email
            ).strip()
            if not _reset_client_traffic_on_panel(
                api,
                email=existing_email_for_reset,
            ):
                return None, None
            # Reset first, then advance the deadline.  If the update fails the
            # old deadline stays overdue, so a later retry cannot silently skip
            # the traffic reset.
            reset_client_traffic = False

        if client_index != -1:
            existing_client = inbound_to_modify.settings.clients[client_index]
            existing_email = str(getattr(existing_client, "email", "") or email or "").strip()
            # Keep traffic reset policy untouched; update actual access expiry.
            existing_client.expiry_time = new_expiry_ms
            existing_client.enable = (
                bool(getattr(existing_client, "enable", False))
                if preserve_existing_enable and not reset_was_requested
                else True
            )
            existing_client.email = email
            existing_client.inbound_id = inbound_id
            _copy_client_config_fields(existing_client, source_client)
            existing_client.flow = _client_flow_for_inbound(inbound_to_modify, source_client)
            _set_client_total_gb(
                existing_client,
                total_gb_bytes,
                traffic_reset_days=traffic_reset_days,
            )
            update_client_uuid = str(existing_client.id or "").strip()
            client_uuid = str(target_client_uuid or update_client_uuid or "").strip()
            if not client_uuid:
                client_uuid = str(uuid.uuid4())
            if not update_client_uuid:
                update_client_uuid = client_uuid
            existing_client.id = client_uuid
            client_sub_id = _ensure_client_sub_id(existing_client, target_sub_id)
            if result_metadata is not None:
                result_metadata["sub_id"] = client_sub_id
            try:
                # 3x-ui toggles client availability via updateClient endpoint.
                # Using this endpoint prevents "enabled in config but still red" state.
                api.client.update(update_client_uuid, existing_client)
            except (
                RequestException,
                RuntimeError,
                TimeoutError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
            ) as client_update_error:
                if _response_status_code(client_update_error) != 404:
                    logger.exception(
                        "updateClient failed for inbound_id=%s email=%s; refusing raw inbound.update fallback.",
                        inbound_id,
                        email,
                    )
                    return None, None
                try:
                    clients_payload = _api_post_client_update(
                        api,
                        existing_email or email,
                        inbound_id,
                        existing_client,
                    )
                    if not _api_payload_success(clients_payload):
                        logger.warning(
                            "clients.update rejected for inbound_id=%s email=%s payload=%s.",
                            inbound_id,
                            email,
                            clients_payload,
                        )
                        return None, None
                    logger.info(
                        "Updated client via clients API for inbound_id=%s email=%s.",
                        inbound_id,
                        email,
                    )
                except (
                    RequestException,
                    RuntimeError,
                    TimeoutError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                ) as clients_update_error:
                    if _response_status_code(clients_update_error) != 404:
                        if _is_duplicate_email_error(clients_update_error):
                            recovered_uuid, recovered_expiry_ms = _recover_secondary_client_update_via_clients_api(
                                api,
                                inbound_to_modify,
                                email,
                                client_uuid=client_uuid,
                                target_expiry_ms=new_expiry_ms,
                                target_sub_id=client_sub_id,
                                total_gb_bytes=total_gb_bytes,
                                source_client=existing_client,
                                preserve_existing_enable=preserve_existing_enable,
                                traffic_reset_days=traffic_reset_days,
                                reset_client_traffic=reset_client_traffic,
                            )
                            if recovered_uuid == client_uuid and recovered_expiry_ms == new_expiry_ms:
                                logger.warning(
                                    "Recovered duplicate-email client update via attach/read-back: "
                                    "inbound_id=%s email=%s",
                                    inbound_id,
                                    email,
                                )
                                return _finish_client_upsert(
                                    api,
                                    email=email,
                                    client_uuid=client_uuid,
                                    expiry_ms=new_expiry_ms,
                                    total_gb_bytes=total_gb_bytes,
                                    reset_client_traffic=reset_client_traffic,
                                )
                        logger.exception(
                            "clients.update failed for inbound_id=%s email=%s; refusing raw inbound.update fallback.",
                            inbound_id,
                            email,
                        )
                        return None, None
                    logger.error(
                        "Client-only update endpoints are unavailable for inbound_id=%s; "
                        "normal issuance refuses full inbound replacement. "
                        "Upgrade the panel or use a separately reviewed repair procedure.",
                        inbound_id,
                    )
                    return None, None
                else:
                    return _finish_client_upsert(
                        api,
                        email=email,
                        client_uuid=client_uuid,
                        expiry_ms=new_expiry_ms,
                        total_gb_bytes=total_gb_bytes,
                        reset_client_traffic=reset_client_traffic,
                    )
        else:
            client_uuid = str(target_client_uuid or "").strip() or str(uuid.uuid4())
            new_client = Client(
                id=client_uuid,
                email=email,
                enable=True,
                password=_client_config_value(source_client, ("password",), ""),
                flow=_client_flow_for_inbound(inbound_to_modify, source_client),
                method=_client_config_value(source_client, ("method",), ""),
                limitIp=_client_config_value(source_client, ("limit_ip", "limitIp"), 0),
                reset=_client_config_value(source_client, ("reset",), None),
                comment=_client_config_value(source_client, ("comment",), ""),
                tgId=_coerce_panel_int(_client_config_value(source_client, ("tg_id", "tgId"), 0)),
                expiry_time=new_expiry_ms,
                sub_id=str(target_sub_id or "").strip() or _generate_sub_id(),
                totalGB=max(0, int(total_gb_bytes or 0)),
            )
            comment_value = _client_config_value(source_client, ("comment",), "")
            if comment_value and not hasattr(new_client, "comment"):
                # py3xui 0.4.0 does not model comment, but some panels accept it.
                object.__setattr__(new_client, "comment", comment_value)
            new_client.inbound_id = inbound_id
            client_sub_id = _ensure_client_sub_id(new_client, target_sub_id)
            if result_metadata is not None:
                result_metadata["sub_id"] = client_sub_id
            _set_client_total_gb(
                new_client,
                total_gb_bytes,
                traffic_reset_days=traffic_reset_days,
            )
            try:
                api.client.add(inbound_id, [new_client])
            except (
                RequestException,
                RuntimeError,
                TimeoutError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
            ) as client_add_error:
                if _response_status_code(client_add_error) != 404:
                    logger.exception(
                        "addClient failed for inbound_id=%s email=%s; refusing raw inbound.update fallback.",
                        inbound_id,
                        email,
                    )
                    return None, None
                try:
                    clients_payload = _api_post_client_add(api, inbound_id, new_client)
                    if not _api_payload_success(clients_payload):
                        logger.warning(
                            "clients.add rejected for inbound_id=%s email=%s payload=%s.",
                            inbound_id,
                            email,
                            clients_payload,
                        )
                        return None, None
                    logger.info(
                        "Added client via clients API for inbound_id=%s email=%s.",
                        inbound_id,
                        email,
                    )
                except (
                    RequestException,
                    RuntimeError,
                    TimeoutError,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                ) as clients_add_error:
                    if _response_status_code(clients_add_error) != 404:
                        if _is_duplicate_email_error(clients_add_error):
                            recovered_uuid, recovered_expiry_ms = _recover_secondary_client_update_via_clients_api(
                                api,
                                inbound_to_modify,
                                email,
                                client_uuid=client_uuid,
                                target_expiry_ms=new_expiry_ms,
                                target_sub_id=client_sub_id,
                                total_gb_bytes=total_gb_bytes,
                                source_client=source_client,
                                preserve_existing_enable=preserve_existing_enable,
                                traffic_reset_days=traffic_reset_days,
                                reset_client_traffic=reset_client_traffic,
                            )
                            if recovered_uuid == client_uuid and recovered_expiry_ms == new_expiry_ms:
                                logger.warning(
                                    "Recovered orphan duplicate-email client via attach/update: "
                                    "inbound_id=%s email=%s",
                                    inbound_id,
                                    email,
                                )
                                return _finish_client_upsert(
                                    api,
                                    email=email,
                                    client_uuid=client_uuid,
                                    expiry_ms=new_expiry_ms,
                                    total_gb_bytes=total_gb_bytes,
                                    reset_client_traffic=reset_client_traffic,
                                )
                        logger.exception(
                            "clients.add failed for inbound_id=%s email=%s; refusing raw inbound.update fallback.",
                            inbound_id,
                            email,
                        )
                        return None, None
                    logger.error(
                        "Client-only add endpoints are unavailable for inbound_id=%s; "
                        "normal issuance refuses full inbound replacement. "
                        "Upgrade the panel or use a separately reviewed repair procedure.",
                        inbound_id,
                    )
                    return None, None
                else:
                    return _finish_client_upsert(
                        api,
                        email=email,
                        client_uuid=client_uuid,
                        expiry_ms=new_expiry_ms,
                        total_gb_bytes=total_gb_bytes,
                        reset_client_traffic=reset_client_traffic,
                    )

        return _finish_client_upsert(
            api,
            email=email,
            client_uuid=client_uuid,
            expiry_ms=new_expiry_ms,
            total_gb_bytes=total_gb_bytes,
            reset_client_traffic=reset_client_traffic,
        )

    except Exception as e:
        logger.error(f"Error in update_or_create_client_on_panel: {e}", exc_info=True)
        return None, None



def _recover_secondary_client_update_via_clients_api(
    api: Api,
    node_inbound: Inbound,
    email: str,
    *,
    client_uuid: str,
    target_expiry_ms: int,
    target_sub_id: str | None,
    total_gb_bytes: int | None,
    source_client: Any | None,
    preserve_existing_enable: bool = False,
    traffic_reset_days: int | None = None,
    reset_client_traffic: bool = False,
) -> tuple[str | None, int | None]:
    """Recover existing/detached secondary clients without full inbound.update.

    Large MainPanel secondary inbounds can reject raw inbound.update with 413, and
    detached clients can make addClient fail with "email already in use".  The
    clients API updates by email and preserves the node-local subId, which is
    enough because managed payload fallback matches by uuid/base email.
    """
    inbound_id = _safe_inbound_id(node_inbound)
    if inbound_id <= 0:
        return None, None

    expected_uuid = _normalized_client_uuid(client_uuid)
    expected_sub_id = str(target_sub_id or "").strip()
    if not expected_uuid:
        return None, None

    def _central_client_by_email() -> tuple[dict[str, Any] | None, bool]:
        try:
            payload = _api_get_json(api, "panel/api/clients/list", skip_check=True)
        except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning(
                "Orphan recovery could not list centralized clients for inbound_id=%s email=%s: %s",
                inbound_id,
                email,
                exc,
            )
            return None, False
        clients = _api_extract_obj(payload)
        if not isinstance(clients, list):
            return None, False
        normalized_email = _normalized_client_email(email)
        matches = [
            dict(candidate)
            for candidate in clients
            if isinstance(candidate, Mapping)
            and _normalized_client_email(candidate.get("email")) == normalized_email
        ]
        if len(matches) > 1:
            return None, True
        return (matches[0] if matches else None), False

    def _central_identity_matches(central_client: Mapping[str, Any]) -> bool:
        central_uuid = _normalized_client_uuid(central_client.get("uuid"))
        central_sub_id = str(central_client.get("subId") or central_client.get("sub_id") or "").strip()
        central_inbound_ids = {
            _coerce_panel_int(item)
            for item in (central_client.get("inboundIds") or central_client.get("inbound_ids") or [])
            if _coerce_panel_int(item) > 0
        }
        if central_uuid != expected_uuid:
            logger.error(
                "Orphan recovery refused UUID collision for inbound_id=%s email=%s",
                inbound_id,
                email,
            )
            return False
        if (
            expected_sub_id
            and central_sub_id != expected_sub_id
            and (not central_sub_id or central_inbound_ids)
        ):
            logger.error(
                "Orphan recovery refused subId collision for inbound_id=%s email=%s",
                inbound_id,
                email,
            )
            return False
        return True

    def _recreate_verified_orphan(central_client: Mapping[str, Any]) -> bool:
        central_sub_id = str(central_client.get("subId") or central_client.get("sub_id") or "").strip()
        inbound_ids = {
            _coerce_panel_int(item)
            for item in (central_client.get("inboundIds") or central_client.get("inbound_ids") or [])
            if _coerce_panel_int(item) > 0
        }
        if inbound_ids:
            logger.warning(
                "Orphan recovery refused to recreate bound centralized client inbound_id=%s email=%s bindings=%s",
                inbound_id,
                email,
                sorted(inbound_ids),
            )
            return False

        # A detached secondary/WHITELIST client intentionally has its own
        # subscription identity.  When email+UUID are exact and it has no
        # bindings, preserve that identity instead of replacing it with the
        # primary client's subId.
        preserved_sub_id = central_sub_id or expected_sub_id
        runtime_inbound = node_inbound
        if _is_hysteria_subscription_inbound(runtime_inbound):
            recreated_client = _build_hysteria_raw_client(
                inbound_id=inbound_id,
                email=email,
                client_uuid=expected_uuid,
                expiry_ms=int(target_expiry_ms),
                sub_id=preserved_sub_id or _generate_sub_id(),
                total_gb_bytes=total_gb_bytes,
                traffic_reset_days=traffic_reset_days,
                existing_client=central_client,
                source_client=source_client,
                preserve_existing_enable=preserve_existing_enable,
                reset_client_traffic=False,
            )
        else:
            recreated_client = {
                "id": expected_uuid,
                "email": email,
                "enable": (
                    bool(central_client.get("enable", True))
                    if preserve_existing_enable
                    else True
                ),
                "expiryTime": int(target_expiry_ms),
                "subId": preserved_sub_id or _generate_sub_id(),
                "totalGB": max(0, int(total_gb_bytes or 0)),
                "limitIp": _coerce_panel_int(central_client.get("limitIp", central_client.get("limit_ip", 0))),
                "flow": _client_flow_for_inbound(runtime_inbound, source_client),
                "tgId": _coerce_panel_int(central_client.get("tgId", central_client.get("tg_id", 0))),
                "comment": str(central_client.get("comment") or ""),
                "reset": (
                    0
                    if int(total_gb_bytes or 0) == 0
                    else (
                        _coerce_panel_int(central_client.get("reset", 0))
                        if traffic_reset_days is None
                        else max(0, int(traffic_reset_days))
                    )
                ),
                "password": str(central_client.get("password") or ""),
                "auth": str(central_client.get("auth") or ""),
                "security": str(central_client.get("security") or ""),
            }

        try:
            delete_payload = _api_post_client_delete(api, email, keep_traffic=True)
            if not _api_payload_success(delete_payload):
                return False
            add_payload = _api_post_client_add(api, inbound_id, recreated_client)
        except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning(
                "Verified orphan recreate failed inbound_id=%s email=%s: %s",
                inbound_id,
                email,
                exc,
            )
            return False
        if not _api_payload_success(add_payload):
            return False
        logger.warning(
            "Recreated verified detached client via clients API: inbound_id=%s email=%s",
            inbound_id,
            email,
        )
        return True

    def _load_existing() -> tuple[
        dict[str, Any] | None,
        dict[str, Any] | None,
        bool,
    ]:
        raw_inbound = _get_inbound_raw_by_id(api, inbound_id)
        raw_clients = [
            dict(raw_client)
            for raw_client in _iter_client_dicts_from_settings(raw_inbound.get("settings"))
            if isinstance(raw_client, dict)
        ]
        normalized_email = _normalized_client_email(email)
        uuid_matches = [
            raw_client
            for raw_client in raw_clients
            if _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid"))
            == expected_uuid
        ]
        foreign_email_matches = [
            raw_client
            for raw_client in raw_clients
            if _normalized_client_email(raw_client.get("email")) == normalized_email
            and _normalized_client_uuid(raw_client.get("id") or raw_client.get("uuid"))
            not in {"", expected_uuid}
        ]
        if len(uuid_matches) > 1 or foreign_email_matches:
            return raw_inbound, None, True
        return raw_inbound, (uuid_matches[0] if uuid_matches else None), False

    try:
        raw_inbound, existing_client, identity_conflict = _load_existing()
    except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning(
            "Secondary clients-api recovery could not load inbound_id=%s email=%s: %s",
            inbound_id,
            email,
            exc,
        )
        return None, None
    if identity_conflict:
        logger.error(
            "Secondary clients-api recovery refused inbound identity collision: "
            "inbound_id=%s email=%s",
            inbound_id,
            email,
        )
        return None, None

    central_client: dict[str, Any] | None = None
    reset_performed = False

    def _reset_verified_identity_if_needed(identity_email: str) -> bool:
        nonlocal reset_performed
        if reset_performed or not reset_client_traffic or int(total_gb_bytes or 0) <= 0:
            return True
        if not _reset_client_traffic_on_panel(api, email=identity_email):
            return False
        reset_performed = True
        return True

    if existing_client is None:
        central_client, central_conflict = _central_client_by_email()
        if (
            central_conflict
            or central_client is None
            or not _central_identity_matches(central_client)
        ):
            return None, None
        if not _reset_verified_identity_if_needed(
            str(central_client.get("email") or email).strip()
        ):
            return None, None
        try:
            attach_payload = _api_post_json(
                api,
                "panel/api/clients/bulkAttach",
                {"emails": [email], "inboundIds": [inbound_id]},
            )
            if not _api_payload_success(attach_payload):
                logger.warning(
                    "Secondary clients-api recovery bulkAttach rejected inbound_id=%s email=%s payload=%s",
                    inbound_id,
                    email,
                    attach_payload,
                )
        except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning(
                "Secondary clients-api recovery bulkAttach failed inbound_id=%s email=%s: %s",
                inbound_id,
                email,
                exc,
            )
        try:
            raw_inbound, existing_client, identity_conflict = _load_existing()
        except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning(
                "Secondary clients-api recovery reload failed inbound_id=%s email=%s: %s",
                inbound_id,
                email,
                exc,
            )
            return None, None
        if identity_conflict:
            return None, None

    if (
        existing_client is None
        and central_client is not None
        and _recreate_verified_orphan(central_client)
    ):
        try:
            raw_inbound, existing_client, identity_conflict = _load_existing()
        except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as exc:
            logger.warning(
                "Verified orphan recovery reload failed inbound_id=%s email=%s: %s",
                inbound_id,
                email,
                exc,
            )
            return None, None
        if identity_conflict:
            return None, None
        if existing_client is not None:
            recreated_uuid = _normalized_client_uuid(existing_client.get("id") or existing_client.get("uuid"))
            recreated_expiry = _coerce_panel_int(
                existing_client.get("expiryTime", existing_client.get("expiry_time", 0))
            )
            if recreated_uuid == expected_uuid and recreated_expiry == int(target_expiry_ms):
                return expected_uuid, int(target_expiry_ms)

    if existing_client is None:
        logger.warning(
            "Secondary clients-api recovery could not find existing client inbound_id=%s email=%s",
            inbound_id,
            email,
        )
        return None, None

    current_email = str(existing_client.get("email") or email or "").strip()
    if not _reset_verified_identity_if_needed(current_email):
        return None, None
    node_sub_id = str(_extract_client_sub_id(existing_client) or "").strip() or _generate_sub_id()
    normalized_uuid = str(client_uuid or existing_client.get("id") or existing_client.get("uuid") or "").strip()
    if not normalized_uuid:
        return None, None

    runtime_inbound = node_inbound
    if raw_inbound and (raw_inbound.get("streamSettings") is not None or raw_inbound.get("stream_settings") is not None):
        runtime_inbound = _raw_inbound_runtime_namespace(raw_inbound, inbound_id=inbound_id)

    if _is_hysteria_subscription_inbound(runtime_inbound):
        client_payload = _build_hysteria_raw_client(
            inbound_id=inbound_id,
            email=email,
            client_uuid=normalized_uuid,
            expiry_ms=int(target_expiry_ms),
            sub_id=node_sub_id,
            total_gb_bytes=total_gb_bytes,
            traffic_reset_days=traffic_reset_days,
            existing_client=existing_client,
            source_client=source_client,
            preserve_existing_enable=preserve_existing_enable,
            reset_client_traffic=False,
        )
    else:
        client_payload = dict(existing_client)
        client_payload.update(
            {
                "id": normalized_uuid,
                "email": email,
                "enable": (
                    bool(existing_client.get("enable", True))
                    if preserve_existing_enable
                    else True
                ),
                "expiryTime": int(target_expiry_ms),
                "subId": node_sub_id,
                "inboundId": inbound_id,
                "totalGB": max(0, int(total_gb_bytes or 0)),
                "limitIp": _coerce_panel_int(existing_client.get("limitIp", existing_client.get("limit_ip"))),
                "flow": _client_flow_for_inbound(runtime_inbound, source_client),
                "tgId": _coerce_panel_int(existing_client.get("tgId", existing_client.get("tg_id", 0))),
                "comment": str(existing_client.get("comment") or ""),
            }
        )
        if existing_client.get("reset") is not None:
            client_payload["reset"] = existing_client.get("reset")
        if int(total_gb_bytes or 0) == 0:
            client_payload["reset"] = 0
        elif traffic_reset_days is not None:
            client_payload["reset"] = max(0, int(traffic_reset_days))
        client_payload = {key: value for key, value in client_payload.items() if value is not None}

    try:
        update_payload = _api_post_client_update(api, current_email or email, inbound_id, client_payload)
    except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError) as exc:
        logger.warning(
            "Secondary clients-api recovery update failed inbound_id=%s email=%s: %s",
            inbound_id,
            email,
            exc,
        )
        return None, None
    if not _api_payload_success(update_payload):
        logger.warning(
            "Secondary clients-api recovery update rejected inbound_id=%s email=%s payload=%s",
            inbound_id,
            email,
            update_payload,
        )
        return None, None
    return normalized_uuid, int(target_expiry_ms)


def _manual_super_postflight_client_matches(
    raw_inbound: Mapping[str, Any] | None,
    *,
    email: str,
    client_uuid: str,
    expected_total_gb_bytes: int,
    expected_reset_days: int | None,
    expected_expiry_ms: int | None,
) -> bool:
    if not raw_inbound:
        return False
    normalized_email = _normalized_client_email(email)
    normalized_uuid = _normalized_client_uuid(client_uuid)
    try:
        matches = [
            dict(client)
            for client in _iter_client_dicts_from_settings(raw_inbound.get("settings"))
            if _normalized_client_email(client.get("email")) == normalized_email
        ]
    except (TypeError, ValueError):
        return False
    if len(matches) != 1:
        return False
    client = matches[0]
    if _normalized_client_uuid(client.get("id") or client.get("uuid")) != normalized_uuid:
        return False
    if _coerce_panel_int(
        client.get("totalGB", client.get("total_gb", client.get("total")))
    ) != max(0, int(expected_total_gb_bytes or 0)):
        return False
    if (
        expected_reset_days is not None
        and _coerce_panel_int(client.get("reset")) != max(0, int(expected_reset_days))
    ):
        return False
    if (
        expected_expiry_ms is not None
        and _coerce_panel_int(client.get("expiryTime", client.get("expiry_time")))
        != int(expected_expiry_ms)
    ):
        return False
    return True


def _run_manual_super_whitelist_postflight(
    api: Api,
    *,
    base_email: str,
    client_uuid: str,
    primary_sub_id: str,
    primary_inbound: Inbound,
    primary_expiry_ms: int,
    primary_total_gb_bytes: int,
    primary_reset_days: int | None,
    primary_preserve_existing_enable: bool,
    active_subscription_inbounds: list[Inbound],
    secondary_inbounds: list[dict[str, Any]],
    white_node_inbound_ids: set[int],
    host_name: str = "",
) -> bool:
    """Repair and verify only the manual Super WHITELIST topology."""
    primary_inbound_id = _safe_inbound_id(primary_inbound)
    verified_uuid, verified_expiry_ms = update_or_create_client_on_panel(
        api,
        primary_inbound_id,
        base_email,
        0,
        target_expiry_ms=int(primary_expiry_ms),
        target_sub_id=primary_sub_id,
        target_client_uuid=client_uuid,
        total_gb_bytes=primary_total_gb_bytes,
        traffic_reset_days=primary_reset_days,
        # The primary may already have been reset by the first upsert.  This
        # topology repair must never reset its counters a second time.
        reset_client_traffic=False,
        preserve_existing_enable=primary_preserve_existing_enable,
    )
    if (
        _normalized_client_uuid(verified_uuid) != _normalized_client_uuid(client_uuid)
        or int(verified_expiry_ms or 0) != int(primary_expiry_ms)
    ):
        logger.error(
            "Manual Super postflight failed to restore primary inbound_id=%s.",
            primary_inbound_id,
        )
        return False
    try:
        primary_raw = _get_inbound_raw_by_id(api, primary_inbound_id, allow_list_fallback=True)
    except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
        logger.exception(
            "Manual Super postflight could not read primary inbound_id=%s.",
            primary_inbound_id,
        )
        return False
    if not _manual_super_postflight_client_matches(
        primary_raw,
        email=base_email,
        client_uuid=client_uuid,
        expected_total_gb_bytes=primary_total_gb_bytes,
        expected_reset_days=primary_reset_days,
        expected_expiry_ms=primary_expiry_ms,
    ):
        logger.error(
            "Manual Super postflight primary read-back mismatch inbound_id=%s.",
            primary_inbound_id,
        )
        return False

    secondary_by_id = {
        _coerce_panel_int(item.get("inbound_id")): item
        for item in secondary_inbounds
        if _coerce_panel_int(item.get("inbound_id")) > 0
    }
    base_email_norm = _normalized_client_email(base_email)
    expected_uuid = _normalized_client_uuid(client_uuid)
    for node_inbound in active_subscription_inbounds[1:]:
        if not _is_white_subscription_inbound(node_inbound, white_node_inbound_ids):
            continue
        inbound_id = _safe_inbound_id(node_inbound)
        expected = secondary_by_id.get(inbound_id)
        if not expected:
            logger.error(
                "Manual Super postflight has no successful WHITELIST upsert inbound_id=%s.",
                inbound_id,
            )
            return False
        expected_email = str(expected.get("email") or "").strip()
        if _normalized_client_email(expected_email) != _normalized_client_email(
            _secondary_inbound_email(base_email, node_inbound, host_name=host_name)
        ):
            logger.error(
                "Manual Super postflight refused unexpected WHITELIST suffix inbound_id=%s.",
                inbound_id,
            )
            return False
        expected_total = max(0, _coerce_panel_int(expected.get("total_gb_bytes")))
        expected_reset = _coerce_panel_int(expected.get("traffic_reset_days"))
        expected_expiry = _coerce_panel_int(expected.get("expiry_timestamp_ms"))
        try:
            raw_inbound = _get_inbound_raw_by_id(api, inbound_id, allow_list_fallback=True)
            raw_clients = [
                dict(client)
                for client in _iter_client_dicts_from_settings(raw_inbound.get("settings"))
            ]
        except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
            logger.exception(
                "Manual Super postflight could not read WHITELIST inbound_id=%s.",
                inbound_id,
            )
            return False
        if not _manual_super_postflight_client_matches(
            raw_inbound,
            email=expected_email,
            client_uuid=client_uuid,
            expected_total_gb_bytes=expected_total,
            expected_reset_days=expected_reset,
            expected_expiry_ms=expected_expiry,
        ):
            logger.error(
                "Manual Super postflight refused cleanup without an exact WHITELIST suffix inbound_id=%s.",
                inbound_id,
            )
            return False

        base_matches = [
            client
            for client in raw_clients
            if _normalized_client_email(client.get("email")) == base_email_norm
        ]
        if not base_matches:
            continue
        if (
            len(base_matches) != 1
            or _normalized_client_uuid(base_matches[0].get("id") or base_matches[0].get("uuid"))
            != expected_uuid
        ):
            logger.error(
                "Manual Super postflight refused ambiguous base-email cleanup inbound_id=%s.",
                inbound_id,
            )
            return False
        if not _detach_client_from_inbound_by_email(api, inbound_id, base_email):
            return False
        try:
            verified_raw = _get_inbound_raw_by_id(api, inbound_id, allow_list_fallback=True)
            verified_clients = [
                dict(client)
                for client in _iter_client_dicts_from_settings(verified_raw.get("settings"))
            ]
        except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
            logger.exception(
                "Manual Super postflight WHITELIST read-back failed inbound_id=%s.",
                inbound_id,
            )
            return False
        if any(
            _normalized_client_email(client.get("email")) == base_email_norm
            for client in verified_clients
        ) or not _manual_super_postflight_client_matches(
            verified_raw,
            email=expected_email,
            client_uuid=client_uuid,
            expected_total_gb_bytes=expected_total,
            expected_reset_days=expected_reset,
            expected_expiry_ms=expected_expiry,
        ):
            logger.error(
                "Manual Super postflight WHITELIST read-back mismatch inbound_id=%s.",
                inbound_id,
            )
            return False
    return True

def _whitelist2_settle_required(host_name: str, include_white_inbounds: bool) -> bool:
    if host_name != "MainPanel" or not include_white_inbounds:
        return False
    return any(
        item["new_inbound_id"] == 25
        and item["new_label"] == "WHITELIST2"
        and item.get("client_profile") == "clearway"
        and not item.get("staged", False)
        for item in subscription_policy.inbound_migrations(host_name)
    )


def _whitelist2_binding_identities(raw_inbound: Mapping[str, Any]) -> frozenset[tuple[str, str, str]]:
    if not isinstance(raw_inbound, Mapping) or raw_inbound.get("settings") is None:
        raise ValueError("whitelist2_snapshot_unavailable")
    identities: set[tuple[str, str, str]] = set()
    emails: set[str] = set()
    for client in _iter_client_dicts_from_settings(raw_inbound["settings"]):
        identity = (
            _normalized_client_email(client.get("email")),
            _normalized_client_uuid(client.get("id") or client.get("uuid")),
            str(_extract_client_sub_id(client) or "").strip(),
        )
        if not all(identity) or identity[0] in emails:
            raise ValueError("whitelist2_snapshot_ambiguous")
        emails.add(identity[0])
        identities.add(identity)
    return frozenset(identities)


def _whitelist2_native_endpoint(value: str) -> tuple[str, str, int, str] | None:
    try:
        parsed = urlparse(value)
        port = parsed.port or 443
    except ValueError:
        return None
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
        or parsed.password is not None or parsed.query or parsed.fragment or parsed.params
    ):
        return None
    return "https", parsed.hostname.lower(), port, _normalize_node_base_path(parsed.path)


def _whitelist2_read_json_once(
    url: str, *, api_token: str, tls_verify: bool | str, timeout_seconds: float,
) -> Mapping[str, Any]:
    """One bounded GET; no redirects, SDK retries, credential logs or disk cache."""
    if timeout_seconds <= 0 or not api_token or _whitelist2_native_endpoint(url) is None:
        raise ValueError("whitelist2_native_read_scope_invalid")
    deadline = time.monotonic() + timeout_seconds
    maximum_body_bytes = 16 * 1024**2
    session = requests.Session()
    session.trust_env = False
    if tls_verify is True:
        # Preserve the SDK's configured CA trust without inheriting env proxies.
        tls_verify = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE") or True
    try:
        response = session.get(
            url, headers={"Authorization": "Bearer " + api_token, "Accept": "application/json"},
            timeout=(min(1.0, timeout_seconds), min(1.0, timeout_seconds)), verify=tls_verify,
            allow_redirects=False, stream=True,
        )
        try:
            if response.status_code != 200:
                raise ValueError("whitelist2_native_read_status_invalid")
            body = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("whitelist2_native_read_deadline")
                block = response.raw.read1(min(64 * 1024, maximum_body_bytes + 1 - len(body)), decode_content=True)
                if not block:
                    break
                body.extend(block)
                if len(body) > maximum_body_bytes:
                    raise ValueError("whitelist2_native_read_body_too_large")
            envelope = json.loads(body)
            if not isinstance(envelope, Mapping) or envelope.get("success") is not True:
                raise ValueError("whitelist2_native_read_envelope_invalid")
            return envelope
        finally:
            response.close()
    finally:
        session.close()


def _resolve_whitelist2_native_child_target(
    api: Api, master_raw: Mapping[str, Any], *, timeout_seconds: float,
) -> tuple[str, str, bool] | None:
    """Match the registered remote node to existing catalog credentials in RAM."""
    node_id = master_raw.get("nodeId", master_raw.get("node_id"))
    if type(node_id) is not int or node_id <= 0:
        return None
    master_token = _normalize_api_token(getattr(api, "_pepel_api_token", None))
    if not master_token:
        return None
    inbound_api = api.inbound
    master_verify: bool | str = (
        (inbound_api.custom_certificate_path or True) if inbound_api.use_tls_verify else False
    )
    envelope = _whitelist2_read_json_once(
        inbound_api._url("panel/api/nodes/list"), api_token=master_token,
        tls_verify=master_verify, timeout_seconds=timeout_seconds,
    )
    nodes = envelope.get("obj")
    if not isinstance(nodes, list):
        return None
    matches = [node for node in nodes if isinstance(node, Mapping) and node.get("id") == node_id]
    if len(matches) != 1 or matches[0].get("enable") is not True:
        return None
    node = matches[0]
    scheme = str(node.get("scheme") or "https")
    address = str(node.get("address") or "")
    port = node.get("port") or 443
    if type(port) is not int or not 0 < port <= 65535:
        return None
    address = "[" + address + "]" if ":" in address else address
    base_path = _normalize_node_base_path(node.get("basePath"))
    target_url = f"{scheme}://{address}:{port}{base_path}"
    target_endpoint = _whitelist2_native_endpoint(target_url)
    tls_mode = str(node.get("tlsVerifyMode") or "verify")
    if target_endpoint is None or tls_mode not in {"verify", "skip"}:
        return None
    catalog = [host for host in get_all_hosts()
               if _whitelist2_native_endpoint(str(host.get("host_url") or "")) == target_endpoint]
    tokens = {_normalize_api_token(_resolve_host_api_token(host)) for host in catalog}
    tokens.discard("")
    if len(tokens) != 1:
        return None
    return target_url + "panel/api/inbounds/list", tokens.pop(), tls_mode == "verify"


def _whitelist2_standard_native_matches(
    master_raw: Mapping[str, Any], child_raw: Mapping[str, Any], *,
    email: str, client_uuid: str, sub_id: str, expiry_ms: int, reset_days: int,
) -> bool:
    """Exact Standard 5-GiB native settings/stat proof; this is not live RPC proof."""
    expected_uuid = _normalized_client_uuid(client_uuid)
    tag = "whitelist2-clearway-xhttp"
    if (
        not expected_uuid or not email or not sub_id
        or master_raw.get("id") != 25 or master_raw.get("tag") != tag or child_raw.get("tag") != tag
        or master_raw.get("enable") is not True or child_raw.get("enable") is not True
        or master_raw.get("protocol") != "vless" or child_raw.get("protocol") != "vless"
        or type(master_raw.get("port")) is not int or not 0 < master_raw["port"] <= 65535
        or type(expiry_ms) is not int or expiry_ms <= int(time.time() * 1000)
        or type(reset_days) is not int or reset_days not in (0, MULTI_NODE_LIMIT_RESET_DAYS)
    ):
        return False
    for field in ("listen", "port", "protocol", "tag", "streamSettings", "sniffing"):
        old, new = master_raw.get(field), child_raw.get(field)
        if field in {"streamSettings", "sniffing"}:
            old = json.loads(old) if isinstance(old, str) and old else old
            new = json.loads(new) if isinstance(new, str) and new else new
        if old != new:
            return False
    matched_clients, matched_stats = [], []
    for raw in (master_raw, child_raw):
        settings = _raw_settings_mapping(raw.get("settings"))
        clients, stats = settings.get("clients"), raw.get("clientStats")
        if not isinstance(clients, list) or not isinstance(stats, list):
            return False
        matches = [row for row in clients if isinstance(row, Mapping) and (
            _normalized_client_uuid(row.get("uuid") or row.get("id")) == expected_uuid or row.get("email") == email
        )]
        traffic = [row for row in stats if isinstance(row, Mapping) and row.get("email") == email]
        if len(matches) != 1 or len(traffic) != 1:
            return False
        client, stat = matches[0], traffic[0]
        if (
            _normalized_client_uuid(client.get("uuid") or client.get("id")) != expected_uuid
            or client.get("email") != email or _extract_client_sub_id(client) != sub_id
            or client.get("enable") is not True or client.get("flow")
            or any(type(client.get(field)) is not int for field in ("totalGB", "expiryTime", "reset"))
            or client.get("totalGB") != STANDARD_WHITELIST_TRAFFIC_BYTES
            or client.get("expiryTime") != expiry_ms or client.get("reset") != reset_days
            or stat.get("total") != STANDARD_WHITELIST_TRAFFIC_BYTES
            or any(type(stat.get(field)) is not int for field in ("total", "expiryTime", "reset"))
            or stat.get("expiryTime") != expiry_ms or stat.get("reset") != reset_days
            or any(type(stat.get(field)) is not int or stat[field] < 0 for field in ("up", "down"))
            or stat["up"] + stat["down"] >= STANDARD_WHITELIST_TRAFFIC_BYTES
        ):
            return False
        matched_clients.append(client)
        matched_stats.append(stat)
    if matched_stats[0].get("enable") is not False or matched_stats[1].get("enable") is not True:
        return False
    return all(matched_clients[0].get(field) == matched_clients[1].get(field)
               for field in ("enable", "totalGB", "expiryTime", "reset", "subId", "flow", "limitIp"))


def _whitelist2_standard_native_readback(
    api: Api, master_raw: Mapping[str, Any], *, email: str, client_uuid: str,
    sub_id: str, expiry_ms: int, reset_days: int, timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    target = _resolve_whitelist2_native_child_target(api, master_raw, timeout_seconds=timeout_seconds / 2)
    remaining = deadline - time.monotonic()
    if target is None or remaining <= 0:
        return False
    url, token, verify = target
    envelope = _whitelist2_read_json_once(url, api_token=token, tls_verify=verify, timeout_seconds=remaining)
    rows = envelope.get("obj")
    if not isinstance(rows, list):
        return False
    matches = [raw for raw in rows if isinstance(raw, Mapping) and raw.get("tag") == master_raw.get("tag")]
    return len(matches) == 1 and _whitelist2_standard_native_matches(
        master_raw, matches[0], email=email, client_uuid=client_uuid, sub_id=sub_id,
        expiry_ms=expiry_ms, reset_days=reset_days,
    )


def _wait_for_whitelist2_write_settle(
    api: Api,
    *,
    baseline: frozenset[tuple[str, str, str]],
    email: str,
    client_uuid: str,
    sub_id: str,
    total_gb_bytes: int,
    expiry_ms: int,
    reset_days: int,
    enabled: bool,
) -> bool:
    """Read only: require two complete observations across a native 5s poll."""
    deadline = time.monotonic() + 30.0
    previous: frozenset[tuple[str, str, str]] | None = None
    previous_at = 0.0
    expected_identity = (
        _normalized_client_email(email), _normalized_client_uuid(client_uuid), str(sub_id).strip(),
    )
    for attempt in range(7):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        observed = None
        try:
            raw = _get_inbound_raw_by_id(
                api, 25, allow_list_fallback=False, request_timeout=min(4.0, remaining),
            )
            identities = _whitelist2_binding_identities(raw)
            state = _raw_whitelist_client_cycle_state(raw, email=email, client_uuid=client_uuid)
            if (
                raw.get("id") == 25
                and baseline.issubset(identities)
                and expected_identity in identities
                and not state.get("collision")
                and state.get("client") and state.get("stat")
                and state.get("sub_id") == sub_id
                and state.get("settings_total_bytes") == total_gb_bytes
                and state.get("traffic_total_bytes") == total_gb_bytes
                and state.get("settings_expiry_ms") == expiry_ms
                and state.get("traffic_expiry_ms") == expiry_ms
                and state.get("settings_reset_days") == reset_days
                and state.get("traffic_reset_days") == reset_days
                and state.get("settings_enable") == enabled
            ):
                traffic_matches = state.get("traffic_enable") == enabled
                if (
                    not traffic_matches and enabled is True
                    and total_gb_bytes == STANDARD_WHITELIST_TRAFFIC_BYTES
                    and state["stat"].get("enable") is False
                ):
                    traffic_matches = _whitelist2_standard_native_readback(
                        api, raw, email=email, client_uuid=client_uuid, sub_id=sub_id,
                        expiry_ms=expiry_ms, reset_days=reset_days,
                        timeout_seconds=min(4.0, max(0.0, deadline - time.monotonic())),
                    )
                if traffic_matches:
                    observed = identities
        except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
            # A transient read may settle on the next native sync. Never replay
            # the write, regenerate credentials, or clean up on this path.
            pass
        now = time.monotonic()
        if observed is not None and observed == previous and now - previous_at >= 5.0:
            return True
        previous = observed
        previous_at = now
        if attempt == 6 or now + 5.0 > deadline:
            break
        time.sleep(5.0)
    return False


def _create_or_update_key_on_host_once(
    *,
    host_name: str,
    host_data: dict,
    email: str,
    days_to_add: int,
    months: int = 1,
    target_expiry_ms: int | None = None,
    target_sub_id: str | None = None,
    target_client_uuid: str | None = None,
    total_gb_bytes: int | None = None,
    white_traffic_limit_bytes: int | None = None,
    reset_limited_traffic: bool = False,
    reset_exhausted_limited_traffic: bool = False,
    whitelist_cycle_anchor_ms: int | None = None,
    include_white_inbounds: bool = True,
    require_white_inbound: bool = False,
    manual_super_whitelist_postflight: bool = False,
    failure_context: MutableMapping[str, str] | None = None,
) -> dict | None:
    api, inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=_resolve_host_api_token(host_data),
        prefer_raw_inbound_lookup=True,
    )
    if not api or not inbound:
        logger.error(f"Workflow failed: Could not log in or find inbound on host '{host_name}'.")
        _record_panel_issue_failure(failure_context, "login_failed")
        return None

    settle_whitelist2 = _whitelist2_settle_required(host_name, include_white_inbounds)
    if manual_super_whitelist_postflight or settle_whitelist2:
        # Manual issuance runs in a daemon thread that cannot be cancelled.
        # Do not let py3xui replay a mutation after the outer workflow timeout.
        _set_api_request_retries(api, 1)

    node_address_by_id, node_address_by_name = _node_public_address_maps(api)
    white_node_inbound_ids = _white_node_inbound_ids_for_panel(host_name)
    primary_inbound_id = _safe_inbound_id(inbound)
    if _host_is_white(host_data) and primary_inbound_id > 0:
        white_node_inbound_ids.add(primary_inbound_id)

    raw_subscription_inbounds = _list_inbounds_raw(api)
    all_subscription_inbounds = _subscription_inbounds(
        api,
        inbound,
        include_white_inbounds=True,
        white_node_inbound_ids=white_node_inbound_ids,
        raw_inbounds=raw_subscription_inbounds or None,
        host_name=host_name,
    )
    active_subscription_inbounds = _subscription_inbounds(
        api,
        inbound,
        include_white_inbounds=include_white_inbounds,
        white_node_inbound_ids=white_node_inbound_ids,
        raw_inbounds=raw_subscription_inbounds or None,
        host_name=host_name,
        require_migration_targets=bool(include_white_inbounds or require_white_inbound),
    )
    if not active_subscription_inbounds:
        logger.error(
            "Workflow failed: no enabled VLESS subscription inbound selected on host '%s' include_white=%s.",
            host_name,
            include_white_inbounds,
        )
        _record_panel_issue_failure(failure_context, "subscription_inbounds_missing")
        return None
    if require_white_inbound and not any(
        _is_white_subscription_inbound(item, white_node_inbound_ids) for item in active_subscription_inbounds
    ):
        logger.error(
            "Workflow failed: SUPER issuance on host '%s' has no enabled WHITELIST inbound.",
            host_name,
        )
        _record_panel_issue_failure(failure_context, "super_whitelist_missing")
        return None

    now_ms = int(time.time() * 1000)
    paid_expiry_ms = _paid_expiry_ms_for_client(
        email,
        target_expiry_ms=target_expiry_ms,
        days_to_add=days_to_add,
        now_ms=now_ms,
    )
    try:
        stored_key = get_key_by_email(str(email or "").strip()) or {}
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, AttributeError) as exc:
        logger.debug("Could not load WHITELIST quota override for email=%s: %s", email, exc)
        stored_key = {}
    # This rollout changes renewal/upsert behavior only for an explicit marker.
    # Existing 2099 issuance keeps using its caller-provided unlimited total.
    unlimited_whitelist = _key_has_unlimited_whitelist_override(stored_key)
    if unlimited_whitelist:
        # A durable per-key exception always wins over caller defaults so a
        # later renewal/retry cannot silently restore the paid traffic cap.
        white_traffic_limit_bytes = 0
    elif white_traffic_limit_bytes is None and _mainpanel_resolve_key_tier(stored_key):
        white_traffic_limit_bytes = whitelist_traffic_limit_bytes_for_key(stored_key)
    cycle_anchor_ms = _whitelist_cycle_anchor_ms_for_client(
        email,
        requested_anchor_ms=whitelist_cycle_anchor_ms,
        now_ms=now_ms,
    )

    issue_inbound = active_subscription_inbounds[0]
    try:
        identity_plan = _plan_subscription_client_identities(
            api, active_subscription_inbounds, all_inbounds=all_subscription_inbounds,
            base_email=email, target_client_uuid=target_client_uuid,
            target_sub_id=target_sub_id, stored_key=stored_key, host_name=host_name,
        )
    except ValueError as exc:
        reason = str(exc)
        logger.error("Subscription identity preflight refused host=%s reason=%s", host_name, reason)
        _record_panel_issue_failure(failure_context, reason)
        return None
    target_client_uuid = identity_plan.client_uuid
    target_sub_id = identity_plan.primary_sub_id
    whitelist2_baseline = None
    if settle_whitelist2:
        try:
            whitelist2_baseline = _whitelist2_binding_identities(identity_plan.bindings[25].snapshot)
        except (KeyError, ValueError, TypeError):
            logger.error("WHITELIST2 identity snapshot refused before client writes: host=%s", host_name)
            _record_panel_issue_failure(failure_context, "subscription_identity_malformed")
            return None



    if (
        unlimited_whitelist
        and _is_white_subscription_inbound(issue_inbound, white_node_inbound_ids)
    ):
        # The durable policy wins even when an older caller passes an explicit
        # primary total.  Otherwise a renewal on a WHITELIST-primary host can
        # silently restore a finite quota.
        primary_total_gb_bytes = 0
    else:
        primary_total_gb_bytes = (
            total_gb_bytes
            if total_gb_bytes is not None
            else _multi_node_total_gb_bytes(
                issue_inbound,
                months=months,
                white_node_inbound_ids=white_node_inbound_ids,
                white_traffic_limit_bytes=white_traffic_limit_bytes,
            )
        )
    primary_is_limited_white = (
        _is_white_subscription_inbound(issue_inbound, white_node_inbound_ids)
        and primary_total_gb_bytes > 0
    )
    primary_is_unlimited_white = (
        _is_white_subscription_inbound(issue_inbound, white_node_inbound_ids)
        and primary_total_gb_bytes == 0
        and white_traffic_limit_bytes == 0
    )
    primary_target_expiry_ms = paid_expiry_ms
    primary_reset_traffic = False
    primary_traffic_reset_days: int | None = None
    if primary_is_limited_white:
        primary_cycle_state = _raw_whitelist_client_cycle_state(
            identity_plan.bindings[issue_inbound.id].snapshot,
            email=email, client_uuid=target_client_uuid,
        )
        if primary_cycle_state.get("collision"):
            logger.error(
                "WHITELIST cycle update refused after identity collision: host=%s inbound_id=%s email=%s",
                host_name,
                issue_inbound.id,
                email,
            )
            _record_panel_issue_failure(failure_context, "whitelist_identity_collision")
            return None
        primary_target_expiry_ms, primary_cycle_overdue = _next_whitelist_reset_deadline_ms(
            cycle_anchor_ms,
            primary_cycle_state.get("current_reset_ms"),
            paid_expiry_ms,
            now_ms,
            current_reset_days=primary_cycle_state.get("reset_days"),
        )
        primary_reset_traffic = _should_reset_limited_whitelist_traffic(
            primary_cycle_state,
            reset_limited_traffic=reset_limited_traffic,
            reset_exhausted_limited_traffic=reset_exhausted_limited_traffic,
            cycle_overdue=primary_cycle_overdue,
        )
        primary_traffic_reset_days = (
            0
            if primary_target_expiry_ms >= paid_expiry_ms
            else MULTI_NODE_LIMIT_RESET_DAYS
        )
    elif primary_is_unlimited_white:
        primary_traffic_reset_days = 0

    primary_metadata: dict[str, Any] = {}
    client_uuid, new_expiry_ms = update_or_create_client_on_panel(
        api,
        issue_inbound.id,
        email,
        days_to_add,
        target_expiry_ms=primary_target_expiry_ms,
        target_sub_id=target_sub_id,
        target_client_uuid=target_client_uuid,
        total_gb_bytes=primary_total_gb_bytes,
        traffic_reset_days=primary_traffic_reset_days,
        reset_client_traffic=primary_reset_traffic,
        preserve_existing_enable=primary_is_limited_white and not primary_reset_traffic,
        result_metadata=primary_metadata,
    )
    if not client_uuid or client_uuid != identity_plan.client_uuid:
        logger.error(f"Workflow failed: Could not create/update client '{email}' on host '{host_name}'.")
        _record_panel_issue_failure(failure_context, "primary_upsert_failed")
        return None
    if target_expiry_ms is not None and int(new_expiry_ms or 0) != int(primary_target_expiry_ms):
        logger.error(
            "Workflow failed: panel returned an unexpected primary expiry for '%s' on host '%s'.",
            email,
            host_name,
        )
        _record_panel_issue_failure(failure_context, "primary_expiry_mismatch")
        return None
    if target_expiry_ms is None and not primary_is_limited_white:
        # Keep the legacy no-target API behavior for callers that let the
        # primary ordinary inbound calculate the paid expiry itself.
        paid_expiry_ms = int(new_expiry_ms)

    result_sub_id = str(primary_metadata.get("sub_id") or target_sub_id or "").strip() or _get_client_sub_id_from_panel(
        api,
        issue_inbound.id,
        email=email,
        client_uuid=client_uuid,
    )
    if result_sub_id != identity_plan.primary_sub_id:
        logger.error("Workflow failed: primary subscription identity mismatch on host '%s'.", host_name)
        _record_panel_issue_failure(failure_context, "sub_id_missing")
        return None

    secondary_inbounds: list[dict[str, Any]] = []
    secondary_failures: list[dict[str, Any]] = []
    critical_white_failures: list[dict[str, Any]] = []
    whitelist2_settle_spec: dict[str, Any] | None = None
    for node_inbound in active_subscription_inbounds[1:]:
        node_email = _secondary_inbound_email(email, node_inbound, host_name=host_name)
        node_total_gb_bytes = _multi_node_total_gb_bytes(
            node_inbound,
            months=months,
            white_node_inbound_ids=white_node_inbound_ids,
            white_traffic_limit_bytes=white_traffic_limit_bytes,
        )
        is_limited_white_inbound = (
            _is_white_subscription_inbound(node_inbound, white_node_inbound_ids)
            and node_total_gb_bytes > 0
        )
        is_unlimited_white_inbound = (
            _is_white_subscription_inbound(node_inbound, white_node_inbound_ids)
            and node_total_gb_bytes == 0
            and white_traffic_limit_bytes == 0
        )
        node_target_expiry_ms = paid_expiry_ms
        node_reset_traffic = False
        node_traffic_reset_days: int | None = None
        if is_limited_white_inbound:
            node_cycle_state = _raw_whitelist_client_cycle_state(
                identity_plan.bindings[node_inbound.id].snapshot,
                email=node_email, client_uuid=client_uuid,
            )
            if node_cycle_state.get("collision"):
                failure = {
                    "inbound_id": node_inbound.id,
                    "label": _inbound_label(node_inbound),
                    "email": node_email,
                    "reason": "white_node_identity_collision",
                    "is_white_inbound": True,
                }
                secondary_failures.append(failure)
                critical_white_failures.append(failure)
                continue
            node_target_expiry_ms, node_cycle_overdue = _next_whitelist_reset_deadline_ms(
                cycle_anchor_ms,
                node_cycle_state.get("current_reset_ms"),
                paid_expiry_ms,
                now_ms,
                current_reset_days=node_cycle_state.get("reset_days"),
            )
            node_reset_traffic = _should_reset_limited_whitelist_traffic(
                node_cycle_state,
                reset_limited_traffic=reset_limited_traffic,
                reset_exhausted_limited_traffic=reset_exhausted_limited_traffic,
                cycle_overdue=node_cycle_overdue,
            )
            node_traffic_reset_days = (
                0
                if node_target_expiry_ms >= paid_expiry_ms
                else MULTI_NODE_LIMIT_RESET_DAYS
            )
        elif is_unlimited_white_inbound:
            node_traffic_reset_days = 0
        node_sub_id = identity_plan.bindings[node_inbound.id].sub_id
        if subscription_policy.migration_for_target(host_name, _safe_inbound_id(node_inbound), _inbound_label(node_inbound)):
            # The complete snapshot was verified before the first client write.
            identity_raw = identity_plan.bindings[node_inbound.id].snapshot
            if not isinstance(identity_raw, Mapping) or identity_raw.get("settings") is None:
                _record_panel_issue_failure(failure_context, "whitelist_identity_unavailable")
                return None
            identity_state = _raw_whitelist_client_cycle_state(
                identity_raw, email=node_email, client_uuid=client_uuid,
            )
            if identity_state.get("collision") or (identity_state.get("client") and not identity_state.get("sub_id")):
                _record_panel_issue_failure(failure_context, "whitelist_identity_collision")
                return None
            if settle_whitelist2 and _safe_inbound_id(node_inbound) == 25:
                whitelist2_settle_spec = {
                    "baseline": whitelist2_baseline,
                    "email": node_email,
                    "client_uuid": client_uuid,
                    "sub_id": node_sub_id,
                    "total_gb_bytes": node_total_gb_bytes,
                    "expiry_ms": node_target_expiry_ms,
                    "reset_days": int(node_traffic_reset_days or 0),
                    "enabled": (
                        bool(identity_state.get("settings_enable", True))
                        if is_limited_white_inbound and not node_reset_traffic
                        else True
                    ),
                }
        node_uuid, node_expiry_ms = update_or_create_client_on_panel(
            api,
            node_inbound.id,
            node_email,
            days_to_add,
            target_expiry_ms=node_target_expiry_ms,
            target_sub_id=node_sub_id,
            target_client_uuid=client_uuid,
            total_gb_bytes=node_total_gb_bytes,
            traffic_reset_days=node_traffic_reset_days,
            reset_client_traffic=node_reset_traffic,
            preserve_existing_enable=is_limited_white_inbound and not node_reset_traffic,
        )
        node_result_matches = node_uuid == client_uuid and (
            target_expiry_ms is None or node_expiry_ms == node_target_expiry_ms
        )
        # A failed client upsert must not authorize orphan deletion/recreation
        # or substitute an identity discovered after the preflight plan.
        node_result_matches = node_uuid == client_uuid and (
            target_expiry_ms is None or node_expiry_ms == node_target_expiry_ms
        )
        if not node_result_matches:
            failure = {
                "inbound_id": node_inbound.id,
                "label": _inbound_label(node_inbound),
                "email": node_email,
                "reason": ("white_node_update_failed" if is_limited_white_inbound else "node_update_failed"),
                "is_white_inbound": is_limited_white_inbound,
            }
            secondary_failures.append(failure)
            if is_limited_white_inbound:
                critical_white_failures.append(failure)
            logger.error(
                "Secondary node update failed for '%s' on host '%s' inbound='%s'. Refusing partial subscription.",
                email,
                host_name,
                _inbound_label(node_inbound),
            )
            continue
        node_connection_string = get_connection_string(
            node_inbound,
            client_uuid,
            host_data["host_url"],
            remark=_inbound_label(node_inbound),
            node_address_by_id=node_address_by_id,
            node_address_by_name=node_address_by_name,
        )
        secondary_inbounds.append(
            {
                "inbound_id": node_inbound.id,
                "label": _inbound_label(node_inbound),
                "email": node_email,
                "sub_id": node_sub_id,
                "connection_string": node_connection_string,
                "total_gb_bytes": node_total_gb_bytes,
                "expiry_timestamp_ms": node_target_expiry_ms,
                "traffic_reset_days": node_traffic_reset_days,
            }
        )

    if manual_super_whitelist_postflight and not critical_white_failures:
        postflight_ok = _run_manual_super_whitelist_postflight(
            api,
            base_email=email,
            client_uuid=client_uuid,
            primary_sub_id=result_sub_id,
            primary_inbound=issue_inbound,
            primary_expiry_ms=primary_target_expiry_ms,
            primary_total_gb_bytes=primary_total_gb_bytes,
            primary_reset_days=primary_traffic_reset_days,
            primary_preserve_existing_enable=(
                primary_is_limited_white and not primary_reset_traffic
            ),
            active_subscription_inbounds=active_subscription_inbounds,
            secondary_inbounds=secondary_inbounds,
            white_node_inbound_ids=white_node_inbound_ids,
            host_name=host_name,
        )
        if not postflight_ok:
            _record_panel_issue_failure(
                failure_context,
                "manual_super_whitelist_postflight_failed",
            )
            return None
        if whitelist2_settle_spec is not None:
            # The verified manual topology repair may deliberately remove this
            # same client's base-email alias. Its final white identity is still
            # checked exactly; every other pre-write membership remains required.
            whitelist2_settle_spec["baseline"] = frozenset(
                identity for identity in whitelist2_settle_spec["baseline"]
                if identity[:2] != (
                    _normalized_client_email(email), _normalized_client_uuid(client_uuid),
                )
            )

    if whitelist2_settle_spec is not None:
        if not _wait_for_whitelist2_write_settle(api, **whitelist2_settle_spec):
            _record_panel_issue_failure(failure_context, "whitelist2_write_pending")
            return None

    # Identity preflight rejects old excluded bindings; routine issuance never
    # performs a hidden detach/migration after renewing the primary client.
    white_cleanup_inbounds: list[dict[str, Any]] = []

    if critical_white_failures:
        _record_panel_issue_failure(failure_context, "secondary_white_failed")
        logger.error(
            "Host '%s' failed to provision subscription '%s' for '%s' safely: critical_white_failures=%s.",
            host_name,
            result_sub_id,
            email,
            critical_white_failures,
        )
        return None
    if secondary_failures:
        _record_panel_issue_failure(failure_context, "secondary_update_failed")
        logger.error(
            "Host '%s' refused partial subscription '%s' for '%s': failures=%s.",
            host_name,
            result_sub_id,
            email,
            secondary_failures,
        )
        return None
    expected_links_count = 1 + len(secondary_inbounds)
    try:
        subscription_links = _get_subscription_links_with_retry(
            api,
            result_sub_id,
            expected_count=expected_links_count,
            attempts=3,
            delay_seconds=1.5,
        )
    except (OSError, RequestException, RuntimeError, TimeoutError, ValueError) as exc:
        logger.warning(
            "Failed to verify subscription links for host '%s' subId '%s': %s",
            host_name,
            result_sub_id,
            exc,
        )
        subscription_links = []
    if subscription_links and len(subscription_links) < expected_links_count:
        logger.warning(
            "Host '%s' subId '%s': received %s subscription link(s), expected at least %s.",
            host_name,
            result_sub_id,
            len(subscription_links),
            expected_links_count,
        )
    fallback_links = _build_subscription_links_from_panel_clients(
        api,
        host_data,
        result_sub_id,
        active_subscription_inbounds,
        raw_inbounds=None,
        node_address_by_id=node_address_by_id,
        node_address_by_name=node_address_by_name,
        client_uuid=client_uuid,
        base_email=email,
    )
    if fallback_links:
        subscription_links = fallback_links
    subscription_links = _filter_subscription_links_for_inbounds(subscription_links, active_subscription_inbounds)
    subscription_links = patch_subscription_links_for_inbounds(
        subscription_links,
        active_subscription_inbounds,
        node_address_by_id=node_address_by_id,
        node_address_by_name=node_address_by_name,
        remark_mode="inbound_label",
    )
    connection_string = None
    if _is_hysteria_subscription_inbound(issue_inbound):
        connection_string = next(
            (
                str(link or "").strip()
                for link in subscription_links
                if urlparse(str(link or "").strip()).scheme.lower() in {"hysteria2", "hy2"}
            ),
            None,
        )
    if not connection_string:
        connection_string = get_connection_string(
            issue_inbound,
            client_uuid,
            host_data["host_url"],
            remark=_inbound_label(issue_inbound),
            node_address_by_id=node_address_by_id,
            node_address_by_name=node_address_by_name,
        )
    if not connection_string:
        logger.error(
            "Empty connection string for '%s' on host '%s'. Check Reality/TLS settings on panel.",
            email,
            host_name,
        )
        _record_panel_issue_failure(failure_context, "connection_string_empty")
        return None
    expected_subscription_labels = [_inbound_label(inbound) for inbound in active_subscription_inbounds]
    actual_subscription_labels = [
        unquote(urlparse(str(link or "").strip()).fragment or "")
        for link in subscription_links
        if str(link or "").strip()
    ]
    missing_subscription_labels = [
        label for label in expected_subscription_labels if label and label not in actual_subscription_labels
    ]
    endpoint_mismatches = _subscription_endpoint_mismatches(
        subscription_links,
        active_subscription_inbounds,
        node_address_by_id=node_address_by_id,
        node_address_by_name=node_address_by_name,
    )
    verify_attempt = 1
    while (
        len(subscription_links) < expected_links_count or missing_subscription_labels or endpoint_mismatches
    ) and verify_attempt < 12:
        verify_attempt += 1
        logger.warning(
            "Host '%s' subscription payload for '%s' is incomplete on verify attempt %s/12: "
            "got labels=%s expected=%s missing=%s. Retrying authoritative fallback build...",
            host_name,
            email,
            verify_attempt - 1,
            actual_subscription_labels,
            expected_subscription_labels,
            missing_subscription_labels,
        )
        time.sleep(2.0)
        retry_fallback_links = _build_subscription_links_from_panel_clients(
            api,
            host_data,
            result_sub_id,
            active_subscription_inbounds,
            raw_inbounds=None,
            node_address_by_id=node_address_by_id,
            node_address_by_name=node_address_by_name,
            client_uuid=client_uuid,
            base_email=email,
        )
        if retry_fallback_links:
            subscription_links = retry_fallback_links
        subscription_links = _filter_subscription_links_for_inbounds(subscription_links, active_subscription_inbounds)
        subscription_links = patch_subscription_links_for_inbounds(
            subscription_links,
            active_subscription_inbounds,
            node_address_by_id=node_address_by_id,
            node_address_by_name=node_address_by_name,
            remark_mode="inbound_label",
        )
        actual_subscription_labels = [
            unquote(urlparse(str(link or "").strip()).fragment or "")
            for link in subscription_links
            if str(link or "").strip()
        ]
        missing_subscription_labels = [
            label for label in expected_subscription_labels if label and label not in actual_subscription_labels
        ]
        endpoint_mismatches = _subscription_endpoint_mismatches(
            subscription_links,
            active_subscription_inbounds,
            node_address_by_id=node_address_by_id,
            node_address_by_name=node_address_by_name,
        )
    if len(subscription_links) < expected_links_count or missing_subscription_labels or endpoint_mismatches:
        logger.error(
            "Host '%s' refused invalid subscription payload for '%s': got labels=%s expected=%s missing=%s "
            "endpoint_mismatches=%s.",
            host_name,
            email,
            actual_subscription_labels,
            expected_subscription_labels,
            missing_subscription_labels,
            endpoint_mismatches,
        )
        _record_panel_issue_failure(failure_context, "subscription_payload_incomplete")
        return None

    subscription_link = None
    subscription_base_url = _resolve_subscription_base_url(host_data)
    if subscription_base_url:
        if result_sub_id:
            subscription_link = _build_subscription_link(subscription_base_url, result_sub_id)
        else:
            logger.warning(
                "Could not build subscription link for host '%s' and client '%s': subId not found. Using connection string fallback.",
                host_name,
                email,
            )

    logger.info(f"Successfully processed key for '{email}' on host '{host_name}'.")

    return {
        "client_uuid": client_uuid,
        "email": email,
        "expiry_timestamp_ms": paid_expiry_ms,
        "whitelist_reset_timestamp_ms": (
            primary_target_expiry_ms if primary_is_limited_white else None
        ),
        "connection_string": connection_string,
        "subscription_link": subscription_link,
        "host_name": host_name,
        "sub_id": result_sub_id,
        "secondary_inbounds": secondary_inbounds,
        "secondary_failures": secondary_failures,
        "white_cleanup_inbounds": white_cleanup_inbounds,
        "subscription_links": subscription_links,
    }


def _rename_client_email_on_host_once(
    *,
    host_name: str,
    host_data: dict,
    old_email: str,
    new_email: str,
    client_uuid: str | None = None,
) -> bool:
    api, inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=_resolve_host_api_token(host_data),
    )
    if not api or not inbound:
        logger.error("Rename client email failed: cannot login to host '%s'.", host_name)
        return False

    inbound_to_modify = api.inbound.get_by_id(inbound.id)
    if not inbound_to_modify or not inbound_to_modify.settings:
        logger.error(
            "Rename client email failed: cannot load inbound for host '%s'.",
            host_name,
        )
        return False
    clients = inbound_to_modify.settings.clients or []
    if not clients:
        logger.error(
            "Rename client email failed: inbound has no clients on host '%s'.",
            host_name,
        )
        return False

    source_client = None
    target_old = str(old_email or "").strip()
    target_uuid = str(client_uuid or "").strip()
    target_new = str(new_email or "").strip()
    if not target_new:
        logger.error("Rename client email failed: empty new email.")
        return False

    for client in clients:
        if target_uuid and str(getattr(client, "id", "")).strip() == target_uuid:
            source_client = client
            break
    if source_client is None:
        for client in clients:
            if str(getattr(client, "email", "")).strip() == target_old:
                source_client = client
                break
    if source_client is None:
        logger.error(
            "Rename client email failed: source client not found on host='%s' old_email='%s' uuid='%s'.",
            host_name,
            target_old,
            target_uuid or "n/a",
        )
        return False

    current_email = str(getattr(source_client, "email", "")).strip()
    if current_email == target_new:
        return True

    for client in clients:
        candidate_email = str(getattr(client, "email", "")).strip()
        candidate_uuid = str(getattr(client, "id", "")).strip()
        if candidate_email == target_new and candidate_uuid != str(getattr(source_client, "id", "")).strip():
            logger.warning(
                "Rename client email skipped: target email already exists on host='%s' email='%s'.",
                host_name,
                target_new,
            )
            return False

    source_client.email = target_new
    source_client.enable = True
    api.inbound.update(inbound.id, inbound_to_modify)
    logger.info(
        "Renamed panel client email on host='%s': '%s' -> '%s'.",
        host_name,
        current_email,
        target_new,
    )
    return True


def _cleanup_failed_subscription_attempt_on_host_once(
    *,
    host_name: str,
    host_data: dict,
    email: str,
    include_white_inbounds: bool,
) -> int:
    api, inbound = login_to_host(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
        inbound_id=host_data["host_inbound_id"],
        api_token=_resolve_host_api_token(host_data),
        prefer_raw_inbound_lookup=True,
    )
    if not api or not inbound:
        logger.warning("Failed-attempt cleanup skipped: cannot login to host '%s'.", host_name)
        return 0

    white_node_inbound_ids = _white_node_inbound_ids_for_panel(host_name)
    primary_inbound_id = _safe_inbound_id(inbound)
    if _host_is_white(host_data) and primary_inbound_id > 0:
        white_node_inbound_ids.add(primary_inbound_id)

    raw_subscription_inbounds = _list_inbounds_raw(api)
    active_subscription_inbounds = _subscription_inbounds(
        api,
        inbound,
        include_white_inbounds=include_white_inbounds,
        white_node_inbound_ids=white_node_inbound_ids,
        raw_inbounds=raw_subscription_inbounds or None,
        host_name=host_name,
    )
    removed_total = 0
    for inbound_index, subscription_inbound in enumerate(active_subscription_inbounds):
        target_email = email if inbound_index == 0 else _secondary_inbound_email(email, subscription_inbound, host_name=host_name)
        removed_count, cleanup_ok = _delete_matching_subscription_client_from_inbound(
            api,
            subscription_inbound,
            client_uuid="",
            sub_id="",
            email=target_email,
        )
        removed_total += removed_count
        if cleanup_ok and removed_count:
            logger.warning(
                "Cleaned failed manual subscription attempt on host='%s' inbound='%s' email='%s'.",
                host_name,
                _inbound_label(subscription_inbound),
                target_email,
            )
        elif not cleanup_ok:
            logger.warning(
                "Failed-attempt cleanup did not complete on host='%s' inbound='%s' email='%s'.",
                host_name,
                _inbound_label(subscription_inbound),
                target_email,
            )
        if removed_count or not cleanup_ok:
            if _delete_detached_client_by_email(api, target_email):
                logger.warning(
                    "Deleted failed-attempt client row on host='%s' email='%s'.",
                    host_name,
                    target_email,
                )
    return removed_total


async def _cleanup_failed_subscription_attempt_on_host(
    *,
    host_name: str,
    host_data: dict,
    email: str,
    include_white_inbounds: bool,
    call_timeout_seconds: float,
) -> int:
    try:
        return await _run_blocking_with_timeout(
            _cleanup_failed_subscription_attempt_on_host_once,
            host_name=host_name,
            host_data=host_data,
            email=email,
            include_white_inbounds=include_white_inbounds,
            timeout=call_timeout_seconds,
            retries=1,
            error_context=f"cleanup_failed_issue:{host_name}",
        )
    except Exception as exc:
        logger.warning(
            "Failed-attempt cleanup raised on host='%s' email='%s': %s",
            host_name,
            email,
            exc,
        )
        return 0


async def create_or_update_key_on_host(
    host_name: str,
    email: str,
    days_to_add: int,
    *,
    months: int = 1,
    target_expiry_ms: int | None = None,
    target_sub_id: str | None = None,
    target_client_uuid: str | None = None,
    total_gb_bytes: int | None = None,
    white_traffic_limit_bytes: int | None = None,
    reset_limited_traffic: bool = False,
    reset_exhausted_limited_traffic: bool = False,
    whitelist_cycle_anchor_ms: int | None = None,
    include_white_inbounds: bool = True,
    require_white_inbound: bool = False,
    manual_super_whitelist_postflight: bool = False,
    panel_call_timeout_seconds: int | float | None = None,
    panel_call_retries: int | None = None,
    max_attempts: int = 1,
    retry_backoff_seconds: tuple[int, ...] = (2, 4, 8),
    failure_context: MutableMapping[str, str] | None = None,
) -> dict | None:
    failure_context = failure_context if failure_context is not None else {}
    host_data = get_host(host_name)
    if not host_data:
        logger.error(f"Workflow failed: Host '{host_name}' not found in the database.")
        _record_panel_issue_failure(failure_context, "host_not_found")
        return None
    safe_include_white_inbounds = (
        should_include_white_subscription_inbounds(host_data, host_name=host_name)
        if include_white_inbounds is None
        else bool(include_white_inbounds)
    )
    settle_whitelist2 = _whitelist2_settle_required(host_name, safe_include_white_inbounds)

    attempts_limit = max(1, int(max_attempts))
    backoffs = tuple(max(0, int(delay)) for delay in retry_backoff_seconds if int(delay) >= 0)
    if not backoffs:
        backoffs = (2, 4, 8)
    call_timeout_seconds = (
        max(float(PANEL_CALL_TIMEOUT_SECONDS), float(panel_call_timeout_seconds))
        if panel_call_timeout_seconds is not None
        else float(PANEL_CALL_TIMEOUT_SECONDS)
    )
    try:
        blocking_call_retries = (
            PANEL_CALL_RETRIES
            if panel_call_retries is None
            else max(1, int(panel_call_retries))
        )
    except (TypeError, ValueError):
        blocking_call_retries = 1

    for attempt in range(1, attempts_limit + 1):
        try:
            operation_kwargs = {
                "host_name": host_name,
                "host_data": host_data,
                "email": email,
                "days_to_add": days_to_add,
                "months": _coerce_positive_months(months),
                "target_expiry_ms": target_expiry_ms,
                "target_sub_id": target_sub_id,
                "target_client_uuid": target_client_uuid,
                "total_gb_bytes": total_gb_bytes,
                "white_traffic_limit_bytes": white_traffic_limit_bytes,
                "reset_limited_traffic": reset_limited_traffic,
                "reset_exhausted_limited_traffic": reset_exhausted_limited_traffic,
                "whitelist_cycle_anchor_ms": whitelist_cycle_anchor_ms,
                "include_white_inbounds": safe_include_white_inbounds,
                "require_white_inbound": bool(require_white_inbound),
                "manual_super_whitelist_postflight": bool(
                    manual_super_whitelist_postflight
                ),
                "failure_context": failure_context,
            }
            if manual_super_whitelist_postflight or settle_whitelist2:
                result = await _run_blocking_once_to_completion(
                    _create_or_update_key_on_host_once,
                    **operation_kwargs,
                )
            else:
                result = await _run_blocking_with_timeout(
                    _create_or_update_key_on_host_once,
                    **operation_kwargs,
                    timeout=call_timeout_seconds,
                    retries=blocking_call_retries,
                    error_context=f"create_or_update_key:{host_name}",
                )
        except Exception as e:
            logger.warning(
                "Key issuance attempt failed on host '%s' (attempt %s/%s): %s",
                host_name,
                attempt,
                attempts_limit,
                e,
            )
            reason = "panel_call_timeout" if "panel_call_timeout" in str(e) else "panel_call_failed"
            _record_panel_issue_failure(failure_context, reason)
            result = None
        if result:
            return result
        if str(failure_context.get("reason") or "").startswith("subscription_identity_"):
            # A read-only refusal never authorizes cleanup, detach, or replay.
            # The caller can schedule a later retry or an explicit repair.
            return None

        if settle_whitelist2:
            # An uncertain WHITELIST2 write may already be present on the child
            # while native reconciliation catches up on the master. Preserve
            # it for the pending transaction; do not delete or replay it.
            return None

        # Unknown/partial attempts may already belong to an existing public
        # subscription even when the local DB has no key yet. Keep panel state
        # for identity-verified retry or explicit repair; never guess a delete.

        if attempt >= attempts_limit:
            break

        # Probe panel health before retry to avoid blind hammering on a dead host.
        try:
            probe_result = await _run_blocking_with_timeout(
                login_to_host_detailed,
                host_url=host_data["host_url"],
                username=host_data["host_username"],
                password=host_data["host_pass"],
                inbound_id=host_data["host_inbound_id"],
                api_token=_resolve_host_api_token(host_data),
                retries=1,
                timeout=call_timeout_seconds,
                error_context=f"retry_probe:{host_name}",
            )
        except Exception:
            _record_panel_issue_failure(failure_context, "retry_probe_failed")
            logger.warning(
                "Key issuance retry probe timed out for host '%s' (attempt %s/%s, timeout=%ss)",
                host_name,
                attempt,
                attempts_limit,
                call_timeout_seconds,
            )
            probe_result = HostLoginResult(
                api=None, inbound=None, error_code="host_timeout", error_message="probe_timeout"
            )
        if probe_result.api and probe_result.inbound:
            logger.warning(
                "Key issuance retry probe succeeded for host '%s' (attempt %s/%s).",
                host_name,
                attempt,
                attempts_limit,
            )
        else:
            _record_panel_issue_failure(failure_context, "retry_probe_failed")
            logger.warning(
                "Key issuance retry probe failed for host '%s' (attempt %s/%s): code=%s error=%s",
                host_name,
                attempt,
                attempts_limit,
                probe_result.error_code,
                probe_result.error_message,
            )

        delay_seconds = backoffs[min(attempt - 1, len(backoffs) - 1)]
        logger.warning(
            "Retrying key issuance for host '%s' in %s seconds (attempt %s/%s).",
            host_name,
            delay_seconds,
            attempt + 1,
            attempts_limit,
        )
        await asyncio.sleep(float(delay_seconds))

    logger.error(
        "Key issuance failed on host '%s' after %s attempts for email '%s'.",
        host_name,
        attempts_limit,
        email,
    )
    _record_panel_issue_failure(failure_context, "panel_call_failed")
    return None


async def create_or_update_standard_subscription_on_hosts(
    host_name: str,
    email: str,
    days_to_add: int,
    months: int,
    *,
    target_expiry_ms: int | None = None,
    whitelist_cycle_anchor_ms: int | None = None,
    max_attempts_per_host: int = 3,
    panel_call_timeout_seconds: int | float | None = None,
) -> dict | None:
    """
    Issue one standard subscription through the configured main XUI panel.

    Multi-node clients are created from that panel's inbound list in
    create_or_update_key_on_host. Separate host records are not panel nodes.
    """
    primary_host_name = str(host_name or "").strip()
    if not primary_host_name:
        return None

    safe_months = _coerce_positive_months(months)
    primary_result = await create_or_update_key_on_host(
        primary_host_name,
        email,
        days_to_add,
        months=safe_months,
        target_expiry_ms=target_expiry_ms,
        include_white_inbounds=True,
        require_white_inbound=True,
        white_traffic_limit_bytes=STANDARD_WHITELIST_TRAFFIC_BYTES,
        whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
        panel_call_timeout_seconds=panel_call_timeout_seconds,
        max_attempts=max_attempts_per_host,
    )
    if primary_result:
        primary_result["months"] = safe_months
    return primary_result


async def rename_client_email_on_host(
    *,
    host_name: str,
    old_email: str,
    new_email: str,
    client_uuid: str | None = None,
    max_attempts: int = 2,
) -> bool:
    host_data = get_host(host_name)
    if not host_data:
        logger.error("Rename client email failed: host '%s' not found.", host_name)
        return False

    attempts = max(1, int(max_attempts or 1))
    for attempt in range(1, attempts + 1):
        try:
            renamed = await _run_blocking_with_timeout(
                _rename_client_email_on_host_once,
                host_name=host_name,
                host_data=host_data,
                old_email=old_email,
                new_email=new_email,
                client_uuid=client_uuid,
                timeout=PANEL_CALL_TIMEOUT_SECONDS,
                retries=PANEL_CALL_RETRIES,
                error_context=f"rename_client_email:{host_name}",
            )
        except Exception as exc:
            logger.warning(
                "Rename client email failed on host='%s' attempt %s/%s: %s",
                host_name,
                attempt,
                attempts,
                exc,
            )
            renamed = False
        if renamed:
            return True
        if attempt < attempts:
            await asyncio.sleep(0.5 * attempt)

    return False


def _enrich_existing_key_result_with_subscription_links(
    *,
    api: Api,
    host_data: Mapping[str, Any],
    inbound: Inbound,
    host_name: str,
    result: Mapping[str, Any],
    subscription_tier: Any,
) -> dict:
    enriched = dict(result or {})
    tier = normalize_subscription_tier(subscription_tier)
    if tier not in {SUBSCRIPTION_TIER_STANDARD, SUBSCRIPTION_TIER_SUPER}:
        return enriched

    sub_id = str(enriched.get("sub_id") or "").strip()
    client_uuid = str(enriched.get("client_uuid") or "").strip()
    email = str(enriched.get("email") or "").strip()
    if not (sub_id or client_uuid or email):
        return enriched

    raw_inbounds = _list_inbounds_raw(api)
    white_node_inbound_ids = _white_node_inbound_ids_for_panel(host_name)
    primary_id = _safe_inbound_id(inbound)
    if _host_is_white(host_data) and primary_id > 0:
        white_node_inbound_ids.add(primary_id)
    stored_key = get_key_by_email(email) or {}
    include_white = not standard_trial_is_ordinary_only(stored_key)
    active_inbounds = _subscription_inbounds(
        api,
        inbound,
        include_white_inbounds=include_white,
        white_node_inbound_ids=white_node_inbound_ids,
        raw_inbounds=raw_inbounds or None,
        host_name=host_name,
        require_migration_targets=include_white,
    )
    if not active_inbounds:
        return enriched

    node_address_by_id, node_address_by_name = _node_public_address_maps(api)
    links = _build_subscription_links_from_panel_clients(
        api,
        host_data,
        sub_id,
        active_inbounds,
        raw_inbounds=None,
        node_address_by_id=node_address_by_id,
        node_address_by_name=node_address_by_name,
        remark_mode="inbound_label",
        client_uuid=client_uuid,
        base_email=email,
    )
    if len(links) >= len(active_inbounds):
        enriched["subscription_links"] = links
    return enriched


def _pending_standard_component_proof(
    raw: Mapping[str, Any], *, email: str, client_uuid: str | None,
    expiry_ms: int, total_bytes: int, reset_days: int, primary: bool = False,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    """Validate fresh client/settings/stat identity without performing an upsert."""
    if not isinstance(raw, Mapping):
        raise TypeError("standard_probe_native_shape")
    clients = _raw_settings_mapping(raw.get("settings")).get("clients")
    stats = raw.get("clientStats")
    if not isinstance(clients, list):
        raise TypeError("standard_probe_native_shape")
    expected_uuid = _normalized_client_uuid(client_uuid)
    matches = [row for row in clients if isinstance(row, Mapping) and (
        (expected_uuid and _normalized_client_uuid(row.get("id") or row.get("uuid")) == expected_uuid)
        or _subscription_email_variant(email, row.get("email", ""))
    )]
    if len({row.get("email") for row in matches}) != len(matches):
        raise ValueError("standard_probe_native_duplicate")
    if not matches:
        return []
    if raw.get("enable") is not True:
        raise ValueError("standard_probe_inbound_disabled")
    if not isinstance(stats, list):
        raise TypeError("standard_probe_native_shape")
    proved = []
    for client in matches:
        uid = _normalized_client_uuid(client.get("id") or client.get("uuid"))
        component_email = client.get("email")
        sid = _extract_client_sub_id(client)
        if (
            not uid or (expected_uuid and uid != expected_uuid)
            or not isinstance(component_email, str) or not _subscription_email_variant(email, component_email)
            or (primary and component_email != email) or not isinstance(sid, str) or not sid.strip()
            or client.get("enable") is not True
            or any(type(client.get(field)) is not int for field in ("totalGB", "expiryTime", "reset"))
            or client["totalGB"] != total_bytes or client["expiryTime"] != expiry_ms or client["reset"] != reset_days
        ):
            raise ValueError("standard_probe_native_identity_or_policy")
        traffic = [row for row in stats if isinstance(row, Mapping) and row.get("email") == component_email]
        if len(traffic) != 1:
            raise ValueError("standard_probe_native_stats_duplicate_or_missing")
        stat = traffic[0]
        if (
            any(type(stat.get(field)) is not int for field in ("total", "expiryTime", "reset", "up", "down"))
            or stat["total"] != total_bytes or stat["expiryTime"] != expiry_ms or stat["reset"] != reset_days
            or stat["up"] < 0 or stat["down"] < 0
            or (total_bytes > 0 and stat["up"] + stat["down"] >= total_bytes)
            or (stat.get("enable") is not True and not (raw.get("id") == 25 and stat.get("enable") is False))
            or ("uuid" in stat and _normalized_client_uuid(stat["uuid"]) != uid)
            or ("subId" in stat and stat["subId"] != sid)
            or any(field in stat and stat[field] != raw.get("id") for field in ("inboundId", "inbound_id"))
        ):
            raise ValueError("standard_probe_native_stats_policy")
        proved.append((client, stat))
    return proved


def _probe_pending_standard_subscription_once(
    host_name: str, email: str, *, target_expiry_ms: int, whitelist_cycle_anchor_ms: int,
) -> dict | None:
    """GET-only proof of an uncertain paid Standard birth; no public/RPC claim."""
    now_ms = int(time.time() * 1000)
    if (
        host_name != "MainPanel" or not isinstance(email, str) or not email.strip() or email != email.strip()
        or type(target_expiry_ms) is not int or target_expiry_ms <= now_ms
        or type(whitelist_cycle_anchor_ms) is not int or not 0 < whitelist_cycle_anchor_ms <= now_ms
    ):
        return None
    host = get_host(host_name)
    token = _resolve_host_api_token(host)
    if not host or not token or not is_host_delivery_active(host):
        return None
    api = _create_api(host["host_url"], host["host_username"], host["host_pass"], token)
    _set_api_request_retries(api, 1)
    deadline = time.monotonic() + 40.0

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("standard_probe_read_deadline")
        return min(4.0, value)

    try:
        inventory = _api_extract_obj(_api_get_json(api, "panel/api/inbounds/list", skip_check=True, timeout=remaining()))
        if not isinstance(inventory, list) or not all(isinstance(raw, Mapping) for raw in inventory):
            return None
        if (not all(type(raw.get("id")) is int and raw["id"] > 0 for raw in inventory)
                or len({raw["id"] for raw in inventory}) != len(inventory)):
            return None
        raw_by_id = {raw["id"]: raw for raw in inventory if type(raw.get("id")) is int and raw["id"] > 0}
        primary_id = _coerce_panel_int(host.get("host_inbound_id"))
        if primary_id <= 0 or primary_id == 25:
            return None
        for iid in dict.fromkeys((primary_id, 22, 23, 24, 25)):
            raw_by_id[iid] = _get_inbound_raw_by_id(api, iid, allow_list_fallback=False, request_timeout=remaining())
            if raw_by_id[iid].get("id") != iid:
                return None
        primary_proof = _pending_standard_component_proof(raw_by_id[primary_id], email=email, client_uuid=None,
                            expiry_ms=target_expiry_ms, total_bytes=0, reset_days=0, primary=True)
        if len(primary_proof) != 1:
            return None
        primary_client = primary_proof[0][0]
        client_uuid = _normalized_client_uuid(primary_client.get("id") or primary_client.get("uuid"))
        if str(uuid.UUID(client_uuid)) != client_uuid.lower():
            return None
        sid = _extract_client_sub_id(primary_client)
        white_expiry, _ = _next_whitelist_reset_deadline_ms(whitelist_cycle_anchor_ms, None, target_expiry_ms, now_ms)
        white_reset = 0 if white_expiry >= target_expiry_ms else MULTI_NODE_LIMIT_RESET_DAYS
        proved_clients = {}
        for iid, raw in raw_by_id.items():
            runtime = _raw_inbound_runtime_namespace(raw, inbound_id=iid)
            if iid != 25 and (_inbound_protocol(runtime) not in SUPPORTED_SUBSCRIPTION_INBOUND_PROTOCOLS
                              or _is_white_subscription_inbound(runtime)):
                continue
            proof = _pending_standard_component_proof(raw, email=email, client_uuid=client_uuid,
                         expiry_ms=white_expiry if iid == 25 else target_expiry_ms,
                         total_bytes=STANDARD_WHITELIST_TRAFFIC_BYTES if iid == 25 else 0,
                         reset_days=white_reset if iid == 25 else 0)
            if iid in (22, 23, 24, 25):
                if len(proof) != 1:
                    return None
                proved_clients[iid] = proof[0]
        if set(proved_clients) != {22, 23, 24, 25}:
            return None
        for iid in (22, 23):
            runtime = _raw_inbound_runtime_namespace(raw_by_id[iid], inbound_id=iid)
            protocol = raw_by_id[iid].get("protocol")
            network = _stream_settings_value(runtime, "network", "")
            hysteria = _as_mapping(_stream_settings_value(runtime, "hysteria_settings", {}))
            canonical_hy2 = protocol == "hysteria2" and network == "hysteria2"
            legacy_hy2 = (protocol == "hysteria" and network == "hysteria"
                          and type(hysteria.get("version")) is int and hysteria["version"] == 2)
            if (not (canonical_hy2 or legacy_hy2) or _stream_settings_value(runtime, "security", "") != "tls"
                    or ("version" in hysteria and (type(hysteria["version"]) is not int or hysteria["version"] != 2))):
                return None
        reality = _raw_inbound_runtime_namespace(raw_by_id[24], inbound_id=24)
        if (reality.protocol != "vless" or _stream_settings_value(reality, "network", "") != "tcp"
                or _stream_settings_value(reality, "security", "") != "reality"
                or reality.stream_settings.reality_settings.get("settings", {}).get("fingerprint") != "firefox"
                or proved_clients[24][0].get("flow") != "xtls-rprx-vision"):
            return None
        white_client, white_stat = proved_clients[25]
        white_raw = raw_by_id[25]
        if (white_raw.get("protocol") != "vless" or white_raw.get("tag") != "whitelist2-clearway-xhttp"
                or white_raw.get("nodeId") != 14
                or white_client.get("flow")):
            return None
        white_runtime = _raw_inbound_runtime_namespace(white_raw, inbound_id=25)
        white_endpoint = _xhttp_external_proxy_endpoint(white_runtime)
        if (white_endpoint is None or not subscription_policy.clearway_client_profile(25, "WHITELIST2", white_endpoint[0])):
            return None
        if white_stat.get("enable") is False and not _whitelist2_standard_native_readback(
            api, white_raw, email=white_client["email"], client_uuid=client_uuid,
            sub_id=_extract_client_sub_id(white_client), expiry_ms=white_expiry, reset_days=white_reset,
            timeout_seconds=remaining(),
        ):
            return None
        nodes = _api_extract_obj(_api_get_json(api, "panel/api/nodes/list", skip_check=True, timeout=remaining()))
        if not isinstance(nodes, list) or not all(isinstance(node, Mapping) for node in nodes):
            return None
        if len({node.get("id") for node in nodes}) != len(nodes):
            return None
        by_id = {node["id"]: node["address"] for node in nodes if type(node.get("id")) is int
                 and node.get("enable") is True and isinstance(node.get("address"), str) and node["address"]}
        links, runtimes, secondary = [], [], []
        expected_labels = ("ROMANIA Hysteria2", "ROMANIA2 Hysteria2",
                           "NEW3-IP10 VLESS TCP REALITY Vision-Mozilla-FF", "WHITELIST2")
        for iid, expected_label in zip((22, 23, 24, 25), expected_labels, strict=True):
            raw = raw_by_id[iid]
            runtime = _raw_inbound_runtime_namespace(raw, inbound_id=iid)
            client = proved_clients[iid][0]
            if runtime.node_id and runtime.node_id not in by_id:
                return None
            label = _inbound_label(runtime)
            link = get_connection_string(runtime, client_uuid, host["host_url"], label,
                                        client=client, node_address_by_id=by_id)
            if not link or label != expected_label:
                return None
            parsed = urlparse(link)
            credential = str(_client_config_value(client, ("auth", "password"), "") or "").strip() if iid in (22, 23) else client_uuid
            if (parsed.scheme != ("hysteria2" if iid in (22, 23) else "vless")
                    or unquote(parsed.username or "") != credential or not credential):
                return None
            links.append(link)
            runtimes.append(runtime)
            secondary.append({"inbound_id": iid, "label": label, "email": client["email"],
                              "expiry_timestamp_ms": client["expiryTime"]})
        links = patch_subscription_links_for_inbounds(links, runtimes, node_address_by_id=by_id, remark_mode="inbound_label")
        for iid, link in zip((22, 23, 24, 25), links, strict=False):
            parsed = urlparse(link)
            client = proved_clients[iid][0]
            credential = str(_client_config_value(client, ("auth", "password"), "") or "").strip() if iid in (22, 23) else client_uuid
            if (parsed.scheme != ("hysteria2" if iid in (22, 23) else "vless")
                    or unquote(parsed.username or "") != credential):
                return None
            query = parse_qs(parsed.query)
            if iid == 24 and (query.get("fp") != ["firefox"] or query.get("flow") != ["xtls-rprx-vision"]):
                return None
            if iid == 25:
                extra = json.loads(query.get("extra", [""])[0])
                if (any(query.get(name) != [value] for name, value in {
                    "type": "xhttp", "security": "tls", "encryption": "none", "mode": "packet-up",
                    "path": _stream_settings_value(white_runtime, "xhttp_settings", {}).get("path"),
                    "host": white_endpoint[0], "sni": white_endpoint[0],
                }.items()) or not isinstance(extra, dict) or extra.get("uplinkHTTPMethod") != "GET"
                        or extra.get("uplinkDataPlacement") != "header" or extra.get("uplinkChunkSize") != 4096
                        or extra.get("scMaxEachPostBytes") != 32768):
                    return None
        labels = [unquote(urlparse(link).fragment) for link in links]
        if (len(links) != 4 or len(set(labels)) != 4 or labels != [_inbound_label(runtime) for runtime in runtimes]
                or _subscription_endpoint_mismatches(links, runtimes, node_address_by_id=by_id)):
            return None
        if min(target_expiry_ms, white_expiry) <= int(time.time() * 1000) or deadline <= time.monotonic():
            return None
        base_url = _resolve_subscription_base_url(host)
        return {"client_uuid": client_uuid, "email": email, "expiry_timestamp_ms": target_expiry_ms,
                "whitelist_reset_timestamp_ms": white_expiry, "connection_string": links[2],
                "subscription_link": _build_subscription_link(base_url, sid) if base_url else None,
                "host_name": host_name, "sub_id": sid, "secondary_inbounds": secondary,
                "secondary_failures": [], "white_cleanup_inbounds": [], "subscription_links": links,
                "standard_readonly_recovery_verified": True}
    except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
        return None


async def probe_pending_standard_subscription(
    host_name: str, email: str, *, target_expiry_ms: int, whitelist_cycle_anchor_ms: int,
) -> dict | None:
    """Recover only a fully proved pending Standard birth; otherwise keep it pending."""
    try:
        return await _run_blocking_once_to_completion(
            _probe_pending_standard_subscription_once, host_name, email,
            target_expiry_ms=target_expiry_ms, whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
        )
    except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _standard_birth_namespace_is_empty_once(host_name: str, email: str) -> bool:
    """Fail closed unless the entire reviewed namespace is observed empty."""
    if host_name != "MainPanel" or not isinstance(email, str) or not email.strip() or email != email.strip():
        return False
    host = get_host(host_name)
    token = _resolve_host_api_token(host)
    if not host or not token or not is_host_delivery_active(host) or get_key_by_email(email):
        return False
    api = _create_api(host["host_url"], host["host_username"], host["host_pass"], token)
    _set_api_request_retries(api, 1)
    deadline = time.monotonic() + 20.0

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("standard_namespace_read_deadline")
        return min(4.0, value)

    def empty(rows):
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) and isinstance(row.get("email"), str)
                                               and row["email"] for row in rows):
            return False
        return not any(_subscription_email_variant(email, row["email"])
                       or _subscription_email_variant(row["email"], email) for row in rows)

    try:
        central = _api_extract_obj(_api_get_json(api, "panel/api/clients/list", skip_check=True, timeout=remaining()))
        if isinstance(central, Mapping):
            central = central.get("clients")
        if not empty(central):
            return False
        inventory = _api_extract_obj(_api_get_json(api, "panel/api/inbounds/list", skip_check=True, timeout=remaining()))
        if (not isinstance(inventory, list) or not all(isinstance(raw, Mapping) and type(raw.get("id")) is int
                                                     and raw["id"] > 0 for raw in inventory)
                or len({raw["id"] for raw in inventory}) != len(inventory)):
            return False
        by_id = {raw["id"]: raw for raw in inventory}
        if not {22, 23, 24, 25}.issubset(by_id):
            return False
        for raw in inventory:
            if (not empty(_raw_settings_mapping(raw.get("settings")).get("clients"))
                    or not empty(raw.get("clientStats"))):
                return False
        white = by_id[25]
        if (white.get("enable") is not True or white.get("nodeId") != 14
                or white.get("tag") != "whitelist2-clearway-xhttp" or white.get("protocol") != "vless"):
            return False
        target = _resolve_whitelist2_native_child_target(api, white, timeout_seconds=remaining() / 2)
        if target is None:
            return False
        url, child_token, verify = target
        child = _api_extract_obj(_whitelist2_read_json_once(url, api_token=child_token, tls_verify=verify,
                                                           timeout_seconds=remaining()))
        if not isinstance(child, list) or not all(isinstance(raw, Mapping) for raw in child):
            return False
        white_child = [raw for raw in child if raw.get("tag") == "whitelist2-clearway-xhttp"]
        if len(white_child) != 1 or white_child[0].get("enable") is not True:
            return False
        if not all(empty(_raw_settings_mapping(raw.get("settings")).get("clients"))
                   and empty(raw.get("clientStats")) for raw in child):
            return False
        return deadline > time.monotonic()
    except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
        return False


async def standard_birth_namespace_is_empty(host_name: str, email: str) -> bool:
    """Permit one first birth only after complete GET-only absence evidence."""
    try:
        return await _run_blocking_once_to_completion(_standard_birth_namespace_is_empty_once, host_name, email)
    except (RequestException, RuntimeError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
        return False


async def get_existing_key_by_email_on_host(
    host_name: str,
    email: str,
    *,
    target_expiry_ms: int | None = None,
    subscription_tier: Any = None,
) -> dict | None:
    """
    Reconcile an already existing panel client by email without creating a new record.
    """
    host_data = get_host(host_name)
    if not host_data:
        return None

    try:
        api, inbound = await _run_blocking_with_timeout(
            login_to_host,
            host_url=host_data["host_url"],
            username=host_data["host_username"],
            password=host_data["host_pass"],
            inbound_id=host_data["host_inbound_id"],
            api_token=_resolve_host_api_token(host_data),
            prefer_raw_inbound_lookup=True,
            timeout=PANEL_CALL_TIMEOUT_SECONDS,
            retries=PANEL_CALL_RETRIES,
            error_context=f"get_existing_key_login:{host_name}",
        )
    except (
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        return None
    if not api or not inbound:
        return None

    candidate_inbounds = [inbound]
    try:
        candidate_inbounds = _subscription_inbounds(
            api,
            inbound,
            include_white_inbounds=True,
            white_node_inbound_ids=_white_node_inbound_ids_for_panel(host_name),
            raw_inbounds=_list_inbounds_raw(api) or None,
            host_name=host_name,
        ) or [inbound]
    except (
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        candidate_inbounds = [inbound]
    if normalize_subscription_tier(subscription_tier) == SUBSCRIPTION_TIER_SUPER:
        candidate_inbounds = sorted(
            candidate_inbounds,
            key=lambda candidate: _is_white_subscription_inbound(
                candidate,
                _white_node_inbound_ids_for_panel(host_name),
            ),
        )

    client = None
    client_inbound = inbound
    for candidate_inbound in candidate_inbounds:
        inbound_id = _safe_inbound_id(candidate_inbound)
        if inbound_id <= 0:
            continue
        try:
            client = await _run_blocking_with_timeout(
                find_inbound_client_minimal,
                api,
                inbound_id,
                email=email,
                timeout=PANEL_CALL_TIMEOUT_SECONDS,
                retries=1,
                error_context=f"find_existing_key_email:{host_name}:{inbound_id}",
            )
        except (
            RequestException,
            RuntimeError,
            TimeoutError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            client = None
        if client:
            client_inbound = candidate_inbound
            break
    if not client:
        return None

    try:
        result = await _run_blocking_with_timeout(
            _build_key_result_from_client,
            api=api,
            inbound=client_inbound,
            host_name=host_name,
            host_data=host_data,
            email=str(getattr(client, "email", "") or email),
            client=client,
            target_expiry_ms=target_expiry_ms,
            subscription_tier=subscription_tier,
            timeout=PANEL_CALL_TIMEOUT_SECONDS,
            retries=1,
            error_context=f"build_existing_key_result:{host_name}",
        )
    except (
        RequestException,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        return None
    if result:
        try:
            result = await _run_blocking_with_timeout(
                _enrich_existing_key_result_with_subscription_links,
                api=api,
                host_data=host_data,
                inbound=inbound,
                host_name=host_name,
                result=result,
                subscription_tier=subscription_tier,
                timeout=PANEL_CALL_TIMEOUT_SECONDS,
                retries=1,
                error_context=f"enrich_existing_key_subscription:{host_name}",
            )
        except (
            RequestException,
            RuntimeError,
            TimeoutError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            pass
    if result:
        logger.warning(
            "Reconciled existing panel client on host '%s' for email '%s'.",
            host_name,
            email,
        )
    return result


async def get_existing_key_by_uuid_on_host(
    host_name: str,
    client_uuid: str,
    *,
    target_expiry_ms: int | None = None,
    subscription_tier: Any = None,
) -> dict | None:
    """Reconcile a panel client by UUID — more reliable than email after rename."""
    host_data = get_host(host_name)
    if not host_data:
        return None

    try:
        api, inbound = await _run_blocking_with_timeout(
            login_to_host,
            host_url=host_data["host_url"],
            username=host_data["host_username"],
            password=host_data["host_pass"],
            inbound_id=host_data["host_inbound_id"],
            api_token=_resolve_host_api_token(host_data),
            prefer_raw_inbound_lookup=True,
            timeout=PANEL_CALL_TIMEOUT_SECONDS,
            retries=PANEL_CALL_RETRIES,
            error_context=f"get_key_by_uuid_login:{host_name}",
        )
    except Exception:
        return None
    if not api or not inbound:
        return None

    candidate_inbounds = [inbound]
    try:
        candidate_inbounds = _subscription_inbounds(
            api,
            inbound,
            include_white_inbounds=True,
            white_node_inbound_ids=_white_node_inbound_ids_for_panel(host_name),
            raw_inbounds=_list_inbounds_raw(api) or None,
            host_name=host_name,
        ) or [inbound]
    except Exception:
        candidate_inbounds = [inbound]
    if normalize_subscription_tier(subscription_tier) == SUBSCRIPTION_TIER_SUPER:
        candidate_inbounds = sorted(
            candidate_inbounds,
            key=lambda candidate: _is_white_subscription_inbound(
                candidate,
                _white_node_inbound_ids_for_panel(host_name),
            ),
        )

    client = None
    client_inbound = inbound
    for candidate_inbound in candidate_inbounds:
        inbound_id = _safe_inbound_id(candidate_inbound)
        if inbound_id <= 0:
            continue
        try:
            client = await _run_blocking_with_timeout(
                find_inbound_client_minimal,
                api,
                inbound_id,
                client_uuid=client_uuid,
                timeout=PANEL_CALL_TIMEOUT_SECONDS,
                retries=1,
                error_context=f"find_existing_key_uuid:{host_name}:{inbound_id}",
            )
        except (
            RequestException,
            RuntimeError,
            TimeoutError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            client = None
        if client:
            client_inbound = candidate_inbound
            break
    if not client:
        return None

    try:
        email = str(getattr(client, "email", "") or "")
        result = await _run_blocking_with_timeout(
            _build_key_result_from_client,
            api=api,
            inbound=client_inbound,
            host_name=host_name,
            host_data=host_data,
            email=email,
            client=client,
            target_expiry_ms=target_expiry_ms,
            subscription_tier=subscription_tier,
            timeout=PANEL_CALL_TIMEOUT_SECONDS,
            retries=1,
            error_context=f"build_key_by_uuid:{host_name}",
        )
    except Exception:
        return None
    if result:
        try:
            result = await _run_blocking_with_timeout(
                _enrich_existing_key_result_with_subscription_links,
                api=api,
                host_data=host_data,
                inbound=inbound,
                host_name=host_name,
                result=result,
                subscription_tier=subscription_tier,
                timeout=PANEL_CALL_TIMEOUT_SECONDS,
                retries=1,
                error_context=f"enrich_existing_key_by_uuid_subscription:{host_name}",
            )
        except (
            RequestException,
            RuntimeError,
            TimeoutError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            pass
    if result:
        logger.warning(
            "Reconciled panel client by UUID on host '%s' uuid='%s'.",
            host_name,
            client_uuid,
        )
    return result


async def find_existing_key_by_email_with_fallback(
    host_name: str,
    email: str,
    *,
    target_expiry_ms: int | None = None,
    subscription_tier: Any = None,
    max_hosts: int = 3,
) -> dict | None:
    """
    Look up an existing panel client by email across primary + fallback hosts.
    """
    from shop_bot.data_manager.database import get_eligible_standard_hosts

    ordered_host_names: list[str] = []

    primary = str(host_name or "").strip()
    if primary:
        ordered_host_names.append(primary)

    for host in get_eligible_standard_hosts(exclude_host_names={primary}, require_plans=False):
        candidate = str(host.get("host_name") or "").strip()
        if not candidate or candidate in ordered_host_names:
            continue
        ordered_host_names.append(candidate)

    if max_hosts > 0:
        ordered_host_names = ordered_host_names[:max_hosts]

    for candidate_host in ordered_host_names:
        result = await get_existing_key_by_email_on_host(
            candidate_host,
            email,
            target_expiry_ms=target_expiry_ms,
            subscription_tier=subscription_tier,
        )
        if result:
            return result

    return None


async def get_key_details_from_host(key_data: dict) -> dict | None:
    host_name = key_data.get("host_name")
    if not host_name:
        logger.error(f"Could not get key details: host_name is missing for key_id {key_data.get('key_id')}")
        return None

    def _normalize_candidates(raw: str) -> list[str]:
        raw = (raw or "").strip()
        if not raw:
            return []
        cands: list[str] = []

        # Common legacy pattern: "Name ➜ сервер 1"
        if "➜" in raw:
            cands.append(raw.split("➜", 1)[0].strip())

        cands.append(re.sub(r"\s*➜\s*сервер\s*\d+\s*$", "", raw, flags=re.IGNORECASE).strip())
        cands.append(re.sub(r"\s*[-\u2013\u2014]\s*сервер\s*\d+\s*$", "", raw, flags=re.IGNORECASE).strip())

        # De-dup, keep order, drop empties and original
        seen = {raw}
        out: list[str] = []
        for c in cands:
            c = re.sub(r"\s+", " ", c).strip()
            if not c or c in seen:
                continue
            seen.add(c)
            out.append(c)
        return out

    host_db_data = get_host(host_name.strip())
    if not host_db_data:
        # Self-heal: try to map legacy host_name to current host names in DB.
        for cand in _normalize_candidates(host_name):
            host_db_data = get_host(cand)
            if host_db_data:
                logger.warning(
                    "Key %s uses legacy host_name '%s' which is not in DB; using '%s' instead.",
                    key_data.get("key_id"),
                    host_name,
                    cand,
                )
                try:
                    from shop_bot.data_manager.database import update_key_host_name

                    if key_data.get("key_id") is not None:
                        update_key_host_name(int(key_data["key_id"]), cand)
                except Exception as e:
                    logger.warning("Failed to update key host_name in DB for key %s: %s", key_data.get("key_id"), e)
                host_name = cand
                break

    if not host_db_data:
        logger.error("Could not get key details: Host '%s' not found in the database.", host_name)
        return None

    try:
        api, inbound = await _run_blocking_with_timeout(
            login_to_host,
            host_url=host_db_data["host_url"],
            username=host_db_data["host_username"],
            password=host_db_data["host_pass"],
            inbound_id=host_db_data["host_inbound_id"],
            api_token=_resolve_host_api_token(host_db_data),
            timeout=PANEL_CALL_TIMEOUT_SECONDS,
            retries=PANEL_CALL_RETRIES,
            error_context="get_key_details_login:" + host_name,
        )
    except Exception:
        return None
    if not api or not inbound:
        return None

    connection_string = get_connection_string(
        inbound, key_data["xui_client_uuid"], host_db_data["host_url"], remark=host_name
    )

    subscription_link = None
    subscription_base_url = _resolve_subscription_base_url(host_db_data)
    if subscription_base_url:
        sub_id = str(key_data.get("panel_sub_id") or "").strip()
        if not sub_id:
            try:
                sub_id = await _run_blocking_with_timeout(
                    _get_client_sub_id_from_panel,
                    api,
                    inbound.id,
                    email=key_data.get("key_email"),
                    client_uuid=key_data.get("xui_client_uuid"),
                    timeout=PANEL_CALL_TIMEOUT_SECONDS,
                    retries=1,
                    error_context="get_sub_id:" + host_name,
                )
            except (
                RequestException,
                RuntimeError,
                TimeoutError,
                ValueError,
                TypeError,
                KeyError,
                AttributeError,
            ):
                sub_id = None
        if sub_id:
            subscription_link = _build_subscription_link(subscription_base_url, sub_id)
        else:
            logger.warning(
                "Failed to build subscription link for key %s on host '%s': subId not found.",
                key_data.get("key_id"),
                host_name,
            )

    return {"connection_string": connection_string, "subscription_link": subscription_link}


def _subscription_email_variant(base_email: str, candidate_email: str) -> bool:
    base_local, base_separator, base_domain = _normalized_client_email(base_email).rpartition("@")
    candidate_local, candidate_separator, candidate_domain = _normalized_client_email(candidate_email).rpartition("@")
    if not base_separator or not candidate_separator or base_domain != candidate_domain:
        return False
    return candidate_local == base_local or candidate_local.startswith(base_local + "-")


def _delete_subscription_components_on_host_once(
    *,
    host_name: str,
    host_data: Mapping[str, Any],
    client_email: str,
    client_uuid: str,
    panel_sub_id: str,
) -> bool:
    api, primary_inbound = login_to_host(
        host_url=str(host_data["host_url"]),
        username=str(host_data["host_username"]),
        password=str(host_data["host_pass"]),
        inbound_id=int(host_data["host_inbound_id"]),
        api_token=_resolve_host_api_token(host_data),
        prefer_raw_inbound_lookup=True,
    )
    if not api or not primary_inbound:
        logger.error("Cannot delete subscription components: login failed for host=%s", host_name)
        return False

    raw_inbounds = _list_inbounds_raw(api)
    if not raw_inbounds:
        logger.error("Cannot delete subscription components: inbound scan is empty for host=%s", host_name)
        return False
    normalized_uuid = _normalized_client_uuid(client_uuid)
    normalized_sub_id = str(panel_sub_id or "").strip()

    def _identity_state(raw_client: Mapping[str, Any]) -> tuple[bool, bool]:
        candidate_email = str(raw_client.get("email") or "").strip()
        candidate_uuid = _normalized_client_uuid(raw_client.get("uuid") or raw_client.get("id"))
        candidate_sub_id = str(_extract_client_sub_id(raw_client) or "").strip()
        email_variant = _subscription_email_variant(client_email, candidate_email)
        if normalized_uuid:
            if candidate_uuid == normalized_uuid:
                return True, False
            return False, email_variant
        if normalized_sub_id:
            if candidate_sub_id == normalized_sub_id:
                return True, False
            return False, email_variant
        return email_variant, False

    def _central_clients() -> list[dict[str, Any]] | None:
        try:
            payload = _api_get_json(api, "panel/api/clients/list", skip_check=True)
        except Exception:
            logger.exception("Cannot list centralized clients during deletion host=%s", host_name)
            return None
        raw_clients = _api_extract_obj(payload)
        if isinstance(raw_clients, Mapping):
            raw_clients = raw_clients.get("clients")
        if not isinstance(raw_clients, list):
            return None
        return [dict(item) for item in raw_clients if isinstance(item, Mapping)]

    central_clients_before = _central_clients()
    if central_clients_before is None:
        return False
    central_emails: set[str] = set()
    for central_client in central_clients_before:
        matches, conflicts = _identity_state(central_client)
        if conflicts:
            logger.error("Refusing deletion after centralized identity conflict host=%s", host_name)
            return False
        if matches:
            central_email = str(central_client.get("email") or "").strip()
            if not central_email:
                return False
            central_emails.add(central_email)

    candidates: list[tuple[int, str]] = []
    seen_candidates: set[tuple[int, str]] = set()
    for raw_inbound in raw_inbounds:
        inbound_id = _coerce_panel_int(raw_inbound.get("id"))
        if inbound_id <= 0:
            continue
        try:
            detailed_raw = _get_inbound_raw_by_id(api, inbound_id, allow_list_fallback=False)
            raw_clients = list(_iter_client_dicts_from_settings(detailed_raw.get("settings")))
        except Exception:
            logger.exception(
                "Cannot delete subscription components: inbound read failed host=%s inbound_id=%s",
                host_name,
                inbound_id,
            )
            return False
        inbound_candidates: list[tuple[int, str]] = []
        for raw_client in raw_clients:
            candidate_email = str(raw_client.get("email") or "").strip()
            identity_matches, identity_conflict = _identity_state(raw_client)
            if identity_conflict:
                logger.error(
                    "Refusing deletion after inbound identity conflict host=%s inbound_id=%s",
                    host_name,
                    inbound_id,
                )
                return False
            if identity_matches:
                if not candidate_email:
                    return False
                inbound_candidates.append((inbound_id, candidate_email))
        if len(inbound_candidates) > 1:
            logger.error(
                "Refusing ambiguous subscription deletion host=%s inbound_id=%s matches=%s",
                host_name,
                inbound_id,
                len(inbound_candidates),
            )
            return False
        for candidate in inbound_candidates:
            if candidate not in seen_candidates:
                candidates.append(candidate)
                seen_candidates.add(candidate)

    for inbound_id, candidate_email in candidates:
        if not _detach_client_from_inbound_by_email(api, inbound_id, candidate_email):
            return False

    delete_emails = central_emails | {
        candidate_email for _inbound_id, candidate_email in candidates
    }
    for candidate_email in sorted(delete_emails):
        if not _delete_detached_client_by_email(api, candidate_email):
            return False

    for raw_inbound in raw_inbounds:
        inbound_id = _coerce_panel_int(raw_inbound.get("id"))
        if inbound_id <= 0:
            continue
        try:
            verify_raw = _get_inbound_raw_by_id(api, inbound_id, allow_list_fallback=False)
            verify_clients = list(_iter_client_dicts_from_settings(verify_raw.get("settings")))
        except (
            OSError,
            RequestException,
            RuntimeError,
            TypeError,
            ValueError,
            KeyError,
            AttributeError,
        ):
            return False
        for raw_client in verify_clients:
            identity_matches, identity_conflict = _identity_state(raw_client)
            if identity_matches or identity_conflict:
                logger.error(
                    "Subscription deletion verification found a remaining component host=%s inbound_id=%s",
                    host_name,
                    inbound_id,
                )
                return False
    central_clients_after = _central_clients()
    if central_clients_after is None:
        return False
    if any(any(_identity_state(client)) for client in central_clients_after):
        logger.error("Subscription deletion verification found a centralized client host=%s", host_name)
        return False
    logger.info(
        "Deleted and verified %s subscription component(s) and %s centralized client(s) on host=%s email=%s",
        len(candidates),
        len(delete_emails),
        host_name,
        client_email,
    )
    return True


async def delete_client_on_host(host_name: str, client_email: str) -> bool:
    host_data = get_host(host_name)
    if not host_data:
        logger.error("Cannot delete client: host '%s' not found.", host_name)
        return False
    key = get_key_by_email(client_email)
    if not key:
        logger.warning("Client with email '%s' is already absent from the application DB.", client_email)
        return True
    try:
        return bool(
            await _run_blocking_with_timeout(
                _delete_subscription_components_on_host_once,
                host_name=host_name,
                host_data=host_data,
                client_email=client_email,
                client_uuid=str(key.get("xui_client_uuid") or "").strip(),
                panel_sub_id=str(key.get("panel_sub_id") or "").strip(),
                timeout=PANEL_CALL_TIMEOUT_SECONDS,
                retries=PANEL_CALL_RETRIES,
                error_context=f"delete_subscription:{host_name}",
            )
        )
    except Exception:
        logger.exception("Failed to delete subscription components host=%s email=%s", host_name, client_email)
        return False


async def create_or_update_key_with_fallback(
    host_name: str,
    email: str,
    days_to_add: int,
    max_retries: int = 3,
    *,
    months: int = 1,
    target_expiry_ms: int | None = None,
    retry_attempts_per_host: int = 1,
    include_white_inbounds: bool = True,
    white_traffic_limit_bytes: int | None = STANDARD_WHITELIST_TRAFFIC_BYTES,
    whitelist_cycle_anchor_ms: int | None = None,
    panel_call_timeout_seconds: int | float | None = None,
) -> dict | None:
    """
    Try to create key on specified host with automatic fallback to other hosts.
    Implements resilient key creation with up to max_retries attempts.

    Flow:
    1. Try to create key on the requested host
    2. If fails, select next least loaded host from available hosts
    3. Repeat until success or max_retries reached

    Args:
        host_name: Primary host to try first
        email: Client email for the key
        days_to_add: Number of days to add to expiry
        max_retries: Maximum number of hosts to try (default: 3)

    Returns:
        Dict with key info (including actual host_name used), or None if all attempts failed
    """
    from shop_bot.data_manager.database import get_eligible_standard_hosts

    # First attempt: try on specified host
    logger.info(f"Attempt 1: Creating key on host '{host_name}'")
    result = await create_or_update_key_on_host(
        host_name,
        email,
        days_to_add,
        months=_coerce_positive_months(months),
        target_expiry_ms=target_expiry_ms,
        include_white_inbounds=include_white_inbounds,
        require_white_inbound=include_white_inbounds,
        white_traffic_limit_bytes=white_traffic_limit_bytes,
        whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
        panel_call_timeout_seconds=panel_call_timeout_seconds,
        max_attempts=retry_attempts_per_host,
    )

    if result:
        return result

    logger.warning(f"Failed to create key on host '{host_name}', trying fallback...")

    attempted_hosts = [host_name]
    attempts = 1
    fallback_hosts = get_eligible_standard_hosts(
        exclude_host_names={host_name},
        require_plans=False,
    )

    while attempts < max_retries and len(attempted_hosts) <= len(fallback_hosts):
        available_hosts = [h for h in fallback_hosts if h["host_name"] not in attempted_hosts]
        if not available_hosts:
            break

        fallback_host = available_hosts[0]

        attempts += 1
        fallback_host_name = fallback_host["host_name"]
        attempted_hosts.append(fallback_host_name)

        logger.info(f"Attempt {attempts}: Trying fallback host '{fallback_host_name}'")
        result = await create_or_update_key_on_host(
            fallback_host_name,
            email,
            days_to_add,
            months=_coerce_positive_months(months),
            target_expiry_ms=target_expiry_ms,
            include_white_inbounds=include_white_inbounds,
            require_white_inbound=include_white_inbounds,
            white_traffic_limit_bytes=white_traffic_limit_bytes,
            whitelist_cycle_anchor_ms=whitelist_cycle_anchor_ms,
            panel_call_timeout_seconds=panel_call_timeout_seconds,
            max_attempts=retry_attempts_per_host,
        )

        if result:
            logger.info(f"SUCCESS: Key created on fallback host '{fallback_host_name}'")
            return result

    logger.error(f"FAILED: Could not create key after {attempts} attempts on hosts: {attempted_hosts}")
    return None
