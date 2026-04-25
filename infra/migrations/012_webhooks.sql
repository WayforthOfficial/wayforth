CREATE TABLE IF NOT EXISTS provider_webhooks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id TEXT NOT NULL,
    webhook_url TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    events TEXT[] DEFAULT ARRAY['tier_change', 'health_alert'],
    secret_token TEXT NOT NULL,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    last_fired_at TIMESTAMPTZ,
    UNIQUE(service_id, webhook_url)
);

CREATE INDEX IF NOT EXISTS idx_webhooks_service ON provider_webhooks(service_id);
CREATE INDEX IF NOT EXISTS idx_webhooks_active ON provider_webhooks(active);
