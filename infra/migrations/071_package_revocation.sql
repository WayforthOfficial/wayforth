-- 071_package_revocation.sql — code-editing v1, Step 5: package revocation flagging.
--
-- When a previously-allowlisted package is later found malicious/vulnerable, we record
-- the revocation and FLAG every agent_version whose baked requirements include it, so
-- affected agents can be rebuilt/reviewed. Revocation also blocks NEW builds from using
-- the package (enforced in the redeploy orchestrator). Additive + reversible.

CREATE TABLE IF NOT EXISTS revoked_packages (
    name        TEXT NOT NULL,
    version     TEXT,                  -- NULL = all versions of the package are revoked
    reason      TEXT,
    revoked_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name, version)
);

-- Flag set on versions whose requirements include a revoked package (set at revoke time).
ALTER TABLE agent_versions
    ADD COLUMN IF NOT EXISTS dep_flagged BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_agent_versions_dep_flagged
    ON agent_versions(agent_id) WHERE dep_flagged;
