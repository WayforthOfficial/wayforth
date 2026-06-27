-- 069_agent_versions.sql — code-editing v1, Step 3: multi-file model + versioning.
--
-- Pure data layer. Adds an immutable per-version record (files + requirements +
-- params_schema + the built image_ref) and an active-version pointer on hosted_agents.
-- Dispatch still reads hosted_agents.code and is UNCHANGED — nothing reads
-- active_version_id/agent_versions yet (Step 4 wires the redeploy to use them).
--
-- Reversible: DROP the column + table; hosted_agents.code is never touched, so the
-- backfill is purely additive and removable.

CREATE TABLE IF NOT EXISTS agent_versions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      UUID NOT NULL REFERENCES hosted_agents(id) ON DELETE CASCADE,
    version_no    INTEGER NOT NULL,
    files         JSONB NOT NULL,        -- {path: content}; entrypoint agent.py (py) / agent.ts (node)
    requirements  JSONB,                 -- pinned deps for the build; NULL = none
    params_schema JSONB,                 -- schema extracted from the entrypoint for this version
    image_ref     TEXT,                  -- per-version snapshot ref (Step 1 build); NULL until built
    status        TEXT NOT NULL DEFAULT 'active',  -- building | active | failed
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_id, version_no)
);

CREATE INDEX IF NOT EXISTS idx_agent_versions_agent
    ON agent_versions(agent_id, version_no DESC);

ALTER TABLE hosted_agents
    ADD COLUMN IF NOT EXISTS active_version_id UUID REFERENCES agent_versions(id);

-- Backfill: each existing agent's single-file code -> one v1 version row. Entrypoint
-- filename is chosen by runtime so node agents keep agent.ts. params_schema carried
-- over; no requirements. hosted_agents.code is left untouched -> dispatch is identical.
-- Idempotent (NOT EXISTS guard + active_version_id IS NULL), so check_db can re-run it.
INSERT INTO agent_versions (agent_id, version_no, files, params_schema, status, created_at)
SELECT h.id, 1,
       jsonb_build_object(
           CASE WHEN h.runtime LIKE 'node%' THEN 'agent.ts' ELSE 'agent.py' END,
           COALESCE(h.code, '')),
       h.params_schema,
       'active',
       COALESCE(h.created_at, NOW())
FROM hosted_agents h
WHERE NOT EXISTS (SELECT 1 FROM agent_versions av WHERE av.agent_id = h.id);

UPDATE hosted_agents h
SET active_version_id = av.id
FROM agent_versions av
WHERE av.agent_id = h.id AND av.version_no = 1 AND h.active_version_id IS NULL;
