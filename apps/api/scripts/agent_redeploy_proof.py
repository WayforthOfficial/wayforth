"""agent_redeploy_proof.py — code-editing v1, Step 4: real-DB proof of the orchestration.

Runs the REAL services.agent_redeploy.redeploy + routers.cloud._resolve_dispatch against a
live Postgres (point DATABASE_URL at a throwaway DB) with an injected fake build_fn — no
E2B/mirror needed. Proves the guarantees the SQL layer is responsible for:

  A. build failure → prior active version still serving (active pointer UNMOVED).
  B. in-flight isolation → a run dispatched before an edit stays bound to its old version
     (agent_runs.version_id), a run dispatched after gets the new one.
  C. forward-only activate → a slow OLDER build cannot clobber a newer already-active version.

Usage: DATABASE_URL=postgres://… AGENT_VERSIONED_DISPATCH_ENABLED=1 python scripts/agent_redeploy_proof.py
"""
import asyncio
import json
import os

import asyncpg

from services.agent_redeploy import redeploy, RedeployError
from routers.cloud import _resolve_dispatch

SCHEMA = """
CREATE TABLE hosted_agents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), name TEXT, runtime TEXT DEFAULT 'python3.12',
  code TEXT, params_schema JSONB, active_version_id UUID,
  created_at TIMESTAMPTZ DEFAULT NOW(), updated_at TIMESTAMPTZ DEFAULT NOW());
CREATE TABLE agent_versions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), agent_id UUID NOT NULL, version_no INT NOT NULL,
  files JSONB NOT NULL, requirements JSONB, params_schema JSONB, image_ref TEXT,
  status TEXT NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(agent_id, version_no));
CREATE TABLE agent_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(), agent_id UUID, version_id UUID, status TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW());
"""

CODE = "print('hello')"
results = []


def check(name, ok):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")


async def fake_build_ok(**k):
    return f"img-v{k['version']['version_no']}"


async def fake_build_fail(**k):
    raise RuntimeError("install_failed: simulated bad build")


async def dispatch_bind(pool, agent, run_status="running"):
    """Mirror what _execute_run does: resolve the version once + stamp agent_runs.version_id."""
    d = await _resolve_dispatch(pool, agent)
    async with pool.acquire() as c:
        rid = await c.fetchval(
            "INSERT INTO agent_runs (agent_id, version_id, status) VALUES ($1::uuid,$2,$3) RETURNING id",
            str(agent["id"]), d["version_id"], run_status)
    return str(rid), d["version_id"]


async def main():
    pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=4)
    async with pool.acquire() as c:
        await c.execute(SCHEMA)
        agent_id = await c.fetchval(
            "INSERT INTO hosted_agents (name, runtime, code) VALUES ('a','python3.12',$1) RETURNING id",
            CODE)
        # seed v1 active (mirrors the migration backfill)
        v1 = await c.fetchval(
            "INSERT INTO agent_versions (agent_id, version_no, files, status) "
            "VALUES ($1, 1, $2, 'active') RETURNING id", agent_id, json.dumps({"agent.py": CODE}))
        await c.execute("UPDATE hosted_agents SET active_version_id=$1 WHERE id=$2", v1, agent_id)
    agent = {"id": str(agent_id), "runtime": "python3.12"}
    v1 = str(v1)

    # ── A. build failure → pointer UNMOVED ───────────────────────────────────────
    print("A. build failure leaves prior active version serving")
    try:
        await redeploy(pool, agent, {"agent.py": CODE}, "", build_fn=fake_build_fail)
        check("redeploy raises on build failure", False)
    except RedeployError as e:
        check("redeploy raises RedeployError(build)", e.stage == "build")
    async with pool.acquire() as c:
        active = str(await c.fetchval("SELECT active_version_id FROM hosted_agents WHERE id=$1", agent_id))
        failed = await c.fetchval("SELECT count(*) FROM agent_versions WHERE agent_id=$1 AND status='failed'", agent_id)
    check("active pointer still v1 (agent not broken)", active == v1)
    check("failed build recorded as a 'failed' version", failed == 1)

    # ── B. in-flight isolation via version_id binding ────────────────────────────
    print("B. in-flight isolation (run before edit stays on old version)")
    r1, r1_ver = await dispatch_bind(pool, agent)            # dispatched BEFORE the edit → binds v1
    out = await redeploy(pool, agent, {"agent.py": CODE + "\n# edit"}, "", build_fn=fake_build_ok)
    v2 = out["version_id"]
    async with pool.acquire() as c:
        active = str(await c.fetchval("SELECT active_version_id FROM hosted_agents WHERE id=$1", agent_id))
    check("edit activated v2", active == v2 and out["activated"])
    r2, r2_ver = await dispatch_bind(pool, agent)            # dispatched AFTER the edit → binds v2
    check("run R1 (pre-edit) still bound to v1", r1_ver == v1)
    check("run R2 (post-edit) bound to v2", r2_ver == v2)
    check("the two runs resolved DIFFERENT versions", r1_ver != r2_ver)

    # ── C. forward-only activate (older build can't clobber newer) ───────────────
    print("C. forward-only activate")
    async with pool.acquire() as c:
        # current active is v2 (version_no=2). Forge an older built version (version_no=2's
        # sibling at a lower number is impossible; instead make a higher-active then an older one)
        vhi = await c.fetchval(
            "INSERT INTO agent_versions (agent_id, version_no, files, image_ref, status) "
            "VALUES ($1, 9, $2, 'img-v9', 'active') RETURNING id", agent_id, json.dumps({"agent.py": CODE}))
        await c.execute("UPDATE hosted_agents SET active_version_id=$1 WHERE id=$2", vhi, agent_id)
        vlo = await c.fetchval(
            "INSERT INTO agent_versions (agent_id, version_no, files, image_ref, status) "
            "VALUES ($1, 8, $2, 'img-v8', 'building') RETURNING id", agent_id, json.dumps({"agent.py": CODE}))
        # the forward-only activate (same SQL the orchestrator uses) for the OLDER version
        moved = await c.fetchval(
            """UPDATE hosted_agents h SET active_version_id=$2::uuid
               WHERE h.id=$1::uuid
                 AND (h.active_version_id IS NULL OR
                      (SELECT version_no FROM agent_versions WHERE id=h.active_version_id) < $3)
               RETURNING h.active_version_id""",
            str(agent_id), str(vlo), 8)
        active = str(await c.fetchval("SELECT active_version_id FROM hosted_agents WHERE id=$1", agent_id))
    check("older build (v8) did NOT clobber newer active (v9)", moved is None and active == str(vhi))

    await pool.close()
    ok = all(v for _, v in results)
    print(f"\n{'ALL PROPERTIES PROVEN ✓' if ok else 'FAILURES ✗'}  ({sum(v for _,v in results)}/{len(results)})")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
