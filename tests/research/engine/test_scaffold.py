"""Phase 1 sub-task 1.0 — engine scaffold verification.

Pins the **two-tier separability matrix**:

* ``research/data/*`` already enforces the strict no-engine rule
  (existing tests under ``tests/research/test_*.py``).
* ``research/engine/*`` enforces a relaxed rule: the live broker, the
  run loop, and deployment code remain forbidden, but the rest of the
  engine (Strategy, DTOs, state, config, time_utils, signal, risk,
  reconcile, recovery) is allowed — that is the keystone reuse.

A regression of either tier silently corrupts the research stack
(strict tier drift → data could touch live trading; engine tier
drift → research importing the live broker is the same class of bug
the lost-fill fix was about). Both rules are mechanically enforced.
"""
from __future__ import annotations

import subprocess
import sys


# --- engine subpackage imports ---------------------------------------------

def test_engine_packages_import() -> None:
    import research.engine
    assert research.engine.__all__ == []


# --- two-tier separability matrix ------------------------------------------

# Strict tier (data layer) — engine fundamentals forbidden.
_FORBIDDEN_DATA = (
    "strategy.broker",
    "strategy.strategy",
    "strategy.recovery",
    "strategy.dto",
    "strategy.state",
    "strategy.signal",
    "strategy.risk",
    "strategy.time_utils",
    "strategy.config",
    "strategy.reconcile",
    "run_strategy",
    "autoflow",
)

# Engine tier — only the live broker, run loop, and deployment are forbidden.
# Strategy + dto + state + signal + risk + … are explicitly allowed because
# the sim spine REUSES them by design.
_FORBIDDEN_ENGINE = (
    "strategy.broker",
    "run_strategy",
    "autoflow",
)


def _imports_after(target: str, forbidden: tuple[str, ...]) -> list[str]:
    """Run ``import target`` in a FRESH interpreter; return forbidden modules
    found in ``sys.modules`` afterwards. Fresh subprocess required because
    in-process ``sys.modules`` is polluted by other tests."""
    code = (
        f"import sys; import {target};"
        f"bad=[m for m in sys.modules if m in {forbidden!r} "
        f"or any(m.startswith(f+'.') for f in {forbidden!r})];"
        f"print('|'.join(sorted(bad)));"
        f"sys.exit(0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=_repo_root(),
    )
    assert proc.returncode == 0, (
        f"subprocess import probe failed: {proc.stderr.strip()!r}"
    )
    out = proc.stdout.strip()
    return [m for m in out.split("|") if m] if out else []


def test_engine_tier_only_forbids_live_broker_run_loop_and_deployment() -> None:
    """research/engine MAY import strategy.Strategy + DTOs (that's the
    keystone reuse), but MUST NOT import the live broker / run loop /
    autoflow. Validated empirically in a fresh interpreter."""
    leaked = _imports_after("research.engine", _FORBIDDEN_ENGINE)
    assert leaked == [], (
        f"research.engine leaked production deploy/live-broker modules: {leaked}"
    )


def test_data_tier_still_forbids_all_engine_fundamentals() -> None:
    """Regression for the strict tier: research.data must NOT pull in
    strategy.strategy / strategy.dto / strategy.broker / etc. as a side
    effect of import. (Same property the existing per-data-module tests
    pin via source inspection; this is the mechanical sys.modules check.)"""
    leaked = _imports_after("research.data", _FORBIDDEN_DATA)
    assert leaked == [], (
        f"research.data leaked engine modules at import time: {leaked}"
    )


def _repo_root() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parents[3])
