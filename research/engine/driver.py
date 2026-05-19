"""BacktestDriver — Phase 1 sub-task 1.2.

Drives the production ``strategy.Strategy`` class unchanged over real
historical bars via :class:`research.engine.sim_broker.SimulatedBroker`
and :class:`research.engine.sim_trade_log.SimulatedTradeLog`. The
deterministic clock advances at **entry-bar-close boundaries** within
the strategy's session window; non-trading days are skipped efficiently
via the XNYS calendar.

Hard determinism gate: two independent driver instances with identical
inputs produce **byte-identical** trade streams (intents/results/
incidents) AND equity series. Tested explicitly in 1.2's test suite.

Verdict-gate caveat (still in force): any RunResult from this driver
is PROVISIONAL until Phase-0 sub-task 0.6 (anti-leak hardening tests)
ships. Build, run, inspect; do not act.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from strategy.state import StateStore
from strategy.strategy import Strategy
from strategy.time_utils import TF_NAME_TO_MINUTES
from strategy.trade_log import LogRecord, RecordKind

from research.engine.sim_broker import SimulatedBroker
from research.engine.sim_trade_log import SimulatedTradeLog


@dataclass(frozen=True, slots=True)
class RunResult:
    """Deterministic snapshot of one backtest run."""
    intents:   tuple[LogRecord, ...]
    results:   tuple[LogRecord, ...]
    incidents: tuple[LogRecord, ...]
    equity:    tuple[tuple[datetime, Decimal], ...]
    steps:     int
    start:     datetime
    end:       datetime


def _align_to_boundary(ts: datetime, tf_min: int) -> datetime:
    """Smallest entry-bar-close boundary at-or-after ``ts``."""
    epoch_min = int(ts.replace(second=0, microsecond=0).timestamp() // 60)
    rem = epoch_min % tf_min
    aligned_epoch_min = epoch_min if rem == 0 and ts.second == 0 \
                                       and ts.microsecond == 0 \
        else epoch_min + (tf_min - rem)
    return datetime.fromtimestamp(aligned_epoch_min * 60, tz=timezone.utc)


def _is_utc(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.utcoffset() == timedelta(0)


class BacktestDriver:
    """Time-stepping harness around the production Strategy.

    Construction does NOT load config or fetch data — caller passes a
    fully-built Strategy + SimulatedBroker + SimulatedTradeLog (the
    driver only owns the clock + collection). The :func:`build` factory
    handles the production wiring (load_config + StateStore + Strategy)
    so callers usually go through that.
    """

    def __init__(
        self,
        *,
        strategy: Strategy,
        broker: SimulatedBroker,
        trade_log: SimulatedTradeLog,
        start: datetime,
        end: datetime,
        tick_interval_min: int,
    ) -> None:
        if not (_is_utc(start) and _is_utc(end)):
            raise ValueError("start/end must be UTC tz-aware")
        if end <= start:
            raise ValueError("end must be strictly after start")
        if tick_interval_min < 1:
            raise ValueError("tick_interval_min must be >= 1")
        self._strategy = strategy
        self._broker = broker
        self._log = trade_log
        self._start = start
        self._end = end
        self._tick_min = tick_interval_min
        self._equity: list[tuple[datetime, Decimal]] = []
        self._steps = 0

    # ---- factory --------------------------------------------------------

    @classmethod
    def build(
        cls,
        *,
        config_path: str | Path,
        bars: dict[tuple[str, int], Any],
        start: datetime,
        end: datetime,
        starting_cash: Decimal,
        state_dir: str | Path | None = None,
        env: dict[str, str] | None = None,
    ) -> "BacktestDriver":
        """Wire a full driver from a production-style config + pre-loaded bars.

        ``state_dir`` defaults to a tempdir (research runs are throwaway);
        pass an explicit dir to persist state across runs."""
        from strategy.config import load_config
        cfg = load_config(Path(config_path), env=env or {})
        sd = Path(state_dir) if state_dir else Path(tempfile.mkdtemp(prefix="bt-state-"))
        sd.mkdir(parents=True, exist_ok=True)
        state_store = StateStore(sd / "state.json", fsync=False)
        broker = SimulatedBroker(bars=bars, starting_cash=starting_cash, now=start)
        # SimulatedTradeLog's clock follows the broker's simulated now.
        tl = SimulatedTradeLog(clock=lambda: broker._require_now())
        strategy = Strategy(cfg, broker, state_store, tl)
        # Seed engine-side equity baselines to match starting cash.
        strategy.state.peak_equity = Decimal(starting_cash)
        strategy.state.last_reconciled_equity = Decimal(starting_cash)
        strategy.state.intraday_low_equity = Decimal(starting_cash)
        tick_interval_min = TF_NAME_TO_MINUTES[cfg.strategy.entry_tf]
        return cls(
            strategy=strategy, broker=broker, trade_log=tl,
            start=start, end=end, tick_interval_min=tick_interval_min,
        )

    # ---- run ------------------------------------------------------------

    def run(self) -> RunResult:
        """Advance the clock from start→end, collecting trades + equity.

        Loop logic (efficient, calendar-aware):
        for each trading day in [start.date(), end.date()]:
            tick = align(max(session_open, start), tick_interval)
            while tick <= min(session_end, end):
                broker.set_now(tick); strategy.tick(tick, ...)
                record equity; tick += tick_interval
        """
        from research.data import calendar as cal  # lazy (pmc dep)

        # One-shot recovery (no pending submissions in a fresh sim; this
        # exercises the production code path so any regression surfaces).
        self._strategy.recover(self._start)

        d = self._start.date()
        end_date = self._end.date()
        while d <= end_date:
            if cal.is_trading_day(d):
                bounds = cal.session_bounds(d)
                if bounds is not None:
                    session_open, session_close = bounds
                    tick = _align_to_boundary(
                        max(session_open, self._start), self._tick_min,
                    )
                    upper = min(session_close, self._end)
                    while tick <= upper:
                        self._broker.set_now(tick)
                        self._strategy.tick(tick, kill_switch_present=False)
                        snap = self._broker.get_account_snapshot()
                        self._equity.append((tick, snap.equity))
                        self._steps += 1
                        tick = tick + timedelta(minutes=self._tick_min)
            d = d + timedelta(days=1)

        return self._result()

    # ---- helpers --------------------------------------------------------

    def _result(self) -> RunResult:
        recs = self._log.records()
        intents   = tuple(r for r in recs if r.kind is RecordKind.INTENT)
        results   = tuple(r for r in recs if r.kind is RecordKind.RESULT)
        incidents = tuple(r for r in recs if r.kind is RecordKind.INCIDENT)
        return RunResult(
            intents=intents, results=results, incidents=incidents,
            equity=tuple(self._equity), steps=self._steps,
            start=self._start, end=self._end,
        )
