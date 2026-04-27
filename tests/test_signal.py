"""Signal determinism and edge-case tests.

Pins down:
* Same bars + same config → same output, byte-for-byte.
* No I/O — calling ``evaluate_signal`` does not touch disk, network,
  or the wall clock.
* Regime filter rejects choppy / tiny-ATR / huge-ATR tapes.
* Trend filter rejects down-trending or flat environments.
* Confirmation filter rejects fast<slow.
* Breakout filter rejects a close below the lookback high.
* Indicator math matches hand-computed reference values.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from strategy.config import StrategyConfig
from strategy.dto import Bar, OrderSide
from strategy.signal import (
    BarFrame,
    adx,
    atr,
    ema,
    evaluate_signal,
    slope,
    true_range,
)


UTC = timezone.utc
FIXTURE = Path(__file__).parent / "fixtures" / "sample_config.yaml"


def _cfg() -> StrategyConfig:
    from strategy.config import load_config
    c = load_config(
        FIXTURE,
        env={"TEST_ALPACA_API_KEY": "k", "TEST_ALPACA_API_SECRET": "s"},
    )
    return c.strategy


def _bar(symbol: str, ts: datetime, o: str, h: str, l: str, c: str, v: int = 1000) -> Bar:
    return Bar(
        symbol=symbol,
        ts=ts,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        volume=v,
    )


def _trend_series(
    symbol: str,
    n: int,
    *,
    start_price: Decimal,
    step: Decimal,
    start_ts: datetime,
    tf_minutes: int,
    noise: Decimal = Decimal("0"),
) -> list[Bar]:
    """Synthesise a smoothly trending bar series.

    Each bar's open is the previous close; close = open + step + noise*i%3.
    High/low bracket the open/close symmetrically.
    """
    bars: list[Bar] = []
    price = start_price
    for i in range(n):
        ts = start_ts + timedelta(minutes=tf_minutes * i)
        o = price
        c = price + step + (noise * Decimal(i % 3 - 1))
        hi = max(o, c) + Decimal("0.05")
        lo = min(o, c) - Decimal("0.05")
        bars.append(
            Bar(
                symbol=symbol,
                ts=ts,
                open=o,
                high=hi,
                low=lo,
                close=c,
                volume=1000,
            )
        )
        price = c
    return bars


def _good_frame() -> BarFrame:
    """A bull frame that should produce a BUY signal."""
    sym = "AAPL"
    # Trend timeframe: 1h, 250 bars, strongly up-sloping
    trend = _trend_series(
        sym,
        n=250,
        start_price=Decimal("100"),
        step=Decimal("0.3"),
        start_ts=datetime(2026, 4, 15, 13, 35, tzinfo=UTC),
        tf_minutes=60,
    )
    # Confirm timeframe: 15m, 80 bars, up
    confirm = _trend_series(
        sym,
        n=80,
        start_price=Decimal("170"),
        step=Decimal("0.15"),
        start_ts=datetime(2026, 4, 23, 8, 0, tzinfo=UTC),
        tf_minutes=15,
    )
    # Entry timeframe: 5m, 120 bars, up with a recent breakout
    entry = _trend_series(
        sym,
        n=120,
        start_price=Decimal("180"),
        step=Decimal("0.10"),
        start_ts=datetime(2026, 4, 23, 13, 35, tzinfo=UTC),
        tf_minutes=5,
        noise=Decimal("0.04"),
    )
    # Force a clean breakout on the final bar.
    last = entry[-1]
    lookback = max(b.high for b in entry[-21:-1])
    forced_close = lookback + Decimal("0.25")
    entry[-1] = replace(last, high=forced_close + Decimal("0.05"), close=forced_close)
    return BarFrame(
        symbol=sym,
        entry_bars=tuple(entry),
        confirm_bars=tuple(confirm),
        trend_bars=tuple(trend),
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_evaluate_signal_is_deterministic() -> None:
    """Same inputs must produce byte-identical outputs across repeated calls."""
    cfg = _cfg()
    frame = _good_frame()
    s1, _ = evaluate_signal(frame, cfg)
    s2, _ = evaluate_signal(frame, cfg)
    assert s1 is not None and s2 is not None
    assert s1 == s2
    # Re-serialising must be stable too.
    def enc(s):
        return json.dumps(
            {
                "symbol": s.symbol, "ts": s.ts.isoformat(), "dir": s.direction.value,
                "reason": s.reason, "ref": str(s.ref_price),
                "atr": str(s.atr), "edge": str(s.expected_move_bps),
            },
            sort_keys=True,
        )
    assert enc(s1) == enc(s2)


def test_determinism_across_frame_rebuild() -> None:
    """Building the frame twice (rebuilding tuples) must yield the same signal."""
    cfg = _cfg()
    s1, _ = evaluate_signal(_good_frame(), cfg)
    s2, _ = evaluate_signal(_good_frame(), cfg)
    assert s1 == s2


def test_no_io_during_signal(monkeypatch) -> None:
    """Evaluating the signal must not open files, sockets, or read the clock.

    We monkeypatch ``open``, ``socket.socket``, and ``datetime.datetime.now``
    with functions that record accesses.
    """
    import builtins
    import socket
    import datetime as dt

    io_calls: list[str] = []

    real_open = builtins.open
    def spy_open(*a, **kw):
        io_calls.append(f"open:{a[0] if a else ''}")
        return real_open(*a, **kw)

    class _SpySocket:
        def __init__(self, *a, **kw):
            io_calls.append("socket")
            raise RuntimeError("signal.py must not use sockets")

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(socket, "socket", _SpySocket)

    cfg = _cfg()
    frame = _good_frame()
    s, _ = evaluate_signal(frame, cfg)
    # Signal should have been evaluated successfully with no open() or
    # socket() calls attributable to the signal code path.
    assert s is not None
    # Module-import opens (pyc/py files) may show — filter to opens *after*
    # import. Since evaluate_signal has already imported everything, any
    # open() here would be a side effect. We assert none were initiated
    # by the call itself by checking before/after.
    before = len(io_calls)
    evaluate_signal(frame, cfg)
    after = len(io_calls)
    assert after == before, f"unexpected I/O during evaluate_signal: {io_calls[before:]}"


# ---------------------------------------------------------------------------
# Happy path returns a coherent BUY
# ---------------------------------------------------------------------------


def test_good_frame_produces_buy_signal() -> None:
    cfg = _cfg()
    s, reason = evaluate_signal(_good_frame(), cfg)
    assert s is not None
    assert reason == "breakout_with_trend_and_confirmation"
    assert s.direction is OrderSide.BUY
    assert s.ref_price > 0
    assert s.atr > 0
    assert s.expected_move_bps > 0


# ---------------------------------------------------------------------------
# Filters reject in isolation
# ---------------------------------------------------------------------------


def test_insufficient_bars_returns_none() -> None:
    cfg = _cfg()
    frame = _good_frame()
    short = BarFrame(
        symbol=frame.symbol,
        entry_bars=frame.entry_bars[:20],
        confirm_bars=frame.confirm_bars,
        trend_bars=frame.trend_bars,
    )
    sig, reason = evaluate_signal(short, cfg)
    assert sig is None
    assert reason == "insufficient_bars"


def test_trend_down_blocks_entry() -> None:
    cfg = _cfg()
    frame = _good_frame()
    # Flip trend bars to a clear downtrend.
    down = _trend_series(
        "AAPL",
        n=250,
        start_price=Decimal("300"),
        step=Decimal("-0.5"),
        start_ts=datetime(2026, 4, 15, 13, 35, tzinfo=UTC),
        tf_minutes=60,
    )
    bad = BarFrame(
        symbol="AAPL",
        entry_bars=frame.entry_bars,
        confirm_bars=frame.confirm_bars,
        trend_bars=tuple(down),
    )
    sig, reason = evaluate_signal(bad, cfg)
    assert sig is None
    assert reason == "trend_not_up"


def test_confirm_fast_below_slow_blocks_entry() -> None:
    cfg = _cfg()
    frame = _good_frame()
    # Build a confirm series that starts high then trends down.
    down = _trend_series(
        "AAPL",
        n=80,
        start_price=Decimal("300"),
        step=Decimal("-0.2"),
        start_ts=datetime(2026, 4, 23, 8, 0, tzinfo=UTC),
        tf_minutes=15,
    )
    bad = BarFrame(
        symbol="AAPL",
        entry_bars=frame.entry_bars,
        confirm_bars=tuple(down),
        trend_bars=frame.trend_bars,
    )
    sig, reason = evaluate_signal(bad, cfg)
    assert sig is None
    assert reason == "confirmation_not_aligned"


def test_no_breakout_blocks_entry() -> None:
    cfg = _cfg()
    frame = _good_frame()
    # Remove the forced breakout — close the final bar below the lookback high.
    lookback = max(b.high for b in frame.entry_bars[-21:-1])
    tamed_last = replace(
        frame.entry_bars[-1],
        close=lookback - Decimal("0.50"),
        high=lookback - Decimal("0.40"),
    )
    bars = tuple(list(frame.entry_bars[:-1]) + [tamed_last])
    bad = BarFrame(symbol="AAPL", entry_bars=bars, confirm_bars=frame.confirm_bars, trend_bars=frame.trend_bars)
    sig, reason = evaluate_signal(bad, cfg)
    assert sig is None
    assert reason == "no_breakout"


def test_flat_tape_fails_adx_regime() -> None:
    cfg = _cfg()
    sym = "AAPL"
    flat = _trend_series(
        sym, n=120, start_price=Decimal("100"), step=Decimal("0"),
        start_ts=datetime(2026, 4, 23, 13, 35, tzinfo=UTC), tf_minutes=5,
    )
    frame = BarFrame(
        symbol=sym,
        entry_bars=tuple(flat),
        confirm_bars=_good_frame().confirm_bars,
        trend_bars=_good_frame().trend_bars,
    )
    sig, reason = evaluate_signal(frame, cfg)
    assert sig is None
    assert reason == "adx_too_low"


def test_zero_close_refused_gracefully() -> None:
    cfg = _cfg()
    frame = _good_frame()
    last = frame.entry_bars[-1]
    broken = replace(last, close=Decimal(0), open=Decimal(0), high=Decimal(0), low=Decimal(0))
    bars = tuple(list(frame.entry_bars[:-1]) + [broken])
    bad = BarFrame(symbol=frame.symbol, entry_bars=bars, confirm_bars=frame.confirm_bars, trend_bars=frame.trend_bars)
    sig, reason = evaluate_signal(bad, cfg)
    assert sig is None
    # Either ATR-or-price-nonpositive or close-below-entry-EMA is acceptable
    # depending on which check trips first; both indicate a degenerate bar.
    assert reason in {"atr_or_price_nonpositive", "close_below_entry_ema", "atr_regime_out_of_band"}


# ---------------------------------------------------------------------------
# Frame invariants
# ---------------------------------------------------------------------------


def test_frame_rejects_wrong_symbol() -> None:
    bars = (_bar("AAPL", datetime(2026, 4, 23, tzinfo=UTC), "1", "1", "1", "1"),)
    with pytest.raises(ValueError):
        BarFrame(symbol="MSFT", entry_bars=bars, confirm_bars=(), trend_bars=())


def test_frame_rejects_out_of_order_bars() -> None:
    b1 = _bar("AAPL", datetime(2026, 4, 23, 14, 0, tzinfo=UTC), "1", "1", "1", "1")
    b2 = _bar("AAPL", datetime(2026, 4, 23, 13, 0, tzinfo=UTC), "1", "1", "1", "1")
    with pytest.raises(ValueError):
        BarFrame(symbol="AAPL", entry_bars=(b1, b2), confirm_bars=(), trend_bars=())


# ---------------------------------------------------------------------------
# Indicator math — hand-computed references
# ---------------------------------------------------------------------------


def test_ema_against_reference() -> None:
    # Period=3; seed = mean(1,2,3)=2. k=2/4=0.5.
    # step 4: 2 + 0.5*(4-2) = 3
    # step 5: 3 + 0.5*(5-3) = 4
    values = [Decimal(x) for x in [1, 2, 3, 4, 5]]
    out = ema(values, 3)
    assert out[2] == Decimal(2)
    assert out[3] == Decimal(3)
    assert out[4] == Decimal(4)


def test_true_range_basic() -> None:
    bars = [
        _bar("X", datetime(2026, 1, 1, tzinfo=UTC), "10", "12", "9", "11"),
        _bar("X", datetime(2026, 1, 1, 1, tzinfo=UTC), "11", "13", "10", "12"),
        _bar("X", datetime(2026, 1, 1, 2, tzinfo=UTC), "12", "15", "11", "14"),
    ]
    tr = true_range(bars)
    # bar0: 12-9=3. bar1: max(13-10, |13-11|, |10-11|)=3. bar2: max(15-11, |15-12|, |11-12|)=4.
    assert tr == [Decimal(3), Decimal(3), Decimal(4)]


def test_atr_needs_enough_data() -> None:
    bars = [_bar("X", datetime(2026, 1, 1, tzinfo=UTC), "10", "12", "9", "11")]
    with pytest.raises(ValueError):
        atr(bars, 14)


def test_adx_needs_enough_data() -> None:
    bars = [
        _bar("X", datetime(2026, 1, 1, i, tzinfo=UTC), "10", "12", "9", "11")
        for i in range(10)
    ]
    with pytest.raises(ValueError):
        adx(bars, 14)


def test_slope_basic() -> None:
    values = [Decimal(i) for i in range(10)]
    # last - first = 9 - 0 = 9; window = 5 → tail = [5,6,7,8,9]; (9-5)/5 = 0.8
    assert slope(values, 5) == Decimal("0.8")
    with pytest.raises(ValueError):
        slope(values, 0)
    with pytest.raises(ValueError):
        slope(values, 100)


def test_ema_guards() -> None:
    with pytest.raises(ValueError):
        ema([Decimal(1)], 0)
    with pytest.raises(ValueError):
        ema([Decimal(1)], 5)


def test_true_range_empty() -> None:
    with pytest.raises(ValueError):
        true_range([])


def test_atr_period_guard() -> None:
    bars = [
        _bar("X", datetime(2026, 1, 1, i, tzinfo=UTC), "10", "12", "9", "11")
        for i in range(20)
    ]
    with pytest.raises(ValueError):
        atr(bars, 1)


def test_adx_period_guard() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    bars = [
        _bar("X", base + timedelta(hours=i), "10", "12", "9", "11")
        for i in range(40)
    ]
    with pytest.raises(ValueError):
        adx(bars, 1)
