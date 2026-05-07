"""Regression tests for trade-log → Postgres ingestion.

Engine RESULT records identify the trade by `intent_id` and don't echo
`symbol`/`side` at the top of the payload. Earlier the ingestion path
read those keys directly, leaving `trades.symbol` empty, every
`trades.side` = "buy", and `trades.qty` = 0. These tests pin the
extractors and the end-to-end behaviour for entry + close fills.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from app import config as app_config
from app import incident_sync
from app.incident_sync import (
    _extract_qty,
    _extract_side,
    _extract_symbol,
    _parse_intent_id,
)


# ----- pure helpers -----------------------------------------------------

def test_parse_intent_id_entry():
    assert _parse_intent_id(
        "entry-MSFT-2026-05-07T13:41:05.108434+00:00"
    ) == ("entry", "MSFT")


def test_parse_intent_id_close():
    assert _parse_intent_id(
        "close-MSFT-2026-05-07T13:42:46.078582+00:00"
    ) == ("close", "MSFT")


def test_parse_intent_id_short_and_cover():
    assert _parse_intent_id("short-AAPL-2026-...")[0] == "short"
    assert _parse_intent_id("cover-AAPL-2026-...")[0] == "cover"


def test_parse_intent_id_handles_garbage():
    assert _parse_intent_id("") == ("", "")
    assert _parse_intent_id("noformat") == ("", "")


def test_extract_symbol_prefers_explicit_field():
    assert _extract_symbol({"symbol": "AAPL", "intent_id": "entry-MSFT-..."}) == "AAPL"


def test_extract_symbol_from_intent_id_when_missing():
    assert _extract_symbol(
        {"intent_id": "entry-MSFT-2026-05-07T13:41:05.108434+00:00"}
    ) == "MSFT"


def test_extract_symbol_empty_when_no_signal():
    assert _extract_symbol({}) == ""


def test_extract_side_prefers_explicit_field_lowercased():
    assert _extract_side({"side": "Sell"}) == "sell"


@pytest.mark.parametrize("action,expected", [
    ("entry", "buy"),
    ("cover", "buy"),
    ("close", "sell"),
    ("short", "sell"),
    ("exit", "sell"),
])
def test_extract_side_maps_action_to_side(action, expected):
    assert _extract_side({"intent_id": f"{action}-MSFT-..."}) == expected


def test_extract_side_unknown_action_defaults_to_buy():
    # Defensive: the constraint on trades.side requires buy or sell, so we
    # can't return an arbitrary string. Future engine actions should land in
    # _ACTION_TO_SIDE before reaching here in production.
    assert _extract_side({"intent_id": "weird-MSFT-..."}) == "buy"


def test_extract_qty_uses_filled_qty():
    assert _extract_qty({"filled_qty": 5}) == 5.0


def test_extract_qty_handles_string_numbers():
    assert _extract_qty({"filled_qty": "5"}) == 5.0


def test_extract_qty_falls_back_to_qty_then_qty_requested():
    assert _extract_qty({"qty": 3}) == 3.0
    assert _extract_qty({"qty_requested": 7}) == 7.0


def test_extract_qty_zero_when_unparseable_or_missing():
    assert _extract_qty({}) == 0.0
    assert _extract_qty({"filled_qty": "abc"}) == 0.0


# ----- end-to-end ingestion -----------------------------------------------

class _FakeResult:
    def scalar_one(self):
        return 0

    def scalar_one_or_none(self):
        return None


class _FakeDb:
    """Records every (sql_text, params) pair the worker submits."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        self.calls.append((str(stmt), dict(params) if params else {}))
        return _FakeResult()

    def commit(self):
        pass

    def rollback(self):
        pass


@pytest.fixture()
def fake_db_and_var(var_dir, monkeypatch) -> _FakeDb:
    monkeypatch.setattr(app_config.settings, "engine_var_dir", var_dir)
    db = _FakeDb()

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(incident_sync, "db_session", _session)
    return db


def _trade_inserts(db: _FakeDb) -> list[dict]:
    return [params for sql, params in db.calls if "INSERT INTO trades" in sql]


def _write_trade_log(var_dir: Path, records: list[dict]) -> None:
    with (var_dir / "trades.jsonl").open("w") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


ENTRY_RESULT = {
    "kind": "RESULT",
    "ts": "2026-05-07T13:41:13.331507+00:00",
    "payload": {
        "status": "filled",
        "intent_id": "entry-MSFT-2026-05-07T13:41:05.108434+00:00",
        "filled_qty": 5,
        "avg_fill_price": "426.73",
        "broker_order_id": "ac01c9a7-46f6-4088-8b30-71900999df18",
        "client_order_id": "TBv1-176569e2d27ff29f3f0e22ccc52ea89c50f8",
        "protective_child_status": "held",
    },
    "line_hash": "abc",
    "prev_hash": "def",
}

CLOSE_RESULT = {
    "kind": "RESULT",
    "ts": "2026-05-07T13:42:47.869833+00:00",
    "payload": {
        "reason": "primary_stop_hit",
        "status": "filled",
        "intent_id": "close-MSFT-2026-05-07T13:42:46.078582+00:00",
        "filled_qty": 5,
        "realized_pnl": "0.270",
        "avg_fill_price": "426.784",
        "broker_order_id": "bb58adaf-b2ff-417b-8933-bda264c8d0a5",
        "client_order_id": "TBv1-close-707ff488e5e740808919a5ad",
    },
    "line_hash": "fff",
    "prev_hash": "eee",
}


def test_sync_once_entry_result_populates_columns(var_dir, fake_db_and_var):
    _write_trade_log(var_dir, [ENTRY_RESULT])

    incident_sync.sync_once()

    rows = _trade_inserts(fake_db_and_var)
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "MSFT"
    assert row["side"] == "buy"
    assert row["qty"] == 5.0
    assert row["avg_fill_price"] == 426.73
    assert row["status"] == "filled"
    # Entry RESULTs don't carry realized_pnl
    assert row["pnl"] is None


def test_sync_once_close_result_populates_columns(var_dir, fake_db_and_var):
    _write_trade_log(var_dir, [CLOSE_RESULT])

    incident_sync.sync_once()

    rows = _trade_inserts(fake_db_and_var)
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "MSFT"
    assert row["side"] == "sell"          # was "buy" before fix
    assert row["qty"] == 5.0              # was 0 before fix
    assert row["avg_fill_price"] == 426.784
    assert row["pnl"] == pytest.approx(0.27)


def test_sync_once_explicit_payload_fields_still_win(var_dir, fake_db_and_var):
    """If a future engine version starts echoing symbol/side/qty back at the
    top of the payload, the explicit values must take precedence over the
    intent_id-parsed ones."""
    record = {
        "kind": "RESULT",
        "ts": "2026-05-07T14:00:00+00:00",
        "payload": {
            "status": "filled",
            "intent_id": "entry-MSFT-2026-05-07T14:00:00+00:00",
            "symbol": "AAPL",   # disagrees with intent_id
            "side": "Sell",
            "qty": 3,
            "filled_qty": 5,
            "avg_fill_price": "100",
        },
        "line_hash": "z",
        "prev_hash": "y",
    }
    _write_trade_log(var_dir, [record])

    incident_sync.sync_once()

    rows = _trade_inserts(fake_db_and_var)
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["side"] == "sell"
    # qty ladder: filled_qty wins (matches what the broker actually did)
    assert rows[0]["qty"] == 5.0
