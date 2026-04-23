"""Tests for strategy.dto.

Key invariants pinned here:
* COID is deterministic across runs and independent of source dict order.
* COID length is within Alpaca's 48-char limit.
* OrderIntent rejects non-UTC timestamps, shorts, non-DAY TIF, non-OTO class,
  negative qty/prices, and stops that aren't strictly below the entry.
* ReconcileReport.is_clean and .requires_halt do what they say.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from strategy.dto import (
    COID_HASH_LEN,
    COID_PREFIX,
    Bar,
    BrokerOrder,
    OrderClass,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
    Quote,
    ReconcileReport,
    RiskDecision,
    Signal,
    TERMINAL_STATUSES,
    TimeInForce,
    Trade,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_intent(**overrides) -> OrderIntent:
    defaults = dict(
        intent_id="intent-001",
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=10,
        limit_price=Decimal("200.10"),
        disaster_stop_price=Decimal("196.00"),
        tif=TimeInForce.DAY,
        order_class=OrderClass.OTO,
        reason="ema-breakout",
        ref_price=Decimal("200.00"),
        atr=Decimal("1.50"),
        spread_bps=Decimal("4"),
        ts=datetime(2026, 4, 23, 14, 30, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return OrderIntent(**defaults)


# ---------------------------------------------------------------------------
# COID determinism & shape
# ---------------------------------------------------------------------------


def test_coid_is_deterministic() -> None:
    a = make_intent()
    b = make_intent()
    assert a.client_order_id() == b.client_order_id()


def test_coid_prefix_and_length() -> None:
    coid = make_intent().client_order_id()
    assert coid.startswith(COID_PREFIX)
    assert len(coid) == len(COID_PREFIX) + COID_HASH_LEN
    assert len(coid) <= 48  # Alpaca's hard limit


def test_coid_changes_when_identity_field_changes() -> None:
    base = make_intent().client_order_id()
    assert make_intent(symbol="MSFT").client_order_id() != base
    assert make_intent(qty=11).client_order_id() != base
    assert make_intent(limit_price=Decimal("200.11")).client_order_id() != base
    assert make_intent(disaster_stop_price=Decimal("195.99")).client_order_id() != base
    assert (
        make_intent(ts=datetime(2026, 4, 23, 14, 31, 0, tzinfo=timezone.utc))
        .client_order_id()
        != base
    )
    assert make_intent(intent_id="intent-002").client_order_id() != base


def test_coid_unchanged_by_non_identity_field() -> None:
    """Changing `reason` should not change the COID — it's audit metadata
    that does not affect order identity."""
    base = make_intent().client_order_id()
    assert make_intent(reason="different-reason").client_order_id() == base
    assert make_intent(atr=Decimal("9.99")).client_order_id() == base
    assert make_intent(spread_bps=Decimal("17")).client_order_id() == base


def test_canonical_payload_is_sorted_json_stable() -> None:
    import json
    payload = make_intent().canonical_payload()
    # Serialise twice with different dict literal orders; both must produce
    # identical canonical strings.
    s1 = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    reordered = {k: payload[k] for k in sorted(payload.keys(), reverse=True)}
    s2 = json.dumps(reordered, sort_keys=True, separators=(",", ":"))
    assert s1 == s2


# ---------------------------------------------------------------------------
# OrderIntent validation
# ---------------------------------------------------------------------------


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make_intent(ts=datetime(2026, 4, 23, 14, 30, 0))


def test_short_side_rejected() -> None:
    with pytest.raises(ValueError, match="long-only"):
        make_intent(side=OrderSide.SELL)


def test_non_day_tif_rejected() -> None:
    class FakeTIF:
        value = "gtc"
    with pytest.raises(ValueError, match="DAY"):
        make_intent(tif=FakeTIF())  # type: ignore[arg-type]


def test_simple_order_class_rejected() -> None:
    with pytest.raises(ValueError, match="OTO"):
        make_intent(order_class=OrderClass.SIMPLE)


def test_non_positive_qty_rejected() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        make_intent(qty=0)
    with pytest.raises(ValueError, match="positive integer"):
        make_intent(qty=-3)


def test_non_positive_prices_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        make_intent(limit_price=Decimal(0))
    with pytest.raises(ValueError, match="positive"):
        make_intent(disaster_stop_price=Decimal(0))


def test_disaster_stop_must_be_below_limit_for_long() -> None:
    with pytest.raises(ValueError, match="strictly less than"):
        make_intent(limit_price=Decimal("100"), disaster_stop_price=Decimal("100"))
    with pytest.raises(ValueError, match="strictly less than"):
        make_intent(limit_price=Decimal("100"), disaster_stop_price=Decimal("101"))


def test_intent_is_frozen() -> None:
    intent = make_intent()
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError subclass of AttributeError
        intent.qty = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Quote helpers
# ---------------------------------------------------------------------------


def test_quote_mid_and_spread() -> None:
    q = Quote(
        symbol="AAPL",
        ts=datetime(2026, 4, 23, tzinfo=timezone.utc),
        bid_price=Decimal("100.00"),
        ask_price=Decimal("100.10"),
        bid_size=100,
        ask_size=200,
    )
    assert q.mid() == Decimal("100.05")
    # spread = 0.10, mid = 100.05  →  bps ≈ 9.995
    assert Decimal("9") < q.spread_bps() < Decimal("11")


def test_quote_bad_mid_returns_huge_spread_bps() -> None:
    """Fail-closed: a quote with non-positive mid is treated as a very
    wide spread so the spread filter denies entry."""
    q = Quote(
        symbol="AAPL",
        ts=datetime(2026, 4, 23, tzinfo=timezone.utc),
        bid_price=Decimal(0),
        ask_price=Decimal(0),
        bid_size=0,
        ask_size=0,
    )
    assert q.spread_bps() > Decimal(10_000)


# ---------------------------------------------------------------------------
# BrokerOrder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (OrderStatus.FILLED, True),
        (OrderStatus.CANCELED, True),
        (OrderStatus.EXPIRED, True),
        (OrderStatus.REJECTED, True),
        (OrderStatus.NEW, False),
        (OrderStatus.PENDING_NEW, False),
        (OrderStatus.ACCEPTED, False),
        (OrderStatus.PARTIALLY_FILLED, False),
        (OrderStatus.HELD, False),
        (OrderStatus.UNKNOWN, False),
    ],
)
def test_broker_order_is_terminal(status: OrderStatus, expected: bool) -> None:
    order = BrokerOrder(
        broker_order_id="b1",
        client_order_id="TBv1-abc",
        symbol="AAPL",
        side=OrderSide.BUY,
        qty=10,
        filled_qty=0,
        avg_fill_price=None,
        status=status,
        order_class=OrderClass.OTO,
        submitted_at=datetime(2026, 4, 23, tzinfo=timezone.utc),
        filled_at=None,
        parent_client_order_id=None,
        leg_role="parent",
    )
    assert order.is_terminal() is expected
    assert (status in TERMINAL_STATUSES) is expected


# ---------------------------------------------------------------------------
# ReconcileReport
# ---------------------------------------------------------------------------


def test_empty_report_is_clean() -> None:
    r = ReconcileReport()
    assert r.is_clean() is True
    assert r.requires_halt() is False
    assert r.requires_entry_block() is False


def test_any_mismatch_breaks_clean() -> None:
    cats = [
        ("missing_positions", ["AAPL"]),
        ("extra_positions", ["MSFT"]),
        ("qty_mismatches", [("AAPL", 10, 5)]),
        ("orphan_protective_orders", ["ord-1"]),
        ("unexpected_protective_children", ["ord-2"]),
        ("partial_fill_with_orphan_child", ["AAPL"]),
        ("position_side_mismatch", ["AAPL"]),
        ("broker_order_with_unknown_coid", ["ord-3"]),
        ("stale_local_orders", ["ord-4"]),
    ]
    for name, value in cats:
        r = ReconcileReport()
        setattr(r, name, value)
        assert r.is_clean() is False, f"{name} should break clean"


def test_unknown_coid_does_not_block_entries_alone() -> None:
    r = ReconcileReport(broker_order_with_unknown_coid=["ord-x"])
    assert r.is_clean() is False          # surfaced
    assert r.requires_entry_block() is False  # but does not gate entry


def test_side_mismatch_requires_halt() -> None:
    r = ReconcileReport(position_side_mismatch=["AAPL"])
    assert r.requires_halt() is True
    assert r.requires_entry_block() is True


# ---------------------------------------------------------------------------
# RiskDecision / Signal / others
# ---------------------------------------------------------------------------


def test_risk_decision_defaults() -> None:
    d = RiskDecision(allowed=False, reason="stale_data")
    assert d.qty == 0
    assert d.notional == Decimal(0)
    assert d.limit_price is None


def test_signal_and_position_dto_roundtrip() -> None:
    s = Signal(
        symbol="AAPL",
        ts=datetime(2026, 4, 23, tzinfo=timezone.utc),
        direction=OrderSide.BUY,
        reason="breakout",
        ref_price=Decimal("100.00"),
        atr=Decimal("1.0"),
        expected_move_bps=Decimal("50"),
    )
    assert s.direction is OrderSide.BUY

    p = Position(
        symbol="AAPL",
        qty=10,
        avg_entry_price=Decimal("100.00"),
        market_value=Decimal("1005.00"),
        unrealized_pl=Decimal("5.00"),
        side=OrderSide.BUY,
    )
    assert p.qty == 10


def test_bar_and_trade_dtos_accept_decimal() -> None:
    b = Bar(
        symbol="AAPL",
        ts=datetime(2026, 4, 23, tzinfo=timezone.utc),
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=1_000_000,
    )
    assert isinstance(b.close, Decimal)
    t = Trade(
        symbol="AAPL",
        ts=datetime(2026, 4, 23, tzinfo=timezone.utc),
        price=Decimal("100.5"),
        size=100,
    )
    assert isinstance(t.price, Decimal)
