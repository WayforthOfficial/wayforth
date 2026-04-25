CREATE TABLE IF NOT EXISTS api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key_hash TEXT NOT NULL UNIQUE,  -- SHA256 of actual key — never store plaintext
    key_prefix TEXT NOT NULL,       -- First 8 chars for identification (wf_live_xx)
    owner_email TEXT NOT NULL,
    tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'starter', 'pro', 'enterprise')),
    rate_limit_per_minute INTEGER DEFAULT 10,
    monthly_quota INTEGER DEFAULT 1000,  -- searches per month
    usage_this_month INTEGER DEFAULT 0,
    quota_reset_at TIMESTAMPTZ DEFAULT date_trunc('month', NOW()) + INTERVAL '1 month',
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_email ON api_keys(owner_email);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(active);
