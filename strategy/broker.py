"""Alpaca broker wrapper — the sole SDK boundary.

Design notes:

* Every ``alpaca`` import lives inside this file. Grep will enforce this
  (see the Phase 1 verification step in the plan file).
* Errors are translated to this package's typed hierarchy by a single
  :func:`_classify` helper. Callers see only ``StrategyError`` subclasses.
* Submission is idempotent: the COID comes from the intent itself
  (``OrderIntent.client_order_id()``). A collision is expected on retry
  and resolved by re-fetching the order by COID — never a duplicate.
* Transient errors are retried with exponential backoff inside the
  wrapper. Permanent errors are never retried.
* The execution primitive is OTO: one ``LimitOrderRequest`` with an
  attached ``StopLossRequest``. Bracket and simple-parent-then-stop are
  rejected designs (see the approved plan).
* The flatten sequence is explicit and auditable: cancel children first,
  confirm cancelled, then submit the close, then verify position=0.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Protocol

# The SDK is imported ONLY here. See plan verification step 5.
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass as SdkOrderClass,
    OrderSide as SdkOrderSide,
    OrderStatus as SdkOrderStatus,
    QueryOrderStatus,
    TimeInForce as SdkTimeInForce,
)
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopLossRequest,
)

from strategy.dto import (
    COID_PREFIX,
    AccountSnapshot,
    Bar,
    BrokerOrder,
    CloseResult,
    OrderClass,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
    Quote,
    SubmittedOrder,
    Trade,
)
from strategy.errors import (
    AccountRestricted,
    DuplicateClientOrderId,
    MarketClosedRejection,
    OrderRejected,
    PermanentBrokerError,
    TransientBrokerError,
)


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 3
    base_s: float = 0.5
    cap_s: float = 4.0


# ---------------------------------------------------------------------------
# Market-data timeframes
# ---------------------------------------------------------------------------


class BarTimeframe(str, Enum):
    """The three timeframes Phase 2's signal cares about.

    The mapping to alpaca-py's :class:`TimeFrame` is explicit (see
    :func:`_to_sdk_timeframe`). Adding a new timeframe means adding a
    case here and in that helper — by design, no free-form strings.
    """

    M5 = "5Min"
    M15 = "15Min"
    H1 = "1Hour"


def _to_sdk_timeframe(tf: BarTimeframe) -> TimeFrame:
    if tf is BarTimeframe.M5:
        return TimeFrame(5, TimeFrameUnit.Minute)
    if tf is BarTimeframe.M15:
        return TimeFrame(15, TimeFrameUnit.Minute)
    if tf is BarTimeframe.H1:
        return TimeFrame(1, TimeFrameUnit.Hour)
    raise PermanentBrokerError(f"unmapped timeframe: {tf!r}")


def _to_sdk_feed(feed: str) -> DataFeed:
    f = feed.lower()
    if f == "sip":
        return DataFeed.SIP
    if f == "iex":
        return DataFeed.IEX
    if f == "otc":
        return DataFeed.OTC
    raise PermanentBrokerError(f"unmapped data feed: {feed!r}")


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


_SDK_STATUS_MAP: dict[SdkOrderStatus, OrderStatus] = {
    SdkOrderStatus.PENDING_NEW: OrderStatus.PENDING_NEW,
    SdkOrderStatus.NEW: OrderStatus.NEW,
    SdkOrderStatus.ACCEPTED: OrderStatus.ACCEPTED,
    SdkOrderStatus.PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    SdkOrderStatus.FILLED: OrderStatus.FILLED,
    SdkOrderStatus.CANCELED: OrderStatus.CANCELED,
    SdkOrderStatus.EXPIRED: OrderStatus.EXPIRED,
    # DONE_FOR_DAY is Alpaca's "DAY order ended, not filled" — treat as
    # EXPIRED / terminal. This is the partial-fill-at-DAY-close case that
    # reconcile must catch for OTO child sizing.
    SdkOrderStatus.DONE_FOR_DAY: OrderStatus.EXPIRED,
    SdkOrderStatus.REJECTED: OrderStatus.REJECTED,
    SdkOrderStatus.HELD: OrderStatus.HELD,
    # Replace and pending-* states aren't used in Phase 1 (we never
    # replace). If they appear, the wrapper treats them as non-terminal
    # UNKNOWN so poll_terminal keeps polling and reconcile will surface
    # any stragglers.
    SdkOrderStatus.REPLACED: OrderStatus.UNKNOWN,
    SdkOrderStatus.PENDING_CANCEL: OrderStatus.UNKNOWN,
    SdkOrderStatus.PENDING_REPLACE: OrderStatus.UNKNOWN,
    SdkOrderStatus.PENDING_REVIEW: OrderStatus.UNKNOWN,
    SdkOrderStatus.ACCEPTED_FOR_BIDDING: OrderStatus.UNKNOWN,
    SdkOrderStatus.STOPPED: OrderStatus.UNKNOWN,
    SdkOrderStatus.SUSPENDED: OrderStatus.UNKNOWN,
    SdkOrderStatus.CALCULATED: OrderStatus.UNKNOWN,
}


def _map_status(sdk_status: Any) -> OrderStatus:
    # Tolerate non-enum inputs (plain strings from test mocks or an
    # unrecognised SDK value) — fail closed to UNKNOWN, which is
    # non-terminal; the caller will keep polling or reconcile will trip.
    if isinstance(sdk_status, SdkOrderStatus):
        return _SDK_STATUS_MAP.get(sdk_status, OrderStatus.UNKNOWN)
    try:
        return _SDK_STATUS_MAP.get(SdkOrderStatus(sdk_status), OrderStatus.UNKNOWN)
    except ValueError:
        return OrderStatus.UNKNOWN


def _map_side(side: Any) -> OrderSide:
    """Map SDK order/position sides to our OrderSide enum.

    Alpaca uses ``buy``/``sell`` on orders and ``long``/``short`` on
    positions. We normalise long → BUY and short → SELL so positions
    flow through the same type.
    """
    if isinstance(side, SdkOrderSide):
        return OrderSide(side.value)
    raw = str(side).lower()
    if raw in ("buy", "long"):
        return OrderSide.BUY
    if raw in ("sell", "short"):
        return OrderSide.SELL
    # Fail closed — any other value is dangerous to default.
    raise PermanentBrokerError(f"unrecognised order/position side: {side!r}")


def _map_order_class(c: Any) -> OrderClass:
    if isinstance(c, SdkOrderClass):
        if c is SdkOrderClass.OTO:
            return OrderClass.OTO
        return OrderClass.SIMPLE
    try:
        return OrderClass(str(c).lower())
    except ValueError:
        return OrderClass.SIMPLE


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


_DUPLICATE_COID_CODES = frozenset({40010001, 42210000, 40310000})
# Historically-observed Alpaca codes for duplicate COID; match on any
# plausible indicator plus the message for robustness.
_DUPLICATE_COID_MARKERS = ("client_order_id", "already used", "duplicate")
_MARKET_CLOSED_MARKERS = ("market is closed", "market closed", "trading is closed")
_ACCOUNT_RESTRICTED_MARKERS = (
    "account is restricted",
    "account is blocked",
    "account blocked",
    "pattern day trader",
)


def _classify(exc: Exception) -> Exception:
    """Translate an alpaca-py / network exception into our typed hierarchy.

    Connection and TLS/DNS errors come through as ordinary builtins
    (``ConnectionError``, ``TimeoutError``, ``OSError``). Everything else
    arrives as :class:`APIError` with ``status_code`` / ``code`` /
    ``message`` attrs.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return TransientBrokerError(f"network error: {exc}")
    if isinstance(exc, OSError):
        # DNS/TLS failures surface here.
        return TransientBrokerError(f"socket error: {exc}")
    if not isinstance(exc, APIError):
        # Unknown exception type — fail closed as permanent, loud and clear.
        return PermanentBrokerError(f"unclassified broker error: {exc!r}")

    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    message = (getattr(exc, "message", None) or str(exc) or "").lower()

    if status in (500, 502, 503, 504) or (isinstance(status, int) and status >= 500):
        return TransientBrokerError(f"server error {status}: {message}")
    if status == 429:
        return TransientBrokerError(f"rate limited: {message}")

    if status == 422:
        # Business-rule rejection. Disambiguate duplicate-COID specifically.
        if (
            (isinstance(code, int) and code in _DUPLICATE_COID_CODES)
            or any(m in message for m in _DUPLICATE_COID_MARKERS)
        ):
            return DuplicateClientOrderId(f"duplicate client_order_id: {message}")
        if any(m in message for m in _MARKET_CLOSED_MARKERS):
            return MarketClosedRejection(f"market closed: {message}")
        return OrderRejected(f"order rejected: {message}", reason_code=str(code) if code else None)

    if status == 403:
        if any(m in message for m in _ACCOUNT_RESTRICTED_MARKERS):
            return AccountRestricted(f"account restricted: {message}")
        return AccountRestricted(f"forbidden: {message}")

    if status in (400, 401, 404):
        return PermanentBrokerError(f"http {status}: {message}")

    # Anything else: fail closed as permanent.
    return PermanentBrokerError(f"unmapped api error status={status} code={code}: {message}")


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


def _with_retry(
    fn: Callable[[], Any],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — we re-classify below
            # Already-classified strategy errors pass through; raw SDK /
            # network exceptions get translated by _classify.
            if isinstance(exc, (TransientBrokerError, PermanentBrokerError)):
                classified: Exception = exc
            else:
                classified = _classify(exc)
            if not isinstance(classified, TransientBrokerError):
                raise classified from exc
            if attempt >= policy.max_retries:
                raise classified from exc
            backoff = min(policy.cap_s, policy.base_s * (2**attempt))
            jitter = backoff * 0.1 * random.random()
            sleep(backoff + jitter)
            attempt += 1


# ---------------------------------------------------------------------------
# SDK adapters — minimal Protocol so tests can substitute a plain mock
# ---------------------------------------------------------------------------


class _TradingClientProto(Protocol):
    def submit_order(self, order_data: Any) -> Any: ...
    def get_orders(self, filter: Any | None = ...) -> list[Any]: ...  # noqa: A002
    def get_order_by_client_id(self, client_id: str) -> Any: ...
    def cancel_order_by_id(self, order_id: str) -> None: ...
    def get_all_positions(self) -> list[Any]: ...
    def get_account(self) -> Any: ...


class _DataClientProto(Protocol):
    def get_stock_bars(self, request_params: Any) -> Any: ...
    def get_stock_latest_quote(self, request_params: Any) -> Any: ...
    def get_stock_latest_trade(self, request_params: Any) -> Any: ...


# ---------------------------------------------------------------------------
# Main wrapper
# ---------------------------------------------------------------------------


class AlpacaBroker:
    """Thin, typed wrapper over :class:`alpaca.trading.client.TradingClient`.

    Construction takes a ``TradingClient``-shaped object so tests can pass
    a mock without instantiating the real SDK or hitting the network.
    """

    def __init__(
        self,
        trading_client: _TradingClientProto,
        data_client: _DataClientProto | None = None,
        *,
        data_feed: str = "iex",
        retry_policy: RetryPolicy | None = None,
        poll_interval_s: float = 0.5,
        poll_timeout_s: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = trading_client
        self._data = data_client
        self._data_feed = data_feed
        self._retry = retry_policy or RetryPolicy()
        self._poll_interval_s = poll_interval_s
        self._poll_timeout_s = poll_timeout_s
        self._sleep = sleep

    @classmethod
    def from_credentials(
        cls,
        api_key: str,
        api_secret: str,
        *,
        paper: bool,
        data_feed: str = "iex",
        retry_policy: RetryPolicy | None = None,
        poll_interval_s: float = 0.5,
        poll_timeout_s: float = 15.0,
    ) -> "AlpacaBroker":
        client = TradingClient(api_key=api_key, secret_key=api_secret, paper=paper)
        data = StockHistoricalDataClient(api_key=api_key, secret_key=api_secret)
        return cls(
            client,
            data,
            data_feed=data_feed,
            retry_policy=retry_policy,
            poll_interval_s=poll_interval_s,
            poll_timeout_s=poll_timeout_s,
        )

    # ------------------------------------------------------------------ #
    # Account / positions / orders — query
    # ------------------------------------------------------------------ #

    def get_account_snapshot(self) -> AccountSnapshot:
        raw = _with_retry(
            lambda: self._client.get_account(),
            self._retry,
            sleep=self._sleep,
        )
        return AccountSnapshot(
            ts=datetime.now(timezone.utc),
            equity=Decimal(str(raw.equity)),
            last_equity=Decimal(str(raw.last_equity)),
            buying_power=Decimal(str(raw.buying_power)),
            cash=Decimal(str(raw.cash)),
            pattern_day_trader=bool(getattr(raw, "pattern_day_trader", False)),
        )

    def get_positions(self) -> list[Position]:
        raws = _with_retry(
            lambda: self._client.get_all_positions(),
            self._retry,
            sleep=self._sleep,
        )
        return [_to_position(r) for r in raws]

    def get_open_orders(self, symbol: str | None = None) -> list[BrokerOrder]:
        def call() -> list[Any]:
            req = GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                symbols=[symbol] if symbol else None,
                nested=True,
            )
            return self._client.get_orders(filter=req)

        raws = _with_retry(call, self._retry, sleep=self._sleep)
        return _flatten_orders(raws)

    def get_order_by_coid(self, client_order_id: str) -> BrokerOrder | None:
        def call() -> Any:
            return self._client.get_order_by_client_id(client_order_id)

        try:
            raw = _with_retry(call, self._retry, sleep=self._sleep)
        except PermanentBrokerError as exc:
            # A 404 here means the order simply doesn't exist.
            if "404" in str(exc):
                return None
            raise
        return _to_broker_order(raw)

    # ------------------------------------------------------------------ #
    # Market data — query
    # ------------------------------------------------------------------ #

    def get_bars(
        self,
        symbol: str,
        timeframe: BarTimeframe,
        *,
        start: datetime,
        end: datetime,
        limit: int | None = None,
    ) -> list[Bar]:
        """Fetch historical bars for ``symbol`` between ``start`` and ``end``.

        The SDK returns a mapping of symbol → list-of-bar-models. We
        normalise to our :class:`Bar` DTO with ``Decimal`` prices.
        """
        if self._data is None:
            raise PermanentBrokerError("market data client not configured")
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=_to_sdk_timeframe(timeframe),
            start=start,
            end=end,
            limit=limit,
            feed=_to_sdk_feed(self._data_feed),
        )

        def call() -> Any:
            return self._data.get_stock_bars(req)

        raw = _with_retry(call, self._retry, sleep=self._sleep)
        return _extract_bars(raw, symbol)

    def get_latest_quote(self, symbol: str) -> Quote:
        if self._data is None:
            raise PermanentBrokerError("market data client not configured")
        req = StockLatestQuoteRequest(
            symbol_or_symbols=symbol,
            feed=_to_sdk_feed(self._data_feed),
        )

        def call() -> Any:
            return self._data.get_stock_latest_quote(req)

        raw = _with_retry(call, self._retry, sleep=self._sleep)
        return _extract_latest_quote(raw, symbol)

    def get_latest_trade(self, symbol: str) -> Trade:
        if self._data is None:
            raise PermanentBrokerError("market data client not configured")
        req = StockLatestTradeRequest(
            symbol_or_symbols=symbol,
            feed=_to_sdk_feed(self._data_feed),
        )

        def call() -> Any:
            return self._data.get_stock_latest_trade(req)

        raw = _with_retry(call, self._retry, sleep=self._sleep)
        return _extract_latest_trade(raw, symbol)

    # ------------------------------------------------------------------ #
    # Submit / cancel / flatten — action
    # ------------------------------------------------------------------ #

    def submit_entry_with_protection(self, intent: OrderIntent) -> SubmittedOrder:
        """Submit the OTO parent + stop child atomically.

        Idempotent: the COID comes from the intent, so repeat submissions
        resolve to the same order. A duplicate COID is not an error from
        the bot's perspective — it's proof the previous submission
        succeeded, and we fetch that order by COID and return it.
        """
        req = LimitOrderRequest(
            symbol=intent.symbol,
            qty=intent.qty,
            side=SdkOrderSide.BUY,
            time_in_force=SdkTimeInForce.DAY,
            extended_hours=False,
            limit_price=float(intent.limit_price),
            client_order_id=intent.client_order_id(),
            order_class=SdkOrderClass.OTO,
            stop_loss=StopLossRequest(stop_price=float(intent.disaster_stop_price)),
        )

        def call() -> Any:
            return self._client.submit_order(order_data=req)

        try:
            raw = _with_retry(call, self._retry, sleep=self._sleep)
        except DuplicateClientOrderId:
            existing = self.get_order_by_coid(intent.client_order_id())
            if existing is None:
                # Duplicate but can't fetch — genuine problem, surface it.
                raise
            return SubmittedOrder(parent=existing, stop_child=_pick_stop_child(existing))

        parent = _to_broker_order(raw)
        return SubmittedOrder(parent=parent, stop_child=_pick_stop_child(raw))

    def poll_terminal(
        self,
        client_order_id: str,
        timeout_s: float | None = None,
    ) -> BrokerOrder:
        """Poll by COID until the order reaches a terminal status."""
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else self._poll_timeout_s)
        last_seen: BrokerOrder | None = None
        while time.monotonic() < deadline:
            order = self.get_order_by_coid(client_order_id)
            if order is None:
                # Not yet visible — treat as non-terminal and keep polling.
                self._sleep(self._poll_interval_s)
                continue
            last_seen = order
            if order.is_terminal():
                return order
            self._sleep(self._poll_interval_s)
        raise TimeoutError(
            f"order {client_order_id} did not reach terminal status within "
            f"{timeout_s or self._poll_timeout_s}s "
            f"(last status={last_seen.status.value if last_seen else 'unknown'})"
        )

    def cancel_order(self, broker_order_id: str) -> None:
        """Idempotent cancel. A cancel on an already-terminal order is a
        no-op — Alpaca returns 422 which we map to ``OrderRejected``; we
        swallow that specific case because the order is already at rest.
        """
        def call() -> None:
            self._client.cancel_order_by_id(broker_order_id)

        try:
            _with_retry(call, self._retry, sleep=self._sleep)
        except OrderRejected:
            # Already terminal; treat as success.
            log.info("cancel on already-terminal order %s: no-op", broker_order_id)
        except PermanentBrokerError as exc:
            if "404" in str(exc):
                # Already cancelled or cleaned up.
                log.info("cancel on unknown order %s: no-op", broker_order_id)
                return
            raise

    def cancel_all_open_orders_for_symbol(self, symbol: str) -> list[str]:
        orders = self.get_open_orders(symbol=symbol)
        ids: list[str] = []
        for o in orders:
            self.cancel_order(o.broker_order_id)
            ids.append(o.broker_order_id)
        return ids

    def submit_market_close(
        self,
        symbol: str,
        qty: int,
        client_order_id: str,
    ) -> BrokerOrder:
        if qty <= 0:
            raise PermanentBrokerError(f"submit_market_close: qty must be positive, got {qty}")
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=SdkOrderSide.SELL,
            time_in_force=SdkTimeInForce.DAY,
            extended_hours=False,
            client_order_id=client_order_id,
        )

        def call() -> Any:
            return self._client.submit_order(order_data=req)

        raw = _with_retry(call, self._retry, sleep=self._sleep)
        return _to_broker_order(raw)

    def flatten_symbol(
        self,
        symbol: str,
        close_client_order_id: str,
    ) -> CloseResult:
        """Safe flatten sequence — the exact ordering the plan locks in.

        1) Cancel every open order on the symbol (including OTO children).
        2) Poll each cancel to terminal.
        3) Submit a market SELL sized to the broker's current position qty.
        4) Poll the close to terminal.
        5) Verify the broker position is zero; raise if not.
        """
        cancelled: list[str] = []
        for order in self.get_open_orders(symbol=symbol):
            self.cancel_order(order.broker_order_id)
            cancelled.append(order.broker_order_id)
        # Poll cancels to terminal.
        for oid in cancelled:
            self._wait_for_cancel_terminal(oid)

        positions = {p.symbol: p for p in self.get_positions()}
        pos = positions.get(symbol)
        if pos is None or pos.qty == 0:
            # Nothing to flatten — return a synthetic result. Upstream
            # reconciliation will have already noted any local drift.
            return CloseResult(
                symbol=symbol,
                cancelled_order_ids=tuple(cancelled),
                close_order=_synthetic_noop_order(symbol, close_client_order_id),
                final_position_qty=0,
            )
        close = self.submit_market_close(symbol, qty=pos.qty, client_order_id=close_client_order_id)
        close_terminal = self.poll_terminal(close.client_order_id)

        # Verify broker truth.
        after = {p.symbol: p for p in self.get_positions()}
        final_qty = after[symbol].qty if symbol in after else 0
        if final_qty != 0:
            raise PermanentBrokerError(
                f"flatten_symbol: position for {symbol} still {final_qty} after close"
            )
        return CloseResult(
            symbol=symbol,
            cancelled_order_ids=tuple(cancelled),
            close_order=close_terminal,
            final_position_qty=0,
        )

    # ------------------------------------------------------------------ #
    # Diagnostics (for the opt-in paper smoke test only)
    # ------------------------------------------------------------------ #

    def diagnose_order_by_coid(self, client_order_id: str) -> dict:
        """Fetch a raw SDK order by COID and return its full field dump.

        Bypasses our typed DTO translation. Used by
        ``scripts/paper_smoke.py`` to expose exactly what alpaca-py
        returned when a wrapper-level assumption appears to be wrong.
        Not used by the orchestrator.
        """
        def call() -> Any:
            return self._client.get_order_by_client_id(client_order_id)

        raw = _with_retry(call, self._retry, sleep=self._sleep)
        return _order_attrs_for_diagnostics(raw)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _wait_for_cancel_terminal(self, broker_order_id: str) -> None:
        """Poll a specific broker_order_id until it's no longer open."""
        deadline = time.monotonic() + self._poll_timeout_s
        while time.monotonic() < deadline:
            opens = self.get_open_orders()
            if not any(o.broker_order_id == broker_order_id for o in opens):
                return
            self._sleep(self._poll_interval_s)
        raise TimeoutError(
            f"cancel of {broker_order_id} did not reach terminal within "
            f"{self._poll_timeout_s}s"
        )


# ---------------------------------------------------------------------------
# Conversion helpers (SDK shapes → our DTOs)
# ---------------------------------------------------------------------------


def _to_position(raw: Any) -> Position:
    return Position(
        symbol=str(raw.symbol),
        qty=int(Decimal(str(raw.qty))),
        avg_entry_price=Decimal(str(raw.avg_entry_price)),
        market_value=Decimal(str(getattr(raw, "market_value", "0"))),
        unrealized_pl=Decimal(str(getattr(raw, "unrealized_pl", "0"))),
        side=_map_side(getattr(raw, "side", "long")),
    )


def _to_broker_order(
    raw: Any,
    *,
    parent_client_order_id: str | None = None,
    parent_broker_order_id: str | None = None,
    leg_role: str | None = None,
) -> BrokerOrder:
    """Convert a raw SDK order into a typed :class:`BrokerOrder`.

    ``parent_client_order_id`` / ``parent_broker_order_id`` / ``leg_role``
    are supplied by callers that have the parent context (i.e. are
    iterating ``parent.legs``). Alpaca does not populate any
    back-reference from an OTO child to its parent on the child's own
    record — observed on paper 2026-04-23 — so the only reliable
    linkage is the traversal the caller performs.

    For top-level orders the three kwargs are ``None`` and the fields
    stay empty, which is correct.
    """
    avg_fill = getattr(raw, "filled_avg_price", None)
    filled_at = getattr(raw, "filled_at", None)
    # If no explicit parent context, fall back to whatever the SDK
    # populated. For top-level orders and for every leg Alpaca has
    # returned to date, these fall-through values are None.
    if parent_client_order_id is None:
        parent_client_order_id = getattr(raw, "parent_client_order_id", None) or getattr(
            raw, "_parent_client_order_id", None
        )
    effective_role = leg_role if leg_role is not None else _derive_leg_role(raw)
    return BrokerOrder(
        broker_order_id=str(raw.id),
        client_order_id=str(raw.client_order_id),
        symbol=str(raw.symbol),
        side=_map_side(raw.side),
        qty=int(Decimal(str(raw.qty))),
        filled_qty=int(Decimal(str(raw.filled_qty or 0))),
        avg_fill_price=Decimal(str(avg_fill)) if avg_fill is not None else None,
        status=_map_status(raw.status),
        order_class=_map_order_class(getattr(raw, "order_class", SdkOrderClass.SIMPLE)),
        submitted_at=_coerce_utc(raw.submitted_at),
        filled_at=_coerce_utc(filled_at) if filled_at else None,
        parent_client_order_id=(
            str(parent_client_order_id) if parent_client_order_id else None
        ),
        leg_role=effective_role,
        parent_broker_order_id=(
            str(parent_broker_order_id) if parent_broker_order_id else None
        ),
    )


def _flatten_orders(raws: list[Any]) -> list[BrokerOrder]:
    """Flatten nested orders (parent + legs) into a single list.

    Leg→parent linkage is established by traversal: a leg is known to
    belong to a parent because we encountered it inside ``parent.legs``.
    We pass that parent context explicitly into :func:`_to_broker_order`
    rather than attempting ``setattr`` on the pydantic leg (which is a
    silent no-op on pydantic v2).
    """
    out: list[BrokerOrder] = []
    for r in raws:
        out.append(_to_broker_order(r))
        parent_coid = getattr(r, "client_order_id", None)
        parent_bid = getattr(r, "id", None)
        for leg in getattr(r, "legs", None) or []:
            out.append(
                _to_broker_order(
                    leg,
                    parent_client_order_id=parent_coid,
                    parent_broker_order_id=str(parent_bid) if parent_bid else None,
                    leg_role=_role_from_order_type(leg),
                )
            )
    return out


def _derive_leg_role(raw: Any) -> str | None:
    """Role derivation for orders we do *not* have parent context for.

    Callers that do have parent context pass ``leg_role`` explicitly to
    :func:`_to_broker_order`. This helper only decides between "parent"
    and ``None`` for top-level orders that may or may not have legs.
    """
    if getattr(raw, "legs", None):
        return "parent"
    return None


def _role_from_order_type(leg: Any) -> str:
    """Map an alpaca-py leg's ``order_type`` to our leg_role label."""
    t = getattr(leg, "order_type", None) or getattr(leg, "type", None)
    t_str = str(t).lower() if t is not None else ""
    if "stop" in t_str:
        return "stop_child"
    if "limit" in t_str:
        return "take_profit_child"
    return "child"


def _pick_stop_child(raw: Any) -> BrokerOrder | None:
    """Find the OTO stop_loss leg on a parent order, if any.

    Relies on ``parent.legs`` — the only reliable linkage Alpaca provides.
    """
    legs = getattr(raw, "legs", None) or []
    parent_coid = getattr(raw, "client_order_id", None)
    parent_bid = getattr(raw, "id", None)
    for leg in legs:
        t = getattr(leg, "order_type", None) or getattr(leg, "type", None)
        if t is not None and "stop" in str(t).lower():
            return _to_broker_order(
                leg,
                parent_client_order_id=parent_coid,
                parent_broker_order_id=str(parent_bid) if parent_bid else None,
                leg_role="stop_child",
            )
    return None


def _coerce_utc(dt: Any) -> datetime:
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    # String ISO-8601 from some SDK paths.
    if isinstance(dt, str):
        parsed = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    # Fallback — produce something deterministic rather than None.
    return datetime.now(timezone.utc)


def _extract_bars(raw: Any, symbol: str) -> list[Bar]:
    """Pull a list of :class:`Bar` from the SDK response.

    alpaca-py's ``BarSet`` exposes ``.data`` as ``dict[str, list[BarModel]]``
    keyed by symbol. An empty or missing list for the symbol returns ``[]``.
    """
    data = getattr(raw, "data", None)
    if data is None:
        return []
    entries = data.get(symbol) or []
    out: list[Bar] = []
    for b in entries:
        out.append(
            Bar(
                symbol=symbol,
                ts=_coerce_utc(b.timestamp),
                open=Decimal(str(b.open)),
                high=Decimal(str(b.high)),
                low=Decimal(str(b.low)),
                close=Decimal(str(b.close)),
                volume=int(b.volume or 0),
                trade_count=int(getattr(b, "trade_count", 0) or 0) or None,
                vwap=Decimal(str(b.vwap)) if getattr(b, "vwap", None) is not None else None,
            )
        )
    return out


def _extract_latest_quote(raw: Any, symbol: str) -> Quote:
    data = getattr(raw, "data", None) if not isinstance(raw, dict) else raw
    if data is None:
        raise PermanentBrokerError(f"no latest quote payload for {symbol}")
    q = data.get(symbol) if hasattr(data, "get") else None
    if q is None:
        raise PermanentBrokerError(f"latest quote for {symbol} missing in response")
    return Quote(
        symbol=symbol,
        ts=_coerce_utc(q.timestamp),
        bid_price=Decimal(str(q.bid_price)),
        ask_price=Decimal(str(q.ask_price)),
        bid_size=int(q.bid_size or 0),
        ask_size=int(q.ask_size or 0),
    )


def _extract_latest_trade(raw: Any, symbol: str) -> Trade:
    data = getattr(raw, "data", None) if not isinstance(raw, dict) else raw
    if data is None:
        raise PermanentBrokerError(f"no latest trade payload for {symbol}")
    t = data.get(symbol) if hasattr(data, "get") else None
    if t is None:
        raise PermanentBrokerError(f"latest trade for {symbol} missing in response")
    return Trade(
        symbol=symbol,
        ts=_coerce_utc(t.timestamp),
        price=Decimal(str(t.price)),
        size=int(t.size or 0),
    )


# ---------------------------------------------------------------------------
# Diagnostics (used by scripts/paper_smoke.py to inspect raw SDK shapes)
# ---------------------------------------------------------------------------


def _order_attrs_for_diagnostics(raw: Any) -> Any:
    """Return a JSON-serialisable view of an SDK Order.

    The preferred path is pydantic's ``model_dump(mode="json")`` because
    alpaca-py's Order model is a pydantic BaseModel; that returns the
    full field set without us having to guess field names. The fallback
    hand-walks a superset of the fields we care about so we still get
    useful output against a non-pydantic mock.

    This helper is intentionally live-only — it exists so the smoke
    script can tell the operator *exactly* what shape alpaca-py
    returned for a parent order and its legs, without requiring changes
    to the DTO mapping. It is not used by the orchestrator.
    """
    if raw is None:
        return None
    # pydantic v2
    fn = getattr(raw, "model_dump", None)
    if callable(fn):
        try:
            return fn(mode="json")
        except Exception:  # noqa: BLE001 — fall through to manual walk
            pass
    # pydantic v1
    fn = getattr(raw, "dict", None)
    if callable(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, Any] = {}
    for key in (
        "id",
        "client_order_id",
        "symbol",
        "side",
        "qty",
        "filled_qty",
        "status",
        "order_class",
        "order_type",
        "type",
        "parent_id",
        "parent_client_order_id",
        "stop_price",
        "limit_price",
        "filled_avg_price",
        "submitted_at",
        "filled_at",
    ):
        val = getattr(raw, key, None)
        out[key] = str(val) if val is not None else None
    legs = getattr(raw, "legs", None)
    if legs:
        out["legs"] = [_order_attrs_for_diagnostics(leg) for leg in legs]
    else:
        out["legs"] = None
    return out


def _synthetic_noop_order(symbol: str, client_order_id: str) -> BrokerOrder:
    now = datetime.now(timezone.utc)
    return BrokerOrder(
        broker_order_id="noop",
        client_order_id=client_order_id,
        symbol=symbol,
        side=OrderSide.SELL,
        qty=0,
        filled_qty=0,
        avg_fill_price=None,
        status=OrderStatus.CANCELED,
        order_class=OrderClass.SIMPLE,
        submitted_at=now,
        filled_at=now,
        parent_client_order_id=None,
        leg_role=None,
    )


# Re-export for the test module's convenience. Tests import the SDK
# symbols via this module rather than importing alpaca themselves, which
# keeps the "alpaca-py imported only in broker.py" invariant clean.
__all__ = [
    "AlpacaBroker",
    "BarTimeframe",
    "RetryPolicy",
    "_classify",
    "_map_status",
    "APIError",
    "LimitOrderRequest",
    "StopLossRequest",
    "MarketOrderRequest",
    "SdkOrderClass",
    "SdkOrderSide",
    "SdkOrderStatus",
    "SdkTimeInForce",
]
