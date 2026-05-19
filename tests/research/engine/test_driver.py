"""Tests for research.engine.driver — BacktestDriver.

The HARD GATE (per the locked Phase-1 plan) is
:func:`test_two_runs_produce_identical_trade_streams`: two independent
driver instances with identical inputs MUST produce byte-identical
RunResults (intents/results/incidents records AND equity series). Any
nondeterminism — dict ordering, RNG leak, wall-clock dependency — fails
loudly here, before it can contaminate any study.

Pure-Python helper tests run unconditionally. Anything that calls
``driver.run()`` is pmc-gated (driver loop uses XNYS calendar). Bar
construction is deliberately *minimal* (flat-price bars, fewer than the
strategy's ``min_bars_entry_tf`` threshold) so no signal fires — the
stream is pure diagnostics + equity, which is exactly what we need to
test determinism without dragging in signal-quality concerns.
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

UTC = timezone.utc
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "sample_config.yaml"
TEST_ENV = {"TEST_ALPACA_API_KEY": "k", "TEST_ALPACA_API_SECRET": "s"}


# --- unconditional pure-Python --------------------------------------------

def test_module_has_no_forbidden_production_imports() -> None:
    import inspect
    s = inspect.getsource(drv)
    for forbidden in ("strategy.broker", "run_strategy", "autoflow"):
        assert forbidden not in s
    # Allowed engine fundamentals SHOULD appear (sanity).
    assert "strategy.strategy" in s
    assert "strategy.state" in s


def test_align_to_boundary_already_aligned_returns_self() -> None:
    ts = datetime(2025, 6, 2, 13, 30, tzinfo=UTC)
    assert drv._align_to_boundary(ts, 5) == ts


def test_align_to_boundary_rounds_up() -> None:
    ts = datetime(2025, 6, 2, 13, 31, tzinfo=UTC)
    assert drv._align_to_boundary(ts, 5) == datetime(2025, 6, 2, 13, 35, tzinfo=UTC)
    ts = datetime(2025, 6, 2, 13, 34, 59, tzinfo=UTC)
    assert drv._align_to_boundary(ts, 5) == datetime(2025, 6, 2, 13, 35, tzinfo=UTC)


def test_constructor_rejects_naive_or_inverted_window() -> None:
    # We don't need real strategy/broker for input validation — just stubs.
    stub = object()
    with pytest.raises(ValueError, match="UTC"):
        drv.BacktestDriver(
            strategy=stub, broker=stub, trade_log=stub,
            start=datetime(2025, 6, 2, 13, 30),       # naive
            end=datetime(2025, 6, 2, 14, 0, tzinfo=UTC),
            tick_interval_min=5,
        )
    with pytest.raises(ValueError, match="strictly after"):
        drv.BacktestDriver(
            strategy=stub, broker=stub, trade_log=stub,
            start=datetime(2025, 6, 2, 14, 0, tzinfo=UTC),
            end=datetime(2025, 6, 2, 13, 30, tzinfo=UTC),
            tick_interval_min=5,
        )


# --- pmc-gated: end-to-end driver via the build() factory -----------------

pd = pytest.importorskip("pandas")


def _flat_frame(symbol: str, day: date, tf_min: int):
    """Minimal flat-price bars across a full XNYS session — enough that
    the SimulatedBroker has a quote (mid = close), too few to pass the
    strategy's ``min_bars_entry_tf`` filter so no signal fires."""
    if not _HAS_PMC:
        # If we reach here without pmc, the gated tests should have skipped.
        raise RuntimeError("pmc absent — test should be skipped")
    from research.data import calendar as cal
    open_utc, close_utc = cal.session_bounds(day)
    n_bars = int((close_utc - open_utc).total_seconds() // 60 // tf_min)
    ts = pd.DatetimeIndex(
        [open_utc + timedelta(minutes=i * tf_min) for i in range(n_bars)],
        tz="UTC", name="ts",
    )
    return pd.DataFrame({
        "open":   [100.0] * n_bars,
        "high":   [100.0] * n_bars,
        "low":    [100.0] * n_bars,
        "close":  [100.0] * n_bars,
        "volume": [1000] * n_bars,
    }, index=ts)


def _bars_for_two_symbols(day: date):
    return {
        (sym, tf): _flat_frame(sym, day, tf)
        for sym in ("AAPL", "MSFT")
        for tf in (5, 15, 60)
    }


@requires_pmc
def test_driver_short_window_produces_expected_step_count(tmp_path) -> None:
    day = date(2025, 6, 2)
    from research.data import calendar as cal
    open_utc, _ = cal.session_bounds(day)
    start = open_utc                                  # 13:30 UTC
    end = open_utc + timedelta(minutes=20)            # 13:50 UTC

    drv_ = drv.BacktestDriver.build(
        config_path=FIXTURE, bars=_bars_for_two_symbols(day),
        start=start, end=end, starting_cash=Decimal("100000"),
        state_dir=tmp_path, env=TEST_ENV,
    )
    res = drv_.run()
    # 5 ticks at 5-min boundaries: 13:30, 13:35, 13:40, 13:45, 13:50.
    assert res.steps == 5
    assert len(res.equity) == 5
    # All equity points are at exactly those boundaries, monotonic time.
    times = [t for t, _ in res.equity]
    assert times == sorted(times)
    # No signal fires (too few bars for min_bars_entry_tf) → no entries.
    assert res.results == ()


@requires_pmc
def test_driver_skips_non_trading_days_efficiently(tmp_path) -> None:
    """A multi-day window must not waste steps on weekends/holidays."""
    # Mon 2025-06-02 + Tue 2025-06-03 trading; Sat/Sun in between never
    # happen here, but we'll pick a window that includes Sunday 2025-06-01.
    sun = datetime(2025, 6, 1, 13, 30, tzinfo=UTC)   # not a trading day
    tue_end = datetime(2025, 6, 3, 13, 50, tzinfo=UTC)
    day_bars = _bars_for_two_symbols(date(2025, 6, 2))
    day_bars.update(_bars_for_two_symbols(date(2025, 6, 3)))
    # Merge per-day frames per (symbol, tf) so the dict has one frame
    # spanning both days.
    merged: dict = {}
    for (sym, tf), df in day_bars.items():
        merged[(sym, tf)] = pd.concat([merged[(sym, tf)], df]).sort_index() \
            if (sym, tf) in merged else df

    drv_ = drv.BacktestDriver.build(
        config_path=FIXTURE, bars=merged,
        start=sun, end=tue_end, starting_cash=Decimal("100000"),
        state_dir=tmp_path, env=TEST_ENV,
    )
    res = drv_.run()
    # Mon: full session (13:30..20:00 UTC = 390min / 5min + 1 = 79 ticks
    # if we include the upper bound). Tue: 13:30..13:50 = 5 ticks. Sun: 0.
    # 79 + 5 == 84 — verify the loop hit both days and skipped Sunday.
    assert res.steps == 79 + 5
    # No equity timestamp falls on Sunday.
    assert all(t.date() != date(2025, 6, 1) for t, _ in res.equity)


# --- THE HARD DETERMINISM GATE --------------------------------------------

@requires_pmc
def test_two_runs_produce_identical_trade_streams(tmp_path) -> None:
    """Independent driver instances + identical inputs ⇒ byte-identical
    RunResult. This is the foundational determinism guarantee Phase 1
    rests on. If this ever fails, do NOT proceed to Phase 2 — diagnose."""
    day = date(2025, 6, 2)
    from research.data import calendar as cal
    open_utc, _ = cal.session_bounds(day)
    start = open_utc
    end = open_utc + timedelta(minutes=45)

    def _build_and_run(state_subdir: str):
        sd = tmp_path / state_subdir
        sd.mkdir()
        d = drv.BacktestDriver.build(
            config_path=FIXTURE,
            bars=_bars_for_two_symbols(day),
            start=start, end=end, starting_cash=Decimal("100000"),
            state_dir=sd, env=TEST_ENV,
        )
        return d.run()

    a = _build_and_run("run_a")
    b = _build_and_run("run_b")

    # Step count + window identical.
    assert a.steps == b.steps
    assert a.start == b.start and a.end == b.end
    # Each record stream byte-identical (LogRecord eq compares all fields
    # including prev_hash and line_hash).
    assert a.intents   == b.intents
    assert a.results   == b.results
    assert a.incidents == b.incidents
    # Equity series byte-identical (tuple of (datetime, Decimal)).
    assert a.equity == b.equity
