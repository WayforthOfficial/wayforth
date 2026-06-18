-- 063_tier3_provider_link.sql
-- Tier 3 application moves out of the public standalone form and into the
-- verified-provider dashboard. Bind each application to the submitting provider
-- and one of their registered services (POST /provider/tier3/apply).
--
-- Additive + idempotent. The columns are mirrored in main.py startup
-- (ALTER TABLE IF EXISTS ... ADD COLUMN IF NOT EXISTS) so they self-apply on
-- deploy; this file is the canonical record and also backfills historical rows.

BEGIN;

ALTER TABLE IF EXISTS tier3_applications
    ADD COLUMN IF NOT EXISTS provider_id  UUID REFERENCES providers(id),
    ADD COLUMN IF NOT EXISTS service_slug TEXT;

-- Backfill: link existing email-keyed applications to a provider by matching the
-- application's contact_email to the provider's email. Rows with no matching
-- provider are left as historical (provider_id stays NULL).
UPDATE tier3_applications t
SET provider_id = p.id
FROM providers p
WHERE t.provider_id IS NULL
  AND lower(t.contact_email) = lower(p.email);

CREATE INDEX IF NOT EXISTS idx_tier3_applications_provider
    ON tier3_applications (provider_id)
    WHERE provider_id IS NOT NULL;

COMMIT;
