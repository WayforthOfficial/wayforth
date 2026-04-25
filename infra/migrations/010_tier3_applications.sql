CREATE TABLE IF NOT EXISTS tier3_applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id TEXT,
    service_name TEXT NOT NULL,
    company_name TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    website TEXT,
    endpoint_url TEXT NOT NULL,
    monthly_volume_usdc FLOAT,
    sla_uptime_target FLOAT,
    kyb_status TEXT DEFAULT 'pending' CHECK (kyb_status IN ('pending', 'in_review', 'approved', 'rejected')),
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_t3_status ON tier3_applications(kyb_status);
CREATE INDEX IF NOT EXISTS idx_t3_email ON tier3_applications(contact_email);
