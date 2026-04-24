ALTER TABLE services ADD COLUMN IF NOT EXISTS payment_tested BOOLEAN DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS service_probes (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id       UUID        REFERENCES services(id) ON DELETE CASCADE,
    probed_at        TIMESTAMPTZ DEFAULT NOW(),
    reachable        BOOLEAN,
    response_time_ms NUMERIC,
    status_code      INTEGER,
    error_message    TEXT
);

CREATE INDEX IF NOT EXISTS idx_probes_service_time
    ON service_probes (service_id, probed_at DESC);
