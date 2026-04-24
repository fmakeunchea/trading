"""Test fixtures: isolate the engine var dir per test, stub docker calls,
and use an in-memory-ish sqlite or skip DB-bound tests if Postgres is absent.

For MVP we stub the DB session at the FastAPI dependency boundary so tests
don't need a live Postgres. Tests that exercise SQL stay out of this file.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# Set env BEFORE importing app modules so settings picks them up.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://stub:stub@localhost/stub")


@pytest.fixture()
def var_dir(tmp_path: Path) -> Path:
    """Isolated engine var/ directory for each test."""
    d = tmp_path / "var"
    d.mkdir()
    return d


@pytest.fixture()
def client(var_dir: Path, monkeypatch):
    """TestClient with engine_var_dir pointed at a temp dir, DB stubbed,
    docker calls stubbed, and the background sync worker disabled."""
    from app import config as app_config
    monkeypatch.setattr(app_config.settings, "engine_var_dir", var_dir)

    # Stub the lifespan-spawned sync worker so tests don't poll a real DB.
    from app import incident_sync
    async def _noop_forever():
        return
    monkeypatch.setattr(incident_sync, "run_forever", _noop_forever)

    from app.main import app
    from app.db import get_db

    db = MagicMock()
    db.execute.return_value.mappings.return_value.first.return_value = None
    db.execute.return_value.mappings.return_value.all.return_value = []
    db.execute.return_value.scalar_one.return_value = 0
    db.execute.return_value.scalar_one_or_none.return_value = None
    app.dependency_overrides[get_db] = lambda: db

    with TestClient(app) as c:
        c.db_mock = db  # expose for assertions
        yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def stub_docker(monkeypatch):
    """Replace bot_control._docker with a recording stub."""
    from app import bot_control

    calls: list[list[str]] = []

    def fake_docker(*args: str):
        calls.append(list(args))
        if args and args[0] == "inspect":
            return 0, "false", ""
        return 0, "autoflow-trading-bot-1", ""

    monkeypatch.setattr(bot_control, "_docker", fake_docker)
    return calls
