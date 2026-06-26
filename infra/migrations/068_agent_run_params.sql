-- 068_agent_run_params.sql — agent parameters v1 (Step 3).
--
-- The resolved, validated parameter values used for a specific run (audit / repro /
-- display). Written at dispatch after server-side validation; NULL for runs of agents
-- that declare no params and for scheduled/webhook runs (which pass no form input).
-- Returned only by GET /cloud/agents/{id}/runs/{run_id}, which is already scoped to
-- the agent's owner — same access control as the rest of the run record.

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS params JSONB;
