"""Tests for strategy.risk.

All tests here are pure: risk.py takes ``now`` and all state explicitly,
so there is no wall-clock mocking needed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from strategy.config import load_config
from strategy.dto import (
    AccountSnapshot,
    OpenTrade,
    OrderSide,
    Quote,
    Signal,
)
from strategy.risk import (
    EntryRequest,
    check_concurrent_positions,
    check_cooldown,
    check_daily_loss_cap,
    check_drawdown_halt,
    check_expected_edge,
    check_exposure_caps,
    check_spread,
    check_stale_data,
    compute_drawdown_throttle,
    compute_qty,
    evaluate_entry,
)
from strategy.state import StrategyState


UTC = timezone.utc
FIXTURE = Path(__file__).parent / "fixtures" / "sample_config.yaml"
TEST_ENV = {
    "TEST_ALPACA_API_KEY": "k",
    "TEST_ALPACA_API_SECRET": "s",
}


@pytest.fixture
def cfg():
    return load_config(FIXTURE, env=TEST_ENV)


@pytest.fixture
def state():
    s = StrategyState()
    s.trading_day = datetime(2026, 4, 23, tzinfo=UTC).date()
    s.peak_equity = Decimal("25000")
    s.last_reconciled_equity = Decimal("25000")
    s.intraday_low_equity = Decimal("25000")
    return s


@pytest.fixture
def now():
    # 14:30 UTC — inside the fixture session (13:35–19:45) and outside
    # the opening 5-min blackout.
    return datetime(2026, 4, 23, 14, 30, tzinfo=UTC)


@pytest.fixture
def account():
    return AccountSnapshot(
        ts=datetime(2026, 4, 23, 14, 30, tzinfo=UTC),
        equity=Decimal("25000"),
        last_equity=Decimal("25000"),
        buying_power=Decimal("50000"),
        cash=Decimal("12500"),
        pattern_day_trader=False,
    )


def _signal(expected_move_bps: Decimal = Decimal("50")) -> Signal:
    return Signal(
        symbol="AAPL",
        ts=datetime(2026, 4, 23, 14, 30, tzinfo=UTC),
        direction=OrderSide.BUY,
        reason="breakout",
        ref_price=Decimal("200.00"),
        atr=Decimal("1.50"),
        expected_move_bps=expected_move_bps,
    )


def _quote(bid: str = "199.98", ask: str = "200.02") -> Quote:
    return Quote(
        symbol="AAPL",
        ts=datetime(2026, 4, 23, 14, 30, tzinfo=UTC),
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
        bid_size=100,
        ask_size=200,
    )


def _req(
    *,
    expected_move_bps: Decimal = Decimal("50"),
    latest_bar_close_ts: datetime | None = None,
    reconcile_clean: bool = True,
    kill_switch_present: bool = False,
    quote: Quote | None = None,
) -> EntryRequest:
    now_ts = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
    return EntryRequest(
        signal=_signal(expected_move_bps),
        quote=quote or _quote(),
        latest_bar_close_ts=latest_bar_close_ts or now_ts - timedelta(seconds=5),
        expected_move_bps=expected_move_bps,
        reconcile_clean=reconcile_clean,
        kill_switch_present=kill_switch_present,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_clean_allow(cfg, state, account, now) -> None:
    d = evaluate_entry(_req(), state, cfg, account, now)
    assert d.allowed, d.reason
    assert d.qty > 0
    assert d.limit_price is not None
    assert d.stop_price is not None and d.stop_price < d.limit_price
    assert d.disaster_stop_price is not None
    assert d.disaster_stop_price < d.stop_price        # wider than primary
    assert d.throttle == Decimal(1)
    assert d.drawdown_pct == Decimal(0)


# ---------------------------------------------------------------------------
# Each gate denies in isolation
# ---------------------------------------------------------------------------


def test_kill_switch_denies(cfg, state, account, now) -> None:
    d = evaluate_entry(_req(kill_switch_present=True), state, cfg, account, now)
    assert not d.allowed and d.reason == "kill_switch_active"


def test_reconcile_not_clean_denies(cfg, state, account, now) -> None:
    d = evaluate_entry(_req(reconcile_clean=False), state, cfg, account, now)
    assert not d.allowed and d.reason == "reconcile_not_clean"


def test_outside_session_denies(cfg, state, account) -> None:
    early = datetime(2026, 4, 23, 8, 0, tzinfo=UTC)
    d = evaluate_entry(_req(), state, cfg, account, early)
    assert not d.allowed and d.reason == "outside_session_window"


def test_blackout_denies(cfg, state, account) -> None:
    open_bar = datetime(2026, 4, 23, 13, 36, tzinfo=UTC)   # inside first-5-min blackout
    d = evaluate_entry(_req(), state, cfg, account, open_bar)
    assert not d.allowed and d.reason == "session_blackout"


def test_stale_bar_denies(cfg, state, account, now) -> None:
    # entry_tf=5Min (300s) + stale_bar_grace_s=30 ⇒ threshold = 330s.
    # Bar close 400s ago is past threshold ⇒ deny stale_bar.
    stale_close = now - timedelta(seconds=400)
    d = evaluate_entry(_req(latest_bar_close_ts=stale_close), state, cfg, account, now)
    assert not d.allowed and d.reason == "stale_bar"


def test_5min_bar_open_5min_ago_passes(cfg, state, account, now) -> None:
    """A 5Min bar whose *open* was 5 minutes ago has just closed — fresh.

    Regression for the bug where the gate compared `now - bar.open` against
    a 30s threshold, which structurally could never pass for 5-min bars.
    """
    bar_open = now - timedelta(minutes=5)
    bar_close = bar_open + timedelta(minutes=5)   # == now
    d = evaluate_entry(_req(latest_bar_close_ts=bar_close), state, cfg, account, now)
    assert d.allowed, d.reason


def test_5min_bar_close_just_past_threshold_denies(cfg, state, account, now) -> None:
    """Bar close 331s ago is just past the 330s threshold ⇒ stale_bar."""
    bar_close = now - timedelta(seconds=331)
    d = evaluate_entry(_req(latest_bar_close_ts=bar_close), state, cfg, account, now)
    assert not d.allowed and d.reason == "stale_bar"


def test_quote_threshold_independent_of_bar_grace(cfg) -> None:
    """The quote threshold (used at the exit-management path) is the
    `stale_data_max_age_s` field and must remain decoupled from
    `stale_bar_grace_s`. The exit path is at strategy.manage_open_trades.
    """
    from strategy.time_utils import is_stale
    assert cfg.risk.stale_data_max_age_s == 30
    assert cfg.risk.stale_bar_grace_s == 30
    qref = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
    # 31s old ⇒ stale at the quote threshold (would skip an exit eval).
    assert is_stale(qref - timedelta(seconds=31), qref,
                    cfg.risk.stale_data_max_age_s) is True
    # 29s old ⇒ fresh at the quote threshold.
    assert is_stale(qref - timedelta(seconds=29), qref,
                    cfg.risk.stale_data_max_age_s) is False


def test_bar_close_timezone_handling(cfg, state, account, now) -> None:
    """Contract for the bar-staleness gate: tz-aware datetimes are
    normalised to UTC; naive datetimes are rejected.

    * Aware UTC → works.
    * Aware non-UTC (same instant, expressed in another zone) → identical.
    * Naive → raises (we never guess the caller's zone).
    """
    from zoneinfo import ZoneInfo
    aware_utc = now - timedelta(seconds=60)                    # well within 330s
    aware_ny = aware_utc.astimezone(ZoneInfo("America/New_York"))  # same instant
    d_utc = evaluate_entry(_req(latest_bar_close_ts=aware_utc), state, cfg, account, now)
    d_ny = evaluate_entry(_req(latest_bar_close_ts=aware_ny), state, cfg, account, now)
    assert d_utc.allowed and d_ny.allowed
    assert d_utc.reason == d_ny.reason

    naive = aware_utc.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_entry(_req(latest_bar_close_ts=naive), state, cfg, account, now)


def test_halt_active_denies(cfg, state, account, now) -> None:
    state.set_halt("daily_loss", "cap breached", now=now)
    d = evaluate_entry(_req(), state, cfg, account, now)
    assert not d.allowed and d.reason.startswith("halt_active")


def test_daily_loss_cap_denies(cfg, state, account, now) -> None:
    # cap_pct = 0.03, equity = 25000 → max loss = 750. Realized = -800.
    state.realized_pnl_today = Decimal("-800")
    d = evaluate_entry(_req(), state, cfg, account, now)
    assert not d.allowed and d.reason == "daily_loss_cap_breached"


def test_drawdown_halt_denies(cfg, state, account, now) -> None:
    # halt_drawdown_pct = 0.08; peak = 25000; equity ≤ 23000 fires.
    state.peak_equity = Decimal("25000")
    account2 = AccountSnapshot(
        ts=account.ts,
        equity=Decimal("22500"),
        last_equity=account.last_equity,
        buying_power=account.buying_power,
        cash=account.cash,
        pattern_day_trader=False,
    )
    d = evaluate_entry(_req(), state, cfg, account2, now)
    assert not d.allowed and d.reason == "drawdown_halt"


def test_max_concurrent_denies(cfg, state, account, now) -> None:
    # max_concurrent_positions = 3 in fixture; fill to cap.
    for s in ("MSFT", "NVDA", "GOOG"):
        state.open_trades[s] = OpenTrade(
            symbol=s, qty=1, entry_price=Decimal("100"),
            entry_ts=now, stop_price=Decimal("95"),
            disaster_stop_price=Decimal("90"), target_price=Decimal("105"),
            intent_id="i", parent_client_order_id="p",
            protective_child_client_order_id=None,
            protective_child_broker_id=None, last_seen_broker_qty=1,
        )
    d = evaluate_entry(_req(), state, cfg, account, now)
    assert not d.allowed and d.reason == "max_concurrent_positions"


def test_cooldown_denies_then_allows(cfg, state, account, now) -> None:
    # loss_cooldown_s = 900. Loss at now-800s still in cooldown.
    state.last_loss_ts_by_symbol["AAPL"] = now - timedelta(seconds=800)
    d = evaluate_entry(_req(), state, cfg, account, now)
    assert not d.allowed and d.reason == "loss_cooldown"
    # Advance clock past the window. Keep the bar fresh relative to `later`.
    later = now + timedelta(seconds=200)   # 1000s after the loss
    fresh_bar = later - timedelta(seconds=5)
    d2 = evaluate_entry(_req(latest_bar_close_ts=fresh_bar), state, cfg, account, later)
    assert d2.allowed, d2.reason


def test_spread_too_wide_denies(cfg, state, account, now) -> None:
    # spread_filter_bps = 15 in fixture; build a quote with ~25 bps.
    wide = _quote(bid="200.00", ask="200.50")   # ~25 bps mid
    d = evaluate_entry(_req(quote=wide), state, cfg, account, now)
    assert not d.allowed and d.reason == "spread_too_wide"


def test_below_min_edge_denies(cfg, state, account, now) -> None:
    # min_expected_edge_bps = 40 in fixture.
    d = evaluate_entry(_req(expected_move_bps=Decimal("30")), state, cfg, account, now)
    assert not d.allowed and d.reason == "below_min_edge"


def test_min_edge_equal_denies(cfg, state, account, now) -> None:
    # Strictly greater required — equal is not enough.
    d = evaluate_entry(_req(expected_move_bps=Decimal("40")), state, cfg, account, now)
    assert not d.allowed and d.reason == "below_min_edge"


def test_equity_unknown_denies(cfg, state, account, now) -> None:
    state.last_reconciled_equity = Decimal(0)
    d = evaluate_entry(_req(), state, cfg, account, now)
    assert not d.allowed and d.reason == "equity_unknown"


def test_size_below_minimum_denies(cfg, state, account, now) -> None:
    # Raise the price so that at 5% of 25k (=1250) you get only 0 shares
    # after flooring. Actually 1250 / 100000 ~= 0. We cap max notional
    # so the clamp still produces 0 shares at ultra-high prices.
    expensive_sig = Signal(
        symbol="AAPL",
        ts=datetime(2026, 4, 23, 14, 30, tzinfo=UTC),
        direction=OrderSide.BUY,
        reason="breakout",
        ref_price=Decimal("50000"),
        atr=Decimal("100"),
        expected_move_bps=Decimal("50"),
    )
    req = EntryRequest(
        signal=expensive_sig,
        quote=_quote(bid="49999", ask="50001"),
        latest_bar_close_ts=now - timedelta(seconds=5),
        expected_move_bps=Decimal("50"),
        reconcile_clean=True,
        kill_switch_present=False,
    )
    d = evaluate_entry(req, state, cfg, account, now)
    assert not d.allowed and d.reason == "size_below_minimum"


def test_exposure_cap_denies(cfg, state, account, now) -> None:
    # Pre-fill an open trade that blows the per-symbol cap on AAPL.
    state.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL",
        qty=10,
        entry_price=Decimal("240"),   # 2400 already on AAPL
        entry_ts=now,
        stop_price=Decimal("235"),
        disaster_stop_price=Decimal("230"),
        target_price=Decimal("245"),
        intent_id="i",
        parent_client_order_id="p",
        protective_child_client_order_id=None,
        protective_child_broker_id=None,
        last_seen_broker_qty=10,
    )
    # First trip another gate (concurrent positions) should be OK since
    # max_concurrent_positions=3; this symbol is already in open_trades.
    # Max symbol exposure = 10% of 25k = 2500; adding any notional > 100
    # tips it over.
    d = evaluate_entry(_req(), state, cfg, account, now)
    assert not d.allowed and d.reason == "exposure_cap"


# ---------------------------------------------------------------------------
# Throttle math
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dd,expected_mult",
    [
        (Decimal("0.00"), Decimal("1")),
        (Decimal("0.01"), Decimal("1")),         # below first threshold
        (Decimal("0.02"), Decimal("0.75")),      # exactly at
        (Decimal("0.03"), Decimal("0.75")),
        (Decimal("0.04"), Decimal("0.50")),
        (Decimal("0.05"), Decimal("0.50")),
        (Decimal("0.06"), Decimal("0.25")),
        (Decimal("0.08"), Decimal("0.25")),      # halt boundary handled elsewhere
    ],
)
def test_throttle_thresholds(dd, expected_mult) -> None:
    levels = [
        (Decimal("0.02"), Decimal("0.75")),
        (Decimal("0.04"), Decimal("0.50")),
        (Decimal("0.06"), Decimal("0.25")),
    ]
    assert compute_drawdown_throttle(dd, levels) == expected_mult


def test_throttle_empty_levels_defaults_to_one() -> None:
    assert compute_drawdown_throttle(Decimal("0.5"), []) == Decimal(1)


# ---------------------------------------------------------------------------
# Sizing math
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "equity,price,npct,throttle,min_n,max_n,expected_qty",
    [
        (Decimal("25000"), Decimal("200"), Decimal("0.05"), Decimal(1), Decimal("200"), Decimal("2500"), 6),  # 25000*.05=1250, /200=6
        (Decimal("25000"), Decimal("200"), Decimal("0.05"), Decimal("0.5"), Decimal("200"), Decimal("2500"), 3),  # throttled to 625, /200=3
        (Decimal("25000"), Decimal("200"), Decimal("0.05"), Decimal("0.25"), Decimal("200"), Decimal("2500"), 1),  # clamp to min 200, /200=1
        (Decimal("25000"), Decimal("10000"), Decimal("0.05"), Decimal(1), Decimal("200"), Decimal("2500"), 0),  # clamp to 2500, /10000=0
    ],
)
def test_compute_qty(equity, price, npct, throttle, min_n, max_n, expected_qty) -> None:
    qty, notional = compute_qty(
        equity=equity,
        price=price,
        notional_pct=npct,
        throttle=throttle,
        min_notional=min_n,
        max_notional=max_n,
    )
    assert qty == expected_qty
    if qty == 0:
        assert notional == Decimal(0)
    else:
        assert notional == (Decimal(qty) * price).quantize(Decimal("0.01"))


@pytest.mark.parametrize("equity,price", [(Decimal(0), Decimal("1")), (Decimal("1"), Decimal(0))])
def test_compute_qty_guards(equity, price) -> None:
    qty, notional = compute_qty(
        equity=equity, price=price, notional_pct=Decimal("0.1"),
        throttle=Decimal(1), min_notional=Decimal("1"), max_notional=Decimal("1000"),
    )
    assert qty == 0 and notional == Decimal(0)


# ---------------------------------------------------------------------------
# Intraday-low stickiness
# ---------------------------------------------------------------------------


def test_intraday_low_is_sticky_throttle(cfg, account, now) -> None:
    """Throttle is derived from the intraday low, not the latest tick, so
    a mid-day bounce does not un-throttle."""
    s = StrategyState()
    s.peak_equity = Decimal("25000")
    s.last_reconciled_equity = Decimal("24500")
    s.intraday_low_equity = Decimal("24000")   # 4% DD from peak
    d = evaluate_entry(_req(), s, cfg, account, now)
    assert d.allowed, d.reason
    # 4% dd → 0.50 throttle per fixture levels {0.02:.75, 0.04:.50, 0.06:.25}
    assert d.throttle == Decimal("0.50")


# ---------------------------------------------------------------------------
# Individual pure-function gates (edge cases not reachable via evaluate_entry)
# ---------------------------------------------------------------------------


def test_check_daily_loss_cap_edges() -> None:
    equity = Decimal("10000")
    cap = Decimal("0.02")   # 200 max loss
    assert check_daily_loss_cap(Decimal("-199.99"), equity, cap) is True
    assert check_daily_loss_cap(Decimal("-200"), equity, cap) is False    # strict
    assert check_daily_loss_cap(Decimal("-201"), equity, cap) is False
    assert check_daily_loss_cap(Decimal("100"), equity, cap) is True      # up day
    assert check_daily_loss_cap(Decimal("0"), Decimal(0), cap) is False   # zero equity fails closed


def test_check_drawdown_halt_edges() -> None:
    peak = Decimal("1000")
    halt = Decimal("0.08")
    assert check_drawdown_halt(Decimal("921"), peak, halt) is False
    assert check_drawdown_halt(Decimal("920"), peak, halt) is True         # at bound
    assert check_drawdown_halt(Decimal("800"), peak, halt) is True
    assert check_drawdown_halt(Decimal("1000"), Decimal(0), halt) is False # no peak


def test_check_spread_edges() -> None:
    assert check_spread(_quote("100.00", "100.05"), Decimal("10")) is True    # ~5 bps
    assert check_spread(_quote("100.00", "100.20"), Decimal("10")) is False   # ~20 bps


def test_check_stale_data_edges() -> None:
    now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
    assert check_stale_data(now - timedelta(seconds=10), now, 30) is True
    assert check_stale_data(now - timedelta(seconds=31), now, 30) is False


def test_check_cooldown_edges() -> None:
    now = datetime(2026, 4, 23, 14, 30, tzinfo=UTC)
    assert check_cooldown("AAPL", {}, 900, now) is True
    assert check_cooldown("AAPL", {"AAPL": now - timedelta(seconds=899)}, 900, now) is False
    assert check_cooldown("AAPL", {"AAPL": now - timedelta(seconds=900)}, 900, now) is True
    assert check_cooldown("AAPL", {"AAPL": now - timedelta(seconds=901)}, 900, now) is True


def test_check_expected_edge_edges() -> None:
    assert check_expected_edge(Decimal("50"), Decimal("40")) is True
    assert check_expected_edge(Decimal("40"), Decimal("40")) is False  # strictly greater
    assert check_expected_edge(Decimal("39"), Decimal("40")) is False


def test_check_concurrent_edges() -> None:
    s = StrategyState()
    assert check_concurrent_positions(s, cap=1) is True
    s.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL", qty=1, entry_price=Decimal("1"),
        entry_ts=datetime(2026, 4, 23, tzinfo=UTC),
        stop_price=Decimal("0.9"), disaster_stop_price=Decimal("0.8"),
        target_price=Decimal("1.1"), intent_id="i",
        parent_client_order_id="p",
        protective_child_client_order_id=None,
        protective_child_broker_id=None, last_seen_broker_qty=1,
    )
    assert check_concurrent_positions(s, cap=1) is False
    assert check_concurrent_positions(s, cap=2) is True


def test_check_exposure_caps_edges() -> None:
    s = StrategyState()
    equity = Decimal("10000")
    # No existing: 9% fits a 10% symbol cap and 50% total cap.
    assert check_exposure_caps(
        state=s, symbol="AAPL", new_notional=Decimal("900"),
        total_cap_pct=Decimal("0.5"), symbol_cap_pct=Decimal("0.10"),
        equity=equity,
    ) is True
    # 11% breaks the 10% symbol cap.
    assert check_exposure_caps(
        state=s, symbol="AAPL", new_notional=Decimal("1100"),
        total_cap_pct=Decimal("0.5"), symbol_cap_pct=Decimal("0.10"),
        equity=equity,
    ) is False
    # Zero equity fails closed.
    assert check_exposure_caps(
        state=s, symbol="AAPL", new_notional=Decimal("1"),
        total_cap_pct=Decimal("1"), symbol_cap_pct=Decimal("1"),
        equity=Decimal(0),
    ) is False
