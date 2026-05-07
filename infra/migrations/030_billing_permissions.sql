-- Migration 030: agent billing permission system

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS billing_permission
    TEXT NOT NULL DEFAULT 'none'
    CHECK (billing_permission IN ('none', 'auto_topup', 'full'));

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS monthly_topup_limit_usd
    DECIMAL(10,2) DEFAULT 20.00;

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS topup_amount_usd
    DECIMAL(10,2) DEFAULT 5.00;

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS topup_trigger_calls
    INTEGER DEFAULT 100;

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS monthly_topup_spent_usd
    DECIMAL(10,2) NOT NULL DEFAULT 0;

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS monthly_topup_reset_at
    TIMESTAMPTZ DEFAULT date_trunc('month', NOW()) + INTERVAL '1 month';
