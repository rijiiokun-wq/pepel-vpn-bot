"""Domain protocols and value objects for backend-migration.

These types form the contract between the Python Telegram bot and the
future Go backend.  Every domain module in this package depends only on
these definitions — never on ``aiogram``, Flask, or SQLite internals.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# ── Value objects ────────────────────────────────────────────────────

class IssuanceAction(enum.StrEnum):
    NEW = "new"
    EXTEND = "extend"


class IssuanceOutcome(enum.StrEnum):
    ISSUED = "issued"
    RETRY_SCHEDULED = "retry_scheduled"
    FATAL_FAILED = "fatal_failed"
    EXHAUSTED = "exhausted"


class HostStatus(enum.StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class CircuitState(enum.StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class PaymentMeta:
    """Validated payment metadata — the canonical subset every domain
    function may rely on."""
    payment_id: str
    user_id: int
    months: int
    price: float
    action: IssuanceAction
    key_id: int
    host_name: str
    plan_id: int
    plan_name: str = ""
    customer_email: str = ""
    payment_method: str = ""
    payment_confirmed: bool = False
    is_super_flow: bool = False
    retry_source: str | None = None
    trace_id: str | None = None


@dataclass(frozen=True, slots=True)
class IssuanceResult:
    outcome: IssuanceOutcome
    key_id: int | None = None
    connection_string: str | None = None
    reason: str | None = None
    retry_at: str | None = None


@dataclass(frozen=True, slots=True)
class KeyResult:
    key_id: int
    email: str
    host_name: str
    connection_string: str
    subscription_link: str = ""
    client_uuid: str = ""


@dataclass(frozen=True, slots=True)
class PreCheckResult:
    allowed: bool
    reason: str | None = None
    user_message: str | None = None


@dataclass(frozen=True, slots=True)
class RetryStats:
    checked: int = 0
    recovered: int = 0
    errors: int = 0
    host_skipped: int = 0
    payment_ids: list[str] = field(default_factory=list)
    recovered_payment_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class HostProbeResult:
    host_name: str
    status: HostStatus
    reason: str | None = None
    outage_seconds: float = 0.0
    circuit_state: CircuitState = CircuitState.CLOSED
    clients_count: int | None = None


# ── Protocols (interfaces) ───────────────────────────────────────────

@runtime_checkable
class PaymentValidator(Protocol):
    def parse_metadata(self, raw: dict) -> PaymentMeta:
        """Parse and validate raw metadata dict → PaymentMeta.
        Raise ``ValueError`` on missing/invalid fields."""
        ...

    def check_confirmed(self, meta: PaymentMeta) -> bool:
        """Return True if the payment is confirmed (DB paid or provider flag)."""
        ...


@runtime_checkable
class IssuanceService(Protocol):
    async def process_payment(self, meta: PaymentMeta) -> IssuanceResult:
        """Full issuance flow: pre-check → create/extend → deliver."""
        ...

    async def create_key(self, meta: PaymentMeta) -> KeyResult | None:
        """Create a new VPN key on the panel."""
        ...

    async def extend_key(self, meta: PaymentMeta) -> KeyResult | None:
        """Extend an existing VPN key."""
        ...


@runtime_checkable
class RetryEngine(Protocol):
    async def run_cycle(
        self,
        *,
        due_only: bool = True,
        limit: int = 50,
    ) -> RetryStats:
        """Run one retry cycle for pending-confirmed transactions."""
        ...

    def scan_candidates(self, **filters: object) -> list[dict]:
        """Return retry-eligible transactions."""
        ...

    def detect_stuck(self, min_age_seconds: int = 1800) -> int:
        """Find and auto-fix unretried stuck transactions. Return count fixed."""
        ...


@runtime_checkable
class AlertDispatcher(Protocol):
    def send(
        self,
        error: object,
        *,
        details: str = "",
        respect_cooldown: bool = True,
    ) -> bool:
        """Send a critical alert. Return True if dispatched."""
        ...

    def send_nonblocking(
        self,
        error: object,
        *,
        details: str = "",
        respect_cooldown: bool = True,
    ) -> bool:
        """Queue an alert without blocking the caller."""
        ...


@runtime_checkable
class HostHealthChecker(Protocol):
    def probe(self, host: dict) -> HostProbeResult:
        """Probe a host and return current health status."""
        ...

    def is_healthy(self, host_name: str) -> bool:
        """Quick check whether a host is considered healthy for retries."""
        ...


@runtime_checkable
class PaymentRepository(Protocol):
    def get_pending_confirmed(self, **filters: object) -> list[dict]:
        """Return transactions eligible for retry."""
        ...

    def mark_retryable(self, payment_id: str, reason: str) -> bool:
        """Move transaction to retryable_failed state."""
        ...

    def mark_fatal(self, payment_id: str, reason: str) -> bool:
        """Move transaction to fatal_failed (non-retryable)."""
        ...

    def mark_exhausted(self, payment_id: str) -> bool:
        """Move transaction to exhausted (dead-letter)."""
        ...

    def start_processing(self, payment_id: str) -> bool:
        """Atomically claim a transaction for processing."""
        ...

    def finalize_paid(self, payment_id: str) -> bool:
        """Mark transaction as paid (key issued)."""
        ...

    def is_paid(self, payment_id: str) -> bool:
        """Check whether a transaction is already paid (idempotency)."""
        ...
