-- Migration 052: canonical email for alias-farming defense (FINDING-011).
-- Stores a normalized email (plus-suffix stripped, Gmail dots removed) used
-- ONLY for uniqueness / Launch-Boost-eligibility checks. The original email
-- column is unchanged and remains what we display.
--
-- No UNIQUE constraint is added: pre-existing alias duplicates would make that
-- fail, and we only need to block NEW collisions, which the app enforces by
-- querying email_canonical. A non-unique index keeps those lookups fast.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email_canonical TEXT;
ALTER TABLE providers
    ADD COLUMN IF NOT EXISTS email_canonical TEXT;

-- Backfill existing rows: lower, strip +suffix, drop dots for gmail/googlemail.
UPDATE users
   SET email_canonical = (
       CASE
         WHEN split_part(lower(email), '@', 2) IN ('gmail.com', 'googlemail.com')
           THEN replace(split_part(split_part(lower(email), '@', 1), '+', 1), '.', '')
         ELSE split_part(split_part(lower(email), '@', 1), '+', 1)
       END
       || '@' || split_part(lower(email), '@', 2)
   )
 WHERE email IS NOT NULL AND email_canonical IS NULL;

UPDATE providers
   SET email_canonical = (
       CASE
         WHEN split_part(lower(email), '@', 2) IN ('gmail.com', 'googlemail.com')
           THEN replace(split_part(split_part(lower(email), '@', 1), '+', 1), '.', '')
         ELSE split_part(split_part(lower(email), '@', 1), '+', 1)
       END
       || '@' || split_part(lower(email), '@', 2)
   )
 WHERE email IS NOT NULL AND email_canonical IS NULL;

CREATE INDEX IF NOT EXISTS idx_users_email_canonical     ON users (email_canonical);
CREATE INDEX IF NOT EXISTS idx_providers_email_canonical ON providers (email_canonical);
