-- 067_agent_params_schema.sql — agent parameters v1 (Step 2).
--
-- Stores the compiled PARAMS schema an agent declares in its code (single source of
-- truth), extracted statically on code upload. NULL = the agent declares no params,
-- which is the current behavior for every existing agent (backward-compatible).
--
-- Shape (JSONB): {"fields": [...normalized field objects...],
--                 "order": [topo-sorted field names],
--                 "deps":  {field: [fields it depends on]}}

ALTER TABLE hosted_agents
    ADD COLUMN IF NOT EXISTS params_schema JSONB;
