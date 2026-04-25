CREATE TABLE IF NOT EXISTS competitive_intelligence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source TEXT NOT NULL,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ci_source ON competitive_intelligence(source);
CREATE INDEX IF NOT EXISTS idx_ci_created ON competitive_intelligence(created_at DESC);
