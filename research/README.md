# research/ — Strategy Edge Validation (Backtester)

Separable research stack. **Not** part of the production execution path.

- **Canonical design:** [`docs/backtester-design-v1.md`](../docs/backtester-design-v1.md) — the authoritative blueprint. Conform to it or amend it explicitly.
- **Purpose:** determine whether the strategy has a real edge after realistic execution costs (out-of-sample, cost-stressed, regime-segmented expectancy with a confidence interval).

## Core invariants (non-negotiable)

1. **Production engine reuse.** The backtester drives the real
   `strategy.Strategy` class unchanged through a `SimulatedBroker`
   implementing the exact duck-typed broker surface. Signal / risk / exit /
   trailing logic is **never** re-implemented here. (The seam is already
   proven by `tests/test_strategy.py`'s `FakeBroker`.)
2. **Separability.** Zero edits to `strategy/`, `autoflow/`,
   `run_strategy.py`, or production `requirements.txt`. Importing `research`
   must not pull in the live broker / orchestrator / deployment code.
3. **Anti-leak by construction.** Future-bar access must be *impossible*,
   not merely discouraged. The anti-leak data accessor (sub-task 0.3/0.6) is
   a foundational research **safety boundary**, treated with the same rigor
   as the production durability work — not a convenience utility.
4. **No tuning in V1.** The first study measures the *current frozen*
   strategy parameters only.

## Status

Phase 0 (data layer), sub-task **0.0 scaffold**. No data fetch, no calendar,
no resample, no cache, no simulation, no Strategy integration yet.

Build order (pause-for-review gate after each): `0.0 → 0.1/0.2 → 0.3 → 0.4 →
0.5 → 0.6`. Phases: 0 data → 1 sim spine → 2 execution realism → 3
metrics/report → 4 experiment harness → run.

## Dependencies

Research-only extras are declared in [`../requirements-research.txt`](../requirements-research.txt),
deliberately separate from production `requirements.txt`. Not installed in
sub-task 0.0 (scaffold only).
