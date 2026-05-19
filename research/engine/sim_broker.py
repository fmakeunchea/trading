"""SimulatedBroker — Phase 1 sim spine.

╔══════════════════════════════════════════════════════════════════════╗
║  PHASE 1 LIMITATIONS — READ BEFORE INTERPRETING ANY OUTPUT           ║
║                                                                      ║
║  This broker models PERFECT FILLS WITH NO COST.                      ║
║                                                                      ║
║  * Limit entries fill exactly at ``intent.limit_price``, immediately.║
║  * Quotes are synthesised: ``bid == ask == mid == latest 1m close``  ║
║    → ``spread_bps == 0``  → the production ``spread_too_wide`` risk  ║
║    gate NEVER FIRES → backtests produce MORE entries than live →     ║
║    treat any entry count / P&L as a PERMISSIVE UPPER BOUND.          ║
║  * No commissions, no slippage, no partial fills, no rejections.     ║
║                                                                      ║
║  Phase 2 introduces realistic spread / slippage / fills. NOT YET.    ║
║  No number produced through this broker is a verdict on the          ║
║  strategy's edge. See [[project_backtester_design.md]] §4.           ║
╚══════════════════════════════════════════════════════════════════════╝

Implements the engine's duck-typed broker surface so the production
``strategy.Strategy`` class drives unchanged. Keystone reuse: the seam
is the one already proven by ``tests/test_strategy.py``'s ``FakeBroker``;
this is that pattern grown into a production-grade simulator.

Anti-leak: every data accessor (:meth:`get_bars`, :meth:`get_latest_quote`)
clips to bars/quotes with ``close_ts <= self._now``. The clock is set by
the BacktestDriver (1.2) before each tick.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

from strategy.dto import (
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
)
from strategy.time_utils import TF_NAME_TO_MINUTES

PHASE1_SPREAD_BPS: Decimal = Decimal(0)
"""LOUD: Phase 1 quote synthesis uses spread=0. See file header for why."""


@dataclass(slots=True)
class _LocalPosition:
    """Internal accounting position (qty + avg fill price)."""
    symbol: str
    qty: int
    avg_entry_price: Decimal


def _tf_name_to_minutes(tf: Any) -> int:
    """Accepts a ``BarTimeframe`` (has ``.value``) or raw minute count/str."""
    if hasattr(tf, "value"):
        v = tf.value
        if v in TF_NAME_TO_MINUTES:
            return TF_NAME_TO_MINUTES[v]
    if isinstance(tf, int):
        return tf
    if isinstance(tf, str) and tf in TF_NAME_TO_MINUTES:
        return TF_NAME_TO_MINUTES[tf]
    raise KeyError(f"unrecognised timeframe: {tf!r}")


def _row_to_bar(symbol: str, ts: datetime, row) -> Bar:
    """One pandas resampled-frame row → engine ``Bar`` DTO. Float→Decimal."""
    return Bar(
        symbol=symbol,
        ts=ts,
        open=Decimal(str(row["open"])),
        high=Decimal(str(row["high"])),
        low=Decimal(str(row["low"])),
        close=Decimal(str(row["close"])),
        volume=int(row["volume"]),
    )


class SimulatedBroker:
    """Engine-facing duck-typed broker, backed by pre-loaded bar frames.

    Construction takes a pre-loaded ``bars`` dict so this class is
    testable offline (in-memory frames) and reusable in production
    (frames loaded by :func:`load_bars_from_cache`, which is a separate
    helper to keep cache/resample concerns out of the broker).
    """

    def __init__(
        self,
        *,
        bars: dict[tuple[str, int], Any],
        starting_cash: Decimal,
        now: datetime | None = None,
    ) -> None:
        """``bars`` maps ``(symbol, tf_minutes) -> pandas DataFrame`` with the
        Phase-0 resample schema (UTC tz-aware index, OHLCV columns).
        ``now`` may be ``None`` until the driver calls :meth:`set_now`."""
        if starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        self._bars: dict[tuple[str, int], Any] = dict(bars)
        self._cash: Decimal = Decimal(starting_cash)
        self._now: datetime | None = now
        self._positions: dict[str, _LocalPosition] = {}
        self._open_orders: list[BrokerOrder] = []
        self._submitted: dict[str, SubmittedOrder] = {}   # by parent COID
        self._symbols_seen: set[str] = {sym for (sym, _) in self._bars}
        # Per-instance, monotonic broker_order_id counter — deterministic
        # by construction across runs (no module-level state).
        self._oid_seq: int = 0

    def _next_oid(self, prefix: str) -> str:
        self._oid_seq += 1
        return f"sim-{prefix}-{self._oid_seq:08d}"

    # ---- clock ----------------------------------------------------------

    def set_now(self, ts: datetime) -> None:
        if ts.tzinfo is None or ts.utcoffset() != timedelta(0):
            raise ValueError("now must be UTC tz-aware")
        if self._now is not None and ts < self._now:
            raise ValueError("clock must be monotonic non-decreasing")
        self._now = ts

    def _require_now(self) -> datetime:
        if self._now is None:
            raise RuntimeError("SimulatedBroker.set_now() not called yet")
        return self._now

    # ---- internal: most-recent-completed 1m close (for quote / MTM) ----

    def _latest_mid(self, symbol: str) -> Decimal | None:
        """Most recent bar at the smallest available tf for ``symbol`` whose
        ``close_ts <= now``, using its close as the mid. Phase 1: bid=ask=mid."""
        now = self._require_now()
        # Prefer 1m if loaded; otherwise pick the smallest tf for symbol.
        tfs = sorted(tf for (s, tf) in self._bars if s == symbol)
        if not tfs:
            return None
        for tf_min in tfs:
            df = self._bars.get((symbol, tf_min))
            if df is None or df.empty:
                continue
            # close_ts = ts + tf; want close_ts <= now → ts <= now - tf
            ts_cutoff = now - timedelta(minutes=tf_min)
            usable = df.loc[df.index <= ts_cutoff]
            if not usable.empty:
                return Decimal(str(usable.iloc[-1]["close"]))
        return None

    # ---- engine-facing surface -----------------------------------------

    def get_account_snapshot(self) -> AccountSnapshot:
        now = self._require_now()
        position_value = Decimal(0)
        for sym, pos in self._positions.items():
            mid = self._latest_mid(sym) or pos.avg_entry_price
            position_value += mid * Decimal(pos.qty)
        equity = self._cash + position_value
        return AccountSnapshot(
            ts=now, equity=equity, last_equity=equity,
            buying_power=equity, cash=self._cash, pattern_day_trader=False,
        )

    def get_positions(self) -> list[Position]:
        out: list[Position] = []
        for sym, pos in self._positions.items():
            mid = self._latest_mid(sym) or pos.avg_entry_price
            out.append(Position(
                symbol=sym, qty=pos.qty, avg_entry_price=pos.avg_entry_price,
                market_value=mid * Decimal(pos.qty),
                unrealized_pl=(mid - pos.avg_entry_price) * Decimal(pos.qty),
                side=OrderSide.BUY,
            ))
        return out

    def get_open_orders(self, symbol: str | None = None) -> list[BrokerOrder]:
        if symbol is None:
            return list(self._open_orders)
        return [o for o in self._open_orders if o.symbol == symbol]

    def get_bars(self, symbol, timeframe, *, start: datetime,
                 end: datetime, limit: int | None = None) -> list[Bar]:
        """Anti-leak: returns bars with ``close_ts <= now`` AND ``ts`` in
        ``[start, end]``. Empty list if none available yet."""
        now = self._require_now()
        tf_min = _tf_name_to_minutes(timeframe)
        df = self._bars.get((symbol, tf_min))
        if df is None or df.empty:
            return []
        ts_cutoff = now - timedelta(minutes=tf_min)
        mask = (df.index >= start) & (df.index <= end) & (df.index <= ts_cutoff)
        win = df.loc[mask]
        if limit is not None and len(win) > limit:
            win = win.iloc[-limit:]
        return [_row_to_bar(symbol, ts, row) for ts, row in win.iterrows()]

    def get_latest_quote(self, symbol: str) -> Quote:
        """PHASE 1 SYNTHESIS — see file header.

        ``bid == ask == mid == most-recent-completed 1m close``;
        ``spread_bps == 0``; the production ``spread_too_wide`` risk gate
        will never fire while running through this broker.
        """
        now = self._require_now()
        mid = self._latest_mid(symbol)
        if mid is None:
            raise KeyError(f"no quote available for {symbol} at {now.isoformat()}")
        return Quote(
            symbol=symbol, ts=now, bid_price=mid, ask_price=mid,
            bid_size=100, ask_size=100,
        )

    # ---- order lifecycle (perfect fills, no cost) ----------------------

    def submit_entry_with_protection(self, intent: OrderIntent) -> SubmittedOrder:
        """Long-only OTO entry, perfect fill at ``intent.limit_price``.

        Cash is debited immediately by ``qty * limit_price``; a Position
        and a protective stop_child (status HELD) are created atomically.
        Returns the SubmittedOrder; ``poll_terminal`` will return the
        already-FILLED parent.
        """
        now = self._require_now()
        if intent.side is not OrderSide.BUY:
            raise NotImplementedError("Phase 1 is long-only (matches OrderIntent)")
        coid = intent.client_order_id()
        if coid in self._submitted:
            return self._submitted[coid]  # idempotent (mirrors real broker)

        fill_price = intent.limit_price          # PHASE 1: no slippage
        debit = fill_price * Decimal(intent.qty)
        if debit > self._cash:
            # Treat as a broker-side rejection — produce a non-filled parent.
            parent = BrokerOrder(
                broker_order_id=self._next_oid("rej"), client_order_id=coid,
                symbol=intent.symbol, side=intent.side, qty=intent.qty,
                filled_qty=0, avg_fill_price=None, status=OrderStatus.REJECTED,
                order_class=OrderClass.OTO, submitted_at=now, filled_at=None,
                parent_client_order_id=None, leg_role="parent",
            )
            submitted = SubmittedOrder(parent=parent, stop_child=None)
            self._submitted[coid] = submitted
            return submitted

        self._cash -= debit
        self._positions[intent.symbol] = _LocalPosition(
            symbol=intent.symbol, qty=intent.qty, avg_entry_price=fill_price,
        )
        parent = BrokerOrder(
            broker_order_id=self._next_oid("parent"), client_order_id=coid,
            symbol=intent.symbol, side=intent.side, qty=intent.qty,
            filled_qty=intent.qty, avg_fill_price=fill_price,
            status=OrderStatus.FILLED, order_class=OrderClass.OTO,
            submitted_at=now, filled_at=now,
            parent_client_order_id=None, leg_role="parent",
        )
        stop_child = BrokerOrder(
            broker_order_id=self._next_oid("stop"),
            client_order_id=f"sim-stop-{coid}",
            symbol=intent.symbol, side=OrderSide.SELL, qty=intent.qty,
            filled_qty=0, avg_fill_price=None, status=OrderStatus.HELD,
            order_class=OrderClass.SIMPLE,
            submitted_at=now, filled_at=None,
            parent_client_order_id=coid, leg_role="stop_child",
            parent_broker_order_id=parent.broker_order_id,
        )
        self._open_orders.append(stop_child)
        submitted = SubmittedOrder(parent=parent, stop_child=stop_child)
        self._submitted[coid] = submitted
        return submitted

    def poll_terminal(self, client_order_id: str,
                      timeout_s: float | None = None) -> BrokerOrder:
        sub = self._submitted.get(client_order_id)
        if sub is None:
            raise KeyError(f"unknown coid: {client_order_id}")
        return sub.parent       # already terminal in Phase 1

    def resolve_by_coid(self, client_order_id: str) -> SubmittedOrder | None:
        return self._submitted.get(client_order_id)

    def flatten_symbol(self, symbol: str,
                       close_client_order_id: str) -> CloseResult:
        """Cancel any open protective children for ``symbol``, then close
        the position at the current synthesized mid (Phase 1: no slippage)."""
        now = self._require_now()
        cancelled: list[str] = []
        keep: list[BrokerOrder] = []
        for o in self._open_orders:
            if o.symbol == symbol and o.status is OrderStatus.HELD:
                cancelled.append(o.broker_order_id)
            else:
                keep.append(o)
        self._open_orders = keep

        pos = self._positions.pop(symbol, None)
        if pos is None or pos.qty == 0:
            close = BrokerOrder(
                broker_order_id=self._next_oid("close-noop"),
                client_order_id=close_client_order_id,
                symbol=symbol, side=OrderSide.SELL, qty=0, filled_qty=0,
                avg_fill_price=None, status=OrderStatus.FILLED,
                order_class=OrderClass.SIMPLE, submitted_at=now, filled_at=now,
                parent_client_order_id=None, leg_role=None,
            )
            return CloseResult(symbol=symbol, cancelled_order_ids=tuple(cancelled),
                               close_order=close, final_position_qty=0)

        exit_price = self._latest_mid(symbol) or pos.avg_entry_price
        self._cash += exit_price * Decimal(pos.qty)
        close = BrokerOrder(
            broker_order_id=self._next_oid("close"),
            client_order_id=close_client_order_id,
            symbol=symbol, side=OrderSide.SELL, qty=pos.qty, filled_qty=pos.qty,
            avg_fill_price=exit_price, status=OrderStatus.FILLED,
            order_class=OrderClass.SIMPLE, submitted_at=now, filled_at=now,
            parent_client_order_id=None, leg_role=None,
        )
        return CloseResult(symbol=symbol, cancelled_order_ids=tuple(cancelled),
                           close_order=close, final_position_qty=0)


