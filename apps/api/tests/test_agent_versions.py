"""test_agent_versions.py — code-editing v1, Step 3 (version data layer).

Pure helpers over agent_versions + hosted_agents.active_version_id, tested with a fake
asyncpg-style db (JSONB comes back as text, like the real driver).
"""
from __future__ import annotations

import json

import pytest

from core import agent_versions as av


class FakeDB:
    """Returns canned results per method; records calls (q, args)."""
    def __init__(self, fetchval=None, fetchrow=None, fetch=None):
        self._fv, self._fr, self._f = fetchval, fetchrow, fetch
        self.calls = []

    async def fetchval(self, q, *a):
        self.calls.append(("fetchval", q, a))
        return self._fv(q, a) if callable(self._fv) else self._fv

    async def fetchrow(self, q, *a):
        self.calls.append(("fetchrow", q, a))
        return self._fr(q, a) if callable(self._fr) else self._fr

    async def fetch(self, q, *a):
        self.calls.append(("fetch", q, a))
        return self._f(q, a) if callable(self._f) else self._f


AGENT = "11111111-1111-4111-8111-111111111111"
VID = "22222222-2222-4222-8222-222222222222"


# ── entrypoint by runtime ───────────────────────────────────────────────────────

@pytest.mark.parametrize("runtime,expected", [
    ("python3.12", "agent.py"), ("python", "agent.py"),
    ("node20", "agent.ts"), ("node", "agent.ts"), (None, "agent.py")])
def test_entrypoint_for_runtime(runtime, expected):
    assert av.entrypoint_for_runtime(runtime) == expected


# ── JSONB parsing (driver returns text) ─────────────────────────────────────────

def test_row_parses_jsonb_text_and_casts_ids():
    raw = {"id": "x", "agent_id": "y", "version_no": 2,
           "files": json.dumps({"agent.py": "print(1)"}),
           "requirements": json.dumps([{"name": "httpx"}]),
           "params_schema": None, "status": "active"}
    d = av._row(raw)
    assert d["files"] == {"agent.py": "print(1)"}
    assert d["requirements"] == [{"name": "httpx"}]
    assert d["params_schema"] is None
    assert d["id"] == "x" and d["agent_id"] == "y"


def test_row_none():
    assert av._row(None) is None


# ── next_version_no ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_next_version_no():
    db = FakeDB(fetchval=3)
    assert await av.next_version_no(db, AGENT) == 3


# ── create_version ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_version_inserts_with_next_no_and_serializes():
    inserted = {"id": VID, "agent_id": AGENT, "version_no": 2,
                "files": json.dumps({"agent.py": "x"}), "requirements": None,
                "params_schema": None, "image_ref": None, "status": "building"}
    db = FakeDB(fetchval=2, fetchrow=inserted)
    out = await av.create_version(db, AGENT, {"agent.py": "x"})
    assert out["version_no"] == 2 and out["files"] == {"agent.py": "x"}
    # files passed to the INSERT as a JSON string (not a dict)
    insert_call = [c for c in db.calls if c[0] == "fetchrow"][0]
    args = insert_call[2]
    assert json.loads(args[2]) == {"agent.py": "x"}      # $3 = files json
    assert args[1] == 2                                   # $2 = version_no (from next_version_no)


@pytest.mark.asyncio
async def test_create_version_serializes_requirements_and_schema():
    db = FakeDB(fetchval=1, fetchrow={"id": VID, "agent_id": AGENT, "version_no": 1,
                "files": "{}", "requirements": "[]", "params_schema": "null", "status": "building"})
    await av.create_version(db, AGENT, {}, requirements=[{"name": "httpx", "version": "0.28.1"}],
                            params_schema={"fields": []}, image_ref="snap-1")
    args = [c for c in db.calls if c[0] == "fetchrow"][0][2]
    assert json.loads(args[3]) == [{"name": "httpx", "version": "0.28.1"}]   # requirements
    assert json.loads(args[4]) == {"fields": []}                            # params_schema
    assert args[5] == "snap-1"                                              # image_ref


# ── get_active_version / list_versions ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_active_version_parses():
    db = FakeDB(fetchrow={"id": VID, "agent_id": AGENT, "version_no": 1,
                          "files": json.dumps({"agent.py": "y"}), "status": "active"})
    out = await av.get_active_version(db, AGENT)
    assert out["files"] == {"agent.py": "y"} and out["version_no"] == 1


@pytest.mark.asyncio
async def test_list_versions_newest_first():
    rows = [{"id": "b", "version_no": 2, "status": "active", "image_ref": None, "created_at": None},
            {"id": "a", "version_no": 1, "status": "active", "image_ref": None, "created_at": None}]
    db = FakeDB(fetch=rows)
    out = await av.list_versions(db, AGENT)
    assert [v["version_no"] for v in out] == [2, 1]


# ── activate_version: ownership-guarded ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_activate_version_ok():
    db = FakeDB(fetchval=AGENT)          # UPDATE … RETURNING id → a row → success
    await av.activate_version(db, AGENT, VID)   # no raise


@pytest.mark.asyncio
async def test_activate_version_rejects_foreign_version():
    db = FakeDB(fetchval=None)           # RETURNING nothing → version not owned by agent
    with pytest.raises(ValueError, match="does not belong"):
        await av.activate_version(db, AGENT, VID)


# ── rollback_to: usability-guarded backward repoint ─────────────────────────────

class _RbConn:
    def __init__(self, target):
        self.target = target
        self.execs = []
    async def fetchrow(self, q, *a): return self.target
    async def execute(self, q, *a): self.execs.append(q)


@pytest.mark.asyncio
async def test_rollback_to_ok():
    conn = _RbConn({"version_no": 2, "status": "superseded"})
    out = await av.rollback_to(conn, AGENT, VID)
    assert out["version_no"] == 2
    assert len(conn.execs) == 2          # repoint + status update


@pytest.mark.asyncio
async def test_rollback_to_rejects_foreign():
    conn = _RbConn(None)
    with pytest.raises(ValueError, match="does not belong"):
        await av.rollback_to(conn, AGENT, VID)
    assert conn.execs == []              # nothing mutated


@pytest.mark.parametrize("status", ["failed", "building"])
@pytest.mark.asyncio
async def test_rollback_to_rejects_unusable_status(status):
    conn = _RbConn({"version_no": 3, "status": status})
    with pytest.raises(ValueError, match=f"cannot roll back to a '{status}'"):
        await av.rollback_to(conn, AGENT, VID)
    assert conn.execs == []              # never repointed to an unbuilt version
