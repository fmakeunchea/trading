"""1-minute bar resampler + anti-leak ``safe_bars`` accessor (Phase 0.3).

Two layers:

1. :func:`resample` — pure aggregation of 1-minute OHLCV bars into the
   target timeframe. **Left-labeled, ``ts = OPEN``** to match Alpaca's
   bar convention and the engine's invariant (a bar with ``ts=T`` is
   known to the engine at ``T + tf_minutes``, i.e. at its close). Bars
   are grouped per trading-session date so a bucket cannot span a
   session/day boundary. Out-of-session bars (pre/post-market that may
   sneak through a fetcher) are filtered defensively.

2. :func:`safe_bars` — **THE anti-leak accessor.** The single chokepoint
   for study-time bar access. By construction it cannot return a bar
   whose ``close_ts > as_of``; passing a fetcher that returns future
   bars cannot leak — they are clipped, and a post-condition raises
   :class:`LookaheadError` if anything slipped through.

These are the foundational research safety boundary. The 0.6 test suite
will weaponise this with a future-bar-injection test that asserts safe
output even with hostile input.

Lazy imports of pandas / calendar preserve separability.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

# Public so studies can write `except LookaheadError`.
class LookaheadError(Exception):
    """Raised if a future bar (close_ts > as_of) slips through the
    anti-leak accessor. Should never trigger in well-behaved code; the
    accessor clips defensively first, then verifies."""


_OHLCV_AGG = {
    "open":   "first",
    "high":   "max",
    "low":    "min",
    "close":  "last",
    "volume": "sum",
}
_REQUIRED_COLS = ("open", "high", "low", "close", "volume")


# --- internal helpers -----------------------------------------------------

def _empty_like(df_1m) -> Any:
    import pandas as pd
    idx = pd.DatetimeIndex([], tz="UTC", name="ts")
    return pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in _REQUIRED_COLS[:-1]}
        | {"volume": pd.Series(dtype="int64")},
        index=idx,
    )


def _assert_input_invariants(df_1m) -> None:
    if df_1m.empty:
        return
    if df_1m.index.tz is None or str(df_1m.index.tz) != "UTC":
        raise ValueError("input ts index must be UTC tz-aware")
    if not df_1m.index.is_monotonic_increasing:
        raise ValueError("input ts must be ascending")
    if not df_1m.index.is_unique:
        raise ValueError("input ts must be unique")
    for c in _REQUIRED_COLS:
        if c not in df_1m.columns:
            raise ValueError(f"input missing required column: {c}")


# --- public: resample -----------------------------------------------------

def resample(df_1m, tf_minutes: int):
    """Resample 1-min OHLCV to ``tf_minutes`` bars (left-labeled,
    ts=open, per-session, defensive against out-of-session bars).

    Returns a DataFrame with the same schema as the input (UTC ts index,
    OHLCV columns; ascending, monotonic, unique). Empty input → empty
    output with correct schema.
    """
    import pandas as pd
    from research.data import calendar as cal  # lazy

    if tf_minutes < 1:
        raise ValueError("tf_minutes must be >= 1")
    _assert_input_invariants(df_1m)
    if df_1m.empty:
        return _empty_like(df_1m)

    pieces: list = []
    for d, group in df_1m.groupby(df_1m.index.date):
        bounds = cal.session_bounds(d)
        if bounds is None:
            continue  # not a trading day — defensive drop
        open_utc, close_utc = bounds
        in_session = group[(group.index >= open_utc) & (group.index < close_utc)]
        if in_session.empty:
            continue
        # Anchor bucketing on the session open so half-days, DST, and
        # arbitrary tf_minutes all align cleanly to session boundaries.
        #
        # ``dropna(subset=ohlc, how="any")`` — NOT ``how="all"`` — is the
        # correct drop. An empty source bucket yields ``[NaN, NaN, NaN,
        # NaN, 0]`` (the ``"sum"`` of empty is 0, NOT NaN), so ``how="all"``
        # leaves the row alive and downstream consumers see a phantom bar
        # with NaN OHLC. ``Decimal(str(nan))`` is ``Decimal('NaN')``,
        # which (unlike float NaN) raises ``InvalidOperation`` on
        # arithmetic — crashing ATR / true_range on first real-data
        # contact in illiquid 5-minute windows. Surfaced by Phase-4
        # spike (2026-05-20).
        bucketed = (
            in_session
            .resample(f"{tf_minutes}min", label="left", closed="left",
                      origin=open_utc)
            .agg(_OHLCV_AGG)
            .dropna(subset=["open", "high", "low", "close"], how="any")
        )
        if not bucketed.empty:
            pieces.append(bucketed)

    if not pieces:
        return _empty_like(df_1m)

    out = pd.concat(pieces).sort_index()
    out.index.name = "ts"
    # Cast volume back to int64 (resample produces float64 after dropna).
    out["volume"] = out["volume"].astype("int64")
    return out


# --- public: anti-leak safe accessor --------------------------------------

def safe_bars(
    symbol: str,
    tf_minutes: int,
    as_of: datetime,
    *,
    lookback_window: timedelta,
    fetch_1m: Callable[..., Any] | None = None,
    feed: str = "iex",
    adjustment: str = "all",
    client: Any | None = None,
):
    """Return resampled bars with ``close_ts <= as_of`` — **anti-leak by
    construction**.

    The single chokepoint for study-time bar access. Fetches 1-minute
    bars over ``[as_of - lookback_window, as_of]`` (default fetcher:
    :func:`research.data.alpaca_source.fetch_1m_bars`), resamples to
    ``tf_minutes``, and clips strictly to bars whose CLOSE timestamp
    (``ts + tf_minutes``) is at-or-before ``as_of``.

    A post-condition raises :class:`LookaheadError` if any bar with
    ``close_ts > as_of`` made it into the output (should never happen;
    the assertion exists so a future bug cannot silently corrupt a
    study).

    ``fetch_1m`` is injectable — the SimulatedBroker (Phase 1) and tests
    supply a deterministic in-memory fetcher; production studies use the
    Alpaca source.
    """
    import pandas as pd

    if as_of.tzinfo is None:
        raise ValueError("as_of must be tz-aware (UTC)")
    if as_of.utcoffset() != timedelta(0):
        raise ValueError("as_of must be UTC")
    if fetch_1m is None:
        from research.data.alpaca_source import fetch_1m_bars
        fetch_1m = fetch_1m_bars

    start = as_of - lookback_window
    df_1m = fetch_1m(symbol, start, as_of, feed=feed, adjustment=adjustment,
                    client=client)
    resampled = resample(df_1m, tf_minutes)

    if resampled.empty:
        out = resampled
    else:
        tf = pd.Timedelta(minutes=tf_minutes)
        close_ts = resampled.index + tf
        # Strict clip: include only bars whose CLOSE is at-or-before as_of.
        out = resampled[close_ts <= as_of]
        # Post-condition: assert by construction. If this ever raises,
        # there is a real bug — do not catch it in a study.
        if not out.empty:
            last_close = out.index[-1] + tf
            if last_close > as_of:
                raise LookaheadError(
                    f"safe_bars produced a future bar: last close_ts="
                    f"{last_close.isoformat()} > as_of={as_of.isoformat()}"
                )

    out.attrs.update(
        symbol=symbol, tf_minutes=tf_minutes, as_of=as_of,
        lookback_window=lookback_window, feed=feed, adjustment=adjustment,
    )
    return out
