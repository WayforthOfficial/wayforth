-- WayforthRank data pipeline — search outcomes and analytics
-- Run in Railway Data tab before deploying

CREATE TABLE IF NOT EXISTS search_outcomes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query_id UUID,
    query_text TEXT,
    service_id UUID REFERENCES services(id) ON DELETE SET NULL,
    outcome_type TEXT NOT NULL CHECK (outcome_type IN ('payment_initiated', 'payment_completed', 'result_viewed')),
    session_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_search_outcomes_service ON search_outcomes(service_id);
CREATE INDEX IF NOT EXISTS idx_search_outcomes_query_id ON search_outcomes(query_id);
CREATE INDEX IF NOT EXISTS idx_search_outcomes_created ON search_outcomes(created_at DESC);

CREATE TABLE IF NOT EXISTS search_analytics (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query TEXT NOT NULL,
    results JSONB,
    top_result_id UUID,
    result_count INTEGER DEFAULT 0,
    session_id TEXT,
    led_to_payment BOOLEAN DEFAULT FALSE,
    payment_service_id UUID,
    rank_scores JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_search_analytics_created ON search_analytics(created_at DESC);
