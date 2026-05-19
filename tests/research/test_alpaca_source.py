"""Offline tests for research.data.alpaca_source.

No network. Uses hand-crafted DataFrames + a fake Alpaca client so we
exercise validation, the quarantine logic, and the separability boundary
without credentials.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from research.data import alpaca_source as src


# --- KNOWN_BAD_DATES contract ---------------------------------------------

def test_known_bad_dates_includes_documented_quarantine() -> None:
    assert date(2024, 12, 23) in src.KNOWN_BAD_DATES
    rationale = src.KNOWN_BAD_DATES[date(2024, 12, 23)].lower()
    assert "iex" in rationale and "probe" in rationale


def test_module_has_no_production_imports() -> None:
    """Separability — must not pull in the live engine."""
    import inspect
    s = inspect.getsource(src)
    for forbidden in ("strategy.broker", "strategy.strategy",
                      "strategy.recovery", "run_strategy", "autoflow"):
        assert forbidden not in s


def test_fails_closed_without_credentials(monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(SystemExit):
        src._client()


# --- pandas-bound tests (skip where pandas missing — engine venv has it) --

pd = pytest.importorskip("pandas")


def _frame(timestamps, *, opens=None, vols=None):
    n = len(timestamps)
    idx = pd.DatetimeIndex(timestamps, name="ts")
    return pd.DataFrame({
        "open":   opens or [1.0] * n,
        "high":   [1.5] * n,
        "low":    [0.5] * n,
        "close":  [1.2] * n,
        "volume": vols or [100] * n,
    }, index=idx)


def test_invariants_reject_non_utc_index() -> None:
    df = _frame([datetime(2025, 1, 2, 14, 30)])  # naive
    with pytest.raises(ValueError, match="UTC"):
        src._assert_invariants(df)


def test_invariants_reject_unsorted_or_duplicate_ts() -> None:
    t = [pd.Timestamp("2025-01-02 14:30", tz="UTC"),
         pd.Timestamp("2025-01-02 14:30", tz="UTC")]
    df = _frame(t)
    with pytest.raises(ValueError):
        src._assert_invariants(df)


def test_invariants_require_all_ohlcv_columns() -> None:
    idx = pd.DatetimeIndex([pd.Timestamp("2025-01-02 14:30", tz="UTC")], name="ts")
    df = pd.DataFrame({"open": [1.0]}, index=idx)
    with pytest.raises(ValueError, match="missing required column"):
        src._assert_invariants(df)


def test_invariants_accept_empty_frame() -> None:
    idx = pd.DatetimeIndex([], tz="UTC", name="ts")
    df = pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in ("open", "high", "low", "close")}
        | {"volume": pd.Series(dtype="int64")},
        index=idx,
    )
    src._assert_invariants(df)  # must not raise


def test_quarantine_drops_only_listed_dates() -> None:
    ts = [
        pd.Timestamp("2024-12-22 15:00", tz="UTC"),  # keep
        pd.Timestamp("2024-12-23 15:00", tz="UTC"),  # drop (quarantined)
        pd.Timestamp("2024-12-23 15:01", tz="UTC"),  # drop
        pd.Timestamp("2024-12-26 15:00", tz="UTC"),  # keep
    ]
    df = _frame(ts)
    out, dropped = src._apply_quarantine(df, src.KNOWN_BAD_DATES)
    assert dropped == (date(2024, 12, 23),)
    assert list(out.index.normalize().unique().date) == [
        date(2024, 12, 22), date(2024, 12, 26),
    ]


def test_quarantine_is_noop_when_empty() -> None:
    ts = [pd.Timestamp("2024-12-23 15:00", tz="UTC")]
    df = _frame(ts)
    out, dropped = src._apply_quarantine(df, {})
    assert dropped == ()
    assert len(out) == 1


# --- end-to-end fetch with a FAKE alpaca client (no network) --------------

class _FakeBar:
    def __init__(self, ts, o, h, l, c, v):
        self.timestamp = ts
        self.open, self.high, self.low, self.close = o, h, l, c
        self.volume = v


class _FakeResp:
    def __init__(self, symbol, bars):
        self.data = {symbol: bars}


class _FakeClient:
    def __init__(self, bars):
        self._bars = bars
        self.calls: list = []

    def get_stock_bars(self, req):
        self.calls.append(req)
        return _FakeResp(req.symbol_or_symbols, self._bars)


def test_fetch_1m_bars_full_path_quarantine_applied() -> None:
    bars = [
        _FakeBar(datetime(2024, 12, 22, 15, 0, tzinfo=timezone.utc), 1, 2, 0.5, 1.5, 10),
        _FakeBar(datetime(2024, 12, 23, 15, 0, tzinfo=timezone.utc), 1, 2, 0.5, 1.5, 10),
        _FakeBar(datetime(2024, 12, 26, 15, 0, tzinfo=timezone.utc), 1, 2, 0.5, 1.5, 10),
    ]
    out = src.fetch_1m_bars(
        "TEST",
        datetime(2024, 12, 20, tzinfo=timezone.utc),
        datetime(2024, 12, 27, tzinfo=timezone.utc),
        client=_FakeClient(bars),
    )
    assert len(out) == 2
    assert out.attrs["symbol"] == "TEST"
    assert out.attrs["feed"] == "iex"
    assert out.attrs["adjustment"] == "all"
    assert out.attrs["quarantined"] == (date(2024, 12, 23),)
    assert str(out.index.tz) == "UTC"
    assert out.index.is_monotonic_increasing and out.index.is_unique
    for c in ("open", "high", "low", "close", "volume"):
        assert c in out.columns


def test_fetch_1m_bars_empty_response_returns_typed_empty_frame() -> None:
    out = src.fetch_1m_bars(
        "TEST",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
        client=_FakeClient([]),
    )
    assert out.empty
    assert str(out.index.tz) == "UTC"
    for c in ("open", "high", "low", "close", "volume"):
        assert c in out.columns
    assert out.attrs["quarantined"] == ()
