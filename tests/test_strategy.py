"""Orchestrator safety-path tests.

The orchestrator is exercised with a fake broker (plain Python object
implementing the methods the orchestrator calls). We verify:

* Reconcile mismatch blocks new entries.
* Kill switch blocks new entries (but exits still flow).
* Stale data blocks entry (risk.py catches; surfaces as deny).
* Daily loss halt flattens and blocks.
* Drawdown halt flattens and blocks.
* Compounding throttle reduces size under drawdown (risk-layer decision
  propagates into the submitted qty).
* Partial fill records an ``OpenTrade`` with broker's actual filled qty,
  never the requested qty.
* Session-end flattens all positions.
* Restart recovery with an unexpected broker position either rebuilds
  the OpenTrade (log match) or halts.
* Graceful shutdown flattens when asked.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategy.config import load_config
from strategy.dto import (
    COID_PREFIX,
    AccountSnapshot,
    Bar,
    BrokerOrder,
    CloseResult,
    OpenTrade,
    OrderClass,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
    Quote,
    SubmittedOrder,
    TimeInForce,
)
from strategy.errors import OrderOutcomeUnknown
from strategy.state import StateStore, StrategyState
from strategy.strategy import Strategy, TickReport
from strategy.trade_log import RecordKind, TradeLog


UTC = timezone.utc
FIXTURE = Path(__file__).parent / "fixtures" / "sample_config.yaml"
TEST_ENV = {"TEST_ALPACA_API_KEY": "k", "TEST_ALPACA_API_SECRET": "s"}


# ---------------------------------------------------------------------------
# FakeBroker — a hand-rolled stand-in for AlpacaBroker.
#
# Using a hand-rolled class rather than MagicMock makes the *interface*
# the orchestrator depends on visible, and makes it harder for an
# orchestrator bug to pass by accident (e.g. a typoed method call
# would silently return a MagicMock, not raise).
# ---------------------------------------------------------------------------


class FakeBroker:
    """Test double implementing the AlpacaBroker surface the orchestrator uses."""

    def __init__(self) -> None:
        self.account = AccountSnapshot(
            ts=datetime(2026, 4, 23, 14, 30, tzinfo=UTC),
            equity=Decimal("25000"),
            last_equity=Decimal("25000"),
            buying_power=Decimal("50000"),
            cash=Decimal("12500"),
            pattern_day_trader=False,
        )
        self.positions: list[Position] = []
        self.open_orders: list[BrokerOrder] = []
        self.bars: dict[tuple[str, str], list[Bar]] = {}   # (symbol, tf_value)
        self.latest_quotes: dict[str, Quote] = {}
        self.submit_calls: list[OrderIntent] = []
        self.flatten_calls: list[str] = []
        self._force_submit_error: Exception | None = None
        self._force_poll_status = OrderStatus.FILLED
        self._force_poll_filled_qty: int | None = None
        self._force_quote_stale: bool = False
        self._force_close_price: Decimal | None = None
        # Tier 3 recovery hooks.
        self._force_poll_error: Exception | None = None
        self._force_flatten_error: Exception | None = None
        self._resolvable_coids: bool = True  # resolve_by_coid returns the order
        self.resolve_calls: list[str] = []
        self.call_log: list[str] = []  # ordering: recovery-before-reconcile

    # ---- trading-client-ish methods (used by orchestrator) ----
    def get_account_snapshot(self):
        return self.account

    def get_positions(self):
        self.call_log.append("get_positions")
        return list(self.positions)

    def get_open_orders(self, symbol=None):
        if symbol is None:
            return list(self.open_orders)
        return [o for o in self.open_orders if o.symbol == symbol]

    def get_bars(self, symbol, timeframe, *, start, end, limit=None):
        return list(self.bars.get((symbol, timeframe.value), []))

    def get_latest_quote(self, symbol):
        q = self.latest_quotes.get(symbol)
        if q is None:
            raise KeyError(symbol)
        if self._force_quote_stale:
            return Quote(
                symbol=symbol,
                ts=q.ts - timedelta(seconds=120),
                bid_price=q.bid_price,
                ask_price=q.ask_price,
                bid_size=q.bid_size,
                ask_size=q.ask_size,
            )
        return q

    def submit_entry_with_protection(self, intent: OrderIntent):
        self.submit_calls.append(intent)
        if self._force_submit_error is not None:
            raise self._force_submit_error
        parent = BrokerOrder(
            broker_order_id="b-" + intent.symbol,
            client_order_id=intent.client_order_id(),
            symbol=intent.symbol,
            side=OrderSide.BUY,
            qty=intent.qty,
            filled_qty=0,
            avg_fill_price=None,
            status=OrderStatus.NEW,
            order_class=OrderClass.OTO,
            submitted_at=intent.ts,
            filled_at=None,
            parent_client_order_id=None,
            leg_role="parent",
        )
        stop = BrokerOrder(
            broker_order_id="b-stop-" + intent.symbol,
            client_order_id=f"{COID_PREFIX}child-{intent.symbol}",
            symbol=intent.symbol,
            side=OrderSide.SELL,
            qty=intent.qty,
            filled_qty=0,
            avg_fill_price=None,
            status=OrderStatus.NEW,
            order_class=OrderClass.SIMPLE,
            submitted_at=intent.ts,
            filled_at=None,
            parent_client_order_id=intent.client_order_id(),
            leg_role="stop_child",
        )
        return SubmittedOrder(parent=parent, stop_child=stop)

    def poll_terminal(self, client_order_id, timeout_s=None):
        self.call_log.append("poll_terminal")
        if self._force_poll_error is not None:
            raise self._force_poll_error
        # Look up the intent we most recently saw with this COID.
        intent = next((i for i in self.submit_calls if i.client_order_id() == client_order_id), None)
        if intent is None:
            raise KeyError(client_order_id)
        filled = intent.qty if self._force_poll_filled_qty is None else self._force_poll_filled_qty
        return BrokerOrder(
            broker_order_id="b-" + intent.symbol,
            client_order_id=client_order_id,
            symbol=intent.symbol,
            side=OrderSide.BUY,
            qty=intent.qty,
            filled_qty=filled,
            avg_fill_price=intent.limit_price if filled > 0 else None,
            status=self._force_poll_status,
            order_class=OrderClass.OTO,
            submitted_at=intent.ts,
            filled_at=intent.ts if filled > 0 else None,
            parent_client_order_id=None,
            leg_role="parent",
        )

    def resolve_by_coid(self, client_order_id):
        """Read-only recovery lookup mirroring AlpacaBroker.resolve_by_coid."""
        self.resolve_calls.append(client_order_id)
        self.call_log.append("resolve_by_coid")
        if not self._resolvable_coids:
            return None
        intent = next(
            (i for i in self.submit_calls if i.client_order_id() == client_order_id),
            None,
        )
        if intent is None:
            return None
        filled = (
            intent.qty if self._force_poll_filled_qty is None
            else self._force_poll_filled_qty
        )
        parent = BrokerOrder(
            broker_order_id="b-" + intent.symbol,
            client_order_id=client_order_id,
            symbol=intent.symbol,
            side=OrderSide.BUY,
            qty=intent.qty,
            filled_qty=filled,
            avg_fill_price=intent.limit_price if filled > 0 else None,
            status=self._force_poll_status,
            order_class=OrderClass.OTO,
            submitted_at=intent.ts,
            filled_at=intent.ts if filled > 0 else None,
            parent_client_order_id=None,
            leg_role="parent",
        )
        stop = BrokerOrder(
            broker_order_id="b-stop-" + intent.symbol,
            client_order_id=f"{COID_PREFIX}child-{intent.symbol}",
            symbol=intent.symbol,
            side=OrderSide.SELL,
            qty=intent.qty,
            filled_qty=0,
            avg_fill_price=None,
            status=OrderStatus.HELD,
            order_class=OrderClass.SIMPLE,
            submitted_at=intent.ts,
            filled_at=None,
            parent_client_order_id=client_order_id,
            leg_role="stop_child",
        )
        return SubmittedOrder(parent=parent, stop_child=stop)

    def flatten_symbol(self, symbol, close_client_order_id):
        self.flatten_calls.append(symbol)
        if self._force_flatten_error is not None:
            raise self._force_flatten_error
        # Remove the position from our broker state.
        self.positions = [p for p in self.positions if p.symbol != symbol]
        close = BrokerOrder(
            broker_order_id="close-" + symbol,
            client_order_id=close_client_order_id,
            symbol=symbol,
            side=OrderSide.SELL,
            qty=0,
            filled_qty=0,
            avg_fill_price=self._force_close_price,
            status=OrderStatus.FILLED,
            order_class=OrderClass.SIMPLE,
            submitted_at=datetime.now(UTC),
            filled_at=datetime.now(UTC),
            parent_client_order_id=None,
            leg_role=None,
        )
        return CloseResult(
            symbol=symbol,
            cancelled_order_ids=(),
            close_order=close,
            final_position_qty=0,
        )


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _bar(symbol: str, ts: datetime, price: Decimal, hi_off=Decimal("0.05"), lo_off=Decimal("0.05")) -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts,
        open=price,
        high=price + hi_off,
        low=price - lo_off,
        close=price,
        volume=1000,
    )


def _synth_trend(
    symbol: str,
    n: int,
    *,
    start_price: Decimal,
    step: Decimal,
    start_ts: datetime,
    tf_minutes: int,
    force_breakout: bool = False,
    bar_range: Decimal = Decimal("0.80"),
) -> list[Bar]:
    """Synthesise a bar series wide enough to exceed the 40 bps min-edge
    filter when ATR is computed. Default bar range 0.80 at ~$200 → ~80
    bps of ATR*2, comfortably above 40.
    """
    bars = []
    price = start_price
    half = bar_range / Decimal(2)
    for i in range(n):
        ts = start_ts + timedelta(minutes=tf_minutes * i)
        o = price
        c = price + step
        bars.append(
            Bar(
                symbol=symbol, ts=ts,
                open=o, high=max(o, c) + half,
                low=min(o, c) - half,
                close=c, volume=1000,
            )
        )
        price = c
    if force_breakout and bars:
        last = bars[-1]
        lookback = max(b.high for b in bars[-21:-1]) if len(bars) >= 21 else last.high
        forced_close = lookback + Decimal("0.60")
        from dataclasses import replace
        bars[-1] = replace(last, close=forced_close, high=forced_close + half)
    return bars


@pytest.fixture
def cfg_and_paths(tmp_path: Path):
    state_path = tmp_path / "state.json"
    log_path = tmp_path / "trades.jsonl"
    # Rewrite the fixture's persistence paths into tmp_path.
    import yaml
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    data["persistence"]["state_path"] = str(state_path)
    data["persistence"]["trade_log_path"] = str(log_path)
    out = tmp_path / "cfg.yaml"
    out.write_text(yaml.safe_dump(data), encoding="utf-8")
    cfg = load_config(out, env=TEST_ENV)
    return cfg, state_path, log_path


@pytest.fixture
def broker() -> FakeBroker:
    b = FakeBroker()
    for sym in ("AAPL", "MSFT"):
        b.latest_quotes[sym] = Quote(
            symbol=sym,
            ts=datetime(2026, 4, 23, 14, 30, tzinfo=UTC),
            bid_price=Decimal("199.98"),
            ask_price=Decimal("200.02"),
            bid_size=100, ask_size=100,
        )
        b.bars[(sym, "5Min")] = _synth_trend(
            sym, 120, start_price=Decimal("180"), step=Decimal("0.10"),
            start_ts=datetime(2026, 4, 23, 13, 35, tzinfo=UTC),
            tf_minutes=5, force_breakout=True,
        )
        b.bars[(sym, "15Min")] = _synth_trend(
            sym, 80, start_price=Decimal("170"), step=Decimal("0.15"),
            start_ts=datetime(2026, 4, 23, 8, 0, tzinfo=UTC),
            tf_minutes=15,
        )
        b.bars[(sym, "1Hour")] = _synth_trend(
            sym, 250, start_price=Decimal("100"), step=Decimal("0.30"),
            start_ts=datetime(2026, 4, 15, 13, 35, tzinfo=UTC),
            tf_minutes=60,
        )
    return b


def _strategy(cfg_and_paths, broker) -> Strategy:
    cfg, state_path, log_path = cfg_and_paths
    store = StateStore(state_path, fsync=False)
    log = TradeLog(log_path, fsync=False)
    s = Strategy(cfg, broker, store, log)
    s.state.peak_equity = Decimal("25000")
    s.state.last_reconciled_equity = Decimal("25000")
    s.state.intraday_low_equity = Decimal("25000")
    return s


NOW = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Happy path: clean tick submits an entry
# ---------------------------------------------------------------------------


def test_clean_tick_submits_entry(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    rep = s.tick(NOW, kill_switch_present=False)
    assert rep.reconcile_report.is_clean()
    assert rep.entries_submitted, f"expected entry; got denies={rep.denies}"
    # State updated to reflect the open trade.
    assert s.state.open_trades


# ---------------------------------------------------------------------------
# Reconcile mismatch blocks new entries
# ---------------------------------------------------------------------------


def test_reconcile_mismatch_blocks_entries(cfg_and_paths, broker) -> None:
    # Inject an extra position the local state doesn't know about — and
    # no matching history for rebuild — so reconcile is not clean.
    broker.positions = [
        Position(
            symbol="UNKNOWN", qty=5, avg_entry_price=Decimal("50"),
            market_value=Decimal("250"), unrealized_pl=Decimal("0"),
            side=OrderSide.BUY,
        )
    ]
    s = _strategy(cfg_and_paths, broker)
    rec = s.recover(NOW)
    # Halted at recover (unrecoverable because no trade log history).
    assert rec.halted is True
    rep = s.tick(NOW, kill_switch_present=False)
    assert not rep.entries_submitted
    assert rep.halted or not rep.reconcile_report.is_clean()


# ---------------------------------------------------------------------------
# Kill switch blocks new entries
# ---------------------------------------------------------------------------


def test_kill_switch_blocks_new_entries(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    rep = s.tick(NOW, kill_switch_present=True)
    assert rep.kill_switch_blocked is True
    assert not rep.entries_submitted
    assert not s.state.open_trades


def test_kill_switch_does_not_block_exits(cfg_and_paths, broker) -> None:
    """Kill switch stops new entries but must not suppress exit logic.

    We pre-seed an open trade whose primary stop has already been breached,
    then tick with the kill switch set. The orchestrator must flatten.
    """
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    # Seed open trade and corresponding broker position.
    s.state.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL", qty=10,
        entry_price=Decimal("200.00"),
        entry_ts=NOW - timedelta(minutes=15),
        stop_price=Decimal("210.00"),   # deliberately high so mid of 200.00 is below
        disaster_stop_price=Decimal("195.00"),
        target_price=Decimal("205.00"),
        intent_id="seed", parent_client_order_id=f"{COID_PREFIX}seed",
        protective_child_client_order_id=f"{COID_PREFIX}child-AAPL",
        protective_child_broker_id="b-stop-AAPL",
        last_seen_broker_qty=10,
    )
    broker.positions = [
        Position(symbol="AAPL", qty=10, avg_entry_price=Decimal("200"),
                 market_value=Decimal("2000"), unrealized_pl=Decimal("0"),
                 side=OrderSide.BUY)
    ]
    broker.open_orders = [
        BrokerOrder(
            broker_order_id="b-stop-AAPL",
            client_order_id=f"{COID_PREFIX}child-AAPL",
            symbol="AAPL", side=OrderSide.SELL, qty=10,
            filled_qty=0, avg_fill_price=None, status=OrderStatus.NEW,
            order_class=OrderClass.OTO, submitted_at=NOW, filled_at=None,
            parent_client_order_id=f"{COID_PREFIX}seed",
            leg_role="stop_child",
        )
    ]
    rep = s.tick(NOW, kill_switch_present=True)
    assert rep.kill_switch_blocked is True
    assert "AAPL" in rep.exits_submitted
    assert "AAPL" not in s.state.open_trades


# ---------------------------------------------------------------------------
# Stale market data blocks entries
# ---------------------------------------------------------------------------


def test_stale_bars_block_entry(cfg_and_paths, broker) -> None:
    """Stale data gate fires against the latest bar close.

    Threshold = entry_tf (5min=300s) + stale_bar_grace_s (30s) = 330s.
    Final bar opens 20 minutes before NOW → closes 15 minutes before NOW
    → age 900s, comfortably past the 330s threshold.
    """
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    for sym in ("AAPL", "MSFT"):
        bars = _synth_trend(
            sym, 120,
            start_price=Decimal("180"), step=Decimal("0.10"),
            start_ts=NOW - timedelta(minutes=5 * 120 + 20),
            tf_minutes=5,
            force_breakout=True,
        )
        broker.bars[(sym, "5Min")] = bars
    rep = s.tick(NOW, kill_switch_present=False)
    assert not rep.entries_submitted
    assert rep.denies
    for _, reason in rep.denies:
        assert reason == "stale_bar", reason


# ---------------------------------------------------------------------------
# Daily-loss halt flattens and blocks
# ---------------------------------------------------------------------------


def test_daily_loss_halt_flattens_and_blocks(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    # Seed open trade + broker position.
    s.state.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL", qty=10,
        entry_price=Decimal("200.00"),
        entry_ts=NOW - timedelta(minutes=15),
        stop_price=Decimal("198.00"),
        disaster_stop_price=Decimal("196.00"),
        target_price=Decimal("204.00"),
        intent_id="seed", parent_client_order_id=f"{COID_PREFIX}seed",
        protective_child_client_order_id=f"{COID_PREFIX}child-AAPL",
        protective_child_broker_id="b-stop-AAPL",
        last_seen_broker_qty=10,
    )
    broker.positions = [
        Position(symbol="AAPL", qty=10, avg_entry_price=Decimal("200"),
                 market_value=Decimal("2000"), unrealized_pl=Decimal("0"),
                 side=OrderSide.BUY)
    ]
    broker.open_orders = [
        BrokerOrder(
            broker_order_id="b-stop-AAPL",
            client_order_id=f"{COID_PREFIX}child-AAPL",
            symbol="AAPL", side=OrderSide.SELL, qty=10,
            filled_qty=0, avg_fill_price=None, status=OrderStatus.NEW,
            order_class=OrderClass.OTO, submitted_at=NOW, filled_at=None,
            parent_client_order_id=f"{COID_PREFIX}seed",
            leg_role="stop_child",
        )
    ]
    # Drive realized PnL to breach the cap (cap = 3% of 25k = 750).
    s.state.realized_pnl_today = Decimal("-760")
    rep = s.tick(NOW, kill_switch_present=False)
    assert rep.halted and rep.halt_reason == "daily_loss_cap"
    assert "AAPL" in broker.flatten_calls
    assert s.state.has_halt("daily_loss_cap")
    assert not rep.entries_submitted


# ---------------------------------------------------------------------------
# Drawdown halt flattens and blocks
# ---------------------------------------------------------------------------


def test_drawdown_halt_flattens_and_blocks(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.state.peak_equity = Decimal("30000")
    s.state.last_reconciled_equity = Decimal("30000")
    s.state.intraday_low_equity = Decimal("30000")
    # Account equity dropped 10% from peak (halt = 8%).
    broker.account = AccountSnapshot(
        ts=NOW, equity=Decimal("27000"), last_equity=Decimal("30000"),
        buying_power=Decimal("50000"), cash=Decimal("12500"),
        pattern_day_trader=False,
    )
    s.recover(NOW)
    rep = s.tick(NOW, kill_switch_present=False)
    assert rep.halted and rep.halt_reason == "drawdown_halt"
    assert s.state.has_halt("drawdown_halt")
    assert not rep.entries_submitted


# ---------------------------------------------------------------------------
# Compounding throttle reduces qty under drawdown
# ---------------------------------------------------------------------------


def test_throttle_reduces_qty_under_drawdown(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.state.peak_equity = Decimal("25000")
    s.state.last_reconciled_equity = Decimal("24000")   # 4% DD
    s.state.intraday_low_equity = Decimal("24000")
    # Broker equity mirrors state's reconciled value for sizing.
    broker.account = AccountSnapshot(
        ts=NOW, equity=Decimal("24000"), last_equity=Decimal("25000"),
        buying_power=Decimal("50000"), cash=Decimal("12500"),
        pattern_day_trader=False,
    )
    s.recover(NOW)
    rep = s.tick(NOW, kill_switch_present=False)
    assert rep.entries_submitted, f"expected entry; denies={rep.denies}"
    intent = broker.submit_calls[-1]
    # throttle at 4% DD = 0.50 → 24000 * 0.05 * 0.50 = 600 notional → 3 shares at $200.
    assert intent.qty <= 4
    assert intent.qty >= 1


# ---------------------------------------------------------------------------
# Partial fill path: record broker's filled qty, not requested qty
# ---------------------------------------------------------------------------


def test_partial_fill_records_broker_qty(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    # Broker partially fills: parent expires as DONE_FOR_DAY with partial.
    broker._force_poll_status = OrderStatus.EXPIRED
    broker._force_poll_filled_qty = 2   # partial
    rep = s.tick(NOW, kill_switch_present=False)
    assert rep.entries_submitted
    assert s.state.open_trades
    for sym, trade in s.state.open_trades.items():
        assert trade.qty == 2, "partial-fill must record broker's filled qty, not requested"


def test_zero_fill_does_not_create_trade(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    broker._force_poll_status = OrderStatus.CANCELED
    broker._force_poll_filled_qty = 0
    rep = s.tick(NOW, kill_switch_present=False)
    # Entry attempt happened but no open trade recorded.
    assert rep.entries_submitted  # logical attempt
    assert not s.state.open_trades


# ---------------------------------------------------------------------------
# Session-end flatten
# ---------------------------------------------------------------------------


def test_session_end_flattens_positions(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    s.state.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL", qty=10,
        entry_price=Decimal("200.00"),
        entry_ts=NOW - timedelta(minutes=15),
        stop_price=Decimal("198.00"),
        disaster_stop_price=Decimal("196.00"),
        target_price=Decimal("204.00"),
        intent_id="seed", parent_client_order_id=f"{COID_PREFIX}seed",
        protective_child_client_order_id=f"{COID_PREFIX}child-AAPL",
        protective_child_broker_id="b-stop-AAPL",
        last_seen_broker_qty=10,
    )
    broker.positions = [
        Position(symbol="AAPL", qty=10, avg_entry_price=Decimal("200"),
                 market_value=Decimal("2000"), unrealized_pl=Decimal("0"),
                 side=OrderSide.BUY)
    ]
    broker.open_orders = [
        BrokerOrder(
            broker_order_id="b-stop-AAPL",
            client_order_id=f"{COID_PREFIX}child-AAPL",
            symbol="AAPL", side=OrderSide.SELL, qty=10,
            filled_qty=0, avg_fill_price=None, status=OrderStatus.NEW,
            order_class=OrderClass.OTO, submitted_at=NOW, filled_at=None,
            parent_client_order_id=f"{COID_PREFIX}seed",
            leg_role="stop_child",
        )
    ]
    # 19:44 UTC is after the flatten deadline (19:40 = 19:45 - 5min).
    close_time = datetime(2026, 4, 23, 19, 44, tzinfo=UTC)
    broker.account = AccountSnapshot(
        ts=close_time, equity=Decimal("25000"), last_equity=Decimal("25000"),
        buying_power=Decimal("50000"), cash=Decimal("12500"),
        pattern_day_trader=False,
    )
    broker.latest_quotes["AAPL"] = Quote(
        symbol="AAPL", ts=close_time,
        bid_price=Decimal("199.98"), ask_price=Decimal("200.02"),
        bid_size=100, ask_size=100,
    )
    rep = s.tick(close_time, kill_switch_present=False)
    assert "AAPL" in broker.flatten_calls
    assert "AAPL" not in s.state.open_trades


# ---------------------------------------------------------------------------
# Trailing stop (opt-in via config; defaults preserve baseline behaviour)
# ---------------------------------------------------------------------------

def _enable_trailing(s: Strategy, *,
                     breakeven_at_atr: str = "0.5",
                     activation_at_atr: str = "1.5",
                     distance_atr: str = "0.5") -> None:
    """Swap the Strategy's config for one with trailing enabled.

    StrategyConfig is frozen — use ``dataclasses.replace`` to build a
    new Config, then point ``s.config`` at it.
    """
    import dataclasses
    sp = dataclasses.replace(
        s.config.strategy,
        trailing_stop_enabled=True,
        trailing_breakeven_at_atr=Decimal(breakeven_at_atr),
        trailing_activation_at_atr=Decimal(activation_at_atr),
        trailing_distance_atr=Decimal(distance_atr),
    )
    s.config = dataclasses.replace(s.config, strategy=sp)


def _quote(price: str, ts: datetime = NOW) -> Quote:
    p = Decimal(price)
    return Quote(symbol="AAPL", ts=ts, bid_price=p, ask_price=p,
                 bid_size=100, ask_size=100)


def _seed_open_trade(s: Strategy, *,
                     entry_price: str = "200.00",
                     stop_price: str = "198.00",
                     entry_atr: str | None = "2.00") -> OpenTrade:
    t = OpenTrade(
        symbol="AAPL", qty=10,
        entry_price=Decimal(entry_price),
        entry_ts=NOW - timedelta(minutes=15),
        stop_price=Decimal(stop_price),
        disaster_stop_price=Decimal("196.00"),
        target_price=Decimal("204.00"),
        intent_id="seed", parent_client_order_id=f"{COID_PREFIX}seed",
        protective_child_client_order_id=f"{COID_PREFIX}child-AAPL",
        protective_child_broker_id="b-stop-AAPL",
        last_seen_broker_qty=10,
        entry_atr=Decimal(entry_atr) if entry_atr is not None else None,
        highest_seen_price=Decimal(entry_price),
    )
    s.state.open_trades["AAPL"] = t
    return t


def test_trailing_disabled_by_default_stop_never_moves(cfg_and_paths, broker) -> None:
    """Baseline behaviour: trailing is opt-in. Even with an entry_atr
    present and a strongly favorable quote, the stop must not move when
    ``trailing_stop_enabled`` is False (the fixture default)."""
    s = _strategy(cfg_and_paths, broker)
    assert s.config.strategy.trailing_stop_enabled is False  # invariant
    t = _seed_open_trade(s)
    original_stop = t.stop_price
    # Quote 5 ATR favorable — well past every threshold.
    s._update_trailing_stop(t, _quote("210.00"))
    assert t.stop_price == original_stop, "trailing must be a no-op when disabled"


def test_trailing_disabled_when_entry_atr_missing(cfg_and_paths, broker) -> None:
    """A recovered OpenTrade rebuilt from history has no ``entry_atr``.
    Trailing must remain a no-op for those trades — they keep the fixed
    stop they were recovered with. Safe default."""
    s = _strategy(cfg_and_paths, broker)
    _enable_trailing(s)
    t = _seed_open_trade(s, entry_atr=None)
    original_stop = t.stop_price
    s._update_trailing_stop(t, _quote("210.00"))
    assert t.stop_price == original_stop


def test_trailing_enabled_breakeven_move_then_ratchet(cfg_and_paths, broker) -> None:
    """Enabled + favorable progression. Walk a position through:
    (a) small favorable move — no change yet,
    (b) crosses breakeven threshold — stop moves to entry,
    (c) crosses activation — trail kicks in,
    (d) further favorable — ratchets up,
    (e) pullback — stop does NOT move down.
    """
    s = _strategy(cfg_and_paths, broker)
    # entry=200, atr=2 → BE at +1 (200→201 quote), trail-activate at +3 (203),
    # trail-distance 1 (so trailing stop sits $1 below high water).
    _enable_trailing(s)
    t = _seed_open_trade(s)

    # (a) Small favorable — under breakeven threshold.
    s._update_trailing_stop(t, _quote("200.50"))
    assert t.stop_price == Decimal("198.00"), "below breakeven threshold; no move"
    assert t.highest_seen_price == Decimal("200.50")

    # (b) Cross breakeven (favorable == 1.0 = 0.5 ATR).
    s._update_trailing_stop(t, _quote("201.00"))
    assert t.stop_price == Decimal("200.00"), "moved to entry (breakeven)"

    # (c) Cross trail activation (favorable == 3.5 > 1.5 ATR).
    s._update_trailing_stop(t, _quote("203.50"))
    # candidate = high (203.50) - distance (1.0) = 202.50
    assert t.stop_price == Decimal("202.50"), "trailed to high - 0.5*ATR"
    assert t.highest_seen_price == Decimal("203.50")

    # (d) Further favorable — high climbs, trail follows.
    s._update_trailing_stop(t, _quote("205.00"))
    assert t.highest_seen_price == Decimal("205.00")
    assert t.stop_price == Decimal("204.00"), "trail ratchets to 205 - 1"

    # (e) Pullback — high stays, stop stays (NEVER moves down).
    s._update_trailing_stop(t, _quote("204.20"))
    assert t.highest_seen_price == Decimal("205.00"), "high water unchanged"
    assert t.stop_price == Decimal("204.00"), "ratchet-only — must not move down"


def test_trailing_enabled_adverse_move_does_not_widen_stop(cfg_and_paths, broker) -> None:
    """Immediate adverse move: favorable <= 0 → early return. Stop
    unchanged; high-water mark not bumped past entry."""
    s = _strategy(cfg_and_paths, broker)
    _enable_trailing(s)
    t = _seed_open_trade(s)
    original_stop = t.stop_price
    s._update_trailing_stop(t, _quote("199.00"))
    assert t.stop_price == original_stop


# ---------------------------------------------------------------------------
# Restart recovery: broker has a position we don't have in state
# ---------------------------------------------------------------------------


def test_recovery_halts_when_no_history_for_extra_position(cfg_and_paths, broker) -> None:
    broker.positions = [
        Position(symbol="UNKNOWN", qty=5, avg_entry_price=Decimal("50"),
                 market_value=Decimal("250"), unrealized_pl=Decimal("0"),
                 side=OrderSide.BUY)
    ]
    s = _strategy(cfg_and_paths, broker)
    rec = s.recover(NOW)
    assert rec.halted is True
    assert rec.halt_reason in ("unrecoverable", "orphan_protective_orders")
    assert s.state.any_halt_active()


def test_recovery_does_not_block_when_everything_clean(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    rec = s.recover(NOW)
    assert rec.halted is False
    assert not s.state.any_halt_active()


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


def test_graceful_shutdown_flattens_when_asked(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    s.state.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL", qty=10,
        entry_price=Decimal("200.00"),
        entry_ts=NOW,
        stop_price=Decimal("198.00"),
        disaster_stop_price=Decimal("196.00"),
        target_price=Decimal("204.00"),
        intent_id="seed", parent_client_order_id=f"{COID_PREFIX}seed",
        protective_child_client_order_id=None,
        protective_child_broker_id=None,
        last_seen_broker_qty=10,
    )
    broker.positions = [
        Position(symbol="AAPL", qty=10, avg_entry_price=Decimal("200"),
                 market_value=Decimal("2000"), unrealized_pl=Decimal("0"),
                 side=OrderSide.BUY)
    ]
    s.graceful_shutdown(NOW, flatten=True)
    assert "AAPL" in broker.flatten_calls


def test_graceful_shutdown_without_flatten_preserves_positions(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    s.state.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL", qty=10,
        entry_price=Decimal("200.00"),
        entry_ts=NOW,
        stop_price=Decimal("198.00"),
        disaster_stop_price=Decimal("196.00"),
        target_price=Decimal("204.00"),
        intent_id="seed", parent_client_order_id=f"{COID_PREFIX}seed",
        protective_child_client_order_id=None,
        protective_child_broker_id=None,
        last_seen_broker_qty=10,
    )
    s.graceful_shutdown(NOW, flatten=False)
    assert broker.flatten_calls == []
    assert "AAPL" in s.state.open_trades


# ---------------------------------------------------------------------------
# Day rollover resets intraday fields
# ---------------------------------------------------------------------------


def test_daily_loss_halt_emits_incident(cfg_and_paths, broker) -> None:
    """The daily-loss halt must leave an auditable trail in the trade log."""
    from strategy.trade_log import RecordKind, TradeLog
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    s.state.realized_pnl_today = Decimal("-1000")
    s.tick(NOW, kill_switch_present=False)
    cfg, _, log_path = cfg_and_paths
    incidents = [
        r for r in TradeLog(log_path, fsync=False).read_all()
        if r.kind is RecordKind.INCIDENT
    ]
    assert any(r.payload.get("kind") == "daily_loss_halt" for r in incidents)


def test_drawdown_halt_emits_incident(cfg_and_paths, broker) -> None:
    from strategy.trade_log import RecordKind, TradeLog
    s = _strategy(cfg_and_paths, broker)
    s.state.peak_equity = Decimal("30000")
    s.state.last_reconciled_equity = Decimal("30000")
    s.state.intraday_low_equity = Decimal("30000")
    broker.account = AccountSnapshot(
        ts=NOW, equity=Decimal("27000"), last_equity=Decimal("30000"),
        buying_power=Decimal("50000"), cash=Decimal("12500"),
        pattern_day_trader=False,
    )
    s.recover(NOW)
    s.tick(NOW, kill_switch_present=False)
    _, _, log_path = cfg_and_paths
    incidents = [
        r for r in TradeLog(log_path, fsync=False).read_all()
        if r.kind is RecordKind.INCIDENT
    ]
    assert any(r.payload.get("kind") == "drawdown_halt" for r in incidents)


def test_orphan_protective_order_emits_incident(cfg_and_paths, broker) -> None:
    from strategy.trade_log import RecordKind, TradeLog
    broker.open_orders = [
        BrokerOrder(
            broker_order_id="orphan-1",
            client_order_id=f"{COID_PREFIX}orphan",
            symbol="AAPL", side=OrderSide.SELL, qty=10,
            filled_qty=0, avg_fill_price=None, status=OrderStatus.NEW,
            order_class=OrderClass.OTO, submitted_at=NOW, filled_at=None,
            parent_client_order_id=f"{COID_PREFIX}parent",
            leg_role="stop_child",
        )
    ]
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    _, _, log_path = cfg_and_paths
    incidents = [
        r for r in TradeLog(log_path, fsync=False).read_all()
        if r.kind is RecordKind.INCIDENT
    ]
    kinds = {r.payload.get("kind") for r in incidents}
    assert "orphan_protective_orders" in kinds


def test_flatten_error_is_logged_and_state_preserved(cfg_and_paths, broker) -> None:
    """If ``flatten_symbol`` raises, the local trade must stay in state so
    the next tick retries — never silently disappear."""
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    s.state.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL", qty=10,
        entry_price=Decimal("200"),
        entry_ts=NOW - timedelta(minutes=15),
        stop_price=Decimal("210"),   # triggers primary_stop_hit
        disaster_stop_price=Decimal("195"),
        target_price=Decimal("205"),
        intent_id="seed", parent_client_order_id=f"{COID_PREFIX}seed",
        protective_child_client_order_id=None,
        protective_child_broker_id=None,
        last_seen_broker_qty=10,
    )
    broker.positions = [
        Position(symbol="AAPL", qty=10, avg_entry_price=Decimal("200"),
                 market_value=Decimal("2000"), unrealized_pl=Decimal("0"),
                 side=OrderSide.BUY)
    ]
    # Simulate the broker failing to flatten.
    from strategy.errors import TransientBrokerError

    def boom(*_a, **_kw):
        raise TransientBrokerError("broker outage")
    broker.flatten_symbol = boom  # type: ignore[assignment]
    rep = s.tick(NOW, kill_switch_present=False)
    # Trade still present so next tick can retry.
    assert "AAPL" in s.state.open_trades


def test_recover_halts_when_broker_has_orphan_protective_order(cfg_and_paths, broker) -> None:
    """A resting protective stop with no matching local position must
    trigger a recovery halt — not be silently accepted."""
    broker.open_orders = [
        BrokerOrder(
            broker_order_id="orphan-1",
            client_order_id=f"{COID_PREFIX}orphan",
            symbol="AAPL", side=OrderSide.SELL, qty=10,
            filled_qty=0, avg_fill_price=None, status=OrderStatus.NEW,
            order_class=OrderClass.OTO, submitted_at=NOW, filled_at=None,
            parent_client_order_id=f"{COID_PREFIX}parent",
            leg_role="stop_child",
        )
    ]
    s = _strategy(cfg_and_paths, broker)
    rec = s.recover(NOW)
    assert rec.halted is True
    assert s.state.any_halt_active()


def test_recover_broker_unreachable_raises(cfg_and_paths, broker) -> None:
    from strategy.errors import TransientBrokerError
    s = _strategy(cfg_and_paths, broker)

    def boom():
        raise TransientBrokerError("no route to host")

    broker.get_account_snapshot = boom  # type: ignore[assignment]
    from strategy.errors import ReconcileMismatch
    with pytest.raises(ReconcileMismatch):
        s.recover(NOW)


def test_recover_rebuilds_from_trade_log(cfg_and_paths, broker) -> None:
    """If broker shows a position we don't have locally but the trade
    log *does* carry the matching INTENT + RESULT, recovery rebuilds
    the OpenTrade rather than halting."""
    cfg, state_path, log_path = cfg_and_paths
    # Seed the trade log with a matching INTENT + RESULT pair.
    tl = TradeLog(log_path, fsync=False)
    tl.append_intent(
        {
            "intent_id": "prev-001",
            "client_order_id": f"{COID_PREFIX}prev",
            "symbol": "AAPL",
            "side": OrderSide.BUY.value,
            "qty_requested": 10,
            "limit_price": "200.00",
            "stop_price": "198.00",
            "disaster_stop_price": "196.00",
            "target_price": "204.00",
            "reason": "prev-entry",
            "result": "submitting",
            "ts": NOW.isoformat(),
        }
    )
    tl.append_result(
        {
            "intent_id": "prev-001",
            "client_order_id": f"{COID_PREFIX}prev",
            "broker_order_id": "b-prev",
            "status": OrderStatus.FILLED.value,
            "filled_qty": 10,
            "avg_fill_price": "200.05",
            "symbol": "AAPL",
        }
    )
    # Broker says the position exists.
    broker.positions = [
        Position(symbol="AAPL", qty=10, avg_entry_price=Decimal("200.05"),
                 market_value=Decimal("2000.50"), unrealized_pl=Decimal("0"),
                 side=OrderSide.BUY)
    ]
    s = _strategy(cfg_and_paths, broker)
    rec = s.recover(NOW)
    assert rec.halted is False
    assert "AAPL" in s.state.open_trades
    assert s.state.open_trades["AAPL"].qty == 10


def test_manage_positions_target_hit_flattens(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    s.state.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL", qty=10,
        entry_price=Decimal("200"), entry_ts=NOW - timedelta(minutes=5),
        stop_price=Decimal("198"), disaster_stop_price=Decimal("196"),
        target_price=Decimal("199"),                     # mid 200 ≥ 199 → target
        intent_id="seed", parent_client_order_id=f"{COID_PREFIX}seed",
        protective_child_client_order_id=None,
        protective_child_broker_id=None, last_seen_broker_qty=10,
    )
    broker.positions = [
        Position(symbol="AAPL", qty=10, avg_entry_price=Decimal("200"),
                 market_value=Decimal("2000"), unrealized_pl=Decimal("0"),
                 side=OrderSide.BUY)
    ]
    rep = s.tick(NOW, kill_switch_present=False)
    assert "AAPL" in rep.exits_submitted


def test_manage_positions_time_stop_flattens(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    # max_hold_bars = 24 * 5min = 120 min. Enter 130 min ago.
    s.state.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL", qty=10,
        entry_price=Decimal("200"), entry_ts=NOW - timedelta(minutes=130),
        stop_price=Decimal("198"), disaster_stop_price=Decimal("196"),
        target_price=Decimal("999"),     # won't hit target at mid ≈ 200
        intent_id="seed", parent_client_order_id=f"{COID_PREFIX}seed",
        protective_child_client_order_id=None,
        protective_child_broker_id=None, last_seen_broker_qty=10,
    )
    broker.positions = [
        Position(symbol="AAPL", qty=10, avg_entry_price=Decimal("200"),
                 market_value=Decimal("2000"), unrealized_pl=Decimal("0"),
                 side=OrderSide.BUY)
    ]
    rep = s.tick(NOW, kill_switch_present=False)
    assert "AAPL" in rep.exits_submitted


def test_day_rollover_resets_intraday(cfg_and_paths, broker) -> None:
    s = _strategy(cfg_and_paths, broker)
    s.state.trading_day = NOW.date() - timedelta(days=1)
    s.state.realized_pnl_today = Decimal("-100")
    s.state.last_loss_ts_by_symbol["AAPL"] = NOW - timedelta(days=1)
    s.recover(NOW)
    assert s.state.trading_day == NOW.date()
    assert s.state.realized_pnl_today == Decimal(0)
    assert s.state.last_loss_ts_by_symbol == {}


# ===========================================================================
# Tier 3 — submitted-but-unconfirmed recovery (the 2026-05-15 lost-fill class)
#
# Principles: assert DURABLE lifecycle truth (audit-log RESULT records,
# RESULT immutability, intent/result pairing, child linkage, halt
# semantics). Where the fold must be isolated from downstream
# manage/flatten/re-entry churn we call _recover_pending_submissions
# directly. Strictness is preserved: exactly one terminal RESULT, no
# duplicate reconstruction, no replay duplication, recovery-before-reconcile.
# ===========================================================================


def _records(log_path):
    return list(TradeLog(log_path, fsync=False).read_all())


def _entry_results(recs, symbol):
    return [r for r in recs if r.kind is RecordKind.RESULT
            and r.payload.get("intent_id", "").startswith(f"entry-{symbol}")]


def _entry_intents(recs, symbol):
    return [r for r in recs if r.kind is RecordKind.INTENT
            and r.payload.get("intent_id", "").startswith(f"entry-{symbol}")]


def _one_symbol(broker, keep="AAPL"):
    """Restrict the fixture broker to ONE tradeable symbol so recovery
    assertions are unambiguous (fix: actually drop every non-kept symbol)."""
    for sym in ("AAPL", "MSFT"):
        if sym == keep:
            continue
        broker.bars.pop((sym, "5Min"), None)
        broker.bars.pop((sym, "15Min"), None)
        broker.bars.pop((sym, "1Hour"), None)
        broker.latest_quotes.pop(sym, None)


def _inject_lost_order(broker, log_path, *, symbol="AAPL", qty=5,
                       intent_id=None, result="submitting"):
    """Seed a submitted-but-unconfirmed order whose COID matches what
    FakeBroker.resolve_by_coid will look up (it matches submit_calls by
    OrderIntent.client_order_id()). Returns (intent_id, coid)."""
    intent_id = intent_id or f"entry-{symbol}-lost"
    intent = OrderIntent(
        intent_id=intent_id, symbol=symbol, side=OrderSide.BUY, qty=qty,
        limit_price=Decimal("200.00"), disaster_stop_price=Decimal("196.00"),
        tif=TimeInForce.DAY, order_class=OrderClass.OTO, reason="seed",
        ref_price=Decimal("200.00"), atr=Decimal("0.50"),
        spread_bps=Decimal("2"), ts=NOW)
    coid = intent.client_order_id()
    broker.submit_calls.append(intent)
    TradeLog(log_path, fsync=False).append_intent({
        "intent_id": intent_id, "client_order_id": coid, "symbol": symbol,
        "side": OrderSide.BUY.value, "qty_requested": qty,
        "limit_price": "200.00", "stop_price": "198.00",
        "disaster_stop_price": "196.00", "target_price": "204.00",
        "reason": "seed", "result": result,
    })
    return intent_id, coid


# ---- _try_enter: unknown outcome is persisted, never optimistically booked


def test_entry_outcome_unknown_logs_unconfirmed_and_no_result(cfg_and_paths, broker):
    _one_symbol(broker)
    broker._force_poll_error = OrderOutcomeUnknown("poll timed out")
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    s.tick(NOW, kill_switch_present=False)  # must not raise

    recs = _records(s.trade_log.path)
    intents = _entry_intents(recs, "AAPL")
    assert any(i.payload["result"] == "submitting" for i in intents)
    assert any(i.payload["result"] == "submitted_unconfirmed" for i in intents)
    assert _entry_results(recs, "AAPL") == []          # NO terminal RESULT
    assert "AAPL" not in s.state.open_trades            # NO optimistic book


# ---- recovery folds the lost submission into exactly one terminal RESULT


def test_recovery_resolves_unconfirmed_into_single_result(cfg_and_paths, broker):
    _one_symbol(broker)
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    broker._force_poll_error = OrderOutcomeUnknown("poll timed out")
    s.tick(NOW, kill_switch_present=False)              # tick 1 → unconfirmed

    recs = _records(s.trade_log.path)
    submitting = [i for i in _entry_intents(recs, "AAPL")
                  if i.payload["result"] == "submitting"][0]
    real_qty = submitting.payload["qty_requested"]
    iid = submitting.payload["intent_id"]
    assert _entry_results(recs, "AAPL") == []

    # Isolate the fold from manage/entry churn: call recovery directly.
    broker._force_poll_error = None
    s._recover_pending_submissions(NOW)

    recs = _records(s.trade_log.path)
    res = _entry_results(recs, "AAPL")
    assert len(res) == 1                                # exactly one, no dup
    assert res[0].payload["intent_id"] == iid           # intent/result pair
    assert res[0].payload["status"] == OrderStatus.FILLED.value
    assert res[0].payload["filled_qty"] == real_qty     # broker truth, not magic
    assert res[0].payload["protective_child_client_order_id"] is not None
    assert s.state.open_trades["AAPL"].qty == real_qty
    assert not s.state.any_halt_active()

    # RESULT-freeze: a second recovery pass must not duplicate anything.
    s._recover_pending_submissions(NOW)
    res2 = _entry_results(_records(s.trade_log.path), "AAPL")
    assert len(res2) == 1
    assert s.state.open_trades["AAPL"].qty == real_qty


def test_recovery_runs_before_reconcile(cfg_and_paths, broker):
    _one_symbol(broker)
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    broker._force_poll_error = OrderOutcomeUnknown("timeout")
    s.tick(NOW, kill_switch_present=False)              # produce unconfirmed
    broker._force_poll_error = None
    broker.call_log.clear()
    s.tick(NOW, kill_switch_present=False)              # recovering tick

    assert "resolve_by_coid" in broker.call_log
    assert "get_positions" in broker.call_log
    assert (broker.call_log.index("resolve_by_coid")
            < broker.call_log.index("get_positions"))


def test_recovery_no_op_when_result_already_exists(cfg_and_paths, broker):
    _one_symbol(broker)
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    s.tick(NOW, kill_switch_present=False)              # normal: RESULT written
    before = _entry_results(_records(s.trade_log.path), "AAPL")
    assert len(before) == 1
    ot_before = dict(s.state.open_trades)

    s._recover_pending_submissions(NOW)                 # nothing pending

    after = _entry_results(_records(s.trade_log.path), "AAPL")
    assert len(after) == 1                              # no duplicate RESULT
    assert s.state.open_trades.keys() == ot_before.keys()


def test_restart_recovery_folds_position_before_orphan_halt(cfg_and_paths, broker):
    """Restart mid-incident: log has submitting INTENT, no RESULT; broker
    shows the real position + its protective child. Recovery (run before
    reconcile inside recover()) folds the position so the existing
    orphan-protective halt does NOT trip."""
    _one_symbol(broker)
    cfg, state_path, log_path = cfg_and_paths
    iid, coid = _inject_lost_order(broker, log_path, symbol="AAPL", qty=5)
    broker.positions = [Position(symbol="AAPL", qty=5,
                                 avg_entry_price=Decimal("200.00"),
                                 market_value=Decimal("1000.00"),
                                 unrealized_pl=Decimal("0"), side=OrderSide.BUY)]
    broker.open_orders = [BrokerOrder(
        broker_order_id="b-stop-AAPL", client_order_id=f"{COID_PREFIX}child-AAPL",
        symbol="AAPL", side=OrderSide.SELL, qty=5, filled_qty=0,
        avg_fill_price=None, status=OrderStatus.HELD, order_class=OrderClass.SIMPLE,
        submitted_at=NOW, filled_at=None,
        parent_client_order_id=coid, leg_role="stop_child")]

    s = _strategy(cfg_and_paths, broker)
    rec = s.recover(NOW)

    assert rec.halted is False                          # orphan-halt avoided
    assert s.state.open_trades["AAPL"].qty == 5
    res = _entry_results(_records(s.trade_log.path), "AAPL")
    assert len(res) == 1                                # terminal RESULT written
    assert res[0].payload["protective_child_client_order_id"] is not None


def test_recovery_escalates_to_halt_and_blocks_entries(cfg_and_paths, broker):
    """Recover-then-halt: order exists but never reaches terminal and the
    attempt budget is exhausted → sticky halt, incident emitted, protective
    order NEVER cancelled, no new entries thereafter."""
    _one_symbol(broker)
    cfg, state_path, log_path = cfg_and_paths
    iid, coid = _inject_lost_order(broker, log_path, symbol="AAPL", qty=5,
                                   intent_id="entry-AAPL-stuck")
    tl = TradeLog(log_path, fsync=False)
    tl.append_incident({"kind": "recovery_attempt", "intent_id": iid,
                        "reason": "still_working"})
    tl.append_incident({"kind": "recovery_attempt", "intent_id": iid,
                        "reason": "still_working"})
    broker._force_poll_error = OrderOutcomeUnknown("still working")

    s = _strategy(cfg_and_paths, broker)
    halted = s._recover_pending_submissions(NOW)

    assert halted is True
    assert s.state.any_halt_active()
    recs = _records(s.trade_log.path)
    assert any(r.kind is RecordKind.INCIDENT
               and r.payload.get("kind") == "recovery_attempt"
               and "escalated" in r.payload.get("reason", "")
               for r in recs)
    assert _entry_results(recs, "AAPL") == []           # nothing booked
    assert broker.flatten_calls == []                   # protective NOT cancelled

    broker._force_poll_error = None
    rep = s.tick(NOW, kill_switch_present=False)
    assert rep.halted is True
    assert not rep.entries_submitted                    # sticky halt blocks entries


def test_flatten_outcome_unknown_caught_position_retained_then_retried(cfg_and_paths, broker):
    """Finding B (strategy-level): a close whose poll raises
    OrderOutcomeUnknown is caught by _flatten_one's StrategyError handler —
    an error RESULT is logged, the position stays tracked (lifecycle
    ownership retained, no tick abort), and the exit is retried next tick."""
    _one_symbol(broker)
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    s.state.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL", qty=10, entry_price=Decimal("200"),
        entry_ts=NOW - timedelta(minutes=5), stop_price=Decimal("198"),
        disaster_stop_price=Decimal("196"), target_price=Decimal("199"),
        intent_id="seed", parent_client_order_id=f"{COID_PREFIX}seed",
        protective_child_client_order_id=None, protective_child_broker_id=None,
        last_seen_broker_qty=10)
    broker.positions = [Position(symbol="AAPL", qty=10,
                                 avg_entry_price=Decimal("200"),
                                 market_value=Decimal("2000"),
                                 unrealized_pl=Decimal("0"), side=OrderSide.BUY)]
    broker._force_flatten_error = OrderOutcomeUnknown("close poll timed out")

    s.tick(NOW, kill_switch_present=False)               # must not raise

    recs = _records(s.trade_log.path)
    err = [r for r in recs if r.kind is RecordKind.RESULT
           and r.payload.get("status") == "error"
           and "close-AAPL" in r.payload.get("intent_id", "")]
    assert err, "expected an error RESULT for the failed close"
    assert "AAPL" in s.state.open_trades                 # ownership retained
    flatten_count_after_fail = len(broker.flatten_calls)
    assert flatten_count_after_fail >= 1

    broker._force_flatten_error = None
    s.tick(NOW, kill_switch_present=False)               # exit retried
    assert len(broker.flatten_calls) > flatten_count_after_fail  # retried


def test_replay_2026_05_15_lost_msft_recovered(cfg_and_paths, broker):
    """Permanent institutional regression artifact — the 2026-05-15 MSFT
    lost-fill incident, kept realistic:

      * an entry submission whose terminal outcome is lost (poll timeout)
      * the protective child is orphaned at the broker
      * NO RESULT is recorded by the engine
      * reconcile pressure (broker shows the real position + orphan child)
      * the parent later fills (as Alpaca's did at 14:57:12)

    Post-fix expectation: recovery folds the real MSFT position with its
    protective child into exactly one terminal RESULT; reconcile is clean
    (no 49-minute orphan spin); no recovery_unresolved halt; no lost
    exposure. (The exact -$19.89 P&L booking is the canonical Tier 4
    end-to-end replay; this asserts the recovery half durably.)"""
    _one_symbol(broker, keep="MSFT")
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)

    # 1) The lost submission: poll times out, nothing booked.
    broker._force_poll_error = OrderOutcomeUnknown(
        "order did not reach terminal status within 15s")
    s.tick(NOW, kill_switch_present=False)
    recs = _records(s.trade_log.path)
    assert _entry_results(recs, "MSFT") == []            # nothing booked yet
    assert "MSFT" not in s.state.open_trades
    submitting = [i for i in _entry_intents(recs, "MSFT")
                  if i.payload["result"] == "submitting"][0]
    real_qty = submitting.payload["qty_requested"]
    iid = submitting.payload["intent_id"]

    # 2) Broker truth: parent filled, position live, orphan protective child
    #    present (exactly the reconcile pressure seen on 2026-05-15).
    broker._force_poll_error = None
    broker.positions = [Position(symbol="MSFT", qty=real_qty,
                                 avg_entry_price=Decimal("200.02"),
                                 market_value=Decimal("2000.20"),
                                 unrealized_pl=Decimal("0"), side=OrderSide.BUY)]
    broker.open_orders = [BrokerOrder(
        broker_order_id="b-stop-MSFT", client_order_id=f"{COID_PREFIX}child-MSFT",
        symbol="MSFT", side=OrderSide.SELL, qty=real_qty, filled_qty=0,
        avg_fill_price=None, status=OrderStatus.HELD, order_class=OrderClass.SIMPLE,
        submitted_at=NOW, filled_at=None, parent_client_order_id=None,
        leg_role="stop_child")]

    # 3) Recovery folds it (isolated assertion of durable lifecycle truth).
    s._recover_pending_submissions(NOW)
    recs = _records(s.trade_log.path)
    res = _entry_results(recs, "MSFT")
    assert len(res) == 1                                  # one terminal RESULT
    assert res[0].payload["intent_id"] == iid
    assert res[0].payload["status"] == OrderStatus.FILLED.value
    assert res[0].payload["filled_qty"] == real_qty
    assert res[0].payload["protective_child_client_order_id"] is not None
    assert s.state.open_trades["MSFT"].qty == real_qty
    assert not s.state.any_halt_active()                  # no orphan-halt

    # 4) A subsequent reconcile is clean — the child is now recognized as
    #    this position's protective leg, not an orphan (no 49-min spin).
    rep = s.tick(NOW, kill_switch_present=False)
    assert not s.state.has_halt("recovery_unresolved")
    assert not any(
        r.kind is RecordKind.INCIDENT
        and r.payload.get("kind") == "reconcile_mismatch"
        and r.payload.get("orphan_protective_orders")
        for r in _records(s.trade_log.path)
    )


# ===========================================================================
# Tier 4 — reconcile/state no-regression guarantees
# ===========================================================================


def test_no_regression_clean_session_emits_zero_recovery_artifacts(cfg_and_paths, broker):
    """Recovery wiring must be completely inert on a normal session: no
    submitted_unconfirmed, no recovery_attempt, no recovery_unresolved
    halt, exactly one entry RESULT, no false-positive halt."""
    _one_symbol(broker)
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    s.tick(NOW, kill_switch_present=False)

    recs = _records(s.trade_log.path)
    assert len(_entry_results(recs, "AAPL")) == 1
    assert not any(i.payload.get("result") == "submitted_unconfirmed"
                   for i in recs if i.kind is RecordKind.INTENT)
    assert not any(r.kind is RecordKind.INCIDENT
                   and r.payload.get("kind") in ("recovery_attempt",)
                   for r in recs)
    assert not s.state.has_halt("recovery_unresolved")
    assert not s.state.any_halt_active()


def test_recovery_does_not_mask_a_genuine_orphan(cfg_and_paths, broker):
    """False-negative guard: an orphan protective order with NO matching
    pending intent must still orphan-halt recover(). Recovery is a no-op
    here (nothing to resolve) and must not swallow the real orphan."""
    _one_symbol(broker)
    broker.open_orders = [BrokerOrder(
        broker_order_id="b-orphan", client_order_id=f"{COID_PREFIX}orphan",
        symbol="AAPL", side=OrderSide.SELL, qty=5, filled_qty=0,
        avg_fill_price=None, status=OrderStatus.HELD,
        order_class=OrderClass.SIMPLE, submitted_at=NOW, filled_at=None,
        parent_client_order_id=f"{COID_PREFIX}gone", leg_role="stop_child")]
    s = _strategy(cfg_and_paths, broker)
    rec = s.recover(NOW)

    assert rec.halted is True
    assert rec.halt_reason == "orphan_protective_orders"
    # Recovery produced nothing — it did not mask the orphan.
    recs = _records(s.trade_log.path)
    assert _entry_results(recs, "AAPL") == []
    assert not any(i.payload.get("result") == "submitted_unconfirmed"
                   for i in recs if i.kind is RecordKind.INTENT)


def test_recovery_state_does_not_leak_across_sessions(cfg_and_paths, broker):
    """Cross-session leak guard: a submitted_unconfirmed from a PRIOR
    session (ts < today's session_start) must NOT be recovered, even if
    the broker could still resolve its COID. Written raw so the top-level
    ts is genuinely stale (TradeLog._append would stamp 'now')."""
    from strategy.trade_log import GENESIS_HASH, RecordKind, _hash_record

    _one_symbol(broker)
    cfg, state_path, log_path = cfg_and_paths
    stale_dt = NOW - timedelta(days=3)            # well before today's session
    intent = OrderIntent(
        intent_id="entry-AAPL-staleday", symbol="AAPL", side=OrderSide.BUY,
        qty=5, limit_price=Decimal("200.00"),
        disaster_stop_price=Decimal("196.00"), tif=TimeInForce.DAY,
        order_class=OrderClass.OTO, reason="seed", ref_price=Decimal("200"),
        atr=Decimal("0.5"), spread_bps=Decimal("2"), ts=NOW)
    coid = intent.client_order_id()
    broker.submit_calls.append(intent)            # broker COULD resolve it

    # Write a VALID hash-chained record whose top-level ts is genuinely
    # stale, using the real hashing primitive (no monkeypatching).
    payload = {
        "intent_id": "entry-AAPL-staleday", "client_order_id": coid,
        "symbol": "AAPL", "side": "buy", "qty_requested": 5,
        "limit_price": "200.00", "stop_price": "198.00",
        "disaster_stop_price": "196.00", "target_price": "204.00",
        "reason": "seed", "result": "submitted_unconfirmed",
    }
    line_hash = _hash_record(RecordKind.INTENT, stale_dt, payload, GENESIS_HASH)
    Path(log_path).write_text(json.dumps({
        "kind": "INTENT", "ts": stale_dt.isoformat(), "payload": payload,
        "prev_hash": GENESIS_HASH, "line_hash": line_hash,
    }) + "\n", encoding="utf-8")

    s = _strategy(cfg_and_paths, broker)
    # Sanity: chain intact and the seeded record's ts really is stale.
    seeded = _records(s.trade_log.path)
    assert seeded and seeded[0].ts == stale_dt
    s._recover_pending_submissions(NOW)

    recs = _records(s.trade_log.path)
    assert _entry_results(recs, "AAPL") == []      # NOT resurrected
    assert "AAPL" not in s.state.open_trades
    assert not s.state.any_halt_active()


# ===========================================================================
# ██  CANONICAL REGRESSION ARTIFACT — 2026-05-15 MSFT LOST-FILL INCIDENT  ██
#
# PERMANENT institutional replay. Do not simplify. It models the real
# lifecycle exactly:
#   14:54:43  OTO bracket submitted; poll times out (OrderOutcomeUnknown)
#   ──────── engine records NO RESULT (the lost-fill bug)
#   14:57:12  broker actually fills parent: 5 MSFT @ 424.85; child orphaned
#   ──────── reconcile pressure: live position + orphan protective child
#   (post-fix) recovery folds the position by COID → one terminal RESULT
#   15:44:39  broker-side protective stop fills: 5 @ 420.872  → flat
#   ──────── realized loss = 5 × (424.85 − 420.872) = −$19.89
#
# Parity is asserted the way the engine actually achieves it: broker-truth
# equity reconciliation (record_reconciled_equity). The engine does NOT
# emit a realized_pnl RESULT for a broker-side OTO fill — that is existing
# design and an explicit non-goal. The −$19.89 is asserted as the exact
# arithmetic identity AND the reconciled-equity delta (the precise
# divergence the fix prevents).
# ===========================================================================


def test_canonical_replay_2026_05_15_msft_full_lifecycle(cfg_and_paths, broker):
    _one_symbol(broker, keep="MSFT")
    cfg, state_path, log_path = cfg_and_paths
    START_EQUITY = Decimal("101266.23")
    broker.account = AccountSnapshot(
        ts=NOW, equity=START_EQUITY, last_equity=START_EQUITY,
        buying_power=Decimal("200000"), cash=Decimal("50000"),
        pattern_day_trader=False)

    # ---- Phase 1: 14:54 — the RECORDED lost submission --------------------
    # Replay from the engine's actual post-poll-timeout audit state with the
    # EXACT incident parameters (qty 5, limit 424.85, primary stop 422.39,
    # disaster/OTO stop 420.98). FakeBroker.resolve_by_coid fills at the
    # intent's limit price and qty, so the recovered entry == 5 @ 424.85.
    QTY = 5
    ENTRY_PX = Decimal("424.85")
    intent = OrderIntent(
        intent_id="entry-MSFT-2026-05-15T14:54:40.885614+00:00",
        symbol="MSFT", side=OrderSide.BUY, qty=QTY,
        limit_price=ENTRY_PX, disaster_stop_price=Decimal("420.98"),
        tif=TimeInForce.DAY, order_class=OrderClass.OTO,
        reason="breakout_with_trend_and_confirmation",
        ref_price=ENTRY_PX, atr=Decimal("1.20"),
        spread_bps=Decimal("3.99"), ts=NOW)
    coid = intent.client_order_id()
    broker.submit_calls.append(intent)
    iid = intent.intent_id
    seed_payload = {
        "intent_id": iid, "client_order_id": coid, "symbol": "MSFT",
        "side": "buy", "qty_requested": QTY, "limit_price": "424.85",
        "stop_price": "422.39", "disaster_stop_price": "420.98",
        "target_price": "427.30", "reason": "breakout_with_trend_and_confirmation",
    }
    tl = TradeLog(log_path, fsync=False)
    tl.append_intent({**seed_payload, "result": "submitting"})
    tl.append_intent({**seed_payload, "result": "submitted_unconfirmed",
                      "unconfirmed_reason": "poll timeout 15s"})

    s = _strategy(cfg_and_paths, broker)
    s.state.peak_equity = START_EQUITY
    s.state.last_reconciled_equity = START_EQUITY
    s.state.intraday_low_equity = START_EQUITY

    recs = _records(s.trade_log.path)
    assert _entry_results(recs, "MSFT") == []          # nothing booked (lost)
    assert "MSFT" not in s.state.open_trades

    # ---- Phase 2: 14:57 — broker truly filled 5 @ 424.85; child orphaned --
    broker._force_poll_filled_qty = QTY
    broker.positions = [Position(symbol="MSFT", qty=QTY,
                                 avg_entry_price=ENTRY_PX,
                                 market_value=ENTRY_PX * QTY,
                                 unrealized_pl=Decimal("0"), side=OrderSide.BUY)]
    broker.open_orders = [BrokerOrder(
        broker_order_id="b-stop-MSFT", client_order_id=f"{COID_PREFIX}child-MSFT",
        symbol="MSFT", side=OrderSide.SELL, qty=QTY, filled_qty=0,
        avg_fill_price=None, status=OrderStatus.HELD,
        order_class=OrderClass.SIMPLE, submitted_at=NOW, filled_at=None,
        parent_client_order_id=None, leg_role="stop_child")]

    # Recovery folds it (isolated — durable lifecycle truth).
    s._recover_pending_submissions(NOW)
    recs = _records(s.trade_log.path)
    res = _entry_results(recs, "MSFT")
    assert len(res) == 1
    assert res[0].payload["intent_id"] == iid
    assert res[0].payload["status"] == OrderStatus.FILLED.value
    assert res[0].payload["filled_qty"] == QTY
    assert res[0].payload["protective_child_client_order_id"] is not None
    assert s.state.open_trades["MSFT"].qty == QTY
    assert s.state.open_trades["MSFT"].entry_price == ENTRY_PX
    assert not s.state.any_halt_active()
    qty = QTY

    # The real incident: the engine never quote-managed this position; the
    # broker-side OTO stop is what closes it (Phase 4). Pin a holding quote
    # (between primary stop 422.39 and target 427.30) and remove MSFT bars
    # so the engine neither quote-exits nor re-enters during reconcile —
    # keeping the replay faithful (one position, one lifecycle).
    broker.latest_quotes["MSFT"] = Quote(
        symbol="MSFT", ts=NOW, bid_price=Decimal("424.80"),
        ask_price=Decimal("424.90"), bid_size=100, ask_size=100)
    for tf in ("5Min", "15Min", "1Hour"):
        broker.bars.pop(("MSFT", tf), None)

    # ---- Phase 3: reconcile clean — child recognised, no orphan spin ----
    rep = s.tick(NOW, kill_switch_present=False)
    assert not s.state.has_halt("recovery_unresolved")
    assert "MSFT" in s.state.open_trades                # still held, no churn
    assert len(_entry_results(_records(s.trade_log.path), "MSFT")) == 1
    assert not any(
        r.kind is RecordKind.INCIDENT
        and r.payload.get("kind") == "reconcile_mismatch"
        and r.payload.get("orphan_protective_orders")
        for r in _records(s.trade_log.path))

    # ---- Phase 4: 15:44 — broker protective stop fills 5 @ 420.872 ------
    EXIT_PX = Decimal("420.872")
    realized = (EXIT_PX - ENTRY_PX) * qty            # exact broker truth
    END_EQUITY = START_EQUITY + realized
    broker.positions = []                            # broker flat
    broker.open_orders = []                           # child consumed
    broker.account = AccountSnapshot(
        ts=NOW, equity=END_EQUITY, last_equity=START_EQUITY,
        buying_power=Decimal("200000"), cash=Decimal("50000"),
        pattern_day_trader=False)
    s.tick(NOW, kill_switch_present=False)

    # ---- Phase 5: EXACT accounting parity -------------------------------
    # Institutional identity — the known numbers, locked forever.
    assert qty == 5
    # Realized loss = (exit − entry) × qty  (broker truth, exact).
    assert (Decimal("420.872") - Decimal("424.85")) * 5 == Decimal("-19.890")
    assert realized == Decimal("-19.890")
    assert END_EQUITY == START_EQUITY - Decimal("19.890")
    # Engine books == broker books (the precise divergence the fix prevents).
    assert s.state.last_reconciled_equity == broker.account.equity
    assert s.state.last_reconciled_equity == START_EQUITY - Decimal("19.890")
    # No orphan open position; no unresolved submission; one RESULT only.
    assert "MSFT" not in s.state.open_trades
    final = _records(s.trade_log.path)
    assert len(_entry_results(final, "MSFT")) == 1
    unresolved = [
        i for i in _entry_intents(final, "MSFT")
        if i.payload.get("result") in ("submitting", "submitted_unconfirmed")
        and not any(r.kind is RecordKind.RESULT
                    and r.payload.get("intent_id") == i.payload["intent_id"]
                    for r in final)]
    assert unresolved == []
    assert not s.state.has_halt("recovery_unresolved")

    # ---- Phase 6: idempotency — replay/second pass mutates nothing ------
    eq_before = s.state.last_reconciled_equity
    ot_before = dict(s.state.open_trades)
    n_results_before = len(_entry_results(_records(s.trade_log.path), "MSFT"))
    s._recover_pending_submissions(NOW)
    after = _records(s.trade_log.path)
    assert len(_entry_results(after, "MSFT")) == n_results_before  # no dup
    assert s.state.open_trades.keys() == ot_before.keys()
    assert s.state.last_reconciled_equity == eq_before
