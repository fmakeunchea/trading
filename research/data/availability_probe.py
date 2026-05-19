"""Data-availability probe (read-only diagnostic).

Phase 0, option (c): profile what Alpaca actually returns for the V1
universe BEFORE the research stack commits to IEX assumptions. A weak
historical dataset can invalidate the entire edge study (false edge /
false no-edge / hidden gaps / distorted intraday behaviour), so this runs
first and is the gate.

Strictly READ-ONLY: only Alpaca historical GETs. No writes, no cache (the
parquet cache is sub-task 0.4), no production imports (separability), no
Strategy / no simulation. Safe to run repeatedly; reusable as a standing
diagnostic utility.

Run (where Alpaca creds exist in env):
    python -m research.data.availability_probe --years 2
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

V1_UNIVERSE = ["SPY", "QQQ", "AAPL", "MSFT"]


@dataclass(frozen=True, slots=True)
class ThinDay:
    """A trading day with abnormally few 1m bars, classified against the
    XNYS calendar so legitimate half-day early-closes are not confused
    with genuine data gaps."""
    date_iso: str
    bar_count: int
    is_half_day: bool


@dataclass(frozen=True, slots=True)
class SymbolProfile:
    symbol: str
    requested_start: datetime
    requested_end: datetime
    first_bar: datetime | None
    last_bar: datetime | None
    total_1m_bars: int
    distinct_days: int
    expected_trading_days: int
    coverage_pct: float
    low_volume_days: int          # days with < 50% of a full session's 1m bars
    sample_resample_ok: bool | None
    thin_days: tuple[ThinDay, ...]
    unexplained_thin: int          # thin days NOT explained by XNYS half-day
    note: str


def _client():
    key = os.environ.get("ALPACA_API_KEY")
    sec = os.environ.get("ALPACA_API_SECRET")
    if not key or not sec:
        raise SystemExit(
            "ALPACA_API_KEY / ALPACA_API_SECRET not in environment. Run where "
            "creds exist (VPS one-shot with --env-file, or export locally). "
            "This probe is read-only."
        )
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(key, sec)


def _xnys_schedule(start: datetime, end: datetime):
    import pandas_market_calendars as mcal
    cal = mcal.get_calendar("XNYS")
    return cal.schedule(start_date=start.date(), end_date=end.date())


def _xnys_trading_days(start: datetime, end: datetime) -> int:
    return len(_xnys_schedule(start, end))


def _xnys_half_days(start: datetime, end: datetime) -> set:
    """Set of dates whose XNYS session is shorter than the standard ~6.5 h
    (early-close days — July 3rd, day-after-Thanksgiving, Christmas Eve …)."""
    sched = _xnys_schedule(start, end)
    half: set = set()
    for ts, row in sched.iterrows():
        if (row["market_close"] - row["market_open"]).total_seconds() < 6 * 3600:
            d = ts.date() if hasattr(ts, "date") else ts
            half.add(d)
    return half


def _profile_symbol(client, symbol: str, start: datetime, end: datetime,
                    feed: str) -> SymbolProfile:
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    feed_enum = DataFeed.IEX if feed == "iex" else DataFeed.SIP
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=feed_enum,
        adjustment="all",
    )
    bars = client.get_stock_bars(req).data.get(symbol, [])
    if not bars:
        return SymbolProfile(
            symbol, start, end, None, None, 0, 0,
            _xnys_trading_days(start, end), 0.0, 0, None, (), 0,
            "NO DATA RETURNED for window — IEX likely lacks this history",
        )
    ts = [b.timestamp.astimezone(timezone.utc) for b in bars]
    first, last = min(ts), max(ts)
    days: dict = {}
    for t in ts:
        days[t.date()] = days.get(t.date(), 0) + 1
    distinct = len(days)
    expected = _xnys_trading_days(first, last)
    coverage = round(100.0 * distinct / expected, 2) if expected else 0.0
    # A full regular session ~ 390 1m bars; flag thin days, then classify
    # each one against the XNYS early-close schedule (legitimate half-days
    # are expected to be thin and must not count as "gaps").
    half_set = _xnys_half_days(first, last)
    thin_records: list[ThinDay] = []
    for d, c in sorted(days.items()):
        if c < 195:
            thin_records.append(ThinDay(d.isoformat(), c, d in half_set))
    unexplained = sum(1 for t in thin_records if not t.is_half_day)
    low_vol = len(thin_records)

    # Native-5m vs resample-from-1m sanity on the most recent full day.
    sample_ok: bool | None = None
    try:
        sample_ok = _resample_sanity(client, symbol, last, feed_enum)
    except Exception:  # noqa: BLE001 — diagnostic only
        sample_ok = None

    note = (
        "looks usable" if (coverage >= 99 and unexplained == 0 and sample_ok)
        else "SUSPECT — review coverage / unexplained thin days"
    )
    return SymbolProfile(
        symbol, start, end, first, last, len(ts), distinct, expected,
        coverage, low_vol, sample_ok, tuple(thin_records), unexplained, note,
    )


def _resample_sanity(client, symbol, around: datetime, feed_enum) -> bool:
    """Fetch native 5m for one recent day and compare to 1m resampled
    (left-labeled, ts=open) — the engine's bar convention."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    day_start = around.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    one = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
        start=day_start, end=day_end, feed=feed_enum, adjustment="all",
    )).data.get(symbol, [])
    five = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=day_start, end=day_end, feed=feed_enum, adjustment="all",
    )).data.get(symbol, [])
    if not one or not five:
        return False
    import pandas as pd
    s = pd.Series(
        {b.timestamp.astimezone(timezone.utc): b.close for b in one}
    ).sort_index()
    # Left-labeled 5-min resample (label=left, closed=left) == ts=open.
    res = s.resample("5min", label="left", closed="left").last().dropna()
    nat = {b.timestamp.astimezone(timezone.utc): b.close for b in five}
    common = [t for t in res.index if t in nat]
    if len(common) < 10:
        return False
    diffs = [abs(float(res[t]) - float(nat[t])) for t in common]
    return max(diffs) < 0.01  # cents tolerance


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Alpaca data-availability probe (read-only)")
    p.add_argument("--symbols", nargs="+", default=V1_UNIVERSE)
    p.add_argument("--years", type=float, default=2.0,
                   help="how far back to attempt (probes real history depth)")
    p.add_argument("--feed", choices=["iex", "sip"], default="iex")
    args = p.parse_args(argv)

    end = datetime.now(timezone.utc) - timedelta(minutes=20)  # avoid last-bar partial
    start = end - timedelta(days=int(args.years * 365))
    client = _client()

    print(f"# Data-availability probe  feed={args.feed}  adjustment=all")
    print(f"# requested window: {start.date()} .. {end.date()} "
          f"({args.years}y)\n")
    verdicts = []
    for sym in args.symbols:
        pr = _profile_symbol(client, sym, start, end, args.feed)
        verdicts.append(pr)
        print(f"[{pr.symbol}]")
        print(f"  returned span : {pr.first_bar} .. {pr.last_bar}")
        print(f"  1m bars       : {pr.total_1m_bars:,}")
        print(f"  days covered  : {pr.distinct_days} / {pr.expected_trading_days} "
              f"expected  ({pr.coverage_pct}%)")
        print(f"  thin days     : {pr.low_volume_days} "
              f"(unexplained: {pr.unexplained_thin})")
        if pr.thin_days:
            for td in pr.thin_days:
                tag = "half-day" if td.is_half_day else "UNEXPLAINED"
                print(f"    - {td.date_iso}  bars={td.bar_count:>3}  [{tag}]")
        print(f"  5m resample   : {pr.sample_resample_ok}")
        print(f"  verdict       : {pr.note}\n")

    usable = all(v.coverage_pct >= 99 and v.unexplained_thin == 0
                 and v.sample_resample_ok for v in verdicts
                 if v.total_1m_bars)
    any_empty = any(v.total_1m_bars == 0 for v in verdicts)
    print("=" * 60)
    if any_empty:
        print("OVERALL: IEX returned NO data for at least one symbol — "
              "history depth insufficient. Consider SIP / provider pivot.")
        return 2
    if usable:
        print("OVERALL: IEX looks USABLE for V1 — coverage >=99%, every thin "
              "day explained by the XNYS half-day calendar, native-5m == "
              "1m-resample within cents.")
        return 0
    print("OVERALL: IEX is SUSPECT — at least one symbol has unexplained "
          "thin days or sub-99% coverage. Investigate before committing.")
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
