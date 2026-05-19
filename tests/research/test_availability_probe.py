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
