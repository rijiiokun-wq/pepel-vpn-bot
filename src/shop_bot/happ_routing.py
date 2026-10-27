"""Happ routing defaults and per-subscription overrides, independent of panel data.

Private settings live in bot_settings, never in a subscription payload cache.
No enabled setting means no response change. Subscription tokens are never logged.
"""

import base64
import hashlib
import json
import logging
import re
from collections.abc import Callable

from flask import request

SETTING_PREFIX = "happ_routing_override_"
DEFAULT_SETTING = "happ_routing_default"
MAX_HEADER_BYTES = 4096
logger = logging.getLogger(__name__)


def setting_key(sub_id: str) -> str:
    normalized = str(sub_id or "").strip().strip("/")
    return SETTING_PREFIX + hashlib.sha256(normalized.encode()).hexdigest()


def routing_header(raw: str | None) -> str | None:
    """Reject malformed overrides without breaking a working subscription."""
    if not raw or len(raw) > 16384:
        return None
    try:
        config = json.loads(raw)
        if not isinstance(config, dict) or config.get("enabled") is not True:
            return None
        profile = config.get("profile")
        if not isinstance(profile, dict) or not isinstance(profile.get("Name"), str):
            return None
        if not profile["Name"].strip():
            return None
        for field in ("DirectSites", "DirectIp", "ProxySites", "ProxyIp", "BlockSites", "BlockIp"):
            values = profile.get(field, [])
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                return None
        encoded = base64.b64encode(
            json.dumps(profile, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        header = "happ://routing/onadd/" + encoded
        return header if len(header) <= MAX_HEADER_BYTES else None
    except (TypeError, ValueError, RecursionError):
        return None


def register_subscription_routing(app, read_setting: Callable[[str], str | None]) -> None:
    @app.after_request
    def apply_happ_subscription_routing(response):
        if (
            request.endpoint != "managed_subscription_route"
            or response.status_code != 200
            or response.mimetype != "text/plain"
        ):
            return response
        sub_id = (request.view_args or {}).get("sub_id")
        if not sub_id:
            return response
        try:
            raw = read_setting(setting_key(sub_id))
            is_override = raw is not None
            if not is_override:
                raw = read_setting(DEFAULT_SETTING)
            header = routing_header(raw)
            # The shared default is domain routing only, never paid app management.
            management_headers = app_management_headers(raw) if header and is_override else {}
        except Exception:  # noqa: BLE001 - optional metadata must never break key delivery
            # Do not leak the token, private setting, or exception message.
            logger.warning("Happ routing override unavailable; returning unchanged subscription")
            return response
        if header:
            response.headers["routing"] = header
            response.headers.update(management_headers)
            response.headers["Cache-Control"] = "private, no-store, no-cache, max-age=0"
        return response


def app_management_headers(raw: str | None) -> dict[str, str]:
    """Only an explicitly enabled private override may enable app management.

    Android-only headers are interpreted by Happ on Android; other platforms
    retain the common domain profile. Never clear the user's existing app list.
    """
    if not raw or len(raw) > 16384:
        return {}
    try:
        config = json.loads(raw)
        if not isinstance(config, dict) or config.get("enabled") is not True:
            return {}
        provider_id = config.get("provider_id")
        if not isinstance(provider_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{4,128}", provider_id):
            return {}
        headers = {"providerid": provider_id}
        packages = config.get("android_bypass_packages")
        if (
            isinstance(packages, list)
            and 0 < len(packages) <= 100
            and all(isinstance(p, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", p)
                    for p in packages)
        ):
            package_list = ",".join(dict.fromkeys(packages))
            if len(package_list) <= MAX_HEADER_BYTES:
                headers["per-app-proxy-mode"] = "bypass"
                headers["per-app-proxy-list"] = package_list
    except (TypeError, ValueError, RecursionError):
        return {}
    return headers
