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
from strategy.state import StateStore, StrategyState
from strategy.strategy import Strategy, TickReport
from strategy.trade_log import TradeLog


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

    # ---- trading-client-ish methods (used by orchestrator) ----
    def get_account_snapshot(self):
        return self.account

    def get_positions(self):
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

    def flatten_symbol(self, symbol, close_client_order_id):
        self.flatten_calls.append(symbol)
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
    """Stale data gate fires against the latest bar timestamp.

    We replace the 5m series with one whose final bar is 10 minutes before
    NOW — well past the 30s stale threshold in the fixture.
    """
    s = _strategy(cfg_and_paths, broker)
    s.recover(NOW)
    for sym in ("AAPL", "MSFT"):
        bars = _synth_trend(
            sym, 120,
            start_price=Decimal("180"), step=Decimal("0.10"),
            start_ts=NOW - timedelta(minutes=5 * 120 + 10),
            tf_minutes=5,
            force_breakout=True,
        )
        broker.bars[(sym, "5Min")] = bars
    rep = s.tick(NOW, kill_switch_present=False)
    assert not rep.entries_submitted
    assert rep.denies
    for _, reason in rep.denies:
        assert reason == "stale_data", reason


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
