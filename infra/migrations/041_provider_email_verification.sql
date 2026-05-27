-- 041_provider_email_verification.sql
--
-- v0.8.0 Item 2: provider email verification.
--
-- Adds three columns to the providers table to support a tokenised
-- email-verification step on registration. New providers start with
-- email_verified=false and can only invoke write endpoints
-- (POST /provider/verify, POST /provider/billing/upgrade) once they have
-- clicked the link in their verification email.
--
-- The existing `verified` column is unchanged — that flag tracks DOMAIN
-- ownership (DNS TXT / response header verification), which is a separate
-- and orthogonal trust signal.
--
-- The partial index speeds up the GET /provider/verify-email lookup
-- (lookup by token) and stays small because cleared tokens are NULL.

ALTER TABLE providers
    ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT false,
    ADD COLUMN IF NOT EXISTS email_verification_token TEXT,
    ADD COLUMN IF NOT EXISTS email_verification_sent_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS providers_email_verification_token_idx
    ON providers(email_verification_token)
    WHERE email_verification_token IS NOT NULL;
