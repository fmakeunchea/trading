"""Pre-trade risk gates + safe compounding sizing.

This module is **pure**: no I/O, no wall-clock reads. Every input is
passed explicitly (including ``now``, ``kill_switch_present``, and the
reconciliation cleanliness flag). This makes the decision
deterministic and trivially testable.

Gate evaluation order (from the approved plan):

1. Kill switch present
2. Reconcile status not clean (entry-block flavour)
3. Session window / blackout
4. Data freshness
5. Halts (daily loss, drawdown, manual)
6. Daily loss cap
7. Drawdown halt
8. Concurrent positions cap
9. Total / per-symbol exposure caps (post-rounding)
10. Loss cooldown per symbol
11. Spread filter
12. Expected-edge gate
13. Sizing (computes qty; denies if 0)

Each gate denies via a short-circuit return, so the first matched reason
is authoritative. The returned :class:`RiskDecision` is audit-loggable:
it carries the exact reason string, the computed qty/notional, the
equity used, the drawdown percent, and the throttle multiplier.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from strategy.config import Config
from strategy.dto import (
    AccountSnapshot,
    Quote,
    RiskDecision,
    Signal,
)
from strategy.state import StrategyState
from strategy.time_utils import SessionClock, TF_NAME_TO_MINUTES, is_stale


# ---------------------------------------------------------------------------
# Candidate intent-request (pre-risk)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntryRequest:
    """What the signal layer proposes. Risk may deny, or size + bless."""

    signal: Signal
    quote: Quote                       # fresh quote at decision time
    # Bar-close timestamp of the latest entry-tf bar. Alpaca returns
    # bar.ts as the bar's *open*; the orchestrator computes ts + period
    # so the freshness gate compares against an honest "data current as
    # of" instant rather than always trailing by a full bar period.
    latest_bar_close_ts: datetime
    expected_move_bps: Decimal         # signal's estimated move
    reconcile_clean: bool              # from strategy.reconcile output
    kill_switch_present: bool          # from a filesystem check in orchestrator


# ---------------------------------------------------------------------------
# Top-level evaluator
# ---------------------------------------------------------------------------


def evaluate_entry(
    request: EntryRequest,
    state: StrategyState,
    config: Config,
    account: AccountSnapshot,
    now: datetime,
    *,
    session_clock: SessionClock | None = None,
) -> RiskDecision:
    """Run all gates in order; return the first deny, or a sized allow.

    ``session_clock`` may be passed in explicitly; otherwise one is
    derived from ``config``. Risk itself never reads config.execution —
    it pulls only the risk / sizing / execution.disaster_stop fields.
    """
    if session_clock is None:
        session_clock = SessionClock(
            session_start=config.risk.session_start_utc,
            session_end=config.risk.session_end_utc,
            block_first_minutes=config.risk.block_first_minutes,
            block_last_minutes=config.risk.block_last_minutes,
            flat_before_close_minutes=config.execution.flat_before_close_minutes,
        )

    sig = request.signal
    symbol = sig.symbol

    # 1. Kill switch
    if request.kill_switch_present:
        return _deny("kill_switch_active")

    # 2. Reconcile
    if not request.reconcile_clean:
        return _deny("reconcile_not_clean")

    # 3. Session & blackout
    if not session_clock.is_within_session(now):
        return _deny("outside_session_window")
    if session_clock.in_blackout(now):
        return _deny("session_blackout")

    # 4. Freshness — bar staleness only. Alpaca's bar.ts is the bar
    # *open*; the orchestrator passes bar-close (open + period) so this
    # threshold is "how long since the latest bar closed" rather than
    # "how long since the latest bar opened" (which is bounded below by
    # the bar period itself, making a 30s gate structurally unreachable).
    bar_period_s = TF_NAME_TO_MINUTES[config.strategy.entry_tf] * 60
    bar_max_age_s = bar_period_s + config.risk.stale_bar_grace_s
    if is_stale(request.latest_bar_close_ts, now, bar_max_age_s):
        return _deny("stale_bar")

    # 5. Halts of any kind
    if state.any_halt_active():
        return _deny(f"halt_active:{_active_halt_name(state)}")

    # 6. Daily loss cap (realized-PnL only in Phase 1; unrealized is
    # intentionally excluded — it's noisy and would cause the bot to
    # deny entries on transient marks. The DD halt and the per-trade
    # disaster stop are the other two layers that cover unrealized.)
    if not check_daily_loss_cap(
        state.realized_pnl_today,
        account.equity,
        config.risk.daily_loss_cap_pct,
    ):
        return _deny("daily_loss_cap_breached")

    # 7. Drawdown halt
    if check_drawdown_halt(
        account.equity,
        state.peak_equity,
        config.sizing.halt_drawdown_pct,
    ):
        return _deny("drawdown_halt")

    # 8. Concurrent positions cap
    if not check_concurrent_positions(state, config.risk.max_concurrent_positions):
        return _deny("max_concurrent_positions")

    # 10. Loss cooldown
    if not check_cooldown(
        symbol,
        state.last_loss_ts_by_symbol,
        config.risk.loss_cooldown_s,
        now,
    ):
        return _deny("loss_cooldown")

    # 11. Spread filter
    if not check_spread(request.quote, config.risk.spread_filter_bps):
        return _deny("spread_too_wide")

    # 12. Expected-edge gate
    if not check_expected_edge(
        request.expected_move_bps,
        config.risk.min_expected_edge_bps,
    ):
        return _deny("below_min_edge")

    # 13. Sizing
    equity_for_sizing = state.last_reconciled_equity
    if equity_for_sizing <= 0:
        # Never sized a trade without a confirmed broker equity.
        return _deny("equity_unknown")
    peak = max(state.peak_equity, equity_for_sizing)
    intraday_low = state.intraday_low_equity if state.intraday_low_equity > 0 else equity_for_sizing
    dd_pct = _drawdown_pct(peak, min(equity_for_sizing, intraday_low))
    throttle = compute_drawdown_throttle(dd_pct, config.sizing.drawdown_throttle_levels)

    qty, notional = compute_qty(
        equity=equity_for_sizing,
        price=sig.ref_price,
        notional_pct=config.sizing.position_notional_pct,
        throttle=throttle,
        min_notional=config.sizing.min_trade_notional,
        max_notional=config.sizing.max_trade_notional,
    )
    if qty <= 0:
        return _deny("size_below_minimum")

    # 9. Exposure caps are evaluated *after* sizing because they depend
    # on the post-rounding notional.
    if not check_exposure_caps(
        state=state,
        symbol=symbol,
        new_notional=notional,
        total_cap_pct=config.sizing.max_total_exposure_pct,
        symbol_cap_pct=config.sizing.max_symbol_exposure_pct,
        equity=equity_for_sizing,
    ):
        return _deny("exposure_cap")

    limit_price = _marketable_limit(sig.ref_price, config.execution.limit_slippage_bps)
    atr_stop_mult = config.risk.atr_stop_mult
    disaster_mult = config.execution.disaster_stop_atr_mult
    stop_price = (sig.ref_price - atr_stop_mult * sig.atr).quantize(Decimal("0.01"))
    disaster_stop_price = (sig.ref_price - disaster_mult * sig.atr).quantize(Decimal("0.01"))

    if disaster_stop_price >= limit_price:
        return _deny("disaster_stop_not_below_limit")
    if stop_price >= limit_price:
        return _deny("stop_not_below_limit")
    if disaster_stop_price >= stop_price:
        # Broker-side disaster stop must be wider than bot-side primary.
        return _deny("disaster_stop_not_wider_than_primary")

    return RiskDecision(
        allowed=True,
        reason="ok",
        qty=qty,
        notional=notional,
        limit_price=limit_price,
        stop_price=stop_price,
        disaster_stop_price=disaster_stop_price,
        throttle=throttle,
        equity_used=equity_for_sizing,
        peak_equity_used=peak,
        drawdown_pct=dd_pct,
    )


# ---------------------------------------------------------------------------
# Pure gate helpers (exported for focused tests)
# ---------------------------------------------------------------------------


def check_daily_loss_cap(
    realized_pnl_today: Decimal,
    equity: Decimal,
    cap_pct: Decimal,
) -> bool:
    """Return True iff we are *within* the daily loss cap (i.e. allowed)."""
    if cap_pct <= 0 or equity <= 0:
        return False
    max_loss = equity * cap_pct
    return realized_pnl_today > -max_loss  # strictly; equal = tripped


def check_drawdown_halt(
    equity: Decimal,
    peak_equity: Decimal,
    halt_pct: Decimal,
) -> bool:
    """Return True iff the halt should fire (i.e. deny)."""
    if peak_equity <= 0:
        return False
    return equity <= peak_equity * (Decimal(1) - halt_pct)


def check_concurrent_positions(state: StrategyState, cap: int) -> bool:
    return len(state.open_trades) < cap


def check_spread(quote: Quote, filter_bps: Decimal) -> bool:
    return quote.spread_bps() <= filter_bps


def check_stale_data(latest_bar_ts: datetime, now: datetime, max_age_s: int) -> bool:
    return not is_stale(latest_bar_ts, now, max_age_s)


def check_session_window(now: datetime, clock: SessionClock) -> bool:
    return clock.is_within_session(now) and not clock.in_blackout(now)


def check_cooldown(
    symbol: str,
    last_loss_ts_by_symbol: dict[str, datetime],
    cooldown_s: int,
    now: datetime,
) -> bool:
    last = last_loss_ts_by_symbol.get(symbol)
    if last is None:
        return True
    return (now - last) >= timedelta(seconds=cooldown_s)


def check_expected_edge(expected_bps: Decimal, min_bps: Decimal) -> bool:
    return expected_bps > min_bps


def check_exposure_caps(
    *,
    state: StrategyState,
    symbol: str,
    new_notional: Decimal,
    total_cap_pct: Decimal,
    symbol_cap_pct: Decimal,
    equity: Decimal,
) -> bool:
    if equity <= 0:
        return False
    # Per-symbol: existing exposure on the symbol + new must stay under cap.
    existing_symbol_notional = _existing_symbol_notional(state, symbol)
    if existing_symbol_notional + new_notional > equity * symbol_cap_pct:
        return False
    # Total: sum existing across all open trades + new.
    total_existing = sum(
        (t.entry_price * Decimal(t.qty) for t in state.open_trades.values()),
        Decimal(0),
    )
    return total_existing + new_notional <= equity * total_cap_pct


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------


def compute_drawdown_throttle(
    dd_pct: Decimal,
    levels: Sequence[tuple[Decimal, Decimal]],
) -> Decimal:
    """Return the appropriate throttle multiplier for the current DD.

    ``levels`` is a sequence of ``(threshold, multiplier)`` pairs sorted
    ascending by threshold. The highest threshold whose value the DD
    meets-or-exceeds wins. If no threshold is met, returns 1.0.
    """
    throttle = Decimal(1)
    for thr, mult in sorted(levels, key=lambda x: x[0]):
        if dd_pct >= thr:
            throttle = mult
    return throttle


def compute_qty(
    *,
    equity: Decimal,
    price: Decimal,
    notional_pct: Decimal,
    throttle: Decimal,
    min_notional: Decimal,
    max_notional: Decimal,
) -> tuple[int, Decimal]:
    """Return ``(qty, actual_notional)`` rounded down to whole shares.

    If the rounded qty produces a notional below ``min_notional``, returns
    (0, 0) — the caller must deny. Never raises on bad inputs; returns
    (0, 0) and lets the caller's deny path handle it.
    """
    if equity <= 0 or price <= 0 or notional_pct <= 0 or throttle <= 0:
        return 0, Decimal(0)
    raw = equity * notional_pct * throttle
    clamped = max(min_notional, min(max_notional, raw))
    # Floor-divide via integer conversion of the quotient.
    qty = int(clamped / price)
    if qty <= 0:
        return 0, Decimal(0)
    actual = (Decimal(qty) * price).quantize(Decimal("0.01"))
    if actual < min_notional:
        return 0, Decimal(0)
    return qty, actual


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _deny(reason: str) -> RiskDecision:
    return RiskDecision(allowed=False, reason=reason)


def _active_halt_name(state: StrategyState) -> str:
    for name, rec in state.halts.items():
        if rec.active:
            return name
    return "unknown"


def _drawdown_pct(peak: Decimal, equity: Decimal) -> Decimal:
    if peak <= 0:
        return Decimal(0)
    if equity >= peak:
        return Decimal(0)
    return ((peak - equity) / peak).quantize(Decimal("0.0001"))


def _existing_symbol_notional(state: StrategyState, symbol: str) -> Decimal:
    trade = state.open_trades.get(symbol)
    if trade is None:
        return Decimal(0)
    return trade.entry_price * Decimal(trade.qty)


def _marketable_limit(ref_price: Decimal, slippage_bps: Decimal) -> Decimal:
    """Long entry: aggressive limit *above* the reference price so we fill
    promptly while still bounding slippage."""
    return (ref_price * (Decimal(1) + slippage_bps / Decimal(10_000))).quantize(
        Decimal("0.01")
    )
