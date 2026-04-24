-- AutoFlow Trader — initial schema.
-- Single-tenant MVP: no users/orgs table yet. Add in v2.

-- Editable strategy presets. The engine still reads YAML from disk; when a
-- strategy is "applied" the API renders YAML from this row and restarts
-- the bot container.
CREATE TABLE IF NOT EXISTS strategies (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    mode            TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    symbols         TEXT[] NOT NULL DEFAULT '{}',
    daily_loss_cap_pct      NUMERIC(6,4) NOT NULL DEFAULT 0.03,
    max_concurrent_positions INT          NOT NULL DEFAULT 3,
    position_notional_pct   NUMERIC(6,4) NOT NULL DEFAULT 0.05,
    -- Catch-all for parameters we don't hoist to columns yet
    extra_config    JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_active       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Only one strategy can be active at a time in MVP.
CREATE UNIQUE INDEX IF NOT EXISTS strategies_one_active
    ON strategies (is_active) WHERE is_active;

-- Mirrored from the engine's trades.jsonl INCIDENT records for fast querying.
-- Source of truth stays the append-only log; this is a cache + index.
CREATE TABLE IF NOT EXISTS incidents (
    id              BIGSERIAL PRIMARY KEY,
    -- Stable dedupe key derived from the jsonl record (e.g. hash chain entry)
    source_id       TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,             -- RECONCILE, HALT, ORPHAN, RESTART, ...
    severity        TEXT NOT NULL DEFAULT 'info',  -- info | warn | error
    phase           TEXT,
    symbols         TEXT[],
    reason          TEXT,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at     TIMESTAMPTZ NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS incidents_occurred_at_idx ON incidents (occurred_at DESC);
CREATE INDEX IF NOT EXISTS incidents_kind_idx        ON incidents (kind);

-- Mirrored from RESULT records in trades.jsonl.
CREATE TABLE IF NOT EXISTS trades (
    id              BIGSERIAL PRIMARY KEY,
    source_id       TEXT NOT NULL UNIQUE,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    qty             NUMERIC(18,8) NOT NULL,
    avg_fill_price  NUMERIC(18,6),
    status          TEXT NOT NULL,             -- filled, partial, canceled, rejected
    pnl             NUMERIC(18,6),
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at     TIMESTAMPTZ NOT NULL,
    ingested_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS trades_occurred_at_idx ON trades (occurred_at DESC);

-- Last-known status snapshot. Upserted by the sync worker. One row only.
CREATE TABLE IF NOT EXISTS bot_state (
    id                      INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    running                 BOOLEAN NOT NULL DEFAULT FALSE,
    mode                    TEXT,
    kill_switch_engaged     BOOLEAN NOT NULL DEFAULT FALSE,
    heartbeat_at            TIMESTAMPTZ,
    reconcile_ok            BOOLEAN,
    reconcile_last_checked  TIMESTAMPTZ,
    broker_connected        BOOLEAN,
    broker_last_contacted   TIMESTAMPTZ,
    open_positions_count    INT NOT NULL DEFAULT 0,
    open_positions          JSONB NOT NULL DEFAULT '[]'::jsonb,
    incidents_today         INT NOT NULL DEFAULT 0,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO bot_state (id) VALUES (1) ON CONFLICT DO NOTHING;

-- Smoke test run history.
CREATE TABLE IF NOT EXISTS smoke_test_runs (
    id              BIGSERIAL PRIMARY KEY,
    status          TEXT NOT NULL CHECK (status IN ('running', 'pass', 'fail', 'skip', 'error')),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    stdout          TEXT,
    stderr          TEXT,
    exit_code       INT
);
CREATE INDEX IF NOT EXISTS smoke_test_runs_started_idx ON smoke_test_runs (started_at DESC);

-- Seed one default strategy so the UI has something to render on first boot.
INSERT INTO strategies (name, mode, symbols, daily_loss_cap_pct, max_concurrent_positions, position_notional_pct, is_active)
VALUES ('Default Paper', 'paper', ARRAY['SPY','QQQ','AAPL','MSFT'], 0.03, 3, 0.05, TRUE)
ON CONFLICT DO NOTHING;
