-- 042_api_key_version.sql
--
-- v0.8.0 Item 3: API key encryption versioning.
--
-- Adds key_version to api_keys so each row records which Fernet key encrypted
-- its `encrypted_key` ciphertext. Without this, a leak of ENCRYPTION_KEY
-- permanently exposes every BYOK key in the table — rotation is impossible
-- because there is no way to decrypt the old ciphertexts.
--
-- Default of 1 means existing rows are treated as encrypted with v1, which
-- matches the runtime fallback in core/auth.py:get_fernet().

ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS key_version INTEGER DEFAULT 1;
