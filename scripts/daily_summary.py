"""Minimal reporting for paper-trading validation.

Reads a hash-chained trade log (``trades.jsonl``) and produces a single
day's summary. Deliberately read-only: this script never writes to the
log, never touches broker state, and never imports the Alpaca SDK. It
is safe to run on a production host against the live trade log.

Metrics reported:

* trade count (closes where filled_qty > 0)
* win rate, avg win $, avg loss $, expectancy per trade
* intraday peak-equity drawdown (from INTENT snapshots)
* denied-entry reason histogram
* INCIDENT histogram split into reconcile incidents vs orphan incidents
  vs halts
* a short list of open concerns (errors, orphan events, etc.)

Usage::

    python -m scripts.daily_summary --trade-log ./var/trades.jsonl
    python -m scripts.daily_summary --trade-log ./var/trades.jsonl --date 2026-04-23
    python -m scripts.daily_summary --trade-log ./var/trades.jsonl --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from strategy.trade_log import RecordKind, TradeLog


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class TradePair:
    """A matched entry + close pair for the same symbol."""

    symbol: str
    entry_ts: datetime
    close_ts: datetime
    qty: int
    entry_price: Decimal
    close_price: Decimal
    realized_pnl: Decimal

    def is_win(self) -> bool:
        return self.realized_pnl > 0


@dataclass
class DailySummary:
    trading_day: str
    trade_log: str
    n_trades: int = 0
    n_wins: int = 0
    n_losses: int = 0
    n_scratches: int = 0
    win_rate: float = 0.0
    avg_win: Decimal = Decimal(0)
    avg_loss: Decimal = Decimal(0)
    expectancy_per_trade: Decimal = Decimal(0)
    total_realized_pnl: Decimal = Decimal(0)
    peak_equity: Decimal | None = None
    trough_equity: Decimal | None = None
    intraday_drawdown_pct: Decimal = Decimal(0)
    denied_reasons: dict[str, int] = field(default_factory=dict)
    incidents_by_kind: dict[str, int] = field(default_factory=dict)
    reconcile_incidents: int = 0
    orphan_incidents: int = 0
    halt_incidents: int = 0
    error_results: int = 0
    audit_integrity_ok: bool = True
    audit_integrity_error: str | None = None
    pairs: list[TradePair] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core summariser
# ---------------------------------------------------------------------------


RECONCILE_KINDS = frozenset({"reconcile_mismatch"})
ORPHAN_KINDS = frozenset(
    {"orphan_protective_orders", "partial_fill_with_orphan_child"}
)
HALT_KINDS = frozenset(
    {
        "daily_loss_halt",
        "drawdown_halt",
        "position_side_mismatch",
        "recovery_unrecoverable",
    }
)


def summarise_trade_log(
    path: Path | str,
    *,
    trading_day: date | None = None,
) -> DailySummary:
    """Build a :class:`DailySummary` from the log at ``path``.

    ``trading_day`` filters INTENT/RESULT/INCIDENT records to those
    whose payload timestamp falls on the given date. When ``None``, the
    summary covers every record in the log.

    If the hash chain is broken, we still produce a summary from the
    parseable records and flag the integrity break — operators must see
    a broken chain loud and clear, but not lose the rest of the report.
    """
    summary = DailySummary(
        trading_day=trading_day.isoformat() if trading_day else "all",
        trade_log=str(path),
    )
    try:
        log = TradeLog(Path(path), fsync=False)
    except Exception as exc:  # noqa: BLE001 — integrity break at construction
        summary.audit_integrity_ok = False
        summary.audit_integrity_error = str(exc)
        return summary

    try:
        log.verify_integrity()
    except Exception as exc:  # noqa: BLE001
        summary.audit_integrity_ok = False
        summary.audit_integrity_error = str(exc)

    records = list(log.read_all())
    if trading_day is not None:
        records = [r for r in records if _record_on_day(r, trading_day)]

    summary.denied_reasons = dict(_count_denies(records))
    incidents = Counter(
        r.payload.get("kind", "unknown")
        for r in records
        if r.kind is RecordKind.INCIDENT
    )
    summary.incidents_by_kind = dict(incidents)
    summary.reconcile_incidents = sum(
        v for k, v in incidents.items() if k in RECONCILE_KINDS
    )
    summary.orphan_incidents = sum(
        v for k, v in incidents.items() if k in ORPHAN_KINDS
    )
    summary.halt_incidents = sum(v for k, v in incidents.items() if k in HALT_KINDS)
    summary.error_results = sum(
        1
        for r in records
        if r.kind is RecordKind.RESULT and r.payload.get("status") == "error"
    )

    pairs = _match_entries_to_closes(records)
    summary.pairs = pairs
    summary.n_trades = len(pairs)
    if pairs:
        wins = [p.realized_pnl for p in pairs if p.realized_pnl > 0]
        losses = [p.realized_pnl for p in pairs if p.realized_pnl < 0]
        scratches = [p.realized_pnl for p in pairs if p.realized_pnl == 0]
        summary.n_wins = len(wins)
        summary.n_losses = len(losses)
        summary.n_scratches = len(scratches)
        summary.win_rate = (len(wins) / len(pairs)) if pairs else 0.0
        summary.avg_win = (sum(wins, Decimal(0)) / len(wins)) if wins else Decimal(0)
        summary.avg_loss = (sum(losses, Decimal(0)) / len(losses)) if losses else Decimal(0)
        summary.expectancy_per_trade = (
            sum((p.realized_pnl for p in pairs), Decimal(0)) / len(pairs)
        )
        summary.total_realized_pnl = sum((p.realized_pnl for p in pairs), Decimal(0))

    # Drawdown from INTENT equity snapshots.
    equity_series = _extract_equity_series(records)
    if equity_series:
        running_peak = equity_series[0]
        peak = equity_series[0]
        trough = equity_series[0]
        max_dd = Decimal(0)
        for eq in equity_series:
            if eq > peak:
                peak = eq
            if eq > running_peak:
                running_peak = eq
            if eq < trough:
                trough = eq
            if running_peak > 0:
                dd = (running_peak - eq) / running_peak
                if dd > max_dd:
                    max_dd = dd
        summary.peak_equity = peak
        summary.trough_equity = trough
        summary.intraday_drawdown_pct = max_dd.quantize(Decimal("0.0001"))

    return summary


# ---------------------------------------------------------------------------
# Pair-matching: entry → close per symbol
# ---------------------------------------------------------------------------


def _match_entries_to_closes(records: Iterable[Any]) -> list[TradePair]:
    """FIFO-match RESULT entries to their subsequent RESULT closes.

    We pair by symbol in chronological order. An entry is a BUY RESULT
    with filled_qty > 0. A close is a SELL RESULT with filled_qty >= 0
    for the same symbol following the entry.

    RESULT payloads in our schema don't always carry ``side`` directly;
    we infer side by pairing INTENT→RESULT: an INTENT with
    ``side="buy"`` + ``result="submitting"`` is an entry; an INTENT with
    ``result="submitting_close"`` is a close.
    """
    pending_entries: dict[str, list[dict]] = defaultdict(list)
    pairs: list[TradePair] = []
    intent_by_coid: dict[str, dict] = {}
    records_list = list(records)

    # Index INTENT records by client_order_id so RESULT lookups are O(1).
    for r in records_list:
        if r.kind is RecordKind.INTENT:
            coid = r.payload.get("client_order_id")
            if coid:
                intent_by_coid[coid] = r.payload
                intent_by_coid[coid]["__ts__"] = r.ts

    for r in records_list:
        if r.kind is not RecordKind.RESULT:
            continue
        p = r.payload
        coid = p.get("client_order_id")
        if not coid:
            continue
        intent = intent_by_coid.get(coid)
        if intent is None:
            continue
        sym = intent.get("symbol")
        if not sym:
            continue
        filled = int(p.get("filled_qty", 0) or 0)
        if filled <= 0:
            continue
        intent_result = intent.get("result")
        try:
            price = Decimal(str(p.get("avg_fill_price", 0) or 0))
        except InvalidOperation:
            price = Decimal(0)
        if intent_result == "submitting":
            pending_entries[sym].append(
                {
                    "ts": r.ts,
                    "qty": filled,
                    "price": price,
                }
            )
        elif intent_result == "submitting_close":
            queue = pending_entries.get(sym) or []
            close_qty_remaining = filled
            while queue and close_qty_remaining > 0:
                entry = queue[0]
                paired_qty = min(entry["qty"], close_qty_remaining)
                pnl = (price - entry["price"]) * Decimal(paired_qty)
                pairs.append(
                    TradePair(
                        symbol=sym,
                        entry_ts=entry["ts"],
                        close_ts=r.ts,
                        qty=paired_qty,
                        entry_price=entry["price"],
                        close_price=price,
                        realized_pnl=pnl,
                    )
                )
                entry["qty"] -= paired_qty
                close_qty_remaining -= paired_qty
                if entry["qty"] <= 0:
                    queue.pop(0)
    return pairs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _record_on_day(record: Any, d: date) -> bool:
    try:
        rec_d = record.ts.date()
    except AttributeError:
        return False
    return rec_d == d


def _count_denies(records: Iterable[Any]) -> Counter:
    c: Counter = Counter()
    for r in records:
        if r.kind is RecordKind.INTENT and r.payload.get("result") == "denied":
            reason = r.payload.get("deny_reason", "unknown")
            c[reason] += 1
    return c


def _extract_equity_series(records: Iterable[Any]) -> list[Decimal]:
    out: list[Decimal] = []
    for r in records:
        if r.kind is not RecordKind.INTENT:
            continue
        raw = r.payload.get("equity_snapshot")
        if raw is None:
            continue
        try:
            out.append(Decimal(str(raw)))
        except InvalidOperation:
            continue
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def format_summary(s: DailySummary) -> str:
    lines: list[str] = []
    lines.append(f"=== Paper validation summary ({s.trading_day}) ===")
    lines.append(f"trade log:            {s.trade_log}")
    lines.append(
        f"audit integrity:      "
        f"{'OK' if s.audit_integrity_ok else f'BROKEN — {s.audit_integrity_error}'}"
    )
    lines.append("")
    lines.append("Trades")
    lines.append(f"  closed:             {s.n_trades}")
    lines.append(
        f"  wins/losses/flats:  {s.n_wins} / {s.n_losses} / {s.n_scratches}"
    )
    if s.n_trades:
        lines.append(f"  win rate:           {s.win_rate:.1%}")
    lines.append(f"  avg win:            {_fmt_money(s.avg_win)}")
    lines.append(f"  avg loss:           {_fmt_money(s.avg_loss)}")
    lines.append(f"  expectancy / trade: {_fmt_money(s.expectancy_per_trade)}")
    lines.append(f"  total realized PnL: {_fmt_money(s.total_realized_pnl)}")
    lines.append("")
    lines.append("Equity")
    lines.append(
        f"  peak (intraday):    "
        f"{_fmt_money(s.peak_equity) if s.peak_equity is not None else '-'}"
    )
    lines.append(
        f"  trough (intraday):  "
        f"{_fmt_money(s.trough_equity) if s.trough_equity is not None else '-'}"
    )
    lines.append(f"  max drawdown:       {s.intraday_drawdown_pct * 100:.2f}%")
    lines.append("")
    lines.append("Denied entries (by reason)")
    if not s.denied_reasons:
        lines.append("  (none)")
    else:
        for reason, n in sorted(s.denied_reasons.items(), key=lambda x: -x[1]):
            lines.append(f"  {reason:32s} {n}")
    lines.append("")
    lines.append("Incidents")
    lines.append(f"  reconcile mismatches: {s.reconcile_incidents}")
    lines.append(f"  orphan events:        {s.orphan_incidents}")
    lines.append(f"  safety halts:         {s.halt_incidents}")
    lines.append(f"  error RESULT records: {s.error_results}")
    if s.incidents_by_kind:
        lines.append("  by kind:")
        for kind, n in sorted(s.incidents_by_kind.items(), key=lambda x: -x[1]):
            lines.append(f"    {kind:32s} {n}")
    else:
        lines.append("  by kind: (none)")
    return "\n".join(lines)


def _fmt_money(d: Decimal) -> str:
    sign = "-" if d < 0 else " "
    return f"{sign}${abs(d):,.2f}"


def summary_to_json(s: DailySummary) -> str:
    def _cvt(v: Any) -> Any:
        if isinstance(v, Decimal):
            return str(v)
        if isinstance(v, datetime):
            return v.isoformat()
        if isinstance(v, TradePair):
            return {
                "symbol": v.symbol,
                "entry_ts": v.entry_ts.isoformat(),
                "close_ts": v.close_ts.isoformat(),
                "qty": v.qty,
                "entry_price": str(v.entry_price),
                "close_price": str(v.close_price),
                "realized_pnl": str(v.realized_pnl),
                "is_win": v.is_win(),
            }
        if isinstance(v, list):
            return [_cvt(x) for x in v]
        if isinstance(v, dict):
            return {k: _cvt(x) for k, x in v.items()}
        return v
    return json.dumps(_cvt(asdict(s)), indent=2, default=str)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Trading bot daily summary")
    ap.add_argument("--trade-log", required=True, type=Path)
    ap.add_argument(
        "--date",
        help="Trading day as YYYY-MM-DD (UTC). Omit to summarise every record.",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    args = ap.parse_args(argv)

    if not args.trade_log.exists():
        print(f"trade log not found: {args.trade_log}", file=sys.stderr)
        return 2
    day = date.fromisoformat(args.date) if args.date else None
    summary = summarise_trade_log(args.trade_log, trading_day=day)
    if args.json:
        print(summary_to_json(summary))
    else:
        print(format_summary(summary))
    return 0 if summary.audit_integrity_ok else 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
