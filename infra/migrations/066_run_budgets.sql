-- 066: Loop-aware spend budgets.
--
-- A developer attaches a credit ceiling to a logical run (a loop / session id).
-- Every external call Wayforth makes on that run's behalf — across iterations,
-- services, and rails — decrements against the run's remaining budget; the next
-- call that would exceed the ceiling is refused BEFORE any deduct, with a typed
-- budget-exhausted error.
--
-- `spent` is ALWAYS ledger-derived: SUM(-amount) over credit_transactions tagged
-- with run_id (scoped to the owning user). It is never a denormalized counter —
-- a counter drifts; the ledger is the source of truth (same rule as
-- credits_used_this_cycle / check_run_credit_cap).
--
-- Runs with NO row in run_budgets are UNBUDGETED and behave exactly as before:
-- credit_transactions.run_id is NULL and no ceiling is consulted.
--
-- Mirrored idempotently in apps/api/main.py check_db().

BEGIN;

-- Tag every ledger row with the run it belongs to. NULL = unbudgeted (unchanged).
-- Nullable ADD COLUMN is a metadata-only change on PG11+ (no table rewrite).
ALTER TABLE credit_transactions
    ADD COLUMN IF NOT EXISTS run_id UUID;

-- Hot path: SUM(-amount) WHERE run_id=$1 AND user_id=$2. Partial index — costs
-- nothing for the unbudgeted (run_id IS NULL) majority of rows.
CREATE INDEX IF NOT EXISTS credit_transactions_run_id_idx
    ON credit_transactions (run_id, user_id)
    WHERE run_id IS NOT NULL;

-- One budget per run. ceiling is mandatory — there is NO uncapped mode.
--   soft_cap = false → hard stop at ceiling (default).
--   soft_cap = true  → allow crossing ceiling (the call is flagged), then hard
--                      stop at ceiling + max_overage.
CREATE TABLE IF NOT EXISTS run_budgets (
    run_id       UUID        PRIMARY KEY,
    user_id      UUID        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ceiling      INTEGER     NOT NULL CHECK (ceiling > 0),
    soft_cap     BOOLEAN     NOT NULL DEFAULT FALSE,
    max_overage  INTEGER     NOT NULL DEFAULT 0 CHECK (max_overage >= 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- soft_cap requires a positive overage; hard mode forbids one (it would be
    -- meaningless). This makes an "uncapped" budget unrepresentable.
    CONSTRAINT run_budget_overage_consistent CHECK (
        (soft_cap AND max_overage > 0) OR (NOT soft_cap AND max_overage = 0)
    )
);

CREATE INDEX IF NOT EXISTS run_budgets_user_idx ON run_budgets (user_id);

COMMIT;
