"""Phase-4 SPIKE — overnight drift on equity ETFs.

==================================================================
⚠️  PROVISIONAL SPIKE — NOT A VERDICT  ⚠️
==================================================================
Same caveats as ``real_bars.py``: perfect fills, spread_bps=0, the
verdict-gate caveat (0.6) is still open. We're looking at numbers to
inform research direction, NOT to deploy.

This spike falsifies (or supports) the **overnight drift** hypothesis
on liquid US equity ETFs. The strategy is deliberately parameter-free:

  Every trading day:
    • At session close: buy a fixed-notional position in each ticker.
    • At first tick after next session open: sell the position.

Zero indicators. Zero thresholds. The hypothesis is binary — either
overnight returns aggregate to positive expectancy over 250+ days, or
they don't. Documented persistence in literature (Knuteson 2019, 2022;
Lou/Polk/Skouras 2019) makes this the highest-information first test
after deprecating the breakout concept.

Reuses everything: SimulatedBroker, SimulatedTradeLog, BacktestDriver,
cost analyzer. The only new piece is :class:`OvernightStrategy`, which
emits intents/results in the production-strategy shape so the existing
analyzer works unchanged.

Tick cadence
------------
The driver ticks at ``tick_interval_min=1`` (1-minute bars). At
session-close ticks we buy; at the first tick AFTER session-open the
following day we sell. The 1-minute grid keeps both endpoints close to
the true MOC/MOO prints — sell happens at ``open + 1min``, which is
typically a few bps off the true open.

Usage
-----
On the VPS, with research venv:
    cd /opt/trading-bot
    source <(grep -E '^ALPACA_API_(KEY|SECRET)=' autoflow/.env | sed 's/^/export /')
    .venv-research/bin/python -m research.spike.overnight \\
        --start 2024-01-01 --end 2024-12-31 \\
        --symbols SPY QQQ --notional-per-trade 5000

The output JSONL is shaped identically to ``real_bars.py`` output so
``research.spike.analyze`` works unchanged.
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
    """Load ALPACA_API_KEY/SECRET from autoflow/.env into os.environ."""
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


# --- strategy state (minimal — just what the driver touches) -------------

@dataclass
class _SimpleState:
    """Equity bookkeeping. Mirrors the subset of StrategyState that
    ``BacktestDriver.build()`` seeds before the loop runs. We don't use
    ``build()`` here (we wire manually), but match the shape so the
    rest of the harness treats this strategy as a peer."""
    peak_equity: Decimal
    last_reconciled_equity: Decimal
    intraday_low_equity: Decimal
    realized_pnl_today: Decimal = Decimal(0)
    open_trades: dict = field(default_factory=dict)


@dataclass
class _OvernightTrade:
    """Lightweight per-position record kept inside the strategy."""
    symbol: str
    qty: int
    entry_price: Decimal
    entry_ts: datetime
    entry_intent_id: str


# --- the strategy --------------------------------------------------------

class OvernightStrategy:
    """Always-long-overnight on a fixed ETF universe.

    Driver contract:
      * ``recover(now)`` — no-op (the spike runs fresh; no on-disk
        recovery needed for a falsification test).
      * ``tick(now, kill_switch_present=False)`` — at each tick, check
        if we're at session-close (entry condition) or past session-open
        with overnight positions still open (exit condition). All other
        ticks are silent no-ops.

    Record shape:
      Intents and results match the production format
      (``entry-{sym}-{iso_ts}`` / ``close-{sym}-{iso_ts}`` ids,
      ``realized_pnl`` on close results) so the existing analyzer
      consumes this output unchanged.
    """

    def __init__(
        self, *,
        broker: Any,
        trade_log: Any,
        symbols: list[str],
        notional_per_trade: Decimal,
        starting_cash: Decimal,
        calendar_module: Any,
    ) -> None:
        self.broker = broker
        self.trade_log = trade_log
        self.symbols = list(symbols)
        self._notional = Decimal(notional_per_trade)
        self._cal = calendar_module
        self.state = _SimpleState(
            peak_equity=Decimal(starting_cash),
            last_reconciled_equity=Decimal(starting_cash),
            intraday_low_equity=Decimal(starting_cash),
        )
        # Internal: open positions keyed by symbol.
        self._open: dict[str, _OvernightTrade] = {}

    # ---- driver contract -------------------------------------------------

    def recover(self, now: datetime) -> None:
        return  # no-op for the spike

    def tick(self, now: datetime, *, kill_switch_present: bool = False) -> None:
        bounds = self._cal.session_bounds(now.date())
        if bounds is None:
            return  # not a trading day
        session_open, session_close = bounds

        # EXIT phase: sell any position whose entry was on a DIFFERENT
        # session date. The "now > session_open" guard ensures we don't
        # try to sell at session_open itself (no current-day bars are
        # visible yet at the open tick — quote synthesis would return
        # stale yesterday data).
        if self._open and now > session_open:
            for sym in list(self._open.keys()):
                trade = self._open[sym]
                if trade.entry_ts.date() != now.date():
                    self._exit(sym, now)

        # ENTRY phase: on the session-close tick exactly, buy each
        # configured symbol that we're currently flat in.
        if now == session_close:
            for sym in self.symbols:
                if sym not in self._open:
                    self._enter(sym, now)

    # ---- internals -------------------------------------------------------

    def _quote_or_none(self, sym: str):
        try:
            return self.broker.get_latest_quote(sym)
        except Exception:
            return None

    def _enter(self, sym: str, now: datetime) -> None:
        # Lazy imports of production DTOs so this spike file stays
        # importable in environments where strategy.dto isn't present.
        from strategy.dto import (
            OrderClass, OrderIntent, OrderSide, TimeInForce,
        )

        quote = self._quote_or_none(sym)
        if quote is None:
            return
        price = quote.mid()
        if price <= 0:
            return
        qty = int(self._notional / price)
        if qty <= 0:
            return

        intent_id = f"entry-{sym}-{now.isoformat()}"
        intent = OrderIntent(
            intent_id=intent_id,
            symbol=sym,
            side=OrderSide.BUY,
            qty=qty,
            limit_price=price,
            # disaster_stop placed far below; SimulatedBroker doesn't
            # actually enforce it — the production strategy's
            # ``_manage_positions`` is what fires bot-side exits.
            # In this spike we never trigger any of that logic; the
            # only exit path is the explicit MOO sell in ``_exit``.
            disaster_stop_price=(price * Decimal("0.5")).quantize(Decimal("0.01")),
            tif=TimeInForce.DAY,
            order_class=OrderClass.OTO,
            reason="overnight_close_buy",
            ref_price=price,
            atr=Decimal("0.01"),     # placeholder; not used downstream
            spread_bps=Decimal(0),
            ts=now,
        )

        # Production-shape INTENT record.
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
            "reason":              "overnight_close_buy",
            "equity_snapshot":     str(self.state.last_reconciled_equity),
            "peak_equity":         str(self.state.peak_equity),
            "drawdown_pct":        "0",
            "throttle_multiplier": "1",
            "spread_bps":          "0",
            "result":              "submitting",
        })

        submitted = self.broker.submit_entry_with_protection(intent)
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
            self._open[sym] = _OvernightTrade(
                symbol=sym, qty=terminal.filled_qty,
                entry_price=avg_price, entry_ts=now,
                entry_intent_id=intent_id,
            )

    def _exit(self, sym: str, now: datetime) -> None:
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
            "reason":              "overnight_open_sell",
            "equity_snapshot":     str(self.state.last_reconciled_equity),
            "peak_equity":         str(self.state.peak_equity),
            "result":              "submitting_close",
        })

        result = self.broker.flatten_symbol(sym, close_client_order_id=coid)
        fill_price = result.close_order.avg_fill_price or trade.entry_price
        realized = (fill_price - trade.entry_price) * Decimal(trade.qty)

        # Update equity bookkeeping so the driver's get_account_snapshot
        # rolls forward correctly.
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
            "reason":          "overnight_open_sell",
            "realized_pnl":    str(realized),
        })


# --- bar pipeline (same as real_bars.py) ---------------------------------

def _fetch_and_resample(
    symbol: str, start: datetime, end: datetime, cache_dir: str | Path,
) -> dict[int, Any]:
    """Cache-fetch 1m bars; resample to 5/15/60 (kept for broker quote
    synthesis even though the strategy only uses 1m)."""
    from research.data.cache import cache_1m_bars
    from research.data.resample import resample

    df_1m = cache_1m_bars(symbol, start, end, cache_dir=cache_dir)
    out: dict[int, Any] = {1: df_1m}
    for tf in (5, 15, 60):
        out[tf] = resample(df_1m, tf)
    return out


# --- main ----------------------------------------------------------------

def _banner(line: str) -> str:
    return "=" * 70 + "\n" + line + "\n" + "=" * 70


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase-4 overnight-drift spike on equity ETFs.",
    )
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    ap.add_argument("--symbols", nargs="+", default=["SPY", "QQQ"])
    ap.add_argument("--cash", default="100000", help="Starting cash (Decimal str)")
    ap.add_argument(
        "--notional-per-trade", default="5000",
        help="Target notional per symbol per overnight position (default $5000)",
    )
    ap.add_argument(
        "--cache-dir", default="/tmp/bt-cache",
        help="1-minute parquet cache directory",
    )
    ap.add_argument(
        "--history-pad-days", type=int, default=5,
        help="Days of bar history before --start (small; no indicators)",
    )
    ap.add_argument(
        "--save-records",
        help="Path to save records JSONL for offline cost-adjusted analysis",
    )
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
        print(
            "ERROR: ALPACA_API_KEY/SECRET not in environment. "
            "Source autoflow/.env first.", file=sys.stderr,
        )
        return 2

    starting_cash = Decimal(args.cash)
    notional = Decimal(args.notional_per_trade)

    # ---- header ---------------------------------------------------------
    print(_banner("⚠️  OVERNIGHT-DRIFT SPIKE — NOT A VERDICT  ⚠️"))
    print("Hypothesis: overnight returns on equity ETFs aggregate to")
    print("positive expectancy. Zero parameters; one trade/day/ticker.")
    print("Phase-1 limitations still apply (perfect fills, spread_bps=0,")
    print("verdict-gate 0.6 open). DO NOT trade real money on these numbers.")
    print("=" * 70)
    print(f"Window           : {args.start} → {args.end} (UTC)")
    print(f"Symbols          : {', '.join(args.symbols)}")
    print(f"Starting cash    : ${args.cash}")
    print(f"Notional/trade   : ${args.notional_per_trade}")
    print(f"Cache dir        : {args.cache_dir}")
    print(f"Env source       : {sorted(found_env.keys())}")
    print()

    # ---- bars -----------------------------------------------------------
    print("Fetching/resampling bars...")
    bars: dict = {}
    for sym in args.symbols:
        frames = _fetch_and_resample(sym, history_start, end, args.cache_dir)
        # Driver tick cadence is 1m for this spike, but we keep
        # 5/15/60 in the broker dict because production SimulatedBroker
        # may consult them for quote synthesis.
        for tf_min, df in frames.items():
            bars[(sym, tf_min)] = df
        print(f"  {sym:5s}  1m={len(frames[1]):6d}  "
              f"5m={len(frames[5]):6d}  15m={len(frames[15]):6d}  "
              f"1h={len(frames[60]):6d}")

    # ---- wire driver (manually, not via .build()) ----------------------
    print()
    print("Wiring driver + OvernightStrategy...")
    from research.data import calendar as cal
    from research.engine.driver import BacktestDriver
    from research.engine.sim_broker import SimulatedBroker
    from research.engine.sim_trade_log import SimulatedTradeLog

    broker = SimulatedBroker(bars=bars, starting_cash=starting_cash, now=start)
    trade_log = SimulatedTradeLog(clock=lambda: broker._require_now())
    strategy = OvernightStrategy(
        broker=broker, trade_log=trade_log,
        symbols=args.symbols,
        notional_per_trade=notional,
        starting_cash=starting_cash,
        calendar_module=cal,
    )
    driver = BacktestDriver(
        strategy=strategy, broker=broker, trade_log=trade_log,
        start=start, end=end, tick_interval_min=1,
    )

    print(f"Running BacktestDriver (tick_interval=1min)...")
    t0 = datetime.now(UTC)
    res = driver.run()
    elapsed_s = (datetime.now(UTC) - t0).total_seconds()

    # ---- analytics (mirrors real_bars.py format) -----------------------
    starting_eq = starting_cash
    ending_eq = res.equity[-1][1] if res.equity else starting_eq
    total_return_pct = (
        (ending_eq - starting_eq) / starting_eq * Decimal(100)
        if starting_eq > 0 else Decimal(0)
    )

    submits = [r for r in res.intents
                if r.payload.get("result") == "submitting"]
    closes  = [r for r in res.intents
                if r.payload.get("result") == "submitting_close"]
    fills   = [r for r in res.results
                if r.payload.get("status") == "filled"]

    # Pair entries with closes to compute per-trade stats.
    entry_fills_by_iid = {
        r.payload["intent_id"]: r for r in res.results
        if r.payload.get("intent_id", "").startswith("entry-")
        and r.payload.get("status") == "filled"
    }
    open_by_sym: dict[str, str] = {}
    rt_pnls: list[Decimal] = []
    hold_minutes: list[float] = []
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
                rt_pnls.append(Decimal(r.payload["realized_pnl"]))
                hold_minutes.append(
                    (r.ts - entry.ts).total_seconds() / 60.0
                )
            except (KeyError, ValueError, TypeError):
                continue

    # Peak / trough equity for drawdown.
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
    print(f"Entry intents        : {len(submits):,}")
    print(f"Exit intents         : {len(closes):,}")
    print(f"Fill records         : {len(fills):,}")
    print(f"Paired round-trips   : {len(rt_pnls):,}")
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
        if hold_minutes:
            print(f"Hold time (min)      : "
                  f"avg={sum(hold_minutes)/len(hold_minutes):.0f}  "
                  f"min={min(hold_minutes):.0f}  "
                  f"max={max(hold_minutes):.0f}")
        print()

    # Save records (for offline cost-adjusted re-analysis).
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
