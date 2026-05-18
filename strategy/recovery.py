"""Submitted-but-unconfirmed order recovery (pure logic).

Background: an entry submission can be acknowledged by the broker while
the engine never records its terminal outcome — a poll timeout, a crash
between submit and result, etc. (the 2026-05-15 lost-fill incident). The
append-only trade log is the source of truth: an INTENT with
``result in {submitting, submitted_unconfirmed}`` and **no** terminal
RESULT for its ``intent_id`` is a *pending* submission that must be
resolved by client_order_id, never discarded.

This module is deliberately isolated:

* It performs NO orchestrator I/O — it does not touch StrategyState,
  open_trades, the trade log, or set halts. It *reads* trade-log records
  (passed in) and a read-only broker handle, and returns a list of
  :class:`RecoveryOutcome` describing what the orchestrator should do.
* That keeps it unit-testable with a fake broker and synthetic records,
  and keeps the Tier-3 wiring a thin, auditable adapter.

Replay protection invariants:

* Any RESULT (terminal fill, ``error``, or ``unsubmitted``) for an
  ``intent_id`` freezes that intent forever — it is never re-resolved.
* Discovery is bounded to the current session (``ts >= session_start``)
  so stale prior-day intents are never resurrected.
* The escalation budget is counted from the log (``recovery_attempt``
  incidents), so a process restart does not reset it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol

from strategy.dto import BrokerOrder, SubmittedOrder
from strategy.errors import OrderOutcomeUnknown
from strategy.trade_log import LogRecord, RecordKind

# Intent.result values that denote an in-flight entry submission.
PENDING_RESULTS: frozenset[str] = frozenset({"submitting", "submitted_unconfirmed"})

# Defaults; the orchestrator may override at the call site.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_VISIBILITY_GRACE = timedelta(seconds=20)
DEFAULT_RECOVERY_POLL_TIMEOUT_S = 5.0


class RecoveryBroker(Protocol):
    """The read-only broker surface recovery needs (see strategy.broker)."""

    def resolve_by_coid(self, client_order_id: str) -> SubmittedOrder | None: ...

    def poll_terminal(
        self, client_order_id: str, timeout_s: float | None = None
    ) -> BrokerOrder: ...


class OutcomeKind(str, Enum):
    RESOLVED = "resolved"          # terminal reached → orchestrator records it
    RETRY = "retry"               # still pending → log attempt, try next tick
    UNSUBMITTED = "unsubmitted"   # never reached broker → terminal, no exposure
    ESCALATE_HALT = "escalate_halt"  # budget exhausted → orchestrator halts


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """What the orchestrator must do for one pending intent.

    The orchestrator is the only thing that mutates state; this is a pure
    instruction. Field meaning by ``kind``:

    * RESOLVED       — ``submitted`` (parent+child) and ``terminal`` set;
                       orchestrator calls its existing entry recorder.
    * RETRY          — ``incident`` set (a ``recovery_attempt`` payload to
                       append) or ``None`` for a silent within-grace wait.
    * UNSUBMITTED    — ``result_payload`` set; orchestrator appends it as a
                       terminal RESULT (no position, no exposure).
    * ESCALATE_HALT  — ``halt_name``/``halt_reason``/``incident`` set;
                       orchestrator sets a sticky halt and stops entering.
    """

    kind: OutcomeKind
    intent_id: str
    client_order_id: str | None
    intent_payload: dict[str, Any]
    submitted: SubmittedOrder | None = None
    terminal: BrokerOrder | None = None
    incident: dict[str, Any] | None = None
    result_payload: dict[str, Any] | None = None
    halt_name: str | None = None
    halt_reason: str | None = None


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _ts(rec: LogRecord) -> datetime:
    return rec.ts


def discover_pending_intents(
    records: list[LogRecord],
    *,
    session_start: datetime,
) -> list[LogRecord]:
    """Return INTENT records that are in-flight and unresolved.

    Pending iff: kind==INTENT, ``result`` in :data:`PENDING_RESULTS`,
    ``ts >= session_start``, and no RESULT exists for the same
    ``intent_id`` (any RESULT — terminal/error/unsubmitted — freezes it).
    Oldest-first.
    """
    resolved_intent_ids: set[str] = set()
    for r in records:
        if r.kind is RecordKind.RESULT:
            iid = r.payload.get("intent_id")
            if iid:
                resolved_intent_ids.add(iid)

    # An intent can have MORE THAN ONE in-flight INTENT record: the
    # original `submitting`, then a `submitted_unconfirmed` written when
    # the poll timed out. Both must collapse to ONE recovery action or we
    # would process (and potentially finalize) the same intent twice.
    # Keep the LATEST record per intent_id (it carries the freshest
    # fields); order by the EARLIEST ts (original submission order).
    latest: dict[str, LogRecord] = {}
    first_ts: dict[str, datetime] = {}
    for r in records:
        if r.kind is not RecordKind.INTENT:
            continue
        p = r.payload
        if p.get("result") not in PENDING_RESULTS:
            continue
        iid = p.get("intent_id")
        if not iid or iid in resolved_intent_ids:
            continue
        if _ts(r) < session_start:
            continue  # stale prior-session intent — never resurrect
        if iid not in first_ts or _ts(r) < first_ts[iid]:
            first_ts[iid] = _ts(r)
        if iid not in latest or _ts(r) >= _ts(latest[iid]):
            latest[iid] = r

    return sorted(latest.values(), key=lambda r: first_ts[r.payload["intent_id"]])


def count_recovery_attempts(
    records: list[LogRecord], intent_id: str
) -> int:
    """Count prior ``recovery_attempt`` incidents for this intent.

    Sourced from the log (not memory) so the escalation budget survives a
    process restart.
    """
    n = 0
    for r in records:
        if r.kind is RecordKind.INCIDENT:
            p = r.payload
            if p.get("kind") == "recovery_attempt" and p.get("intent_id") == intent_id:
                n += 1
    return n


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #


def _attempt_incident(intent_id: str, reason: str, detail: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "kind": "recovery_attempt",
        "intent_id": intent_id,
        "reason": reason,
    }
    if detail is not None:
        payload["detail"] = detail
    return payload


def _resolve_one(
    intent_rec: LogRecord,
    broker: RecoveryBroker,
    now: datetime,
    *,
    prior_attempts: int,
    max_attempts: int,
    visibility_grace: timedelta,
    poll_timeout_s: float,
) -> RecoveryOutcome:
    p = intent_rec.payload
    iid: str = p["intent_id"]
    coid: str | None = p.get("client_order_id")
    age = now - intent_rec.ts

    if not coid:
        # No COID was ever recorded → the order never reached the broker
        # build/submit. No exposure. Freeze the intent terminally.
        return RecoveryOutcome(
            kind=OutcomeKind.UNSUBMITTED,
            intent_id=iid,
            client_order_id=None,
            intent_payload=p,
            result_payload={
                "intent_id": iid,
                "client_order_id": None,
                "broker_order_id": None,
                "status": "unsubmitted",
                "rejected_reason": "no client_order_id recorded; never submitted",
            },
        )

    submitted = broker.resolve_by_coid(coid)

    if submitted is None:
        # Broker has no order for this COID.
        if age < visibility_grace:
            # Could simply not be visible yet — wait silently, no incident.
            return RecoveryOutcome(
                kind=OutcomeKind.RETRY,
                intent_id=iid,
                client_order_id=coid,
                intent_payload=p,
                incident=None,
            )
        if prior_attempts + 1 < max_attempts:
            return RecoveryOutcome(
                kind=OutcomeKind.RETRY,
                intent_id=iid,
                client_order_id=coid,
                intent_payload=p,
                incident=_attempt_incident(iid, "coid_not_found"),
            )
        return RecoveryOutcome(
            kind=OutcomeKind.ESCALATE_HALT,
            intent_id=iid,
            client_order_id=coid,
            intent_payload=p,
            halt_name="recovery_unresolved",
            halt_reason=f"{iid}: order not found at broker after {max_attempts} attempts",
            incident=_attempt_incident(iid, "coid_not_found_escalated"),
        )

    # Order exists at the broker — drive it to a terminal state.
    try:
        terminal = broker.poll_terminal(coid, timeout_s=poll_timeout_s)
    except OrderOutcomeUnknown as exc:
        if prior_attempts + 1 < max_attempts:
            return RecoveryOutcome(
                kind=OutcomeKind.RETRY,
                intent_id=iid,
                client_order_id=coid,
                intent_payload=p,
                incident=_attempt_incident(iid, "still_working", str(exc)),
            )
        return RecoveryOutcome(
            kind=OutcomeKind.ESCALATE_HALT,
            intent_id=iid,
            client_order_id=coid,
            intent_payload=p,
            halt_name="recovery_unresolved",
            halt_reason=f"{iid}: not terminal after {max_attempts} attempts",
            incident=_attempt_incident(iid, "still_working_escalated", str(exc)),
        )

    # Terminal reached. Hand the broker truth back; the orchestrator's
    # existing entry recorder decides fill vs no-fill and writes the
    # RESULT + open_trades (with the recovered protective child COID).
    return RecoveryOutcome(
        kind=OutcomeKind.RESOLVED,
        intent_id=iid,
        client_order_id=coid,
        intent_payload=p,
        submitted=submitted,
        terminal=terminal,
    )


def recover_pending_submissions(
    records: list[LogRecord],
    broker: RecoveryBroker,
    now: datetime,
    *,
    session_start: datetime,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    visibility_grace: timedelta = DEFAULT_VISIBILITY_GRACE,
    poll_timeout_s: float = DEFAULT_RECOVERY_POLL_TIMEOUT_S,
) -> list[RecoveryOutcome]:
    """Resolve every pending submission, oldest-first.

    Returns the ordered outcomes. Stops at the first ESCALATE_HALT
    (a sticky halt supersedes — there is no point resolving further
    intents until an operator intervenes), returning the outcomes
    collected up to and including it.
    """
    pending = discover_pending_intents(records, session_start=session_start)
    outcomes: list[RecoveryOutcome] = []
    for rec in pending:
        iid = rec.payload["intent_id"]
        prior = count_recovery_attempts(records, iid)
        outcome = _resolve_one(
            rec,
            broker,
            now,
            prior_attempts=prior,
            max_attempts=max_attempts,
            visibility_grace=visibility_grace,
            poll_timeout_s=poll_timeout_s,
        )
        outcomes.append(outcome)
        if outcome.kind is OutcomeKind.ESCALATE_HALT:
            break
    return outcomes
