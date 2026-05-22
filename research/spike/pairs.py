"""Phase-4 SPIKE — pairs / statistical arbitrage (Family C).

==================================================================
⚠️  PROVISIONAL SPIKE — NOT A VERDICT  ⚠️
==================================================================
Same Phase-1 spirit as the other spikes: perfect fills, no real
two-leg execution, verdict-gate 0.6 open. This is the cheapest honest
test of whether cointegrated-pair spreads mean-revert profitably.

Synthetic-spread model
----------------------
Each pair (A, B) is modeled as ONE position on the spread:

    spread_t = log(A_t) - beta * log(B_t)

where ``beta`` is the rolling OLS hedge ratio. "Long the spread" means
long A / short B; "short the spread" means short A / long B. P&L is
computed at the spread level:

    pnl = notional_per_leg * (spread_exit - spread_entry) * direction

(direction = +1 long-spread, -1 short-spread). This avoids needing
real short mechanics in the SimulatedBroker — appropriate for a
viability test. If pairs proves out, THEN we build true two-leg
execution.

ANTI-LEAK (the critical correctness issue for pairs backtests)
--------------------------------------------------------------
Most naive pairs backtests are WRONG because they estimate the
cointegration hedge ratio over the FULL sample — look-ahead bias.
Here, at each decision day ``t``:

  * beta and the spread mean/std are estimated over the STRICTLY
    PRIOR window [t-lookback, t-1]
  * today's spread (using those prior-window params) is measured
    against that prior distribution to get z_t
  * we trade at today's close on z_t

No future data ever touches the parameters. Documented and enforced
in ``_pair_backtest``.

Trading logic (literature-standard, LOCKED)
-------------------------------------------
  Entry: |z| > 2.0
    z > +2  -> SHORT the spread (bet it falls back to mean)
    z < -2  -> LONG the spread  (bet it rises back to mean)
  Exit (whichever first):
    |z| < 0.5    -> reverted (take profit)
    |z| > 3.5    -> stop-loss (spread diverging / relationship broke)
    held >= 30d  -> timeout
  Hedge ratio: rolling OLS over 60 trading days (numpy lstsq).

Cost model
----------
A pairs round-trip touches 4 single-leg transactions (enter A, enter B,
exit A, exit B). Cost = 4 * (half_spread + slippage)/1e4 * notional_per_leg.
At default 1+1 bp and $5k/leg that's ~$4 per pair round-trip.

Usage
-----
    cd /opt/trading-bot
    source <(grep -E '^ALPACA_API_(KEY|SECRET)=' autoflow/.env | sed 's/^/export /')
    .venv-research/bin/python -m research.spike.pairs \\
        --start 2024-01-01 --end 2024-12-31 \\
        --pairs QQQ:SPY AAPL:MSFT GOOG:META KO:PEP XOM:CVX V:MA HD:LOW \\
        --save-records /tmp/spike_pairs_2024.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
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


# --- daily close series --------------------------------------------------

def _daily_closes(symbol: str, start: datetime, end: datetime, cache_dir: str | Path):
    """Cache-fetch 1m bars; resample to daily; return close Series indexed
    by session date (python ``date``)."""
    import pandas as pd
    from research.data.cache import cache_1m_bars
    from research.data.resample import resample

    df_1m = cache_1m_bars(symbol, start, end, cache_dir=cache_dir)
    df_d = resample(df_1m, 390)
    if df_d.empty:
        return pd.Series(dtype="float64")
    s = df_d["close"].astype(float)
    s.index = [ts.date() for ts in s.index]  # index by session date
    return s


# --- core backtest (anti-leak rolling window) ----------------------------

def _pair_backtest(
    name: str,
    closes_a, closes_b, *,
    lookback: int,
    entry_z: float,
    exit_z: float,
    stop_z: float,
    max_hold_days: int,
    notional_per_leg: Decimal,
    start: date,
    end: date,
) -> list[dict]:
    """Run the synthetic-spread strategy on one pair. Returns a list of
    closed round-trips (dicts).

    Anti-leak invariant: at decision index t, beta/mean/std use ONLY
    [t-lookback, t-1]; today's spread is measured against that prior
    distribution. No future data touches the parameters.
    """
    import numpy as np
    import pandas as pd

    df = pd.DataFrame({"a": closes_a, "b": closes_b}).dropna()
    if len(df) < lookback + 5:
        return []
    la = np.log(df["a"].to_numpy())
    lb = np.log(df["b"].to_numpy())
    dates = list(df.index)
    n = len(df)
    N = float(notional_per_leg)

    trades: list[dict] = []
    pos: dict | None = None

    for t in range(lookback, n):
        d = dates[t]
        # Prior window [t-lookback, t-1] — strictly historical.
        win_a = la[t - lookback:t]
        win_b = lb[t - lookback:t]
        # OLS: la = beta*lb + alpha (slope first from polyfit deg-1).
        beta, alpha = np.polyfit(win_b, win_a, 1)
        spread_win = win_a - beta * win_b
        mu = float(spread_win.mean())
        sigma = float(spread_win.std())
        if sigma <= 0 or not np.isfinite(sigma):
            continue
        # Today's spread using prior-window beta; measured vs prior dist.
        spread_t = float(la[t] - beta * lb[t])
        z = (spread_t - mu) / sigma

        in_window = (start <= d <= end)

        if pos is None:
            if not in_window:
                continue
            if z > entry_z:
                pos = {"direction": -1, "entry_date": d, "entry_spread": spread_t,
                       "entry_z": z, "beta": float(beta)}
            elif z < -entry_z:
                pos = {"direction": +1, "entry_date": d, "entry_spread": spread_t,
                       "entry_z": z, "beta": float(beta)}
        else:
            days_held = (d - pos["entry_date"]).days
            reason: str | None = None
            if pos["direction"] == +1:           # long spread, want z -> 0 from below
                if z > -exit_z:
                    reason = "reverted"
                elif z < -stop_z:
                    reason = "stop_diverged"
            else:                                # short spread, want z -> 0 from above
                if z < exit_z:
                    reason = "reverted"
                elif z > stop_z:
                    reason = "stop_diverged"
            if reason is None and days_held >= max_hold_days:
                reason = "max_hold"
            if reason is not None:
                # P&L MUST use the hedge ratio locked in at ENTRY — that's
                # the position we actually held. The z-score signal above
                # uses the current rolling beta (correct for the decision),
                # but exit_value re-prices today's legs with beta_entry so
                # (exit_value - entry_spread) is the true spread P&L of a
                # fixed-hedge position. Using the current beta here would be
                # a real accounting error (caught by the synthetic test).
                beta_entry = pos["beta"]
                exit_value = float(la[t] - beta_entry * lb[t])
                pnl = N * (exit_value - pos["entry_spread"]) * pos["direction"]
                trades.append({
                    "pair":          name,
                    "entry_date":    pos["entry_date"],
                    "exit_date":     d,
                    "direction":     pos["direction"],
                    "entry_spread":  pos["entry_spread"],
                    "exit_spread":   spread_t,
                    "entry_z":       pos["entry_z"],
                    "exit_z":        z,
                    "beta":          pos["beta"],
                    "days_held":     days_held,
                    "exit_value":    exit_value,
                    "pnl_perfect":   Decimal(str(pnl)),
                    "reason":        reason,
                })
                pos = None

    # Force-close any position still open at the last available bar.
    # Use the ENTRY beta for the exit valuation (same fixed-hedge logic
    # as the normal exit path above).
    if pos is not None:
        t = n - 1
        d = dates[t]
        beta_entry = pos["beta"]
        exit_value = float(la[t] - beta_entry * lb[t])
        pnl = N * (exit_value - pos["entry_spread"]) * pos["direction"]
        trades.append({
            "pair":         name,
            "entry_date":   pos["entry_date"],
            "exit_date":    d,
            "direction":    pos["direction"],
            "entry_spread": pos["entry_spread"],
            "exit_spread":  spread_t,
            "entry_z":      pos["entry_z"],
            "exit_z":       None,
            "beta":         pos["beta"],
            "days_held":    (d - pos["entry_date"]).days,
            "pnl_perfect":  Decimal(str(pnl)),
            "reason":       "force_close_end",
        })
    return trades


# --- main ----------------------------------------------------------------

def _banner(line: str) -> str:
    return "=" * 70 + "\n" + line + "\n" + "=" * 70


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase-4 pairs/stat-arb spike — Family C.")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    ap.add_argument(
        "--pairs", nargs="+",
        default=["QQQ:SPY", "AAPL:MSFT", "GOOG:META",
                 "KO:PEP", "XOM:CVX", "V:MA", "HD:LOW"],
        help="Pairs as A:B tokens",
    )
    ap.add_argument("--notional-per-leg", default="5000")
    ap.add_argument("--cache-dir", default="/tmp/bt-cache")
    ap.add_argument("--history-pad-days", type=int, default=120,
                     help="Days before --start for the rolling lookback warm-up")
    # Literature-locked params.
    ap.add_argument("--lookback", type=int, default=60,
                     help="Rolling window (trading days) for beta + z-score")
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--exit-z", type=float, default=0.5)
    ap.add_argument("--stop-z", type=float, default=3.5)
    ap.add_argument("--max-hold-days", type=int, default=30)
    ap.add_argument("--half-spread-bps", type=float, default=1.0)
    ap.add_argument("--slippage-bps", type=float, default=1.0)
    ap.add_argument("--save-records")
    args = ap.parse_args(argv)

    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)
    start_dt = datetime.combine(start_d, datetime.min.time(), tzinfo=UTC)
    end_dt = datetime.combine(end_d, datetime.min.time(),
                               tzinfo=UTC) + timedelta(hours=23, minutes=59)
    history_start = start_dt - timedelta(days=args.history_pad_days)

    found_env = _load_alpaca_env_into_process()
    if "ALPACA_API_KEY" not in os.environ or "ALPACA_API_SECRET" not in os.environ:
        print("ERROR: ALPACA_API_KEY/SECRET not in environment.", file=sys.stderr)
        return 2

    notional = Decimal(args.notional_per_leg)
    pairs = [tuple(p.split(":")) for p in args.pairs]
    symbols = sorted({s for pr in pairs for s in pr})

    print(_banner("⚠️  PAIRS / STAT-ARB SPIKE — NOT A VERDICT  ⚠️"))
    print("Hypothesis: cointegrated-pair spreads mean-revert profitably.")
    print("Synthetic-spread model; rolling-window anti-leak hedge ratio.")
    print("LITERATURE CONFIG LOCKED — no tuning across runs.")
    print("=" * 70)
    print(f"Window           : {args.start} → {args.end} (UTC)")
    print(f"Pairs            : {', '.join(args.pairs)}")
    print(f"Notional/leg     : ${args.notional_per_leg} (gross ${float(notional)*2:,.0f}/pair)")
    print(f"Lookback         : {args.lookback} trading days")
    print(f"Entry/Exit/Stop z: {args.entry_z} / {args.exit_z} / {args.stop_z}")
    print(f"Max hold days    : {args.max_hold_days}")
    print(f"Cost (per leg/sd): {args.half_spread_bps + args.slippage_bps} bps "
          f"(4 legs/round-trip)")
    print(f"Env source       : {sorted(found_env.keys())}")
    print()

    print("Fetching daily closes...")
    closes: dict[str, Any] = {}
    for sym in symbols:
        s = _daily_closes(sym, history_start, end_dt, args.cache_dir)
        closes[sym] = s
        print(f"  {sym:5s}  daily_bars={len(s)}")

    # Run each pair.
    print()
    print("Running pair backtests...")
    all_trades: list[dict] = []
    per_pair: dict[str, list[dict]] = {}
    for a, b in pairs:
        name = f"{a}:{b}"
        if closes.get(a) is None or closes.get(b) is None \
                or len(closes[a]) == 0 or len(closes[b]) == 0:
            print(f"  {name:14s}  SKIP (missing data)")
            continue
        tr = _pair_backtest(
            name, closes[a], closes[b],
            lookback=args.lookback, entry_z=args.entry_z,
            exit_z=args.exit_z, stop_z=args.stop_z,
            max_hold_days=args.max_hold_days,
            notional_per_leg=notional, start=start_d, end=end_d,
        )
        per_pair[name] = tr
        all_trades.extend(tr)
        print(f"  {name:14s}  trades={len(tr)}")

    if not all_trades:
        print("\nNo trades generated.")
        return 0

    # Cost: 4 single-leg transactions per pair round-trip.
    bps_per_side = Decimal(str(args.half_spread_bps)) + Decimal(str(args.slippage_bps))
    cost_per_rt = Decimal(4) * (bps_per_side / Decimal(10000)) * notional
    for t in all_trades:
        t["cost"] = cost_per_rt
        t["pnl_adjusted"] = t["pnl_perfect"] - cost_per_rt

    # --- report ---------------------------------------------------------
    def _stats(trades, key):
        pnls = [t[key] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        n = len(trades)
        total = sum(pnls, Decimal(0))
        avg = total / Decimal(n) if n else Decimal(0)
        avg_w = sum(wins, Decimal(0)) / Decimal(len(wins)) if wins else Decimal(0)
        avg_l = sum(losses, Decimal(0)) / Decimal(len(losses)) if losses else Decimal(0)
        wr = Decimal(len(wins)) / Decimal(n) * Decimal(100) if n else Decimal(0)
        return {"n": n, "wins": len(wins), "losses": len(losses),
                "total": total, "avg": avg, "avg_w": avg_w, "avg_l": avg_l, "wr": wr}

    perfect = _stats(all_trades, "pnl_perfect")
    adj = _stats(all_trades, "pnl_adjusted")
    reasons = Counter(t["reason"] for t in all_trades)

    print()
    print(_banner(f"RESULTS  (n={perfect['n']} round-trips)"))
    print(f"Cost per round-trip  : ${cost_per_rt:.2f} (4 legs)")
    print()
    print("PERFECT FILL:")
    print(f"  Win rate           : {perfect['wr']:.1f}%  ({perfect['wins']}/{perfect['n']})")
    print(f"  Total P&L          : ${perfect['total']:>10,.2f}")
    print(f"  Avg per trade      : ${perfect['avg']:>10,.2f}")
    print(f"  Avg win / loss     : ${perfect['avg_w']:>8,.2f} / ${perfect['avg_l']:>8,.2f}")
    print()
    print("AFTER COSTS (4-leg):")
    print(f"  Win rate           : {adj['wr']:.1f}%  ({adj['wins']}/{adj['n']})")
    print(f"  Total P&L          : ${adj['total']:>10,.2f}  "
          f"({adj['total']/Decimal('100000')*Decimal(100):+.3f}% of $100k)")
    print(f"  Avg per trade      : ${adj['avg']:>10,.2f}")
    print(f"  Avg win / loss     : ${adj['avg_w']:>8,.2f} / ${adj['avg_l']:>8,.2f}")
    if adj["avg_l"] != 0:
        payoff = adj["avg_w"] / abs(adj["avg_l"])
        print(f"  Payoff ratio       : {payoff:.2f}")
        print(f"  Breakeven WR       : {Decimal(1)/(Decimal(1)+payoff)*Decimal(100):.1f}%")
    print()
    print("Exit-reason breakdown:")
    for reason, n in reasons.most_common():
        print(f"  {reason:18s} {n:>4}  ({n/perfect['n']*100:>5.1f}%)")
    print()
    print("Per-pair P&L (after costs):")
    for name in sorted(per_pair):
        tr = per_pair[name]
        if not tr:
            print(f"  {name:14s}  trades=  0")
            continue
        tot = sum((t["pnl_adjusted"] for t in tr), Decimal(0))
        w = sum(1 for t in tr if t["pnl_adjusted"] > 0)
        print(f"  {name:14s}  trades={len(tr):3d}  wins={w:3d}  "
              f"total=${tot:>9,.2f}  avg=${tot/Decimal(len(tr)):>7,.2f}")

    # Save JSONL (entry/close pairs in production-ish shape for quarterly).
    if args.save_records:
        out_path = Path(args.save_records)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            for t in all_trades:
                sym = t["pair"]
                entry_ts = datetime.combine(t["entry_date"], datetime.min.time(),
                                             tzinfo=UTC).isoformat()
                exit_ts = datetime.combine(t["exit_date"], datetime.min.time(),
                                            tzinfo=UTC).isoformat()
                iid_e = f"entry-{sym}-{entry_ts}"
                iid_c = f"close-{sym}-{exit_ts}"
                # Entry intent + result (avg_fill_price = gross pair notional
                # so a generic single-leg analyzer roughly approximates the
                # pair cost; the inline report above is authoritative).
                f.write(json.dumps({"record_kind": "intent", "ts": entry_ts,
                    "payload": {"intent_id": iid_e, "symbol": sym, "side": "buy",
                                "qty_requested": 1, "result": "submitting"}}) + "\n")
                f.write(json.dumps({"record_kind": "result", "ts": entry_ts,
                    "payload": {"intent_id": iid_e, "status": "filled",
                                "filled_qty": 1,
                                "avg_fill_price": str((notional * 2).quantize(Decimal("0.01")))}}) + "\n")
                f.write(json.dumps({"record_kind": "intent", "ts": exit_ts,
                    "payload": {"intent_id": iid_c, "symbol": sym, "side": "sell",
                                "qty_requested": 1, "result": "submitting_close"}}) + "\n")
                f.write(json.dumps({"record_kind": "result", "ts": exit_ts,
                    "payload": {"intent_id": iid_c, "status": "filled",
                                "filled_qty": 1,
                                "avg_fill_price": str((notional * 2).quantize(Decimal("0.01"))),
                                "reason": t["reason"],
                                "realized_pnl": str(t["pnl_perfect"].quantize(Decimal("0.01")))}}) + "\n")
        print(f"\nRecords saved: {out_path}")

    print()
    print(_banner("⚠️  REMINDER: PROVISIONAL — NOT A VERDICT  ⚠️"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
