"""Read-side integration with the existing trading engine.

The engine writes four artifacts to `./var/`:
    - trading_bot.lock        PID file (liveness proxy)
    - trading_bot.heartbeat   ISO timestamp, rewritten each tick
    - trading_bot.kill        existence = kill-switch engaged
    - state.json              StrategyState dataclass JSON
    - trades.jsonl            append-only hash-chained audit log

This module reads them. It does NOT import anything from the engine — it
treats the files as a contract so the engine can be bumped independently.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .config import settings


def _var(name: str) -> Path:
    return settings.engine_var_dir / name


def kill_switch_engaged() -> bool:
    return _var("trading_bot.kill").exists()


def engage_kill_switch() -> None:
    settings.engine_var_dir.mkdir(parents=True, exist_ok=True)
    _var("trading_bot.kill").touch()


def disengage_kill_switch() -> None:
    try:
        _var("trading_bot.kill").unlink()
    except FileNotFoundError:
        pass


def heartbeat() -> datetime | None:
    p = _var("trading_bot.heartbeat")
    if not p.exists():
        return None
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def heartbeat_fresh(now: datetime | None = None) -> bool:
    hb = heartbeat()
    if hb is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - hb).total_seconds() < settings.heartbeat_stale_seconds


def bot_pid_alive() -> bool:
    """True iff the engine's lock file points to a live process."""
    lock = _var("trading_bot.lock")
    if not lock.exists():
        return False
    try:
        pid = int(lock.read_text().strip())
        os.kill(pid, 0)  # signal 0 == existence check
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False
    return True


def read_state() -> dict:
    """Returns the engine's state.json as a dict, or {} if missing/corrupt."""
    p = _var("state.json")
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def open_positions() -> list[dict]:
    """Extract open positions from state.json.

    The engine stores open_trades as a dict keyed by symbol. We normalise
    to the Position schema so the API layer stays dumb.
    """
    state = read_state()
    open_trades = state.get("open_trades", {}) or {}
    out: list[dict] = []
    for sym, t in open_trades.items():
        if not isinstance(t, dict):
            continue
        qty = float(t.get("qty", 0) or 0)
        side = "long" if qty >= 0 else "short"
        out.append(
            {
                "symbol": sym,
                "qty": abs(qty),
                "avg_price": _maybe_float(t.get("avg_price") or t.get("entry_price")),
                "side": side,
                "unrealized_pnl": _maybe_float(t.get("unrealized_pnl")),
            }
        )
    return out


def last_reconcile_ok() -> bool | None:
    """Best-effort: the most recent RECONCILE incident's outcome."""
    for rec in tail_trade_log(limit=500, kinds={"INCIDENT"}):
        payload = rec.get("payload", {}) or {}
        if rec.get("phase") == "RECONCILE" or payload.get("phase") == "RECONCILE":
            reason = (payload.get("reason") or "").lower()
            return "mismatch" not in reason and "orphan" not in reason
    return None


def broker_last_contact() -> datetime | None:
    state = read_state()
    ts = state.get("broker_last_contact") or state.get("last_broker_ok_at")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def tail_trade_log(limit: int = 200, kinds: set[str] | None = None) -> list[dict]:
    """Return the last `limit` records from trades.jsonl, most-recent first.

    For MVP we read the whole file and slice — it's an append-only log and
    will be rotated well before this matters. Revisit with a seek-from-end
    reader once files exceed ~10MB.
    """
    p = _var("trades.jsonl")
    if not p.exists():
        return []
    lines: list[dict] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kinds and rec.get("kind") not in kinds:
                continue
            lines.append(rec)
    return list(reversed(lines[-limit:]))


def _maybe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def iter_trade_log() -> Iterator[dict]:
    """Full-file iterator for the background incident sync worker."""
    p = _var("trades.jsonl")
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
