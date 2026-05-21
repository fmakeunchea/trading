"""Phase-4 SPIKE — RSI(2) mean reversion on liquid US equities.

==================================================================
⚠️  PROVISIONAL SPIKE — NOT A VERDICT  ⚠️
==================================================================
Same Phase-1 limitations as ``overnight.py`` and ``real_bars.py``:
perfect fills, spread_bps=0, verdict-gate 0.6 OPEN. Cost-adjusted
re-analysis via ``research.spike.analyze`` is the next step after
any result.

Falsifies (or supports) **Family A** from the strategy-concept
survey (2026-05-21): Connors / Avellaneda-Lee style short-horizon
mean reversion. Buy uptrending names that have suffered an extreme
1–2 day oversold dip; exit on mean reversion or short timeout.

LITERATURE CONFIG — LOCKED
--------------------------
No parameter sweeps until/unless this configuration shows clean
signal across 2022 + 2023 + 2024. The strategy has explicit knobs
(RSI thresholds, SMA periods, hold days) — exactly the situation
where in-sample tuning is most tempting. Discipline: ship the
canonical Connors values, accept or reject as-is.

Strategy
--------
Universe: --symbols (default: SPY QQQ AAPL MSFT NVDA)

Entry at session close on day T (all must hold):
  1. close[T] > SMA(close, 200)[T]   — uptrend filter
  2. RSI(2)[T] < 10                  — extreme oversold

Exit at session close (whichever fires first):
  1. RSI(2) > 70                     — overbought
  2. close > SMA(close, 5)           — back above short-term mean
  3. days_held >= 10                 — defensive timeout

Position size: --notional-per-trade (default $5,000 — matches
the overnight spike so cross-strategy P&L is comparable).

Tick cadence: 1-minute ticks via BacktestDriver. Strategy is a
no-op on all ticks except session_close, when it computes daily
indicators and makes entry/exit decisions.

Record format
-------------
Intents and results match production strategy shape so
``research.spike.analyze`` works unchanged.

History pad
-----------
Default 280 calendar days to give SMA(200) on daily bars enough
warm-up before the backtest start. (200 trading days ≈ 280
calendar days.)

Usage
-----
On the VPS, with research venv:
    cd /opt/trading-bot
    source <(grep -E '^ALPACA_API_(KEY|SECRET)=' autoflow/.env | sed 's/^/export /')
    .venv-research/bin/python -m research.spike.mean_reversion \\
        --start 2024-01-01 --end 2024-12-31 \\
        --symbols SPY QQQ AAPL MSFT NVDA \\
        --notional-per-trade 5000 \\
        --save-records /tmp/spike_mr_2024.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

UTC = timezone.utc


# --- env -----------------------------------------------------------------

def _load_alpaca_env_into_process() -> dict[str, str]:
    candidates = [
        Path("/opt/trading-bot/autoflow/.env"),
        Path(__file__).resolve().parents[2] / "autoflow" / ".env",
    ]
    found: dict[str, str] = {}
    for p in candidates:
        if not p.exists():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() in ("ALPACA_API_KEY", "ALPACA_API_SECRET"):
                found[k.strip()] = v.strip().strip('"').strip("'")
        if found:
            break
    for k, v in found.items():
        os.environ.setdefault(k, v)
    return found


# --- daily indicators ----------------------------------------------------

def _compute_daily_indicators(
    df_1m, *,
    rsi_period: int = 2,
    sma_short: int = 5,
    sma_long: int = 200,
):
    """1m bars → daily resampled bars with RSI_2, SMA_5, SMA_200 columns.

    RSI uses Wilder smoothing (alpha = 1/period via pandas ewm with
    ``adjust=False``). With period=2 the smoothing is fast — extreme
    values (< 10, > 90) occur frequently, which is the regime Connors's
    RSI-2 strategy exploits.

    Anti-leak invariant: each row's indicators depend ONLY on prior
    rows (rolling/ewm with no centering, no future references). The
    strategy looks up indicator values for ``today`` at the today's
    session-close tick, so no future bar is ever consulted.
    """
    from research.data.resample import resample
    df_d = resample(df_1m, 390)  # one bar per regular session
    if df_d.empty:
        return df_d
    close = df_d["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / rsi_period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / rsi_period, adjust=False).mean()
    # Avoid division-by-zero — when avg_loss is exactly 0, RSI is 100.
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    df_d["RSI_2"] = 100 - 100 / (1 + rs)
    df_d["RSI_2"] = df_d["RSI_2"].fillna(100)  # all-gain windows -> 100
    df_d["SMA_5"] = close.rolling(sma_short).mean()
    df_d["SMA_200"] = close.rolling(sma_long).mean()
    return df_d


# --- strategy state (minimal — driver contract surface) ------------------

@dataclass
class _SimpleState:
    peak_equity: Decimal
    last_reconciled_equity: Decimal
    intraday_low_equity: Decimal
    realized_pnl_today: Decimal = Decimal(0)
    open_trades: dict = field(default_factory=dict)


@dataclass
class _MRTrade:
    symbol: str
    qty: int
    entry_price: Decimal
    entry_ts: datetime
    entry_intent_id: str


# --- strategy ------------------------------------------------------------

class MeanReversionStrategy:
    """RSI(2) mean reversion on a fixed universe.

    All indicators are pre-computed at init time on a per-symbol daily
    DataFrame. At each session_close tick the strategy looks up
    today's row, evaluates exit conditions on open positions, then
    evaluates entry conditions on flat symbols. All other ticks are
    silent no-ops.

    Anti-leak: the indicator DF is computed over the full window once,
    but the strategy only consults today's row (by date). Each row's
    indicators depend only on prior rows by construction (rolling /
    EWM with no centering, no shifting).
    """

    def __init__(
        self, *,
        broker: Any,
        trade_log: Any,
        daily_indicators: dict[str, Any],   # {sym -> df_with_indicators}
        notional_per_trade: Decimal,
        starting_cash: Decimal,
        rsi_buy_threshold: float,
        rsi_sell_threshold: float,
        max_hold_days: int,
        calendar_module: Any,
    ) -> None:
        self.broker = broker
        self.trade_log = trade_log
        self._indicators = daily_indicators
        # Build O(1) date → row lookup per symbol. Daily bars are
        # session-open-anchored, so ``row.name.date()`` is the session date.
        self._by_date: dict[str, dict[date, Any]] = {}
        for sym, df in daily_indicators.items():
            self._by_date[sym] = {ts.date(): row for ts, row in df.iterrows()}

        self._notional = Decimal(notional_per_trade)
        self._rsi_buy = float(rsi_buy_threshold)
        self._rsi_sell = float(rsi_sell_threshold)
        self._max_hold_days = int(max_hold_days)
        self._cal = calendar_module
        self.state = _SimpleState(
            peak_equity=Decimal(starting_cash),
            last_reconciled_equity=Decimal(starting_cash),
            intraday_low_equity=Decimal(starting_cash),
        )
        self._open: dict[str, _MRTrade] = {}
        self._last_handled_date: date | None = None

    # ---- driver contract -------------------------------------------------

    def recover(self, now: datetime) -> None:
        return

    def tick(self, now: datetime, *, kill_switch_present: bool = False) -> None:
        bounds = self._cal.session_bounds(now.date())
        if bounds is None:
            return
        _, session_close = bounds
        if now != session_close:
            return
        # Idempotency: only handle the close ONCE per day.
        if self._last_handled_date == now.date():
            return
        self._last_handled_date = now.date()

        # Exits first (frees the symbol for re-entry on the same close,
        # but in practice the re-entry condition is unlikely to fire
        # in the same tick if we just exited at the same close — the
        # entry requires RSI < 10, which the exit RSI > 70 contradicts).
        for sym in list(self._open.keys()):
            self._maybe_exit(sym, now)

        # Then entries on flat symbols.
        for sym in self._by_date:
            if sym not in self._open:
                self._maybe_enter(sym, now)

    # ---- decision helpers -----------------------------------------------

    def _todays_row(self, sym: str, now: datetime):
        return self._by_date.get(sym, {}).get(now.date())

    def _maybe_enter(self, sym: str, now: datetime) -> None:
        import pandas as pd
        row = self._todays_row(sym, now)
        if row is None:
            return
        rsi = row["RSI_2"]
        sma_long = row["SMA_200"]
        close = row["close"]
        if pd.isna(rsi) or pd.isna(sma_long) or pd.isna(close):
            return
        if not (close > sma_long):
            return  # not in uptrend
        if not (rsi < self._rsi_buy):
            return  # not oversold enough
        self._enter(sym, now, Decimal(str(close)))

    def _maybe_exit(self, sym: str, now: datetime) -> None:
        import pandas as pd
        trade = self._open[sym]
        row = self._todays_row(sym, now)
        if row is None:
            return
        rsi = row["RSI_2"]
        sma_short = row["SMA_5"]
        close = row["close"]
        days_held = (now.date() - trade.entry_ts.date()).days
        reason: str | None = None
        if (not pd.isna(rsi)) and rsi > self._rsi_sell:
            reason = "rsi_overbought"
        elif (not pd.isna(sma_short)) and (not pd.isna(close)) and close > sma_short:
            reason = "close_above_sma5"
        elif days_held >= self._max_hold_days:
            reason = "max_hold_days"
        if reason is not None:
            self._exit(sym, now, reason)

    # ---- order emission (production-format records) ---------------------

    def _enter(self, sym: str, now: datetime, fill_price: Decimal) -> None:
        from strategy.dto import OrderClass, OrderIntent, OrderSide, TimeInForce
        price = fill_price.quantize(Decimal("0.01"))
        qty = int(self._notional / price)
        if qty <= 0:
            return
        intent_id = f"entry-{sym}-{now.isoformat()}"
        intent = OrderIntent(
            intent_id=intent_id,
            symbol=sym, side=OrderSide.BUY, qty=qty,
            limit_price=price,
            disaster_stop_price=(price * Decimal("0.5")).quantize(Decimal("0.01")),
            tif=TimeInForce.DAY, order_class=OrderClass.OTO,
            reason="rsi2_oversold_uptrend",
            ref_price=price, atr=Decimal("0.01"),
            spread_bps=Decimal(0), ts=now,
        )
        self.trade_log.append_intent({
            "intent_id":           intent_id,
            "client_order_id":     intent.client_order_id(),
            "symbol":              sym,
            "side":                OrderSide.BUY.value,
            "qty_requested":       qty,
            "limit_price":         str(price),
            "stop_price":          str(intent.disaster_stop_price),
            "disaster_stop_price": str(intent.disaster_stop_price),
            "target_price":        "0",
            "reason":              "rsi2_oversold_uptrend",
            "equity_snapshot":     str(self.state.last_reconciled_equity),
            "peak_equity":         str(self.state.peak_equity),
            "drawdown_pct":        "0",
            "throttle_multiplier": "1",
            "spread_bps":          "0",
            "result":              "submitting",
        })
        self.broker.submit_entry_with_protection(intent)
        terminal = self.broker.poll_terminal(intent.client_order_id())
        avg_price = terminal.avg_fill_price or price
        self.trade_log.append_result({
            "intent_id":                       intent_id,
            "client_order_id":                 intent.client_order_id(),
            "broker_order_id":                 terminal.broker_order_id,
            "status":                          terminal.status.value,
            "filled_qty":                      terminal.filled_qty,
            "avg_fill_price":                  str(avg_price),
            "protective_child_client_order_id": None,
            "protective_child_broker_id":      None,
            "protective_child_status":         None,
        })
        if terminal.filled_qty > 0:
            self._open[sym] = _MRTrade(
                symbol=sym, qty=terminal.filled_qty,
                entry_price=avg_price, entry_ts=now,
                entry_intent_id=intent_id,
            )

    def _exit(self, sym: str, now: datetime, reason: str) -> None:
        trade = self._open.pop(sym, None)
        if trade is None:
            return
        coid = f"close-{sym}-{now.isoformat()}"
        self.trade_log.append_intent({
            "intent_id":           coid,
            "client_order_id":     coid,
            "symbol":              sym,
            "side":                "sell",
            "qty_requested":       trade.qty,
            "reason":              reason,
            "equity_snapshot":     str(self.state.last_reconciled_equity),
            "peak_equity":         str(self.state.peak_equity),
            "result":              "submitting_close",
        })
        result = self.broker.flatten_symbol(sym, close_client_order_id=coid)
        fill_price = result.close_order.avg_fill_price or trade.entry_price
        realized = (fill_price - trade.entry_price) * Decimal(trade.qty)
        self.state.realized_pnl_today += realized
        self.state.last_reconciled_equity += realized
        if self.state.last_reconciled_equity > self.state.peak_equity:
            self.state.peak_equity = self.state.last_reconciled_equity
        self.trade_log.append_result({
            "intent_id":       coid,
            "client_order_id": coid,
            "broker_order_id": result.close_order.broker_order_id,
            "status":          result.close_order.status.value,
            "filled_qty":      result.close_order.filled_qty,
            "avg_fill_price":  str(fill_price),
            "reason":          reason,
            "realized_pnl":    str(realized),
        })


# --- bar pipeline --------------------------------------------------------

def _fetch_and_compute(
    symbol: str, start: datetime, end: datetime, cache_dir: str | Path,
) -> tuple[dict[int, Any], Any]:
    """Fetch 1m bars; resample to 5/15/60 (for broker quote synthesis);
    compute daily-bar DF with indicators (for strategy)."""
    from research.data.cache import cache_1m_bars
    from research.data.resample import resample

    df_1m = cache_1m_bars(symbol, start, end, cache_dir=cache_dir)
    broker_bars = {1: df_1m}
    for tf in (5, 15, 60):
        broker_bars[tf] = resample(df_1m, tf)
    df_daily_with_ind = _compute_daily_indicators(df_1m)
    return broker_bars, df_daily_with_ind


# --- main ----------------------------------------------------------------

def _banner(line: str) -> str:
    return "=" * 70 + "\n" + line + "\n" + "=" * 70


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase-4 RSI(2) mean-reversion spike — Family A.",
    )
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    ap.add_argument("--symbols", nargs="+",
                     default=["SPY", "QQQ", "AAPL", "MSFT", "NVDA"])
    ap.add_argument("--cash", default="100000")
    ap.add_argument("--notional-per-trade", default="5000")
    ap.add_argument("--cache-dir", default="/tmp/bt-cache")
    ap.add_argument("--history-pad-days", type=int, default=280,
                     help="Days of bar history before --start (SMA(200) "
                          "on daily bars needs ~200 trading days ≈ 280 calendar days)")
    # Literature config — locked. Documented as args for transparency
    # but the spike protocol forbids tuning across runs until/unless
    # the canonical values show signal across 3 years.
    ap.add_argument("--rsi-buy-threshold", type=float, default=10.0,
                     help="Connors canonical: 10")
    ap.add_argument("--rsi-sell-threshold", type=float, default=70.0,
                     help="Connors canonical: 70")
    ap.add_argument("--max-hold-days", type=int, default=10,
                     help="Defensive timeout; Connors canonical: 10")
    ap.add_argument("--save-records",
                     help="Path to save records JSONL for offline analysis")
    args = ap.parse_args(argv)

    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)
    start = datetime.combine(start_d, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(end_d, datetime.min.time(),
                            tzinfo=UTC) + timedelta(hours=23, minutes=59)
    if end <= start:
        print("ERROR: --end must be strictly after --start", file=sys.stderr)
        return 2
    history_start = start - timedelta(days=args.history_pad_days)

    found_env = _load_alpaca_env_into_process()
    if "ALPACA_API_KEY" not in os.environ or "ALPACA_API_SECRET" not in os.environ:
        print("ERROR: ALPACA_API_KEY/SECRET not in environment.", file=sys.stderr)
        return 2

    starting_cash = Decimal(args.cash)
    notional = Decimal(args.notional_per_trade)

    # ---- header ---------------------------------------------------------
    print(_banner("⚠️  RSI(2) MEAN-REVERSION SPIKE — NOT A VERDICT  ⚠️"))
    print("Hypothesis: liquid US equities in uptrends, after extreme")
    print("oversold 1-2 day dips, mean-revert within 1-10 days with")
    print("positive expectancy after costs.")
    print("LITERATURE CONFIG LOCKED — no tuning across runs.")
    print("=" * 70)
    print(f"Window           : {args.start} → {args.end} (UTC)")
    print(f"Symbols          : {', '.join(args.symbols)}")
    print(f"Starting cash    : ${args.cash}")
    print(f"Notional/trade   : ${args.notional_per_trade}")
    print(f"RSI(2) buy / sell: {args.rsi_buy_threshold} / "
          f"{args.rsi_sell_threshold}")
    print(f"Max hold days    : {args.max_hold_days}")
    print(f"History pad      : {args.history_pad_days} days "
          f"(from {history_start.date()})")
    print(f"Cache dir        : {args.cache_dir}")
    print(f"Env source       : {sorted(found_env.keys())}")
    print()

    # ---- bars + indicators ---------------------------------------------
    print("Fetching/resampling bars and computing daily indicators...")
    broker_bars: dict = {}
    daily_indicators: dict = {}
    for sym in args.symbols:
        bb, df_d = _fetch_and_compute(sym, history_start, end, args.cache_dir)
        for tf, df in bb.items():
            broker_bars[(sym, tf)] = df
        daily_indicators[sym] = df_d
        # Diagnostic: how many days within the backtest window have
        # SMA(200) seeded (i.e., are eligible for signal evaluation)?
        in_window = df_d.loc[(df_d.index >= start) & (df_d.index <= end)]
        eligible = in_window["SMA_200"].notna().sum() if len(in_window) else 0
        print(f"  {sym:5s}  daily_bars={len(df_d):4d}  "
              f"in_window={len(in_window):3d}  eligible={eligible:3d}")

    # ---- wire driver (manual, like overnight.py) ----------------------
    print()
    print("Wiring driver + MeanReversionStrategy...")
    from research.data import calendar as cal
    from research.engine.driver import BacktestDriver
    from research.engine.sim_broker import SimulatedBroker
    from research.engine.sim_trade_log import SimulatedTradeLog

    broker = SimulatedBroker(bars=broker_bars, starting_cash=starting_cash, now=start)
    trade_log = SimulatedTradeLog(clock=lambda: broker._require_now())
    strategy = MeanReversionStrategy(
        broker=broker, trade_log=trade_log,
        daily_indicators=daily_indicators,
        notional_per_trade=notional,
        starting_cash=starting_cash,
        rsi_buy_threshold=args.rsi_buy_threshold,
        rsi_sell_threshold=args.rsi_sell_threshold,
        max_hold_days=args.max_hold_days,
        calendar_module=cal,
    )
    driver = BacktestDriver(
        strategy=strategy, broker=broker, trade_log=trade_log,
        start=start, end=end, tick_interval_min=1,
    )

    print("Running BacktestDriver (tick_interval=1min)...")
    t0 = datetime.now(UTC)
    res = driver.run()
    elapsed_s = (datetime.now(UTC) - t0).total_seconds()

    # ---- analytics ------------------------------------------------------
    starting_eq = starting_cash
    ending_eq = res.equity[-1][1] if res.equity else starting_eq
    total_return_pct = (
        (ending_eq - starting_eq) / starting_eq * Decimal(100)
        if starting_eq > 0 else Decimal(0)
    )

    # Pair entries with closes.
    entry_fills_by_iid = {
        r.payload["intent_id"]: r for r in res.results
        if r.payload.get("intent_id", "").startswith("entry-")
        and r.payload.get("status") == "filled"
    }
    open_by_sym: dict[str, str] = {}
    rt_pnls: list[Decimal] = []
    hold_days: list[float] = []
    exit_reasons: Counter = Counter()
    per_symbol: dict[str, list[Decimal]] = {}
    for r in res.results:
        iid = r.payload.get("intent_id", "")
        parts = iid.split("-", 2)
        sym = parts[1] if len(parts) >= 2 else "?"
        if iid.startswith("entry-") and r.payload.get("status") == "filled":
            open_by_sym[sym] = iid
        elif iid.startswith("close-") and r.payload.get("status") == "filled":
            entry_iid = open_by_sym.pop(sym, None)
            if entry_iid is None:
                continue
            entry = entry_fills_by_iid.get(entry_iid)
            if entry is None:
                continue
            try:
                pnl = Decimal(r.payload["realized_pnl"])
                rt_pnls.append(pnl)
                hold_days.append((r.ts - entry.ts).total_seconds() / 86400.0)
                exit_reasons[r.payload.get("reason", "?")] += 1
                per_symbol.setdefault(sym, []).append(pnl)
            except (KeyError, ValueError, TypeError):
                continue

    # Peak / trough.
    peak = starting_eq
    max_dd = Decimal(0)
    for _, eq in res.equity:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * Decimal(100)
            if dd > max_dd:
                max_dd = dd

    print()
    print(_banner("RESULTS"))
    print(f"Wall-clock elapsed   : {elapsed_s:.1f}s")
    print(f"Driver steps         : {res.steps:,}")
    print(f"Equity start         : ${starting_eq:>12,.2f}")
    print(f"Equity end           : ${ending_eq:>12,.2f}")
    print(f"Total return         : {total_return_pct:+.3f}%")
    print(f"Max drawdown         : {max_dd:.3f}%")
    print()

    if rt_pnls:
        wins   = [p for p in rt_pnls if p > 0]
        losses = [p for p in rt_pnls if p < 0]
        be     = [p for p in rt_pnls if p == 0]
        total  = sum(rt_pnls, Decimal(0))
        avg    = total / Decimal(len(rt_pnls))
        avg_w  = sum(wins, Decimal(0)) / Decimal(len(wins)) if wins else Decimal(0)
        avg_l  = sum(losses, Decimal(0)) / Decimal(len(losses)) if losses else Decimal(0)
        wr     = Decimal(len(wins)) / Decimal(len(rt_pnls)) * Decimal(100)

        print(_banner(f"PER-TRADE ANALYTICS  (n={len(rt_pnls)})"))
        print(f"Wins / Losses / BE   : {len(wins)} / {len(losses)} / {len(be)}")
        print(f"Win rate             : {wr:.1f}%")
        print(f"Total P&L            : ${total:>10,.2f}")
        print(f"Avg P&L per trade    : ${avg:>10,.2f}")
        print(f"Avg win              : ${avg_w:>10,.2f}")
        print(f"Avg loss             : ${avg_l:>10,.2f}")
        if avg_l != 0:
            payoff = avg_w / abs(avg_l)
            print(f"Payoff ratio         : {payoff:.2f}")
            print(f"Breakeven WR needed  : "
                  f"{Decimal(1)/(Decimal(1)+payoff)*Decimal(100):.1f}%")
        if hold_days:
            print(f"Hold time (days)     : "
                  f"avg={sum(hold_days)/len(hold_days):.1f}  "
                  f"min={min(hold_days):.0f}  "
                  f"max={max(hold_days):.0f}")
        print()
        print("Exit-reason breakdown:")
        for reason, n in exit_reasons.most_common():
            pct = n / len(rt_pnls) * 100
            print(f"  {reason:25s} {n:>4}  ({pct:>5.1f}%)")
        print()
        print("Per-symbol P&L:")
        for sym in sorted(per_symbol):
            pnls = per_symbol[sym]
            total_sym = sum(pnls, Decimal(0))
            wins_sym = sum(1 for p in pnls if p > 0)
            print(f"  {sym:5s}  trades={len(pnls):3d}  "
                  f"wins={wins_sym:3d}  total=${total_sym:>10,.2f}  "
                  f"avg=${total_sym/Decimal(len(pnls)):>7,.2f}")

    if args.save_records:
        out_path = Path(args.save_records)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            for kind_name, recs in (
                ("intent", res.intents),
                ("result", res.results),
                ("incident", res.incidents),
            ):
                for r in recs:
                    f.write(json.dumps({
                        "record_kind": kind_name,
                        "ts": r.ts.isoformat(),
                        "payload": r.payload,
                        "line_hash": r.line_hash,
                        "prev_hash": r.prev_hash,
                    }) + "\n")
            for t, eq in res.equity:
                f.write(json.dumps({
                    "record_kind": "equity",
                    "ts": t.isoformat(),
                    "equity": str(eq),
                }) + "\n")
        print(f"Records saved: {out_path}")

    print()
    print(_banner("⚠️  REMINDER: PROVISIONAL — NOT A VERDICT  ⚠️"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
