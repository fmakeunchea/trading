"""Tests for scripts.paper_cleanup.

Non-network only. The live cleanup path (actual Alpaca calls) is
validated by running the script against a paper endpoint, not by pytest.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import scripts.paper_cleanup as pc
from strategy.dto import (
    BrokerOrder,
    CloseResult,
    OrderClass,
    OrderSide,
    OrderStatus,
    Position,
)


UTC = timezone.utc


def _broker_with_positions(pos_list, orders_list=()) -> MagicMock:
    b = MagicMock()
    b.get_positions.return_value = list(pos_list)
    b.get_open_orders.return_value = list(orders_list)
    return b


def _pos(symbol: str = "SPY", qty: int = 1) -> Position:
    return Position(
        symbol=symbol, qty=qty,
        avg_entry_price=Decimal("500"),
        market_value=Decimal("500"),
        unrealized_pl=Decimal("0"),
        side=OrderSide.BUY,
    )


def _order(broker_order_id: str, symbol: str = "SPY") -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=broker_order_id,
        client_order_id="TBv1-whatever",
        symbol=symbol,
        side=OrderSide.SELL,
        qty=1,
        filled_qty=0,
        avg_fill_price=None,
        status=OrderStatus.NEW,
        order_class=OrderClass.OTO,
        submitted_at=datetime(2026, 4, 24, tzinfo=UTC),
        filled_at=None,
        parent_client_order_id="TBv1-parent",
        leg_role="stop_child",
    )


def _close_result(symbol: str = "SPY") -> CloseResult:
    return CloseResult(
        symbol=symbol,
        cancelled_order_ids=(),
        close_order=BrokerOrder(
            broker_order_id="close-1",
            client_order_id="TBv1-close",
            symbol=symbol,
            side=OrderSide.SELL,
            qty=0,
            filled_qty=0,
            avg_fill_price=Decimal("500"),
            status=OrderStatus.FILLED,
            order_class=OrderClass.SIMPLE,
            submitted_at=datetime(2026, 4, 24, tzinfo=UTC),
            filled_at=datetime(2026, 4, 24, tzinfo=UTC),
            parent_client_order_id=None,
            leg_role=None,
        ),
        final_position_qty=0,
    )


# ---------------------------------------------------------------------------
# CLI refuses without --yes
# ---------------------------------------------------------------------------


def test_cli_refuses_without_yes(capsys, monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    rc = pc.main(["--env-file", "/does/not/exist.env"])
    assert rc == 2
    assert "--yes" in capsys.readouterr().err


def test_cli_refuses_when_credentials_missing(monkeypatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    monkeypatch.delenv("ALPACA_API_KEY_PAPER", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_PAPER", raising=False)
    rc = pc.main(["--yes", "--env-file", "/does/not/exist.env"])
    assert rc == 2


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------


def test_snapshot_shapes_output() -> None:
    b = _broker_with_positions([_pos("SPY")], [_order("o-1")])
    snap = pc.snapshot(b)
    assert snap["positions"] == [{"symbol": "SPY", "qty": 1, "side": "buy"}]
    assert snap["open_orders"][0]["broker_order_id"] == "o-1"
    assert snap["open_orders"][0]["side"] == "sell"


def test_snapshot_returns_error_on_broker_failure() -> None:
    from strategy.errors import TransientBrokerError
    b = MagicMock()
    b.get_positions.side_effect = TransientBrokerError("rate limited")
    out = pc.snapshot(b)
    assert "error" in out


# ---------------------------------------------------------------------------
# cancel_all_orders
# ---------------------------------------------------------------------------


def test_cancel_all_orders_cancels_each() -> None:
    b = _broker_with_positions([], [_order("o-1"), _order("o-2")])
    cancelled = pc.cancel_all_orders(b)
    assert cancelled == ["o-1", "o-2"]
    assert b.cancel_order.call_count == 2


def test_cancel_all_orders_tolerates_individual_failures() -> None:
    from strategy.errors import PermanentBrokerError
    b = _broker_with_positions([], [_order("o-1"), _order("o-2")])
    b.cancel_order.side_effect = [None, PermanentBrokerError("bad id")]
    cancelled = pc.cancel_all_orders(b)
    # Only the first succeeded; the second was attempted but raised.
    assert cancelled == ["o-1"]


# ---------------------------------------------------------------------------
# flatten_all_positions
# ---------------------------------------------------------------------------


def test_flatten_all_positions_flattens_each() -> None:
    b = _broker_with_positions([_pos("SPY"), _pos("AAPL")])
    b.flatten_symbol.side_effect = [_close_result("SPY"), _close_result("AAPL")]
    out = pc.flatten_all_positions(b)
    symbols = [s for s, _ in out]
    assert symbols == ["SPY", "AAPL"]
    assert b.flatten_symbol.call_count == 2


def test_flatten_all_positions_tolerates_individual_failure() -> None:
    from strategy.errors import PermanentBrokerError
    b = _broker_with_positions([_pos("SPY"), _pos("AAPL")])
    b.flatten_symbol.side_effect = [
        _close_result("SPY"),
        PermanentBrokerError("flatten blew up"),
    ]
    out = pc.flatten_all_positions(b)
    # Both symbols reported; second carries an error string.
    assert out[0][0] == "SPY"
    assert "error" in out[1][1]


# ---------------------------------------------------------------------------
# run_cleanup end-to-end
# ---------------------------------------------------------------------------


def test_run_cleanup_reports_clean_when_account_is_flat(capsys) -> None:
    b = MagicMock()
    b.get_positions.return_value = []
    b.get_open_orders.return_value = []
    rc = pc.run_cleanup(b, settle_s=0.0, poll_interval_s=0.0, poll_deadline_s=0.5)
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK: account is flat" in out


def test_run_cleanup_runs_full_sequence_when_dirty(capsys) -> None:
    b = MagicMock()
    # BEFORE: 1 position, 1 open order. cancel_all_orders then sees []
    # (we simulate Alpaca having cleared it). flatten_all sees position.
    # AFTER polling: empty.
    b.get_positions.side_effect = [
        [_pos("SPY")],   # BEFORE snapshot
        [_pos("SPY")],   # flatten_all lists positions
        [],              # AFTER poll #1 - flat
    ]
    b.get_open_orders.side_effect = [
        [_order("o-1")], # BEFORE snapshot
        [_order("o-1")], # cancel_all lists orders
        [],              # AFTER poll #1 - no orders
    ]
    b.flatten_symbol.return_value = _close_result("SPY")
    rc = pc.run_cleanup(b, settle_s=0.0, poll_interval_s=0.0, poll_deadline_s=0.5)
    assert rc == 0
    out = capsys.readouterr().out
    assert "BEFORE:" in out
    assert "AFTER:" in out
    assert "OK: account is flat" in out


def test_run_cleanup_returns_nonzero_when_account_stays_dirty(capsys) -> None:
    b = MagicMock()
    # Account never gets clean — polls keep showing residuals.
    b.get_positions.return_value = [_pos("SPY")]
    b.get_open_orders.return_value = [_order("o-1")]
    b.flatten_symbol.return_value = _close_result("SPY")
    rc = pc.run_cleanup(b, settle_s=0.0, poll_interval_s=0.0, poll_deadline_s=0.1)
    assert rc == 1
    out = capsys.readouterr().out
    assert "FAIL" in out
