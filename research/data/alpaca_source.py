"""Read-only Alpaca 1-minute historical bar source (Phase 0.1).

Scope: a typed, deterministic fetcher used by later sub-tasks (0.3
resample, 0.4 cache, 0.5 CLI). Strictly READ-ONLY (Alpaca historical
GETs). Lazy SDK imports preserve separability (importing this module does
not pull in ``alpaca.*`` or ``pandas`` at module-load time).

The "as-of" anti-leakage clamp is NOT implemented here — that is the
foundational accessor primitive introduced in 0.3 (resampler / safe
window) and locked down in 0.6 (tests). 0.1 fetches the requested raw
window faithfully and stops.

Return shape (validated):

* pandas DataFrame, indexed by UTC tz-aware ``ts`` (left-labeled,
  ``ts=OPEN`` — matches Alpaca's bar convention and the engine invariant)
* columns: ``open, high, low, close`` (float64), ``volume`` (int64)
* sorted ascending, monotonic & unique on ``ts``
* ``df.attrs`` records: ``symbol``, ``feed``, ``adjustment``,
  ``quarantined`` (tuple of dropped dates, audit trail)

Quarantine: ``KNOWN_BAD_DATES`` are excluded by default. Seeded from the
2026-05-19 availability probe (commit 2448430): 2024-12-23 returned
~50 1-min bars on all V1 symbols from IEX (vs ~390 for a full session) —
a single-date feed event, not a systemic gap. The cache (0.4) will stamp
this quarantine into the reproducibility manifest; every metrics report
must surface the exclusion.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any, Mapping

# Single source of truth for the research stack's known-bad dates. Add
# entries here (with a rationale string) only after a probe diagnoses them.
KNOWN_BAD_DATES: Mapping[date, str] = {
    date(2024, 12, 23): (
        "alpaca/iex truncated session (~50 bars on all V1 symbols); "
        "probe 2026-05-19"
    ),
}

_REQUIRED_COLS = ("open", "high", "low", "close", "volume")


def _client() -> Any:
    key = os.environ.get("ALPACA_API_KEY")
    sec = os.environ.get("ALPACA_API_SECRET")
    if not key or not sec:
        raise SystemExit(
            "ALPACA_API_KEY / ALPACA_API_SECRET not in environment. "
            "research.data.alpaca_source is read-only; run where creds exist."
        )
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(key, sec)


def fetch_1m_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    feed: str = "iex",
    adjustment: str = "all",
    client: Any | None = None,
    quarantine: Mapping[date, str] | None = None,
):
    """Read-only fetch of 1-minute bars from Alpaca historical.

    ``quarantine`` defaults to :data:`KNOWN_BAD_DATES`; pass ``{}`` to
    fetch everything (used by the probe / diagnostics, never by a study).
    """
    import pandas as pd  # noqa: I001 — lazy on purpose (separability)
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    if client is None:
        client = _client()
    feed_enum = DataFeed.IEX if feed == "iex" else DataFeed.SIP
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start, end=end, feed=feed_enum, adjustment=adjustment,
    )
    bars = client.get_stock_bars(req).data.get(symbol, [])
    df = _bars_to_df(bars)
    _assert_invariants(df)
    q = KNOWN_BAD_DATES if quarantine is None else quarantine
    df, dropped = _apply_quarantine(df, q)
    df.attrs.update(
        symbol=symbol, feed=feed, adjustment=adjustment, quarantined=dropped,
    )
    return df


def _bars_to_df(bars):
    import pandas as pd
    if not bars:
        idx = pd.DatetimeIndex([], tz="UTC", name="ts")
        return pd.DataFrame(
            {c: pd.Series(dtype="float64") for c in _REQUIRED_COLS[:-1]}
            | {"volume": pd.Series(dtype="int64")},
            index=idx,
        )
    rows = [
        {
            "ts": b.timestamp.astimezone(timezone.utc),
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": int(b.volume),
        }
        for b in bars
    ]
    df = pd.DataFrame(rows).set_index("ts").sort_index()
    df.index.name = "ts"
    return df


def _assert_invariants(df) -> None:
    if df.empty:
        return
    if df.index.tz is None or str(df.index.tz) != "UTC":
        raise ValueError("ts index must be UTC tz-aware")
    if not df.index.is_monotonic_increasing:
        raise ValueError("ts must be ascending")
    if not df.index.is_unique:
        raise ValueError("ts must be unique (no duplicate bars)")
    for col in _REQUIRED_COLS:
        if col not in df.columns:
            raise ValueError(f"missing required column: {col}")


def _apply_quarantine(df, q: Mapping[date, str]):
    """Drop bars whose date is in ``q``. Returns (filtered_df, dropped_dates)."""
    import pandas as pd
    if df.empty or not q:
        return df, ()
    bad = pd.to_datetime(sorted(q.keys()), utc=True)
    day_index = df.index.normalize()
    keep = ~day_index.isin(bad)
    dropped = tuple(sorted({d.date() for d in df.index[~keep].unique()}))
    return df.loc[keep], dropped
