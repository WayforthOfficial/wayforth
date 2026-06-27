"""test_agent_redeploy.py — code-editing v1, Step 4 (save→redeploy orchestration).

Control-flow unit tests with a fake pool: validation is fail-closed BEFORE a build is
spent, and a build failure never reaches the activate step (pointer untouched). The
SQL-level guarantees (pointer-unmoved, version_id binding, forward-only activate) are
proven against real Postgres in scripts/agent_redeploy_proof.py.
"""
from __future__ import annotations

import json

import pytest

from services import agent_redeploy as R
from services.agent_redeploy import RedeployError


class _Txn:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


class FakeConn:
    def __init__(self, store):
        self.store = store

    async def fetchval(self, q, *a):
        self.store["queries"].append(q)
        if "COALESCE(MAX(version_no)" in q:
            return self.store["next_no"]
        if "UPDATE hosted_agents" in q and "active_version_id" in q:
            self.store["activated"] = True       # forward-only activate fired
            return "agent-id"
        return None

    async def fetchrow(self, q, *a):
        self.store["queries"].append(q)
        if "INSERT INTO agent_versions" in q:
            self.store["created"] = True
            return {"id": f"vid-{a[1]}", "agent_id": a[0], "version_no": a[1],
                    "files": a[2], "requirements": a[3], "params_schema": a[4],
                    "image_ref": a[5], "status": a[6]}
        return None

    async def execute(self, q, *a):
        self.store["queries"].append(q)
        return "OK"

    def transaction(self): return _Txn()


class _Acq:
    def __init__(self, store): self.store = store
    async def __aenter__(self): return FakeConn(self.store)
    async def __aexit__(self, *a): return False


class FakePool:
    def __init__(self, store): self.store = store
    def acquire(self): return _Acq(self.store)


def _store():
    return {"queries": [], "next_no": 2, "created": False, "activated": False}


AGENT = {"id": "agent-1", "runtime": "python3.12"}
GOOD_CODE = "print('hi')"   # no PARAMS declared → schema None, valid


def _activate_ran(store):
    return any("UPDATE hosted_agents" in q and "active_version_id" in q for q in store["queries"])


def _insert_ran(store):
    return any("INSERT INTO agent_versions" in q for q in store["queries"])


# ── validation is fail-closed BEFORE any build/version ──────────────────────────

@pytest.mark.asyncio
async def test_rejects_missing_entrypoint():
    store = _store()
    called = {"build": False}
    async def build_fn(**k): called["build"] = True; return "img"
    with pytest.raises(RedeployError) as ei:
        await R.redeploy(FakePool(store), AGENT, {"helper.py": "x"}, "", build_fn=build_fn)
    assert ei.value.stage == "files"
    assert not called["build"] and not _insert_ran(store)   # nothing spent


@pytest.mark.asyncio
async def test_rejects_non_literal_params():
    store = _store()
    called = {"build": False}
    async def build_fn(**k): called["build"] = True; return "img"
    bad = "PARAMS = build_params()\nprint(1)"   # non-literal → ParamsSchemaError
    with pytest.raises(RedeployError) as ei:
        await R.redeploy(FakePool(store), AGENT, {"agent.py": bad}, "", build_fn=build_fn)
    assert ei.value.stage == "params"
    assert not called["build"] and not _insert_ran(store)


@pytest.mark.asyncio
async def test_rejects_non_allowlisted_requirement():
    store = _store()
    called = {"build": False}
    async def build_fn(**k): called["build"] = True; return "img"
    with pytest.raises(RedeployError) as ei:
        await R.redeploy(FakePool(store), AGENT, {"agent.py": GOOD_CODE},
                         "evil-canary==1.0", build_fn=build_fn)
    assert ei.value.stage == "requirements"
    assert ei.value.errors and not called["build"] and not _insert_ran(store)


# ── build failure: version recorded failed, ACTIVATE NEVER RUNS (pointer unmoved) ─

@pytest.mark.asyncio
async def test_build_failure_marks_failed_and_never_activates():
    store = _store()
    async def build_fn(**k): raise RuntimeError("install_failed: boom")
    with pytest.raises(RedeployError) as ei:
        await R.redeploy(FakePool(store), AGENT, {"agent.py": GOOD_CODE}, "", build_fn=build_fn)
    assert ei.value.stage == "build"
    assert _insert_ran(store)                                  # version was created
    assert any("SET status = $2" in q for q in store["queries"])  # marked (failed)
    assert not _activate_ran(store)                            # the pointer is NEVER touched


# ── success: build then forward-only activate fires ─────────────────────────────

@pytest.mark.asyncio
async def test_success_builds_then_activates():
    store = _store()
    seen = {}
    async def build_fn(**k): seen.update(k); return "img-123"
    out = await R.redeploy(FakePool(store), AGENT, {"agent.py": GOOD_CODE}, "", build_fn=build_fn)
    assert seen["files"] == {"agent.py": GOOD_CODE}            # build_fn got the files
    assert _activate_ran(store) and out["activated"] and out["status"] == "active"
    assert out["image_ref"] == "img-123"


# ── dispatch resolution: flag OFF is byte-identical to the legacy .code path ─────

class _DispatchConn:
    """Returns the code row for the legacy read; None for the active-version lookup."""
    def __init__(self, code="hi", version=None):
        self.code, self.version = code, version
    async def fetchrow(self, q, *a):
        if "SELECT code FROM hosted_agents" in q:
            return {"code": self.code}
        return self.version          # get_active_version's row
    async def fetchval(self, q, *a):
        return self.version["id"] if self.version else None


class _DispatchPool:
    def __init__(self, conn): self._c = conn
    def acquire(self):
        c = self._c
        class _A:
            async def __aenter__(self): return c
            async def __aexit__(self, *a): return False
        return _A()


@pytest.mark.asyncio
async def test_resolve_dispatch_flag_off_is_legacy(monkeypatch):
    monkeypatch.delenv("AGENT_VERSIONED_DISPATCH_ENABLED", raising=False)
    from routers.cloud import _resolve_dispatch
    d = await _resolve_dispatch(_DispatchPool(_DispatchConn(code="legacy code")), AGENT)
    assert d == {"code": "legacy code", "files": None, "image_ref": None, "version_id": None}


@pytest.mark.asyncio
async def test_resolve_dispatch_flag_on_no_active_version_falls_back(monkeypatch):
    monkeypatch.setenv("AGENT_VERSIONED_DISPATCH_ENABLED", "1")
    from routers.cloud import _resolve_dispatch
    d = await _resolve_dispatch(_DispatchPool(_DispatchConn(code="x", version=None)), AGENT)
    assert d["files"] is None and d["image_ref"] is None and d["version_id"] is None


@pytest.mark.asyncio
async def test_resolve_dispatch_null_image_binds_but_uses_code(monkeypatch):
    monkeypatch.setenv("AGENT_VERSIONED_DISPATCH_ENABLED", "1")
    from routers.cloud import _resolve_dispatch
    ver = {"id": "v-backfilled", "files": {"agent.py": "x"}, "image_ref": None}
    d = await _resolve_dispatch(_DispatchPool(_DispatchConn(code="x", version=ver)), AGENT)
    # version exists but no built image → legacy code path, version_id still bound for audit
    assert d["version_id"] == "v-backfilled" and d["image_ref"] is None and d["files"] is None


@pytest.mark.asyncio
async def test_resolve_dispatch_built_image_uses_versioned_path(monkeypatch):
    monkeypatch.setenv("AGENT_VERSIONED_DISPATCH_ENABLED", "1")
    from routers.cloud import _resolve_dispatch
    ver = {"id": "v2", "files": {"agent.py": "y", "helper.py": "z"}, "image_ref": "img-v2"}
    d = await _resolve_dispatch(_DispatchPool(_DispatchConn(code="x", version=ver)), AGENT)
    assert d["image_ref"] == "img-v2" and d["files"] == {"agent.py": "y", "helper.py": "z"}
    assert d["version_id"] == "v2"
