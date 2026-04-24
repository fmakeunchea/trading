"""Safe pre-smoke cleanup of the paper account.

Cancels every open order and flattens every position via a
market-close, then polls until the account is verifiably flat. Run
this before a paper smoke test to ensure no residual state from a
prior aborted run interferes with the new run.

Guardrails:

* Refuses to run without ``--yes``.
* Refuses to run against a non-paper base URL.
* Prints account state **before** and **after** so the operator can
  sanity-check what happened.
* Uses the same :class:`AlpacaBroker` wrapper that production code
  uses — the cleanup exercises the same code path as real operation.

Usage::

    python -m scripts.paper_cleanup --yes

Exit codes:

* ``0`` — account is flat with no open orders.
* ``1`` — cleanup performed but account is still not flat (operator
  intervention required).
* ``2`` — precondition error (missing creds, non-paper URL, no --yes).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from pathlib import Path

from scripts.paper_smoke import DEFAULT_ENV_FILE, build_broker, load_env
from strategy.broker import AlpacaBroker
from strategy.dto import COID_PREFIX
from strategy.errors import StrategyError


log = logging.getLogger("paper_cleanup")


def snapshot(broker: AlpacaBroker) -> dict:
    """Return a dict of positions + open orders for reporting."""
    try:
        positions = broker.get_positions()
        orders = broker.get_open_orders()
    except StrategyError as exc:
        return {"error": str(exc)}
    return {
        "positions": [
            {"symbol": p.symbol, "qty": p.qty, "side": p.side.value}
            for p in positions
        ],
        "open_orders": [
            {
                "broker_order_id": o.broker_order_id,
                "client_order_id": o.client_order_id,
                "symbol": o.symbol,
                "side": o.side.value,
                "order_class": o.order_class.value,
                "status": o.status.value,
                "leg_role": o.leg_role,
                "parent_client_order_id": o.parent_client_order_id,
            }
            for o in orders
        ],
    }


def cancel_all_orders(broker: AlpacaBroker) -> list[str]:
    """Cancel every open order on the account.

    Returns the list of broker_order_ids we attempted to cancel.
    Individual cancel failures are logged and collected but do not
    abort the sequence — we want to hit every order so residuals
    don't carry over.
    """
    cancelled: list[str] = []
    try:
        open_orders = broker.get_open_orders()
    except StrategyError as exc:
        log.warning("cannot list open orders: %s", exc)
        return cancelled
    for o in open_orders:
        try:
            broker.cancel_order(o.broker_order_id)
            cancelled.append(o.broker_order_id)
        except StrategyError as exc:
            log.warning("cancel %s failed: %s", o.broker_order_id, exc)
    return cancelled


def flatten_all_positions(broker: AlpacaBroker) -> list[tuple[str, str]]:
    """Flatten every open position via the broker's flatten_symbol.

    Returns a list of ``(symbol, outcome)`` tuples where outcome is
    either the close order's broker_order_id or the error string.
    """
    results: list[tuple[str, str]] = []
    try:
        positions = broker.get_positions()
    except StrategyError as exc:
        log.warning("cannot list positions: %s", exc)
        return results
    for p in positions:
        coid = f"{COID_PREFIX}cleanup-{uuid.uuid4().hex[:20]}"
        try:
            res = broker.flatten_symbol(p.symbol, close_client_order_id=coid)
            results.append((p.symbol, res.close_order.broker_order_id))
        except StrategyError as exc:
            log.warning("flatten %s failed: %s", p.symbol, exc)
            results.append((p.symbol, f"error: {exc}"))
    return results


def run_cleanup(
    broker: AlpacaBroker,
    *,
    settle_s: float = 2.0,
    poll_interval_s: float = 1.0,
    poll_deadline_s: float = 15.0,
) -> int:
    """Run the cleanup sequence end-to-end.

    Prints a BEFORE snapshot, cancels orders, flattens positions,
    polls until the account looks clean (or gives up after
    ``poll_deadline_s`` seconds), prints an AFTER snapshot. Returns an
    exit code.

    ``poll_interval_s`` / ``poll_deadline_s`` are tunable so tests can
    exercise the dirty-account branch quickly.
    """
    print("=== paper_cleanup ===")
    before = snapshot(broker)
    print("BEFORE:")
    print(json.dumps(before, indent=2))

    cancelled = cancel_all_orders(broker)
    if cancelled:
        print(f"cancelled order ids: {cancelled}")
    else:
        print("no open orders to cancel")

    # Give Alpaca a moment to process cancels before we flatten.
    time.sleep(settle_s)

    flattened = flatten_all_positions(broker)
    if flattened:
        print("flatten results:")
        for sym, outcome in flattened:
            print(f"  {sym}: {outcome}")
    else:
        print("no positions to flatten")

    # Poll for a clean final state. In paper, Alpaca usually settles
    # within ~1 second; the deadline is generous.
    deadline = time.monotonic() + poll_deadline_s
    after: dict = {}
    while time.monotonic() < deadline:
        after = snapshot(broker)
        if "error" in after:
            break
        if not after["positions"] and not after["open_orders"]:
            break
        time.sleep(poll_interval_s)

    print("AFTER:")
    print(json.dumps(after, indent=2))

    if "error" in after:
        return 1
    if after["positions"] or after["open_orders"]:
        print("FAIL: account not clean; operator must intervene")
        return 1
    print("OK: account is flat with no open orders")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Paper-account cleanup")
    ap.add_argument("--yes", action="store_true", help="Required to place close orders")
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not args.yes:
        print(
            "paper_cleanup: refusing to place close orders without --yes.",
            file=sys.stderr,
        )
        return 2

    env = load_env(args.env_file)
    try:
        broker = build_broker(env)
    except SystemExit as exc:
        print(f"paper_cleanup: {exc}", file=sys.stderr)
        return 2

    return run_cleanup(broker)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
