"""UTC clock and session-window helpers.

Design notes:

* Internally the bot operates exclusively in UTC. Config accepts labels in
  ``America/New_York`` which are converted to UTC once at load.
* :func:`now_utc` is the *sole* wall-clock source. Tests monkeypatch this
  one symbol (or use ``freezegun``) — every other module reads time by
  passing ``now`` as an argument, which keeps ``risk.py`` pure.
* :class:`SessionClock` does not fetch the Alpaca market calendar here;
  the broker wrapper owns that I/O and feeds ``is_trading_day`` via its
  own calendar cache. This module only does arithmetic.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone


def now_utc() -> datetime:
    """Single wall-clock source for the package.

    Tests override this via ``monkeypatch`` or ``freezegun``.
    """
    return datetime.now(timezone.utc)


def ensure_utc(dt: datetime) -> datetime:
    """Return ``dt`` as a UTC-tzaware datetime.

    Naive datetimes are rejected — we do not guess the caller's intent.
    """
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.astimezone(timezone.utc)


def parse_hhmm_utc(s: str) -> time:
    """Parse ``"HH:MM"`` as a naive UTC time-of-day.

    Used by config to normalise session_start/end into UTC time objects.
    The caller is expected to have already converted any non-UTC labels.
    """
    hh, mm = s.split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad HH:MM value: {s!r}")
    return time(h, m, 0, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class SessionClock:
    """Encapsulates session-window arithmetic.

    ``session_start`` and ``session_end`` are UTC time-of-day.
    ``block_first_minutes`` / ``block_last_minutes`` express the opening
    and closing blackouts; both must fit strictly inside the session.
    ``flat_before_close_minutes`` is how far before ``session_end`` the
    bot must be flat.
    """

    session_start: time
    session_end: time
    block_first_minutes: int
    block_last_minutes: int
    flat_before_close_minutes: int

    def __post_init__(self) -> None:
        if self.session_start >= self.session_end:
            raise ValueError("session_start must be strictly before session_end")
        session_minutes = (
            self.session_end.hour * 60 + self.session_end.minute
        ) - (self.session_start.hour * 60 + self.session_start.minute)
        if self.block_first_minutes < 0 or self.block_last_minutes < 0:
            raise ValueError("blackouts must be non-negative")
        if self.flat_before_close_minutes < 0:
            raise ValueError("flat_before_close_minutes must be non-negative")
        if (
            self.block_first_minutes + self.block_last_minutes
            >= session_minutes
        ):
            raise ValueError("blackouts cannot cover the whole session")
        if self.flat_before_close_minutes >= session_minutes:
            raise ValueError("flat_before_close_minutes exceeds session length")

    def is_within_session(self, now: datetime) -> bool:
        """True iff ``now`` is inside [session_start, session_end) on the
        same UTC day.

        The check is intentionally half-open at the end so the exact
        session-close minute counts as outside — matching the
        flat-by-close invariant."""
        n = ensure_utc(now).timetz()
        # Strip date; compare time-of-day only.
        n_t = n.replace(tzinfo=timezone.utc)
        return self.session_start <= n_t < self.session_end

    def in_blackout(self, now: datetime) -> bool:
        """True iff ``now`` is inside the opening or closing blackout
        window.

        Opening blackout: [session_start, session_start + block_first).
        Closing blackout: [session_end - block_last, session_end).
        Outside the session this returns ``False`` — the session-window
        check is the authoritative out-of-session gate.
        """
        if not self.is_within_session(now):
            return False
        n_t = ensure_utc(now).timetz().replace(tzinfo=timezone.utc)
        open_end = _add_minutes(self.session_start, self.block_first_minutes)
        close_start = _sub_minutes(self.session_end, self.block_last_minutes)
        return (self.session_start <= n_t < open_end) or (
            close_start <= n_t < self.session_end
        )

    def flatten_deadline(self, now: datetime) -> datetime:
        """Return the UTC datetime by which the bot must be flat *today*.

        If ``now`` is before today's flatten deadline, return today's.
        Otherwise return the next trading day's flatten deadline is NOT
        computed here (the broker calendar owns weekends/holidays). The
        caller should treat the returned value as the earliest upcoming
        flatten if it is in the past.
        """
        now = ensure_utc(now)
        deadline_t = _sub_minutes(self.session_end, self.flat_before_close_minutes)
        return datetime.combine(now.date(), deadline_t, tzinfo=timezone.utc)


def _add_minutes(t: time, minutes: int) -> time:
    total = t.hour * 60 + t.minute + minutes
    if not (0 <= total < 24 * 60):
        raise ValueError("session time overflow")
    return time(total // 60, total % 60, tzinfo=timezone.utc)


def _sub_minutes(t: time, minutes: int) -> time:
    return _add_minutes(t, -minutes)


def is_stale(latest_bar_ts: datetime, now: datetime, max_age_s: int) -> bool:
    """True iff the latest bar is older than ``max_age_s`` seconds."""
    latest_bar_ts = ensure_utc(latest_bar_ts)
    now = ensure_utc(now)
    return (now - latest_bar_ts) > timedelta(seconds=max_age_s)


def seconds_between(earlier: datetime, later: datetime) -> float:
    earlier = ensure_utc(earlier)
    later = ensure_utc(later)
    return (later - earlier).total_seconds()


def today_utc(now: datetime) -> date:
    return ensure_utc(now).date()
