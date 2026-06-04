-- ============================================================================
-- Data fix: move accumulated pioneer drip into the new pioneer_credits_balance
-- Date:    2026-06-04
-- Context: Migration 055 adds the separate pioneer overflow pool. This one-time
--          script moves each enrolled user's accumulated pioneer-drip credits
--          (since their last subscription reset) out of the main credits_balance
--          and into pioneer_credits_balance, leaving total credits unchanged.
--
-- ⚠️  ONE-TIME — NOT idempotent. The second UPDATE subtracts from credits_balance;
--     re-running would double-subtract. Run exactly once after migration 055.
-- ============================================================================

BEGIN;

-- 1. Populate pioneer_credits_balance with drip credited since the last reset.
--    last_credited_at is NULL for never-reset accounts → COALESCE to epoch so we
--    capture all drip so far.
UPDATE user_credits uc
SET pioneer_credits_balance = (
  SELECT COALESCE(SUM(amount), 0)
  FROM credit_transactions ct
  WHERE ct.user_id = uc.user_id
    AND ct.type = 'pioneer_drip'
    AND ct.created_at >= (
      SELECT COALESCE(last_credited_at, '2000-01-01')
      FROM user_credits
      WHERE user_id = uc.user_id
    )
)
WHERE EXISTS (
  SELECT 1 FROM users WHERE id = uc.user_id
  AND pioneer_opt_in = true
);

-- 2. Remove the same amount from the main balance (it now lives in the pool).
--    Guarded so the main balance never goes negative / below the moved amount.
UPDATE user_credits uc
SET credits_balance = credits_balance - pioneer_credits_balance
WHERE pioneer_credits_balance > 0
  AND credits_balance > pioneer_credits_balance;

COMMIT;
