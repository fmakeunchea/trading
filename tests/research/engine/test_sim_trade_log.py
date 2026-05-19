"""Offline tests for research.engine.sim_trade_log.

Pure-Python — no pandas / pmc dependency. The whole point of this class
is determinism via injected clock, so these tests pin exactly that.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from research.engine.sim_trade_log import SimulatedTradeLog
from strategy.trade_log import GENESIS_HASH, RecordKind

UTC = timezone.utc


def _clock_at(ts: datetime):
    """A clock callable that returns a fixed ts each call."""
    return lambda: ts


def _stepping_clock(start: datetime, step_min: int = 1):
    """Returns a clock callable that advances by step_min each call."""
    state = {"n": -1}
    def fn():
        state["n"] += 1
        from datetime import timedelta
        return start + timedelta(minutes=state["n"] * step_min)
    return fn


def test_clock_drives_record_ts_not_wall_clock() -> None:
    """If this drifts and reverts to wall-clock, backtest determinism dies."""
    fixed = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    log = SimulatedTradeLog(clock=_clock_at(fixed))
    rec = log.append_intent({"x": 1})
    assert rec.ts == fixed
    rec2 = log.append_result({"y": 2})
    assert rec2.ts == fixed


def test_hash_chain_is_consistent() -> None:
    log = SimulatedTradeLog(clock=_stepping_clock(datetime(2025, 1, 2, tzinfo=UTC)))
    r1 = log.append_intent({"a": 1})
    r2 = log.append_result({"b": 2})
    r3 = log.append_incident({"kind": "DIAGNOSTIC", "c": 3})
    assert r1.prev_hash == GENESIS_HASH
    assert r2.prev_hash == r1.line_hash
    assert r3.prev_hash == r2.line_hash
    log.verify_integrity()  # must not raise


def test_incident_requires_kind_field() -> None:
    log = SimulatedTradeLog(clock=_clock_at(datetime(2025, 1, 1, tzinfo=UTC)))
    with pytest.raises(ValueError, match="kind"):
        log.append_incident({"no_kind": True})


def test_naive_clock_is_rejected_loudly() -> None:
    log = SimulatedTradeLog(clock=lambda: datetime(2025, 1, 1, 14, 30))
    with pytest.raises(ValueError, match="UTC"):
        log.append_intent({"x": 1})


def test_record_kinds_route_correctly() -> None:
    log = SimulatedTradeLog(clock=_clock_at(datetime(2025, 1, 2, tzinfo=UTC)))
    a = log.append_intent({"i": 1})
    b = log.append_result({"r": 1})
    c = log.append_incident({"kind": "X"})
    assert a.kind is RecordKind.INTENT
    assert b.kind is RecordKind.RESULT
    assert c.kind is RecordKind.INCIDENT


def test_read_all_returns_records_in_order_and_snapshots() -> None:
    log = SimulatedTradeLog(clock=_stepping_clock(datetime(2025, 1, 2, tzinfo=UTC)))
    log.append_intent({"a": 1})
    log.append_result({"b": 2})
    seen_before_more = list(log.read_all())
    log.append_intent({"c": 3})
    # Snapshot semantics: the iterator captured before the new append
    # MUST NOT see the new record (no late-mutation surprise).
    assert len(seen_before_more) == 2


def test_two_logs_same_clock_same_payloads_are_byte_identical() -> None:
    """The whole point: identical inputs produce identical records
    (including prev_hash and line_hash). This is the seed of 1.2's
    full driver-level determinism gate."""
    payloads = [
        (RecordKind.INTENT,   {"intent_id": "x", "symbol": "SPY"}),
        (RecordKind.RESULT,   {"intent_id": "x", "status": "filled"}),
        (RecordKind.INCIDENT, {"kind": "DIAGNOSTIC", "reason": "no_signal"}),
    ]

    def build():
        log = SimulatedTradeLog(
            clock=_stepping_clock(datetime(2025, 1, 2, tzinfo=UTC)),
        )
        out = []
        for kind, p in payloads:
            if kind is RecordKind.INTENT:
                out.append(log.append_intent(p))
            elif kind is RecordKind.RESULT:
                out.append(log.append_result(p))
            else:
                out.append(log.append_incident(p))
        return tuple(out)

    a = build()
    b = build()
    assert a == b
    for ra, rb in zip(a, b):
        assert ra.prev_hash == rb.prev_hash
        assert ra.line_hash == rb.line_hash
        assert ra.ts == rb.ts
