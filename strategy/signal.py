"""Pure, deterministic signal generation.

Design notes:

* This module has **no I/O**: no broker access, no file reads, no clock
  reads, no logging side effects. The only outputs are the return values.
* All arithmetic is ``Decimal``. No float creeps in, even in indicators
  — an EMA that loses a cent on each step because of float rounding is
  a silent strategy-drift bug.
* Indicators and the decision function live together because they are
  only used here. Splitting them would create API surface without
  reducing complexity.
* ``evaluate_signal`` is a total function: it always returns a decision
  (``Signal`` or ``None``) — no exceptions for ordinary "not enough
  data" or "regime filtered" cases. Exceptions are reserved for
  programmer errors (e.g. empty bar list handed to an indicator).

The strategy shape:

* Trend filter on ``trend_tf`` bars: close > EMA(trend_ema) AND EMA slope
  over the last 10 bars > 0.
* Confirmation on ``confirm_tf``: EMA(ema_fast) > EMA(ema_slow) AND
  close > EMA(ema_fast).
* Entry on ``entry_tf``: breakout of prior N-bar high, close > EMA(ema_fast),
  ADX >= adx_min, ATR within a healthy band.
* Expected move bps = (atr_target_mult * ATR) / close * 10_000.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from strategy.config import StrategyConfig
from strategy.dto import Bar, OrderSide, Signal


# ---------------------------------------------------------------------------
# BarFrame: the inputs to a single evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BarFrame:
    """Bars at the three timeframes the signal inspects.

    Bars must be sorted ascending by ``ts`` and contiguous within each
    timeframe. The caller (:mod:`strategy.strategy`) owns that invariant.
    """

    symbol: str
    entry_bars: tuple[Bar, ...]
    confirm_bars: tuple[Bar, ...]
    trend_bars: tuple[Bar, ...]

    def __post_init__(self) -> None:
        for label, bars in (
            ("entry_bars", self.entry_bars),
            ("confirm_bars", self.confirm_bars),
            ("trend_bars", self.trend_bars),
        ):
            for b in bars:
                if b.symbol != self.symbol:
                    raise ValueError(f"{label} contains wrong symbol: {b.symbol!r}")
            ts_list = [b.ts for b in bars]
            if ts_list != sorted(ts_list):
                raise ValueError(f"{label} must be sorted ascending by ts")


# ---------------------------------------------------------------------------
# Indicators (pure Decimal)
# ---------------------------------------------------------------------------


def ema(values: Sequence[Decimal], period: int) -> list[Decimal]:
    """Exponential moving average.

    Seed = simple mean of the first ``period`` values. Subsequent values
    use ``k = 2 / (period + 1)``.

    Returns a list of the same length as ``values`` with ``None``-less
    numerics; the first ``period - 1`` outputs are ``Decimal(0)`` and
    callers must not read them (the ``min_bars_*_tf`` config invariants
    ensure this).
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if len(values) < period:
        raise ValueError(f"need at least {period} values for EMA({period})")
    out: list[Decimal] = [Decimal(0)] * (period - 1)
    seed = sum(values[:period], Decimal(0)) / Decimal(period)
    out.append(seed)
    k = Decimal(2) / Decimal(period + 1)
    for v in values[period:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def true_range(bars: Sequence[Bar]) -> list[Decimal]:
    """Wilder's True Range series.

    TR_0 = high_0 - low_0; TR_i = max(high_i-low_i, |high_i-close_{i-1}|,
    |low_i-close_{i-1}|).
    """
    if not bars:
        raise ValueError("true_range requires at least one bar")
    tr: list[Decimal] = [bars[0].high - bars[0].low]
    for i in range(1, len(bars)):
        hi, lo = bars[i].high, bars[i].low
        prev_close = bars[i - 1].close
        tr.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
    return tr


def atr(bars: Sequence[Bar], period: int) -> list[Decimal]:
    """Wilder-smoothed ATR.

    Seed = simple mean of the first ``period`` TRs. Thereafter:
    ATR_i = ATR_{i-1} + (TR_i - ATR_{i-1}) / period.
    """
    if period <= 1:
        raise ValueError("ATR period must be > 1")
    tr = true_range(bars)
    if len(tr) < period:
        raise ValueError(f"need at least {period} bars for ATR({period})")
    out: list[Decimal] = [Decimal(0)] * (period - 1)
    seed = sum(tr[:period], Decimal(0)) / Decimal(period)
    out.append(seed)
    for i in range(period, len(tr)):
        out.append(out[-1] + (tr[i] - out[-1]) / Decimal(period))
    return out


def adx(bars: Sequence[Bar], period: int) -> list[Decimal]:
    """Wilder's ADX.

    Computed from +DM / -DM / TR, each Wilder-smoothed. The returned
    list is of length ``len(bars)``; values before index ``2*period - 1``
    are ``Decimal(0)`` and must not be relied on.
    """
    if period <= 1:
        raise ValueError("ADX period must be > 1")
    n = len(bars)
    if n < 2 * period:
        raise ValueError(f"need at least {2 * period} bars for ADX({period})")

    plus_dm: list[Decimal] = [Decimal(0)]
    minus_dm: list[Decimal] = [Decimal(0)]
    for i in range(1, n):
        up = bars[i].high - bars[i - 1].high
        dn = bars[i - 1].low - bars[i].low
        plus_dm.append(up if up > dn and up > 0 else Decimal(0))
        minus_dm.append(dn if dn > up and dn > 0 else Decimal(0))

    tr = true_range(bars)

    def wilder(series: Sequence[Decimal]) -> list[Decimal]:
        out: list[Decimal] = [Decimal(0)] * (period - 1)
        out.append(sum(series[1:period + 1], Decimal(0)))  # skip the 0 at index 0
        for i in range(period + 1, len(series)):
            out.append(out[-1] - (out[-1] / Decimal(period)) + series[i])
        # Pad to full length so indexing lines up with input.
        while len(out) < len(series):
            out.append(out[-1] if out else Decimal(0))
        return out

    atr_s = wilder(tr)
    plus_di_s = wilder(plus_dm)
    minus_di_s = wilder(minus_dm)

    dx: list[Decimal] = [Decimal(0)] * n
    for i in range(period, n):
        if atr_s[i] == 0:
            continue
        plus_di = Decimal(100) * plus_di_s[i] / atr_s[i]
        minus_di = Decimal(100) * minus_di_s[i] / atr_s[i]
        s = plus_di + minus_di
        if s == 0:
            dx[i] = Decimal(0)
        else:
            dx[i] = Decimal(100) * abs(plus_di - minus_di) / s

    out: list[Decimal] = [Decimal(0)] * n
    start = 2 * period - 1
    if start < n:
        seed = sum(dx[period:2 * period], Decimal(0)) / Decimal(period)
        out[start] = seed
        for i in range(start + 1, n):
            out[i] = (out[i - 1] * Decimal(period - 1) + dx[i]) / Decimal(period)
    return out


def slope(values: Sequence[Decimal], window: int) -> Decimal:
    """Simple slope over the last ``window`` values: (last - first) / window.

    Not a least-squares slope — we only care about the direction and a
    rough magnitude for regime checks.
    """
    if window <= 0:
        raise ValueError("slope window must be > 0")
    if len(values) < window:
        raise ValueError("not enough values for slope window")
    tail = values[-window:]
    return (tail[-1] - tail[0]) / Decimal(window)


# ---------------------------------------------------------------------------
# Signal decision
# ---------------------------------------------------------------------------


SIGNAL_OK = "breakout_with_trend_and_confirmation"


def evaluate_signal(
    frame: BarFrame, cfg: StrategyConfig
) -> tuple[Signal | None, str]:
    """Decide whether to enter on ``frame.symbol`` right now.

    Returns ``(Signal, "breakout_with_trend_and_confirmation")`` on a BUY
    setup, or ``(None, <reason>)`` otherwise. The reason is one of the
    short snake_case strings below; useful for paper-mode observability.

    Pure — no exceptions for ordinary cases. Trading behavior unchanged
    vs the original: callers should treat ``result[0] is None`` exactly
    as before.
    """
    if (
        len(frame.entry_bars) < cfg.min_bars_entry_tf
        or len(frame.confirm_bars) < cfg.min_bars_confirm_tf
        or len(frame.trend_bars) < cfg.min_bars_trend_tf
    ):
        return None, "insufficient_bars"

    # --- Trend filter on ``trend_tf`` ---------------------------------
    trend_closes = [b.close for b in frame.trend_bars]
    trend_ema_series = ema(trend_closes, cfg.trend_ema)
    last_trend_close = frame.trend_bars[-1].close
    last_trend_ema = trend_ema_series[-1]
    trend_slope = slope(trend_ema_series[-11:], window=10)
    trend_up = last_trend_close > last_trend_ema and trend_slope > Decimal(0)
    if not trend_up:
        return None, "trend_not_up"

    # --- Confirmation on ``confirm_tf`` -------------------------------
    confirm_closes = [b.close for b in frame.confirm_bars]
    confirm_fast = ema(confirm_closes, cfg.ema_fast)
    confirm_slow = ema(confirm_closes, cfg.ema_slow)
    if (
        confirm_fast[-1] <= confirm_slow[-1]
        or frame.confirm_bars[-1].close <= confirm_fast[-1]
    ):
        return None, "confirmation_not_aligned"

    # --- Entry on ``entry_tf`` ---------------------------------------
    entry_bars = frame.entry_bars
    entry_closes = [b.close for b in entry_bars]
    entry_fast = ema(entry_closes, cfg.ema_fast)
    atr_series = atr(entry_bars, cfg.atr_period)
    adx_series = adx(entry_bars, cfg.adx_period)

    last_bar = entry_bars[-1]
    prior_highs = [b.high for b in entry_bars[-(cfg.breakout_lookback + 1):-1]]
    breakout_level = max(prior_highs) if prior_highs else last_bar.high

    cur_atr = atr_series[-1]
    cur_adx = adx_series[-1]

    # Regime checks:
    # 1) trending enough
    if cur_adx < cfg.adx_min:
        return None, "adx_too_low"
    # 2) volatility alive (ATR > 0.05% of price) and not extreme (< 3% of price)
    if cur_atr <= 0 or last_bar.close <= 0:
        return None, "atr_or_price_nonpositive"
    atr_pct = cur_atr / last_bar.close
    if atr_pct < Decimal("0.0005") or atr_pct > Decimal("0.03"):
        return None, "atr_regime_out_of_band"

    # Entry conditions:
    if last_bar.close <= entry_fast[-1]:
        return None, "close_below_entry_ema"
    if last_bar.close <= breakout_level:
        return None, "no_breakout"

    # Expected move in bps: atr_target_mult * ATR / price * 10_000
    expected_move_bps = (
        cfg.atr_target_mult * cur_atr / last_bar.close * Decimal(10_000)
    ).quantize(Decimal("0.01"))

    return (
        Signal(
            symbol=frame.symbol,
            ts=last_bar.ts,
            direction=OrderSide.BUY,
            reason=SIGNAL_OK,
            ref_price=last_bar.close,
            atr=cur_atr,
            expected_move_bps=expected_move_bps,
        ),
        SIGNAL_OK,
    )
