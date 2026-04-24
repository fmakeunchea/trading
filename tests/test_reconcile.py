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


def test_reconcile_recognises_detached_stop_child_by_local_state_match() -> None:
    """Regression (2026-04-24 RTH smoke): once the OTO parent fills on
    Alpaca, the active stop child is returned by
    ``get_open_orders(status=OPEN, nested=True)`` as a **standalone
    top-level order** with no parent linkage on its own record. Our
    DTO mapping sees ``leg_role=stop_child`` (via _derive_leg_role
    detecting order_class=OTO + type=stop) but
    ``parent_client_order_id=None`` and ``parent_broker_order_id=None``.
    Reconcile must still recognise the child as ours via a lookup
    against our locally stored ``protective_child_client_order_id``.
    Before this change the smoke ran
        unknown_coid=['<child broker id>']
    which would block entries every tick with an open position.
    """
    alpaca_child_coid = "f7fa3282-8611-4e6f-90ee-d28f6ef9a4cb"
    state = StrategyState()
    state.open_trades["SPY"] = _open_trade(
        "SPY", qty=1, child_coid=alpaca_child_coid,
    )
    detached_child = BrokerOrder(
        broker_order_id="b-child",
        client_order_id=alpaca_child_coid,   # Alpaca-generated UUID, not TBv1-
        symbol="SPY",
        side=OrderSide.SELL,
        qty=1,
        filled_qty=0,
        avg_fill_price=None,
        status=OrderStatus.NEW,
        order_class=OrderClass.OTO,
        submitted_at=datetime(2026, 4, 24, tzinfo=UTC),
        filled_at=None,
        parent_client_order_id=None,         # NO parent linkage after fill
        leg_role="stop_child",                # set by _derive_leg_role
    )
    report = reconcile(
        state,
        broker_positions=[_pos("SPY", qty=1)],
        broker_open_orders=[detached_child],
    )
    assert report.is_clean() is True, (
        f"detached child must be recognised as ours; got {report}"
    )
    assert "b-child" not in report.broker_order_with_unknown_coid


def test_reconcile_detached_child_without_local_record_is_still_flagged() -> None:
    """Safety: a detached OTO stop child whose COID we did NOT record
    in local state is still treated as foreign — the local-state match
    clause must not accidentally claim everything that looks like an
    OTO leg."""
    alien_child_coid = "somebody-elses-uuid"
    state = StrategyState()
    # No open_trades, nothing to match against.
    detached = BrokerOrder(
        broker_order_id="alien-child",
        client_order_id=alien_child_coid,
        symbol="SPY",
        side=OrderSide.SELL,
        qty=1,
        filled_qty=0,
        avg_fill_price=None,
        status=OrderStatus.NEW,
        order_class=OrderClass.OTO,
        submitted_at=datetime(2026, 4, 24, tzinfo=UTC),
        filled_at=None,
        parent_client_order_id=None,
        leg_role="stop_child",
    )
    report = reconcile(
        state,
        broker_positions=[],
        broker_open_orders=[detached],
    )
    assert "alien-child" in report.broker_order_with_unknown_coid


def test_real_alpaca_child_uuid_coid_is_still_ours() -> None:
    """Regression: the 2026-04-23 paper smoke confirmed that Alpaca
    auto-generates a UUID ``client_order_id`` on OTO children. The
    ownership rule must therefore key on the *parent's* COID (carried
    on the leg via traversal), not the child's own COID."""
    state = StrategyState()
    # Local state points at the real Alpaca-generated child COID.
    alpaca_child_coid = "ebbe5352-cf19-4a57-8eaf-f9773c910b91"
    state.open_trades["AAPL"] = _open_trade(
        "AAPL", qty=10, child_coid=alpaca_child_coid,
    )
    # Broker returns the same UUID on the child, with our TBv1- parent
    # COID on `parent_client_order_id` (populated by the wrapper during
    # traversal, not by the SDK).
    child = _protective_child(
        "AAPL",
        qty=10,
        coid=alpaca_child_coid,
        broker_id="b-child",
        parent_coid=f"{COID_PREFIX}parent-AAPL",
    )
    report = reconcile(
        state,
        broker_positions=[_pos("AAPL", qty=10)],
        broker_open_orders=[child],
    )
    assert report.is_clean() is True
    # Crucially, the child is NOT flagged as unknown_coid even though
    # its own COID is a non-TBv1 UUID.
    assert child.broker_order_id not in report.broker_order_with_unknown_coid


def test_foreign_leg_with_non_prefixed_parent_is_unknown_coid() -> None:
    """A resting OTO stop from some other system: its parent COID does
    not start with ``TBv1-``, so the leg is flagged as unknown_coid and
    NOT added to protective_by_symbol (which would otherwise misfire
    orphan detection)."""
    state = StrategyState()
    foreign_child = _protective_child(
        "NVDA",
        qty=3,
        coid="alien-abc",                  # foreign child's own COID
        broker_id="alien-child",
        parent_coid="OTHER-alien-parent",  # foreign parent COID
    )
    report = reconcile(
        state,
        broker_positions=[],
        broker_open_orders=[foreign_child],
    )
    assert "alien-child" in report.broker_order_with_unknown_coid
    # Critically: NOT in orphan_protective_orders, because we refused
    # to treat it as "ours" in the first place.
    assert report.orphan_protective_orders == []


def test_top_level_order_ownership_keyed_on_own_coid() -> None:
    """For top-level orders (no parent), ownership is still decided by
    the order's own ``client_order_id``. This test pins down the
    fallback branch of the owning-COID rule."""
    state = StrategyState()
    ours = BrokerOrder(
        broker_order_id="ours-1",
        client_order_id=f"{COID_PREFIX}top-level",
        symbol="NVDA",
        side=OrderSide.BUY,
        qty=3,
        filled_qty=0,
        avg_fill_price=None,
        status=OrderStatus.NEW,
        order_class=OrderClass.SIMPLE,
        submitted_at=datetime(2026, 4, 23, 14, 0, tzinfo=UTC),
        filled_at=None,
        parent_client_order_id=None,    # top-level
        leg_role=None,
    )
    foreign = BrokerOrder(
        broker_order_id="foreign-1",
        client_order_id="SOMEBODY-ELSES",
        symbol="NVDA",
        side=OrderSide.BUY,
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
    report = reconcile(
        state,
        broker_positions=[],
        broker_open_orders=[ours, foreign],
    )
    # The foreign top-level order is flagged; ours is not.
    assert report.broker_order_with_unknown_coid == ["foreign-1"]


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
