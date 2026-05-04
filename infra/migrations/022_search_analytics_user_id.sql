-- Add user_id to search_analytics so authenticated searches can be linked to accounts
ALTER TABLE search_analytics
    ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_search_analytics_user_id
    ON search_analytics(user_id);
