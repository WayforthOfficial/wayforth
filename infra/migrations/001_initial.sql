CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE IF NOT EXISTS services (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT        NOT NULL,
    description     TEXT,
    endpoint_url    TEXT        NOT NULL UNIQUE,
    category        TEXT        CHECK (category IN ('inference', 'data', 'translation')),
    coverage_tier   INTEGER     NOT NULL DEFAULT 0,
    pricing_usdc    NUMERIC,
    source          TEXT,
    metadata        JSONB       NOT NULL DEFAULT '{}',
    last_tested_at  TIMESTAMPTZ,
    uptime_7d       NUMERIC,
    schema_validated BOOLEAN    NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_services_category       ON services (category);
CREATE INDEX IF NOT EXISTS idx_services_coverage_tier  ON services (coverage_tier);
CREATE INDEX IF NOT EXISTS idx_services_source         ON services (source);
