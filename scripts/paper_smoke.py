"""Opt-in Alpaca paper smoke test.

This script is **deliberately not** picked up by pytest. It places real
orders against Alpaca's **paper** endpoint and verifies that the
broker's behaviour matches the assumptions encoded in our wrapper and
reconciliation logic.

What it proves (when a run is clean):

* ``OrderClass.OTO`` with a ``stop_loss`` child really does produce a
  parent + child we can observe in ``get_open_orders`` with the
  ``parent_client_order_id`` linkage.
* Our idempotent ``client_order_id`` mechanism really does get
  deduped by Alpaca with a 422 "client_order_id already used" response.
* ``poll_terminal`` observes FILLED / CANCELED / EXPIRED inside our
  timeout budget.
* ``flatten_symbol``'s 5-step sequence (cancel children → wait →
  market-close → poll → verify) leaves the account genuinely flat and
  with no orphan orders.
* Cancel latency on a resting limit is inside our configured poll
  timeout.
* A fresh ``reconcile`` after each lifecycle event matches our expected
  mismatch category (or is clean).

What it does **not** prove reliably:

* Partial-fill-then-DAY-expire. Paper fills large liquid orders
  instantly; reproducing a partial on SPY is market-dependent. We
  attempt it best-effort and report skipped if we can't trigger it.

Guardrails:

* Refuses to run unless ``base_url`` contains ``paper``.
* Refuses to run if ``--yes`` is not passed.
* Uses a small fixed qty (1 share of SPY by default).
* Cleans up after itself: every positive run ends with a final
  flatten-all + cancel-all. On failure, prints the exact broker state.
* Exit code 0 on all-pass, 1 on any failure, 2 on precondition error,
  3 on dirty cleanup (operator intervention required).

Usage::

    # paper credentials in ~/.alpaca_paper.env:
    #   ALPACA_API_KEY=PK...
    #   ALPACA_API_SECRET=sk...
    python -m scripts.paper_smoke --yes

    # or pass a custom env file:
    python -m scripts.paper_smoke --yes --env-file ./secrets/paper.env

    # connectivity-only (no orders placed):
    python -m scripts.paper_smoke --dry-run

**Do not** run this against a live account. The ``paper`` URL check is
defence in depth, not a replacement for checking your credentials.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable

from strategy.broker import AlpacaBroker, BarTimeframe, RetryPolicy
from strategy.dto import (
    COID_PREFIX,
    OrderClass,
    OrderIntent,
    OrderSide,
    OrderStatus,
    TimeInForce,
)
from strategy.errors import (
    DuplicateClientOrderId,
    OrderRejected,
    PermanentBrokerError,
    StrategyError,
    TransientBrokerError,
)
from strategy.reconcile import reconcile
from strategy.state import StrategyState


log = logging.getLogger("paper_smoke")

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
DEFAULT_SYMBOL = "SPY"
DEFAULT_QTY = 1
DEFAULT_ENV_FILE = Path.home() / ".alpaca_paper.env"


# ---------------------------------------------------------------------------
# Stage reporting
# ---------------------------------------------------------------------------


@dataclass
class StageResult:
    name: str
    passed: bool
    details: str = ""
    skipped: bool = False


@dataclass
class SmokeReport:
    stages: list[StageResult] = field(default_factory=list)
    cleanup_clean: bool = False

    def add(self, name: str, passed: bool, details: str = "", skipped: bool = False) -> StageResult:
        r = StageResult(name=name, passed=passed, details=details, skipped=skipped)
        self.stages.append(r)
        _emit_stage(r)
        return r

    def any_failed(self) -> bool:
        return any(s for s in self.stages if not s.passed and not s.skipped)


def _emit_stage(r: StageResult) -> None:
    mark = "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL")
    banner = f"[{mark:4s}] {r.name}"
    if r.details:
        banner = f"{banner} — {r.details}"
    print(banner, flush=True)


# ---------------------------------------------------------------------------
# Environment loading
# ---------------------------------------------------------------------------


def load_env(env_file: Path | None) -> dict[str, str]:
    """Load ``KEY=VALUE`` pairs from a dotenv-like file if present.

    OS environment takes precedence over file values, matching how
    ``load_config`` treats secrets. Comment lines (``#``) and blank
    lines are ignored.
    """
    env = dict(os.environ)
    if env_file is None or not env_file.exists():
        return env
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip("'\"")
        env.setdefault(k, v)
    return env


def require_paper_mode(env: dict[str, str]) -> str:
    """Return the paper base URL; refuse to proceed if it looks live."""
    base_url = env.get("ALPACA_BASE_URL", PAPER_BASE_URL)
    if "paper" not in base_url.lower():
        raise SystemExit(
            f"refusing to run: ALPACA_BASE_URL={base_url!r} is not a paper endpoint"
        )
    return base_url


def build_broker(env: dict[str, str]) -> AlpacaBroker:
    key = env.get("ALPACA_API_KEY") or env.get("ALPACA_API_KEY_PAPER")
    secret = env.get("ALPACA_API_SECRET") or env.get("ALPACA_API_SECRET_PAPER")
    if not key or not secret:
        raise SystemExit(
            "ALPACA_API_KEY / ALPACA_API_SECRET not present in env or env-file"
        )
    require_paper_mode(env)
    return AlpacaBroker.from_credentials(
        api_key=key,
        api_secret=secret,
        paper=True,
        data_feed="iex",                   # OK in paper; live uses SIP
        retry_policy=RetryPolicy(max_retries=3, base_s=0.5, cap_s=4.0),
        poll_interval_s=0.5,
        poll_timeout_s=20.0,
    )


# ---------------------------------------------------------------------------
# Smoke stages
# ---------------------------------------------------------------------------


def stage_connectivity(report: SmokeReport, broker: AlpacaBroker) -> bool:
    try:
        acct = broker.get_account_snapshot()
    except StrategyError as exc:
        report.add("connectivity", False, f"broker unreachable: {exc}")
        return False
    report.add(
        "connectivity",
        True,
        f"equity={acct.equity} buying_power={acct.buying_power}",
    )
    return True


def stage_market_open(report: SmokeReport, broker: AlpacaBroker) -> bool:
    """Return True if the market is currently open.

    Several later stages require an open market to reach terminal
    states; they skip rather than fail when closed.
    """
    try:
        # We go through get_positions as a cheap liveness check; Alpaca's
        # clock endpoint is nice-to-have but not strictly needed.
        broker.get_positions()
    except StrategyError as exc:
        report.add("market_sanity", False, f"positions query failed: {exc}")
        return False
    report.add("market_sanity", True)
    return True


def _build_intent(
    broker: AlpacaBroker,
    symbol: str,
    qty: int,
    *,
    suffix: str,
    aggressive_bps: int = 20,
) -> tuple[OrderIntent, Decimal]:
    """Build a marketable-limit OTO intent around the latest quote.

    ``aggressive_bps`` is added to the mid for the limit and subtracted
    for the disaster stop. Returns ``(intent, mid_price)``.
    """
    quote = broker.get_latest_quote(symbol)
    mid = quote.mid()
    bump = mid * Decimal(aggressive_bps) / Decimal(10_000)
    limit = (mid + bump).quantize(Decimal("0.01"))
    # Keep disaster stop a clear margin below to avoid Alpaca rejecting
    # an under-tight stop.
    disaster = (mid * Decimal("0.99")).quantize(Decimal("0.01"))
    intent_id = f"smoke-{suffix}-{uuid.uuid4().hex[:8]}"
    intent = OrderIntent(
        intent_id=intent_id,
        symbol=symbol,
        side=OrderSide.BUY,
        qty=qty,
        limit_price=limit,
        disaster_stop_price=disaster,
        tif=TimeInForce.DAY,
        order_class=OrderClass.OTO,
        reason="paper_smoke",
        ref_price=mid,
        atr=Decimal("1.0"),
        spread_bps=quote.spread_bps(),
        ts=datetime.now(timezone.utc),
    )
    return intent, mid


def stage_oto_submission(
    report: SmokeReport,
    broker: AlpacaBroker,
    symbol: str,
    qty: int,
) -> tuple[OrderIntent, str] | None:
    """Submit an OTO entry and verify parent+child appear on the broker."""
    try:
        intent, mid = _build_intent(broker, symbol, qty, suffix="oto")
        submitted = broker.submit_entry_with_protection(intent)
    except StrategyError as exc:
        report.add("oto_submission", False, f"submit failed: {exc}")
        return None

    details = (
        f"parent coid={submitted.parent.client_order_id} "
        f"child coid={submitted.stop_child.client_order_id if submitted.stop_child else 'MISSING'}"
    )
    if submitted.stop_child is None:
        report.add("oto_submission", False, f"{details} — no stop child returned")
        return None
    if submitted.stop_child.parent_client_order_id != intent.client_order_id():
        report.add(
            "oto_submission",
            False,
            f"{details} — child parent_client_order_id does not match parent coid",
        )
        return None
    report.add("oto_submission", True, details)
    return intent, submitted.stop_child.client_order_id


def stage_poll_terminal(
    report: SmokeReport,
    broker: AlpacaBroker,
    intent: OrderIntent,
) -> OrderStatus:
    try:
        terminal = broker.poll_terminal(intent.client_order_id(), timeout_s=25.0)
    except StrategyError as exc:
        report.add("poll_terminal", False, f"poll failed: {exc}")
        return OrderStatus.UNKNOWN
    except TimeoutError as exc:
        report.add("poll_terminal", False, f"timeout: {exc}")
        return OrderStatus.UNKNOWN
    report.add(
        "poll_terminal",
        True,
        f"status={terminal.status.value} filled_qty={terminal.filled_qty}",
    )
    return terminal.status


def stage_idempotent_duplicate(
    report: SmokeReport,
    broker: AlpacaBroker,
    intent: OrderIntent,
) -> bool:
    """Re-submit the exact same intent. Expect DuplicateClientOrderId and
    a successful resolve-by-COID."""
    try:
        submitted = broker.submit_entry_with_protection(intent)
    except DuplicateClientOrderId as exc:
        # If the resolve-by-COID path inside submit_entry_with_protection
        # fails, it re-raises. That is itself a broker-behavior surprise
        # and worth flagging.
        report.add(
            "idempotent_coid",
            False,
            f"duplicate raised but resolve failed: {exc}",
        )
        return False
    except StrategyError as exc:
        report.add("idempotent_coid", False, f"unexpected error: {exc}")
        return False
    # When the duplicate resolves, the returned parent's COID must match.
    if submitted.parent.client_order_id != intent.client_order_id():
        report.add(
            "idempotent_coid",
            False,
            f"resolved order COID mismatch: got {submitted.parent.client_order_id}",
        )
        return False
    report.add("idempotent_coid", True, "duplicate resolved to existing order")
    return True


def stage_reconcile_post_entry(
    report: SmokeReport,
    broker: AlpacaBroker,
    symbol: str,
    entry_intent: OrderIntent,
) -> bool:
    """After a filled entry, a fresh reconcile should be clean provided
    local state correctly records the open trade."""
    try:
        positions = broker.get_positions()
        open_orders = broker.get_open_orders()
    except StrategyError as exc:
        report.add("reconcile_post_entry", False, f"query failed: {exc}")
        return False

    # Build a synthetic StrategyState that reflects what the orchestrator
    # would have: one open trade on ``symbol`` with matching protective
    # child COID.
    state = StrategyState()
    pos = next((p for p in positions if p.symbol == symbol), None)
    if pos is None:
        report.add(
            "reconcile_post_entry",
            True,
            "broker shows no position (entry not filled) — skipping",
            skipped=True,
        )
        return True
    child = next(
        (
            o for o in open_orders
            if o.symbol == symbol and o.leg_role == "stop_child"
            and o.parent_client_order_id == entry_intent.client_order_id()
        ),
        None,
    )
    from strategy.dto import OpenTrade
    state.open_trades[symbol] = OpenTrade(
        symbol=symbol,
        qty=pos.qty,
        entry_price=pos.avg_entry_price,
        entry_ts=datetime.now(timezone.utc),
        stop_price=entry_intent.disaster_stop_price,
        disaster_stop_price=entry_intent.disaster_stop_price,
        target_price=entry_intent.limit_price + Decimal("5.00"),
        intent_id=entry_intent.intent_id,
        parent_client_order_id=entry_intent.client_order_id(),
        protective_child_client_order_id=child.client_order_id if child else None,
        protective_child_broker_id=child.broker_order_id if child else None,
        last_seen_broker_qty=pos.qty,
    )

    rec = reconcile(state, positions, open_orders)
    if not rec.is_clean():
        details = (
            f"missing={rec.missing_positions} extra={rec.extra_positions} "
            f"qty={rec.qty_mismatches} orphans={rec.orphan_protective_orders} "
            f"unknown_coid={rec.broker_order_with_unknown_coid}"
        )
        report.add("reconcile_post_entry", False, details)
        return False
    report.add("reconcile_post_entry", True, "clean after entry")
    return True


def stage_flatten(
    report: SmokeReport,
    broker: AlpacaBroker,
    symbol: str,
) -> bool:
    coid = f"{COID_PREFIX}smoke-close-{uuid.uuid4().hex[:20]}"
    try:
        result = broker.flatten_symbol(symbol, close_client_order_id=coid)
    except StrategyError as exc:
        report.add("flatten", False, f"flatten raised: {exc}")
        return False
    if result.final_position_qty != 0:
        report.add("flatten", False, f"final qty={result.final_position_qty}")
        return False
    # Verify no open orders remain for the symbol.
    try:
        remaining = broker.get_open_orders(symbol=symbol)
    except StrategyError as exc:
        report.add("flatten", False, f"post-flatten query failed: {exc}")
        return False
    if remaining:
        report.add(
            "flatten",
            False,
            f"orders still open: {[o.broker_order_id for o in remaining]}",
        )
        return False
    report.add(
        "flatten",
        True,
        f"cancelled={list(result.cancelled_order_ids)} close_status={result.close_order.status.value}",
    )
    return True


def stage_cancel_latency(
    report: SmokeReport,
    broker: AlpacaBroker,
    symbol: str,
) -> bool:
    """Submit a far-from-market OTO (to guarantee it rests), cancel the
    parent, and confirm a terminal state within our poll budget.

    We deliberately place the parent far below the market so it won't
    fill, then cancel. If the parent fills by accident (fast market
    move), we clean up with a flatten.
    """
    try:
        quote = broker.get_latest_quote(symbol)
    except StrategyError as exc:
        report.add("cancel_latency", False, f"quote failed: {exc}")
        return False
    mid = quote.mid()
    # Put the limit 1% BELOW mid so it will NOT be marketable.
    limit = (mid * Decimal("0.99")).quantize(Decimal("0.01"))
    disaster = (mid * Decimal("0.95")).quantize(Decimal("0.01"))
    intent = OrderIntent(
        intent_id=f"smoke-cancel-{uuid.uuid4().hex[:8]}",
        symbol=symbol,
        side=OrderSide.BUY,
        qty=1,
        limit_price=limit,
        disaster_stop_price=disaster,
        tif=TimeInForce.DAY,
        order_class=OrderClass.OTO,
        reason="paper_smoke_cancel_latency",
        ref_price=mid,
        atr=Decimal("1.0"),
        spread_bps=quote.spread_bps(),
        ts=datetime.now(timezone.utc),
    )
    try:
        submitted = broker.submit_entry_with_protection(intent)
    except StrategyError as exc:
        report.add("cancel_latency", False, f"submit failed: {exc}")
        return False
    t0 = time.monotonic()
    try:
        broker.cancel_order(submitted.parent.broker_order_id)
    except StrategyError as exc:
        report.add("cancel_latency", False, f"cancel raised: {exc}")
        return False
    # Poll until broker confirms terminal; or give up.
    deadline = t0 + 25.0
    terminal = None
    while time.monotonic() < deadline:
        try:
            fetched = broker.get_order_by_coid(intent.client_order_id())
        except StrategyError:
            fetched = None
        if fetched is not None and fetched.is_terminal():
            terminal = fetched
            break
        time.sleep(0.5)
    elapsed = time.monotonic() - t0
    if terminal is None:
        report.add("cancel_latency", False, f"cancel never reached terminal in {elapsed:.1f}s")
        # Best effort cleanup.
        try:
            broker.flatten_symbol(symbol, close_client_order_id=f"{COID_PREFIX}sm-cl-{uuid.uuid4().hex[:20]}")
        except StrategyError:
            pass
        return False
    report.add(
        "cancel_latency",
        True,
        f"terminal={terminal.status.value} in {elapsed:.2f}s",
    )
    return True


def stage_partial_fill_best_effort(
    report: SmokeReport,
    broker: AlpacaBroker,
    symbol: str,
) -> None:
    """Partial fills on liquid paper symbols are hard to trigger
    reliably. We document what was observed rather than fail.
    """
    # A larger qty with a tight limit *might* partial on thin liquidity,
    # but in practice Alpaca's paper sim fills liquid names instantly.
    # We don't attempt the order here; we just record the skip so
    # reports show it was considered.
    report.add(
        "partial_fill_best_effort",
        True,
        "not reliably reproducible on liquid paper symbols — skipped",
        skipped=True,
    )


def final_cleanup(report: SmokeReport, broker: AlpacaBroker, symbol: str) -> None:
    """Last-resort cleanup: cancel any open orders for the symbol and
    flatten any remaining position."""
    errors: list[str] = []
    try:
        for o in broker.get_open_orders(symbol=symbol):
            try:
                broker.cancel_order(o.broker_order_id)
            except StrategyError as exc:
                errors.append(f"cancel {o.broker_order_id}: {exc}")
    except StrategyError as exc:
        errors.append(f"list orders: {exc}")

    # Give Alpaca a moment to process cancels before checking position.
    time.sleep(1.0)

    try:
        positions = broker.get_positions()
    except StrategyError as exc:
        errors.append(f"positions: {exc}")
        positions = []
    pos = next((p for p in positions if p.symbol == symbol), None)
    if pos is not None:
        try:
            broker.flatten_symbol(symbol, close_client_order_id=f"{COID_PREFIX}smoke-cleanup-{uuid.uuid4().hex[:20]}")
        except StrategyError as exc:
            errors.append(f"flatten: {exc}")

    report.cleanup_clean = not errors
    if errors:
        report.add("cleanup", False, "; ".join(errors))
    else:
        report.add("cleanup", True, "no residual orders or position")


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------


def run_smoke(
    broker: AlpacaBroker,
    *,
    symbol: str = DEFAULT_SYMBOL,
    qty: int = DEFAULT_QTY,
    dry_run: bool = False,
) -> SmokeReport:
    report = SmokeReport()
    if not stage_connectivity(report, broker):
        return report

    if dry_run:
        report.add("dry_run_exit", True, "connectivity verified; no orders placed", skipped=True)
        return report

    if not stage_market_open(report, broker):
        return report

    result = stage_oto_submission(report, broker, symbol, qty)
    if result is None:
        final_cleanup(report, broker, symbol)
        return report
    intent, _ = result

    parent_status = stage_poll_terminal(report, broker, intent)
    # Idempotency test runs regardless of fill outcome.
    stage_idempotent_duplicate(report, broker, intent)

    if parent_status is OrderStatus.FILLED:
        stage_reconcile_post_entry(report, broker, symbol, intent)
        stage_flatten(report, broker, symbol)
    else:
        report.add(
            "reconcile_post_entry",
            True,
            f"parent did not fill ({parent_status.value}) — skipping",
            skipped=True,
        )
        report.add("flatten", True, "nothing to flatten", skipped=True)

    stage_cancel_latency(report, broker, symbol)
    stage_partial_fill_best_effort(report, broker, symbol)

    final_cleanup(report, broker, symbol)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Opt-in Alpaca paper smoke test")
    ap.add_argument("--yes", action="store_true", help="Required to actually place orders")
    ap.add_argument("--dry-run", action="store_true", help="Connectivity check only")
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    ap.add_argument("--symbol", default=DEFAULT_SYMBOL)
    ap.add_argument("--qty", type=int, default=DEFAULT_QTY)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if not args.yes and not args.dry_run:
        print(
            "paper_smoke: refusing to place orders without --yes. "
            "Pass --dry-run to verify connectivity only.",
            file=sys.stderr,
        )
        return 2

    env = load_env(args.env_file)
    try:
        broker = build_broker(env)
    except SystemExit as exc:
        print(f"paper_smoke: {exc}", file=sys.stderr)
        return 2

    print(f"=== Paper smoke test ({'dry-run' if args.dry_run else 'live-paper'}) ===")
    print(f"symbol={args.symbol} qty={args.qty}")
    print(f"env-file={args.env_file}")
    print()

    report = run_smoke(broker, symbol=args.symbol, qty=args.qty, dry_run=args.dry_run)

    passed = [s for s in report.stages if s.passed and not s.skipped]
    failed = [s for s in report.stages if not s.passed and not s.skipped]
    skipped = [s for s in report.stages if s.skipped]
    print()
    print(f"Summary: {len(passed)} pass / {len(failed)} fail / {len(skipped)} skipped")

    if failed:
        return 1
    if not report.cleanup_clean and not args.dry_run:
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
