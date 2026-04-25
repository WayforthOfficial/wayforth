CREATE TABLE IF NOT EXISTS service_score_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id TEXT NOT NULL,
    wri_score FLOAT NOT NULL,
    rank_score FLOAT,
    tier INTEGER,
    consecutive_failures INTEGER DEFAULT 0,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_score_history_service ON service_score_history(service_id);
CREATE INDEX IF NOT EXISTS idx_score_history_recorded ON service_score_history(recorded_at DESC);

-- Auto-cleanup: keep only 90 days of history
CREATE INDEX IF NOT EXISTS idx_score_history_cleanup ON service_score_history(recorded_at);
