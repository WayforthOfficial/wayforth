-- Migration 046 (v0.8.2): Pioneer Program — one-time award → monthly daily drip.
--
-- The program now drips tier-based credits once per UTC day to opted-in users
-- (handled by the pioneer drip background loop) and enforces a 7-day cooldown
-- before a developer who leaves can rejoin. The old one-time guard column
-- pioneer_credits_awarded is removed.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS pioneer_cooldown_until  TIMESTAMPTZ  NULL,
    ADD COLUMN IF NOT EXISTS pioneer_last_drip_date  DATE         NULL;

ALTER TABLE users
    DROP COLUMN IF EXISTS pioneer_credits_awarded;

-- Drip query support: find opted-in users not yet dripped today.
CREATE INDEX IF NOT EXISTS idx_users_pioneer_drip
  ON users (pioneer_last_drip_date)
  WHERE pioneer_opt_in = TRUE;
