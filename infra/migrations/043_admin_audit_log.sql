-- 043_admin_audit_log.sql
--
-- v0.8.0 Item 4: append-only admin audit log.
--
-- v0.7.8 added `logger.warning("ADMIN_ACTION ...")` at every mutating admin
-- endpoint, but application logs are mutable and rotated. This table is the
-- tamper-resistant system of record.
--
-- The triggers below enforce append-only at the DB level: UPDATE and DELETE
-- both raise. A separate `wayforth_app_user` DB role + REVOKE could be added
-- later, but the trigger works for any role today and ships with the rest of
-- the v0.8.0 hardening.
--
-- admin_id references admin_users.id (the admin who took the action),
-- target_user_id references users.id (the customer whose state changed),
-- which is the standard shape for admin-vs-customer separation in this repo.

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_id         UUID NOT NULL REFERENCES admin_users(id),
    admin_email      TEXT NOT NULL,
    action           TEXT NOT NULL,
    target_user_id   UUID REFERENCES users(id),
    target_resource  TEXT,
    payload          JSONB,
    ip_address       TEXT,
    user_agent       TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS admin_audit_log_created_idx
    ON admin_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS admin_audit_log_admin_idx
    ON admin_audit_log(admin_id, created_at DESC);
CREATE INDEX IF NOT EXISTS admin_audit_log_action_idx
    ON admin_audit_log(action, created_at DESC);

CREATE OR REPLACE FUNCTION admin_audit_log_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'admin_audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS admin_audit_log_no_update ON admin_audit_log;
CREATE TRIGGER admin_audit_log_no_update
    BEFORE UPDATE ON admin_audit_log
    FOR EACH ROW EXECUTE FUNCTION admin_audit_log_append_only();

DROP TRIGGER IF EXISTS admin_audit_log_no_delete ON admin_audit_log;
CREATE TRIGGER admin_audit_log_no_delete
    BEFORE DELETE ON admin_audit_log
    FOR EACH ROW EXECUTE FUNCTION admin_audit_log_append_only();
