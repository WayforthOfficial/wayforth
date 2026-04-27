CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    supabase_id TEXT UNIQUE,
    stripe_customer_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS subscription_status TEXT DEFAULT 'active';
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMPTZ;
