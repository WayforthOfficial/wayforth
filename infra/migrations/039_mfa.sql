-- 039_mfa.sql — TOTP MFA for developer, provider, and admin accounts

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS mfa_secret        TEXT,
    ADD COLUMN IF NOT EXISTS mfa_enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS mfa_backup_codes  TEXT[],
    ADD COLUMN IF NOT EXISTS mfa_enabled_at    TIMESTAMPTZ;

ALTER TABLE providers
    ADD COLUMN IF NOT EXISTS mfa_secret        TEXT,
    ADD COLUMN IF NOT EXISTS mfa_enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS mfa_backup_codes  TEXT[],
    ADD COLUMN IF NOT EXISTS mfa_enabled_at    TIMESTAMPTZ;

ALTER TABLE admin_users
    ADD COLUMN IF NOT EXISTS mfa_secret        TEXT,
    ADD COLUMN IF NOT EXISTS mfa_enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS mfa_backup_codes  TEXT[],
    ADD COLUMN IF NOT EXISTS mfa_enabled_at    TIMESTAMPTZ;

-- Short-lived challenge tokens issued during login when MFA is required
CREATE TABLE IF NOT EXISTS mfa_challenges (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_type   TEXT        NOT NULL CHECK (user_type IN ('user', 'provider', 'admin')),
    user_id     UUID        NOT NULL,
    token_hash  TEXT        NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL,
    used        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mfa_challenges_token_hash ON mfa_challenges (token_hash);
CREATE INDEX IF NOT EXISTS idx_mfa_challenges_expires_at ON mfa_challenges (expires_at);
