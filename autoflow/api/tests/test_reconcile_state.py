"""Pin the heuristic that decides bot_state.reconcile_ok.

The engine emits a `reconcile_mismatch` INCIDENT only on the unhappy path;
on a clean tick nothing is written. So the API infers "ok" from the
absence of a recent mismatch while the heartbeat is fresh.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app import config as app_config
from app import engine_io


def _write_records(var_dir: Path, records: list[dict]) -> None:
    p = var_dir / "trades.jsonl"
    with p.open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _set_heartbeat(var_dir: Path, age_seconds: float) -> None:
    p = var_dir / "trading_bot.heartbeat"
    p.write_text("ok")
    mtime = datetime.now(timezone.utc).timestamp() - age_seconds
    os.utime(p, (mtime, mtime))


def test_reconcile_ok_none_when_no_heartbeat(var_dir, monkeypatch):
    monkeypatch.setattr(app_config.settings, "engine_var_dir", var_dir)
    assert engine_io.last_reconcile_ok() is None


def test_reconcile_ok_none_when_heartbeat_stale(var_dir, monkeypatch):
    monkeypatch.setattr(app_config.settings, "engine_var_dir", var_dir)
    _set_heartbeat(var_dir, age_seconds=600)
    assert engine_io.last_reconcile_ok() is None


def test_reconcile_ok_true_when_no_recent_mismatch(var_dir, monkeypatch):
    monkeypatch.setattr(app_config.settings, "engine_var_dir", var_dir)
    _set_heartbeat(var_dir, age_seconds=2)
    now_iso = datetime.now(timezone.utc).isoformat()
    _write_records(var_dir, [
        {"kind": "INCIDENT", "ts": now_iso,
         "payload": {"kind": "DIAGNOSTIC", "reason": "no_breakout", "symbol": "SPY"}},
    ])
    assert engine_io.last_reconcile_ok() is True


def test_reconcile_ok_true_when_log_empty(var_dir, monkeypatch):
    """Bot just started, heartbeat fresh, no records yet — best we can say
    is 'no evidence of trouble'. Returning True is the documented choice."""
    monkeypatch.setattr(app_config.settings, "engine_var_dir", var_dir)
    _set_heartbeat(var_dir, age_seconds=2)
    assert engine_io.last_reconcile_ok() is True


def test_reconcile_ok_false_when_recent_mismatch(var_dir, monkeypatch):
    monkeypatch.setattr(app_config.settings, "engine_var_dir", var_dir)
    _set_heartbeat(var_dir, age_seconds=2)
    now_iso = datetime.now(timezone.utc).isoformat()
    _write_records(var_dir, [
        {"kind": "INCIDENT", "ts": now_iso,
         "payload": {"kind": "reconcile_mismatch", "phase": "tick",
                     "missing_positions": ["AAPL"]}},
    ])
    assert engine_io.last_reconcile_ok() is False


def test_reconcile_ok_true_when_old_mismatch_outside_horizon(var_dir, monkeypatch):
    monkeypatch.setattr(app_config.settings, "engine_var_dir", var_dir)
    _set_heartbeat(var_dir, age_seconds=2)
    # Default horizon is heartbeat_stale_seconds * 4 = 30 * 4 = 120s.
    # Put the mismatch 5 minutes back.
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    _write_records(var_dir, [
        {"kind": "INCIDENT", "ts": old_ts,
         "payload": {"kind": "reconcile_mismatch", "phase": "tick"}},
    ])
    assert engine_io.last_reconcile_ok() is True


def test_reconcile_ok_false_overrides_later_diagnostics(var_dir, monkeypatch):
    """A diagnostic written after a recent mismatch must NOT mask the
    mismatch — both records are inside the horizon and we should still
    surface the failure."""
    monkeypatch.setattr(app_config.settings, "engine_var_dir", var_dir)
    _set_heartbeat(var_dir, age_seconds=2)
    now = datetime.now(timezone.utc)
    mismatch_ts = (now - timedelta(seconds=20)).isoformat()
    diagnostic_ts = (now - timedelta(seconds=5)).isoformat()
    _write_records(var_dir, [
        {"kind": "INCIDENT", "ts": mismatch_ts,
         "payload": {"kind": "reconcile_mismatch", "phase": "tick"}},
        {"kind": "INCIDENT", "ts": diagnostic_ts,
         "payload": {"kind": "DIAGNOSTIC", "reason": "no_breakout", "symbol": "SPY"}},
    ])
    assert engine_io.last_reconcile_ok() is False
