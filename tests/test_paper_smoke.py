"""Unit tests for the non-order-placing parts of scripts.paper_smoke.

The live-paper smoke test itself is **not** run by pytest — these tests
cover env loading, the paper-URL guardrail, and argument parsing. No
network calls. No orders placed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import scripts.paper_smoke as ps


# ---------------------------------------------------------------------------
# Env file loading
# ---------------------------------------------------------------------------


def test_load_env_returns_os_env_when_no_file(monkeypatch) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "env-only-key")
    env = ps.load_env(Path("/does/not/exist.env"))
    assert env["ALPACA_API_KEY"] == "env-only-key"


def test_load_env_merges_file_without_overriding_os(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ALPACA_API_KEY", "os-key")
    f = tmp_path / "p.env"
    f.write_text(
        "# a comment\n\n"
        "ALPACA_API_KEY=file-key\n"
        "ALPACA_API_SECRET=\"file-secret\"\n"
        "BOGUS_LINE_WITHOUT_EQUALS\n",
        encoding="utf-8",
    )
    env = ps.load_env(f)
    # OS var wins:
    assert env["ALPACA_API_KEY"] == "os-key"
    # File fills in what OS doesn't:
    assert env["ALPACA_API_SECRET"] == "file-secret"


def test_load_env_handles_missing_env_file() -> None:
    env = ps.load_env(None)
    assert isinstance(env, dict)


# ---------------------------------------------------------------------------
# Paper-mode guardrail
# ---------------------------------------------------------------------------


def test_require_paper_mode_accepts_paper_url() -> None:
    url = ps.require_paper_mode({"ALPACA_BASE_URL": "https://paper-api.alpaca.markets"})
    assert "paper" in url


def test_require_paper_mode_default_is_paper() -> None:
    url = ps.require_paper_mode({})
    assert url == ps.PAPER_BASE_URL


def test_require_paper_mode_rejects_live_url() -> None:
    with pytest.raises(SystemExit):
        ps.require_paper_mode({"ALPACA_BASE_URL": "https://api.alpaca.markets"})


# ---------------------------------------------------------------------------
# build_broker refuses without credentials
# ---------------------------------------------------------------------------


def test_build_broker_requires_credentials() -> None:
    with pytest.raises(SystemExit):
        ps.build_broker({"ALPACA_BASE_URL": "https://paper-api.alpaca.markets"})


# ---------------------------------------------------------------------------
# main() refuses to place orders without --yes
# ---------------------------------------------------------------------------


def test_main_refuses_without_yes(monkeypatch, capsys) -> None:
    # Ensure no real credentials are discovered so even if we accidentally
    # proceeded, the broker build would fail.
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    rc = ps.main(["--env-file", "/does/not/exist.env"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "--yes" in err


def test_main_refuses_when_credentials_missing_even_with_yes(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    monkeypatch.delenv("ALPACA_API_KEY_PAPER", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_PAPER", raising=False)
    rc = ps.main(["--yes", "--env-file", "/does/not/exist.env"])
    assert rc == 2


# ---------------------------------------------------------------------------
# pytest discovery: the live smoke file must not be auto-collected
# ---------------------------------------------------------------------------


def test_paper_smoke_not_collected_by_pytest() -> None:
    """The live smoke script must sit outside ``tests/`` so pytest's
    ``testpaths`` setting never picks it up."""
    script = Path(ps.__file__)
    assert script.parent.name == "scripts"
    assert not script.name.startswith("test_")
