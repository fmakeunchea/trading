-- Track when a strategy edit has been applied to the YAML config and when
-- the bot was last restarted with that config. If applied_at < updated_at,
-- the UI shows "restart required". If last_restart_at < applied_at, same.
ALTER TABLE strategies
    ADD COLUMN IF NOT EXISTS applied_at      TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_restart_at TIMESTAMPTZ;
