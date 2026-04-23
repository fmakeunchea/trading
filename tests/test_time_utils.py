"""Tests for strategy.time_utils.

The module is small but critical — a session-window bug could let the bot
trade into the close or through a blackout. Test every boundary.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from strategy.time_utils import (
    SessionClock,
    ensure_utc,
    is_stale,
    now_utc,
    parse_hhmm_utc,
    seconds_between,
    today_utc,
)


UTC = timezone.utc


def _dt(h: int, m: int, *, day: int = 23) -> datetime:
    return datetime(2026, 4, day, h, m, 0, tzinfo=UTC)


def _clock() -> SessionClock:
    # Session 13:35 UTC → 19:45 UTC (9:35 ET → 15:45 ET in winter)
    return SessionClock(
        session_start=time(13, 35, tzinfo=UTC),
        session_end=time(19, 45, tzinfo=UTC),
        block_first_minutes=5,
        block_last_minutes=10,
        flat_before_close_minutes=5,
    )


# ---------------------------------------------------------------------------
# ensure_utc / now_utc / parse_hhmm_utc
# ---------------------------------------------------------------------------


def test_now_utc_is_tzaware() -> None:
    assert now_utc().tzinfo is UTC


def test_ensure_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_utc(datetime(2026, 4, 23, 14, 0, 0))


def test_ensure_utc_converts_non_utc() -> None:
    # 14:00 in a +02:00 zone is 12:00 UTC
    from datetime import timezone as tz
    plus2 = tz(timedelta(hours=2))
    out = ensure_utc(datetime(2026, 4, 23, 14, 0, 0, tzinfo=plus2))
    assert out.tzinfo is UTC
    assert out.hour == 12 and out.minute == 0


def test_parse_hhmm_utc_valid() -> None:
    t = parse_hhmm_utc("13:35")
    assert (t.hour, t.minute) == (13, 35)
    assert t.tzinfo is UTC


@pytest.mark.parametrize("bad", ["24:00", "-1:00", "12:60", "xx:yy"])
def test_parse_hhmm_utc_invalid(bad: str) -> None:
    with pytest.raises(Exception):
        parse_hhmm_utc(bad)


# ---------------------------------------------------------------------------
# SessionClock construction invariants
# ---------------------------------------------------------------------------


def test_session_clock_rejects_inverted_window() -> None:
    with pytest.raises(ValueError):
        SessionClock(
            session_start=time(19, 45, tzinfo=UTC),
            session_end=time(13, 35, tzinfo=UTC),
            block_first_minutes=5,
            block_last_minutes=10,
            flat_before_close_minutes=5,
        )


def test_session_clock_rejects_equal_window() -> None:
    with pytest.raises(ValueError):
        SessionClock(
            session_start=time(13, 35, tzinfo=UTC),
            session_end=time(13, 35, tzinfo=UTC),
            block_first_minutes=0,
            block_last_minutes=0,
            flat_before_close_minutes=0,
        )


def test_session_clock_rejects_oversize_blackouts() -> None:
    # Session is 370 min; blackouts totalling 400 min cannot fit.
    with pytest.raises(ValueError):
        SessionClock(
            session_start=time(13, 35, tzinfo=UTC),
            session_end=time(19, 45, tzinfo=UTC),
            block_first_minutes=200,
            block_last_minutes=200,
            flat_before_close_minutes=5,
        )


def test_session_clock_rejects_oversize_flatten() -> None:
    with pytest.raises(ValueError):
        SessionClock(
            session_start=time(13, 35, tzinfo=UTC),
            session_end=time(19, 45, tzinfo=UTC),
            block_first_minutes=0,
            block_last_minutes=0,
            flat_before_close_minutes=1000,
        )


def test_session_clock_rejects_negative_blackout() -> None:
    with pytest.raises(ValueError):
        SessionClock(
            session_start=time(13, 35, tzinfo=UTC),
            session_end=time(19, 45, tzinfo=UTC),
            block_first_minutes=-1,
            block_last_minutes=0,
            flat_before_close_minutes=0,
        )


# ---------------------------------------------------------------------------
# is_within_session
# ---------------------------------------------------------------------------


def test_within_session_boundaries() -> None:
    c = _clock()
    assert c.is_within_session(_dt(13, 35)) is True   # exact open = inside
    assert c.is_within_session(_dt(13, 34)) is False  # 1 min before open
    assert c.is_within_session(_dt(19, 44)) is True
    assert c.is_within_session(_dt(19, 45)) is False  # close = outside
    assert c.is_within_session(_dt(8, 0)) is False
    assert c.is_within_session(_dt(22, 0)) is False


def test_within_session_rejects_naive_now() -> None:
    c = _clock()
    with pytest.raises(ValueError):
        c.is_within_session(datetime(2026, 4, 23, 14, 0))


# ---------------------------------------------------------------------------
# in_blackout
# ---------------------------------------------------------------------------


def test_opening_blackout() -> None:
    c = _clock()
    assert c.in_blackout(_dt(13, 35)) is True   # start of opening blackout
    assert c.in_blackout(_dt(13, 39)) is True
    assert c.in_blackout(_dt(13, 40)) is False  # exactly at open + 5min


def test_closing_blackout() -> None:
    c = _clock()
    # Close = 19:45, block_last = 10 → [19:35, 19:45)
    assert c.in_blackout(_dt(19, 34)) is False
    assert c.in_blackout(_dt(19, 35)) is True
    assert c.in_blackout(_dt(19, 44)) is True
    assert c.in_blackout(_dt(19, 45)) is False   # after session end


def test_blackout_outside_session_is_false() -> None:
    c = _clock()
    assert c.in_blackout(_dt(8, 0)) is False
    assert c.in_blackout(_dt(22, 0)) is False


# ---------------------------------------------------------------------------
# flatten_deadline
# ---------------------------------------------------------------------------


def test_flatten_deadline_today() -> None:
    c = _clock()
    now = _dt(14, 0)
    d = c.flatten_deadline(now)
    # 19:45 - 5 = 19:40 UTC today
    assert d.date() == now.date()
    assert (d.hour, d.minute) == (19, 40)
    assert d.tzinfo is UTC


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


def test_is_stale_boundary() -> None:
    now = _dt(14, 0)
    # 30s stale threshold
    assert is_stale(now - timedelta(seconds=31), now, 30) is True
    assert is_stale(now - timedelta(seconds=30), now, 30) is False  # exactly 30 = fresh
    assert is_stale(now - timedelta(seconds=1), now, 30) is False


def test_is_stale_rejects_naive() -> None:
    now = _dt(14, 0)
    with pytest.raises(ValueError):
        is_stale(datetime(2026, 4, 23, 14, 0, 0), now, 30)


# ---------------------------------------------------------------------------
# DST: session times in config are already UTC, so the clock itself is
# DST-agnostic. This test documents that by crossing a US DST boundary.
# ---------------------------------------------------------------------------


def test_dst_transition_is_transparent_in_utc() -> None:
    # Mar 8 2026 is the US "spring forward" date. In UTC, nothing jumps.
    c = _clock()
    pre = datetime(2026, 3, 7, 13, 35, tzinfo=UTC)
    post = datetime(2026, 3, 9, 13, 35, tzinfo=UTC)
    assert c.is_within_session(pre) is True
    assert c.is_within_session(post) is True


# ---------------------------------------------------------------------------
# Trivial helpers
# ---------------------------------------------------------------------------


def test_seconds_between_and_today_utc() -> None:
    a = _dt(13, 0)
    b = _dt(14, 0)
    assert seconds_between(a, b) == 3600.0
    assert today_utc(a) == a.date()
