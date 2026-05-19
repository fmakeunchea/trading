"""Offline tests for research.data.calendar.

Most cases require ``pandas_market_calendars`` (research-only dep) and are
gated by importorskip — they pass in ``.venv-research``, skip cleanly in
the engine ``.venv``. Separability check runs unconditionally.
"""
from __future__ import annotations

import importlib.util
from datetime import date, timedelta

import pytest

from research.data import calendar as cal


# pmc-bound tests skip individually (module-level importorskip would skip
# the entire file incl. the unconditional separability test below).
_HAS_PMC = importlib.util.find_spec("pandas_market_calendars") is not None
requires_pmc = pytest.mark.skipif(
    not _HAS_PMC, reason="pandas_market_calendars not installed (research-only dep)",
)


def test_module_has_no_production_imports() -> None:
    """Separability — never pull in the live engine. Runs unconditionally."""
    import inspect
    s = inspect.getsource(cal)
    for forbidden in ("strategy.broker", "strategy.strategy",
                      "strategy.recovery", "run_strategy", "autoflow"):
        assert forbidden not in s


# --- pmc-gated cases ------------------------------------------------------

@requires_pmc
def test_christmas_2026_is_not_a_trading_day() -> None:
    # 2026-12-25 is a Friday → full NYSE holiday.
    assert cal.is_trading_day(date(2026, 12, 25)) is False
    days = cal.trading_days(date(2026, 12, 21), date(2026, 12, 31))
    assert date(2026, 12, 25) not in days


@requires_pmc
def test_known_2026_half_day_classified_correctly() -> None:
    # Black Friday 2026 (day after Thanksgiving Nov 26) — established
    # early-close on XNYS.
    d = date(2026, 11, 27)
    assert cal.is_trading_day(d) is True
    assert cal.is_half_day(d) is True
    assert cal.expected_1m_bars(d) == 210      # 9:30 → 13:00 ET = 3.5h


@requires_pmc
def test_regular_monday_has_390_1m_bars() -> None:
    # 2026-06-01 is a Monday, not adjacent to any holiday.
    d = date(2026, 6, 1)
    assert cal.is_trading_day(d) is True
    assert cal.is_half_day(d) is False
    assert cal.expected_1m_bars(d) == 390      # 9:30 → 16:00 ET = 6.5h


@requires_pmc
def test_session_bounds_are_utc_and_correct_length() -> None:
    d = date(2026, 6, 1)
    bounds = cal.session_bounds(d)
    assert bounds is not None
    open_utc, close_utc = bounds
    assert str(open_utc.tzinfo) == "UTC"
    assert str(close_utc.tzinfo) == "UTC"
    assert (close_utc - open_utc).total_seconds() == 6.5 * 3600


@requires_pmc
def test_session_bounds_none_on_weekend_and_holiday() -> None:
    assert cal.session_bounds(date(2026, 6, 6)) is None    # Saturday
    assert cal.session_bounds(date(2026, 12, 25)) is None  # Christmas
    assert cal.expected_1m_bars(date(2026, 6, 6)) == 0


@requires_pmc
def test_trading_days_excludes_weekends_and_holidays() -> None:
    days = cal.trading_days(date(2026, 1, 1), date(2026, 1, 31))
    # Holidays
    assert date(2026, 1, 1) not in days   # New Year's Day (Thu)
    assert date(2026, 1, 19) not in days  # MLK Day (Mon)
    # Weekends
    for d in (date(2026, 1, 3), date(2026, 1, 4),
              date(2026, 1, 10), date(2026, 1, 11)):
        assert d not in days
    # And no weekday returned is itself a weekend.
    assert all(d.weekday() < 5 for d in days)
    # Ascending + unique
    assert days == sorted(set(days))


@requires_pmc
def test_half_days_set_contains_known_early_close() -> None:
    hd = cal.half_days(date(2026, 11, 1), date(2026, 11, 30))
    assert date(2026, 11, 27) in hd


@requires_pmc
def test_calendar_object_is_memoised() -> None:
    # Same calendar instance returned across calls (lru_cache).
    assert cal._calendar() is cal._calendar()
