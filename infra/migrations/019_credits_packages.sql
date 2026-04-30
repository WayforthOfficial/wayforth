-- Migration 019: credits/packages system
-- user_credits has UNIQUE (user_id) so ON CONFLICT (user_id) works correctly

CREATE TABLE user_credits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    credits_balance BIGINT NOT NULL DEFAULT 0,
    lifetime_credits BIGINT NOT NULL DEFAULT 0,
    package_tier TEXT NOT NULL DEFAULT 'free',
    payment_method TEXT DEFAULT 'card',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE package_purchases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    package_name TEXT NOT NULL,
    credits_purchased BIGINT NOT NULL,
    credits_bonus BIGINT DEFAULT 0,
    credits_total BIGINT NOT NULL,
    amount_usd NUMERIC(10,2),
    amount_usdc NUMERIC(18,8),
    amount_wayf NUMERIC(18,8),
    payment_method TEXT NOT NULL,
    payment_status TEXT DEFAULT 'pending',
    stripe_payment_id TEXT,
    tx_hash TEXT,
    chain TEXT DEFAULT 'base-mainnet',
    purchased_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE credit_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    amount BIGINT NOT NULL,
    balance_after BIGINT NOT NULL,
    type TEXT NOT NULL,
    description TEXT,
    api_endpoint TEXT,
    service_id TEXT,
    payment_tx_hash TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_user_credits_user ON user_credits(user_id);
CREATE INDEX idx_credit_tx_user ON credit_transactions(user_id, created_at DESC);
CREATE INDEX idx_pkg_purchases_user ON package_purchases(user_id, purchased_at DESC);
