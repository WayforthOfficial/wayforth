CREATE TABLE IF NOT EXISTS service_queries (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id  UUID        REFERENCES services(id) ON DELETE CASCADE,
    query_text  TEXT,
    score       INTEGER,
    queried_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_queries_service ON service_queries (service_id);
CREATE INDEX IF NOT EXISTS idx_queries_time    ON service_queries (queried_at DESC);

CREATE TABLE IF NOT EXISTS service_submissions (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    service_id    UUID        REFERENCES services(id) ON DELETE CASCADE,
    contact_email TEXT,
    submitted_at  TIMESTAMPTZ DEFAULT NOW(),
    ip_address    TEXT
);
