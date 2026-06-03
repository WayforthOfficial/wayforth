-- Migration 054: add updated_at to usdc_payments.
-- The table tracked created_at but not last-modified time; status transitions
-- (pending → confirmed/expired/consumed) had nowhere to record when they
-- happened. Adds updated_at with a NOW() default for new rows.
--
-- Note: because the column is added with DEFAULT NOW(), existing rows are
-- populated with the migration timestamp, so the IS NULL backfill below is a
-- no-op on an already-patched DB. To make historical rows reflect created_at
-- instead, run the unconditional variant in the comment.
ALTER TABLE usdc_payments
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();

UPDATE usdc_payments SET updated_at = created_at WHERE updated_at IS NULL;

-- Optional: backfill historical rows to created_at unconditionally instead.
-- UPDATE usdc_payments SET updated_at = created_at;
