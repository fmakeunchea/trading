"""Tests for scripts.daily_summary.

Exercises the reporter against a seeded trade log covering:
* One winning round trip.
* One losing round trip.
* A denied entry.
* An incident of each major kind.
* A partial fill at close.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.daily_summary import (
    RECONCILE_KINDS,
    summarise_trade_log,
    format_summary,
    summary_to_json,
)
from strategy.trade_log import TradeLog


UTC = timezone.utc


def _seed_log(path: Path) -> TradeLog:
    log = TradeLog(path, fsync=False)

    # Winning round trip on AAPL.
    log.append_intent(
        {
            "intent_id": "i-1",
            "client_order_id": "TBv1-entry-1",
            "symbol": "AAPL",
            "side": "buy",
            "qty_requested": 10,
            "limit_price": "200.00",
            "reason": "breakout",
            "equity_snapshot": "25000.00",
            "peak_equity": "25000.00",
            "result": "submitting",
        }
    )
    log.append_result(
        {
            "intent_id": "i-1",
            "client_order_id": "TBv1-entry-1",
            "broker_order_id": "b-1",
            "status": "filled",
            "filled_qty": 10,
            "avg_fill_price": "200.00",
            "symbol": "AAPL",
        }
    )
    log.append_intent(
        {
            "intent_id": "c-1",
            "client_order_id": "TBv1-close-1",
            "symbol": "AAPL",
            "side": "sell",
            "qty_requested": 10,
            "reason": "target_hit",
            "equity_snapshot": "25050.00",
            "peak_equity": "25050.00",
            "result": "submitting_close",
        }
    )
    log.append_result(
        {
            "intent_id": "c-1",
            "client_order_id": "TBv1-close-1",
            "broker_order_id": "b-2",
            "status": "filled",
            "filled_qty": 10,
            "avg_fill_price": "205.00",   # win of $50
            "symbol": "AAPL",
        }
    )

    # Losing round trip on MSFT.
    log.append_intent(
        {
            "intent_id": "i-2",
            "client_order_id": "TBv1-entry-2",
            "symbol": "MSFT",
            "side": "buy",
            "qty_requested": 5,
            "limit_price": "300.00",
            "reason": "breakout",
            "equity_snapshot": "25050.00",
            "peak_equity": "25050.00",
            "result": "submitting",
        }
    )
    log.append_result(
        {
            "intent_id": "i-2",
            "client_order_id": "TBv1-entry-2",
            "broker_order_id": "b-3",
            "status": "filled",
            "filled_qty": 5,
            "avg_fill_price": "300.00",
            "symbol": "MSFT",
        }
    )
    log.append_intent(
        {
            "intent_id": "c-2",
            "client_order_id": "TBv1-close-2",
            "symbol": "MSFT",
            "side": "sell",
            "qty_requested": 5,
            "reason": "primary_stop_hit",
            "equity_snapshot": "24950.00",
            "peak_equity": "25050.00",
            "result": "submitting_close",
        }
    )
    log.append_result(
        {
            "intent_id": "c-2",
            "client_order_id": "TBv1-close-2",
            "broker_order_id": "b-4",
            "status": "filled",
            "filled_qty": 5,
            "avg_fill_price": "298.00",   # loss of $10
            "symbol": "MSFT",
        }
    )

    # Denied entry.
    log.append_intent(
        {
            "intent_id": "deny-1",
            "client_order_id": None,
            "symbol": "SPY",
            "side": "buy",
            "qty_requested": 0,
            "reason": "breakout",
            "result": "denied",
            "deny_reason": "below_min_edge",
            "equity_snapshot": "24950.00",
            "peak_equity": "25050.00",
        }
    )

    # Incidents.
    log.append_incident({"kind": "reconcile_mismatch", "phase": "tick", "missing_positions": ["GOOG"]})
    log.append_incident({"kind": "orphan_protective_orders", "phase": "recover", "orphans": ["X"]})
    log.append_incident({"kind": "daily_loss_halt", "phase": "tick"})

    return log


# ---------------------------------------------------------------------------
# Summary correctness
# ---------------------------------------------------------------------------


def test_summary_counts_trades(tmp_path: Path) -> None:
    _seed_log(tmp_path / "trades.jsonl")
    s = summarise_trade_log(tmp_path / "trades.jsonl")
    assert s.n_trades == 2
    assert s.n_wins == 1
    assert s.n_losses == 1
    assert s.win_rate == 0.5


def test_summary_computes_pnl(tmp_path: Path) -> None:
    _seed_log(tmp_path / "trades.jsonl")
    s = summarise_trade_log(tmp_path / "trades.jsonl")
    # Win: (205-200)*10 = 50. Loss: (298-300)*5 = -10. Expectancy = 20.
    assert s.avg_win == Decimal("50")
    assert s.avg_loss == Decimal("-10")
    assert s.expectancy_per_trade == Decimal("20")
    assert s.total_realized_pnl == Decimal("40")


def test_summary_denied_histogram(tmp_path: Path) -> None:
    _seed_log(tmp_path / "trades.jsonl")
    s = summarise_trade_log(tmp_path / "trades.jsonl")
    assert s.denied_reasons == {"below_min_edge": 1}


def test_summary_incident_split(tmp_path: Path) -> None:
    _seed_log(tmp_path / "trades.jsonl")
    s = summarise_trade_log(tmp_path / "trades.jsonl")
    assert s.reconcile_incidents == 1
    assert s.orphan_incidents == 1
    assert s.halt_incidents == 1
    assert s.incidents_by_kind == {
        "reconcile_mismatch": 1,
        "orphan_protective_orders": 1,
        "daily_loss_halt": 1,
    }


def test_summary_drawdown_from_equity_snapshots(tmp_path: Path) -> None:
    _seed_log(tmp_path / "trades.jsonl")
    s = summarise_trade_log(tmp_path / "trades.jsonl")
    # Peak 25050, trough 24950 → DD = 100/25050 ≈ 0.40%.
    assert s.peak_equity == Decimal("25050.00")
    assert s.trough_equity == Decimal("24950.00")
    assert Decimal("0.003") < s.intraday_drawdown_pct < Decimal("0.005")


def test_date_filter(tmp_path: Path) -> None:
    _seed_log(tmp_path / "trades.jsonl")
    # The seeded records are timestamped with datetime.now() inside
    # TradeLog.append_*, so they all share today's date.
    from datetime import date as _d
    s = summarise_trade_log(tmp_path / "trades.jsonl", trading_day=_d(1990, 1, 1))
    # Far-past date filter → no records → empty summary.
    assert s.n_trades == 0
    assert s.incidents_by_kind == {}
    assert s.denied_reasons == {}


def test_integrity_failure_surfaced(tmp_path: Path) -> None:
    path = tmp_path / "trades.jsonl"
    _seed_log(path)
    # Corrupt one record's line_hash.
    import json
    lines = path.read_text(encoding="utf-8").splitlines()
    obj = json.loads(lines[0])
    obj["line_hash"] = "0" * 64
    lines[0] = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    s = summarise_trade_log(path)
    assert s.audit_integrity_ok is False
    assert s.audit_integrity_error


def test_format_renders_without_raising(tmp_path: Path) -> None:
    _seed_log(tmp_path / "trades.jsonl")
    s = summarise_trade_log(tmp_path / "trades.jsonl")
    text = format_summary(s)
    assert "Paper validation summary" in text
    assert "reconcile mismatches" in text
    assert "expectancy" in text
    j = summary_to_json(s)
    import json as _j
    parsed = _j.loads(j)
    assert parsed["n_trades"] == 2


def test_cli_missing_log(tmp_path: Path, capsys) -> None:
    from scripts.daily_summary import main
    rc = main(["--trade-log", str(tmp_path / "nope.jsonl")])
    assert rc == 2


def test_cli_happy_path(tmp_path: Path, capsys) -> None:
    from scripts.daily_summary import main
    _seed_log(tmp_path / "trades.jsonl")
    rc = main(["--trade-log", str(tmp_path / "trades.jsonl")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "trade log" in out


def test_cli_json_mode(tmp_path: Path, capsys) -> None:
    from scripts.daily_summary import main
    _seed_log(tmp_path / "trades.jsonl")
    rc = main(["--trade-log", str(tmp_path / "trades.jsonl"), "--json"])
    assert rc == 0
    import json as _j
    parsed = _j.loads(capsys.readouterr().out)
    assert parsed["n_trades"] == 2


# ---------------------------------------------------------------------------
# Partial-close sanity: closing only part of a larger entry
# ---------------------------------------------------------------------------


def test_partial_close_pairs_fifo(tmp_path: Path) -> None:
    log = TradeLog(tmp_path / "trades.jsonl", fsync=False)
    log.append_intent(
        {
            "intent_id": "i",
            "client_order_id": "TBv1-e",
            "symbol": "AAPL",
            "side": "buy",
            "qty_requested": 10,
            "reason": "x",
            "result": "submitting",
            "equity_snapshot": "25000.00",
            "peak_equity": "25000.00",
        }
    )
    log.append_result(
        {
            "intent_id": "i",
            "client_order_id": "TBv1-e",
            "broker_order_id": "b1",
            "status": "filled",
            "filled_qty": 10,
            "avg_fill_price": "100.00",
            "symbol": "AAPL",
        }
    )
    log.append_intent(
        {
            "intent_id": "c",
            "client_order_id": "TBv1-c",
            "symbol": "AAPL",
            "side": "sell",
            "qty_requested": 4,
            "reason": "x",
            "result": "submitting_close",
            "equity_snapshot": "25010.00",
            "peak_equity": "25010.00",
        }
    )
    log.append_result(
        {
            "intent_id": "c",
            "client_order_id": "TBv1-c",
            "broker_order_id": "b2",
            "status": "filled",
            "filled_qty": 4,
            "avg_fill_price": "102.50",
            "symbol": "AAPL",
        }
    )
    s = summarise_trade_log(tmp_path / "trades.jsonl")
    assert s.n_trades == 1
    assert s.pairs[0].qty == 4
    assert s.pairs[0].realized_pnl == Decimal("10.00")  # (102.50-100)*4
