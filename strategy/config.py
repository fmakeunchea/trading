"""Typed configuration loader and validator.

Design notes:

* Config is frozen after load. No runtime mutation is permitted — a
  config change is a restart event, which also forces a reconciliation
  pass, which is exactly what we want.
* Secrets come from the environment only. YAML never holds credentials.
* The secret fields are wrapped in :class:`Secret` so ``repr`` cannot
  leak them; ``str(Secret)`` returns a redacted placeholder.
* Live-mode is fail-closed for every invariant the "real money safety
  first" rule cares about: SIP required, no fractional, no extended
  hours, positive loss caps, etc.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import yaml

from strategy.errors import ConfigError
from strategy.time_utils import parse_hhmm_utc


# ---------------------------------------------------------------------------
# Secret wrapper
# ---------------------------------------------------------------------------


class Secret:
    """String wrapper that never prints its value.

    Use :meth:`reveal` at the exact call site that needs the secret (inside
    ``broker.py``). ``repr`` / ``str`` both return a redacted placeholder.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("Secret expects str")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "Secret(***redacted***)"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "***redacted***"

    def __eq__(self, other: object) -> bool:
        # Equality is by *content* — needed for round-trip tests that
        # load the same config twice.
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:  # pragma: no cover - trivial
        return hash(self._value)


# ---------------------------------------------------------------------------
# Dataclasses (all frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    base_url: str
    data_feed: str                 # "sip" or "iex"
    api_key: Secret
    api_secret: Secret
    request_timeout_s: float
    max_retries: int
    retry_backoff_base_s: float
    retry_backoff_cap_s: float
    order_poll_interval_s: float
    order_poll_timeout_s: float


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    limit_slippage_bps: Decimal
    disaster_stop_atr_mult: Decimal
    flat_before_close_minutes: int


@dataclass(frozen=True, slots=True)
class SizingConfig:
    enable_compounding: bool
    starting_equity: Decimal
    position_notional_pct: Decimal
    min_trade_notional: Decimal
    max_trade_notional: Decimal
    max_total_exposure_pct: Decimal
    max_symbol_exposure_pct: Decimal
    drawdown_throttle_levels: tuple[tuple[Decimal, Decimal], ...]
    halt_drawdown_pct: Decimal


@dataclass(frozen=True, slots=True)
class RiskConfig:
    daily_loss_cap_pct: Decimal
    max_concurrent_positions: int
    spread_filter_bps: Decimal
    # Quote-staleness threshold (seconds). Used by the exit-management
    # path that reads live quotes; not used for bar staleness.
    stale_data_max_age_s: int
    # Bar-staleness *grace* window (seconds) added on top of the entry-
    # timeframe period. Effective bar threshold = entry_tf_minutes * 60
    # + stale_bar_grace_s. Kept as a grace rather than an absolute so it
    # auto-adjusts if entry_tf changes.
    stale_bar_grace_s: int
    session_start_utc: time
    session_end_utc: time
    loss_cooldown_s: int
    min_expected_edge_bps: Decimal
    block_first_minutes: int
    block_last_minutes: int
    atr_stop_mult: Decimal


@dataclass(frozen=True, slots=True)
class PersistenceConfig:
    state_path: Path
    trade_log_path: Path
    state_fsync: bool


@dataclass(frozen=True, slots=True)
class ProcessConfig:
    lock_file: Path
    heartbeat_path: Path
    kill_switch_path: Path


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Parameters consumed by :mod:`strategy.signal` and
    :mod:`strategy.strategy`.

    Deliberately narrow: only the knobs that control entry/exit
    generation or orchestration cadence. Risk gates live in
    :class:`RiskConfig`; sizing lives in :class:`SizingConfig`.
    """

    symbols: tuple[str, ...]
    entry_tf: str                 # "5Min"
    confirm_tf: str               # "15Min"
    trend_tf: str                 # "1Hour"
    ema_fast: int                 # entry-TF EMA fast (e.g. 20)
    ema_slow: int                 # entry-TF EMA slow (e.g. 50)
    trend_ema: int                # trend-TF EMA (e.g. 200)
    adx_period: int
    adx_min: Decimal
    atr_period: int
    atr_target_mult: Decimal
    breakout_lookback: int
    min_bars_entry_tf: int        # bars we need before trusting a signal
    min_bars_confirm_tf: int
    min_bars_trend_tf: int
    reentry_cooldown_bars: int
    max_hold_bars: int            # bot-side time-stop on entry TF
    tick_interval_s: float        # orchestrator sleep between ticks
    # --- trailing stop (opt-in, defaults preserve baseline behaviour) ----
    # When enabled, the bot-side stop ratchets up as the trade moves
    # favorably. Two-phase: (1) move stop to entry (breakeven) after the
    # trade has gained `trailing_breakeven_at_atr × ATR`; (2) once the
    # trade has gained `trailing_activation_at_atr × ATR`, start trailing
    # the stop at `trailing_distance_atr × ATR` below the highest mid
    # seen since entry. Ratchet-only — the stop never moves down.
    trailing_stop_enabled: bool = False
    trailing_breakeven_at_atr: Decimal = Decimal("0.5")
    trailing_activation_at_atr: Decimal = Decimal("1.5")
    trailing_distance_atr: Decimal = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class Config:
    mode: str                      # "paper" or "live"
    broker: BrokerConfig
    execution: ExecutionConfig
    sizing: SizingConfig
    risk: RiskConfig
    strategy: StrategyConfig
    persistence: PersistenceConfig
    process: ProcessConfig
    live_allow_non_sip: bool = False

    def is_live(self) -> bool:
        return self.mode == "live"


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(
    path: Path | str,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Read ``path``, overlay env secrets, validate, return a frozen Config."""
    env = dict(env) if env is not None else dict(os.environ)
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parse error: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    mode = _req_str(data, "mode")
    if mode not in ("paper", "live"):
        raise ConfigError(f"mode must be 'paper' or 'live', got {mode!r}")

    broker = _load_broker(data.get("broker", {}), env)
    execution = _load_execution(data.get("execution", {}))
    sizing = _load_sizing(data.get("sizing", {}))
    risk = _load_risk(data.get("risk", {}))
    strategy = _load_strategy(data.get("strategy", {}))
    persistence = _load_persistence(data.get("persistence", {}))
    process = _load_process(data.get("process", {}))
    live_allow_non_sip = bool(data.get("live_allow_non_sip", False))

    cfg = Config(
        mode=mode,
        broker=broker,
        execution=execution,
        sizing=sizing,
        risk=risk,
        strategy=strategy,
        persistence=persistence,
        process=process,
        live_allow_non_sip=live_allow_non_sip,
    )
    _validate(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Section loaders
# ---------------------------------------------------------------------------


def _load_broker(d: Mapping[str, Any], env: Mapping[str, str]) -> BrokerConfig:
    api_key_env = _req_str(d, "api_key_env")
    api_secret_env = _req_str(d, "api_secret_env")
    key = env.get(api_key_env, "")
    secret = env.get(api_secret_env, "")
    if not key or not secret:
        raise ConfigError(
            f"API credentials missing: {api_key_env} and/or {api_secret_env} "
            "not set in environment"
        )
    return BrokerConfig(
        base_url=_req_str(d, "base_url"),
        data_feed=_req_str(d, "data_feed").lower(),
        api_key=Secret(key),
        api_secret=Secret(secret),
        request_timeout_s=float(d.get("request_timeout_s", 10.0)),
        max_retries=int(d.get("max_retries", 3)),
        retry_backoff_base_s=float(d.get("retry_backoff_base_s", 0.5)),
        retry_backoff_cap_s=float(d.get("retry_backoff_cap_s", 4.0)),
        order_poll_interval_s=float(d.get("order_poll_interval_s", 0.5)),
        order_poll_timeout_s=float(d.get("order_poll_timeout_s", 15.0)),
    )


def _load_execution(d: Mapping[str, Any]) -> ExecutionConfig:
    return ExecutionConfig(
        limit_slippage_bps=_dec(d, "limit_slippage_bps", "8"),
        disaster_stop_atr_mult=_dec(d, "disaster_stop_atr_mult", "2.0"),
        flat_before_close_minutes=int(d.get("flat_before_close_minutes", 10)),
    )


def _load_sizing(d: Mapping[str, Any]) -> SizingConfig:
    levels_raw = d.get("drawdown_throttle_levels", {}) or {}
    if not isinstance(levels_raw, dict):
        raise ConfigError("drawdown_throttle_levels must be a mapping")
    levels: list[tuple[Decimal, Decimal]] = []
    for k, v in levels_raw.items():
        try:
            levels.append((Decimal(str(k)), Decimal(str(v))))
        except Exception as exc:
            raise ConfigError(
                f"bad drawdown_throttle_levels entry: {k!r}: {v!r}"
            ) from exc
    # Sort ascending by threshold so callers iterate in order.
    levels.sort(key=lambda x: x[0])
    return SizingConfig(
        enable_compounding=bool(d.get("enable_compounding", True)),
        starting_equity=_dec(d, "starting_equity"),
        position_notional_pct=_dec(d, "position_notional_pct"),
        min_trade_notional=_dec(d, "min_trade_notional"),
        max_trade_notional=_dec(d, "max_trade_notional"),
        max_total_exposure_pct=_dec(d, "max_total_exposure_pct"),
        max_symbol_exposure_pct=_dec(d, "max_symbol_exposure_pct"),
        drawdown_throttle_levels=tuple(levels),
        halt_drawdown_pct=_dec(d, "halt_drawdown_pct"),
    )


def _load_risk(d: Mapping[str, Any]) -> RiskConfig:
    return RiskConfig(
        daily_loss_cap_pct=_dec(d, "daily_loss_cap_pct"),
        max_concurrent_positions=int(_req_int(d, "max_concurrent_positions")),
        spread_filter_bps=_dec(d, "spread_filter_bps"),
        stale_data_max_age_s=int(_req_int(d, "stale_data_max_age_s")),
        stale_bar_grace_s=int(_req_int(d, "stale_bar_grace_s")),
        session_start_utc=parse_hhmm_utc(_req_str(d, "session_start_utc")),
        session_end_utc=parse_hhmm_utc(_req_str(d, "session_end_utc")),
        loss_cooldown_s=int(_req_int(d, "loss_cooldown_s")),
        min_expected_edge_bps=_dec(d, "min_expected_edge_bps"),
        block_first_minutes=int(d.get("block_first_minutes", 5)),
        block_last_minutes=int(d.get("block_last_minutes", 10)),
        atr_stop_mult=_dec(d, "atr_stop_mult", "1.2"),
    )


def _load_strategy(d: Mapping[str, Any]) -> StrategyConfig:
    syms = d.get("symbols")
    if not syms or not isinstance(syms, list) or not all(isinstance(s, str) and s for s in syms):
        raise ConfigError("strategy.symbols must be a non-empty list of symbol strings")
    valid_tfs = ("5Min", "15Min", "1Hour")
    for key in ("entry_tf", "confirm_tf", "trend_tf"):
        tf = d.get(key)
        if tf not in valid_tfs:
            raise ConfigError(f"strategy.{key} must be one of {valid_tfs}, got {tf!r}")
    return StrategyConfig(
        symbols=tuple(syms),
        entry_tf=str(d["entry_tf"]),
        confirm_tf=str(d["confirm_tf"]),
        trend_tf=str(d["trend_tf"]),
        ema_fast=int(_req_int(d, "ema_fast")),
        ema_slow=int(_req_int(d, "ema_slow")),
        trend_ema=int(_req_int(d, "trend_ema")),
        adx_period=int(_req_int(d, "adx_period")),
        adx_min=_dec(d, "adx_min"),
        atr_period=int(_req_int(d, "atr_period")),
        atr_target_mult=_dec(d, "atr_target_mult"),
        breakout_lookback=int(_req_int(d, "breakout_lookback")),
        min_bars_entry_tf=int(d.get("min_bars_entry_tf", 100)),
        min_bars_confirm_tf=int(d.get("min_bars_confirm_tf", 60)),
        min_bars_trend_tf=int(d.get("min_bars_trend_tf", 210)),
        reentry_cooldown_bars=int(d.get("reentry_cooldown_bars", 6)),
        max_hold_bars=int(d.get("max_hold_bars", 24)),
        tick_interval_s=float(d.get("tick_interval_s", 10.0)),
        trailing_stop_enabled=bool(d.get("trailing_stop_enabled", False)),
        trailing_breakeven_at_atr=Decimal(str(d.get("trailing_breakeven_at_atr", "0.5"))),
        trailing_activation_at_atr=Decimal(str(d.get("trailing_activation_at_atr", "1.5"))),
        trailing_distance_atr=Decimal(str(d.get("trailing_distance_atr", "0.5"))),
    )


def _load_persistence(d: Mapping[str, Any]) -> PersistenceConfig:
    return PersistenceConfig(
        state_path=Path(_req_str(d, "state_path")),
        trade_log_path=Path(_req_str(d, "trade_log_path")),
        state_fsync=bool(d.get("state_fsync", True)),
    )


def _load_process(d: Mapping[str, Any]) -> ProcessConfig:
    return ProcessConfig(
        lock_file=Path(_req_str(d, "lock_file")),
        heartbeat_path=Path(_req_str(d, "heartbeat_path")),
        kill_switch_path=Path(_req_str(d, "kill_switch_path")),
    )


# ---------------------------------------------------------------------------
# Cross-section validations
# ---------------------------------------------------------------------------


def _validate(cfg: Config) -> None:
    s = cfg.sizing
    r = cfg.risk
    e = cfg.execution
    b = cfg.broker

    if s.starting_equity <= 0:
        raise ConfigError("starting_equity must be positive")
    if s.position_notional_pct <= 0 or s.position_notional_pct > 1:
        raise ConfigError("position_notional_pct must be in (0, 1]")
    if s.min_trade_notional <= 0 or s.max_trade_notional <= 0:
        raise ConfigError("min/max_trade_notional must be positive")
    if s.min_trade_notional > s.max_trade_notional:
        raise ConfigError("min_trade_notional must be <= max_trade_notional")
    if s.max_trade_notional > s.starting_equity * s.max_total_exposure_pct:
        raise ConfigError(
            "max_trade_notional must not exceed starting_equity * max_total_exposure_pct"
        )
    if s.max_symbol_exposure_pct > s.max_total_exposure_pct:
        raise ConfigError(
            "max_symbol_exposure_pct must be <= max_total_exposure_pct"
        )
    if s.halt_drawdown_pct <= 0:
        raise ConfigError("halt_drawdown_pct must be positive")
    if s.drawdown_throttle_levels:
        max_throttle_thr = max(x[0] for x in s.drawdown_throttle_levels)
        if s.halt_drawdown_pct <= max_throttle_thr:
            raise ConfigError(
                "halt_drawdown_pct must be strictly greater than every "
                "drawdown_throttle_levels threshold"
            )
        for thr, mult in s.drawdown_throttle_levels:
            if thr <= 0:
                raise ConfigError("drawdown_throttle_levels thresholds must be > 0")
            if mult <= 0 or mult > 1:
                raise ConfigError("drawdown_throttle_levels mults must be in (0, 1]")

    if r.daily_loss_cap_pct <= 0:
        raise ConfigError("daily_loss_cap_pct must be positive")
    if r.min_expected_edge_bps <= 2 * r.spread_filter_bps:
        raise ConfigError(
            "min_expected_edge_bps must be strictly greater than 2 * spread_filter_bps"
        )
    if r.spread_filter_bps <= 0:
        raise ConfigError("spread_filter_bps must be positive")
    if r.stale_data_max_age_s <= 0:
        raise ConfigError("stale_data_max_age_s must be positive")
    if r.stale_bar_grace_s <= 0:
        raise ConfigError("stale_bar_grace_s must be positive")
    if r.max_concurrent_positions <= 0:
        raise ConfigError("max_concurrent_positions must be positive")
    if r.loss_cooldown_s < 0:
        raise ConfigError("loss_cooldown_s must be non-negative")
    if r.session_start_utc >= r.session_end_utc:
        raise ConfigError("session_start_utc must be before session_end_utc")

    if e.disaster_stop_atr_mult <= r.atr_stop_mult:
        raise ConfigError(
            "disaster_stop_atr_mult must be strictly greater than atr_stop_mult"
        )
    if e.disaster_stop_atr_mult > Decimal("5.0"):
        raise ConfigError(
            "disaster_stop_atr_mult capped at 5.0 to prevent mis-configuration"
        )
    if e.flat_before_close_minutes < 5:
        raise ConfigError("flat_before_close_minutes must be at least 5")
    session_minutes = (
        r.session_end_utc.hour * 60 + r.session_end_utc.minute
    ) - (r.session_start_utc.hour * 60 + r.session_start_utc.minute)
    if e.flat_before_close_minutes >= session_minutes:
        raise ConfigError("flat_before_close_minutes >= session length")

    if b.order_poll_interval_s * 2 >= b.order_poll_timeout_s:
        raise ConfigError(
            "order_poll_interval_s * 2 must be < order_poll_timeout_s "
            "(need room for >1 poll within the timeout window)"
        )
    if b.max_retries < 0:
        raise ConfigError("max_retries must be non-negative")
    if b.retry_backoff_base_s <= 0 or b.retry_backoff_cap_s <= 0:
        raise ConfigError("retry backoff values must be positive")
    if b.retry_backoff_cap_s < b.retry_backoff_base_s:
        raise ConfigError("retry_backoff_cap_s must be >= retry_backoff_base_s")

    if b.data_feed not in ("sip", "iex"):
        raise ConfigError("broker.data_feed must be 'sip' or 'iex'")

    # --- strategy invariants -------------------------------------------
    sp = cfg.strategy
    if sp.ema_fast <= 0 or sp.ema_slow <= 0 or sp.trend_ema <= 0:
        raise ConfigError("EMA periods must be positive")
    if sp.ema_fast >= sp.ema_slow:
        raise ConfigError("ema_fast must be strictly less than ema_slow")
    if sp.adx_period <= 1 or sp.atr_period <= 1:
        raise ConfigError("adx_period and atr_period must be > 1")
    if sp.adx_min <= 0:
        raise ConfigError("adx_min must be positive")
    if sp.atr_target_mult <= 0:
        raise ConfigError("atr_target_mult must be positive")
    if sp.atr_target_mult <= r.atr_stop_mult:
        raise ConfigError(
            "atr_target_mult must be strictly greater than atr_stop_mult "
            "(positive R:R before costs)"
        )
    if sp.breakout_lookback <= 0:
        raise ConfigError("breakout_lookback must be positive")
    if sp.min_bars_entry_tf < max(sp.ema_slow, sp.adx_period + 1, sp.atr_period + 1, sp.breakout_lookback + 1):
        raise ConfigError(
            "min_bars_entry_tf must be at least max(ema_slow, adx_period+1, atr_period+1, breakout_lookback+1)"
        )
    if sp.min_bars_confirm_tf < sp.ema_slow:
        raise ConfigError("min_bars_confirm_tf must be >= ema_slow")
    if sp.min_bars_trend_tf < sp.trend_ema:
        raise ConfigError("min_bars_trend_tf must be >= trend_ema")
    if sp.reentry_cooldown_bars < 0 or sp.max_hold_bars <= 0:
        raise ConfigError("reentry_cooldown_bars/max_hold_bars invariants violated")
    if sp.tick_interval_s <= 0:
        raise ConfigError("tick_interval_s must be positive")
    if sp.trailing_stop_enabled:
        if sp.trailing_breakeven_at_atr <= 0:
            raise ConfigError("trailing_breakeven_at_atr must be positive when trailing enabled")
        if sp.trailing_activation_at_atr <= sp.trailing_breakeven_at_atr:
            raise ConfigError(
                "trailing_activation_at_atr must be strictly greater than "
                "trailing_breakeven_at_atr (breakeven before trail)"
            )
        if sp.trailing_distance_atr <= 0:
            raise ConfigError("trailing_distance_atr must be positive when trailing enabled")
    if len(sp.symbols) > 50:
        raise ConfigError("symbols universe capped at 50 in v1")

    # --- live-mode-specific invariants ---------------------------------
    if cfg.is_live():
        if b.data_feed != "sip" and not cfg.live_allow_non_sip:
            raise ConfigError(
                "live mode requires SIP data_feed; set live_allow_non_sip: true "
                "to override (not recommended)"
            )
        # base_url must point at the live endpoint, not paper.
        if "paper" in b.base_url.lower():
            raise ConfigError(
                "live mode cannot use a base_url that contains 'paper'"
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req_str(d: Mapping[str, Any], key: str) -> str:
    if key not in d or d[key] is None:
        raise ConfigError(f"missing required config key: {key}")
    v = d[key]
    if not isinstance(v, str) or not v:
        raise ConfigError(f"config key {key!r} must be a non-empty string")
    return v


def _req_int(d: Mapping[str, Any], key: str) -> int:
    if key not in d or d[key] is None:
        raise ConfigError(f"missing required config key: {key}")
    try:
        return int(d[key])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config key {key!r} must be an integer") from exc


def _dec(d: Mapping[str, Any], key: str, default: str | None = None) -> Decimal:
    raw = d.get(key, default)
    if raw is None:
        raise ConfigError(f"missing required config key: {key}")
    try:
        return Decimal(str(raw))
    except Exception as exc:
        raise ConfigError(f"config key {key!r} must be a number") from exc
