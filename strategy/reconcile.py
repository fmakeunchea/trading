"""Pure reconciliation diff.

Design notes:

* This module has **no** Alpaca SDK imports. It operates on DTOs only so
  the diff logic is deterministic and easy to test.
* The output is a :class:`ReconcileReport` with categorised mismatches.
* An empty report is the *only* state that clears the entry gate.
  ``report.requires_entry_block()`` is the gate the orchestrator reads.
* The caller provides the COID prefix for "our" orders. Anything without
  the prefix is surfaced as ``broker_order_with_unknown_coid`` — that
  does *not* by itself block entries, but it warns loudly because the
  account has resting orders from some other system.
"""
from __future__ import annotations

from typing import Iterable

from strategy.dto import (
    COID_PREFIX,
    BrokerOrder,
    OrderClass,
    OrderSide,
    OrderStatus,
    Position,
    ReconcileReport,
)
from strategy.state import StrategyState


# Orders whose presence suggests a dangling protective leg.
PROTECTIVE_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.NEW,
        OrderStatus.ACCEPTED,
        OrderStatus.PENDING_NEW,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.HELD,
    }
)


def reconcile(
    state: StrategyState,
    broker_positions: Iterable[Position],
    broker_open_orders: Iterable[BrokerOrder],
    *,
    coid_prefix: str = COID_PREFIX,
) -> ReconcileReport:
    """Diff local :class:`StrategyState` against broker truth.

    Broker truth is authoritative; this function does not mutate state.
    The caller (orchestrator) decides what to do with the report.
    """
    report = ReconcileReport()
    positions = list(broker_positions)
    open_orders = list(broker_open_orders)

    broker_by_symbol: dict[str, Position] = {p.symbol: p for p in positions}
    local_by_symbol: dict[str, int] = {
        sym: t.qty for sym, t in state.open_trades.items()
    }

    # --- position-level diffs ---------------------------------------------
    for sym, broker_pos in broker_by_symbol.items():
        if broker_pos.side is OrderSide.SELL:
            # Long-only bot — a short is a halt-level error.
            report.position_side_mismatch.append(sym)
        if sym not in local_by_symbol:
            report.extra_positions.append(sym)
        else:
            local_qty = local_by_symbol[sym]
            if broker_pos.qty != local_qty:
                report.qty_mismatches.append((sym, local_qty, broker_pos.qty))

    for sym, _qty in local_by_symbol.items():
        if sym not in broker_by_symbol:
            report.missing_positions.append(sym)

    # --- protective-order diffs -------------------------------------------
    # Map: COID of protective children we *know* about, keyed by symbol.
    expected_protective: dict[str, str | None] = {
        sym: t.protective_child_client_order_id
        for sym, t in state.open_trades.items()
    }
    # Count protective-looking orders (OTO children) per symbol.
    protective_by_symbol: dict[str, list[BrokerOrder]] = {}
    for order in open_orders:
        if order.status not in PROTECTIVE_STATUSES:
            continue
        # Ownership rule (paper-smoke verified 2026-04-23):
        # Alpaca auto-generates UUID client_order_ids for OTO children,
        # so we cannot identify ownership from the child's own COID.
        # The **owning COID** is the parent's ``client_order_id`` for
        # legs and the order's own ``client_order_id`` for top-level
        # orders. Ours iff the owning COID starts with our prefix.
        owning_coid = order.parent_client_order_id or order.client_order_id
        if not owning_coid or not owning_coid.startswith(coid_prefix):
            report.broker_order_with_unknown_coid.append(order.broker_order_id)
            continue
        if order.leg_role == "stop_child" or order.parent_client_order_id is not None:
            protective_by_symbol.setdefault(order.symbol, []).append(order)

    for sym, orders in protective_by_symbol.items():
        # Protective stop exists; is there a matching local position?
        if sym not in state.open_trades:
            # A resting protective stop with no local position is the
            # single most dangerous leak: if it fires, it creates an
            # unintended short.
            for o in orders:
                report.orphan_protective_orders.append(o.broker_order_id)
            continue
        # We expect at most one protective child per open trade.
        if len(orders) > 1:
            for o in orders[1:]:
                report.unexpected_protective_children.append(o.broker_order_id)
        # Canonical child must size to the *actual broker position*, not
        # the local-expected qty. A child whose qty != position qty is a
        # classic partial-fill orphan (the parent filled less than
        # requested and the child did not resize).
        child = orders[0]
        broker_pos = broker_by_symbol.get(sym)
        if broker_pos is not None and child.qty != broker_pos.qty:
            report.partial_fill_with_orphan_child.append(sym)
        # Confirm the child's COID matches what we recorded.
        if expected_protective.get(sym) and expected_protective[sym] != child.client_order_id:
            # Child exists but isn't the one we recorded — count as orphan.
            report.orphan_protective_orders.append(child.broker_order_id)

    return report


def reconcile_stale_local_orders(
    state: StrategyState,
    broker_open_orders: Iterable[BrokerOrder],
    *,
    local_pending_parent_coids: Iterable[str] = (),
) -> list[str]:
    """Return the subset of ``local_pending_parent_coids`` that are not
    present in the broker's open-orders list.

    Separated from :func:`reconcile` because "pending" is a transient
    state the orchestrator owns; state.py does not persist it.
    """
    live = {o.client_order_id for o in broker_open_orders}
    return [coid for coid in local_pending_parent_coids if coid not in live]
