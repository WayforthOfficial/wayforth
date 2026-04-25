CREATE TABLE IF NOT EXISTS service_graph (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    service_a_id TEXT NOT NULL,
    service_b_id TEXT NOT NULL,
    co_search_count INTEGER DEFAULT 1,
    co_payment_count INTEGER DEFAULT 0,
    last_updated TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(service_a_id, service_b_id)
);

CREATE INDEX IF NOT EXISTS idx_graph_service_a ON service_graph(service_a_id);
CREATE INDEX IF NOT EXISTS idx_graph_service_b ON service_graph(service_b_id);
CREATE INDEX IF NOT EXISTS idx_graph_co_search ON service_graph(co_search_count DESC);
