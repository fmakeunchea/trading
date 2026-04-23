"""Typed contracts shared by every module.

Design notes:

* All monetary values use ``decimal.Decimal``. ``float`` for money is
  forbidden in this package.
* Timestamps are timezone-aware UTC (:class:`datetime.datetime` with
  ``tzinfo=timezone.utc``). Naive datetimes are rejected at construction
  time where the invariant matters (e.g. :class:`OrderIntent`).
* Dataclasses are frozen where mutation would be an invariant violation.
* :meth:`OrderIntent.client_order_id` is the idempotency fence: the same
  intent always produces the same COID, so retries and restarts can never
  create duplicate live orders.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


COID_PREFIX = "TBv1-"
COID_HASH_LEN = 36  # total length = len(prefix) + 36 = 41 chars <= Alpaca's 48


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class OrderSide(str, Enum):
    """Long-only in Phase 1 live. SHORT exists only to let tests assert it
    is rejected, and to make reconciliation errors crystal clear if the
    broker ever reports a short position on our account."""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class TimeInForce(str, Enum):
    """Only DAY is used in v1. GTC would span sessions and contradicts the
    intraday-only + flat-by-close invariants."""

    DAY = "day"


class OrderClass(str, Enum):
    SIMPLE = "simple"
    OTO = "oto"


class OrderStatus(str, Enum):
    """Superset of Alpaca order statuses we care about. UNKNOWN is a
    fail-closed placeholder for any value the SDK might add that we have
    not mapped yet — the broker wrapper treats UNKNOWN as non-terminal."""

    PENDING_NEW = "pending_new"
    NEW = "new"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    HELD = "held"
    UNKNOWN = "unknown"


TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.EXPIRED,
        OrderStatus.REJECTED,
    }
)


# ---------------------------------------------------------------------------
# Market data DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    trade_count: int | None = None
    vwap: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    ts: datetime
    bid_price: Decimal
    ask_price: Decimal
    bid_size: int
    ask_size: int

    def mid(self) -> Decimal:
        return (self.bid_price + self.ask_price) / Decimal(2)

    def spread_bps(self) -> Decimal:
        mid = self.mid()
        if mid <= 0:
            # Fail closed: a non-positive mid is a broken quote. The spread
            # filter will reject entry because this is treated as a very
            # wide spread rather than a valid 0-bps.
            return Decimal("99999")
        return (self.ask_price - self.bid_price) / mid * Decimal(10_000)


@dataclass(frozen=True, slots=True)
class Trade:
    symbol: str
    ts: datetime
    price: Decimal
    size: int


# ---------------------------------------------------------------------------
# Account / position DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    ts: datetime
    equity: Decimal
    last_equity: Decimal
    buying_power: Decimal
    cash: Decimal
    pattern_day_trader: bool


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    qty: int
    avg_entry_price: Decimal
    market_value: Decimal
    unrealized_pl: Decimal
    side: OrderSide  # BUY = long, SELL = short


# ---------------------------------------------------------------------------
# Order DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """The bot's intent to submit an order, before any broker contact.

    The COID is a pure function of the canonical JSON of this dataclass's
    identity-determining fields. Two identical intents always produce the
    same COID, so duplicate submission is impossible by construction.
    """

    intent_id: str              # UUID-ish string, generated once per decision
    symbol: str
    side: OrderSide
    qty: int
    limit_price: Decimal
    disaster_stop_price: Decimal
    tif: TimeInForce
    order_class: OrderClass
    reason: str                 # human-readable strategy reason
    ref_price: Decimal          # snapshot reference price at decision time
    atr: Decimal                # ATR at decision time
    spread_bps: Decimal         # snapshot spread at decision time
    ts: datetime                # decision timestamp (UTC)

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise ValueError("OrderIntent.ts must be timezone-aware")
        if self.qty <= 0:
            raise ValueError("OrderIntent.qty must be a positive integer")
        if self.side is not OrderSide.BUY:
            # Phase 1 live is long-only. The DTO still carries OrderSide to
            # let reconciliation name a short if Alpaca ever reports one,
            # but OrderIntent itself cannot carry a SELL entry.
            raise ValueError("OrderIntent is long-only in v1 (side must be BUY)")
        if self.tif is not TimeInForce.DAY:
            raise ValueError("OrderIntent.tif must be DAY in v1")
        if self.order_class is not OrderClass.OTO:
            raise ValueError("OrderIntent.order_class must be OTO in v1")
        if self.limit_price <= 0 or self.disaster_stop_price <= 0:
            raise ValueError("Prices must be positive")
        if self.disaster_stop_price >= self.limit_price:
            # Long-only OTO: the stop child must sit strictly below the
            # parent's limit price, otherwise the broker will reject.
            raise ValueError(
                "disaster_stop_price must be strictly less than limit_price for long entries"
            )

    def canonical_payload(self) -> dict[str, Any]:
        """Identity-determining fields, normalised to JSON-serialisable
        primitives, sorted for stability.

        ``ts`` and ``intent_id`` are included so two independent decisions
        at different times can never collide on a COID.
        """
        return {
            "intent_id": self.intent_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "qty": self.qty,
            "limit_price": str(self.limit_price),
            "disaster_stop_price": str(self.disaster_stop_price),
            "tif": self.tif.value,
            "order_class": self.order_class.value,
            "ts": self.ts.astimezone(timezone.utc).isoformat(),
        }

    def client_order_id(self) -> str:
        """Deterministic, collision-resistant idempotency key.

        Shape: ``TBv1-<36-hex>`` — 41 chars total, well within Alpaca's
        48-char limit. Re-deriving from the same intent always yields the
        same COID; the broker will reject a duplicate with a 422 which we
        map to :class:`DuplicateClientOrderId`.
        """
        canonical = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return COID_PREFIX + digest[:COID_HASH_LEN]


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    """Normalised view of an order as the broker sees it.

    Only the fields the bot actually uses.

    Linkage between a leg and its parent is established by **traversal**
    from ``parent.legs`` at construction time, not by any field on the
    leg itself: Alpaca does not populate a back-reference on OTO
    children. The wrapper records that traversal by setting
    ``parent_client_order_id`` (parent's COID) and
    ``parent_broker_order_id`` (parent's server id) on the leg's DTO.
    ``leg_role`` ("parent" | "stop_child" | "take_profit_child" | None)
    distinguishes roles so reconciliation does not need to re-derive
    them from order-type string matching.
    """

    broker_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    qty: int
    filled_qty: int
    avg_fill_price: Decimal | None
    status: OrderStatus
    order_class: OrderClass
    submitted_at: datetime
    filled_at: datetime | None
    parent_client_order_id: str | None
    leg_role: str | None  # "parent" | "stop_child" | "take_profit_child" | None
    parent_broker_order_id: str | None = None

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class SubmittedOrder:
    """Return value of :meth:`Broker.submit_entry_with_protection`."""

    parent: BrokerOrder
    stop_child: BrokerOrder | None  # None only if broker hasn't attached yet


@dataclass(frozen=True, slots=True)
class CloseResult:
    symbol: str
    cancelled_order_ids: tuple[str, ...]
    close_order: BrokerOrder
    final_position_qty: int  # should be 0 on success


# ---------------------------------------------------------------------------
# State DTOs
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OpenTrade:
    """Local view of a live position, persisted in :class:`StrategyState`."""

    symbol: str
    qty: int
    entry_price: Decimal
    entry_ts: datetime
    stop_price: Decimal              # bot-managed primary stop
    disaster_stop_price: Decimal     # broker-side OTO child stop
    target_price: Decimal
    intent_id: str
    parent_client_order_id: str
    protective_child_client_order_id: str | None
    protective_child_broker_id: str | None
    last_seen_broker_qty: int


@dataclass(slots=True)
class HaltRecord:
    name: str
    active: bool
    triggered_at: datetime
    reason: str


# ---------------------------------------------------------------------------
# Signal / risk DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signal:
    """Output of signal.py — defined here to keep all DTOs in one place.

    The signal layer is Phase 2; the DTO lives in Phase 1 so tests and
    risk can reference the shape.
    """

    symbol: str
    ts: datetime
    direction: OrderSide  # BUY only in v1
    reason: str
    ref_price: Decimal
    atr: Decimal
    expected_move_bps: Decimal


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Output of :func:`strategy.risk.evaluate_entry`."""

    allowed: bool
    reason: str
    qty: int = 0
    notional: Decimal = Decimal(0)
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    disaster_stop_price: Decimal | None = None
    throttle: Decimal = Decimal(1)
    equity_used: Decimal = Decimal(0)
    peak_equity_used: Decimal = Decimal(0)
    drawdown_pct: Decimal = Decimal(0)


# ---------------------------------------------------------------------------
# Reconciliation DTOs (populated by strategy.reconcile)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReconcileReport:
    """Structured diff between local truth and broker truth.

    Every category is a list so callers can iterate and log each mismatch.
    :meth:`is_clean` is the entry gate: any non-empty category means the
    orchestrator must block new entries.
    """

    missing_positions: list[str] = field(default_factory=list)
    extra_positions: list[str] = field(default_factory=list)
    qty_mismatches: list[tuple[str, int, int]] = field(default_factory=list)
    orphan_protective_orders: list[str] = field(default_factory=list)
    unexpected_protective_children: list[str] = field(default_factory=list)
    partial_fill_with_orphan_child: list[str] = field(default_factory=list)
    position_side_mismatch: list[str] = field(default_factory=list)
    broker_order_with_unknown_coid: list[str] = field(default_factory=list)
    stale_local_orders: list[str] = field(default_factory=list)

    def is_clean(self) -> bool:
        """Strict entry gate.

        Unknown-COID orders are surfaced but do NOT by themselves block
        entries — see :meth:`requires_entry_block`. is_clean is the
        strongest invariant; requires_entry_block is the operational gate.
        """
        return (
            not self.missing_positions
            and not self.extra_positions
            and not self.qty_mismatches
            and not self.orphan_protective_orders
            and not self.unexpected_protective_children
            and not self.partial_fill_with_orphan_child
            and not self.position_side_mismatch
            and not self.broker_order_with_unknown_coid
            and not self.stale_local_orders
        )

    def requires_halt(self) -> bool:
        """The bot must halt immediately (not just refuse new entries)."""
        return bool(self.position_side_mismatch)

    def requires_entry_block(self) -> bool:
        """New entries must be blocked. Unknown-COID alone only warns."""
        return (
            bool(self.missing_positions)
            or bool(self.extra_positions)
            or bool(self.qty_mismatches)
            or bool(self.orphan_protective_orders)
            or bool(self.unexpected_protective_children)
            or bool(self.partial_fill_with_orphan_child)
            or bool(self.position_side_mismatch)
            or bool(self.stale_local_orders)
        )
