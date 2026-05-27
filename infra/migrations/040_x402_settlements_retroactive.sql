-- 040_x402_settlements_retroactive.sql
--
-- v0.8.0 Item 1: x402 payment replay protection.
--
-- The x402_settlements table was originally created inline in the API
-- lifespan handler in v0.7.8 (Section 9, commit d6ee0c3). v0.8.0 wires the
-- application INSERT into routers/x402.py:x402_execute and x402_search so a
-- replayed X-PAYMENT header is durably rejected even after Redis TTL expiry.
--
-- This file captures the schema in the migration audit trail. Production
-- databases that booted under v0.7.8+ already have this table from the
-- lifespan create; the IF NOT EXISTS clauses make this file safe to apply
-- to any environment without side effects.

CREATE TABLE IF NOT EXISTS x402_settlements (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_hash  TEXT NOT NULL,
    amount        NUMERIC NOT NULL,
    service_slug  TEXT NOT NULL,
    user_id       UUID REFERENCES users(id),
    settled_at    TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT x402_settlements_payment_hash_unique
        UNIQUE (payment_hash)
);

CREATE INDEX IF NOT EXISTS x402_settlements_user_idx
    ON x402_settlements(user_id, settled_at DESC);
