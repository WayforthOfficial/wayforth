-- Migration 053: self-serve account deletion with grace period (FINDING-016).
-- deletion_scheduled_at marks an account for hard deletion. DELETE /account sets
-- it to NOW()+24h and immediately revokes access (keys inactive, sessions
-- killed); a reaper hard-deletes rows once the grace window elapses. A user can
-- cancel within the window via POST /account/undelete.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS deletion_scheduled_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_users_deletion_scheduled
    ON users (deletion_scheduled_at)
    WHERE deletion_scheduled_at IS NOT NULL;
