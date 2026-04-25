CREATE TABLE IF NOT EXISTS agent_identities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id TEXT NOT NULL UNIQUE,  -- wallet address or session-derived ID
    display_name TEXT,
    total_searches INTEGER DEFAULT 0,
    total_payments INTEGER DEFAULT 0,
    total_spend_usdc FLOAT DEFAULT 0.0,
    preferred_services JSONB DEFAULT '[]',
    trust_score FLOAT DEFAULT 50.0,  -- 0-100, starts at 50
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_active_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_identity_agent ON agent_identities(agent_id);
CREATE INDEX IF NOT EXISTS idx_identity_trust ON agent_identities(trust_score DESC);
