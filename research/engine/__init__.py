"""Research sim spine — drives the PRODUCTION Strategy class unchanged.

SEPARABILITY (two-tier rule):

* ``research/data/*``  — strict: must NOT import the production execution
  stack (broker, strategy orchestrator, recovery, run loop, autoflow).
  Enforced by tests in ``tests/research/test_*.py``.
* ``research/engine/*`` — DELIBERATELY couples to the production engine:
  ``strategy.strategy.Strategy``, ``strategy.dto``, ``strategy.state``,
  ``strategy.config``, ``strategy.time_utils``. This is the keystone
  reuse — it is what guarantees the backtester exercises the SAME code
  that trades live, eliminating logic drift. ``run_strategy`` and
  ``autoflow`` (deployment / driver loop) remain forbidden — the
  ``BacktestDriver`` replaces them, not extends them.

This package contains no Phase-1 logic until sub-task 1.1.
"""

__all__: list[str] = []
