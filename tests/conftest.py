"""Shared test fixtures."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def frozen_now() -> datetime:
    return datetime(2026, 4, 23, 18, 30, 0, tzinfo=timezone.utc)


@pytest.fixture
def sample_decimal() -> Decimal:
    return Decimal("100.25")
