# Backtester Architecture v1 — Research-System Design Blueprint

**Status:** design approved 2026-05-19 · implementation NOT started ·
Phase 0 gated behind final supervised-rollout review + explicit approval.

**Scope:** determine whether the strategy has a real edge after realistic
execution costs. Design only. Strategy parameters frozen. Production engine
untouched.

> **Core invariant:** the backtester reuses the **production `Strategy`
> class unchanged**, driven through a `SimulatedBroker` that implements the
> exact duck-typed broker surface. We never re-implement signal/risk/exit
> logic. `tests/test_strategy.py`'s `FakeBroker` already proves this seam
> holds — the backtester is that pattern grown into a production-grade
> simulator + historical feed + deterministic clock + metrics.

---

## 1. Core architecture

New top-level package `research/`, fully separable from `strategy/` and
`autoflow/`.

- **Data layer** (`research/data/`): Alpaca historical fetch → calendar-aware
  resample → parquet cache → typed `Bar` series. Offline, deterministic.
- **Event clock / replay driver** (`research/engine/driver.py`): deterministic
  discrete clock advancing over entry-bar-close boundaries; calls
  `Strategy.recover()` once then `Strategy.tick(now)` per step. No wall-clock.
  Seeded RNG injected.
- **SimulatedBroker** (`research/engine/sim_broker.py`): implements
  `get_bars`, `get_latest_quote`, `get_account_snapshot`, `get_positions`,
  `get_open_orders`, `submit_entry_with_protection`, `poll_terminal`,
  `flatten_symbol`, `resolve_by_coid`. Deterministic OHLC matching engine;
  single source of truth for account equity/positions/orders.
- **Signal evaluation loop**: *not authored here* — it is `Strategy.tick()`
  unchanged. The driver only supplies time and (via SimulatedBroker) data.
- **Portfolio/accounting**: inside SimulatedBroker, so real `evaluate_entry`
  risk sizing (reads `last_reconciled_equity`, drawdown, throttle) behaves
  identically to live. Engine-side state is the real `state.py` with an
  in-memory `StateStore`.
- **Metrics/reporting** (`research/report/`): consumes a recorded
  trade/equity stream; produces metrics + HTML; fully decoupled.

Data flow per step: `driver.advance() → now → Strategy.tick(now) → Strategy
calls SimulatedBroker → SimulatedBroker matches fills vs historical OHLC and
mutates sim portfolio → Strategy records via in-memory TradeLog → driver
collects the trade/equity record.`

## 2. Reuse boundaries

**Reused UNCHANGED (import + dependency injection):** `strategy.signal`
(`evaluate_signal`, `BarFrame`); `strategy.risk` (`evaluate_entry`,
`EntryRequest`); `strategy.strategy.Strategy` (whole orchestrator incl.
`_try_enter`, `_determine_exit` and any trailing logic, `_manage_positions`,
`_finalize_entry`, `recover`, `_recover_pending_submissions`);
`strategy.state`; `strategy.time_utils` (`SessionClock`, `ensure_utc`,
`today_utc`, `TF_NAME_TO_MINUTES`, `is_stale`); `strategy.dto`;
`strategy.config`; `strategy.reconcile` (semantics).

**Abstracted / replaced (research-only):** `AlpacaBroker` →
`SimulatedBroker`; `run_strategy.py` loop / cron / deployment →
`BacktestDriver`; `TradeLog` → in-memory recorder (same interface, list-backed,
optional JSONL export); recovery/remediation → present but dormant
(deterministic `poll_terminal` ⇒ no `submitted_unconfirmed`; correct — V1 is
not testing recovery); websocket / kill-switch / observability DB → unused.

**Rule:** no file under `strategy/` or `autoflow/` is edited. If a needed
rule turns out welded to orchestrator I/O (it isn't — `FakeBroker` proves the
seam), revisit before any extraction, never silently.

## 3. Historical data strategy

- **Provider V1:** Alpaca `StockHistoricalDataClient` — same source as live
  to minimize sim-vs-live skew. Polygon/Databento = future fidelity, non-goal.
- **Feed:** IEX (paper parity). SIP is a documented future fidelity option;
  IEX partial-tape skew is a stated V1 limitation.
- **Granularity:** fetch native 1-minute bars; resample to 5m/15m/1h using
  Alpaca's left-labeled `ts=open` convention (bar "known" at `ts + tf`).
  Validate resampled 5m vs native Alpaca 5m before trusting it.
- **Survivorship bias:** fixed large-cap liquid universe (SPY/QQQ/AAPL/MSFT)
  ⇒ negligible V1; documented — do not generalize to a dynamic universe
  without point-in-time constituents.
- **Market hours:** regular session only (`SessionClock`); exchange calendar
  (`pandas_market_calendars`) for holidays/half-days; drop extended-hours.
- **Corporate actions:** `adjustment='all'` for series self-consistency;
  dividend total-return vs price subtlety documented as minor fidelity limit.
- **Storage:** Parquet partitioned `symbol/timeframe/year`.
- **Caching:** content-addressed by `(symbol, tf, start, end, adjustment,
  feed, provider)`; atomic writes; manifest records provider response hash.

## 4. Replay semantics

- **Clock cadence:** step at entry-bar-close boundaries (sub-bar live ticks
  cannot change the signal). Documented; config knob for future sub-bar
  strategies.
- **Bar-close decision timing (anti-lookahead rule):** at `T = B.ts + tf`,
  `get_bars` returns only bars with `close_ts ≤ T`; hard assertion otherwise.
  Reproduces the live `_fetch_bar_frame` + freshness gate exactly.
- **Quote synthesis:** no historical quotes V1. `get_latest_quote` →
  `mid ≈ close@T` + a modeled spread (per-symbol fixed bps or
  range/ATR-scaled) so the real `spread_too_wide` gate and `_determine_exit`
  run unmodified. Spread model is first-class and sensitivity-tested.
- **Entry fill (DAY limit + OTO disaster child):** buy limit fills only if a
  subsequent bar's `low ≤ L`; fill = `min(L, bar.open)` on gap-down else `L`;
  plus slippage. Unfilled by DAY/session end ⇒ `EXPIRED`. OTO child activates
  on parent fill.
- **Exits:** each step run real `_determine_exit` (mid = close@T). Intrabar
  scan: `low ≤ stop` ⇒ primary stop; `low ≤ disaster_stop` ⇒ backstop;
  `high ≥ target` ⇒ target; real time-stop.
- **OHLC ambiguity:** a bar spanning both stop and target ⇒ **assume stop
  first (pessimistic)**. Biases results down; documented; default pessimistic.
- **Trailing stops:** whatever `strategy.py` implements — reused, never
  re-modeled.
- **Partial fills:** V1 full-or-none (deterministic); documented conservative
  simplification; partial modeling is non-goal.
- **Costs:** Alpaca equities ≈ $0 commission; optional regulatory micro-fees;
  dominant cost = spread + slippage, explicit parameters.
- **Rejected orders:** `spread_too_wide` via synthesized spread (real gate).
  Other liquidity/halt rejections non-goal V1.
- **Latency:** configurable decision→effective latency, default 0.

## 5. Research safety

- **Lookahead:** single chokepoint — sim data accessors clip to `≤ now` with
  a raising invariant + a future-bar-injection test.
- **Data leakage:** chronological splits only; warm-up uses only
  at-or-before data; no statistic computed on test data.
- **Parameter leakage / overfitting:** pre-register metric + threshold before
  any sweep; cap sweep dimensionality; report the full sweep surface (not
  argmax); require robustness across symbols and regimes; prefer plateaus;
  penalize parameter count.
- **Regime over-specialization:** tag each day/trade by regime; edge must
  survive ≥2 regimes.
- **Nondeterminism:** clock is the only time source; seeded RNG; byte-identical
  reruns enforced by a determinism test.

## 6. Experiment workflow

- **Splits:** strictly chronological — In-Sample → Validation → Test/holdout
  (most recent, locked, observed once). No random splitting.
- **Walk-forward:** anchored/rolling; optimize window N, evaluate untouched
  N+1, roll; headline = concatenated out-of-sample equity.
- **Out-of-sample discipline:** holdout is one-shot.
- **Parameter sweeps:** coarse grids, full logging, stability heatmaps,
  pre-committed iteration stop rule.
- **Regime segmentation:** per-regime expectancy tables.
- **Benchmarks (must beat all):** costs/zero with bootstrap CI excluding 0;
  random-entry null on the same risk/exit machinery; risk-adjusted
  buy-and-hold SPY; always-flat.

## 7. Metrics (beyond raw P&L)

- **Per-trade:** expectancy ($ and R), win rate, payoff ratio, win/loss
  asymmetry, MAE/MFE, duration distribution, exit-reason breakdown.
- **Portfolio:** CAGR, Sharpe, Sortino, Calmar, max drawdown + duration,
  profit factor, exposure, worst-day/tail.
- **Robustness (decisive):** per-regime & per-symbol expectancy;
  slippage/spread sensitivity curve; parameter-stability heatmaps; rolling
  Sharpe stability; **bootstrap 95% CI on expectancy vs zero, out-of-sample,
  after costs** — this is the verdict metric.

## 8. Deliverables (Milestone 1)

- **CLI:** `python -m research.backtest --symbols … --start … --end …
  --config <frozen> --costs <model> --seed N`, deterministic.
- **Artifacts:** parquet bar cache; `trades.csv`; `equity_curve.parquet`;
  `metrics.json`; HTML report (equity/drawdown, per-regime table,
  slippage-sensitivity, histograms, benchmark overlays); deterministic replay
  log (trade-log JSON shape); reproducibility manifest (code SHA, config
  hash, data hash, cost params, seed).
- Notebook integration secondary.

## 9. Incremental build plan

| Phase | Scope | Blast radius | Deps | Validation gate | Est. |
|---|---|---|---|---|---|
| 0 Data | Alpaca fetch + calendar + resample + parquet cache | `research/` only | Alpaca client, market calendar | resampled 5m == native 5m within tol; no gaps; deterministic cache | 2–3 d |
| 1 Sim spine | `SimulatedBroker` (perfect fills, no cost) + driver on real `Strategy` | `research/` only | P0 | reproduces known INTENT→RESULT→exit; parity vs `FakeBroker` scenarios | 3–4 d |
| 2 Execution realism | spread synth, slippage, pessimistic OHLC ambiguity, DAY-limit fill/expire, session flatten | `research/` only | P1 | cost-monotonicity; pessimism checks; byte-identical reruns | 3–4 d |
| 3 Metrics/report | metrics + HTML + manifest | `research/` only | P2 | hand-verified metrics on tiny dataset; deterministic | 2–3 d |
| 4 Experiment harness | walk-forward, regime tagging, benchmarks, sensitivity sweep | `research/` only | P3 | train/test isolation asserts; null/benchmark wired | 3–5 d |
| → Run | edge study on frozen params | — | P4 | the verdict | — |

Each phase pauses for review (same discipline as the durability project).

## 10. Explicit non-goals (V1)

Tick/quote/L2 microstructure · partial-fill & queue modeling · ML /
auto-optimization / parameter auto-tuning · large-universe portfolio
optimization · multi-broker / alternative providers · distributed compute ·
live-paper or real-money integration · options/shorting beyond current
strategy · corporate-action edge cases beyond split/div adjustment · regime
*prediction* (only segmentation) · **any strategy tuning** (V1 measures the
current frozen parameters only).

## Constraint compliance

No strategy tuning · no live trading · no premature optimization (pessimistic
simple cost model first) · no framework rewrite (instantiate the real
`Strategy`) · production engine integrity preserved (zero edits under
`strategy/` / `autoflow/`) · research stack fully separable (`research/`
package, own CLI, own deps).

**Biggest risk** is not engineering — it is the temptation to tune when
early results look bad. The design forces the verdict from out-of-sample,
cost-stressed, regime-segmented expectancy with a confidence interval,
*before* any parameter is touched.
