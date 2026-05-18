"""Strict orchestrator — ties broker, risk, state, log, and signal together.

Design invariants (preserved from Phase 1):

* Broker truth is authoritative. Any mismatch blocks new entries.
* Every entry is logged as INTENT before any broker contact.
* Every broker outcome is logged as RESULT after terminal status.
* ``client_order_id`` is derived from the intent (idempotent).
* Exits are driven by the bot (primary stop / target / time-stop / session
  flatten) with the OTO stop child as the broker-side disaster backstop.
* On any non-zero protective-leg orphan or side mismatch, the bot halts.
* Session-end flatten is non-negotiable: intraday only, flat by close.
* No local optimistic fill assumptions — only broker-confirmed state is
  recorded.

What this module does NOT do:

* Market data: it asks the broker. There is no local cache beyond the
  in-flight tick.
* Strategy logic: it delegates to :mod:`strategy.signal`.
* Risk math: it delegates to :mod:`strategy.risk`.
* SDK calls: it goes through :class:`AlpacaBroker`.

A single :meth:`Strategy.tick` is one orchestrator iteration. The driver
(``run_strategy.py``) owns the loop, the sleep cadence, and the kill
switch / lock file.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from strategy.broker import AlpacaBroker, BarTimeframe
from strategy.config import Config
from strategy.dto import (
    COID_PREFIX,
    AccountSnapshot,
    Bar,
    BrokerOrder,
    OpenTrade,
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
)
from strategy.errors import (
    AuditIntegrityError,
    OrderOutcomeUnknown,
    PermanentBrokerError,
    ReconcileMismatch,
    StaleDataError,
    StrategyError,
    TransientBrokerError,
)
from strategy.reconcile import reconcile
from strategy.recovery import OutcomeKind, recover_pending_submissions
from strategy.risk import EntryRequest, evaluate_entry
from strategy.signal import BarFrame, evaluate_signal
from strategy.state import StateStore, StrategyState
from strategy.time_utils import (
    SessionClock,
    TF_NAME_TO_MINUTES,
    ensure_utc,
    is_stale,
    today_utc,
)
from strategy.trade_log import TradeLog


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tick / recovery reports
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TickReport:
    ts: datetime
    reconcile_report: ReconcileReport
    entries_submitted: list[str] = field(default_factory=list)
    exits_submitted: list[str] = field(default_factory=list)
    denies: list[tuple[str, str]] = field(default_factory=list)   # (symbol, reason)
    halted: bool = False
    halt_reason: str | None = None
    kill_switch_blocked: bool = False


@dataclass(slots=True)
class RecoveryReport:
    ts: datetime
    initial_report: ReconcileReport
    repaired_extra_positions: list[str] = field(default_factory=list)
    dropped_missing_positions: list[str] = field(default_factory=list)
    unrecoverable: list[str] = field(default_factory=list)
    halted: bool = False
    halt_reason: str | None = None


# ---------------------------------------------------------------------------
# Timeframe helpers
# ---------------------------------------------------------------------------


_TF_NAME_TO_ENUM: dict[str, BarTimeframe] = {
    "5Min": BarTimeframe.M5,
    "15Min": BarTimeframe.M15,
    "1Hour": BarTimeframe.H1,
}


# ---------------------------------------------------------------------------
# Strategy orchestrator
# ---------------------------------------------------------------------------


class Strategy:
    """Single-responsibility orchestrator.

    One instance per process. Not thread-safe by design.
    """

    # Throttle window for per-(symbol, decision_kind, reason) diagnostic
    # records. Engine ticks every ~10s; without throttling we'd emit
    # dozens of identical "trend_not_up" lines per 5 min per symbol.
    DIAG_THROTTLE_S = 300

    def __init__(
        self,
        config: Config,
        broker: AlpacaBroker,
        state_store: StateStore,
        trade_log: TradeLog,
    ) -> None:
        self.config = config
        self.broker = broker
        self.state_store = state_store
        self.trade_log = trade_log
        self._state: StrategyState = state_store.load()
        self._session_clock = SessionClock(
            session_start=config.risk.session_start_utc,
            session_end=config.risk.session_end_utc,
            block_first_minutes=config.risk.block_first_minutes,
            block_last_minutes=config.risk.block_last_minutes,
            flat_before_close_minutes=config.execution.flat_before_close_minutes,
        )
        # Per-(symbol, decision_kind, reason) → last emit timestamp.
        # Populated by _emit_diagnostic; never read in trading code paths.
        self._diag_last_emit: dict[tuple[str, str, str], datetime] = {}

    # ---- observability ----------------------------------------------

    def _emit_diagnostic(
        self,
        now: datetime,
        symbol: str,
        decision_kind: str,
        reason: str,
        extra: dict | None = None,
    ) -> None:
        """Log + (paper-mode only) audit a per-symbol decision.

        Throttled to once per :attr:`DIAG_THROTTLE_S` seconds per
        (symbol, decision_kind, reason) tuple. Never raises — diagnostics
        must not interfere with trading. ``decision_kind`` is one of
        ``no_signal`` / ``risk_denied`` / ``evaluated``.
        """
        key = (symbol, decision_kind, reason)
        last = self._diag_last_emit.get(key)
        if last is not None and (now - last).total_seconds() < self.DIAG_THROTTLE_S:
            return
        self._diag_last_emit[key] = now

        log.info(
            "evaluated symbol=%s decision=%s reason=%s%s",
            symbol,
            decision_kind,
            reason,
            f" {extra}" if extra else "",
        )

        # Audit-log the decision in paper mode so trades.jsonl exists
        # even on no-trade days. Live mode keeps the audit lean.
        if self.config.mode != "paper":
            return
        try:
            payload = {
                "kind": "DIAGNOSTIC",
                "decision": decision_kind,
                "symbol": symbol,
                "reason": reason,
            }
            if extra:
                payload["extra"] = extra
            self.trade_log.append_incident(payload)
        except Exception:  # noqa: BLE001
            log.exception("failed to append diagnostic to trade_log")

    # ---- accessors for tests / driver -------------------------------

    @property
    def state(self) -> StrategyState:
        return self._state

    @property
    def session_clock(self) -> SessionClock:
        return self._session_clock

    # ---- public lifecycle -------------------------------------------

    def recover(self, now: datetime) -> RecoveryReport:
        """Load state, reconcile against broker, repair or halt.

        On any protective-leg orphan or side mismatch that cannot be
        auto-resolved, we set a persistent halt and refuse to submit new
        entries until an operator clears it.
        """
        now = ensure_utc(now)
        # Day rollover pre-reconcile: intraday-scoped counters reset to
        # today so sizing and cooldowns aren't carrying yesterday's state.
        today = today_utc(now)
        if self._state.trading_day != today:
            if self._state.trading_day != date(1970, 1, 1):
                self._state.roll_to_new_day(today)
            else:
                self._state.trading_day = today

        # Recover any submitted-but-unconfirmed entry BEFORE reconcile so a
        # restart mid-incident folds the real position into open_trades
        # first — otherwise the existing orphan-protective halt below would
        # trip on a position we can actually account for.
        self._recover_pending_submissions(now)

        try:
            account = self.broker.get_account_snapshot()
            positions = self.broker.get_positions()
            open_orders = self.broker.get_open_orders()
        except TransientBrokerError as exc:
            # If we cannot talk to the broker at startup, fail closed —
            # do not proceed to any trading.
            raise ReconcileMismatch(f"broker unreachable during recovery: {exc}") from exc

        self._state.record_reconciled_equity(account.equity)
        report = reconcile(self._state, positions, open_orders)

        rep = RecoveryReport(ts=now, initial_report=report)

        # -- Repair extra positions when we can reconstruct metadata ---
        for sym in report.extra_positions:
            rebuilt = self._rebuild_open_trade_from_history(sym, positions)
            if rebuilt is not None:
                self._state.open_trades[sym] = rebuilt
                rep.repaired_extra_positions.append(sym)
            else:
                rep.unrecoverable.append(
                    f"extra_position_no_history:{sym}"
                )

        # -- Drop local phantom positions ------------------------------
        for sym in report.missing_positions:
            self._state.open_trades.pop(sym, None)
            rep.dropped_missing_positions.append(sym)

        # -- Halt on the truly dangerous mismatches --------------------
        if report.requires_halt():
            self._state.set_halt(
                "recovery_position_side_mismatch",
                reason=f"shorts seen: {report.position_side_mismatch}",
                now=now,
            )
            self._log_incident(
                "position_side_mismatch",
                {
                    "phase": "recover",
                    "symbols": list(report.position_side_mismatch),
                    "now": now.isoformat(),
                },
            )
            rep.halted = True
            rep.halt_reason = "position_side_mismatch"
        elif report.orphan_protective_orders or report.unexpected_protective_children:
            self._state.set_halt(
                "recovery_orphan_protective_orders",
                reason=(
                    f"orphans: {report.orphan_protective_orders}; "
                    f"extras: {report.unexpected_protective_children}"
                ),
                now=now,
            )
            self._log_incident(
                "orphan_protective_orders",
                {
                    "phase": "recover",
                    "orphans": list(report.orphan_protective_orders),
                    "extras": list(report.unexpected_protective_children),
                    "now": now.isoformat(),
                },
            )
            rep.halted = True
            rep.halt_reason = "orphan_protective_orders"
        elif rep.unrecoverable:
            self._state.set_halt(
                "recovery_unrecoverable",
                reason=f"cannot reconstruct: {rep.unrecoverable}",
                now=now,
            )
            self._log_incident(
                "recovery_unrecoverable",
                {
                    "phase": "recover",
                    "unrecoverable": list(rep.unrecoverable),
                    "now": now.isoformat(),
                },
            )
            rep.halted = True
            rep.halt_reason = "unrecoverable"

        self.state_store.save(self._state)
        return rep

    def tick(self, now: datetime, *, kill_switch_present: bool) -> TickReport:
        """One orchestrator iteration.

        Order:
        1) Day rollover check (intraday counters).
        2) Kill switch blocks NEW entries (but we still manage exits!).
        3) Reconcile. Any non-clean report blocks new entries.
        4) Update reconciled equity / peak / intraday low.
        5) Check daily-loss / drawdown halts; if just tripped, flatten all.
        6) Manage existing positions (primary stop/target/time/session-end).
        7) If clean & session-active & no halt: per-symbol entry eval.
        8) Save state.
        """
        now = ensure_utc(now)
        report = TickReport(ts=now, reconcile_report=ReconcileReport())

        self._maybe_rollover(now)

        # --- recover lost submissions FIRST ---------------------------
        # Strictly before reconcile/entry/orphan-checks: a recovered
        # position must be in open_trades when reconcile runs, or it
        # would be flagged as an orphan-protective mismatch (the exact
        # 2026-05-15 failure). An escalated halt here flows naturally
        # into the sticky-halt branch below (exits managed, no entries).
        self._recover_pending_submissions(now)

        # --- reconcile first ------------------------------------------
        try:
            account = self.broker.get_account_snapshot()
            positions = self.broker.get_positions()
            open_orders = self.broker.get_open_orders()
        except StrategyError as exc:
            # Leave state intact. No new entries or exits this tick.
            log.warning("tick broker query failed: %s", exc)
            report.halted = True
            report.halt_reason = f"broker_error:{exc.__class__.__name__}"
            return report
        recon = reconcile(self._state, positions, open_orders)
        report.reconcile_report = recon

        if recon.requires_halt():
            self._state.set_halt(
                "tick_position_side_mismatch",
                reason=f"shorts seen: {recon.position_side_mismatch}",
                now=now,
            )
            self._log_incident(
                "position_side_mismatch",
                {
                    "phase": "tick",
                    "symbols": list(recon.position_side_mismatch),
                    "now": now.isoformat(),
                },
            )
            report.halted = True
            report.halt_reason = "position_side_mismatch"
            self.state_store.save(self._state)
            return report

        self._state.record_reconciled_equity(account.equity)

        # --- halt detection drives flatten ----------------------------
        if self._just_tripped_daily_loss(account):
            self._state.set_halt("daily_loss_cap", "cap breached", now=now)
            self._log_incident(
                "daily_loss_halt",
                {
                    "phase": "tick",
                    "realized_pnl_today": str(self._state.realized_pnl_today),
                    "equity": str(account.equity),
                    "now": now.isoformat(),
                },
            )
            self._flatten_all(now, reason="daily_loss_halt", report=report)
            report.halted = True
            report.halt_reason = "daily_loss_cap"
            self.state_store.save(self._state)
            return report
        if self._just_tripped_drawdown(account):
            self._state.set_halt("drawdown_halt", "drawdown threshold", now=now)
            self._log_incident(
                "drawdown_halt",
                {
                    "phase": "tick",
                    "peak_equity": str(self._state.peak_equity),
                    "equity": str(account.equity),
                    "now": now.isoformat(),
                },
            )
            self._flatten_all(now, reason="drawdown_halt", report=report)
            report.halted = True
            report.halt_reason = "drawdown_halt"
            self.state_store.save(self._state)
            return report

        if self._state.any_halt_active():
            # Honour sticky halts (including recovery halts). Manage exits
            # only — do not submit new entries.
            self._manage_positions(now, account, positions, report)
            report.halted = True
            report.halt_reason = "sticky_halt"
            self.state_store.save(self._state)
            return report

        # --- manage existing positions (stops/targets/session-end) ----
        self._manage_positions(now, account, positions, report)

        if kill_switch_present:
            report.kill_switch_blocked = True
            self.state_store.save(self._state)
            return report
        if not recon.is_clean():
            # Reconcile is not clean but not halt-worthy. Entries are
            # blocked this tick; log the mismatch categories so the
            # operator can investigate. This is the single most common
            # source of slow-build audit gaps.
            self._log_incident(
                "reconcile_mismatch",
                {
                    "phase": "tick",
                    "missing_positions": list(recon.missing_positions),
                    "extra_positions": list(recon.extra_positions),
                    "qty_mismatches": [list(x) for x in recon.qty_mismatches],
                    "orphan_protective_orders": list(recon.orphan_protective_orders),
                    "unexpected_protective_children": list(recon.unexpected_protective_children),
                    "partial_fill_with_orphan_child": list(recon.partial_fill_with_orphan_child),
                    "broker_order_with_unknown_coid": list(recon.broker_order_with_unknown_coid),
                    "stale_local_orders": list(recon.stale_local_orders),
                    "now": now.isoformat(),
                },
            )
            self.state_store.save(self._state)
            return report
        if not self._session_clock.is_within_session(now):
            self.state_store.save(self._state)
            return report
        if self._session_clock.in_blackout(now):
            self.state_store.save(self._state)
            return report

        # --- evaluate new entries -------------------------------------
        for symbol in self.config.strategy.symbols:
            if symbol in self._state.open_trades:
                continue
            try:
                decision = self._try_enter(
                    symbol, now, account, recon_clean=recon.is_clean(),
                    kill_switch_present=kill_switch_present,
                )
            except StrategyError as exc:
                log.warning("entry attempt for %s failed: %s", symbol, exc)
                report.denies.append((symbol, f"error:{exc.__class__.__name__}"))
                continue
            if decision is None:
                continue
            if decision.allowed:
                report.entries_submitted.append(symbol)
            else:
                report.denies.append((symbol, decision.reason))

        self.state_store.save(self._state)
        return report

    def graceful_shutdown(self, now: datetime, *, flatten: bool) -> None:
        """Called by the driver on SIGTERM.

        When ``flatten=True`` we flatten every open position before
        returning. We always save state.
        """
        now = ensure_utc(now)
        if flatten:
            self._flatten_all(now, reason="graceful_shutdown", report=None)
        self.state_store.save(self._state)

    # ---- internals ---------------------------------------------------

    def _log_incident(self, kind: str, payload: dict) -> None:
        """Append an INCIDENT record to the trade log.

        Swallows its own failures — the trade log's integrity is
        important, but a failed INCIDENT write must not itself halt a
        tick that is trying to flatten or halt for safety reasons. We
        still log the failure to the Python logger.
        """
        try:
            self.trade_log.append_incident({"kind": kind, **payload})
        except Exception:  # noqa: BLE001
            log.exception("failed to append INCIDENT(%s) to trade log", kind)

    def _maybe_rollover(self, now: datetime) -> None:
        today = today_utc(now)
        if self._state.trading_day != today:
            if self._state.trading_day != date(1970, 1, 1):
                self._state.roll_to_new_day(today)
            else:
                self._state.trading_day = today

    def _just_tripped_daily_loss(self, account: AccountSnapshot) -> bool:
        # We use *realized* PnL for this gate, matching risk.py. Daily loss
        # is tripped when realized_pnl_today <= -cap * equity.
        if account.equity <= 0:
            return False
        if self._state.has_halt("daily_loss_cap"):
            return False
        cap = self.config.risk.daily_loss_cap_pct
        return self._state.realized_pnl_today <= -(account.equity * cap)

    def _just_tripped_drawdown(self, account: AccountSnapshot) -> bool:
        if self._state.peak_equity <= 0:
            return False
        if self._state.has_halt("drawdown_halt"):
            return False
        halt_pct = self.config.sizing.halt_drawdown_pct
        return account.equity <= self._state.peak_equity * (Decimal(1) - halt_pct)

    def _manage_positions(
        self,
        now: datetime,
        account: AccountSnapshot,
        positions: list[Position],
        report: TickReport,
    ) -> None:
        broker_by_symbol = {p.symbol: p for p in positions}
        # Force-flatten window first.
        if self._session_clock.is_within_session(now):
            deadline = self._session_clock.flatten_deadline(now)
            if now >= deadline:
                for sym in list(self._state.open_trades.keys()):
                    self._flatten_one(sym, now, reason="session_end_flatten", report=report)
                return

        # Outside session: nothing to manage (market is closed).
        if not self._session_clock.is_within_session(now):
            return

        for sym, trade in list(self._state.open_trades.items()):
            if sym not in broker_by_symbol:
                # Broker says we're flat — reconcile should have surfaced
                # this. Drop from local state and continue.
                self._state.open_trades.pop(sym, None)
                continue
            # Quote-driven exit: ask for latest quote; bail out safely if
            # market data is stale.
            try:
                quote = self.broker.get_latest_quote(sym)
            except StrategyError:
                continue
            if is_stale(quote.ts, now, self.config.risk.stale_data_max_age_s):
                continue

            exit_reason = self._determine_exit(trade, quote, now)
            if exit_reason is not None:
                self._flatten_one(sym, now, reason=exit_reason, report=report)

    def _determine_exit(
        self,
        trade: OpenTrade,
        quote: Quote,
        now: datetime,
    ) -> str | None:
        ref = quote.mid()
        if ref <= trade.stop_price:
            return "primary_stop_hit"
        if ref >= trade.target_price:
            return "target_hit"
        # Time stop: compare held duration against max_hold_bars * entry_tf minutes.
        minutes_per_bar = TF_NAME_TO_MINUTES[self.config.strategy.entry_tf]
        max_hold = timedelta(minutes=minutes_per_bar * self.config.strategy.max_hold_bars)
        if now - trade.entry_ts >= max_hold:
            return "time_stop"
        return None

    def _flatten_all(
        self,
        now: datetime,
        *,
        reason: str,
        report: TickReport | None,
    ) -> None:
        for sym in list(self._state.open_trades.keys()):
            self._flatten_one(sym, now, reason=reason, report=report)

    def _flatten_one(
        self,
        symbol: str,
        now: datetime,
        *,
        reason: str,
        report: TickReport | None,
    ) -> None:
        trade = self._state.open_trades.get(symbol)
        if trade is None:
            return
        coid = f"{COID_PREFIX}close-{uuid.uuid4().hex[:24]}"
        self.trade_log.append_intent(
            {
                "intent_id": f"close-{symbol}-{now.isoformat()}",
                "client_order_id": coid,
                "symbol": symbol,
                "side": OrderSide.SELL.value,
                "qty_requested": trade.qty,
                "reason": reason,
                "equity_snapshot": str(self._state.last_reconciled_equity),
                "peak_equity": str(self._state.peak_equity),
                "result": "submitting_close",
            }
        )
        try:
            result = self.broker.flatten_symbol(symbol, close_client_order_id=coid)
        except StrategyError as exc:
            # Log the failure; keep state intact for the next tick to retry.
            self.trade_log.append_result(
                {
                    "intent_id": f"close-{symbol}-{now.isoformat()}",
                    "client_order_id": coid,
                    "broker_order_id": None,
                    "status": "error",
                    "rejected_reason": f"{exc.__class__.__name__}:{exc}",
                }
            )
            log.warning("flatten %s failed: %s", symbol, exc)
            return
        # Record realized PnL at mid-price approximation (broker truth
        # gives exact on its own side; we log what we know).
        fill_price = result.close_order.avg_fill_price or trade.entry_price
        realized = (fill_price - trade.entry_price) * Decimal(trade.qty)
        self._state.realized_pnl_today += realized
        if realized < 0:
            self._state.last_loss_ts_by_symbol[symbol] = now
        self._state.open_trades.pop(symbol, None)
        self.trade_log.append_result(
            {
                "intent_id": f"close-{symbol}-{now.isoformat()}",
                "client_order_id": coid,
                "broker_order_id": result.close_order.broker_order_id,
                "status": result.close_order.status.value,
                "filled_qty": result.close_order.filled_qty,
                "avg_fill_price": str(fill_price),
                "reason": reason,
                "realized_pnl": str(realized),
            }
        )
        if report is not None:
            report.exits_submitted.append(symbol)

    # ---- entry path --------------------------------------------------

    def _try_enter(
        self,
        symbol: str,
        now: datetime,
        account: AccountSnapshot,
        *,
        recon_clean: bool,
        kill_switch_present: bool,
    ) -> RiskDecision | None:
        """Fetch bars + quote, generate signal, pass through risk, submit.

        Returns ``None`` when the signal itself is silent (no deny-log
        record in that case — nothing happened). Returns the
        :class:`RiskDecision` on both allow and deny so the caller can
        record it.
        """
        # --- fetch market data ---------------------------------------
        frame = self._fetch_bar_frame(symbol, now)
        if frame is None:
            self._emit_diagnostic(now, symbol, "no_signal", "no_bar_frame")
            return None
        sig, sig_reason = evaluate_signal(frame, self.config.strategy)
        if sig is None:
            self._emit_diagnostic(now, symbol, "no_signal", sig_reason)
            return None
        try:
            quote = self.broker.get_latest_quote(symbol)
        except StrategyError:
            self._emit_diagnostic(now, symbol, "no_signal", "quote_fetch_failed")
            return None
        # --- risk gate -----------------------------------------------
        # Alpaca's bar.ts is the bar *open* time, so the freshness check
        # gets the bar close: open + entry_tf_minutes.
        latest_bar_close_ts = frame.entry_bars[-1].ts + timedelta(
            minutes=TF_NAME_TO_MINUTES[self.config.strategy.entry_tf]
        )
        request = EntryRequest(
            signal=sig,
            quote=quote,
            latest_bar_close_ts=latest_bar_close_ts,
            expected_move_bps=sig.expected_move_bps,
            reconcile_clean=recon_clean,
            kill_switch_present=kill_switch_present,
        )
        decision = evaluate_entry(
            request, self._state, self.config, account, now,
            session_clock=self._session_clock,
        )
        if not decision.allowed:
            self.trade_log.append_intent(
                {
                    "intent_id": f"deny-{symbol}-{now.isoformat()}",
                    "client_order_id": None,
                    "symbol": symbol,
                    "side": OrderSide.BUY.value,
                    "qty_requested": 0,
                    "reason": sig.reason,
                    "result": "denied",
                    "deny_reason": decision.reason,
                    "equity_snapshot": str(self._state.last_reconciled_equity),
                    "peak_equity": str(self._state.peak_equity),
                    "drawdown_pct": str(decision.drawdown_pct),
                    "throttle_multiplier": str(decision.throttle),
                }
            )
            # Throttled human-readable mirror of the deny — the INTENT
            # record above is the audit ground truth, this is for
            # operator observability when nothing is trading.
            self._emit_diagnostic(now, symbol, "risk_denied", decision.reason)
            return decision
        assert decision.qty > 0 and decision.limit_price is not None
        assert decision.disaster_stop_price is not None and decision.stop_price is not None
        # --- build intent + log intent + submit ---------------------
        intent_id = f"entry-{symbol}-{now.isoformat()}"
        intent = OrderIntent(
            intent_id=intent_id,
            symbol=symbol,
            side=OrderSide.BUY,
            qty=decision.qty,
            limit_price=decision.limit_price,
            disaster_stop_price=decision.disaster_stop_price,
            tif=TimeInForce.DAY,
            order_class=OrderClass.OTO,
            reason=sig.reason,
            ref_price=sig.ref_price,
            atr=sig.atr,
            spread_bps=quote.spread_bps(),
            ts=now,
        )
        coid = intent.client_order_id()
        # target_price is logged on the INTENT so the recovery path can
        # reconstruct the OpenTrade WITHOUT recomputing it (no parallel
        # accounting). It is the exact value _finalize_entry uses.
        target_price = (
            decision.limit_price
            + (self.config.strategy.atr_target_mult * sig.atr)
        ).quantize(Decimal("0.01"))
        _intent_record = {
            "intent_id": intent_id,
            "client_order_id": coid,
            "symbol": symbol,
            "side": OrderSide.BUY.value,
            "qty_requested": decision.qty,
            "limit_price": str(decision.limit_price),
            "stop_price": str(decision.stop_price),
            "disaster_stop_price": str(decision.disaster_stop_price),
            "target_price": str(target_price),
            "reason": sig.reason,
            "equity_snapshot": str(self._state.last_reconciled_equity),
            "peak_equity": str(self._state.peak_equity),
            "drawdown_pct": str(decision.drawdown_pct),
            "throttle_multiplier": str(decision.throttle),
            "spread_bps": str(quote.spread_bps()),
            "result": "submitting",
        }
        self.trade_log.append_intent(_intent_record)
        try:
            submitted = self.broker.submit_entry_with_protection(intent)
            terminal = self.broker.poll_terminal(coid)
        except OrderOutcomeUnknown as exc:
            # The order may be live at the broker — its terminal state is
            # simply not known yet (a resting limit's fill latency can far
            # exceed the poll window). This is NOT a failure: do NOT write a
            # terminal RESULT and do NOT touch open_trades. Persist an
            # intent-progress marker so _recover_pending_submissions resolves
            # it by COID on a later tick (idempotent, broker-truth driven).
            self.trade_log.append_intent(
                {**_intent_record, "result": "submitted_unconfirmed",
                 "unconfirmed_reason": str(exc)}
            )
            log.warning(
                "entry %s outcome unknown; deferred to recovery: %s", symbol, exc
            )
            return decision
        except StrategyError as exc:
            self.trade_log.append_result(
                {
                    "intent_id": intent_id,
                    "client_order_id": coid,
                    "broker_order_id": None,
                    "status": "error",
                    "rejected_reason": f"{exc.__class__.__name__}:{exc}",
                }
            )
            log.warning("entry submit/poll %s failed: %s", symbol, exc)
            return decision
        # --- post-fill: broker truth is authoritative ---------------
        # We must not assume the parent filled just because it's terminal;
        # partial fill with DONE_FOR_DAY/EXPIRED is a real case.
        self._record_entry_result(
            intent=intent,
            decision=decision,
            terminal=terminal,
            submitted_stop_child=submitted.stop_child,
            now=now,
        )
        return decision

    def _record_entry_result(
        self,
        *,
        intent: OrderIntent,
        decision: RiskDecision,
        terminal: BrokerOrder,
        submitted_stop_child: BrokerOrder | None,
        now: datetime,
    ) -> None:
        """Normal-path entry finalisation. Thin wrapper that resolves the
        derived values (target/stop) and delegates to the single canonical
        writer :meth:`_finalize_entry`, which the recovery path also uses —
        so there is exactly one place that writes an entry RESULT +
        reconstructs open_trades (no parallel accounting)."""
        target_price = (
            intent.limit_price
            + (self.config.strategy.atr_target_mult * intent.atr)
        ).quantize(Decimal("0.01"))
        self._finalize_entry(
            intent_id=intent.intent_id,
            parent_coid=intent.client_order_id(),
            symbol=intent.symbol,
            limit_price=intent.limit_price,
            stop_price=decision.stop_price or intent.disaster_stop_price,
            disaster_stop_price=intent.disaster_stop_price,
            target_price=target_price,
            terminal=terminal,
            submitted_stop_child=submitted_stop_child,
            now=now,
        )

    def _finalize_entry(
        self,
        *,
        intent_id: str,
        parent_coid: str,
        symbol: str,
        limit_price: Decimal,
        stop_price: Decimal,
        disaster_stop_price: Decimal,
        target_price: Decimal,
        terminal: BrokerOrder,
        submitted_stop_child: BrokerOrder | None,
        now: datetime,
    ) -> None:
        """THE canonical entry writer. Appends the terminal RESULT and, on
        a non-zero fill, reconstructs open_trades. Used by both the normal
        submit path and the recovery adapter so their audit + state output
        are byte-for-byte identical. Caller guarantees this runs at most
        once per intent_id (normal path: single call; recovery: only for
        intents with no prior RESULT — RESULT-freeze prevents re-entry)."""
        filled_qty = terminal.filled_qty
        avg_price = terminal.avg_fill_price or limit_price
        child_coid = submitted_stop_child.client_order_id if submitted_stop_child else None
        child_bid = submitted_stop_child.broker_order_id if submitted_stop_child else None
        child_status = submitted_stop_child.status.value if submitted_stop_child else None
        self.trade_log.append_result(
            {
                "intent_id": intent_id,
                "client_order_id": parent_coid,
                "broker_order_id": terminal.broker_order_id,
                "status": terminal.status.value,
                "filled_qty": filled_qty,
                "avg_fill_price": str(avg_price),
                "protective_child_client_order_id": child_coid,
                "protective_child_broker_id": child_bid,
                "protective_child_status": child_status,
            }
        )
        if filled_qty <= 0:
            # Nothing executed: do not record an open trade.
            return
        # Partial or full fill: record the actual qty. Reconcile will
        # verify the OTO child sizes correctly.
        self._state.open_trades[symbol] = OpenTrade(
            symbol=symbol,
            qty=filled_qty,
            entry_price=avg_price,
            entry_ts=now,
            stop_price=stop_price,
            disaster_stop_price=disaster_stop_price,
            target_price=target_price,
            intent_id=intent_id,
            parent_client_order_id=parent_coid,
            protective_child_client_order_id=child_coid,
            protective_child_broker_id=child_bid,
            last_seen_broker_qty=filled_qty,
        )

    # ---- submitted-but-unconfirmed recovery --------------------------

    def _recover_pending_submissions(self, now: datetime) -> bool:
        """Resolve any in-flight submission whose terminal outcome we never
        recorded (the 2026-05-15 lost-fill class). MUST run before reconcile
        and before entry evaluation so a recovered position is in
        open_trades when reconcile computes orphans.

        Pure decisions are made by :mod:`strategy.recovery`; this adapter
        only *applies* the returned instructions, reusing the canonical
        writers (no parallel accounting). Returns True iff a sticky halt
        was escalated this call.
        """
        records = list(self.trade_log.read_all())
        session_start = datetime.combine(
            today_utc(now), self._session_clock.session_start,
            tzinfo=timezone.utc,
        )
        outcomes = recover_pending_submissions(
            records, self.broker, now, session_start=session_start
        )
        halted = False
        for o in outcomes:
            if o.kind is OutcomeKind.RESOLVED:
                p = o.intent_payload
                self._finalize_entry(
                    intent_id=o.intent_id,
                    parent_coid=o.client_order_id,
                    symbol=p["symbol"],
                    limit_price=Decimal(str(p["limit_price"])),
                    stop_price=Decimal(str(p.get("stop_price")
                                           or p["disaster_stop_price"])),
                    disaster_stop_price=Decimal(str(p["disaster_stop_price"])),
                    target_price=Decimal(str(p["target_price"])),
                    terminal=o.terminal,
                    submitted_stop_child=o.submitted.stop_child if o.submitted else None,
                    now=now,
                )
                log.warning(
                    "recovered lost submission %s: status=%s filled=%s",
                    o.intent_id, o.terminal.status.value, o.terminal.filled_qty,
                )
            elif o.kind is OutcomeKind.UNSUBMITTED:
                self.trade_log.append_result(o.result_payload)
                log.warning("submission %s never reached broker; sealed",
                            o.intent_id)
            elif o.kind is OutcomeKind.RETRY:
                if o.incident is not None:
                    self.trade_log.append_incident(o.incident)
            elif o.kind is OutcomeKind.ESCALATE_HALT:
                self._state.set_halt(
                    o.halt_name, reason=o.halt_reason or o.intent_id, now=now
                )
                if o.incident is not None:
                    self.trade_log.append_incident(o.incident)
                self.state_store.save(self._state)
                halted = True
                log.error("recovery escalated to halt: %s (%s)",
                          o.halt_name, o.halt_reason)
        return halted

    # ---- market data helper (broker-only, no cache) ------------------

    # Calendar:trading ratio. US regular hours are ~6.5h/day × 5 days/week.
    # That is ~32.5 trading hours per ~168 calendar hours = 5.17×.
    #
    # _CAL_BUFFER=8 was insufficient on Monday-morning sessions because
    # the buffer's weekend gap consumed most of the lookback window. With
    # 5-min entry bars at min_bars=100, we need ~1.3 trading days back —
    # which on Monday open is Thursday last week. _CAL_BUFFER=8 only
    # reached Friday evening, yielding ~20 entry bars vs 100 required.
    # Bumped to 16 (~5.5 calendar days = Tue→Mon worst case = ~3 trading
    # days banked, comfortably above all three thresholds).
    _CAL_BUFFER = 16

    def _fetch_bar_frame(self, symbol: str, now: datetime) -> BarFrame | None:
        sp = self.config.strategy
        entry_tf = _TF_NAME_TO_ENUM[sp.entry_tf]
        confirm_tf = _TF_NAME_TO_ENUM[sp.confirm_tf]
        trend_tf = _TF_NAME_TO_ENUM[sp.trend_tf]
        try:
            # Window has to cover ``min_bars_*_tf`` *trading* bars; multiply
            # by _CAL_BUFFER to convert from trading minutes to calendar
            # minutes. The previous ``* 2`` factor under-fetched on every
            # timeframe — the strategy never reached signal evaluation.
            entry_start = now - timedelta(
                minutes=TF_NAME_TO_MINUTES[sp.entry_tf] * sp.min_bars_entry_tf * self._CAL_BUFFER
            )
            confirm_start = now - timedelta(
                minutes=TF_NAME_TO_MINUTES[sp.confirm_tf] * sp.min_bars_confirm_tf * self._CAL_BUFFER
            )
            trend_start = now - timedelta(
                minutes=TF_NAME_TO_MINUTES[sp.trend_tf] * sp.min_bars_trend_tf * self._CAL_BUFFER
            )
            entry_bars = self.broker.get_bars(symbol, entry_tf, start=entry_start, end=now)
            confirm_bars = self.broker.get_bars(symbol, confirm_tf, start=confirm_start, end=now)
            trend_bars = self.broker.get_bars(symbol, trend_tf, start=trend_start, end=now)
        except StrategyError:
            return None
        if not entry_bars or not confirm_bars or not trend_bars:
            return None
        return BarFrame(
            symbol=symbol,
            entry_bars=tuple(entry_bars),
            confirm_bars=tuple(confirm_bars),
            trend_bars=tuple(trend_bars),
        )

    # ---- recovery helper ---------------------------------------------

    def _rebuild_open_trade_from_history(
        self,
        symbol: str,
        broker_positions: list[Position],
    ) -> OpenTrade | None:
        """Try to reconstruct an :class:`OpenTrade` from the trade log.

        Walks the log newest-first looking for the most recent INTENT
        record for ``symbol`` whose result was ``submitting`` (i.e. an
        entry we kicked off) and whose matching RESULT shows a fill.
        If no match: return None and let the caller halt.
        """
        pos = next((p for p in broker_positions if p.symbol == symbol), None)
        if pos is None:
            return None
        # Walk the log and collect symbol-scoped INTENT/RESULT pairs.
        records = list(self.trade_log.read_all())
        latest_intent = None
        latest_result = None
        for rec in reversed(records):
            payload = rec.payload or {}
            if payload.get("symbol") != symbol:
                continue
            if rec.kind.value == "RESULT" and latest_result is None:
                latest_result = payload
            elif rec.kind.value == "INTENT" and latest_intent is None:
                latest_intent = payload
            if latest_intent and latest_result:
                break
        if latest_intent is None or latest_result is None:
            return None
        try:
            return OpenTrade(
                symbol=symbol,
                qty=int(pos.qty),
                entry_price=Decimal(str(latest_result.get("avg_fill_price", pos.avg_entry_price))),
                entry_ts=ensure_utc(datetime.fromisoformat(latest_intent.get("ts", datetime.now(timezone.utc).isoformat()))) if latest_intent.get("ts") else datetime.now(timezone.utc),
                stop_price=Decimal(str(latest_intent.get("stop_price", latest_intent.get("disaster_stop_price", 0)))),
                disaster_stop_price=Decimal(str(latest_intent.get("disaster_stop_price", 0))),
                target_price=Decimal(str(latest_intent.get("target_price", latest_intent.get("disaster_stop_price", 0)))),
                intent_id=str(latest_intent.get("intent_id", "recovered")),
                parent_client_order_id=str(latest_intent.get("client_order_id", "recovered")),
                protective_child_client_order_id=latest_result.get("protective_child_client_order_id"),
                protective_child_broker_id=latest_result.get("protective_child_broker_id"),
                last_seen_broker_qty=int(pos.qty),
            )
        except (KeyError, ValueError, TypeError):
            return None
