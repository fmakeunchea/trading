"""Append-only, hash-chained JSONL audit log.

Design notes:

* The trade log is the bot's write-ahead log as well as its audit trail.
  INTENT records are written *before* the broker is contacted so that a
  crash between INTENT and broker submission can be recovered by
  reconciling the INTENT's COID against the broker on restart.
* The log is append-only by contract: there is deliberately no update or
  delete API. ``test_trade_log`` asserts this surface.
* Every record carries ``prev_hash`` + ``line_hash``. Tampering with any
  record breaks the chain and :meth:`verify_integrity` raises
  :class:`AuditIntegrityError`.
* ``fsync`` is called after each write so a power-loss cannot leave the
  INTENT record in the page cache but not on disk (the rule of thumb:
  if the broker could have received it, the log *must* have it).
* Daily SEAL records close a file at session rollover; the rolled file
  is renamed to include the sealed trading-day so reconstruction is
  deterministic.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from strategy.errors import AuditIntegrityError


log = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64


class RecordKind(str, Enum):
    INTENT = "INTENT"
    RESULT = "RESULT"
    SEAL = "SEAL"
    INCIDENT = "INCIDENT"


@dataclass(frozen=True, slots=True)
class LogRecord:
    kind: RecordKind
    ts: datetime
    payload: dict[str, Any]
    prev_hash: str
    line_hash: str


# ---------------------------------------------------------------------------
# TradeLog
# ---------------------------------------------------------------------------


class TradeLog:
    """Append-only JSONL log with hash-chained integrity.

    Not thread-safe by design — the orchestrator is single-threaded.
    If that changes, wrap writes in an external lock.
    """

    def __init__(self, path: Path | str, *, fsync: bool = True) -> None:
        self._path = Path(path)
        self._fsync = fsync
        self._closed = False
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash = self._compute_last_hash()

    # -------- public API --------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    def append_intent(self, payload: dict[str, Any]) -> LogRecord:
        """Write an INTENT record. Must be called before broker submit."""
        return self._append(RecordKind.INTENT, payload)

    def append_result(self, payload: dict[str, Any]) -> LogRecord:
        """Write a RESULT record after a broker terminal status."""
        return self._append(RecordKind.RESULT, payload)

    def append_incident(self, payload: dict[str, Any]) -> LogRecord:
        """Write an INCIDENT record for a safety event.

        Used for: halts (daily_loss / drawdown / recovery), reconcile
        mismatches that block entries, orphan protective orders, and
        position side mismatches. Payload must include a ``kind`` field
        so reports can histogram by incident type.
        """
        if "kind" not in payload:
            raise ValueError("incident payload requires a 'kind' field")
        return self._append(RecordKind.INCIDENT, payload)

    def append_seal(self, trading_day: date, line_count: int) -> LogRecord:
        """Write the end-of-day SEAL record and close the current file.

        After sealing, :meth:`append_intent` / :meth:`append_result` will
        refuse to write — the caller should rotate to a fresh file.
        """
        payload = {
            "trading_day": trading_day.isoformat(),
            "line_count": line_count,
            "chain_hash": self._last_hash,
        }
        rec = self._append(RecordKind.SEAL, payload)
        self._closed = True
        return rec

    def read_all(self) -> Iterator[LogRecord]:
        """Yield every record in order.

        A trailing incomplete line (e.g. a crash mid-write) is skipped
        with a logged warning — it cannot be part of the committed
        audit trail because fsync is called after each full line.
        """
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                if not raw.endswith("\n"):
                    log.warning(
                        "trade log %s: dropping incomplete trailing line %d",
                        self._path,
                        lineno,
                    )
                    break
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    raise AuditIntegrityError(
                        f"trade log {self._path} line {lineno} is not valid JSON"
                    )
                try:
                    yield LogRecord(
                        kind=RecordKind(obj["kind"]),
                        ts=datetime.fromisoformat(obj["ts"]),
                        payload=obj["payload"],
                        prev_hash=obj["prev_hash"],
                        line_hash=obj["line_hash"],
                    )
                except (KeyError, ValueError) as exc:
                    raise AuditIntegrityError(
                        f"trade log {self._path} line {lineno} malformed: {exc}"
                    ) from exc

    def verify_integrity(self) -> None:
        """Walk the entire chain and raise on any break."""
        prev = GENESIS_HASH
        for i, rec in enumerate(self.read_all(), start=1):
            if rec.prev_hash != prev:
                raise AuditIntegrityError(
                    f"trade log {self._path}: prev_hash mismatch at record {i}"
                )
            expected = _hash_record(rec.kind, rec.ts, rec.payload, rec.prev_hash)
            if rec.line_hash != expected:
                raise AuditIntegrityError(
                    f"trade log {self._path}: line_hash mismatch at record {i}"
                )
            prev = rec.line_hash

    # -------- internals ---------------------------------------------------

    def _append(self, kind: RecordKind, payload: dict[str, Any]) -> LogRecord:
        if self._closed:
            raise AuditIntegrityError(
                f"trade log {self._path} is sealed; no further writes permitted"
            )
        ts = datetime.now(timezone.utc)
        prev = self._last_hash
        line_hash = _hash_record(kind, ts, payload, prev)
        rec = LogRecord(
            kind=kind,
            ts=ts,
            payload=_json_safe(payload),
            prev_hash=prev,
            line_hash=line_hash,
        )
        line = json.dumps(
            {
                "kind": rec.kind.value,
                "ts": rec.ts.isoformat(),
                "payload": rec.payload,
                "prev_hash": rec.prev_hash,
                "line_hash": rec.line_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        # O_APPEND guarantees atomic single-line append on POSIX.
        fd = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
            if self._fsync:
                os.fsync(fd)
        finally:
            os.close(fd)
        self._last_hash = line_hash
        return rec

    def _compute_last_hash(self) -> str:
        """Resolve the prev_hash to use for the next append.

        If the file doesn't exist: genesis. If it exists: verify the full
        chain and use the final ``line_hash`` as the prev for the next
        record — this guarantees integrity is checked before we ever
        write anything that could further extend a broken chain.
        """
        if not self._path.exists():
            return GENESIS_HASH
        last = GENESIS_HASH
        prev = GENESIS_HASH
        for i, rec in enumerate(self.read_all(), start=1):
            if rec.prev_hash != prev:
                raise AuditIntegrityError(
                    f"trade log {self._path}: prev_hash break at record {i}"
                )
            expected = _hash_record(rec.kind, rec.ts, rec.payload, rec.prev_hash)
            if rec.line_hash != expected:
                raise AuditIntegrityError(
                    f"trade log {self._path}: line_hash break at record {i}"
                )
            if rec.kind is RecordKind.SEAL:
                # If the last record is a SEAL, new appends are forbidden.
                self._closed = True
            else:
                self._closed = False
            prev = rec.line_hash
            last = rec.line_hash
        return last


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_record(
    kind: RecordKind, ts: datetime, payload: dict[str, Any], prev_hash: str
) -> str:
    canonical = json.dumps(
        {
            "kind": kind.value,
            "ts": ts.isoformat(),
            "payload": _json_safe(payload),
            "prev_hash": prev_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_safe(obj: Any) -> Any:
    """Convert Decimals, datetimes, and Path-like objects to JSON-safe
    primitives while preserving numeric precision (Decimal → str)."""
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    return obj
