"""Tests for strategy.reconcile."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from strategy.dto import (
    COID_PREFIX,
    BrokerOrder,
    OpenTrade,
    OrderClass,
    OrderSide,
    OrderStatus,
    Position,
)
from strategy.reconcile import reconcile, reconcile_stale_local_orders
from strategy.state import StrategyState


UTC = timezone.utc


def _pos(sym: str, qty: int = 10, side: OrderSide = OrderSide.BUY) -> Position:
    return Position(
        symbol=sym,
        qty=qty,
        avg_entry_price=Decimal("200.00"),
        market_value=Decimal("2000.00"),
        unrealized_pl=Decimal("0"),
        side=side,
    )


def _open_trade(sym: str, qty: int = 10, child_coid: str | None = None) -> OpenTrade:
    return OpenTrade(
        symbol=sym,
        qty=qty,
        entry_price=Decimal("200.00"),
        entry_ts=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
        stop_price=Decimal("198.00"),
        disaster_stop_price=Decimal("196.00"),
        target_price=Decimal("204.00"),
        intent_id="i-001",
        parent_client_order_id=f"{COID_PREFIX}parent-{sym}",
        protective_child_client_order_id=child_coid,
        protective_child_broker_id=None,
        last_seen_broker_qty=qty,
    )


def _protective_child(
    sym: str,
    qty: int = 10,
    coid: str | None = None,
    broker_id: str = "child-1",
    status: OrderStatus = OrderStatus.NEW,
    parent_coid: str = f"{COID_PREFIX}parent-AAPL",
) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=broker_id,
        client_order_id=coid if coid is not None else f"{COID_PREFIX}child-{sym}",
        symbol=sym,
        side=OrderSide.SELL,
        qty=qty,
        filled_qty=0,
        avg_fill_price=None,
        status=status,
        order_class=OrderClass.OTO,
        submitted_at=datetime(2026, 4, 23, 14, 1, tzinfo=UTC),
        filled_at=None,
        parent_client_order_id=parent_coid,
        leg_role="stop_child",
    )


# ---------------------------------------------------------------------------
# Clean scenario
# ---------------------------------------------------------------------------


def test_clean_reconcile() -> None:
    state = StrategyState()
    child_coid = f"{COID_PREFIX}child-AAPL"
    state.open_trades["AAPL"] = _open_trade("AAPL", qty=10, child_coid=child_coid)
    report = reconcile(
        state,
        broker_positions=[_pos("AAPL", qty=10)],
        broker_open_orders=[_protective_child("AAPL", qty=10, coid=child_coid)],
    )
    assert report.is_clean() is True
    assert report.requires_entry_block() is False
    assert report.requires_halt() is False


# ---------------------------------------------------------------------------
# Missing / extra / qty mismatch
# ---------------------------------------------------------------------------


def test_missing_position() -> None:
    state = StrategyState()
    state.open_trades["AAPL"] = _open_trade("AAPL")
    report = reconcile(state, broker_positions=[], broker_open_orders=[])
    assert report.missing_positions == ["AAPL"]
    assert report.is_clean() is False
    assert report.requires_entry_block() is True


def test_extra_position() -> None:
    state = StrategyState()
    report = reconcile(
        state,
        broker_positions=[_pos("MSFT", qty=5)],
        broker_open_orders=[],
    )
    assert report.extra_positions == ["MSFT"]
    assert report.is_clean() is False
    assert report.requires_entry_block() is True


def test_qty_mismatch() -> None:
    state = StrategyState()
    state.open_trades["AAPL"] = _open_trade("AAPL", qty=10)
    report = reconcile(
        state,
        broker_positions=[_pos("AAPL", qty=7)],
        broker_open_orders=[],
    )
    assert report.qty_mismatches == [("AAPL", 10, 7)]
    assert report.is_clean() is False


# ---------------------------------------------------------------------------
# Protective-leg mismatches
# ---------------------------------------------------------------------------


def test_orphan_protective_order_no_local_position() -> None:
    state = StrategyState()
    report = reconcile(
        state,
        broker_positions=[],
        broker_open_orders=[_protective_child("AAPL", qty=10, broker_id="orph-1")],
    )
    assert report.orphan_protective_orders == ["orph-1"]
    assert report.requires_entry_block() is True


def test_unexpected_multiple_protective_children() -> None:
    state = StrategyState()
    state.open_trades["AAPL"] = _open_trade(
        "AAPL", qty=10, child_coid=f"{COID_PREFIX}child-AAPL"
    )
    report = reconcile(
        state,
        broker_positions=[_pos("AAPL", qty=10)],
        broker_open_orders=[
            _protective_child("AAPL", qty=10, coid=f"{COID_PREFIX}child-AAPL", broker_id="c1"),
            _protective_child("AAPL", qty=10, coid=f"{COID_PREFIX}extra-AAPL", broker_id="c2"),
        ],
    )
    assert "c2" in report.unexpected_protective_children


def test_partial_fill_with_orphan_child_detected() -> None:
    state = StrategyState()
    child_coid = f"{COID_PREFIX}child-AAPL"
    state.open_trades["AAPL"] = _open_trade("AAPL", qty=10, child_coid=child_coid)
    report = reconcile(
        state,
        broker_positions=[_pos("AAPL", qty=4)],
        broker_open_orders=[_protective_child("AAPL", qty=10, coid=child_coid)],
    )
    # Position shows 4, local expects 10 → qty mismatch AND child mismatched.
    assert ("AAPL", 10, 4) in report.qty_mismatches
    assert "AAPL" in report.partial_fill_with_orphan_child


def test_unknown_coid_surfaced_not_blocking() -> None:
    """Broker has a resting order from some other system. Surface it as
    warning, but it alone should not block new entries."""
    state = StrategyState()
    foreign = BrokerOrder(
        broker_order_id="foreign-1",
        client_order_id="FOREIGN-xyz",
        symbol="NVDA",
        side=OrderSide.SELL,
        qty=3,
        filled_qty=0,
        avg_fill_price=None,
        status=OrderStatus.NEW,
        order_class=OrderClass.SIMPLE,
        submitted_at=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
        filled_at=None,
        parent_client_order_id=None,
        leg_role=None,
    )
    report = reconcile(state, broker_positions=[], broker_open_orders=[foreign])
    assert report.broker_order_with_unknown_coid == ["foreign-1"]
    assert report.is_clean() is False          # surfaced
    assert report.requires_entry_block() is False  # not alone a block


def test_orphan_child_wrong_coid_flagged() -> None:
    state = StrategyState()
    # Local records the "real" child COID...
    state.open_trades["AAPL"] = _open_trade(
        "AAPL", qty=10, child_coid=f"{COID_PREFIX}child-expected"
    )
    # ...but broker shows a different child COID for the same symbol.
    wrong = _protective_child(
        "AAPL", qty=10, coid=f"{COID_PREFIX}child-wrong", broker_id="wrong-1"
    )
    report = reconcile(
        state,
        broker_positions=[_pos("AAPL", qty=10)],
        broker_open_orders=[wrong],
    )
    assert "wrong-1" in report.orphan_protective_orders


# ---------------------------------------------------------------------------
# Side mismatch triggers halt
# ---------------------------------------------------------------------------


def test_short_position_triggers_halt() -> None:
    state = StrategyState()
    report = reconcile(
        state,
        broker_positions=[_pos("AAPL", qty=10, side=OrderSide.SELL)],
        broker_open_orders=[],
    )
    assert "AAPL" in report.position_side_mismatch
    assert report.requires_halt() is True
    assert report.requires_entry_block() is True


# ---------------------------------------------------------------------------
# Stale local orders helper
# ---------------------------------------------------------------------------


def test_stale_local_orders_detected() -> None:
    state = StrategyState()
    stale = reconcile_stale_local_orders(
        state,
        broker_open_orders=[_protective_child("AAPL", coid=f"{COID_PREFIX}live")],
        local_pending_parent_coids=[f"{COID_PREFIX}live", f"{COID_PREFIX}stale"],
    )
    assert stale == [f"{COID_PREFIX}stale"]


def test_stale_empty_when_all_present() -> None:
    state = StrategyState()
    stale = reconcile_stale_local_orders(
        state,
        broker_open_orders=[_protective_child("AAPL", coid=f"{COID_PREFIX}x")],
        local_pending_parent_coids=[f"{COID_PREFIX}x"],
    )
    assert stale == []
