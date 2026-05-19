"""Offline tests for research.engine.sim_broker (Phase 1 sub-task 1.1).

In-memory only — bars are hand-crafted pandas frames passed directly to
the broker (no cache, no resample, no Alpaca). Production wiring will
add a separate ``load_bars_from_cache`` helper in a later sub-task.

Loud Phase-1 limitations are pinned by tests so the contract can't drift
silently (spread_bps == 0, perfect fills at limit price).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

pd = pytest.importorskip("pandas")

from research.engine import sim_broker as sb
from strategy.broker import BarTimeframe
from strategy.dto import (
    OrderClass, OrderIntent, OrderSide, OrderStatus, TimeInForce,
)

UTC = timezone.utc


# --- builders --------------------------------------------------------------

def _frame_1m(symbol: str, start: datetime, n: int, base: float = 100.0):
    idx = pd.DatetimeIndex(
        [start + timedelta(minutes=i) for i in range(n)],
        tz="UTC", name="ts",
    )
    return pd.DataFrame({
        "open":   [base + i * 0.01 for i in range(n)],
        "high":   [base + 0.5 + i * 0.01 for i in range(n)],
        "low":    [base - 0.5 + i * 0.01 for i in range(n)],
        "close":  [base + 0.2 + i * 0.01 for i in range(n)],
        "volume": [1000 + i for i in range(n)],
    }, index=idx)


def _intent(symbol="SPY", qty=10, limit=Decimal("100.20"),
            stop=Decimal("99.00"), ts=None):
    return OrderIntent(
        intent_id=f"entry-{symbol}-test",
        symbol=symbol, side=OrderSide.BUY, qty=qty,
        limit_price=limit, disaster_stop_price=stop,
        tif=TimeInForce.DAY, order_class=OrderClass.OTO,
        reason="test", ref_price=limit, atr=Decimal("0.5"),
        spread_bps=Decimal(0), ts=ts or datetime(2025, 1, 2, 14, 30, tzinfo=UTC),
    )


def _broker(bars=None, cash="100000", now=None):
    return sb.SimulatedBroker(
        bars=bars or {("SPY", 1): _frame_1m("SPY", datetime(2025,1,2,14,30,tzinfo=UTC), 60)},
        starting_cash=Decimal(cash),
        now=now or datetime(2025, 1, 2, 15, 0, tzinfo=UTC),
    )


# --- clock + anti-leak -----------------------------------------------------

def test_set_now_requires_utc_and_monotonic() -> None:
    b = _broker()
    with pytest.raises(ValueError, match="UTC"):
        b.set_now(datetime(2025, 1, 2, 16, 0))           # naive
    b.set_now(datetime(2025, 1, 2, 15, 30, tzinfo=UTC))
    with pytest.raises(ValueError, match="monotonic"):
        b.set_now(datetime(2025, 1, 2, 15, 0, tzinfo=UTC))


def test_get_bars_clips_to_now_close_at_or_before(now_ts=None) -> None:
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    bars = {("SPY", 1): _frame_1m("SPY", start, 60)}     # 14:30..15:29
    b = sb.SimulatedBroker(bars=bars, starting_cash=Decimal("1e6"),
                            now=start + timedelta(minutes=10))
    out = b.get_bars("SPY", BarTimeframe.M5,
                     start=start, end=start + timedelta(hours=2))
    # tf=5: a 5m bar with ts=T has close_ts=T+5; need close<=now=14:40 →
    # last allowed ts is 14:35. But this 1m frame is being treated as if
    # it were the 5m timeframe (no resample in this fixture); intentional
    # to test the clip math: ts <= now - 5min.
    cutoff = start + timedelta(minutes=10) - timedelta(minutes=5)
    assert all(bar.ts <= cutoff for bar in out)


def test_get_bars_unknown_symbol_returns_empty() -> None:
    b = _broker()
    out = b.get_bars("XXX", BarTimeframe.M5,
                     start=datetime(2025,1,2,14,30,tzinfo=UTC),
                     end=datetime(2025,1,2,16,0,tzinfo=UTC))
    assert out == []


def test_get_bars_limit_returns_last_n() -> None:
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    b = sb.SimulatedBroker(
        bars={("SPY", 5): _frame_1m("SPY", start, 30)},
        starting_cash=Decimal("1e6"),
        now=start + timedelta(hours=3),
    )
    out = b.get_bars("SPY", BarTimeframe.M5,
                     start=start, end=start + timedelta(hours=2), limit=5)
    assert len(out) == 5


# --- quote synthesis (Phase 1 limitation) ----------------------------------

def test_quote_synthesis_phase1_spread_is_zero() -> None:
    """LOUD contract: Phase 1 quote synthesis sets spread_bps == 0.
    If this ever drifts the production spread_too_wide gate behavior
    silently changes — must be a deliberate, reviewed Phase 2 change."""
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    b = sb.SimulatedBroker(
        bars={("SPY", 1): _frame_1m("SPY", start, 60)},
        starting_cash=Decimal("1e6"),
        now=start + timedelta(minutes=30),
    )
    q = b.get_latest_quote("SPY")
    assert q.bid_price == q.ask_price
    assert q.spread_bps() == Decimal(0)
    # mid == most-recent-completed 1m close
    assert q.mid() > 0
    assert sb.PHASE1_SPREAD_BPS == Decimal(0)


def test_get_latest_quote_raises_when_no_bars_yet() -> None:
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    b = sb.SimulatedBroker(
        bars={("SPY", 1): _frame_1m("SPY", start, 5)},
        starting_cash=Decimal("1e6"),
        now=start - timedelta(minutes=1),                 # before any bar
    )
    with pytest.raises(KeyError):
        b.get_latest_quote("SPY")


# --- account / positions ---------------------------------------------------

def test_initial_account_equity_equals_cash_no_positions() -> None:
    b = _broker(cash="50000")
    snap = b.get_account_snapshot()
    assert snap.cash == Decimal("50000")
    assert snap.equity == Decimal("50000")
    assert b.get_positions() == []


def test_submit_entry_perfect_fill_decrements_cash_and_opens_position() -> None:
    b = _broker(cash="100000")
    sub = b.submit_entry_with_protection(_intent(qty=10, limit=Decimal("100.20")))
    # Perfect fill at limit price.
    assert sub.parent.status is OrderStatus.FILLED
    assert sub.parent.filled_qty == 10
    assert sub.parent.avg_fill_price == Decimal("100.20")
    assert sub.stop_child is not None
    assert sub.stop_child.status is OrderStatus.HELD
    # Cash debited, position recorded.
    snap = b.get_account_snapshot()
    assert snap.cash == Decimal("100000") - Decimal("100.20") * 10
    pos = b.get_positions()
    assert len(pos) == 1
    assert pos[0].symbol == "SPY"
    assert pos[0].qty == 10
    assert pos[0].avg_entry_price == Decimal("100.20")


def test_submit_entry_rejects_when_insufficient_cash() -> None:
    b = _broker(cash="100")                                # not enough for 10 @ 100.20
    sub = b.submit_entry_with_protection(_intent(qty=10, limit=Decimal("100.20")))
    assert sub.parent.status is OrderStatus.REJECTED
    assert sub.stop_child is None
    assert b.get_positions() == []


def test_submit_entry_idempotent_by_coid() -> None:
    b = _broker(cash="100000")
    intent = _intent()
    s1 = b.submit_entry_with_protection(intent)
    s2 = b.submit_entry_with_protection(intent)
    assert s1 is s2
    assert len(b.get_positions()) == 1                     # not doubled


def test_poll_terminal_returns_already_filled_parent() -> None:
    b = _broker(cash="100000")
    sub = b.submit_entry_with_protection(_intent())
    term = b.poll_terminal(sub.parent.client_order_id)
    assert term is sub.parent
    assert term.status is OrderStatus.FILLED


def test_poll_terminal_unknown_coid_raises() -> None:
    b = _broker()
    with pytest.raises(KeyError):
        b.poll_terminal("nope")


def test_resolve_by_coid_hits_and_misses() -> None:
    b = _broker(cash="100000")
    sub = b.submit_entry_with_protection(_intent())
    assert b.resolve_by_coid(sub.parent.client_order_id) is sub
    assert b.resolve_by_coid("never-submitted") is None


def test_open_orders_show_only_active_stop_child() -> None:
    b = _broker(cash="100000")
    sub = b.submit_entry_with_protection(_intent())
    opens = b.get_open_orders()
    assert len(opens) == 1
    assert opens[0].leg_role == "stop_child"
    assert opens[0].status is OrderStatus.HELD
    assert b.get_open_orders("XXX") == []


def test_flatten_cancels_protective_child_and_credits_cash() -> None:
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    bars = {("SPY", 1): _frame_1m("SPY", start, 60, base=100.0)}
    b = sb.SimulatedBroker(bars=bars, starting_cash=Decimal("100000"),
                            now=start + timedelta(minutes=15))
    b.submit_entry_with_protection(_intent(qty=10, limit=Decimal("100.20")))
    # Advance the clock to a later bar so exit mid > entry; expect a gain.
    b.set_now(start + timedelta(minutes=45))
    res = b.flatten_symbol("SPY", "close-coid-1")
    assert res.final_position_qty == 0
    assert len(res.cancelled_order_ids) == 1               # the stop_child
    assert res.close_order.status is OrderStatus.FILLED
    assert b.get_positions() == []
    assert b.get_open_orders() == []
    # Cash > initial because the close mid was higher than entry.
    assert b.get_account_snapshot().cash > Decimal("100000")


def test_flatten_with_no_position_is_a_safe_noop() -> None:
    b = _broker()
    res = b.flatten_symbol("SPY", "close-coid-noop")
    assert res.final_position_qty == 0
    assert res.close_order.qty == 0


# --- equity MTM ------------------------------------------------------------

def test_account_equity_marks_position_to_market() -> None:
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    bars = {("SPY", 1): _frame_1m("SPY", start, 60, base=100.0)}
    b = sb.SimulatedBroker(bars=bars, starting_cash=Decimal("100000"),
                            now=start + timedelta(minutes=10))
    b.submit_entry_with_protection(_intent(qty=10, limit=Decimal("100.20")))
    # Advance clock; mid drifts upward (frame is monotonically increasing).
    b.set_now(start + timedelta(minutes=50))
    snap = b.get_account_snapshot()
    # equity = cash + qty * mid; mid > entry → equity > initial cash
    assert snap.equity > Decimal("100000")
    pos = b.get_positions()[0]
    assert pos.unrealized_pl > 0


# --- determinism -----------------------------------------------------------

def test_two_brokers_with_same_setup_produce_identical_sequences() -> None:
    """Determinism (seed for the harder gate in 1.2): two completely
    independent broker instances with identical setup produce identical
    observable output (broker_order_ids + equity). The per-instance OID
    counter starts at 0 for each broker, so identity holds by construction."""
    def run():
        start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
        bars = {("SPY", 1): _frame_1m("SPY", start, 60)}
        b = sb.SimulatedBroker(bars=bars, starting_cash=Decimal("100000"),
                                now=start + timedelta(minutes=10))
        s = b.submit_entry_with_protection(_intent())
        b.set_now(start + timedelta(minutes=50))
        r = b.flatten_symbol("SPY", "close-coid-det")
        return (s.parent.broker_order_id, s.stop_child.broker_order_id,
                r.close_order.broker_order_id,
                b.get_account_snapshot().equity)
    a = run()
    c = run()
    assert a == c
