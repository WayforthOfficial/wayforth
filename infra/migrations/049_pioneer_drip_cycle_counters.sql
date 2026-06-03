-- Migration 049: per-cycle pioneer drip counters on users table.
-- pioneer_drip_credits_this_cycle and pioneer_drip_days_this_cycle reset to 0
-- on each subscription renewal (in _monthly_topup_reset) and increment on each
-- successful daily drip (in run_pioneer_drip). Lifetime days enrolled is derived
-- at query time from pioneer_opted_in_at — never stored, never drifts.
ALTER TABLE users
    ADD COLUMN IF NOT EXISTS pioneer_drip_credits_this_cycle  INT  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pioneer_drip_days_this_cycle     INT  NOT NULL DEFAULT 0;
