"""Offline tests for research.data.resample (Phase 0.3).

Pure-Python invariants run unconditionally; aggregation/session-boundary
correctness needs ``pandas_market_calendars`` and is gated by
``@requires_pmc`` (runs in ``.venv-research``, skips in engine ``.venv``).

THE foundational test is :func:`test_future_bar_injection_is_clipped` —
the anti-leak guarantee that 0.6 will harden: hostile fetcher returns
bars after ``as_of`` and :func:`safe_bars` MUST NOT leak any of them.
"""
from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta, timezone

import pytest

from research.data import resample as rs
from research.data.resample import LookaheadError

_HAS_PMC = importlib.util.find_spec("pandas_market_calendars") is not None
requires_pmc = pytest.mark.skipif(
    not _HAS_PMC, reason="pandas_market_calendars not installed (research-only dep)",
)


# --- unconditional ---------------------------------------------------------

def test_module_has_no_production_imports() -> None:
    import inspect
    s = inspect.getsource(rs)
    for forbidden in ("strategy.broker", "strategy.strategy",
                      "strategy.recovery", "run_strategy", "autoflow"):
        assert forbidden not in s


def test_lookahead_error_is_public() -> None:
    # Studies must be able to `except LookaheadError`.
    assert issubclass(LookaheadError, Exception)
    assert LookaheadError.__module__.endswith("resample")


pd = pytest.importorskip("pandas")


def _empty_1m():
    idx = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in ("open", "high", "low", "close")}
        | {"volume": pd.Series(dtype="int64")},
        index=idx,
    )


def test_resample_rejects_non_utc_input() -> None:
    df = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0], "volume": [1]},
        index=pd.DatetimeIndex([datetime(2026, 6, 1, 14, 0)], name="ts"),
    )
    with pytest.raises(ValueError, match="UTC"):
        rs.resample(df, 5)


def test_resample_rejects_bad_tf() -> None:
    with pytest.raises(ValueError):
        rs.resample(_empty_1m(), 0)


def test_resample_empty_input_returns_empty_typed_frame() -> None:
    out = rs.resample(_empty_1m(), 5)
    assert out.empty
    assert str(out.index.tz) == "UTC"
    for c in ("open", "high", "low", "close", "volume"):
        assert c in out.columns


def test_safe_bars_requires_utc_as_of() -> None:
    with pytest.raises(ValueError, match="UTC"):
        rs.safe_bars("SPY", 5, datetime(2026, 6, 1, 18, 0),
                     lookback_window=timedelta(hours=1),
                     fetch_1m=lambda *a, **kw: _empty_1m())


# --- pmc-gated: correctness ------------------------------------------------

def _mk_1m_bars_for_day(day: date, n_bars: int, *, start_offset_min: int = 0):
    """Synth 1-min bars beginning at the XNYS session open + offset."""
    from research.data import calendar as cal
    open_utc, _close_utc = cal.session_bounds(day)
    start = open_utc + timedelta(minutes=start_offset_min)
    ts = [start + timedelta(minutes=i) for i in range(n_bars)]
    return pd.DataFrame({
        "open":   [100.0 + i * 0.01 for i in range(n_bars)],
        "high":   [100.5 + i * 0.01 for i in range(n_bars)],
        "low":    [ 99.5 + i * 0.01 for i in range(n_bars)],
        "close":  [100.2 + i * 0.01 for i in range(n_bars)],
        "volume": [1000 + i for i in range(n_bars)],
    }, index=pd.DatetimeIndex(ts, tz="UTC", name="ts"))


@requires_pmc
def test_resample_5m_left_labeled_ts_open_first_bucket_is_session_open() -> None:
    d = date(2026, 6, 1)             # full regular session
    df = _mk_1m_bars_for_day(d, n_bars=390)
    out = rs.resample(df, 5)
    from research.data import calendar as cal
    open_utc, _ = cal.session_bounds(d)
    # 390 1m bars -> 78 5m buckets, first bucket ts == session open.
    assert len(out) == 78
    assert out.index[0] == open_utc
    # Aggregation correctness on the first bucket.
    first = out.iloc[0]
    assert first["open"]   == 100.0                       # first of 1m[0..5)
    assert first["high"]   == max(100.5 + i*0.01 for i in range(5))
    assert first["low"]    == min( 99.5 + i*0.01 for i in range(5))
    assert first["close"]  == 100.2 + 4*0.01              # last 1m close
    assert first["volume"] == sum(1000 + i for i in range(5))


@requires_pmc
def test_resample_15m_and_60m_yield_correct_counts() -> None:
    d = date(2026, 6, 1)
    df = _mk_1m_bars_for_day(d, 390)
    assert len(rs.resample(df, 15)) == 26    # 390 / 15
    assert len(rs.resample(df, 60)) == 7     # 6 full hours + a 30-min remainder bucket


@requires_pmc
def test_resample_does_not_bleed_across_sessions() -> None:
    """Two trading days concatenated → no bucket spans the day boundary."""
    d1, d2 = date(2026, 6, 1), date(2026, 6, 2)
    df = pd.concat([_mk_1m_bars_for_day(d1, 390), _mk_1m_bars_for_day(d2, 390)])
    out = rs.resample(df, 5)
    # Every bar's date is one of the two; no bar spans both.
    dates_seen = set(out.index.date)
    assert dates_seen == {d1, d2}
    # Counts: 78 5m buckets per session.
    assert (out.index.date == d1).sum() == 78
    assert (out.index.date == d2).sum() == 78


@requires_pmc
def test_resample_drops_out_of_session_bars_defensively() -> None:
    """Pre/post-market bars must be filtered, not aggregated."""
    d = date(2026, 6, 1)
    from research.data import calendar as cal
    open_utc, close_utc = cal.session_bounds(d)
    # Two bars BEFORE open, two AFTER close, sandwiching a real session bar.
    extra_pre = [open_utc - timedelta(minutes=30), open_utc - timedelta(minutes=15)]
    extra_post = [close_utc + timedelta(minutes=1), close_utc + timedelta(minutes=10)]
    real_ts = [open_utc + timedelta(minutes=i) for i in range(5)]
    all_ts = sorted(extra_pre + real_ts + extra_post)
    df = pd.DataFrame({
        "open":   [1.0] * len(all_ts),
        "high":   [1.0] * len(all_ts),
        "low":    [1.0] * len(all_ts),
        "close":  [1.0] * len(all_ts),
        "volume": [1] * len(all_ts),
    }, index=pd.DatetimeIndex(all_ts, tz="UTC", name="ts"))
    out = rs.resample(df, 5)
    assert len(out) == 1                       # exactly the 5 real bars
    assert out.index[0] == open_utc            # at session open


# --- pmc-gated: anti-leak (THE foundational guarantee) --------------------

def _fake_fetcher(bars_df):
    """A fetcher that ignores its window and returns the canned DataFrame."""
    def fetch(symbol, start, end, **kw):
        return bars_df.copy()
    return fetch


@requires_pmc
def test_safe_bars_clips_to_close_ts_at_or_before_as_of() -> None:
    """The accessor must include a bar iff bar.ts + tf <= as_of."""
    d = date(2026, 6, 1)
    from research.data import calendar as cal
    open_utc, _ = cal.session_bounds(d)
    # 1m bars across one hour, fed wholesale.
    bars = _mk_1m_bars_for_day(d, 60)
    # Set as_of exactly at the close of the 6th 5-min bar (open + 30m).
    as_of = open_utc + timedelta(minutes=30)
    out = rs.safe_bars(
        "SPY", 5, as_of,
        lookback_window=timedelta(hours=1),
        fetch_1m=_fake_fetcher(bars),
    )
    # Buckets: 0..5m, 5..10m, ..., 25..30m  → 6 bars, last close == as_of.
    assert len(out) == 6
    last_close = out.index[-1] + pd.Timedelta(minutes=5)
    assert last_close == as_of
    # No bar has close_ts > as_of.
    assert all((out.index + pd.Timedelta(minutes=5)) <= as_of)


@requires_pmc
def test_future_bar_injection_is_clipped() -> None:
    """FOUNDATIONAL anti-leak guarantee. A hostile fetcher returns bars
    AFTER ``as_of``; safe_bars must NOT leak any of them and the output
    must contain only bars whose close_ts <= as_of. (0.6 will harden
    this with more variants.)"""
    d = date(2026, 6, 1)
    from research.data import calendar as cal
    open_utc, _ = cal.session_bounds(d)
    bars = _mk_1m_bars_for_day(d, 120)             # 2h of bars
    as_of = open_utc + timedelta(minutes=20)       # only 4 full 5m buckets allowed

    out = rs.safe_bars(
        "SPY", 5, as_of,
        lookback_window=timedelta(hours=3),
        fetch_1m=_fake_fetcher(bars),
    )
    # Strictly clipped.
    assert len(out) == 4
    assert all((out.index + pd.Timedelta(minutes=5)) <= as_of)
    # The hostile rows ARE present in the input but absent from output.
    assert bars.index[-1] > as_of
    assert out.index[-1] + pd.Timedelta(minutes=5) <= as_of


@requires_pmc
def test_safe_bars_records_metadata_in_attrs() -> None:
    d = date(2026, 6, 1)
    from research.data import calendar as cal
    open_utc, _ = cal.session_bounds(d)
    as_of = open_utc + timedelta(minutes=30)
    out = rs.safe_bars(
        "SPY", 5, as_of,
        lookback_window=timedelta(hours=1),
        fetch_1m=_fake_fetcher(_mk_1m_bars_for_day(d, 60)),
    )
    assert out.attrs["symbol"] == "SPY"
    assert out.attrs["tf_minutes"] == 5
    assert out.attrs["as_of"] == as_of
    assert out.attrs["lookback_window"] == timedelta(hours=1)


@requires_pmc
def test_safe_bars_empty_when_no_bars_old_enough() -> None:
    """If every fetched bar's close is in the future, output is empty
    (and does NOT raise — that's only for the defensive post-condition)."""
    d = date(2026, 6, 1)
    from research.data import calendar as cal
    open_utc, _ = cal.session_bounds(d)
    # 1 minute of bars; ask for 5m bars with as_of < first bar's close.
    bars = _mk_1m_bars_for_day(d, 1)
    out = rs.safe_bars(
        "SPY", 5, open_utc,                         # as_of == open; no 5m bar closes yet
        lookback_window=timedelta(hours=1),
        fetch_1m=_fake_fetcher(bars),
    )
    assert out.empty
