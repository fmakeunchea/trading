"""Phase 0 sub-task 0.0 — scaffold verification.

Minimal by design (scaffold only). The separability test is NOT a
convenience check: it is the first enforcement of the research safety
boundary — importing the research stack must never drag in the live
broker / orchestrator / deployment code.
"""
from __future__ import annotations

import subprocess
import sys


def test_research_packages_import() -> None:
    import research
    import research.data

    assert research.__all__ == []
    assert research.data.__all__ == []


def test_importing_research_does_not_pull_production_execution_stack() -> None:
    """Foundational separability boundary.

    Run in a FRESH interpreter (other tests in this session may already
    have imported strategy.* — in-process sys.modules would be unreliable).
    Importing ``research`` must not, as a side effect, import the live
    broker, the orchestrator, the run loop, or any autoflow/deployment code.
    """
    code = (
        "import sys, research, research.data;"
        "bad=[m for m in sys.modules if "
        "m=='strategy.broker' or m=='strategy.strategy' or "
        "m=='run_strategy' or m=='strategy.recovery' or "
        "m.startswith('autoflow')];"
        "print(','.join(sorted(bad)));"
        "sys.exit(1 if bad else 0)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=_repo_root(),
    )
    assert proc.returncode == 0, (
        "research import leaked production modules: "
        f"{proc.stdout.strip()!r} stderr={proc.stderr.strip()!r}"
    )


def _repo_root() -> str:
    from pathlib import Path
    return str(Path(__file__).resolve().parents[2])
