-- Migration 051: USDC payer binding, tx dedup, scan cursor, admin reconciliation
-- (FINDING-002 / FINDING-003).
--
-- Binds each USDC payment to the on-chain sender, makes tx hashes unique, and
-- persists the scan cursor so the watcher never re-scans from genesis. Also adds
-- columns for the admin reconciliation path (manually-reviewed stranded funds).
ALTER TABLE usdc_payments
    ADD COLUMN IF NOT EXISTS payer_address       TEXT,
    ADD COLUMN IF NOT EXISTS consumed            BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS reconciliation_note TEXT,
    ADD COLUMN IF NOT EXISTS reconciled_by       TEXT,
    ADD COLUMN IF NOT EXISTS reconciled_at       TIMESTAMPTZ;

-- tx_hash must be unique. Many prod DBs already have usdc_payments_tx_hash_key
-- (migration 029); only add ours if NO unique constraint on tx_hash exists, and
-- only when there are no pre-existing duplicate non-null tx hashes.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'usdc_payments'::regclass
          AND contype = 'u'
          AND pg_get_constraintdef(oid) LIKE '%tx_hash%'
    ) AND NOT EXISTS (
        SELECT tx_hash FROM usdc_payments
        WHERE tx_hash IS NOT NULL
        GROUP BY tx_hash HAVING COUNT(*) > 1
    ) THEN
        ALTER TABLE usdc_payments
            ADD CONSTRAINT usdc_payments_tx_hash_unique UNIQUE (tx_hash);
    END IF;
END
$$;

-- Singleton scan cursor for the Base watcher.
CREATE TABLE IF NOT EXISTS usdc_scan_state (
    id         INT PRIMARY KEY DEFAULT 1,
    last_block BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    CHECK (id = 1)
);
INSERT INTO usdc_scan_state (id, last_block) VALUES (1, 0) ON CONFLICT DO NOTHING;
