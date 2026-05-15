-- Remove first-party "Wayforth Labs Summarizer" from general catalog.
-- It is a first-party service (not a third-party API integration) and should
-- not appear alongside external managed services in the public catalog.
DELETE FROM services
WHERE slug = 'wayforth_labs_summarizer'
   OR (endpoint_url ILIKE '%labs-production%' AND name ILIKE '%summarizer%');
