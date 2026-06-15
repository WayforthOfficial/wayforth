-- Migration 060: scheduler + webhook support for hosted agents
--
-- runner_key_encrypted / runner_key_version
--   Fernet-encrypted copy of the owner's Wayforth API key, stored at agent
--   creation time. Decrypted only at sandbox dispatch — never logged.
--   Required for schedule and webhook triggers where no HTTP request carries
--   the key; manual dispatch re-uses the caller's header key instead.
--
-- next_run_at
--   Pre-computed next fire time for scheduled agents. Scheduler queries
--   WHERE trigger_type='schedule' AND next_run_at <= NOW(). Updated to the
--   next occurrence after every dispatch (successful or skipped).
--
-- concurrent_max
--   Per-agent ceiling on simultaneous queued/running runs. Scheduler and
--   manual dispatch both enforce this. Default 1 (one run at a time).
--
-- webhook_id
--   Unguessable UUID that acts as the per-agent webhook secret.
--   POST /cloud/webhooks/{webhook_id} triggers a run without requiring the
--   owner's API key in the request — the webhook_id IS the credential.
--   Generated on agent creation; rotation via PATCH /cloud/agents/{id}.

ALTER TABLE hosted_agents
    ADD COLUMN IF NOT EXISTS runner_key_encrypted TEXT,
    ADD COLUMN IF NOT EXISTS runner_key_version    INTEGER  DEFAULT 1,
    ADD COLUMN IF NOT EXISTS next_run_at           TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS concurrent_max        INTEGER  NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS webhook_id            UUID     DEFAULT gen_random_uuid();

-- Unique index so GET /cloud/webhooks/{webhook_id} is O(1)
CREATE UNIQUE INDEX IF NOT EXISTS idx_hosted_agents_webhook_id
    ON hosted_agents(webhook_id)
    WHERE webhook_id IS NOT NULL;

-- Partial index for the scheduler's "find due agents" query
CREATE INDEX IF NOT EXISTS idx_hosted_agents_next_run
    ON hosted_agents(next_run_at)
    WHERE trigger_type = 'schedule' AND next_run_at IS NOT NULL;
