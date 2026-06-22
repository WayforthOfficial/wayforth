-- 064_substitution_engine.sql — config-driven substitution groups + failover event log (v0.9.1)
--
-- Extends the Reliability Proxy (/proxy/{slug}) from single-hop into a full
-- multi-hop substitution/failover engine. Two tables:
--   substitution_groups — equivalence sets of interchangeable providers per
--     Wayforth logical category. A provider is ONLY ever substituted within its
--     group. Ordering at runtime is COALESCE(wri_score,-1) DESC, manual_rank ASC,
--     slug ASC — so pre-launch (all wri_score null) the curated manual_rank is the
--     deterministic baseline, and wri_score takes over once rank data exists.
--   substitution_events — one row per failover ATTEMPT (the future learned-layer
--     training signal). Nothing consumes it yet; written fire-and-forget on the
--     failure path only.
--
-- Additive + idempotent. Mirrored as CREATE TABLE IF NOT EXISTS in main.py startup.

BEGIN;

CREATE TABLE IF NOT EXISTS substitution_groups (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    category      TEXT        NOT NULL,          -- 'web-search' | 'llm-inference' | ...
    service_slug  TEXT        NOT NULL,          -- managed slug (SERVICE_CONFIGS key)
    manual_rank   INTEGER,                       -- curated priority; lower = preferred
    active        BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (category, service_slug)
);
CREATE INDEX IF NOT EXISTS idx_subst_groups_category
    ON substitution_groups (category) WHERE active = TRUE;

-- Seed with categories that have >=2 genuinely interchangeable providers today.
-- Curated manual_rank makes pre-launch ordering deterministic. Weather (only
-- openweather) and geocoding/maps (no providers) are intentionally NOT seeded
-- until peers exist — substitution within a 1-member group is a no-op.
INSERT INTO substitution_groups (category, service_slug, manual_rank) VALUES
    ('web-search',    'serper',     1),
    ('web-search',    'brave',      2),
    ('web-search',    'tavily',     3),
    ('web-search',    'perplexity', 4),
    ('llm-inference', 'groq',       1),
    ('llm-inference', 'together',   2),
    ('llm-inference', 'mistral',    3),
    ('llm-inference', 'gemini',     4)
ON CONFLICT (category, service_slug) DO NOTHING;

-- One row per failover attempt. settlement_class + duplicate_upstream_cost_possible
-- + second_upstream_cost_credits make the idempotency leak MEASURED, not assumed.
CREATE TABLE IF NOT EXISTS substitution_events (
    id                                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    slug                              TEXT        NOT NULL,        -- candidate attempted this hop
    category                          TEXT,
    primary_provider                  TEXT        NOT NULL,        -- originally-requested slug
    failure_reason                    TEXT,                        -- failure_code that triggered this hop
    substitute_chosen                 TEXT,                        -- slug actually attempted (== slug)
    latency_ms                        INTEGER,
    success                           BOOLEAN     NOT NULL DEFAULT FALSE,
    cost_credits                      INTEGER,
    settlement_class                  TEXT        NOT NULL,        -- 'pre_send' | 'post_send_ambiguous'
    rail                              TEXT        NOT NULL DEFAULT 'managed',
    duplicate_upstream_cost_possible  BOOLEAN     NOT NULL DEFAULT FALSE,
    second_upstream_cost_credits      INTEGER,                     -- measured duplicate-cost leak
    retried_primary                   BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at                        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_subst_events_primary
    ON substitution_events (primary_provider, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_subst_events_category
    ON substitution_events (category, created_at DESC);

COMMIT;
