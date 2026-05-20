"""Phase-4 SPIKE — offline cost-adjusted re-analysis of saved spike runs.

Reads a spike's ``--save-records`` JSONL output (intents/results/incidents/
equity) and applies a realistic cost model on top of the perfect-fill
P&L the spike already recorded. Outputs PERFECT-FILL vs COST-ADJUSTED
side-by-side so we can see how much of the strategy's apparent edge
survives realistic execution.

This is a stand-in for full Phase-2 execution realism. Phase 2 will
model costs INSIDE the backtest loop (so fills can be partial, stops
can slip on fast moves, etc.). This analyzer only post-adjusts the
final per-trade P&L — good enough for cheap "would costs kill it?"
decisions, not a substitute for Phase 2.

Cost model
----------
Per round-trip:
    spread_slippage = (half_spread_bps + slippage_bps) / 10000
                      * (entry_notional + exit_notional)
    commission      = commission_per_share * qty * 2
                      + commission_per_trade * 2
    total_cost      = spread_slippage + commission

Defaults: half_spread=1bp, slippage=1bp, commission=0 (Alpaca-style).
Round-trip on notional ≈ 4bps. On a $2,500 trade that's ~$1.

Usage
-----
    .venv-research/bin/python -m research.spike.analyze \\
        /tmp/spike_2024.jsonl

With custom costs:
    .venv-research/bin/python -m research.spike.analyze \\
        /tmp/spike_2024.jsonl --half-spread-bps 2 --slippage-bps 3
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path


# --- IO -------------------------------------------------------------------

def _read_jsonl(path: Path) -> dict[str, list]:
    """Load the four record streams from a spike's saved JSONL."""
    out: dict[str, list] = {
        "intent": [], "result": [], "incident": [], "equity": [],
    }
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            kind = r.get("record_kind")
            if kind in out:
                out[kind].append(r)
    return out


# --- trade pairing --------------------------------------------------------

def _pair_trades(records: dict[str, list]) -> list[dict]:
    """Pair entry fills with subsequent close-RESULT records.

    Mirrors ``real_bars._analyze_trades``: one position per symbol at a
    time, so a per-symbol stack works. Close RESULTs carry the
    ``realized_pnl`` and exit ``reason`` that the strategy already
    computed at fill time.
    """
    entry_fills_by_iid = {
        r["payload"]["intent_id"]: r for r in records["result"]
        if r["payload"].get("intent_id", "").startswith("entry-")
        and r["payload"].get("status") == "filled"
    }
    open_by_sym: dict[str, str] = {}
    trades: list[dict] = []
    for r in records["result"]:
        iid = r["payload"].get("intent_id", "")
        parts = iid.split("-", 2)
        sym = parts[1] if len(parts) >= 2 else "?"
        if iid.startswith("entry-") and r["payload"].get("status") == "filled":
            open_by_sym[sym] = iid
        elif iid.startswith("close-") and r["payload"].get("status") == "filled":
            entry_iid = open_by_sym.pop(sym, None)
            if entry_iid is None:
                continue
            entry = entry_fills_by_iid.get(entry_iid)
            if entry is None:
                continue
            try:
                pnl = Decimal(r["payload"]["realized_pnl"])
                entry_px = Decimal(entry["payload"]["avg_fill_price"])
                exit_px = Decimal(r["payload"]["avg_fill_price"])
                qty = int(entry["payload"]["filled_qty"])
            except (KeyError, ValueError, TypeError):
                continue
            trades.append({
                "symbol":       sym,
                "entry_ts":     entry["ts"],
                "exit_ts":      r["ts"],
                "entry_price":  entry_px,
                "exit_price":   exit_px,
                "qty":          qty,
                "pnl_perfect":  pnl,
                "reason":       r["payload"].get("reason", "?"),
            })
    return trades


# --- cost model ----------------------------------------------------------

def _roundtrip_cost(
    entry_px: Decimal, exit_px: Decimal, qty: int, *,
    half_spread_bps: Decimal, slippage_bps: Decimal,
    commission_per_share: Decimal, commission_per_trade: Decimal,
) -> Decimal:
    """Total round-trip cost: half-spread + slippage on both sides + commissions."""
    bps_per_side = half_spread_bps + slippage_bps
    entry_notional = entry_px * Decimal(qty)
    exit_notional = exit_px * Decimal(qty)
    spread_slippage = (bps_per_side / Decimal(10000)) * (entry_notional + exit_notional)
    commission = (
        commission_per_share * Decimal(qty) * Decimal(2)
        + commission_per_trade * Decimal(2)
    )
    return spread_slippage + commission


# --- summaries ------------------------------------------------------------

def _summarize(trades: list[dict], pnl_key: str, *, starting_cash: Decimal) -> dict:
    pnls = [t[pnl_key] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    breakeven = [p for p in pnls if p == 0]
    n = len(trades)
    total = sum(pnls, Decimal(0))

    avg_win = sum(wins, Decimal(0)) / Decimal(len(wins)) if wins else Decimal(0)
    avg_loss = (
        sum(losses, Decimal(0)) / Decimal(len(losses))
        if losses else Decimal(0)
    )
    win_rate = Decimal(len(wins)) / Decimal(n) * Decimal(100) if n else Decimal(0)
    expectancy = total / Decimal(n) if n else Decimal(0)
    payoff = (
        avg_win / abs(avg_loss)
        if wins and losses else Decimal(0)
    )
    breakeven_wr = (
        Decimal(1) / (Decimal(1) + payoff) * Decimal(100)
        if payoff > 0 else Decimal(0)
    )
    total_pct = total / starting_cash * Decimal(100) if starting_cash > 0 else Decimal(0)

    return {
        "n":            n,
        "wins":         len(wins),
        "losses":       len(losses),
        "breakeven":    len(breakeven),
        "win_rate":     win_rate,
        "total":        total,
        "total_pct":    total_pct,
        "expectancy":   expectancy,
        "avg_win":      avg_win,
        "avg_loss":     avg_loss,
        "payoff":       payoff,
        "breakeven_wr": breakeven_wr,
    }


def _print_block(label: str, s: dict, *, starting_cash: Decimal) -> None:
    print(f"{label}")
    print(f"  Wins / Losses / BE   : {s['wins']} / {s['losses']} / {s['breakeven']}")
    print(f"  Win rate             : {s['win_rate']:.1f}%")
    print(f"  Total P&L            : ${s['total']:>10,.2f}  "
          f"({s['total_pct']:+.3f}% of ${starting_cash:,.0f})")
    print(f"  Avg P&L per trade    : ${s['expectancy']:>10,.2f}")
    print(f"  Avg win / Avg loss   : ${s['avg_win']:>8,.2f} / ${s['avg_loss']:>8,.2f}")
    if s["payoff"] > 0:
        print(f"  Payoff ratio         : {s['payoff']:.2f}")
        print(f"  Breakeven WR needed  : {s['breakeven_wr']:.1f}%")


# --- main -----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Cost-adjusted re-analysis of a saved spike run (JSONL)",
    )
    ap.add_argument("jsonl", help="Path to spike --save-records output (JSONL)")
    ap.add_argument(
        "--half-spread-bps", type=float, default=1.0,
        help="Half of bid-ask spread paid per side, in bps (default 1.0 — "
             "typical for liquid US equities at retail tier)",
    )
    ap.add_argument(
        "--slippage-bps", type=float, default=1.0,
        help="Additional execution slippage per side, in bps (default 1.0 — "
             "limit orders with modest fill latency)",
    )
    ap.add_argument(
        "--commission-per-share", type=float, default=0.0,
        help="Commission $/share per side (default 0.0, Alpaca commission-free)",
    )
    ap.add_argument(
        "--commission-per-trade", type=float, default=0.0,
        help="Commission $/trade per side (default 0.0, Alpaca)",
    )
    ap.add_argument(
        "--starting-cash", type=float, default=100000.0,
        help="Starting equity for return-percent calc (default 100000)",
    )
    args = ap.parse_args(argv)

    path = Path(args.jsonl)
    if not path.exists():
        print(f"ERROR: {path} not found", file=sys.stderr)
        return 2

    starting_cash = Decimal(str(args.starting_cash))
    half_spread = Decimal(str(args.half_spread_bps))
    slippage = Decimal(str(args.slippage_bps))
    comm_per_share = Decimal(str(args.commission_per_share))
    comm_per_trade = Decimal(str(args.commission_per_trade))

    # Header
    print("=" * 70)
    print(f"COST-ADJUSTED RE-ANALYSIS")
    print(f"File: {path}")
    print("=" * 70)
    print("Cost model:")
    print(f"  Half-spread          : {half_spread} bps/side")
    print(f"  Slippage             : {slippage} bps/side")
    print(f"  Commission           : ${comm_per_share}/share/side "
          f"+ ${comm_per_trade}/trade/side")
    print(f"  Round-trip notional  : ~{(half_spread + slippage) * 2} bps")
    print()

    records = _read_jsonl(path)
    trades = _pair_trades(records)
    if not trades:
        print("No paired trades found in records.")
        return 0

    # Apply cost adjustment per trade.
    total_cost = Decimal(0)
    total_notional = Decimal(0)
    for t in trades:
        cost = _roundtrip_cost(
            t["entry_price"], t["exit_price"], t["qty"],
            half_spread_bps=half_spread, slippage_bps=slippage,
            commission_per_share=comm_per_share,
            commission_per_trade=comm_per_trade,
        )
        t["cost"] = cost
        t["pnl_adjusted"] = t["pnl_perfect"] - cost
        total_cost += cost
        total_notional += t["entry_price"] * Decimal(t["qty"])

    n = len(trades)
    avg_notional = total_notional / Decimal(n)
    avg_cost = total_cost / Decimal(n)
    print(f"Paired trades        : {n}")
    print(f"Avg trade notional   : ${avg_notional:>12,.2f}")
    print(f"Avg cost per trade   : ${avg_cost:>12,.4f}")
    print(f"Total cost charged   : ${total_cost:>12,.2f}")
    print()

    # Side-by-side summaries.
    perfect = _summarize(trades, "pnl_perfect", starting_cash=starting_cash)
    adjusted = _summarize(trades, "pnl_adjusted", starting_cash=starting_cash)
    print("─" * 70)
    _print_block("PERFECT FILL (as the spike reported):",
                  perfect, starting_cash=starting_cash)
    print()
    print("─" * 70)
    _print_block("AFTER COSTS:", adjusted, starting_cash=starting_cash)
    print()

    # Delta + verdict.
    print("─" * 70)
    print("DELTA (perfect → after-costs):")
    print(f"  Expectancy/trade     : ${perfect['expectancy']:>8,.2f}  →  "
          f"${adjusted['expectancy']:>8,.2f}   "
          f"({adjusted['expectancy'] - perfect['expectancy']:+,.2f})")
    print(f"  Total P&L            : ${perfect['total']:>10,.2f}  →  "
          f"${adjusted['total']:>10,.2f}   "
          f"({adjusted['total'] - perfect['total']:+,.2f})")
    print(f"  Win rate             : {perfect['win_rate']:.1f}%  →  "
          f"{adjusted['win_rate']:.1f}%   "
          f"({adjusted['win_rate'] - perfect['win_rate']:+.1f} pts)")
    print()
    print("Verdict: ", end="")
    if adjusted["expectancy"] > Decimal("0.50"):
        print("POSITIVE expectancy after costs — strategy is viable on this data.")
    elif adjusted["expectancy"] > Decimal("-0.50"):
        print("MARGINAL — close to breakeven; outcome depends on cost assumptions.")
    else:
        print("NEGATIVE after costs — strategy not viable as configured.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
