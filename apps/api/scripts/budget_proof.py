"""scripts/budget_proof.py — forced four-step proof of loop budgets vs a real DB.

Gated on RUN_BUDGET_PROOF_DSN. Unset → skips (exit 0), so it's inert outside the
dedicated CI job. Set it to an ephemeral Postgres DSN and it applies migration 066
and exercises the REAL money-path SQL — run_budgets upsert, credit_transactions
writes tagged with run_id, the ledger SUM, and the pre-spend enforcement in
_run_core — through the actual /run/budgets + /run handlers.

Real: the DB, migration 066, and all budget/ledger SQL. Stubbed ONLY the non-DB
boundaries (auth resolution, service selection/ranker, the upstream LLM call) —
never the budget logic. Exits non-zero if any of the four steps fails, incl. the
tightened step-4 invariant (402 run_budget_exhausted AND spent unchanged — the
rejected call left no debit, i.e. refused before deduct).

CI: .github/workflows/tests.yml :: budget-proof (Postgres service container).
Local: RUN_BUDGET_PROOF_DSN=postgresql://postgres@127.0.0.1:5432/proof \
       uv run python scripts/budget_proof.py
"""
import asyncio
import json
import os
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))          # apps/api/scripts
APP_ROOT = os.path.dirname(HERE)                            # apps/api
REPO_ROOT = os.path.dirname(os.path.dirname(APP_ROOT))     # repo root
MIG_066 = os.path.join(REPO_ROOT, "infra/migrations/066_run_budgets.sql")
sys.path.insert(0, APP_ROOT)

DSN = os.environ.get("RUN_BUDGET_PROOF_DSN")
if not DSN:
    print("[skip] RUN_BUDGET_PROOF_DSN not set — loop-budget proof is inert here.")
    raise SystemExit(0)

import asyncpg
from cryptography.fernet import Fernet
from fastapi import HTTPException

os.environ.setdefault("ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("ENVIRONMENT", "development")
os.environ["GROQ_API_KEY"] = "proof-key"

# Minimal prerequisite schema (the tables 066 + the money path reference); shapes
# mirror the real migrations. The ephemeral DB starts empty, so this is self-contained.
PREREQ = [
    """CREATE TABLE IF NOT EXISTS users (
         id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         email TEXT, created_at TIMESTAMPTZ DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS user_credits (
         user_id UUID PRIMARY KEY REFERENCES users(id),
         credits_balance BIGINT NOT NULL DEFAULT 0,
         pioneer_credits_balance BIGINT NOT NULL DEFAULT 0,
         lifetime_credits BIGINT NOT NULL DEFAULT 0,
         package_tier TEXT DEFAULT 'free',
         updated_at TIMESTAMPTZ DEFAULT NOW())""",
    """CREATE TABLE IF NOT EXISTS credit_transactions (
         id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
         user_id UUID NOT NULL REFERENCES users(id),
         amount BIGINT NOT NULL, balance_after BIGINT NOT NULL,
         type TEXT NOT NULL, description TEXT, api_endpoint TEXT, service_id TEXT,
         agent_id TEXT, api_key_id UUID, payment_tx_hash TEXT,
         created_at TIMESTAMPTZ DEFAULT NOW())""",
]

U = str(uuid.uuid4())
API_KEY_ID = str(uuid.uuid4())
KEY_HEADER = "wf_live_" + "x" * 43
INTENT = "chat hello"
INPUT = {"messages": [{"role": "user", "content": "hi"}]}


class FakeReq:
    def __init__(self, body, key=KEY_HEADER):
        self._b = body
        self.headers = {"X-Wayforth-API-Key": key}
        self.state = type("S", (), {"request_id": "proof"})()

    async def json(self):
        return self._b


class FakeResp:
    def __init__(self):
        self.headers = {}


def _raw(label, method, path, body, status, resp):
    print(f"\n{'─'*72}\n{label}")
    print(f"  → REQUEST   {method} {path}")
    print(f"    headers   X-Wayforth-API-Key: {KEY_HEADER[:11]}…")
    if body is not None:
        print(f"    body      {json.dumps(body)}")
    print(f"  ← RESPONSE  HTTP {status}")
    print(f"    body      {json.dumps(resp, default=str)}")


async def call(label, fn, *, method, path, body, **kw):
    try:
        resp = await fn(**kw)
        status, payload = 200, resp
    except HTTPException as e:
        status, payload = e.status_code, e.detail
    _raw(label, method, path, body, status, payload)
    return status, payload


async def main() -> int:
    conn = await asyncpg.connect(DSN)
    try:
        for stmt in PREREQ:
            await conn.execute(stmt)
        with open(MIG_066) as f:
            await conn.execute(f.read())   # migration 066, applied verbatim
        run_id_col = await conn.fetch(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='credit_transactions' AND column_name='run_id'")
        has_budgets = await conn.fetchval("SELECT to_regclass('run_budgets') IS NOT NULL")
        print(f"[schema] 066 applied: credit_transactions.run_id={'present' if run_id_col else 'MISSING'}, "
              f"run_budgets={'present' if has_budgets else 'MISSING'}")

        await conn.execute("INSERT INTO users (id, email) VALUES ($1::uuid, 'proof@local')", U)
        await conn.execute(
            "INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier) "
            "VALUES ($1::uuid, 1000, 1000, 'growth')", U)
        print(f"[seed] user={U} credits_balance=1000")

        import ranker_client
        import routers.execute as ex
        from main import app
        app.state.pool = object()

        async def fake_resolve(db, key): return U, API_KEY_ID, "growth"
        async def fake_rate(*a, **k): return None
        async def fake_exec(slug, params, key): return ({"content": "hi"}, None, 5)
        async def fake_incr(*a, **k): return None
        async def noop(*a, **k): return None

        async def fixed_select(db, intent, input, prefs):
            # Selection/ranker are not under test — fix groq @ 3 credits; everything
            # downstream (budget gate, deduct, ledger SUM) runs for real on Postgres.
            return ex._RunSelection(
                selected_slug="groq",
                selected_svc={"wri_score": 80.0, "category": "inference"},
                selected_rank=1, mapped_params=INPUT, credit_cost=3, svc_key="proof-key",
                top5=[], ranked=[], compatible_cats=None, input_dict=input or {})

        ex._resolve_user = fake_resolve
        ex.check_rate_limit = fake_rate
        ex._run_select = fixed_select
        ex._try_execute_managed = fake_exec
        ex._increment_calls = fake_incr
        ex._update_search_signal = noop
        ex._maybe_dispatch_credits_low = noop
        ex._check_spend_anomaly = noop
        ex._patch_tx_signals = noop
        ex._fetch_wri = noop
        ranker_client.rank_services = lambda *a, **k: []

        async def run(body):
            ex._RUN_CACHE.clear()   # bypass the 10s identical-intent cache so each call re-gates
            return await call("POST /run", ex.run_endpoint, method="POST", path="/run",
                              body=body, request=FakeReq(body), response=FakeResp(), db=conn)

        # STEP 0 — unbudgeted control (cost discovery)
        s, r = await run({"intent": INTENT, "input": INPUT})
        assert s == 200, r
        C = r["service_used"]["credits_used"]
        assert "budget" not in r, "unbudgeted result must carry no budget block"
        print(f"[cost] C = {C} credits/call (unbudgeted control: 200, no budget block)")

        # STEP 1 — create a tiny budget (ceiling == C → exactly one call fits)
        s, r = await call("POST /run/budgets", ex.set_run_budget, method="POST", path="/run/budgets",
                          body={"ceiling": C}, request=FakeReq({"ceiling": C}), db=conn)
        assert s == 200, r
        RID = r["run_id"]
        assert r["ceiling"] == C and r["spent"] == 0 and r["remaining"] == C

        # STEP 2 — one budgeted call: passes, spends exactly C
        s, r = await run({"intent": INTENT, "input": INPUT, "run_id": RID})
        assert s == 200, r
        assert r["budget"]["spent"] == C and r["budget"]["remaining"] == 0
        assert r["budget"]["over_soft_cap"] is False
        cost2 = r["service_used"]["credits_used"]
        if cost2 != C:
            print(f"[NOTE] step-2 selected cost {cost2} != warm-up C {C} — boundary would differ.")

        # STEP 3 — over-cap call: REFUSED before any deduct
        async def _spent():
            return int(await conn.fetchval(
                "SELECT COALESCE(SUM(-amount),0) FROM credit_transactions "
                "WHERE run_id=$1::uuid AND user_id=$2::uuid", RID, U))

        async def _rows():
            return int(await conn.fetchval(
                "SELECT COUNT(*) FROM credit_transactions WHERE run_id=$1::uuid AND user_id=$2::uuid", RID, U))

        spent_before, rows_before = await _spent(), await _rows()
        s, r = await run({"intent": INTENT, "input": INPUT, "run_id": RID})
        spent_after, rows_after = await _spent(), await _rows()

        # STEP 4 — ledger truth
        s4, r4 = await call("GET /run/budgets/{run_id}", ex.get_run_budget, method="GET",
                            path=f"/run/budgets/{RID}", body=None, request=FakeReq(None), run_id=RID, db=conn)

        print(f"\n{'═'*72}\nINVARIANTS\n{'═'*72}")
        ok = []

        def check(name, cond):
            ok.append(bool(cond))
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

        check("step3 over-cap call returned HTTP 402", s == 402)
        check("step3 error == 'run_budget_exhausted'", isinstance(r, dict) and r.get("error") == "run_budget_exhausted")
        check("refused BEFORE deduct — ledger row count unchanged", rows_after == rows_before == 1)
        check("refused BEFORE deduct — ledger spent unchanged", spent_after == spent_before == C)
        check(f"ledger-derived spent == C ({C})", spent_after == C)
        check("GET budget spent matches ledger SUM", s4 == 200 and r4["spent"] == spent_after == C)
        check("GET budget remaining == 0", s4 == 200 and r4["remaining"] == 0)

        ledger = await conn.fetch(
            "SELECT amount, type, service_id, run_id FROM credit_transactions WHERE run_id=$1::uuid ORDER BY created_at", RID)
        print(f"\n  ledger rows for run {RID}:")
        for row in ledger:
            print(f"    amount={row['amount']} type={row['type']} service={row['service_id']} run_id={row['run_id']}")
        print(f"  Σ(-amount) over run = {spent_after}  (ceiling C={C})")

        print(f"\n{'═'*72}")
        print("ALL FOUR STEPS HOLD ✅" if all(ok) else "PROOF FAILED ❌")
        print('═'*72)
        return 0 if all(ok) else 1
    finally:
        await conn.close()


raise SystemExit(asyncio.run(main()))
