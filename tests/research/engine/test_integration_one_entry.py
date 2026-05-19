"""Phase 1 sub-task 1.3 — end-to-end integration scenario.

First test in the suite where the driver actually drives the production
:class:`strategy.Strategy` through a *real* entry: synthetic bars are
engineered to satisfy every entry precondition (>=100 5Min bars, >=60
15Min bars, >=210 1Hour bars; EMAs aligned; ADX high; ATR/close in
band; close strictly above the prior breakout-lookback high) so the
strategy fires a long entry on the first allowed tick.

What this PROVES (and what it doesn't)
--------------------------------------
PROVES: the seam holds — production Strategy + SimulatedBroker +
SimulatedTradeLog + BacktestDriver produce a coherent INTENT→RESULT
audit pair for a real entry, position lands in state.open_trades,
cash is debited by the perfect-fill notional, hash chain stays intact.

DOES NOT PROVE: signal QUALITY (synthetic bars are designed to fire
unconditionally; they say nothing about whether the strategy has
edge on real data). PHASE-1 LIMITATIONS still apply loudly:
* fills are perfect at intent.limit_price (no slippage, no cost)
* spread_bps == 0 (production spread_too_wide gate auto-passes)
* the verdict-gate caveat (0.6 anti-leak hardening) is still in
  force — this is build/run/inspect, NOT a verdict.

Bar pattern (per timeframe, independently consistent)
-----------------------------------------------------
* AAPL: close[i] = 100 + 0.15*i, high = close + 0.10, low = close - 0.10.
  Step (0.15) > wick (0.10)  =>  close[i] > high[i-1] on every bar
  (breakout fires unconditionally). TR ~ 0.25, ATR/close in
  [0.0021, 0.0017] across the series (in [0.0005, 0.03] band).
  Expected-move ~ (2.0 * 0.25 / close) * 1e4  >=  ~41bps  >  40 min.
* MSFT: flat at $100 — signal denies "no_breakout", strategy emits
  DIAGNOSTIC incidents only.
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from research.engine import driver as drv

_HAS_PMC = importlib.util.find_spec("pandas_market_calendars") is not None
requires_pmc = pytest.mark.skipif(
    not _HAS_PMC, reason="pandas_market_calendars not installed (research-only dep)",
)

pd = pytest.importorskip("pandas")

UTC = timezone.utc
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "sample_config.yaml"
TEST_ENV = {"TEST_ALPACA_API_KEY": "k", "TEST_ALPACA_API_SECRET": "s"}


# --- bar builders ----------------------------------------------------------

def _rising_frame(tf_min: int, n_bars: int, end_ts: datetime, *,
                  base: float = 100.0, step: float = 0.15,
                  wick: float = 0.10):
    """Monotonically rising OHLCV designed so every bar breaks out.

    step > wick guarantees close[i] > high[i-1] for all i (where
    high[i-1] = close[i-1] + wick), so any breakout_lookback >= 1
    triggers on every bar after warmup.
    """
    timestamps = [end_ts - timedelta(minutes=tf_min * (n_bars - 1 - i))
                  for i in range(n_bars)]
    idx = pd.DatetimeIndex(timestamps, tz="UTC", name="ts")
    closes = [base + step * i for i in range(n_bars)]
    highs  = [c + wick for c in closes]
    lows   = [c - wick for c in closes]
    opens  = [base] + closes[:-1]
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": [1000] * n_bars,
    }, index=idx)


def _flat_frame(tf_min: int, n_bars: int, end_ts: datetime,
                base: float = 100.0):
    """Flat-price OHLCV — no breakout possible."""
    timestamps = [end_ts - timedelta(minutes=tf_min * (n_bars - 1 - i))
                  for i in range(n_bars)]
    idx = pd.DatetimeIndex(timestamps, tz="UTC", name="ts")
    return pd.DataFrame({
        "open": [base] * n_bars, "high": [base] * n_bars,
        "low":  [base] * n_bars, "close": [base] * n_bars,
        "volume": [1000] * n_bars,
    }, index=idx)


def _bars_for_scenario(end_ts: datetime) -> dict:
    """AAPL rises (will trigger), MSFT flat (will only deny).

    Bar counts are tuned to comfortably exceed the strategy's
    min_bars_*_tf thresholds (100 entry / 60 confirm / 210 trend).
    """
    return {
        ("AAPL", 60): _rising_frame(60, 250, end_ts),
        ("AAPL", 15): _rising_frame(15,  80, end_ts),
        ("AAPL",  5): _rising_frame( 5, 140, end_ts),
        ("MSFT", 60): _flat_frame(60, 250, end_ts),
        ("MSFT", 15): _flat_frame(15,  80, end_ts),
        ("MSFT",  5): _flat_frame( 5, 140, end_ts),
    }


# --- the integration test --------------------------------------------------

@requires_pmc
def test_one_entry_cycle_end_to_end(tmp_path) -> None:
    """Drive Strategy through one full INTENT->RESULT entry cycle.

    Expected outcome:
      * 1 INTENT for AAPL with result="submitting" (the real entry)
      * 1 RESULT with matching intent_id and status="filled"
      * Zero submit-intents for MSFT (flat bars cannot break out)
      * 1 position in broker.get_positions() — AAPL, qty > 0
      * Cash debited by exactly limit_price * qty (perfect fill)
      * Hash chain intact

    The window deliberately spans the session-start blackout so we
    also exercise the "outside session / in blackout" branches with
    no incidents from the entry path (silent, by design).
    """
    day = date(2025, 6, 2)  # Monday, regular XNYS 13:30-20:00 UTC
    from research.data import calendar as cal
    open_utc, _ = cal.session_bounds(day)
    start = open_utc                                  # 13:30 (XNYS open)
    end = open_utc + timedelta(minutes=20)            # 13:50

    # Bars extend past `end` so each tick sees a fresh entry-tf bar.
    bars = _bars_for_scenario(end_ts=end + timedelta(minutes=5))

    d = drv.BacktestDriver.build(
        config_path=FIXTURE, bars=bars,
        start=start, end=end, starting_cash=Decimal("100000"),
        state_dir=tmp_path, env=TEST_ENV,
    )
    res = d.run()

    # --- audit invariants ------------------------------------------------
    # Exactly one entry-intent for AAPL.
    aapl_submits = [
        r for r in res.intents
        if r.payload.get("symbol") == "AAPL"
        and r.payload.get("result") == "submitting"
    ]
    assert len(aapl_submits) == 1, (
        f"expected exactly one AAPL submit intent; got {len(aapl_submits)}. "
        f"All intents: "
        f"{[(r.payload.get('symbol'), r.payload.get('result'), r.payload.get('reason')) for r in res.intents]}"
    )
    submit = aapl_submits[0]
    assert submit.payload["side"] == "BUY"
    assert submit.payload["qty_requested"] > 0
    assert submit.payload["reason"] == "breakout_with_trend_and_confirmation"

    # Exactly one matching RESULT, filled.
    intent_id = submit.payload["intent_id"]
    matching_results = [
        r for r in res.results if r.payload.get("intent_id") == intent_id
    ]
    assert len(matching_results) == 1, (
        f"expected exactly one RESULT for intent_id={intent_id}; "
        f"got {len(matching_results)}"
    )
    fill = matching_results[0]
    assert fill.payload["status"] == "filled"
    assert int(fill.payload["filled_qty"]) == submit.payload["qty_requested"]
    # Perfect fill: avg price == limit price (Phase-1 limitation, asserted
    # loudly so it can't drift silently when Phase-2 adds slippage).
    assert Decimal(fill.payload["avg_fill_price"]) == Decimal(submit.payload["limit_price"])
    # OTO child protective stop was submitted.
    assert fill.payload.get("protective_child_broker_id") is not None

    # MSFT (flat) never gets to the submit branch — only DIAGNOSTIC
    # incidents at most (signal denies before risk gate).
    msft_submits = [
        r for r in res.intents
        if r.payload.get("symbol") == "MSFT"
        and r.payload.get("result") == "submitting"
    ]
    assert msft_submits == [], (
        f"flat MSFT must never break out; got {len(msft_submits)} submits"
    )
    # MSFT incident trail: at least one DIAGNOSTIC of decision=no_signal.
    msft_diags = [
        r for r in res.incidents
        if r.payload.get("kind") == "DIAGNOSTIC"
        and r.payload.get("symbol") == "MSFT"
        and r.payload.get("decision") == "no_signal"
    ]
    assert len(msft_diags) >= 1, "expected at least one MSFT no_signal diagnostic"

    # --- broker / state invariants --------------------------------------
    positions = d._broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert positions[0].qty == submit.payload["qty_requested"]

    qty = submit.payload["qty_requested"]
    limit = Decimal(submit.payload["limit_price"])
    snap = d._broker.get_account_snapshot()
    expected_cash = Decimal("100000") - limit * Decimal(qty)
    assert snap.cash == expected_cash, (
        f"cash debit mismatch: expected {expected_cash}, got {snap.cash}"
    )

    # state.open_trades reflects the entry (one canonical place — the
    # _finalize_entry path that recovery and normal entry share).
    assert "AAPL" in d._strategy.state.open_trades
    open_trade = d._strategy.state.open_trades["AAPL"]
    assert open_trade.qty == qty
    assert open_trade.entry_price == limit

    # --- hash chain integrity -------------------------------------------
    d._log.verify_integrity()

    # --- equity series sanity ------------------------------------------
    # Pre-entry ticks (13:30, 13:35): equity == starting cash (no pos).
    # Post-entry ticks: equity = cash + qty * mid (rising → equity rises).
    assert len(res.equity) == res.steps
    pre_entry_ticks = [(t, eq) for t, eq in res.equity if t < submit.ts]
    post_entry_ticks = [(t, eq) for t, eq in res.equity if t >= fill.ts]
    assert all(eq == Decimal("100000") for _, eq in pre_entry_ticks), (
        f"pre-entry equity drifted: {pre_entry_ticks}"
    )
    # Last equity reading should reflect MTM gain (mid rose between
    # entry tick and last tick).
    final_t, final_eq = res.equity[-1]
    assert final_eq > snap.cash, "post-entry equity should reflect MTM gain"
