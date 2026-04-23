"""Tests for strategy.state.

Pins down:
* Atomic write — a crash between tempfile creation and rename leaves the
  previous file intact.
* peak_equity monotonicity — never regresses.
* Day rollover resets intraday fields, preserves peak_equity.
* Corrupt / schema-mismatched files raise StateCorruption (never silent
  default state).
* Round-trip preserves Decimal precision and timezone-aware datetimes.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from strategy.dto import HaltRecord, OpenTrade
from strategy.errors import StateCorruption
from strategy.state import SCHEMA_VERSION, StateStore, StrategyState


UTC = timezone.utc


def _make_state() -> StrategyState:
    s = StrategyState()
    s.trading_day = date(2026, 4, 23)
    s.peak_equity = Decimal("25500.00")
    s.last_reconciled_equity = Decimal("25100.25")
    s.intraday_low_equity = Decimal("25050.00")
    s.realized_pnl_today = Decimal("-150.00")
    s.open_trades["AAPL"] = OpenTrade(
        symbol="AAPL",
        qty=10,
        entry_price=Decimal("200.10"),
        entry_ts=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
        stop_price=Decimal("198.50"),
        disaster_stop_price=Decimal("196.00"),
        target_price=Decimal("202.00"),
        intent_id="i-001",
        parent_client_order_id="TBv1-abc",
        protective_child_client_order_id="TBv1-def",
        protective_child_broker_id="br-002",
        last_seen_broker_qty=10,
    )
    s.halts["manual"] = HaltRecord(
        name="manual",
        active=True,
        triggered_at=datetime(2026, 4, 23, 13, 0, tzinfo=UTC),
        reason="operator paused",
    )
    s.last_loss_ts_by_symbol["MSFT"] = datetime(2026, 4, 23, 13, 30, tzinfo=UTC)
    return s


# ---------------------------------------------------------------------------
# Load on missing file → default
# ---------------------------------------------------------------------------


def test_load_missing_returns_default(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    s = store.load()
    assert s.schema_version == SCHEMA_VERSION
    assert s.peak_equity == Decimal(0)
    assert s.open_trades == {}


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_round_trip_preserves_precision(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.json")
    original = _make_state()
    store.save(original)
    loaded = store.load()

    assert loaded.peak_equity == original.peak_equity
    assert loaded.realized_pnl_today == original.realized_pnl_today
    assert loaded.trading_day == original.trading_day
    assert loaded.open_trades["AAPL"].entry_price == Decimal("200.10")
    assert loaded.open_trades["AAPL"].entry_ts == datetime(2026, 4, 23, 14, 0, tzinfo=UTC)
    assert loaded.halts["manual"].reason == "operator paused"
    assert loaded.last_loss_ts_by_symbol["MSFT"] == datetime(2026, 4, 23, 13, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Atomic write: old file intact on failure
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_old_file_intact_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save(_make_state())
    before = store.path.read_text(encoding="utf-8")

    # Force os.replace to fail mid-save. The previous file must survive.
    import strategy.state as state_mod

    def boom(*_a, **_kw):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(state_mod.os, "replace", boom)

    new_state = _make_state()
    new_state.peak_equity = Decimal("99999.00")
    with pytest.raises(OSError, match="simulated crash"):
        store.save(new_state)

    after = store.path.read_text(encoding="utf-8")
    assert after == before  # prior state fully preserved

    # No leftover .tmp files beside the main file.
    siblings = [p.name for p in tmp_path.iterdir()]
    tmp_leftovers = [n for n in siblings if n.endswith(".tmp")]
    assert tmp_leftovers == [], f"tempfiles not cleaned up: {tmp_leftovers}"


# ---------------------------------------------------------------------------
# Corruption / schema handling
# ---------------------------------------------------------------------------


def test_corrupt_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("{not json", encoding="utf-8")
    store = StateStore(p)
    with pytest.raises(StateCorruption):
        store.load()


def test_non_object_root_raises(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("[]", encoding="utf-8")
    store = StateStore(p)
    with pytest.raises(StateCorruption):
        store.load()


def test_schema_version_mismatch_raises(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    store = StateStore(p)
    store.save(_make_state())
    # Corrupt the schema_version field.
    data = json.loads(p.read_text(encoding="utf-8"))
    data["schema_version"] = 999
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateCorruption, match="schema version"):
        store.load()


def test_missing_required_field_raises(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    store = StateStore(p)
    store.save(_make_state())
    data = json.loads(p.read_text(encoding="utf-8"))
    del data["peak_equity"]
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StateCorruption):
        store.load()


# ---------------------------------------------------------------------------
# peak_equity monotonicity
# ---------------------------------------------------------------------------


def test_advance_peak_ratchets_up() -> None:
    s = StrategyState()
    s.peak_equity = Decimal("1000")
    s.advance_peak_equity(Decimal("1100"))
    assert s.peak_equity == Decimal("1100")
    s.advance_peak_equity(Decimal("1050"))  # lower → ignored
    assert s.peak_equity == Decimal("1100")
    s.advance_peak_equity(Decimal("1100"))  # equal → no-op
    assert s.peak_equity == Decimal("1100")


def test_record_reconciled_updates_intraday_low_and_peak() -> None:
    s = StrategyState()
    s.record_reconciled_equity(Decimal("1000"))
    assert s.peak_equity == Decimal("1000")
    assert s.intraday_low_equity == Decimal("1000")
    s.record_reconciled_equity(Decimal("900"))
    assert s.peak_equity == Decimal("1000")   # unchanged
    assert s.intraday_low_equity == Decimal("900")
    s.record_reconciled_equity(Decimal("950"))
    assert s.intraday_low_equity == Decimal("900")  # sticky


# ---------------------------------------------------------------------------
# Halts
# ---------------------------------------------------------------------------


def test_halts_set_and_clear() -> None:
    s = StrategyState()
    now = datetime(2026, 4, 23, 14, 0, tzinfo=UTC)
    s.set_halt("daily_loss", "cap breached", now=now)
    assert s.has_halt("daily_loss") is True
    assert s.any_halt_active() is True
    s.clear_halt("daily_loss")
    assert s.has_halt("daily_loss") is False
    assert s.any_halt_active() is False


# ---------------------------------------------------------------------------
# Day rollover
# ---------------------------------------------------------------------------


def test_roll_to_new_day_resets_intraday_preserves_peak() -> None:
    s = _make_state()
    s.peak_equity = Decimal("30000")
    s.realized_pnl_today = Decimal("-100")
    s.intraday_low_equity = Decimal("24000")
    s.last_loss_ts_by_symbol["AAPL"] = datetime(2026, 4, 23, tzinfo=UTC)
    s.last_reconciled_equity = Decimal("25500")

    s.roll_to_new_day(date(2026, 4, 24))
    assert s.trading_day == date(2026, 4, 24)
    assert s.peak_equity == Decimal("30000")        # preserved
    assert s.realized_pnl_today == Decimal(0)
    assert s.intraday_low_equity == s.last_reconciled_equity
    assert s.last_loss_ts_by_symbol == {}
    # open_trades and halts are preserved — policy-free
    assert "AAPL" in s.open_trades


def test_roll_to_new_day_rejects_non_forward_move() -> None:
    s = _make_state()
    with pytest.raises(ValueError):
        s.roll_to_new_day(date(2026, 4, 22))


# ---------------------------------------------------------------------------
# Tempfile-name pattern is same-parent (ensures same-fs atomic rename)
# ---------------------------------------------------------------------------


def test_tempfile_sits_in_same_directory(tmp_path: Path, monkeypatch) -> None:
    """Capture the tempfile path to prove the rename is same-fs atomic."""
    store = StateStore(tmp_path / "state.json")
    import strategy.state as state_mod

    captured: list[str] = []
    real_mkstemp = state_mod.tempfile.mkstemp

    def spy(*a, **kw):
        fd, p = real_mkstemp(*a, **kw)
        captured.append(p)
        return fd, p

    monkeypatch.setattr(state_mod.tempfile, "mkstemp", spy)
    store.save(_make_state())
    assert captured
    for p in captured:
        assert Path(p).parent == tmp_path
