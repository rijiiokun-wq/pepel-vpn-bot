"""
Blitz API client for Hysteria 2 user management.

Provides async methods to create, update, delete users and
retrieve connection URIs from a Blitz panel instance.
"""

import json
import logging
import os
import time
from typing import Dict

import aiohttp

from shop_bot.config import is_truthy as _is_truthy
from shop_bot.data_manager.database import get_setting

logger = logging.getLogger(__name__)

# Timeout for all Blitz API requests (seconds)
BLITZ_API_TIMEOUT = 15
BLITZ_AUTH_CACHE_TTL_SECONDS = 600
BLITZ_AUTH_CACHE_MAX_SIZE = max(
    8,
    int(str(os.environ.get("BLITZ_AUTH_CACHE_MAX_SIZE", "256")).strip() or "256"),
)

# Sentinel host_name used in vpn_keys table for Blitz-issued keys
BLITZ_HOST_NAME = "blitz_hysteria2"
_AUTH_CACHE: dict[str, tuple[dict, float]] = {}


def _prune_auth_cache(now: float) -> None:
    expired_keys = [
        key
        for key, (_ctx, expires_at) in _AUTH_CACHE.items()
        if float(expires_at) <= now
    ]
    for key in expired_keys:
        _AUTH_CACHE.pop(key, None)

    overflow = len(_AUTH_CACHE) - BLITZ_AUTH_CACHE_MAX_SIZE
    if overflow <= 0:
        return

    sorted_keys = sorted(_AUTH_CACHE, key=lambda key: _AUTH_CACHE[key][1])
    for key in sorted_keys[:overflow]:
        _AUTH_CACHE.pop(key, None)


def _get_ssl_verify_flag() -> bool:
    """
    Return SSL verification mode for aiohttp.

    By default certificate verification is enabled.
    Set bot_settings.blitz_insecure_tls=true only for self-signed certs.
    """
    return not _is_truthy(get_setting("blitz_insecure_tls"))


async def _request_with_reauth(
    session: aiohttp.ClientSession,
    cfg: dict,
    method: str,
    url: str,
    auth_ctx: dict,
    **kwargs,
) -> tuple[int, str]:
    req_headers = dict(auth_ctx.get("headers") or {})
    if kwargs.get("headers"):
        req_headers.update(kwargs.pop("headers"))

    async with session.request(
        method,
        url,
        headers=req_headers,
        cookies=auth_ctx.get("cookies"),
        ssl=_get_ssl_verify_flag(),
        **kwargs,
    ) as resp:
        status = resp.status
        body = await resp.text()

    if status not in (401, 403):
        return status, body

    refreshed_auth_ctx = await _get_auth_context(cfg, force_refresh=True)
    if not refreshed_auth_ctx:
        return status, body

    retry_headers = dict(refreshed_auth_ctx.get("headers") or {})
    if kwargs.get("headers"):
        retry_headers.update(kwargs["headers"])

    async with session.request(
        method,
        url,
        headers=retry_headers,
        cookies=refreshed_auth_ctx.get("cookies"),
        ssl=_get_ssl_verify_flag(),
        **kwargs,
    ) as resp:
        return resp.status, await resp.text()


def _parse_json_dict(text: str) -> dict | None:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _get_blitz_config() -> Dict[str, str] | None:
    """Read Blitz connection settings from bot_settings."""
    url = get_setting("blitz_url")
    username = get_setting("blitz_username")
    password = get_setting("blitz_password")
    api_token = get_setting("blitz_api_token")

    has_api_token = bool((api_token or "").strip())
    has_login_pair = bool(username and password)
    if not url or (not has_api_token and not has_login_pair):
        logger.error("Blitz API is not configured. Set blitz_url + blitz_api_token (preferred) or blitz_username/blitz_password.")
        return None

    return {
        "url": url.rstrip("/"),
        "username": username,
        "password": password,
        "api_token": api_token,
    }


async def _get_auth_context(cfg: dict, force_refresh: bool = False) -> dict | None:
    """
    Build auth context for Blitz requests.
    Preferred mode: API token in Authorization header.
    Fallback mode: session cookie from /login form.
    """
    cache_key = f"{cfg['url']}|{cfg.get('api_token') or cfg.get('username')}"
    now = time.monotonic()
    _prune_auth_cache(now)
    if not force_refresh:
        cached = _AUTH_CACHE.get(cache_key)
        if cached and cached[1] > now:
            return cached[0]

    api_token = (cfg.get("api_token") or "").strip()
    if api_token:
        auth_ctx = {"headers": {"Authorization": api_token}, "cookies": None}
        _AUTH_CACHE[cache_key] = (auth_ctx, now + BLITZ_AUTH_CACHE_TTL_SECONDS)
        _prune_auth_cache(now)
        return auth_ctx

    login_url = f"{cfg['url']}/login"
    form_payload = {"username": cfg["username"], "password": cfg["password"]}

    try:
        timeout = aiohttp.ClientTimeout(total=BLITZ_API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                login_url,
                data=form_payload,
                allow_redirects=False,
                ssl=_get_ssl_verify_flag()
            ) as resp:
                if resp.status not in (200, 302, 303, 307):
                    logger.error(f"Blitz login failed with status {resp.status}.")
                    return None

                cookie = resp.cookies.get("session_id") or resp.cookies.get("session")
                if cookie:
                    auth_ctx = {"headers": {}, "cookies": {cookie.key: cookie.value}}
                    _AUTH_CACHE[cache_key] = (auth_ctx, now + BLITZ_AUTH_CACHE_TTL_SECONDS)
                    _prune_auth_cache(now)
                    return auth_ctx

                logger.error("Blitz login succeeded but no session cookie was returned.")
                return None
    except Exception as e:
        logger.error(f"Blitz login request error: {e}")
        return None


async def create_user(
    username: str,
    expiry_days: int,
    traffic_limit_gb: int = 0,
    unlimited: bool = True,
) -> Dict | None:
    """
    Create a new user on the Blitz panel.

    Args:
        username: Unique username for the client.
        expiry_days: Number of days until the account expires.
        traffic_limit_gb: Traffic limit in GB (0 = unlimited when unlimited=True).
        unlimited: If True, give unlimited traffic.

    Returns:
        Dict with user data on success, None on failure.
    """
    cfg = _get_blitz_config()
    if not cfg:
        return None

    auth_ctx = await _get_auth_context(cfg)
    if not auth_ctx:
        return None

    url = f"{cfg['url']}/api/v1/users/"
    payload = {
        "username": username,
        "expiration_days": expiry_days,
        # Blitz API expects traffic_limit in GB, not bytes.
        "traffic_limit": int(traffic_limit_gb) if not unlimited else 0,
        "unlimited": unlimited,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=BLITZ_API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            status, body = await _request_with_reauth(session, cfg, "POST", url, auth_ctx, json=payload)
            if status in (200, 201):
                logger.info(f"Blitz: Created user '{username}' for {expiry_days} days")
                return {"detail": "ok"}
            logger.error(f"Blitz create_user failed ({status}): {body}")
            return None
    except Exception as e:
        logger.error(f"Blitz create_user request error: {e}")
        return None


async def get_user(username: str) -> Dict | None:
    """Fetch user details from Blitz."""
    cfg = _get_blitz_config()
    if not cfg:
        return None

    auth_ctx = await _get_auth_context(cfg)
    if not auth_ctx:
        return None

    url = f"{cfg['url']}/api/v1/users/{username}"

    try:
        timeout = aiohttp.ClientTimeout(total=BLITZ_API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            status, body = await _request_with_reauth(session, cfg, "GET", url, auth_ctx)
            if status == 200:
                return _parse_json_dict(body) or {"raw": body}
            logger.error(f"Blitz get_user '{username}' failed ({status}): {body}")
            return None
    except Exception as e:
        logger.error(f"Blitz get_user request error: {e}")
        return None


async def update_user(
    username: str,
    new_expiration_days: int | None = None,
    blocked: bool | None = None,
    unlimited_ip: bool | None = None,
) -> Dict | None:
    """
    Partially update a Blitz user.

    Args:
        username: Existing username.
        new_expiration_days: Override remaining days.
        blocked: Set blocked state.
        unlimited_ip: Set unlimited IP access.

    Returns:
        Updated user data or None.
    """
    cfg = _get_blitz_config()
    if not cfg:
        return None

    auth_ctx = await _get_auth_context(cfg)
    if not auth_ctx:
        return None

    url = f"{cfg['url']}/api/v1/users/{username}"
    payload = {}
    if new_expiration_days is not None:
        payload["new_expiration_days"] = new_expiration_days
    if blocked is not None:
        payload["blocked"] = blocked
    if unlimited_ip is not None:
        payload["unlimited_ip"] = unlimited_ip

    try:
        timeout = aiohttp.ClientTimeout(total=BLITZ_API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            status, body = await _request_with_reauth(session, cfg, "PATCH", url, auth_ctx, json=payload)
            if status == 200:
                logger.info(f"Blitz: Updated user '{username}'")
                return {"detail": "ok"}
            logger.error(f"Blitz update_user '{username}' failed ({status}): {body}")
            return None
    except Exception as e:
        logger.error(f"Blitz update_user request error: {e}")
        return None


async def delete_user(username: str) -> bool:
    """Delete a user from the Blitz panel."""
    cfg = _get_blitz_config()
    if not cfg:
        return False

    auth_ctx = await _get_auth_context(cfg)
    if not auth_ctx:
        return False

    url = f"{cfg['url']}/api/v1/users/{username}"

    try:
        timeout = aiohttp.ClientTimeout(total=BLITZ_API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            status, body = await _request_with_reauth(session, cfg, "DELETE", url, auth_ctx)
            if status in (200, 204):
                logger.info(f"Blitz: Deleted user '{username}'")
                return True
            logger.error(f"Blitz delete_user '{username}' failed ({status}): {body}")
            return False
    except Exception as e:
        logger.error(f"Blitz delete_user request error: {e}")
        return False


async def get_connection_uri(username: str) -> str | None:
    """
    Retrieve the Hysteria 2 connection URI for a user.

    Returns:
        Connection string (hysteria2://...) or None.
    """
    cfg = _get_blitz_config()
    if not cfg:
        return None

    auth_ctx = await _get_auth_context(cfg)
    if not auth_ctx:
        return None

    try:
        timeout = aiohttp.ClientTimeout(total=BLITZ_API_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Different Blitz versions expose URI/subscription under different paths.
            for suffix in ("uri", "subscription", "sub"):
                url = f"{cfg['url']}/api/v1/users/{username}/{suffix}"
                status, body = await _request_with_reauth(session, cfg, "GET", url, auth_ctx)
                if status != 200:
                    continue

                body = body.strip()
                if not body:
                    continue

                parsed = _parse_json_dict(body)
                if parsed:
                    nodes = parsed.get("nodes")
                    first_node_uri = None
                    if isinstance(nodes, list) and nodes:
                        first = nodes[0]
                        if isinstance(first, dict):
                            first_node_uri = first.get("uri")
                    # Prefer normal subscription link for client import.
                    return (
                        parsed.get("normal_sub")
                        or parsed.get("subscription")
                        or parsed.get("subscription_url")
                        or parsed.get("uri")
                        or parsed.get("url")
                        or parsed.get("connection_string")
                        or first_node_uri
                    )
                return body
            logger.error(f"Blitz get_uri '{username}' failed: no valid URI/subscription endpoint responded with data.")
            return None
    except Exception as e:
        logger.error(f"Blitz get_uri request error: {e}")
        return None


def is_blitz_enabled() -> bool:
    """
    Check whether Blitz integration is turned on in settings.

    Accepts common truthy values ("true", "1", "yes", "on").
    Also requires Blitz connection settings to be present.
    """
    raw_value = (get_setting("enable_blitz") or "").strip().lower()
    if raw_value not in {"true", "1", "yes", "on"}:
        return False
    cfg = _get_blitz_config()
    return cfg is not None


def is_blitz_key(key_data: dict) -> bool:
    """Check whether a vpn_keys record belongs to Blitz."""
    return key_data.get("host_name") == BLITZ_HOST_NAME
