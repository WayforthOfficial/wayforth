-- Migration 029: three-track payment model
-- Adds payment_rail tracking to api_keys, USDC payments table

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS payment_rail
    TEXT NOT NULL DEFAULT 'card'
    CHECK (payment_rail IN ('card','usdc','x402'));

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS usdc_wallet_address TEXT;

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS subscription_expires_at TIMESTAMPTZ;

-- executions table does not exist in this schema; search_outcomes is the equivalent.
-- Add payment_rail and cross_rail_fee_credits to search_outcomes instead.
ALTER TABLE search_outcomes
  ADD COLUMN IF NOT EXISTS cross_rail_fee_credits INTEGER NOT NULL DEFAULT 0;

-- payment_track already exists on search_outcomes (from migration 020); no change needed.

CREATE TABLE IF NOT EXISTS usdc_payments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  reference_id TEXT UNIQUE NOT NULL,
  api_key_id UUID REFERENCES api_keys(id),
  plan TEXT NOT NULL,
  amount_usdc DECIMAL(18,6) NOT NULL,
  wallet_address TEXT,
  tx_hash TEXT UNIQUE,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','confirmed','expired','refunded')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  confirmed_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS usdc_payments_status_idx
  ON usdc_payments(status, expires_at);
