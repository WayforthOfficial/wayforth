-- Migration 047: developer Pioneer Program opt-in fields on users
ALTER TABLE users ADD COLUMN IF NOT EXISTS pioneer_opt_in          BOOLEAN      DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS pioneer_opted_in_at     TIMESTAMPTZ  NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS pioneer_credits_awarded BOOLEAN      DEFAULT FALSE;
  -- one-time guard: 15% monthly-allowance bonus awarded exactly once on join
ALTER TABLE users ADD COLUMN IF NOT EXISTS pioneer_opt_out_at      TIMESTAMPTZ  NULL;
