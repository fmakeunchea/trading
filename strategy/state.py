"""Durable strategy state with crash-safe atomic writes.

Design notes:

* Atomic write: we write to a sibling ``.tmp`` file, ``fsync`` it, then
  ``rename`` it over the live file, then ``fsync`` the containing
  directory. POSIX ``rename`` is atomic within a filesystem, so a
  crash can leave us with either the old file or the new file — never
  a half-written one.
* ``peak_equity`` is monotonic upward. ``advance_peak_equity`` is the
  *only* API that can move it, and it only accepts reconciled broker
  equity (the caller's contract). This is enforced by having no setter
  that takes a raw value plus a property docstring in the dataclass.
* Schema version lives in the on-disk file. A mismatch raises
  :class:`StateCorruption`.
* On day rollover, intraday-scoped fields (realized_pnl_today,
  intraday_low_equity, last_loss_ts_by_symbol) are reset. ``peak_equity``
  and ``open_trades`` cross session boundaries (open_trades only by the
  force-flatten policy enforced at the orchestrator layer; state itself
  doesn't decide when to flatten).
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from strategy.dto import HaltRecord, OpenTrade, OrderSide
from strategy.errors import StateCorruption


SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# StrategyState dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StrategyState:
    schema_version: int = SCHEMA_VERSION
    trading_day: date = field(default_factory=lambda: date(1970, 1, 1))
    peak_equity: Decimal = Decimal(0)
    last_reconciled_equity: Decimal = Decimal(0)
    intraday_low_equity: Decimal = Decimal(0)
    realized_pnl_today: Decimal = Decimal(0)
    open_trades: dict[str, OpenTrade] = field(default_factory=dict)
    halts: dict[str, HaltRecord] = field(default_factory=dict)
    last_loss_ts_by_symbol: dict[str, datetime] = field(default_factory=dict)

    # ---- mutators with invariants ---------------------------------------

    def advance_peak_equity(self, reconciled_equity: Decimal) -> None:
        """Ratchet peak_equity upward only. Never regresses.

        ``reconciled_equity`` must come from a successful broker
        reconciliation — the caller's contract. This method does not
        itself call the broker; it just enforces the monotonic ratchet.
        """
        if reconciled_equity > self.peak_equity:
            self.peak_equity = reconciled_equity

    def record_reconciled_equity(self, reconciled_equity: Decimal) -> None:
        """Update reconciled + intraday-low. peak is ratcheted separately."""
        self.last_reconciled_equity = reconciled_equity
        if self.intraday_low_equity == 0 or reconciled_equity < self.intraday_low_equity:
            self.intraday_low_equity = reconciled_equity
        self.advance_peak_equity(reconciled_equity)

    def set_halt(self, name: str, reason: str, *, now: datetime) -> None:
        self.halts[name] = HaltRecord(
            name=name, active=True, triggered_at=now, reason=reason
        )

    def clear_halt(self, name: str) -> None:
        self.halts.pop(name, None)

    def has_halt(self, name: str) -> bool:
        rec = self.halts.get(name)
        return rec is not None and rec.active

    def any_halt_active(self) -> bool:
        return any(rec.active for rec in self.halts.values())

    def roll_to_new_day(self, new_day: date) -> None:
        """Reset intraday-scoped fields. Preserve peak_equity and open_trades.

        open_trades are preserved because the force-flatten policy lives
        at the orchestrator layer and should be the one to remove them —
        StrategyState is deliberately policy-free.
        """
        if new_day <= self.trading_day and self.trading_day != date(1970, 1, 1):
            raise ValueError(f"new_day {new_day} is not after {self.trading_day}")
        self.trading_day = new_day
        self.realized_pnl_today = Decimal(0)
        self.intraday_low_equity = self.last_reconciled_equity
        self.last_loss_ts_by_symbol = {}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class StateStore:
    """Atomic file-backed persistence for :class:`StrategyState`."""

    def __init__(self, path: Path | str, *, fsync: bool = True) -> None:
        self._path = Path(path)
        self._fsync = fsync
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def exists(self) -> bool:
        return self._path.is_file()

    # -------- save -------------------------------------------------------

    def save(self, state: StrategyState) -> None:
        """Atomic write: tempfile → fsync → rename → fsync(dir).

        If the process dies before the ``rename``, the previous file
        remains intact. The ``.tmp`` sibling may be left behind and is
        harmless — it will be overwritten on the next successful save.
        """
        payload = _encode(state)
        parent = self._path.parent
        # tempfile in the same directory so the rename is same-fs atomic.
        fd, tmp = tempfile.mkstemp(
            prefix=self._path.name + ".",
            suffix=".tmp",
            dir=str(parent),
        )
        tmp_path = Path(tmp)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, sort_keys=True, separators=(",", ":"))
                f.flush()
                if self._fsync:
                    os.fsync(f.fileno())
            os.replace(tmp_path, self._path)   # atomic within fs
            if self._fsync:
                dir_fd = os.open(str(parent), os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except Exception:
            # Clean up orphaned tempfile if we blew up before rename.
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise

    # -------- load -------------------------------------------------------

    def load(self) -> StrategyState:
        """Load, validate schema, return :class:`StrategyState`.

        Raises :class:`StateCorruption` on any problem — we never
        silently continue on broken state.
        """
        if not self._path.exists():
            return StrategyState()
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise StateCorruption(
                f"state file {self._path} is unreadable or malformed: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise StateCorruption("state root must be an object")

        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise StateCorruption(
                f"state schema version {version!r} does not match {SCHEMA_VERSION}"
            )

        try:
            return _decode(data)
        except (KeyError, TypeError, ValueError) as exc:
            raise StateCorruption(f"state decode failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Encode / decode
# ---------------------------------------------------------------------------


def _encode(s: StrategyState) -> dict[str, Any]:
    return {
        "schema_version": s.schema_version,
        "trading_day": s.trading_day.isoformat(),
        "peak_equity": str(s.peak_equity),
        "last_reconciled_equity": str(s.last_reconciled_equity),
        "intraday_low_equity": str(s.intraday_low_equity),
        "realized_pnl_today": str(s.realized_pnl_today),
        "open_trades": {sym: _encode_open_trade(t) for sym, t in s.open_trades.items()},
        "halts": {
            name: {
                "name": h.name,
                "active": h.active,
                "triggered_at": h.triggered_at.isoformat(),
                "reason": h.reason,
            }
            for name, h in s.halts.items()
        },
        "last_loss_ts_by_symbol": {
            sym: ts.isoformat() for sym, ts in s.last_loss_ts_by_symbol.items()
        },
    }


def _encode_open_trade(t: OpenTrade) -> dict[str, Any]:
    return {
        "symbol": t.symbol,
        "qty": t.qty,
        "entry_price": str(t.entry_price),
        "entry_ts": t.entry_ts.isoformat(),
        "stop_price": str(t.stop_price),
        "disaster_stop_price": str(t.disaster_stop_price),
        "target_price": str(t.target_price),
        "intent_id": t.intent_id,
        "parent_client_order_id": t.parent_client_order_id,
        "protective_child_client_order_id": t.protective_child_client_order_id,
        "protective_child_broker_id": t.protective_child_broker_id,
        "last_seen_broker_qty": t.last_seen_broker_qty,
        # Trailing-stop bookkeeping (optional; serialised as str | None
        # so pre-trailing state.json files decode back to None defaults).
        "entry_atr": None if t.entry_atr is None else str(t.entry_atr),
        "highest_seen_price": None if t.highest_seen_price is None else str(t.highest_seen_price),
    }


def _decode(data: dict[str, Any]) -> StrategyState:
    return StrategyState(
        schema_version=int(data["schema_version"]),
        trading_day=date.fromisoformat(data["trading_day"]),
        peak_equity=Decimal(data["peak_equity"]),
        last_reconciled_equity=Decimal(data["last_reconciled_equity"]),
        intraday_low_equity=Decimal(data["intraday_low_equity"]),
        realized_pnl_today=Decimal(data["realized_pnl_today"]),
        open_trades={
            sym: _decode_open_trade(t) for sym, t in data.get("open_trades", {}).items()
        },
        halts={
            name: HaltRecord(
                name=h["name"],
                active=bool(h["active"]),
                triggered_at=datetime.fromisoformat(h["triggered_at"]),
                reason=h["reason"],
            )
            for name, h in data.get("halts", {}).items()
        },
        last_loss_ts_by_symbol={
            sym: datetime.fromisoformat(ts)
            for sym, ts in data.get("last_loss_ts_by_symbol", {}).items()
        },
    )


def _decode_open_trade(t: dict[str, Any]) -> OpenTrade:
    return OpenTrade(
        symbol=t["symbol"],
        qty=int(t["qty"]),
        entry_price=Decimal(t["entry_price"]),
        entry_ts=datetime.fromisoformat(t["entry_ts"]),
        stop_price=Decimal(t["stop_price"]),
        disaster_stop_price=Decimal(t["disaster_stop_price"]),
        target_price=Decimal(t["target_price"]),
        intent_id=t["intent_id"],
        parent_client_order_id=t["parent_client_order_id"],
        protective_child_client_order_id=t.get("protective_child_client_order_id"),
        protective_child_broker_id=t.get("protective_child_broker_id"),
        last_seen_broker_qty=int(t["last_seen_broker_qty"]),
        # Backwards-compat: pre-trailing state.json has no entry_atr /
        # highest_seen_price keys; .get(...) returns None and the trailing
        # logic seeds them on first quote tick.
        entry_atr=(Decimal(t["entry_atr"]) if t.get("entry_atr") is not None else None),
        highest_seen_price=(
            Decimal(t["highest_seen_price"])
            if t.get("highest_seen_price") is not None else None
        ),
    )
