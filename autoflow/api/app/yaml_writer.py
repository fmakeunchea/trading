"""Render strategy DB rows back into the engine's YAML config.

The engine's YAML has many fields the SaaS doesn't model (broker timeouts,
sizing curves, tick interval, etc). Don't clobber them — load the existing
file, patch the handful of keys the UI controls, atomically rename.

Atomic write: write to <path>.next, fsync, rename → <path>. The engine reads
the file fresh on startup, so the only risk is the engine getting a
half-written file mid-read; rename() avoids that.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from .config import settings


# Fields on the strategies row that map into YAML.
# (db_column, yaml_path_dot_separated)
FIELD_MAP: list[tuple[str, str]] = [
    ("mode", "mode"),
    ("symbols", "strategy.symbols"),
    ("daily_loss_cap_pct", "risk.daily_loss_cap_pct"),
    ("max_concurrent_positions", "risk.max_concurrent_positions"),
    ("position_notional_pct", "sizing.position_notional_pct"),
]


def _set_path(d: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value


def render_yaml(strategy: dict, target: Path | None = None) -> Path:
    """Write strategy fields into the engine YAML. Returns the path written."""
    target = target or settings.engine_config_path
    if not target.exists():
        raise FileNotFoundError(f"engine config not found: {target}")
    with target.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    for col, path in FIELD_MAP:
        if col in strategy and strategy[col] is not None:
            value = strategy[col]
            # YAML wants a list, not a Postgres array repr
            if col == "symbols":
                value = list(value)
            # Convert Decimal / numeric to native float
            elif col in ("daily_loss_cap_pct", "position_notional_pct"):
                value = float(value)
            elif col == "max_concurrent_positions":
                value = int(value)
            _set_path(cfg, path, value)

    tmp = target.with_suffix(target.suffix + ".next")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return target
