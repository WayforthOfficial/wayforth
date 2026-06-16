-- Migration 061: Swap tier keys "builder" <-> "starter"
--
-- GOAL END STATE (developer tiers, ascending):
--   key "starter" = $12/mo, 6 000 credits/mo, 30 drip/day, 3 hosted-agent slots
--   key "builder" = $29/mo, 21 000 credits/mo, 105 drip/day, 5 hosted-agent slots
--   free / pro / growth / enterprise: unchanged
--
-- Three-step swap via temp placeholder to avoid collision.
-- Constraint dropped first because the live database may not include 'builder'
-- in api_keys_tier_check (it was missing from migration 025) — we normalise it here.
--
-- !! REQUIRED: after this migration is deployed, swap FOUR env vars in Railway:
--      STRIPE_PRICE_STARTER        <- current value of STRIPE_PRICE_BUILDER        ($12/mo price ID)
--      STRIPE_PRICE_BUILDER        <- current value of STRIPE_PRICE_STARTER        ($29/mo price ID)
--      STRIPE_PRICE_STARTER_ANNUAL <- current value of STRIPE_PRICE_BUILDER_ANNUAL ($99/yr price ID)
--      STRIPE_PRICE_BUILDER_ANNUAL <- current value of STRIPE_PRICE_STARTER_ANNUAL ($290/yr price ID)

BEGIN;

-- ── api_keys.tier ─────────────────────────────────────────────────────────────

-- Capture row counts before swap (written to migration log via RAISE NOTICE)
DO $$
DECLARE
  cnt_builder INTEGER;
  cnt_starter INTEGER;
BEGIN
  SELECT COUNT(*) INTO cnt_builder FROM api_keys WHERE tier = 'builder';
  SELECT COUNT(*) INTO cnt_starter FROM api_keys WHERE tier = 'starter';
  RAISE NOTICE 'api_keys BEFORE: builder=%, starter=%', cnt_builder, cnt_starter;
END $$;

ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_tier_check;

UPDATE api_keys SET tier = '_wf_tmp_' WHERE tier = 'builder';
UPDATE api_keys SET tier = 'builder'  WHERE tier = 'starter';
UPDATE api_keys SET tier = 'starter'  WHERE tier = '_wf_tmp_';

ALTER TABLE api_keys ADD CONSTRAINT api_keys_tier_check
    CHECK (tier = ANY (ARRAY['free','starter','builder','pro','growth','enterprise']));

DO $$
DECLARE
  cnt_builder INTEGER;
  cnt_starter INTEGER;
BEGIN
  SELECT COUNT(*) INTO cnt_builder FROM api_keys WHERE tier = 'builder';
  SELECT COUNT(*) INTO cnt_starter FROM api_keys WHERE tier = 'starter';
  RAISE NOTICE 'api_keys AFTER:  builder=%, starter=%', cnt_builder, cnt_starter;
END $$;

-- ── user_credits.package_tier ─────────────────────────────────────────────────

DO $$
DECLARE
  cnt_builder INTEGER;
  cnt_starter INTEGER;
BEGIN
  SELECT COUNT(*) INTO cnt_builder FROM user_credits WHERE package_tier = 'builder';
  SELECT COUNT(*) INTO cnt_starter FROM user_credits WHERE package_tier = 'starter';
  RAISE NOTICE 'user_credits BEFORE: builder=%, starter=%', cnt_builder, cnt_starter;
END $$;

UPDATE user_credits SET package_tier = '_wf_tmp_' WHERE package_tier = 'builder';
UPDATE user_credits SET package_tier = 'builder'  WHERE package_tier = 'starter';
UPDATE user_credits SET package_tier = 'starter'  WHERE package_tier = '_wf_tmp_';

DO $$
DECLARE
  cnt_builder INTEGER;
  cnt_starter INTEGER;
BEGIN
  SELECT COUNT(*) INTO cnt_builder FROM user_credits WHERE package_tier = 'builder';
  SELECT COUNT(*) INTO cnt_starter FROM user_credits WHERE package_tier = 'starter';
  RAISE NOTICE 'user_credits AFTER:  builder=%, starter=%', cnt_builder, cnt_starter;
END $$;

-- ── usdc_payments.plan ────────────────────────────────────────────────────────

DO $$
DECLARE
  cnt_builder INTEGER;
  cnt_starter INTEGER;
BEGIN
  SELECT COUNT(*) INTO cnt_builder FROM usdc_payments WHERE plan = 'builder';
  SELECT COUNT(*) INTO cnt_starter FROM usdc_payments WHERE plan = 'starter';
  RAISE NOTICE 'usdc_payments BEFORE: builder=%, starter=%', cnt_builder, cnt_starter;
END $$;

UPDATE usdc_payments SET plan = '_wf_tmp_' WHERE plan = 'builder';
UPDATE usdc_payments SET plan = 'builder'  WHERE plan = 'starter';
UPDATE usdc_payments SET plan = 'starter'  WHERE plan = '_wf_tmp_';

DO $$
DECLARE
  cnt_builder INTEGER;
  cnt_starter INTEGER;
BEGIN
  SELECT COUNT(*) INTO cnt_builder FROM usdc_payments WHERE plan = 'builder';
  SELECT COUNT(*) INTO cnt_starter FROM usdc_payments WHERE plan = 'starter';
  RAISE NOTICE 'usdc_payments AFTER:  builder=%, starter=%', cnt_builder, cnt_starter;
END $$;

COMMIT;
