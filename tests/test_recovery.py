"""Isolated unit tests for strategy.recovery.

No orchestrator, no StrategyState, no real broker — recovery is pure
logic over trade-log records + a read-only broker Protocol. We pin:

* pending discovery + replay-protection invariants
* the escalation budget being sourced from the log (restart-durable)
* every resolution branch (unsubmitted / not-visible / retry / escalate /
  resolved) including the OrderOutcomeUnknown poll path
* driver ordering and stop-at-first-halt
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from strategy.dto import BrokerOrder, OrderStatus, OrderSide, SubmittedOrder
from strategy.errors import OrderOutcomeUnknown
from strategy.recovery import (
    OutcomeKind,
    discover_pending_intents,
    count_recovery_attempts,
    recover_pending_submissions,
)
from strategy.trade_log import LogRecord, RecordKind

UTC = timezone.utc
T0 = datetime(2026, 5, 15, 14, 0, tzinfo=UTC)
SESSION_START = datetime(2026, 5, 15, 13, 35, tzinfo=UTC)


def _rec(kind: RecordKind, payload: dict, ts: datetime) -> LogRecord:
    return LogRecord(kind=kind, ts=ts, payload=payload, prev_hash="p", line_hash="l")


def _intent(intent_id: str, *, coid: str | None = "TBv1-x", result: str = "submitting",
            ts: datetime = T0) -> LogRecord:
    return _rec(RecordKind.INTENT, {
        "intent_id": intent_id, "client_order_id": coid, "symbol": "MSFT",
        "side": "buy", "qty_requested": 5, "result": result,
    }, ts)


def _result(intent_id: str, *, status: str = "filled", ts: datetime = T0) -> LogRecord:
    return _rec(RecordKind.RESULT, {"intent_id": intent_id, "status": status}, ts)


def _attempt(intent_id: str, *, ts: datetime = T0) -> LogRecord:
    return _rec(RecordKind.INCIDENT,
                {"kind": "recovery_attempt", "intent_id": intent_id, "reason": "x"}, ts)


def _bo(status: OrderStatus, *, coid: str = "TBv1-x", role: str | None = None,
        filled: int = 0) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id="b-1", client_order_id=coid, symbol="MSFT",
        side=OrderSide.BUY, qty=5, filled_qty=filled,
        avg_fill_price=Decimal("424.85") if filled else None,
        status=status, order_class=None,
        submitted_at=T0, filled_at=None,
        parent_client_order_id=None, leg_role=role, parent_broker_order_id=None,
    )


class FakeBroker:
    """Scriptable RecoveryBroker."""

    def __init__(self, *, resolve=None, poll=None):
        self._resolve = resolve
        self._poll = poll
        self.resolve_calls: list[str] = []
        self.poll_calls: list[str] = []

    def resolve_by_coid(self, coid: str):
        self.resolve_calls.append(coid)
        return self._resolve(coid) if callable(self._resolve) else self._resolve

    def poll_terminal(self, coid: str, timeout_s=None):
        self.poll_calls.append(coid)
        if callable(self._poll):
            return self._poll(coid)
        if isinstance(self._poll, Exception):
            raise self._poll
        return self._poll


# ----- discovery + replay protection ---------------------------------------

def test_discover_empty():
    assert discover_pending_intents([], session_start=SESSION_START) == []


def test_discover_finds_unresolved_submitting():
    recs = [_intent("entry-MSFT-1")]
    out = discover_pending_intents(recs, session_start=SESSION_START)
    assert [r.payload["intent_id"] for r in out] == ["entry-MSFT-1"]


def test_discover_excludes_intent_with_terminal_result():
    recs = [_intent("entry-MSFT-1"), _result("entry-MSFT-1", status="filled")]
    assert discover_pending_intents(recs, session_start=SESSION_START) == []


def test_discover_any_result_freezes_intent_even_error():
    # Replay protection: an `error`/`unsubmitted` RESULT also freezes it.
    recs = [_intent("entry-MSFT-1"), _result("entry-MSFT-1", status="error")]
    assert discover_pending_intents(recs, session_start=SESSION_START) == []


def test_discover_excludes_stale_prior_session_intent():
    stale = _intent("entry-MSFT-old", ts=SESSION_START - timedelta(hours=2))
    assert discover_pending_intents([stale], session_start=SESSION_START) == []


def test_discover_includes_submitted_unconfirmed():
    recs = [_intent("entry-MSFT-1", result="submitted_unconfirmed")]
    out = discover_pending_intents(recs, session_start=SESSION_START)
    assert len(out) == 1


def test_discover_excludes_deny_and_close_intents():
    recs = [
        _rec(RecordKind.INTENT, {"intent_id": "deny-MSFT-1", "result": "denied"}, T0),
        _rec(RecordKind.INTENT, {"intent_id": "close-MSFT-1", "result": "submitting_close"}, T0),
    ]
    assert discover_pending_intents(recs, session_start=SESSION_START) == []


def test_discover_oldest_first():
    a = _intent("entry-A", ts=T0 + timedelta(minutes=5))
    b = _intent("entry-B", ts=T0 + timedelta(minutes=1))
    out = discover_pending_intents([a, b], session_start=SESSION_START)
    assert [r.payload["intent_id"] for r in out] == ["entry-B", "entry-A"]


def test_count_recovery_attempts_matches_only_that_intent():
    recs = [_attempt("entry-A"), _attempt("entry-A"), _attempt("entry-B")]
    assert count_recovery_attempts(recs, "entry-A") == 2
    assert count_recovery_attempts(recs, "entry-B") == 1
    assert count_recovery_attempts(recs, "entry-C") == 0


# ----- resolution branches -------------------------------------------------

def test_resolve_no_coid_is_unsubmitted_no_exposure():
    recs = [_intent("entry-MSFT-1", coid=None)]
    out = recover_pending_submissions(recs, FakeBroker(), T0, session_start=SESSION_START)
    assert out[0].kind is OutcomeKind.UNSUBMITTED
    assert out[0].result_payload["status"] == "unsubmitted"
    assert out[0].result_payload["broker_order_id"] is None


def test_resolve_not_visible_within_grace_is_silent_retry():
    recs = [_intent("entry-MSFT-1", ts=T0)]
    broker = FakeBroker(resolve=None)
    out = recover_pending_submissions(
        recs, broker, T0 + timedelta(seconds=5),
        session_start=SESSION_START, visibility_grace=timedelta(seconds=20))
    assert out[0].kind is OutcomeKind.RETRY
    assert out[0].incident is None  # silent — no incident within grace


def test_resolve_not_found_past_grace_retries_with_incident():
    recs = [_intent("entry-MSFT-1", ts=T0)]
    broker = FakeBroker(resolve=None)
    out = recover_pending_submissions(
        recs, broker, T0 + timedelta(seconds=60),
        session_start=SESSION_START, max_attempts=3,
        visibility_grace=timedelta(seconds=20))
    assert out[0].kind is OutcomeKind.RETRY
    assert out[0].incident["kind"] == "recovery_attempt"
    assert out[0].incident["reason"] == "coid_not_found"


def test_resolve_not_found_escalates_when_budget_exhausted():
    recs = [
        _intent("entry-MSFT-1", ts=T0),
        _attempt("entry-MSFT-1"), _attempt("entry-MSFT-1"),  # 2 prior attempts
    ]
    broker = FakeBroker(resolve=None)
    out = recover_pending_submissions(
        recs, broker, T0 + timedelta(seconds=60),
        session_start=SESSION_START, max_attempts=3,
        visibility_grace=timedelta(seconds=20))
    assert out[0].kind is OutcomeKind.ESCALATE_HALT
    assert out[0].halt_name == "recovery_unresolved"
    assert out[0].incident["reason"] == "coid_not_found_escalated"


def test_resolve_terminal_returns_resolved_with_parent_and_child():
    recs = [_intent("entry-MSFT-1")]
    parent = _bo(OrderStatus.FILLED, filled=5)
    child = _bo(OrderStatus.HELD, coid="child", role="stop_child")
    broker = FakeBroker(
        resolve=SubmittedOrder(parent=parent, stop_child=child),
        poll=_bo(OrderStatus.FILLED, filled=5),
    )
    out = recover_pending_submissions(recs, broker, T0, session_start=SESSION_START)
    o = out[0]
    assert o.kind is OutcomeKind.RESOLVED
    assert o.terminal.status is OrderStatus.FILLED
    assert o.submitted.stop_child.leg_role == "stop_child"
    assert o.intent_id == "entry-MSFT-1"


def test_resolve_poll_unknown_retries_then_escalates():
    recs = [_intent("entry-MSFT-1")]
    parent = _bo(OrderStatus.NEW)
    broker = FakeBroker(
        resolve=SubmittedOrder(parent=parent, stop_child=None),
        poll=OrderOutcomeUnknown("still working"),
    )
    # Attempt 1 (no prior) → RETRY
    out = recover_pending_submissions(recs, broker, T0, session_start=SESSION_START,
                                      max_attempts=3)
    assert out[0].kind is OutcomeKind.RETRY
    assert out[0].incident["reason"] == "still_working"

    # With 2 prior attempts logged → next is the 3rd → ESCALATE
    recs2 = recs + [_attempt("entry-MSFT-1"), _attempt("entry-MSFT-1")]
    out2 = recover_pending_submissions(recs2, broker, T0, session_start=SESSION_START,
                                       max_attempts=3)
    assert out2[0].kind is OutcomeKind.ESCALATE_HALT
    assert out2[0].halt_name == "recovery_unresolved"


# ----- driver ordering -----------------------------------------------------

def test_driver_processes_oldest_first_and_stops_at_halt():
    older = _intent("entry-OLD", ts=T0 + timedelta(minutes=1))
    newer = _intent("entry-NEW", ts=T0 + timedelta(minutes=5))
    # OLD resolves to escalate (not found, no grace, budget tiny); NEW would
    # resolve fine — but must NOT be processed because OLD halts first.
    broker = FakeBroker(resolve=None)
    out = recover_pending_submissions(
        [older, newer], broker, T0 + timedelta(minutes=10),
        session_start=SESSION_START, max_attempts=1,
        visibility_grace=timedelta(seconds=1))
    assert len(out) == 1
    assert out[0].intent_id == "entry-OLD"
    assert out[0].kind is OutcomeKind.ESCALATE_HALT


def test_driver_returns_all_when_no_halt():
    a = _intent("entry-A", coid="A", ts=T0 + timedelta(minutes=1))
    b = _intent("entry-B", coid="B", ts=T0 + timedelta(minutes=2))
    parent = _bo(OrderStatus.FILLED, filled=5)
    broker = FakeBroker(
        resolve=SubmittedOrder(parent=parent, stop_child=None),
        poll=_bo(OrderStatus.FILLED, filled=5),
    )
    out = recover_pending_submissions([a, b], broker, T0 + timedelta(minutes=3),
                                      session_start=SESSION_START)
    assert [o.intent_id for o in out] == ["entry-A", "entry-B"]
    assert all(o.kind is OutcomeKind.RESOLVED for o in out)
