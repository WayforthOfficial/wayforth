ALTER TABLE search_analytics
    ADD COLUMN IF NOT EXISTS top_result_slug TEXT,
    ADD COLUMN IF NOT EXISTS top_result_wri INT,
    ADD COLUMN IF NOT EXISTS results_count INT,
    ADD COLUMN IF NOT EXISTS clicked_slug TEXT,
    ADD COLUMN IF NOT EXISTS payment_followed BOOLEAN DEFAULT FALSE;
