"""Phase-4 SPIKE — peek at strategy behavior on real bars.

==================================================================
⚠️  PROVISIONAL — NOT A VERDICT  ⚠️
==================================================================
Phase-1 limitations are in force on EVERY number this script prints:
  • Perfect fills at intent.limit_price (no slippage, no fees)
  • spread_bps = 0 (the production spread_too_wide gate never fires
    while running through SimulatedBroker)
  • Verdict-gate caveat OPEN — sub-task 0.6 (anti-leak hardening
    tests) has not closed; future-bar safety is asserted by the
    accessor but not yet pinned by exhaustive tests.

DO NOT trade real money on these numbers. The purpose is purely
informational: see whether the strategy looks plausible at all under
perfect-fill paper, so we can prioritise next cycle's work:
  • If it makes money even under perfect fills → invest in 0.6 +
    Phase 2 execution realism (slippage / commissions) to harden
    the verdict.
  • If it loses badly under perfect fills → no amount of slippage
    modeling fixes that. Pivot to signal redesign.

Usage
-----
On the VPS, with research venv:
    cd /opt/trading-bot
    source <(grep -E '^ALPACA_API_(KEY|SECRET)=' autoflow/.env | sed 's/^/export /')
    .venv-research/bin/python -m research.spike.real_bars \\
        --start 2025-03-01 --end 2025-03-31 \\
        --symbols AAPL MSFT --cash 100000
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

UTC = timezone.utc


# --- env loading ----------------------------------------------------------

def _load_alpaca_env_into_process() -> dict[str, str]:
    """Read ALPACA_API_KEY/SECRET from autoflow/.env into os.environ.

    Tries VPS path first, then local fallback. Idempotent (won't
    overwrite existing env vars). Returns the dict of what was found.
    """
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
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() in ("ALPACA_API_KEY", "ALPACA_API_SECRET"):
                found[k.strip()] = v.strip().strip('"').strip("'")
        if found:
            break
    for k, v in found.items():
        os.environ.setdefault(k, v)
    return found


# --- bar pipeline ---------------------------------------------------------

def _fetch_and_resample(
    symbol: str, start: datetime, end: datetime,
    cache_dir: str | Path,
) -> dict[int, "object"]:
    """Cache-fetch 1m bars then resample to 5/15/60-minute frames."""
    from research.data.cache import cache_1m_bars
    from research.data.resample import resample

    df_1m = cache_1m_bars(symbol, start, end, cache_dir=cache_dir)
    return {tf: resample(df_1m, tf) for tf in (5, 15, 60)}


# --- metrics --------------------------------------------------------------

def _max_drawdown(equity: list[tuple[datetime, Decimal]],
                  starting: Decimal) -> Decimal:
    """Peak-to-trough drawdown in percent."""
    if not equity:
        return Decimal(0)
    peak = starting
    max_dd = Decimal(0)
    for _, eq in equity:
        if eq > peak:
            peak = eq
        if peak > 0:
            dd = (peak - eq) / peak * Decimal(100)
            if dd > max_dd:
                max_dd = dd
    return max_dd


# --- main -----------------------------------------------------------------

def _banner(line: str) -> str:
    return "=" * 70 + "\n" + line + "\n" + "=" * 70


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Phase-4 spike — real-bar peek at strategy behaviour"
    )
    ap.add_argument("--start", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    ap.add_argument("--symbols", nargs="+", default=["AAPL", "MSFT"])
    ap.add_argument("--cash", default="100000", help="Starting cash (Decimal str)")
    ap.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "sample_config.yaml"),
        help="Strategy config YAML",
    )
    ap.add_argument(
        "--cache-dir", default="/tmp/bt-cache",
        help="Where to cache 1-minute parquet bars",
    )
    ap.add_argument(
        "--history-pad-days", type=int, default=180,
        help="Calendar days of bar history to prefetch before --start "
             "(must cover min_bars_trend_tf * _CAL_BUFFER ≈ 140 days)",
    )
    args = ap.parse_args(argv)

    # Window
    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)
    start = datetime.combine(start_d, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(end_d, datetime.min.time(),
                            tzinfo=UTC) + timedelta(hours=23, minutes=59)
    if end <= start:
        print("ERROR: --end must be strictly after --start", file=sys.stderr)
        return 2
    history_start = start - timedelta(days=args.history_pad_days)

    # Env
    found_env = _load_alpaca_env_into_process()
    if "ALPACA_API_KEY" not in os.environ or "ALPACA_API_SECRET" not in os.environ:
        print(
            "ERROR: ALPACA_API_KEY / ALPACA_API_SECRET not in environment. "
            "Source autoflow/.env first.",
            file=sys.stderr,
        )
        return 2

    # Banner
    print(_banner("⚠️  PROVISIONAL SPIKE — NOT A VERDICT  ⚠️"))
    print("Phase-1 limitations in force:")
    print("  • perfect fills at intent.limit_price (no slippage, no fees)")
    print("  • spread_bps = 0 (spread_too_wide gate never fires)")
    print("  • verdict-gate caveat OPEN (0.6 anti-leak hardening not closed)")
    print("DO NOT trade real money on these numbers.")
    print("=" * 70)
    print(f"Backtest window  : {args.start} → {args.end}  (UTC)")
    print(f"History pad      : {args.history_pad_days} days "
          f"(fetch from {history_start.date()})")
    print(f"Symbols          : {', '.join(args.symbols)}")
    print(f"Starting cash    : ${args.cash}")
    print(f"Config           : {args.config}")
    print(f"Cache dir        : {args.cache_dir}")
    print(f"Env source       : {sorted(found_env.keys())}")
    print()

    # Fetch + resample
    print("Fetching/resampling bars...")
    bars: dict = {}
    for sym in args.symbols:
        frames = _fetch_and_resample(sym, history_start, end, args.cache_dir)
        for tf_min, df in frames.items():
            bars[(sym, tf_min)] = df
        print(f"  {sym:5s}  5m={len(frames[5]):6d}  "
              f"15m={len(frames[15]):6d}  1h={len(frames[60]):6d}")

    # Run backtest
    print()
    print("Running BacktestDriver...")
    from research.engine.driver import BacktestDriver
    t0 = datetime.now(UTC)
    drv = BacktestDriver.build(
        config_path=args.config, bars=bars,
        start=start, end=end, starting_cash=Decimal(args.cash),
        env={"TEST_ALPACA_API_KEY": "k", "TEST_ALPACA_API_SECRET": "s"},
    )
    res = drv.run()
    elapsed_s = (datetime.now(UTC) - t0).total_seconds()

    # Metrics
    starting_eq = Decimal(args.cash)
    ending_eq = res.equity[-1][1] if res.equity else starting_eq
    total_return_pct = (
        (ending_eq - starting_eq) / starting_eq * Decimal(100)
        if starting_eq > 0 else Decimal(0)
    )
    max_dd = _max_drawdown(list(res.equity), starting_eq)

    submit_intents = [
        r for r in res.intents if r.payload.get("result") == "submitting"
    ]
    deny_intents = [
        r for r in res.intents if r.payload.get("result") == "denied"
    ]
    fills = [r for r in res.results if r.payload.get("status") == "filled"]

    # Deny reason breakdown
    deny_reasons = Counter(r.payload.get("deny_reason") for r in deny_intents)
    signal_no_signal = Counter(
        r.payload.get("reason") for r in res.incidents
        if r.payload.get("kind") == "DIAGNOSTIC"
        and r.payload.get("decision") == "no_signal"
    )

    # Report
    print()
    print(_banner("RESULTS"))
    print(f"Wall-clock elapsed : {elapsed_s:.1f}s")
    print(f"Driver steps       : {res.steps:,}")
    print(f"Equity start       : ${starting_eq:>12,.2f}")
    print(f"Equity end         : ${ending_eq:>12,.2f}")
    print(f"Total return       : {total_return_pct:+.2f}%")
    print(f"Max drawdown       : {max_dd:.2f}%")
    print()
    print(f"Entry intents (submit) : {len(submit_intents):,}")
    print(f"Entry intents (deny)   : {len(deny_intents):,}")
    print(f"Entry fills (filled)   : {len(fills):,}")
    print(f"Incident records       : {len(res.incidents):,}")
    print()

    if deny_reasons:
        print("Top deny reasons (risk-gate, fired AFTER signal passed):")
        for reason, n in deny_reasons.most_common(10):
            print(f"  {reason:35s} {n:>8,}")
        print()

    if signal_no_signal:
        print("Top no-signal reasons (signal didn't fire — pre-risk):")
        for reason, n in signal_no_signal.most_common(10):
            print(f"  {reason:35s} {n:>8,}")
        print()

    # Per-symbol fill summary
    if fills:
        by_sym: dict[str, list] = {}
        for f in fills:
            # intent_id format: "entry-{SYM}-{ISO_TS}"
            iid = f.payload.get("intent_id", "")
            parts = iid.split("-", 2)
            sym = parts[1] if len(parts) >= 2 else "?"
            by_sym.setdefault(sym, []).append(f)
        print("Per-symbol fills:")
        for sym in sorted(by_sym):
            print(f"  {sym}: {len(by_sym[sym])} fills")
        print()
        print("First 5 fills:")
        for f in fills[:5]:
            p = f.payload
            print(f"  ts={f.ts.isoformat()}  "
                  f"qty={p.get('filled_qty'):>4}  "
                  f"px=${p.get('avg_fill_price'):>8}  "
                  f"id={p.get('intent_id')}")

    print()
    print(_banner("⚠️  REMINDER: PROVISIONAL — NOT A VERDICT  ⚠️"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
