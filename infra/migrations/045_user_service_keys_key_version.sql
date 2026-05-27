-- Migration 045: add key_version to user_service_keys for BYOK rotation parity
ALTER TABLE user_service_keys
    ADD COLUMN IF NOT EXISTS key_version INTEGER NOT NULL DEFAULT 1;
