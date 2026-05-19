"""Simulated TradeLog — in-memory, simulated-clock variant.

The production ``strategy.trade_log.TradeLog._append`` stamps records
with ``datetime.now(timezone.utc)`` (wall-clock). That breaks
backtest determinism: two runs of the same scenario would produce
different ``line_hash`` values purely from wall-clock drift.

This class implements the same duck-typed contract Strategy depends on
(``append_intent``/``append_result``/``append_incident``/``read_all``),
but takes a **clock callable** instead of using ``datetime.now``. The
driver feeds it the simulated tick clock, so two runs of the same
inputs produce byte-identical log streams (the foundation of 1.2's
hard determinism gate).

Hash-chain integrity uses the production helpers (``_hash_record``,
``_json_safe``, ``GENESIS_HASH``, ``RecordKind``, ``LogRecord``) so the
research log is structurally identical to the live one — no parallel
hashing implementation, no drift risk.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterator

# Reuse the production hashing primitives + record dataclass; no parallel
# implementation. If the production module changes, this breaks loudly
# in tests rather than silently.
from strategy.trade_log import (
    GENESIS_HASH,
    LogRecord,
    RecordKind,
    _hash_record,
    _json_safe,
)


class SimulatedTradeLog:
    """In-memory hash-chained trade log driven by an injected clock.

    Same duck-typed surface Strategy consumes via ``self.trade_log``.
    Records are kept in memory; :meth:`read_all` yields them in order.
    """

    def __init__(self, clock: Callable[[], datetime]) -> None:
        self._clock = clock
        self._records: list[LogRecord] = []
        self._last_hash: str = GENESIS_HASH

    # ---- public surface (matches strategy.trade_log.TradeLog) ----------

    def append_intent(self, payload: dict[str, Any]) -> LogRecord:
        return self._append(RecordKind.INTENT, payload)

    def append_result(self, payload: dict[str, Any]) -> LogRecord:
        return self._append(RecordKind.RESULT, payload)

    def append_incident(self, payload: dict[str, Any]) -> LogRecord:
        if "kind" not in payload:
            raise ValueError("incident payload requires a 'kind' field")
        return self._append(RecordKind.INCIDENT, payload)

    def read_all(self) -> Iterator[LogRecord]:
        # Snapshot copy so callers iterating during a later append do not
        # observe mid-iteration mutation.
        return iter(list(self._records))

    def verify_integrity(self) -> None:
        # By construction the chain is consistent — but verify cheaply.
        prev = GENESIS_HASH
        for i, rec in enumerate(self._records):
            if rec.prev_hash != prev:
                raise AssertionError(
                    f"sim trade log: prev_hash break at record {i}"
                )
            expected = _hash_record(rec.kind, rec.ts, rec.payload, rec.prev_hash)
            if rec.line_hash != expected:
                raise AssertionError(
                    f"sim trade log: line_hash mismatch at record {i}"
                )
            prev = rec.line_hash

    # ---- read-only helpers (not in the production API, useful here) ----

    def records(self) -> tuple[LogRecord, ...]:
        return tuple(self._records)

    def __len__(self) -> int:
        return len(self._records)

    # ---- internals -----------------------------------------------------

    def _append(self, kind: RecordKind, payload: dict[str, Any]) -> LogRecord:
        ts = self._clock()
        if ts.tzinfo is None:
            raise ValueError("SimulatedTradeLog clock must return UTC tz-aware")
        prev = self._last_hash
        line_hash = _hash_record(kind, ts, payload, prev)
        rec = LogRecord(
            kind=kind, ts=ts,
            payload=_json_safe(payload),
            prev_hash=prev, line_hash=line_hash,
        )
        self._records.append(rec)
        self._last_hash = line_hash
        return rec
