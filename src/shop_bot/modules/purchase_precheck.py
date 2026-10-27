"""
Centralized pre-check before any purchase flow.

Validates host availability, maintenance mode, and product status
before allowing the user to proceed with payment.
"""

import logging
import os
from dataclasses import dataclass

from shop_bot.data_manager.database import (
    get_host,
    get_maintenance_message,
    get_setting,
    is_global_maintenance,
    is_host_delivery_active,
    update_host_status,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAINTENANCE_TEXT = (
    "Покупка временно недоступна, ведутся технические работы. Попробуйте позже."
)
PRECHECK_REVALIDATE_UNAVAILABLE_HOSTS = (
    str(os.environ.get("PRECHECK_REVALIDATE_UNAVAILABLE_HOSTS", "false")).strip().lower()
    in {"1", "true", "yes", "on"}
)


@dataclass(frozen=True, slots=True)
class PreCheckResult:
    allowed: bool
    reason: str | None = None
    user_message: str | None = None


def _resolve_maintenance_text() -> str:
    try:
        text = str(get_maintenance_message() or "").strip()
    except Exception as e:
        logger.error("Failed to read maintenance message in purchase pre-check: %s", e, exc_info=True)
        text = ""
    return text or _DEFAULT_MAINTENANCE_TEXT


def _blocked_result(reason: str) -> PreCheckResult:
    return PreCheckResult(
        allowed=False,
        reason=reason,
        user_message=_resolve_maintenance_text(),
    )


def _revalidate_host(host_name: str, host: dict) -> bool:
    """
    Re-check a stale unhealthy host against the panel before blocking purchase.

    This avoids false "technical works" when the scheduler briefly marked a host
    unavailable after a transient network flap, but the panel is already
    reachable again.
    """
    try:
        from shop_bot.modules.xui_api import _resolve_host_api_token, login_to_host

        api, inbound = login_to_host(
            host_url=host["host_url"],
            username=host["host_username"],
            password=host["host_pass"],
            inbound_id=int(host["host_inbound_id"]),
            api_token=_resolve_host_api_token(host),
        )
        if api and inbound:
            logger.warning(
                "Purchase pre-check recovered stale host status for '%s': %s -> healthy",
                host_name,
                host.get("status"),
            )
            update_host_status(host_name, "healthy")
            return True
    except Exception as e:
        logger.warning("Purchase pre-check revalidation failed for host '%s': %s", host_name, e)
    return False


def check_purchase(host_name: str) -> PreCheckResult:
    """
    Run all pre-purchase validations for the given host.

    Returns PreCheckResult.allowed=True when purchase can proceed.
    Otherwise returns a user-facing message explaining the block.
    """
    try:
        if is_global_maintenance():
            return _blocked_result("global_maintenance")

        if not host_name:
            return _blocked_result("no_host")

        host = get_host(host_name)
        if not host:
            return _blocked_result("host_not_found")

        if host.get("is_maintenance"):
            return _blocked_result("host_maintenance")

        if not is_host_delivery_active(host):
            return _blocked_result("host_delivery_disabled")

        host_status = str(host.get("status", "unknown")).lower()
        if host_status in {"unavailable", "maintenance"}:
            if (
                host_status == "unavailable"
                and PRECHECK_REVALIDATE_UNAVAILABLE_HOSTS
                and _revalidate_host(host_name, host)
            ):
                return PreCheckResult(allowed=True)
            return _blocked_result(f"host_{host_status}")

        return PreCheckResult(allowed=True)
    except Exception as e:
        logger.error(
            "Purchase pre-check crashed for host '%s': %s",
            host_name,
            e,
            exc_info=True,
        )
        return _blocked_result("precheck_exception")


def check_super_purchase() -> PreCheckResult:
    """Pre-check specifically for Super key purchases."""
    super_host_name = (get_setting("super_key_host_name") or "").strip()
    if not super_host_name:
        return PreCheckResult(
            allowed=False,
            reason="super_not_configured",
            user_message="Суперключ (обход белых списков) временно недоступен. Обратитесь в поддержку.",
        )
    return check_purchase(super_host_name)
