"""Background worker: mirror trades.jsonl INCIDENT/RESULT records into Postgres.

Runs on FastAPI startup as an asyncio task. Polls the file every few seconds;
for an append-only log with low write rate this is fine. If volumes grow, swap
for inotify (linux) / fsevents.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone, date

from sqlalchemy import text

from . import engine_io
from .db import db_session

log = logging.getLogger("incident_sync")

POLL_INTERVAL_S = 3.0


def _stable_source_id(rec: dict) -> str:
    """Prefer engine's hash-chain digest; fall back to a content hash."""
    if "hash" in rec:
        return str(rec["hash"])
    payload = json.dumps(rec, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


# Engine RESULT records identify the trade by `intent_id`, formatted as
# "{action}-{SYMBOL}-{ts}" — they don't echo `symbol`/`side` at the top of
# the payload. The ingestion path therefore parses the id back out. Keep
# this mapping in sync with strategy.trade_log if new actions get added.
_ACTION_TO_SIDE: dict[str, str] = {
    "entry": "buy",
    "cover": "buy",
    "close": "sell",
    "short": "sell",
    "exit": "sell",
}


def _parse_intent_id(intent_id: str) -> tuple[str, str]:
    """Split an intent_id into (action, symbol). Returns ('', '') on bad input."""
    if not intent_id:
        return "", ""
    parts = intent_id.split("-", 2)
    if len(parts) < 2:
        return "", ""
    return parts[0].lower(), parts[1]


def _extract_symbol(payload: dict) -> str:
    if payload.get("symbol"):
        return str(payload["symbol"])
    _, sym = _parse_intent_id(str(payload.get("intent_id") or ""))
    return sym


def _extract_side(payload: dict) -> str:
    side = payload.get("side")
    if side:
        return str(side).lower()
    action, _ = _parse_intent_id(str(payload.get("intent_id") or ""))
    return _ACTION_TO_SIDE.get(action, "buy")


def _extract_qty(payload: dict) -> float:
    for key in ("filled_qty", "qty", "qty_requested"):
        v = payload.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return 0.0


def _parse_ts(rec: dict) -> datetime:
    ts = rec.get("timestamp") or rec.get("ts") or rec.get("at")
    if not ts:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


def sync_once() -> None:
    """Mirror engine trade-log records into Postgres.

    Engine record shape is:
        {"kind": "INCIDENT|RESULT|...", "ts": ..., "payload": {...},
         "prev_hash": ..., "line_hash": ...}

    Almost every interesting field (symbol, reason, side, qty, etc.) lives
    inside `payload`. Earlier versions of this module read the top-level
    keys, which silently produced rows full of nulls.
    """
    with db_session() as db:
        for rec in engine_io.iter_trade_log():
            top_kind = rec.get("kind")
            if top_kind not in {"INCIDENT", "RESULT"}:
                continue
            payload = rec.get("payload", {}) or {}
            source_id = _stable_source_id(rec)
            if top_kind == "INCIDENT":
                # Sub-kind ("HALT", "RECONCILE", "DIAGNOSTIC", ...) is in
                # payload["kind"]; required by the engine for INCIDENT records.
                sub_kind = payload.get("kind") or "INCIDENT"
                db.execute(
                    text(
                        """
                        INSERT INTO incidents
                          (source_id, kind, severity, phase, symbols, reason, payload, occurred_at)
                        VALUES
                          (:source_id, :kind, :severity, :phase, :symbols, :reason,
                           CAST(:payload AS JSONB), :occurred_at)
                        ON CONFLICT (source_id) DO NOTHING
                        """
                    ),
                    {
                        "source_id": source_id,
                        "kind": sub_kind,
                        "severity": payload.get("severity", "info"),
                        "phase": payload.get("phase"),
                        "symbols": _as_symbol_list(payload),
                        "reason": payload.get("reason"),
                        "payload": json.dumps(rec, default=str),
                        "occurred_at": _parse_ts(rec),
                    },
                )
            else:  # RESULT
                db.execute(
                    text(
                        """
                        INSERT INTO trades
                          (source_id, symbol, side, qty, avg_fill_price, status, pnl, payload, occurred_at)
                        VALUES
                          (:source_id, :symbol, :side, :qty, :avg_fill_price, :status, :pnl,
                           CAST(:payload AS JSONB), :occurred_at)
                        ON CONFLICT (source_id) DO NOTHING
                        """
                    ),
                    {
                        "source_id": source_id,
                        "symbol": _extract_symbol(payload),
                        "side": _extract_side(payload),
                        "qty": _extract_qty(payload),
                        "avg_fill_price": _maybe_float(payload.get("avg_fill_price")),
                        "status": payload.get("status", "unknown"),
                        "pnl": _maybe_float(payload.get("realized_pnl") or payload.get("pnl")),
                        "payload": json.dumps(rec, default=str),
                        "occurred_at": _parse_ts(rec),
                    },
                )

        _refresh_bot_state(db)


def _as_symbol_list(payload: dict) -> list[str]:
    """Normalise to a list[str] of symbols. Engine sometimes uses
    `symbols` (plural list) for halts/reconciles, sometimes `symbol`
    (singular) for per-symbol diagnostics."""
    if "symbols" in payload and isinstance(payload["symbols"], list):
        return [str(s) for s in payload["symbols"]]
    if "symbol" in payload and payload["symbol"]:
        return [str(payload["symbol"])]
    return []


def _refresh_bot_state(db) -> None:
    hb = engine_io.heartbeat()
    today = date.today()
    # Critical = anything that isn't an observability diagnostic. Diagnostic
    # records are emitted by the engine for transparency on quiet days
    # (bars_fetched, no_signal, risk_denied, etc.) and must not count
    # toward the operator-facing "incidents today" badge.
    incidents_today = db.execute(
        text(
            "SELECT COUNT(*) FROM incidents "
            "WHERE occurred_at::date = :d AND kind != 'DIAGNOSTIC'"
        ),
        {"d": today},
    ).scalar_one()
    diagnostics_today = db.execute(
        text(
            "SELECT COUNT(*) FROM incidents "
            "WHERE occurred_at::date = :d AND kind = 'DIAGNOSTIC'"
        ),
        {"d": today},
    ).scalar_one()
    positions = engine_io.open_positions()
    active_mode = db.execute(
        text("SELECT mode FROM strategies WHERE is_active LIMIT 1")
    ).scalar_one_or_none()
    db.execute(
        text(
            """
            UPDATE bot_state SET
              running = :running,
              mode = :mode,
              kill_switch_engaged = :kill,
              heartbeat_at = :hb,
              reconcile_ok = :reconcile_ok,
              reconcile_last_checked = now(),
              broker_connected = :broker_connected,
              broker_last_contacted = :broker_last,
              open_positions_count = :npos,
              open_positions = CAST(:positions AS JSONB),
              incidents_today = :incidents_today,
              diagnostics_today = :diagnostics_today,
              updated_at = now()
            WHERE id = 1
            """
        ),
        {
            "running": engine_io.bot_pid_alive(),
            "mode": active_mode,
            "kill": engine_io.kill_switch_engaged(),
            "hb": hb,
            "reconcile_ok": engine_io.last_reconcile_ok(),
            "broker_connected": engine_io.heartbeat_fresh(),
            "broker_last": engine_io.broker_last_contact(),
            "npos": len(positions),
            "positions": json.dumps(positions),
            "incidents_today": incidents_today,
            "diagnostics_today": diagnostics_today,
        },
    )


def _maybe_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def run_forever() -> None:
    log.info("incident_sync started (poll=%.1fs)", POLL_INTERVAL_S)
    while True:
        try:
            await asyncio.to_thread(sync_once)
        except Exception:
            log.exception("incident_sync tick failed")
        await asyncio.sleep(POLL_INTERVAL_S)
