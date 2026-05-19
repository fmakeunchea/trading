# research/engine/ — Sim Spine (Phase 1)

Drives the **production `strategy.Strategy` class unchanged** over real
historical bars via a `SimulatedBroker` implementing the engine's
duck-typed broker surface. This is the keystone of the entire research
plan: signal/risk/exit/trailing logic is **never re-implemented here**.
The seam is already proven by `tests/test_strategy.py`'s `FakeBroker`;
the sim spine is that pattern grown into a production-grade simulator
with a historical bar feed (Phase 0) + deterministic clock + metrics
(Phase 3+).

## Two-tier separability

Unlike `research/data/*`, this package **deliberately couples to the
production engine** — that is the design:

| Allowed imports                 | Forbidden imports                  |
|---------------------------------|------------------------------------|
| `strategy.strategy.Strategy`    | `strategy.broker` (the live broker) |
| `strategy.dto.*`                | `run_strategy` (live driver loop)  |
| `strategy.state.*`              | `autoflow.*` (deployment)          |
| `strategy.config.*`             |                                    |
| `strategy.time_utils.*`         |                                    |
| `strategy.signal.*`             |                                    |
| `strategy.risk.*`               |                                    |
| `strategy.reconcile.*`          |                                    |
| `strategy.recovery.*`           |                                    |

The strict no-engine rule still applies to `research/data/*`. Enforced by
`tests/research/test_*.py` separability tests with two different
forbidden-import lists.

## Sub-tasks (pause-for-review after each)

- **1.0** — scaffold + two-tier separability matrix *(this sub-task)*
- **1.1** — `SimulatedBroker` (incl. inline bar/quote conversion).
  `spread_bps = 0` is a **Phase-1 limitation** loudly labelled in code,
  docstrings, and here. `spread_too_wide` gate will NOT fire → Phase 1
  produces **MORE entries than live** = permissive upper bound on entry
  count. **Not suitable for any decision.** Phase 2 introduces realistic
  spread + slippage.
- **1.2** — `BacktestDriver` (deterministic clock, real `Strategy`
  harness). **Hard gate:** `test_two_runs_produce_identical_trade_streams`.
- **1.3** — integration: known INTENT→RESULT→exit on synthetic bars.
- **1.4** — parity vs `FakeBroker` scenarios (no-logic-drift proof).

## Verdict-gate caveat (still in force)

Phase 0 sub-tasks **0.5 (CLI) and 0.6 (anti-leak hardening tests) are
deferred**. Any output from this package is **PROVISIONAL** until 0.6
ships and passes. Build, run, look; do not act.
