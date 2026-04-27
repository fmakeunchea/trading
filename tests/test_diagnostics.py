"""Per-symbol diagnostic emitter on Strategy.

Covers _emit_diagnostic in isolation:
- throttle dedupes within DIAG_THROTTLE_S per (symbol, decision_kind, reason)
- different keys are not throttled together
- paper mode appends a DIAGNOSTIC incident; live mode does not
- failures inside the trade_log append do not raise out of _emit_diagnostic
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from strategy.strategy import Strategy


UTC = timezone.utc


def _stub_strategy(tmp_path: Path, mode: str = "paper") -> Strategy:
    """Build a Strategy without going through __init__ — we only need the
    fields _emit_diagnostic touches, plus a real-ish trade_log mock."""
    strat = Strategy.__new__(Strategy)
    strat.config = SimpleNamespace(mode=mode)
    strat.trade_log = MagicMock()
    strat._diag_last_emit = {}
    return strat


def test_throttle_dedupes_within_window(tmp_path: Path) -> None:
    strat = _stub_strategy(tmp_path)
    t0 = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)

    strat._emit_diagnostic(t0, "SPY", "no_signal", "trend_not_up")
    strat._emit_diagnostic(
        t0 + timedelta(seconds=Strategy.DIAG_THROTTLE_S - 1),
        "SPY", "no_signal", "trend_not_up",
    )
    # Only the first one should have written to trade_log.
    assert strat.trade_log.append_incident.call_count == 1


def test_throttle_releases_after_window(tmp_path: Path) -> None:
    strat = _stub_strategy(tmp_path)
    t0 = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)

    strat._emit_diagnostic(t0, "SPY", "no_signal", "trend_not_up")
    strat._emit_diagnostic(
        t0 + timedelta(seconds=Strategy.DIAG_THROTTLE_S + 1),
        "SPY", "no_signal", "trend_not_up",
    )
    assert strat.trade_log.append_incident.call_count == 2


def test_throttle_independent_per_key(tmp_path: Path) -> None:
    strat = _stub_strategy(tmp_path)
    t0 = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)

    strat._emit_diagnostic(t0, "SPY", "no_signal", "trend_not_up")
    strat._emit_diagnostic(t0, "QQQ", "no_signal", "trend_not_up")           # different symbol
    strat._emit_diagnostic(t0, "SPY", "risk_denied", "below_min_edge")       # different decision_kind
    strat._emit_diagnostic(t0, "SPY", "no_signal", "adx_too_low")            # different reason

    assert strat.trade_log.append_incident.call_count == 4


def test_paper_mode_appends_diagnostic_incident(tmp_path: Path) -> None:
    strat = _stub_strategy(tmp_path, mode="paper")
    t0 = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)

    strat._emit_diagnostic(t0, "SPY", "risk_denied", "below_min_edge",
                           extra={"edge_bps": "30"})

    assert strat.trade_log.append_incident.call_count == 1
    payload = strat.trade_log.append_incident.call_args[0][0]
    assert payload["kind"] == "DIAGNOSTIC"
    assert payload["decision"] == "risk_denied"
    assert payload["symbol"] == "SPY"
    assert payload["reason"] == "below_min_edge"
    assert payload["extra"] == {"edge_bps": "30"}


def test_live_mode_does_not_append(tmp_path: Path) -> None:
    strat = _stub_strategy(tmp_path, mode="live")
    t0 = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)

    strat._emit_diagnostic(t0, "SPY", "no_signal", "trend_not_up")
    strat._emit_diagnostic(t0, "QQQ", "risk_denied", "spread_too_wide")

    # Logs still emit (verified via caplog if you want); audit log stays clean.
    strat.trade_log.append_incident.assert_not_called()


def test_trade_log_failure_does_not_raise(tmp_path: Path) -> None:
    strat = _stub_strategy(tmp_path, mode="paper")
    strat.trade_log.append_incident.side_effect = RuntimeError("disk full")
    t0 = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)

    # Should swallow the exception so trading code is never affected.
    strat._emit_diagnostic(t0, "SPY", "no_signal", "trend_not_up")
