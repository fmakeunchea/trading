"""Tests for strategy.trade_log.

Critical invariants:
* Append-only — no update/delete API.
* Hash chain detects tampering.
* Partial trailing line is skipped with a warning (not an exception), but
  integrity of the committed records is verified.
* fsync is called on each write when enabled.
* SEAL closes the log.
"""
from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from strategy.errors import AuditIntegrityError
from strategy.trade_log import GENESIS_HASH, RecordKind, TradeLog


def _read_raw_lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines()


def _sample_intent_payload() -> dict:
    return {
        "intent_id": "i-001",
        "client_order_id": "TBv1-abc",
        "symbol": "AAPL",
        "side": "buy",
        "qty_requested": 10,
        "limit_price": Decimal("200.10"),
        "disaster_stop_price": Decimal("196.00"),
        "reason": "ema-breakout",
        "equity_snapshot": Decimal("25000.00"),
        "peak_equity": Decimal("25500.00"),
        "drawdown_pct": Decimal("0.02"),
        "throttle_multiplier": Decimal("0.75"),
        "spread_bps": Decimal("4"),
        "risk_decision": "allowed",
        "result": "submitted",
    }


def _sample_result_payload() -> dict:
    return {
        "intent_id": "i-001",
        "client_order_id": "TBv1-abc",
        "broker_order_id": "b-001",
        "status": "filled",
        "filled_qty": 10,
        "avg_fill_price": Decimal("200.08"),
    }


# ---------------------------------------------------------------------------
# Basic append
# ---------------------------------------------------------------------------


def test_append_intent_and_result(tmp_path: Path) -> None:
    log = TradeLog(tmp_path / "trades.jsonl")
    r1 = log.append_intent(_sample_intent_payload())
    r2 = log.append_result(_sample_result_payload())
    assert r1.kind is RecordKind.INTENT
    assert r2.kind is RecordKind.RESULT
    records = list(log.read_all())
    assert len(records) == 2
    assert records[0].prev_hash == GENESIS_HASH
    assert records[1].prev_hash == records[0].line_hash


def test_decimal_precision_preserved(tmp_path: Path) -> None:
    log = TradeLog(tmp_path / "trades.jsonl")
    log.append_intent(_sample_intent_payload())
    rec = next(iter(log.read_all()))
    assert rec.payload["limit_price"] == "200.10"  # stringified for precision
    assert rec.payload["equity_snapshot"] == "25000.00"


# ---------------------------------------------------------------------------
# No mutation API exists
# ---------------------------------------------------------------------------


def test_no_mutation_api(tmp_path: Path) -> None:
    log = TradeLog(tmp_path / "trades.jsonl")
    forbidden = {"update", "delete", "remove", "overwrite", "edit", "rewrite"}
    public = {m for m in dir(log) if not m.startswith("_")}
    assert not (forbidden & public), f"forbidden API present: {forbidden & public}"


# ---------------------------------------------------------------------------
# Hash chain integrity
# ---------------------------------------------------------------------------


def test_tamper_middle_line_breaks_chain(tmp_path: Path) -> None:
    p = tmp_path / "trades.jsonl"
    log = TradeLog(p)
    log.append_intent(_sample_intent_payload())
    log.append_result(_sample_result_payload())
    # Tamper: rewrite the first line with a perturbed payload while
    # keeping its hash field unchanged.
    lines = _read_raw_lines(p)
    first = json.loads(lines[0])
    first["payload"]["symbol"] = "MSFT"   # silently edit
    lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(AuditIntegrityError):
        # Just constructing a new TradeLog re-verifies the chain.
        TradeLog(p)


def test_tamper_last_hash_is_detected(tmp_path: Path) -> None:
    p = tmp_path / "trades.jsonl"
    log = TradeLog(p)
    log.append_intent(_sample_intent_payload())
    lines = _read_raw_lines(p)
    obj = json.loads(lines[0])
    obj["line_hash"] = "0" * 64   # bogus hash
    lines[0] = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(AuditIntegrityError):
        TradeLog(p)


def test_verify_integrity_walks_clean_chain(tmp_path: Path) -> None:
    log = TradeLog(tmp_path / "trades.jsonl")
    log.append_intent(_sample_intent_payload())
    log.append_result(_sample_result_payload())
    log.verify_integrity()  # should not raise


# ---------------------------------------------------------------------------
# Partial line on crash
# ---------------------------------------------------------------------------


def test_partial_trailing_line_skipped(tmp_path: Path, caplog) -> None:
    p = tmp_path / "trades.jsonl"
    log = TradeLog(p)
    log.append_intent(_sample_intent_payload())
    # Append a partial (no newline) line that looks plausible.
    with p.open("a", encoding="utf-8") as f:
        f.write('{"kind":"INTENT","ts":"2026-04')   # no newline

    # Reader tolerates the partial and raises no exception.
    log2 = TradeLog(p)  # construction re-walks; partial line is dropped
    recs = list(log2.read_all())
    assert len(recs) == 1


def test_partial_line_does_not_stop_new_appends(tmp_path: Path) -> None:
    p = tmp_path / "trades.jsonl"
    log = TradeLog(p)
    log.append_intent(_sample_intent_payload())
    with p.open("a", encoding="utf-8") as f:
        f.write("incomplete")  # no newline
    # Re-open; the new log should be able to append onto a clean chain.
    log2 = TradeLog(p)
    log2.append_result(_sample_result_payload())
    # The re-loaded log sees 1 committed record (intent).
    # Note: we simply appended onto an incomplete trailing line. That means
    # the physical file now contains a malformed record, so a fresh reader
    # from scratch will see garbage. This test documents that the bot must
    # detect this in verify_integrity and that it does:
    with pytest.raises(AuditIntegrityError):
        TradeLog(p).verify_integrity()


# ---------------------------------------------------------------------------
# SEAL semantics
# ---------------------------------------------------------------------------


def test_seal_closes_log(tmp_path: Path) -> None:
    p = tmp_path / "trades.jsonl"
    log = TradeLog(p)
    log.append_intent(_sample_intent_payload())
    log.append_seal(date(2026, 4, 23), line_count=1)
    with pytest.raises(AuditIntegrityError, match="sealed"):
        log.append_intent(_sample_intent_payload())


def test_reopen_sealed_log_refuses_appends(tmp_path: Path) -> None:
    p = tmp_path / "trades.jsonl"
    log = TradeLog(p)
    log.append_intent(_sample_intent_payload())
    log.append_seal(date(2026, 4, 23), line_count=1)
    del log
    log2 = TradeLog(p)
    with pytest.raises(AuditIntegrityError, match="sealed"):
        log2.append_intent(_sample_intent_payload())


def test_seal_record_contains_day_and_count(tmp_path: Path) -> None:
    p = tmp_path / "trades.jsonl"
    log = TradeLog(p)
    log.append_intent(_sample_intent_payload())
    log.append_intent(_sample_intent_payload())
    rec = log.append_seal(date(2026, 4, 23), line_count=2)
    assert rec.kind is RecordKind.SEAL
    assert rec.payload["trading_day"] == "2026-04-23"
    assert rec.payload["line_count"] == 2
    assert "chain_hash" in rec.payload


# ---------------------------------------------------------------------------
# fsync is called on writes
# ---------------------------------------------------------------------------


def test_fsync_called_when_enabled(tmp_path: Path, mocker) -> None:
    spy = mocker.spy(os, "fsync")
    log = TradeLog(tmp_path / "trades.jsonl", fsync=True)
    log.append_intent(_sample_intent_payload())
    log.append_result(_sample_result_payload())
    assert spy.call_count >= 2


def test_fsync_not_called_when_disabled(tmp_path: Path, mocker) -> None:
    spy = mocker.spy(os, "fsync")
    log = TradeLog(tmp_path / "trades.jsonl", fsync=False)
    log.append_intent(_sample_intent_payload())
    assert spy.call_count == 0


# ---------------------------------------------------------------------------
# Empty log edge case
# ---------------------------------------------------------------------------


def test_append_incident_records_structured_payload(tmp_path: Path) -> None:
    log = TradeLog(tmp_path / "trades.jsonl")
    rec = log.append_incident(
        {
            "kind": "reconcile_mismatch",
            "phase": "tick",
            "missing_positions": ["AAPL"],
            "now": "2026-04-23T14:30:00+00:00",
        }
    )
    assert rec.kind is RecordKind.INCIDENT
    assert rec.payload["kind"] == "reconcile_mismatch"
    records = list(log.read_all())
    assert len(records) == 1 and records[0].kind is RecordKind.INCIDENT


def test_append_incident_requires_kind(tmp_path: Path) -> None:
    log = TradeLog(tmp_path / "trades.jsonl")
    with pytest.raises(ValueError, match="kind"):
        log.append_incident({"phase": "tick"})   # missing required field


def test_empty_log_returns_nothing(tmp_path: Path) -> None:
    log = TradeLog(tmp_path / "trades.jsonl")
    assert list(log.read_all()) == []
    log.verify_integrity()  # no raise


# ---------------------------------------------------------------------------
# Chain continuity across multiple TradeLog instances on the same file
# ---------------------------------------------------------------------------


def test_reopen_continues_chain(tmp_path: Path) -> None:
    p = tmp_path / "trades.jsonl"
    log_a = TradeLog(p)
    r1 = log_a.append_intent(_sample_intent_payload())
    del log_a

    log_b = TradeLog(p)
    r2 = log_b.append_result(_sample_result_payload())
    assert r2.prev_hash == r1.line_hash
    log_b.verify_integrity()
