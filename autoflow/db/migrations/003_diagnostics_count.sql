-- Split diagnostics from critical incidents in bot_state.
-- Diagnostics (kind = 'DIAGNOSTIC') are observability records like
-- bars_fetched / no_signal / risk_denied — they are NOT safety events
-- and shouldn't inflate the "Incidents today" badge.
ALTER TABLE bot_state
    ADD COLUMN IF NOT EXISTS diagnostics_today INT NOT NULL DEFAULT 0;
