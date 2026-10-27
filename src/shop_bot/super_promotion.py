"""Offer availability; paid invoices keep their stored terms.

These guards belong only to menus and creation of new invoices. Never use them
in webhook fulfillment or retries: a September invoice still grants 3 months
when its confirmed payment arrives in October.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

PROMO_CODE = "promo_202609_2plus1"
PROMO_START = datetime(2026, 9, 14, tzinfo=ZoneInfo("Europe/Moscow"))
PROMO_END: datetime | None = None
PROMO_EXPIRED_TEXT = "Акция завершена. Выберите актуальный тариф в меню покупки или продления."
PROMO_SUPER_PLAN = {
    "code": PROMO_CODE,
    "promo_code": PROMO_CODE,
    "months": 3,
    "paid_months": 2,
    "bonus_months": 1,
    "price": 1100,
    "old_price": None,
    "discount_text": "",
    "name": "Суперключ (обход белых списков) 2 месяца + 1 бесплатный",
    "button_text": "🎁🔥 2 месяца + 1 бесплатно — 1100 ₽",
}


PROMO_STANDARD_PLAN = {
    "code": "promo_standard_6plus1",
    "promo_code": "promo_standard_6plus1",
    "months": 7,
    "paid_months": 6,
    "bonus_months": 1,
    "price": 500,
    "name": "Стандарт — 6 месяцев + 1 бесплатно",
    "button_text": "🎁🔥 6 месяцев + 1 бесплатно — 500 ₽",
}


def _promotional_offer(plan: dict | None) -> dict | None:
    if not plan:
        return None
    for offer in (PROMO_SUPER_PLAN, PROMO_STANDARD_PLAN):
        if any(plan.get(field) == offer["code"] for field in ("promo_code", "code", "super_plan_code")):
            return offer
    # Standard offers use ordinary DB plan IDs and the normal Standard flow.
    if (plan.get("plan_name") == PROMO_STANDARD_PLAN["name"]
            and plan.get("months") == 7 and plan.get("price") == 500):
        return PROMO_STANDARD_PLAN
    return None


def is_promotional_plan(plan: dict | None) -> bool:
    return _promotional_offer(plan) is not None


def promotion_price(plan: dict) -> int | float:
    offer = _promotional_offer(plan)
    return offer["price"] if offer else plan["price"]


def promotion_button_text(plan: dict) -> str:
    offer = _promotional_offer(plan)
    return offer["button_text"] if offer else f"{plan['plan_name']} - {plan['price']:.0f} RUB"


def is_super_offer_available(plan: dict | None, *, now: datetime | None = None) -> bool:
    offer = _promotional_offer(plan)
    if not offer or offer is PROMO_STANDARD_PLAN:
        return True
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Promotion clock must be timezone-aware")
    return PROMO_START <= current and (PROMO_END is None or current < PROMO_END)


def promotion_metadata(plan: dict | None) -> dict:
    """Duration already includes the bonus; never add it again at fulfillment."""
    offer = _promotional_offer(plan)
    if not offer:
        return {}
    return {field: offer[field] for field in ("promo_code", "paid_months", "bonus_months")}
