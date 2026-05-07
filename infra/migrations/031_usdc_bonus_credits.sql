-- Migration 031: track bonus credits on every USDC payment

ALTER TABLE usdc_payments
  ADD COLUMN IF NOT EXISTS bonus_credits
    INTEGER NOT NULL DEFAULT 0;
