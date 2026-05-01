CREATE TABLE IF NOT EXISTS user_service_keys (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    service_slug    TEXT NOT NULL,
    service_name    TEXT NOT NULL,
    encrypted_key   TEXT NOT NULL,
    key_preview     TEXT NOT NULL,
    total_calls     INTEGER NOT NULL DEFAULT 0,
    last_used_at    TIMESTAMP WITH TIME ZONE,
    active          BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, service_slug)
);

CREATE INDEX IF NOT EXISTS idx_user_service_keys_user_id ON user_service_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_user_service_keys_active ON user_service_keys(user_id, active);
