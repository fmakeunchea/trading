"""XNYS calendar — typed wrapper around pandas_market_calendars (Phase 0.2).

The canonical research-stack source of truth for: trading days, half-day
early-closes, session bounds, and the per-day expected 1-minute bar count.
Used by 0.3 (resampler — verify no bar crosses a session boundary; gap
detection vs ``expected_1m_bars``) and by the anti-leak accessor (0.3 +
0.6) for safe-window arithmetic.

Lazy import of ``pandas_market_calendars`` — this module is importable
without the research deps installed (separability), and only touches the
SDK on first call. Calendar object is memoised.

Note: ``research/data/availability_probe.py`` contains intentional
duplicates of these helpers. The probe is a standalone diagnostic; it
predates this module and is left unchanged to preserve its
stand-alone-ness. New code uses calendar.py as the canonical source.
"""
from __future__ import annotations

import functools
from datetime import date, datetime, timezone
from typing import Any

XNYS = "XNYS"

# Full regular NYSE session is exactly 6h 30m. Anything strictly shorter
# is treated as a half-day early-close (matches XNYS reality + the probe).
_FULL_SESSION_SECONDS = 6 * 3600 + 30 * 60
_HALF_DAY_THRESHOLD_S = 6 * 3600


# --- internals -------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _calendar():
    import pandas_market_calendars as mcal  # lazy
    return mcal.get_calendar(XNYS)


def _as_date(d: date | datetime) -> date:
    if isinstance(d, datetime):
        return d.date()
    return d


def _schedule(start: date | datetime, end: date | datetime):
    return _calendar().schedule(start_date=_as_date(start),
                                end_date=_as_date(end))


def _row_date(ts: Any) -> date:
    return ts.date() if hasattr(ts, "date") else ts


# --- public API ------------------------------------------------------------

def trading_days(start: date | datetime, end: date | datetime) -> list[date]:
    """Trading dates in ``[start, end]`` inclusive, ascending."""
    return [_row_date(ts) for ts in _schedule(start, end).index]


def is_trading_day(d: date | datetime) -> bool:
    d = _as_date(d)
    return d in trading_days(d, d)


def session_bounds(
    d: date | datetime,
) -> tuple[datetime, datetime] | None:
    """``(open_utc, close_utc)`` for the trading day, else ``None``.

    Both are tz-aware UTC; converted from the calendar's native tz so
    consumers never have to reason about DST or local time.
    """
    d = _as_date(d)
    sched = _schedule(d, d)
    if len(sched) == 0:
        return None
    row = sched.iloc[0]
    open_utc = row["market_open"].tz_convert("UTC").to_pydatetime()
    close_utc = row["market_close"].tz_convert("UTC").to_pydatetime()
    return open_utc, close_utc


def is_half_day(d: date | datetime) -> bool:
    """True iff ``d`` is a trading day with a session strictly shorter
    than 6h (the XNYS half-day early-close pattern)."""
    bounds = session_bounds(d)
    if bounds is None:
        return False
    o, c = bounds
    return (c - o).total_seconds() < _HALF_DAY_THRESHOLD_S


def half_days(start: date | datetime, end: date | datetime) -> set[date]:
    sched = _schedule(start, end)
    out: set[date] = set()
    for ts, row in sched.iterrows():
        secs = (row["market_close"] - row["market_open"]).total_seconds()
        if secs < _HALF_DAY_THRESHOLD_S:
            out.add(_row_date(ts))
    return out


def expected_1m_bars(d: date | datetime) -> int:
    """Number of 1-minute bars expected in the session (0 if non-trading).

    Used by the resampler / cache as the per-day completeness target.
    """
    bounds = session_bounds(d)
    if bounds is None:
        return 0
    o, c = bounds
    return int((c - o).total_seconds() // 60)
