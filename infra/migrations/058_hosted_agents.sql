-- Migration 058: Wayforth Cloud — hosted agent runtime tables
--
-- Unit economics (confirmed before this migration):
--   E2B config: 1 vCPU + 512 MiB = $0.00001625/s = $0.000975/min
--   Charge rate: 1 credit/minute (ceil), minimum 1 credit per run
--   Growth tier credit value: $299/240,000 = $0.001246/credit
--   Margin: 27.8% above E2B cost at Growth tier (worst case) — all tiers provably above cost

CREATE TABLE hosted_agents (
    id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name             TEXT        NOT NULL,
    slug             TEXT        NOT NULL,
    runtime          TEXT        NOT NULL DEFAULT 'python3.12',
        -- 'python3.12', 'node20'
    code_path        TEXT,
        -- Supabase Storage path: {user_id}/agents/{id}/agent.{py|ts} (future)
    code             TEXT,
        -- Agent source code stored directly in DB for v0.9.0 (< 512 KB enforced by API)
        -- Future: move to Supabase Storage and reference via code_path
    status           TEXT        NOT NULL DEFAULT 'draft',
        -- 'draft', 'ready', 'running', 'error'
    trigger_type     TEXT        NOT NULL DEFAULT 'manual',
        -- 'manual', 'schedule', 'webhook'
    schedule         TEXT,
        -- cron expression; non-null only when trigger_type = 'schedule'
    env_encrypted    BYTEA,
        -- AES-256-GCM encrypted JSON object of user-supplied env vars
        -- decrypted only at dispatch, injected into sandbox, never logged
    credit_cap       INTEGER,
        -- max credits allowed per run; null = account balance is the only limit
    sandbox_provider TEXT        NOT NULL DEFAULT 'e2b',
        -- 'e2b' | future: 'fly', 'firecracker' — narrow interface, provider-agnostic
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_run_at      TIMESTAMPTZ,
    UNIQUE (user_id, slug)
);

CREATE INDEX idx_hosted_agents_user   ON hosted_agents(user_id, created_at DESC);
CREATE INDEX idx_hosted_agents_status ON hosted_agents(status);

-- agent_runs is the primary ranking data path for Cloud.
-- Every completed run writes outcome data that feeds the ranking pipeline.
CREATE TABLE agent_runs (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    hosted_agent_id    UUID        NOT NULL REFERENCES hosted_agents(id) ON DELETE CASCADE,
    user_id            UUID        NOT NULL REFERENCES users(id),
    status             TEXT        NOT NULL DEFAULT 'queued',
        -- 'queued', 'running', 'completed', 'failed', 'timeout', 'oom', 'credit_cap', 'cancelled'
    trigger            TEXT        NOT NULL DEFAULT 'manual',
        -- 'manual', 'schedule', 'webhook', 'api'
    sandbox_id         TEXT,
        -- E2B sandbox_id (or future provider's run ID) for correlation / debugging
    started_at         TIMESTAMPTZ,
    completed_at       TIMESTAMPTZ,
    duration_ms        INTEGER,
    exit_code          INTEGER,
    -- Billing
    credits_compute    INTEGER     DEFAULT 0,
        -- ceil(duration_seconds / 60) * 1 credit, deducted at completion
    credits_proxy      INTEGER     DEFAULT 0,
        -- sum of proxy call charges attributed to this run (from credit_transactions)
    credits_total      INTEGER     DEFAULT 0,
        -- credits_compute + credits_proxy
    -- Ranking signal data (first-class, not bolted-on logging)
    services_called    JSONB       DEFAULT '[]',
        -- [{slug, calls, credits_spent, avg_latency_ms}]
    failover_events    INTEGER     DEFAULT 0,
    substitutions      JSONB       DEFAULT '[]',
        -- [{from, to, reason, timestamp}]
    error_type         TEXT,
        -- 'timeout', 'oom', 'credit_exhausted', 'code_error', 'sandbox_error'
    error_message      TEXT,
    -- Logs
    log_tail           TEXT,
        -- last 4 KB of stdout+stderr for dashboard display
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_agent_runs_agent     ON agent_runs(hosted_agent_id, created_at DESC);
CREATE INDEX idx_agent_runs_user      ON agent_runs(user_id, created_at DESC);
CREATE INDEX idx_agent_runs_active    ON agent_runs(status) WHERE status IN ('queued', 'running');
