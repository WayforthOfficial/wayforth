-- Migration 033: add slug column to services for stable, developer-friendly identifiers

ALTER TABLE services
  ADD COLUMN IF NOT EXISTS slug TEXT;

-- Generate slug from name:
-- "DeepL API" → "deepl"
-- "Airtable API" → "airtable"
-- "Twelve Data API" → "twelve_data"
-- Rule: strip trailing noise words, lowercase, non-alphanumeric → underscore
UPDATE services
SET slug = LOWER(
  REGEXP_REPLACE(
    REGEXP_REPLACE(
      TRIM(name),
      '\s*(api|the|platform|service)\s*$', '', 'gi'
    ),
    '[^a-z0-9]+', '_', 'g'
  )
);

-- Remove leading/trailing underscores left by the regex
UPDATE services
SET slug = TRIM(BOTH '_' FROM slug);

-- Unique index (partial, allows future NULLs if needed)
CREATE UNIQUE INDEX IF NOT EXISTS services_slug_idx ON services(slug)
  WHERE slug IS NOT NULL;
