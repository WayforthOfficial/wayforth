-- Migration 045 (v0.8.2): soft-delete flag for provider-managed services.
-- DELETE /provider/services/{slug} sets active=FALSE so the service stops
-- surfacing in /search and fallback while its catalog row and WayforthRank
-- signal history are preserved. Defaults TRUE so all existing rows stay live.
ALTER TABLE services
  ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_services_active ON services (active) WHERE active = FALSE;
