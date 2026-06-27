-- 070_agent_run_version.sql — code-editing v1, Step 4: bind each run to its version.
--
-- Records which agent_version a run executed. This is the in-flight isolation anchor:
-- dispatch resolves the active version ONCE at start and stamps version_id here, so a
-- later edit (which repoints active_version_id) cannot change a running run's binding.
-- Additive + nullable; dispatch is unchanged until AGENT_VERSIONED_DISPATCH_ENABLED flips.

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS version_id UUID REFERENCES agent_versions(id);

CREATE INDEX IF NOT EXISTS idx_agent_runs_version ON agent_runs(version_id);
