-- Fix: subscription_status defaulted to 'active' for all api_keys, even before Stripe existed.
-- Change the default to NULL so only real Stripe subscribers have a non-null status.
ALTER TABLE api_keys ALTER COLUMN subscription_status SET DEFAULT NULL;

-- Backfill: clear 'active' status on rows with no stripe_subscription_id
-- (these are pre-Stripe accounts that got the default, not real subscribers)
UPDATE api_keys
SET subscription_status = NULL
WHERE subscription_status = 'active'
  AND stripe_subscription_id IS NULL;
