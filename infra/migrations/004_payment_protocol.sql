ALTER TABLE services ADD COLUMN IF NOT EXISTS payment_protocol TEXT DEFAULT 'wayforth';
