-- Expand category CHECK to include 10 new service categories for seed v8
ALTER TABLE services DROP CONSTRAINT IF EXISTS services_category_check;
ALTER TABLE services ADD CONSTRAINT services_category_check
  CHECK (category IN (
    'inference', 'data', 'translation', 'image', 'code', 'audio', 'embeddings',
    'communication', 'location', 'identity', 'payments', 'productivity',
    'devops', 'legal', 'healthcare', 'real_estate', 'social', 'analytics'
  ));
