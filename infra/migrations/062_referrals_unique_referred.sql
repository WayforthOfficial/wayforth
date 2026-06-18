-- 062_referrals_unique_referred.sql
-- BILLING-1: prevent referral double-redeem (one redemption per user, DB-enforced).
--
-- The redeem flow used SELECT-then-UPDATE with no row lock and no uniqueness, so
-- concurrent redeems by one account double-granted the 500-credit bonus. The app
-- now claims atomically, and this partial UNIQUE index makes a second claim by the
-- same user impossible at the database layer.
--
-- The de-dup step first clears any existing double-redemptions (keeping the
-- earliest claim per user) so the index can be created on already-affected data.

BEGIN;

WITH ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY referred_user_id
               ORDER BY redeemed_at NULLS LAST, id
           ) AS rn
    FROM referrals
    WHERE referred_user_id IS NOT NULL
)
UPDATE referrals r
SET referred_user_id = NULL,
    redeemed_at = NULL
FROM ranked
WHERE r.id = ranked.id
  AND ranked.rn > 1;

CREATE UNIQUE INDEX IF NOT EXISTS idx_referrals_referred_user_unique
    ON referrals (referred_user_id)
    WHERE referred_user_id IS NOT NULL;

COMMIT;
