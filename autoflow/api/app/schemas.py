from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field


class Health(BaseModel):
    status: Literal["ok", "degraded"]
    api_uptime_s: float
    db_ok: bool
    engine_var_dir_ok: bool


class Position(BaseModel):
    symbol: str
    qty: float
    avg_price: float | None = None
    side: Literal["long", "short"]
    unrealized_pnl: float | None = None


class Order(BaseModel):
    id: str
    symbol: str
    side: Literal["buy", "sell"]
    qty: float
    status: str
    submitted_at: datetime | None = None


class Incident(BaseModel):
    id: int
    kind: str
    severity: str
    phase: str | None = None
    symbols: list[str] = []
    reason: str | None = None
    payload: dict[str, Any] = {}
    occurred_at: datetime


class Trade(BaseModel):
    id: int
    symbol: str
    side: Literal["buy", "sell"]
    qty: float
    avg_fill_price: float | None
    status: str
    pnl: float | None
    occurred_at: datetime


class Strategy(BaseModel):
    id: int
    name: str
    mode: Literal["paper", "live"]
    symbols: list[str]
    daily_loss_cap_pct: float = Field(ge=0, le=1)
    max_concurrent_positions: int = Field(ge=1, le=50)
    position_notional_pct: float = Field(ge=0, le=1)
    is_active: bool
    applied_at: datetime | None = None
    last_restart_at: datetime | None = None
    updated_at: datetime | None = None
    # True iff edits have not been written to YAML yet (DB → YAML pending)
    dirty: bool = False
    # True iff YAML is fresh but bot wasn't restarted since apply
    restart_required: bool = False


class StrategyUpsert(BaseModel):
    name: str
    mode: Literal["paper", "live"]
    symbols: list[str]
    daily_loss_cap_pct: float = Field(ge=0, le=1)
    max_concurrent_positions: int = Field(ge=1, le=50)
    position_notional_pct: float = Field(ge=0, le=1)


class BotStatus(BaseModel):
    running: bool
    mode: str | None
    kill_switch_engaged: bool
    heartbeat_at: datetime | None
    heartbeat_fresh: bool
    reconcile_ok: bool | None
    broker_connected: bool | None
    open_positions_count: int
    incidents_today: int


class SmokeTestResult(BaseModel):
    id: int
    status: Literal["running", "pass", "fail", "skip", "error"]
    started_at: datetime
    finished_at: datetime | None
    exit_code: int | None
    stdout: str | None
    stderr: str | None


class KillSwitchState(BaseModel):
    engaged: bool


class ActionResult(BaseModel):
    ok: bool
    message: str | None = None
