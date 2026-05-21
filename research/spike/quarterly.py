"""Per-quarter (and optional per-month) P&L breakdown of spike JSONL files.

Reads paired round-trips from one or more ``--save-records`` JSONL
outputs, groups by quarter, and prints a chronologically-sorted
table. Used to check whether a strategy's measured edge is
*consistent* across periods or concentrated in a few outlier quarters.

Cost adjustment is applied uniformly per trade (same model as
``research.spike.analyze``).

Usage
-----
    .venv-research/bin/python -m research.spike.quarterly \\
        /tmp/spike_mr_2022_broad.jsonl \\
        /tmp/spike_mr_2023_broad.jsonl \\
        /tmp/spike_mr_2024_broad.jsonl

    # With monthly breakdown too:
    .venv-research/bin/python -m research.spike.quarterly --monthly ...

    # Different cost assumptions:
    .venv-research/bin/python -m research.spike.quarterly \\
        --half-spread-bps 2 --slippage-bps 2 ...
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path


# --- pairing (same shape as analyze.py) ----------------------------------

def _pair_trades(records: dict[str, list]) -> list[dict]:
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
                entry_ts = datetime.fromisoformat(entry["ts"])
                exit_ts = datetime.fromisoformat(r["ts"])
            except (KeyError, ValueError, TypeError):
                continue
            trades.append({
                "symbol":      sym,
                "entry_ts":    entry_ts,
                "exit_ts":     exit_ts,
                "entry_price": entry_px,
                "exit_price":  exit_px,
                "qty":         qty,
                "pnl_perfect": pnl,
            })
    return trades


def _read_jsonl(path: Path) -> dict[str, list]:
    out: dict[str, list] = {"intent": [], "result": [], "incident": [], "equity": []}
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


# --- cost model ----------------------------------------------------------

def _roundtrip_cost(
    entry_px: Decimal, exit_px: Decimal, qty: int, *,
    half_spread_bps: Decimal, slippage_bps: Decimal,
) -> Decimal:
    bps_per_side = half_spread_bps + slippage_bps
    return (bps_per_side / Decimal(10000)) * (
        entry_px * Decimal(qty) + exit_px * Decimal(qty)
    )


# --- grouping ------------------------------------------------------------

def _quarter_key(ts: datetime) -> str:
    q = (ts.month - 1) // 3 + 1
    return f"{ts.year}Q{q}"


def _month_key(ts: datetime) -> str:
    return f"{ts.year}-{ts.month:02d}"


def _group(trades: list[dict], key_fn) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in trades:
        # Group by ENTRY date — a trade "belongs" to the period it was opened.
        out.setdefault(key_fn(t["entry_ts"]), []).append(t)
    return out


# --- summary -------------------------------------------------------------

def _summary_row(period: str, ts: list[dict]) -> dict:
    n = len(ts)
    perfect_pnls = [t["pnl_perfect"] for t in ts]
    adj_pnls = [t["pnl_adjusted"] for t in ts]
    wins_p = [p for p in perfect_pnls if p > 0]
    wins_a = [p for p in adj_pnls if p > 0]
    wr_p = Decimal(len(wins_p)) / Decimal(n) * Decimal(100) if n else Decimal(0)
    wr_a = Decimal(len(wins_a)) / Decimal(n) * Decimal(100) if n else Decimal(0)
    return {
        "period":       period,
        "n":            n,
        "wr_perfect":   wr_p,
        "wr_adjusted":  wr_a,
        "total_perfect": sum(perfect_pnls, Decimal(0)),
        "total_adj":    sum(adj_pnls, Decimal(0)),
        "avg_perfect":  sum(perfect_pnls, Decimal(0)) / Decimal(n) if n else Decimal(0),
        "avg_adj":      sum(adj_pnls, Decimal(0)) / Decimal(n) if n else Decimal(0),
        "best":         max(adj_pnls),
        "worst":        min(adj_pnls),
    }


def _print_table(rows: list[dict], *, label: str) -> None:
    print()
    print("=" * 100)
    print(f"  {label}")
    print("=" * 100)
    print(f"  {'Period':<10} {'N':>4} {'WR%(perfect)':>12} {'WR%(adj)':>9}  "
          f"{'Total(perf)':>11} {'Total(adj)':>11}  "
          f"{'Avg/tr(perf)':>11} {'Avg/tr(adj)':>11}  "
          f"{'Best':>9} {'Worst':>9}")
    print(f"  {'-'*10} {'-'*4} {'-'*12} {'-'*9}  "
          f"{'-'*11} {'-'*11}  {'-'*11} {'-'*11}  "
          f"{'-'*9} {'-'*9}")
    for r in rows:
        print(
            f"  {r['period']:<10} {r['n']:>4} "
            f"{r['wr_perfect']:>11.1f}% {r['wr_adjusted']:>8.1f}%  "
            f"${r['total_perfect']:>10,.2f} ${r['total_adj']:>10,.2f}  "
            f"${r['avg_perfect']:>10,.2f} ${r['avg_adj']:>10,.2f}  "
            f"${r['best']:>8,.2f} ${r['worst']:>8,.2f}"
        )
    # Aggregate footer.
    if rows:
        n = sum(r["n"] for r in rows)
        wins_p = sum(int(r["wr_perfect"] / Decimal(100) * r["n"]) for r in rows)
        wins_a = sum(int(r["wr_adjusted"] / Decimal(100) * r["n"]) for r in rows)
        total_p = sum(r["total_perfect"] for r in rows)
        total_a = sum(r["total_adj"] for r in rows)
        avg_p = total_p / Decimal(n) if n else Decimal(0)
        avg_a = total_a / Decimal(n) if n else Decimal(0)
        wr_p = Decimal(wins_p) / Decimal(n) * Decimal(100) if n else Decimal(0)
        wr_a = Decimal(wins_a) / Decimal(n) * Decimal(100) if n else Decimal(0)
        print(f"  {'-'*10} {'-'*4} {'-'*12} {'-'*9}  "
              f"{'-'*11} {'-'*11}  {'-'*11} {'-'*11}")
        print(
            f"  {'TOTAL':<10} {n:>4} "
            f"{wr_p:>11.1f}% {wr_a:>8.1f}%  "
            f"${total_p:>10,.2f} ${total_a:>10,.2f}  "
            f"${avg_p:>10,.2f} ${avg_a:>10,.2f}"
        )


# --- main ----------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Per-quarter (and optional per-month) breakdown of "
                    "spike JSONL records.",
    )
    ap.add_argument("jsonl", nargs="+", help="One or more spike JSONL files")
    ap.add_argument("--monthly", action="store_true",
                     help="Also print per-month breakdown (in addition to quarterly)")
    ap.add_argument("--half-spread-bps", type=float, default=1.0)
    ap.add_argument("--slippage-bps", type=float, default=1.0)
    args = ap.parse_args(argv)

    half = Decimal(str(args.half_spread_bps))
    slip = Decimal(str(args.slippage_bps))

    # Aggregate trades from all input files.
    all_trades: list[dict] = []
    for p in args.jsonl:
        path = Path(p)
        if not path.exists():
            print(f"WARNING: {path} not found, skipping", file=sys.stderr)
            continue
        records = _read_jsonl(path)
        all_trades.extend(_pair_trades(records))

    if not all_trades:
        print("No trades found.")
        return 0

    # Apply cost adjustment in place.
    for t in all_trades:
        cost = _roundtrip_cost(
            t["entry_price"], t["exit_price"], t["qty"],
            half_spread_bps=half, slippage_bps=slip,
        )
        t["cost"] = cost
        t["pnl_adjusted"] = t["pnl_perfect"] - cost

    # Sort by entry timestamp so groups print chronologically.
    all_trades.sort(key=lambda t: t["entry_ts"])

    print(f"Total trades   : {len(all_trades)}")
    print(f"Date range     : {all_trades[0]['entry_ts'].date()} "
          f"→ {all_trades[-1]['entry_ts'].date()}")
    print(f"Cost model     : half_spread={half}bp + slippage={slip}bp = "
          f"{(half + slip) * 2}bp round-trip")

    # Quarterly view.
    qgroups = _group(all_trades, _quarter_key)
    qrows = [_summary_row(period, qgroups[period])
             for period in sorted(qgroups.keys())]
    _print_table(qrows, label="QUARTERLY")

    # Monthly view (optional).
    if args.monthly:
        mgroups = _group(all_trades, _month_key)
        mrows = [_summary_row(period, mgroups[period])
                  for period in sorted(mgroups.keys())]
        _print_table(mrows, label="MONTHLY")

    # Edge-consistency diagnostics.
    print()
    print("=" * 70)
    print("  CONSISTENCY DIAGNOSTICS")
    print("=" * 70)
    positive_qs = [r for r in qrows if r["total_adj"] > 0]
    negative_qs = [r for r in qrows if r["total_adj"] < 0]
    breakeven_qs = [r for r in qrows if r["total_adj"] == 0]
    print(f"  Positive quarters : {len(positive_qs)} / {len(qrows)}")
    print(f"  Negative quarters : {len(negative_qs)} / {len(qrows)}")
    print(f"  Breakeven quarters: {len(breakeven_qs)} / {len(qrows)}")
    if qrows:
        best_q = max(qrows, key=lambda r: r["total_adj"])
        worst_q = min(qrows, key=lambda r: r["total_adj"])
        print(f"  Best quarter      : {best_q['period']:<10} "
              f"${best_q['total_adj']:>10,.2f}")
        print(f"  Worst quarter     : {worst_q['period']:<10} "
              f"${worst_q['total_adj']:>10,.2f}")
        # Concentration check: top quarter as fraction of total.
        total_adj = sum(r["total_adj"] for r in qrows)
        if total_adj != 0:
            top_share = best_q["total_adj"] / total_adj * Decimal(100)
            print(f"  Top quarter share : {top_share:.1f}% of total P&L "
                  f"(lower = more consistent edge)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
