-- Expand category CHECK to include new service types
ALTER TABLE services DROP CONSTRAINT IF EXISTS services_category_check;
ALTER TABLE services ADD CONSTRAINT services_category_check
  CHECK (category IN ('inference', 'data', 'translation', 'image', 'code', 'audio', 'embeddings'));
