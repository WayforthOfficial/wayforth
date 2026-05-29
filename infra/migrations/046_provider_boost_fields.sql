-- Migration 046: provider boost fields for Pioneer Program
-- boost_used is permanent/immutable once set TRUE — admin cannot reset it.
ALTER TABLE providers ADD COLUMN IF NOT EXISTS boost_used          BOOLEAN      DEFAULT FALSE;
ALTER TABLE providers ADD COLUMN IF NOT EXISTS boost_activated_at  TIMESTAMPTZ  NULL;
ALTER TABLE providers ADD COLUMN IF NOT EXISTS boost_expires_at    TIMESTAMPTZ  NULL;
ALTER TABLE providers ADD COLUMN IF NOT EXISTS boost_tier          VARCHAR(20)  NULL;
  -- 'intelligence' | 'premium'
ALTER TABLE providers ADD COLUMN IF NOT EXISTS boost_wri_bonus     INTEGER      DEFAULT 0;
ALTER TABLE providers ADD COLUMN IF NOT EXISTS boost_paused        BOOLEAN      DEFAULT FALSE;
  -- auto-paused when provider drops below Tier 2; restored on recovery
