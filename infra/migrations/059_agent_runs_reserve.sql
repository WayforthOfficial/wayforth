-- Migration 059: add pre-reservation tracking columns to agent_runs
--
-- Pre-reserve model (approved in isolation architecture):
--   credits_reserved  — deducted from balance at dispatch (= credit_cap if set, else 0)
--   credits_released  — refunded at completion (= reserved - actual_spend if reserved > 0)
--   credits_spent     — alias for credits_total (compute + proxy), for checklist clarity
--
-- credit_transactions evidence:
--   dispatch  → type='agent_reserve', amount=-reserved, agent_id=run_id
--   proxy     → type='execution', deducted normally during run
--   compute   → type='cloud_compute', deducted at completion
--   release   → type='agent_release', amount=+released, agent_id=run_id  (if released > 0)

ALTER TABLE agent_runs
    ADD COLUMN IF NOT EXISTS credits_reserved INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS credits_released INTEGER NOT NULL DEFAULT 0;
