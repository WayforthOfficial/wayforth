-- Allow 'growth' as a valid API key tier (credits-based plan with 300 RPM).
-- The check constraint previously only allowed: free, starter, pro, enterprise.
ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_tier_check;
ALTER TABLE api_keys ADD CONSTRAINT api_keys_tier_check
    CHECK (tier = ANY (ARRAY['free'::text, 'starter'::text, 'pro'::text, 'growth'::text, 'enterprise'::text]));
