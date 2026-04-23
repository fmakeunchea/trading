"""Tests for strategy.config.

These tests use a fixture YAML and a fresh copy-modify pattern to exercise
every validation path. Config is frozen after load, so mutation attempts
must raise.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from strategy.config import (
    Config,
    Secret,
    load_config,
)
from strategy.errors import ConfigError


FIXTURE = Path(__file__).parent / "fixtures" / "sample_config.yaml"


TEST_ENV = {
    "TEST_ALPACA_API_KEY": "pk_test_key_12345",
    "TEST_ALPACA_API_SECRET": "sk_test_secret_SHOULD_NOT_LEAK",
}


def _write_variant(tmp_path: Path, overrides: dict, *, delete: list[str] | None = None) -> Path:
    data = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    for path, value in overrides.items():
        parts = path.split(".")
        d = data
        for p in parts[:-1]:
            d = d[p]
        d[parts[-1]] = value
    for path in delete or []:
        parts = path.split(".")
        d = data
        for p in parts[:-1]:
            d = d[p]
        d.pop(parts[-1], None)
    out = tmp_path / "cfg.yaml"
    out.write_text(yaml.safe_dump(data), encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_loads_paper_fixture() -> None:
    cfg = load_config(FIXTURE, env=TEST_ENV)
    assert isinstance(cfg, Config)
    assert cfg.mode == "paper"
    assert cfg.broker.data_feed == "iex"
    assert isinstance(cfg.broker.api_key, Secret)
    assert cfg.broker.api_key.reveal() == "pk_test_key_12345"
    # Throttle levels are sorted ascending.
    levels = cfg.sizing.drawdown_throttle_levels
    assert list(levels) == sorted(levels, key=lambda x: x[0])


def test_config_is_frozen() -> None:
    cfg = load_config(FIXTURE, env=TEST_ENV)
    with pytest.raises(Exception):
        cfg.mode = "live"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


def test_secret_never_leaks_in_repr() -> None:
    cfg = load_config(FIXTURE, env=TEST_ENV)
    r = repr(cfg)
    assert "sk_test_secret_SHOULD_NOT_LEAK" not in r
    assert "pk_test_key_12345" not in r
    assert "redacted" in repr(cfg.broker.api_key)


# ---------------------------------------------------------------------------
# Missing pieces
# ---------------------------------------------------------------------------


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "does_not_exist.yaml", env=TEST_ENV)


def test_malformed_yaml(tmp_path: Path) -> None:
    f = tmp_path / "bad.yaml"
    f.write_text("a: b\n  c: d: e\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(f, env=TEST_ENV)


def test_non_mapping_root(tmp_path: Path) -> None:
    f = tmp_path / "list.yaml"
    f.write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(f, env=TEST_ENV)


def test_missing_api_key(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {})
    env = dict(TEST_ENV)
    env.pop("TEST_ALPACA_API_KEY")
    with pytest.raises(ConfigError, match="API credentials missing"):
        load_config(p, env=env)


def test_missing_api_secret(tmp_path: Path) -> None:
    env = dict(TEST_ENV)
    env.pop("TEST_ALPACA_API_SECRET")
    with pytest.raises(ConfigError, match="API credentials missing"):
        load_config(FIXTURE, env=env)


def test_empty_api_key(tmp_path: Path) -> None:
    env = dict(TEST_ENV)
    env["TEST_ALPACA_API_KEY"] = ""
    with pytest.raises(ConfigError, match="missing"):
        load_config(FIXTURE, env=env)


# ---------------------------------------------------------------------------
# Mode validation
# ---------------------------------------------------------------------------


def test_invalid_mode(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"mode": "dry-run"})
    with pytest.raises(ConfigError, match="mode must be"):
        load_config(p, env=TEST_ENV)


# ---------------------------------------------------------------------------
# Live-mode enforcements
# ---------------------------------------------------------------------------


def test_live_requires_sip(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {
            "mode": "live",
            "broker.base_url": "https://api.alpaca.markets",
            "broker.data_feed": "iex",
        },
    )
    with pytest.raises(ConfigError, match="SIP"):
        load_config(p, env=TEST_ENV)


def test_live_allow_non_sip_override(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {
            "mode": "live",
            "broker.base_url": "https://api.alpaca.markets",
            "broker.data_feed": "iex",
            "live_allow_non_sip": True,
        },
    )
    cfg = load_config(p, env=TEST_ENV)
    assert cfg.is_live()
    assert cfg.live_allow_non_sip is True


def test_live_rejects_paper_base_url(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {
            "mode": "live",
            "broker.base_url": "https://paper-api.alpaca.markets",
            "broker.data_feed": "sip",
        },
    )
    with pytest.raises(ConfigError, match="paper"):
        load_config(p, env=TEST_ENV)


# ---------------------------------------------------------------------------
# Sizing invariants
# ---------------------------------------------------------------------------


def test_max_trade_notional_exceeds_total_exposure(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {
            "sizing.max_trade_notional": 20000,
            "sizing.max_total_exposure_pct": 0.5,  # 25k * 0.5 = 12.5k < 20k
        },
    )
    with pytest.raises(ConfigError, match="max_trade_notional"):
        load_config(p, env=TEST_ENV)


def test_symbol_exposure_exceeds_total(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {"sizing.max_symbol_exposure_pct": 0.6, "sizing.max_total_exposure_pct": 0.5},
    )
    with pytest.raises(ConfigError, match="max_symbol_exposure_pct"):
        load_config(p, env=TEST_ENV)


def test_halt_not_greater_than_throttle(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {"sizing.halt_drawdown_pct": 0.06},  # equal to max throttle threshold
    )
    with pytest.raises(ConfigError, match="halt_drawdown_pct"):
        load_config(p, env=TEST_ENV)


def test_halt_must_be_positive(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"sizing.halt_drawdown_pct": 0})
    with pytest.raises(ConfigError):
        load_config(p, env=TEST_ENV)


def test_throttle_mult_out_of_range(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path, {"sizing.drawdown_throttle_levels": {0.02: 1.5}}
    )
    with pytest.raises(ConfigError):
        load_config(p, env=TEST_ENV)


def test_min_greater_than_max_trade_notional(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {"sizing.min_trade_notional": 3000, "sizing.max_trade_notional": 2500},
    )
    with pytest.raises(ConfigError, match="min_trade_notional"):
        load_config(p, env=TEST_ENV)


def test_position_notional_pct_out_of_range(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"sizing.position_notional_pct": 1.5})
    with pytest.raises(ConfigError):
        load_config(p, env=TEST_ENV)


# ---------------------------------------------------------------------------
# Risk invariants
# ---------------------------------------------------------------------------


def test_daily_loss_cap_must_be_positive(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"risk.daily_loss_cap_pct": 0})
    with pytest.raises(ConfigError):
        load_config(p, env=TEST_ENV)


def test_min_edge_must_exceed_twice_spread(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {"risk.min_expected_edge_bps": 30, "risk.spread_filter_bps": 15},
    )
    with pytest.raises(ConfigError, match="min_expected_edge_bps"):
        load_config(p, env=TEST_ENV)


def test_session_times_must_order(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {"risk.session_start_utc": "20:00", "risk.session_end_utc": "19:00"},
    )
    with pytest.raises(ConfigError, match="session_start"):
        load_config(p, env=TEST_ENV)


# ---------------------------------------------------------------------------
# Execution invariants
# ---------------------------------------------------------------------------


def test_disaster_stop_must_exceed_atr_stop(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {"execution.disaster_stop_atr_mult": 1.0, "risk.atr_stop_mult": 1.2},
    )
    with pytest.raises(ConfigError, match="disaster_stop_atr_mult"):
        load_config(p, env=TEST_ENV)


def test_disaster_stop_upper_bound(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"execution.disaster_stop_atr_mult": 6.0})
    with pytest.raises(ConfigError, match="5.0"):
        load_config(p, env=TEST_ENV)


def test_flat_before_close_floor(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"execution.flat_before_close_minutes": 3})
    with pytest.raises(ConfigError, match="flat_before_close_minutes"):
        load_config(p, env=TEST_ENV)


def test_flat_before_close_exceeds_session(tmp_path: Path) -> None:
    # Session is 370 min; set flat to 400.
    p = _write_variant(tmp_path, {"execution.flat_before_close_minutes": 400})
    with pytest.raises(ConfigError):
        load_config(p, env=TEST_ENV)


# ---------------------------------------------------------------------------
# Broker invariants
# ---------------------------------------------------------------------------


def test_poll_interval_vs_timeout(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {"broker.order_poll_interval_s": 10.0, "broker.order_poll_timeout_s": 15.0},
    )
    with pytest.raises(ConfigError, match="order_poll_interval_s"):
        load_config(p, env=TEST_ENV)


def test_bad_data_feed(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"broker.data_feed": "otc"})
    with pytest.raises(ConfigError, match="data_feed"):
        load_config(p, env=TEST_ENV)


def test_negative_retries(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"broker.max_retries": -1})
    with pytest.raises(ConfigError):
        load_config(p, env=TEST_ENV)


def test_backoff_cap_less_than_base(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {"broker.retry_backoff_base_s": 2.0, "broker.retry_backoff_cap_s": 1.0},
    )
    with pytest.raises(ConfigError):
        load_config(p, env=TEST_ENV)


# ---------------------------------------------------------------------------
# Shipped configs parse (with dummy env)
# ---------------------------------------------------------------------------


def test_shipped_paper_config_parses() -> None:
    repo = Path(__file__).resolve().parent.parent
    env = {"ALPACA_API_KEY": "dummy", "ALPACA_API_SECRET": "dummy"}
    cfg = load_config(repo / "config" / "config.paper.yaml", env=env)
    assert cfg.mode == "paper"


def test_shipped_live_config_parses_with_live_creds() -> None:
    repo = Path(__file__).resolve().parent.parent
    env = {"ALPACA_API_KEY_LIVE": "dummy", "ALPACA_API_SECRET_LIVE": "dummy"}
    cfg = load_config(repo / "config" / "config.live.yaml", env=env)
    assert cfg.mode == "live"
    assert cfg.broker.data_feed == "sip"


# ---------------------------------------------------------------------------
# Decimal precision preserved
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Strategy section
# ---------------------------------------------------------------------------


def test_strategy_section_loads() -> None:
    cfg = load_config(FIXTURE, env=TEST_ENV)
    assert cfg.strategy.symbols == ("AAPL", "MSFT")
    assert cfg.strategy.entry_tf == "5Min"
    assert cfg.strategy.ema_fast < cfg.strategy.ema_slow
    assert cfg.strategy.atr_target_mult > cfg.risk.atr_stop_mult


def test_strategy_rejects_empty_symbols(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"strategy.symbols": []})
    with pytest.raises(ConfigError, match="symbols"):
        load_config(p, env=TEST_ENV)


def test_strategy_rejects_bad_timeframe(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"strategy.entry_tf": "3Min"})
    with pytest.raises(ConfigError, match="entry_tf"):
        load_config(p, env=TEST_ENV)


def test_strategy_rejects_ema_fast_ge_slow(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"strategy.ema_fast": 50, "strategy.ema_slow": 50})
    with pytest.raises(ConfigError, match="ema_fast"):
        load_config(p, env=TEST_ENV)


def test_strategy_target_must_exceed_stop(tmp_path: Path) -> None:
    p = _write_variant(
        tmp_path,
        {"strategy.atr_target_mult": 1.2, "risk.atr_stop_mult": 1.2},
    )
    with pytest.raises(ConfigError, match="atr_target_mult"):
        load_config(p, env=TEST_ENV)


def test_strategy_min_bars_enforced(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"strategy.min_bars_entry_tf": 10})
    with pytest.raises(ConfigError, match="min_bars_entry_tf"):
        load_config(p, env=TEST_ENV)


def test_strategy_symbol_universe_capped(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"strategy.symbols": [f"SYM{i}" for i in range(60)]})
    with pytest.raises(ConfigError, match="universe"):
        load_config(p, env=TEST_ENV)


def test_strategy_rejects_adx_period_too_small(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, {"strategy.adx_period": 1})
    with pytest.raises(ConfigError, match="adx_period"):
        load_config(p, env=TEST_ENV)


# ---------------------------------------------------------------------------
# Existing Decimal precision test
# ---------------------------------------------------------------------------


def test_decimal_precision_preserved() -> None:
    cfg = load_config(FIXTURE, env=TEST_ENV)
    assert isinstance(cfg.sizing.position_notional_pct, Decimal)
    assert cfg.sizing.position_notional_pct == Decimal("0.05")
    assert isinstance(cfg.execution.disaster_stop_atr_mult, Decimal)
