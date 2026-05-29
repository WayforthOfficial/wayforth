-- Migration 048: pioneer routing signal columns on search_outcomes
-- signal_weight < 1.0 discounts WRI conversion contribution for pioneer-routed calls
-- (pioneer calls are not organic demand — weight them at 0.75 so they don't over-boost WRI).
ALTER TABLE search_outcomes ADD COLUMN IF NOT EXISTS signal_weight   FLOAT   DEFAULT 1.0;
ALTER TABLE search_outcomes ADD COLUMN IF NOT EXISTS pioneer_routed  BOOLEAN DEFAULT FALSE;
