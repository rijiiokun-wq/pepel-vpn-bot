"""Pure, read-only accounting helpers for the shared LAVA profile.

PePel's payment history is not the profile balance. The other project's balance
must be provided independently; no residual is attributed to ``subscriptions``.
This module has no database, environment, filesystem or provider API access.
"""

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

PROJECT_IDS = ("pepel", "subscriptions")


class FinanceDataError(ValueError):
    """Inputs cannot support an exact, authoritative financial calculation."""


def money_to_cents(value: Any) -> int:
    """Read an exact RUB amount, rejecting missing values and sub-kopeck input.

    SQLite REAL values are converted using their decimal representation. This
    does not repair pre-existing floating point errors by silently rounding.
    """
    if value is None or isinstance(value, bool):
        raise FinanceDataError("A monetary amount is required")
    if not isinstance(value, (str, int, float, Decimal)):
        raise FinanceDataError("Unsupported monetary amount")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise FinanceDataError("Invalid monetary amount") from None
    if not amount.is_finite():
        raise FinanceDataError("Monetary amount must be finite")
    numerator, denominator = amount.as_integer_ratio()
    cents, remainder = divmod(numerator * 100, denominator)
    if remainder:
        raise FinanceDataError("Monetary amount must have whole kopecks")
    return cents


def _checked_cents(value: Any, *, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise FinanceDataError(f"{field} must be integer kopecks >= {minimum}")
    return value


def format_cents(value: int) -> str:
    """Format integer kopecks without floating point conversion."""
    if type(value) is not int:
        raise FinanceDataError("Expected integer kopecks")
    sign = "-" if value < 0 else ""
    rubles, kopecks = divmod(abs(value), 100)
    return f"{sign}{rubles}.{kopecks:02d}"


def _aware_timestamp(value: str | datetime) -> str:
    try:
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise FinanceDataError("Snapshot timestamp is invalid") from None
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise FinanceDataError("Snapshot timestamp must include its timezone")
    return instant.isoformat(timespec="microseconds")


@dataclass(frozen=True)
class ProjectBalance:
    project_id: str
    available_cents: int | None
    authoritative: bool = False
    complete: bool = False
    source: str = ""


@dataclass(frozen=True)
class BalanceSnapshot:
    snapshot_id: str
    as_of: str
    balances: tuple[ProjectBalance, ...]
    currency: str = "RUB"


def freeze_balance_snapshot(
    *,
    snapshot_id: str,
    as_of: str | datetime,
    balances: Iterable[ProjectBalance],
    currency: str = "RUB",
) -> BalanceSnapshot:
    """Copy and validate both authoritative project balances at one instant.

    ``source`` identifies the reconciliation evidence, not a guessed allocation.
    A shared profile balance alone cannot satisfy these inputs.
    """
    if not isinstance(snapshot_id, str) or not snapshot_id.strip():
        raise FinanceDataError("Snapshot identifier is required")
    if currency != "RUB":
        raise FinanceDataError("Only RUB snapshots are supported")
    records = tuple(balances)
    if any(not isinstance(item, ProjectBalance) for item in records):
        raise FinanceDataError("Expected ProjectBalance records")
    if len(records) != len(PROJECT_IDS) or {item.project_id for item in records} != set(PROJECT_IDS):
        raise FinanceDataError("Both PePel and subscriptions balances are required exactly once")
    for item in records:
        _checked_cents(item.available_cents, field=f"{item.project_id} available balance")
        if item.authoritative is not True or item.complete is not True:
            raise FinanceDataError("Both project balances must be authoritative and complete")
        if not isinstance(item.source, str) or not item.source.strip():
            raise FinanceDataError("Each project balance requires a source")
    if sum(item.available_cents for item in records) <= 0:
        raise FinanceDataError("Combined available balance must be positive")
    return BalanceSnapshot(
        snapshot_id=snapshot_id.strip(),
        as_of=_aware_timestamp(as_of),
        balances=tuple(sorted(records, key=lambda item: item.project_id)),
        currency=currency,
    )


@dataclass(frozen=True)
class ProjectAllocation:
    project_id: str
    available_before_cents: int
    debit_cents: int
    available_after_cents: int


@dataclass(frozen=True)
class PayoutPreview:
    snapshot: BalanceSnapshot
    payout_amount_cents: int
    payout_fee_cents: int
    total_debit_cents: int
    total_available_cents: int
    allocations: tuple[ProjectAllocation, ...]
    kind: str = "preview_only"
    allocation_rule: str = "proportional_available_largest_remainder_project_id"

    def to_dict(self) -> dict:
        """Return a detached display/export record; no payment is executed."""
        return asdict(self)


def preview_payout_allocation(
    snapshot: BalanceSnapshot,
    *,
    payout_amount: Any,
    payout_fee: Any,
) -> PayoutPreview:
    """Allocate total debit (recipient amount + known fee), in integer kopecks.

    Fractions use largest remainder; equal remainders break by project ID.
    Balances and resulting allocations are frozen in the returned preview.
    A preview is not an authorization or an instruction to move money.
    """
    if not isinstance(snapshot, BalanceSnapshot):
        raise FinanceDataError("An authoritative balance snapshot is required")
    # Validate again: callers can instantiate dataclasses without the factory.
    checked = freeze_balance_snapshot(
        snapshot_id=snapshot.snapshot_id,
        as_of=snapshot.as_of,
        balances=snapshot.balances,
        currency=snapshot.currency,
    )
    amount = _checked_cents(money_to_cents(payout_amount), field="Payout amount", minimum=1)
    fee = _checked_cents(money_to_cents(payout_fee), field="Payout fee")
    total_debit = amount + fee
    total_available = sum(item.available_cents for item in checked.balances)
    if total_debit > total_available:
        raise FinanceDataError("Total payout debit exceeds available project balances")
    quotients = {}
    remainders = {}
    for item in checked.balances:
        quotients[item.project_id], remainders[item.project_id] = divmod(
            total_debit * item.available_cents, total_available
        )
    remaining = total_debit - sum(quotients.values())
    order = sorted(quotients, key=lambda project_id: (-remainders[project_id], project_id))
    for project_id in order[:remaining]:
        quotients[project_id] += 1
    allocations = tuple(
        ProjectAllocation(
            project_id=item.project_id,
            available_before_cents=item.available_cents,
            debit_cents=quotients[item.project_id],
            available_after_cents=item.available_cents - quotients[item.project_id],
        )
        for item in checked.balances
    )
    if sum(item.debit_cents for item in allocations) != total_debit or any(
        item.available_after_cents < 0 for item in allocations
    ):
        raise FinanceDataError("Allocation invariants failed")
    return PayoutPreview(checked, amount, fee, total_debit, total_available, allocations)


@dataclass(frozen=True)
class IncomePayment:
    payment_id: str
    gross_cents: int
    paid_at: str | None
    date_source: str
    bot_source: str
    payment_service: str
    project_id: str = "pepel"
    currency: str = "RUB"
    fee_cents: int | None = None
    net_cents: int | None = None


@dataclass(frozen=True)
class IncomeSnapshot:
    payments: tuple[IncomePayment, ...]
    gross_total_cents: int
    fee_total_cents: int | None = None
    net_total_cents: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _safe_metadata(value: Any) -> Mapping:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_pepel_lava_income(
    rows: Iterable[Mapping],
    *,
    payment_time_resolver: Callable[[dict], tuple[datetime | None, str]] | None = None,
) -> IncomeSnapshot:
    """Normalize PePel DB rows; only confirmed LAVA gross income is knowable.

    The caller supplies PePel's rows and may pass the existing dashboard's
    ``payment_time_moscow`` resolver. It must not pass a mixed-project export.
    Historical provider fees/net stay unknown even when metadata has a tariff.
    Metadata, usernames, subscription links and credentials are never returned.
    Duplicate payment IDs are counted once; conflicting records fail closed.
    """
    payments: dict[str, IncomePayment] = {}
    for raw in rows:
        row = dict(raw)
        metadata = _safe_metadata(row.get("metadata"))
        method = str(row.get("payment_method") or metadata.get("payment_method") or "").strip().casefold()
        if method != "lava":
            continue
        status = str(row.get("status") or "").strip().lower()
        confirmed = metadata.get("payment_confirmed", row.get("payment_confirmed"))
        if status != "paid" and not (
            status in {"pending", "processing"} and (confirmed is True or type(confirmed) is int and confirmed == 1)
        ):
            continue
        project_id = row.get("project_id") or metadata.get("project_id")
        if project_id is not None and project_id != "pepel":
            raise FinanceDataError("Income input contains a different project")
        payment_id = str(row.get("payment_id") or "").strip()
        if not payment_id:
            raise FinanceDataError("A confirmed LAVA payment lacks its identifier")
        gross = _checked_cents(money_to_cents(row.get("amount_rub")), field="Confirmed gross", minimum=1)
        paid_at = None
        date_source = "unknown"
        if payment_time_resolver is not None:
            timestamp_fields = {
                key: row.get(key) if row.get(key) is not None else metadata.get(key)
                for key in ("confirmed_at", "payment_confirmed_at", "paid_at", "created_date")
            }
            instant, date_source = payment_time_resolver(timestamp_fields)
            paid_at = _aware_timestamp(instant) if instant is not None else None
        bot_source = str(row.get("bot_source") or metadata.get("bot_source") or "").strip().lower()
        if bot_source not in {"main", "reserve"}:
            bot_source = "unknown"
        service = str(
            row.get("payment_service") or metadata.get("lava_pay_service")
            or metadata.get("lava_payment_service") or metadata.get("lava_payment_variant") or ""
        ).strip().lower()
        if service not in {"card", "sbp"}:
            service = "unknown"
        payment = IncomePayment(payment_id, gross, paid_at, str(date_source), bot_source, service)
        if payment_id in payments and payments[payment_id] != payment:
            raise FinanceDataError("Conflicting duplicate confirmed payment")
        payments[payment_id] = payment
    ordered = tuple(sorted(payments.values(), key=lambda item: (item.paid_at or "", item.payment_id), reverse=True))
    return IncomeSnapshot(ordered, sum(item.gross_cents for item in ordered))
