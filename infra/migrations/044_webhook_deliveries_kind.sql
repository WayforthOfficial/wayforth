-- 044_webhook_deliveries_kind.sql
--
-- v0.8.0 Item 5: WRI alert retry queue (reuse webhook_deliveries).
--
-- Today WRI alerts are delivered synchronously by _fire_wri_alerts; a single
-- failed POST drops the alert on the floor with no retry. The generic
-- _webhook_retry_loop in core/credits.py already has the right retry-with-
-- exponential-backoff behaviour for provider webhooks, so the v0.8.0 fix
-- folds WRI alerts into that same machinery rather than duplicating it.
--
-- Schema changes:
--   - kind: 'generic' (existing provider webhooks) or 'wri_alert'.
--   - notify_url + hmac_secret: populated for kind='wri_alert' so the worker
--     doesn't need to JOIN against wri_alerts on every retry, and the row
--     stays valid even if the source alert is later deactivated.
--   - source_id: optional originating row id (wri_alerts.id) for audit /
--     dedup. For kind='generic' webhook_id already captures this.
--
-- webhook_id is already nullable (no NOT NULL constraint), so kind='wri_alert'
-- rows can simply leave it NULL.

ALTER TABLE webhook_deliveries
    ADD COLUMN IF NOT EXISTS kind        TEXT NOT NULL DEFAULT 'generic',
    ADD COLUMN IF NOT EXISTS notify_url  TEXT,
    ADD COLUMN IF NOT EXISTS hmac_secret TEXT,
    ADD COLUMN IF NOT EXISTS source_id   UUID;

-- Pending-delivery index already exists on (next_retry_at, status) and is
-- the right one for the worker query — no kind-specific index needed.
