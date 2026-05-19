"""Offline guards for the data-availability probe.

No network here — we only assert the probe is importable, separable, and
*fails closed* (read-only) when credentials are absent. Real profiling runs
where Alpaca creds exist (VPS), by design.
"""
from __future__ import annotations

import pytest

from research.data import availability_probe as ap


def test_universe_is_v1_set() -> None:
    assert ap.V1_UNIVERSE == ["SPY", "QQQ", "AAPL", "MSFT"]


def test_fails_closed_without_credentials(monkeypatch) -> None:
    # No creds -> SystemExit BEFORE any network/client construction.
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    with pytest.raises(SystemExit):
        ap.main(["--years", "1"])


def test_module_has_no_production_imports() -> None:
    # Separability: the probe must not import the production engine.
    import inspect
    src = inspect.getsource(ap)
    for forbidden in ("strategy.broker", "strategy.strategy",
                      "strategy.recovery", "run_strategy", "autoflow"):
        assert forbidden not in src


def test_half_day_classifier_recognises_known_early_close() -> None:
    """Gated by importorskip — only runs in the research venv. The XNYS
    early-close on 2024-07-03 (day before July 4) is a textbook half-day
    and must be classified as such, NOT as an unexplained data gap."""
    pytest.importorskip("pandas_market_calendars")
    from datetime import datetime, timezone

    half = ap._xnys_half_days(
        datetime(2024, 7, 1, tzinfo=timezone.utc),
        datetime(2024, 7, 5, tzinfo=timezone.utc),
    )
    from datetime import date
    assert date(2024, 7, 3) in half      # half-day before July 4
    assert date(2024, 7, 2) not in half  # normal session
    assert date(2024, 7, 4) not in half  # full holiday (not a half-day; closed)
