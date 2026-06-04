-- Migration 055: pioneer drip credits become a separate overflow pool.
-- Pioneer drip no longer accumulates in the main credits_balance; it lands in
-- pioneer_credits_balance and is spent only after the main balance is exhausted.
-- Resets to 0 each subscription cycle (the main pool floors at plan_max via
-- GREATEST, preserving USDC prepay).
ALTER TABLE user_credits
    ADD COLUMN IF NOT EXISTS pioneer_credits_balance INT NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'pioneer_credits_non_negative'
    ) THEN
        ALTER TABLE user_credits
            ADD CONSTRAINT pioneer_credits_non_negative CHECK (pioneer_credits_balance >= 0);
    END IF;
END
$$;
