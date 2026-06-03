-- Migration 050: non-negative credits balance (FINDING-013).
-- This CHECK constraint was applied ad-hoc directly to production during the
-- v0.8.4 work but never committed, so a rebuild from migrations alone omitted
-- it. Committing here makes the schema reproducible from source.
--
-- Postgres has no "ADD CONSTRAINT IF NOT EXISTS", so guard with a catalog
-- lookup to keep this migration idempotent against the already-patched prod DB.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'user_credits_credits_balance_non_negative'
    ) THEN
        ALTER TABLE user_credits
            ADD CONSTRAINT user_credits_credits_balance_non_negative
            CHECK (credits_balance >= 0);
    END IF;
END
$$;
