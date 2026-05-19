"""Research stack — strategy edge validation (backtesting).

SEPARABILITY INVARIANT (foundational, do not violate):

* This package is fully separable from the production execution stack.
  Nothing here is imported by ``strategy/``, ``autoflow/`` or
  ``run_strategy.py``, and importing ``research`` must not, as a side
  effect, import the live broker / orchestrator / deployment code.
* The backtester reuses the PRODUCTION ``strategy.Strategy`` class
  unchanged, driven through a ``SimulatedBroker`` (later phase). Signal /
  risk / exit / trailing logic is never re-implemented here.
* Future-bar access must become impossible by construction (anti-leak
  accessor primitive — introduced in sub-task 0.3/0.6, mandatory).

Canonical design: ``docs/backtester-design-v1.md``.

Status: Phase 0 (data layer) — sub-task 0.0 scaffold. No data fetch, no
simulation, no Strategy integration yet.
"""

__all__: list[str] = []
