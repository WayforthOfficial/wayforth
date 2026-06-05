-- Migration 056: keep quota_reset_at and monthly_calls_reset_at in sync.
-- Root cause of the drift: quota_reset_at had a DB DEFAULT
-- (date_trunc('month', now()) + 1 month, anchored to key CREATION time) while
-- monthly_calls_reset_at defaulted NULL and was only set lazily by
-- _increment_calls on the FIRST execution (anchored to first-execution time).
-- Different anchors → the two reset-date fields disagreed (e.g. June 1 vs July 1),
-- and the reset job only reads monthly_calls_reset_at.
--
-- Give monthly_calls_reset_at the same creation-time default so both fields are
-- set together on new keys, and backfill existing NULLs to match quota_reset_at.
ALTER TABLE api_keys
    ALTER COLUMN monthly_calls_reset_at
    SET DEFAULT (date_trunc('month', now()) + interval '1 month');

UPDATE api_keys
   SET monthly_calls_reset_at = COALESCE(
           quota_reset_at,
           date_trunc('month', now()) + interval '1 month'
       )
 WHERE monthly_calls_reset_at IS NULL;
