"""test_proxy_run_token_auth.py — Step 2: /proxy wf_run_ auth branch.

Covers _resolve_proxy_caller (verify + binding cross-check + scope + revocation,
with the consolidated agent_runs load) and the cap-check prefetch consolidation
(Guardrail 2 — no extra read).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from core import run_token as rt
import routers.proxy as proxy
from core.credits import check_run_credit_cap

USER = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
RUN = "33333333-3333-4333-8333-333333333333"
KEY = "44444444-4444-4444-8444-444444444444"
SECRET = "proxy-step2-secret-AAAAAAAAAAAAAAAAAAAAAAAA"


@pytest.fixture
def secret(monkeypatch):
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", SECRET)
    monkeypatch.delenv("RUN_TOKEN_SIGNING_SECRET_PREV", raising=False)


class FakeDB:
    """Returns a fixed agent_runs+key row for the consolidated load."""
    def __init__(self, row):
        self._row = row
        self.fetchrow_calls = 0

    async def fetchrow(self, q, *a):
        self.fetchrow_calls += 1
        return self._row


def _row(**over):
    r = {"user_id": USER, "hosted_agent_id": AGENT, "status": "running",
         "credits_reserved": 50, "api_key_id": KEY, "tier": "pro"}
    r.update(over)
    return r


def _resolve(token, db):
    return asyncio.run(proxy._resolve_proxy_caller(token, db))


# ── happy path ────────────────────────────────────────────────────────────────

def test_valid_token_resolves_identity_and_prefetch(secret):
    db = FakeDB(_row())
    user_id, api_key_id, tier, forced_agent_id, run_prefetch = _resolve(
        rt.mint_run_token(USER, AGENT, RUN), db)
    assert user_id == USER
    assert api_key_id == KEY
    assert tier == "pro"
    assert forced_agent_id == RUN                      # SIGNED run_id is authoritative
    assert run_prefetch == {"credits_reserved": 50, "status": "running"}
    assert db.fetchrow_calls == 1                      # single consolidated load


def test_no_active_key_falls_back_to_free_tier_and_null_key(secret):
    db = FakeDB(_row(api_key_id=None, tier=None))
    user_id, api_key_id, tier, forced_agent_id, _ = _resolve(
        rt.mint_run_token(USER, AGENT, RUN), db)
    assert user_id == USER and api_key_id is None and tier == "free"


# ── verify / scope failures ─────────────────────────────────────────────────────

def test_garbage_token_rejected(secret):
    with pytest.raises(HTTPException) as e:
        _resolve("wf_run_not.a.jwt", FakeDB(_row()))
    assert e.value.status_code == 401 and e.value.detail["error"] == "invalid_run_token"


def test_token_without_proxy_scope_rejected(secret):
    tok = rt.mint_run_token(USER, AGENT, RUN, scope=(rt.SCOPE_X402_EXECUTE,))  # no proxy
    with pytest.raises(HTTPException) as e:
        _resolve(tok, FakeDB(_row()))
    assert e.value.detail["error"] == "invalid_run_token"


def test_expired_token_rejected(secret):
    tok = rt.mint_run_token(USER, AGENT, RUN, ttl_seconds=-1)
    with pytest.raises(HTTPException) as e:
        _resolve(tok, FakeDB(_row()))
    assert e.value.detail["error"] == "invalid_run_token"


# ── binding cross-check ─────────────────────────────────────────────────────────

def test_run_not_found_rejected(secret):
    with pytest.raises(HTTPException) as e:
        _resolve(rt.mint_run_token(USER, AGENT, RUN), FakeDB(None))
    assert e.value.detail["error"] == "run_not_found"


def test_wrong_user_binding_rejected(secret):
    db = FakeDB(_row(user_id="99999999-9999-4999-8999-999999999999"))
    with pytest.raises(HTTPException) as e:
        _resolve(rt.mint_run_token(USER, AGENT, RUN), db)
    assert e.value.detail["error"] == "run_binding_mismatch"


def test_wrong_agent_binding_rejected(secret):
    db = FakeDB(_row(hosted_agent_id="88888888-8888-4888-8888-888888888888"))
    with pytest.raises(HTTPException) as e:
        _resolve(rt.mint_run_token(USER, AGENT, RUN), db)
    assert e.value.detail["error"] == "run_binding_mismatch"


# ── revocation (run no longer active) ───────────────────────────────────────────

@pytest.mark.parametrize("state", ["completed", "failed", "cancelled", "timeout", "oom"])
def test_inactive_run_revokes_token(secret, state):
    db = FakeDB(_row(status=state))
    with pytest.raises(HTTPException) as e:
        _resolve(rt.mint_run_token(USER, AGENT, RUN), db)
    assert e.value.detail["error"] == "run_not_active"


# ── wf_live_ path unchanged ─────────────────────────────────────────────────────

def test_wf_live_key_delegates_to_resolve_user(secret, monkeypatch):
    async def fake_resolve_user(db, key):
        return (USER, KEY, "scale")
    monkeypatch.setattr(proxy, "_resolve_user", fake_resolve_user)
    db = FakeDB(_row())
    user_id, api_key_id, tier, forced_agent_id, run_prefetch = _resolve("wf_live_abc", db)
    assert (user_id, api_key_id, tier) == (USER, KEY, "scale")
    assert forced_agent_id is None and run_prefetch is None
    assert db.fetchrow_calls == 0                       # key path doesn't load the run


# ── Guardrail 2: cap prefetch consolidation ─────────────────────────────────────

def test_cap_prefetch_skips_run_load():
    class DB:
        def __init__(self):
            self.fetchrow_called = False
        async def fetchrow(self, q, *a):
            self.fetchrow_called = True
            return {"credits_reserved": 50, "status": "running"}
        async def fetchval(self, q, *a):
            return 0  # spent so far

    db = DB()
    asyncio.run(check_run_credit_cap(
        db, RUN, 5, prefetched={"credits_reserved": 50, "status": "running"}))
    assert db.fetchrow_called is False                  # used prefetch — no extra read


def test_cap_without_prefetch_still_loads():
    class DB:
        def __init__(self):
            self.fetchrow_called = False
        async def fetchrow(self, q, *a):
            self.fetchrow_called = True
            return {"credits_reserved": 50, "status": "running"}
        async def fetchval(self, q, *a):
            return 0

    db = DB()
    asyncio.run(check_run_credit_cap(db, RUN, 5))
    assert db.fetchrow_called is True                   # legacy callers unchanged
