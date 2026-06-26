"""test_cloud_run_token_dispatch.py — Step 3: dispatch mints, _execute_run injects.

The whole point of Option 3: with AGENT_RUN_TOKENS_ENABLED on, the sandbox gets a
wf_run_ token and the user's raw wf_live_ key NEVER enters the env. Flag off must be
byte-identical to today's snapshot behavior (exact rollback). Proven here, not by eye.
"""
from __future__ import annotations

import asyncio

import pytest

from core import run_token as rt
import routers.cloud as cloud

USER = "14b4a56e-88fe-4099-b703-ded9d9220c44"
AGENT = "312f7603-2ba1-4975-a447-76743ab92b1b"
RUN = "1765d54f-f630-4493-bc77-dd85f3ceac4d"
SNAPSHOT = "wf_live_RAWUSERKEY_must_never_enter_sandbox"
SECRET = "step3-secret-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _run_execute_capturing_env(monkeypatch, runtime_key, params=None):
    """Drive _execute_run with mocked deps; return the run_env passed to the sandbox."""
    captured = {}

    class _Result:
        stdout = "ok"; stderr = ""; exit_code = 0; duration_ms = 1000; sandbox_id = "sb"

    class _Provider:
        async def run(self, **kw):
            captured.update(kw)        # kw["env"] is the sandbox env
            return _Result()

    class _Conn:
        async def fetchrow(self, q, *a):
            return {"code": "print(1)"} if "FROM hosted_agents" in q else None
        async def execute(self, q, *a):
            return None

    class _PoolCtx:
        async def __aenter__(self): return _Conn()
        async def __aexit__(self, *e): return False

    class _Pool:
        def acquire(self): return _PoolCtx()

    async def _deduct(conn, user_id, amount, endpoint, **kw): return True, 1000 - amount
    async def _release(pool, user_id, amount, run_id): return None
    async def _signals(pool, run_id, user_id):
        return {"credits_proxy": 0, "services_called": [], "failover_events": 0, "substitutions": []}

    monkeypatch.setattr(cloud, "get_provider", lambda *a, **k: _Provider())
    monkeypatch.setattr(cloud, "compute_credits_for_run", lambda ms: 1)
    monkeypatch.setattr(cloud, "check_and_deduct_credits", _deduct)
    monkeypatch.setattr(cloud, "_release_reserve", _release)
    monkeypatch.setattr(cloud, "_reconcile_run_signals", _signals)

    agent = {"id": AGENT, "runtime": "python", "env_encrypted": None, "sandbox_provider": "e2b"}
    asyncio.run(cloud._execute_run(_Pool(), RUN, agent, USER, runtime_key, 0, params=params))
    return captured["env"]


# ── flag ON: token injected, raw key absent ─────────────────────────────────────

def test_flag_on_mints_token_not_snapshot(monkeypatch):
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGENT_RUN_TOKENS_ENABLED", "1")
    key = cloud._runtime_key_for_run(USER, AGENT, RUN, SNAPSHOT)
    assert key.startswith("wf_run_")
    assert key != SNAPSHOT
    claims = rt.verify_run_token(key)                       # bound to (user, agent, run)
    assert claims["sub"] == USER and claims["agent_id"] == AGENT and claims["run_id"] == RUN


def test_flag_on_env_has_token_and_no_raw_key(monkeypatch):
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", SECRET)
    monkeypatch.setenv("AGENT_RUN_TOKENS_ENABLED", "1")
    runtime_key = cloud._runtime_key_for_run(USER, AGENT, RUN, SNAPSHOT)
    env = _run_execute_capturing_env(monkeypatch, runtime_key)

    assert env["WAYFORTH_API_KEY"].startswith("wf_run_")
    # THE invariant: the raw wf_live_ key appears NOWHERE in the sandbox env.
    for k, v in env.items():
        assert "wf_live_" not in str(v), f"raw key leaked into env[{k}]"


# ── flag OFF: byte-identical snapshot behavior ──────────────────────────────────

def test_flag_off_returns_snapshot_unchanged(monkeypatch):
    monkeypatch.delenv("AGENT_RUN_TOKENS_ENABLED", raising=False)
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", SECRET)     # set but unused while OFF
    assert cloud._runtime_key_for_run(USER, AGENT, RUN, SNAPSHOT) == SNAPSHOT


def test_flag_off_injects_snapshot_into_env(monkeypatch):
    monkeypatch.delenv("AGENT_RUN_TOKENS_ENABLED", raising=False)
    env = _run_execute_capturing_env(monkeypatch, SNAPSHOT)
    assert env["WAYFORTH_API_KEY"] == SNAPSHOT                 # exactly today's behavior


@pytest.mark.parametrize("val", ["0", "false", "no", "", "off"])
def test_flag_values_treated_as_off(monkeypatch, val):
    monkeypatch.setenv("AGENT_RUN_TOKENS_ENABLED", val)
    monkeypatch.setenv("RUN_TOKEN_SIGNING_SECRET", SECRET)
    assert cloud._runtime_key_for_run(USER, AGENT, RUN, SNAPSHOT) == SNAPSHOT


# ── flag ON but misconfigured: never break a run ────────────────────────────────

def test_flag_on_without_secret_falls_back_to_snapshot(monkeypatch):
    monkeypatch.setenv("AGENT_RUN_TOKENS_ENABLED", "1")
    monkeypatch.delenv("RUN_TOKEN_SIGNING_SECRET", raising=False)
    monkeypatch.delenv("RUN_TOKEN_SIGNING_SECRET_PREV", raising=False)
    assert cloud._runtime_key_for_run(USER, AGENT, RUN, SNAPSHOT) == SNAPSHOT


# ── Step 3: resolved params injected as WAYFORTH_PARAMS ──────────────────────────

def test_params_injected_as_wayforth_params(monkeypatch):
    import json
    monkeypatch.delenv("AGENT_RUN_TOKENS_ENABLED", raising=False)
    env = _run_execute_capturing_env(monkeypatch, SNAPSHOT, params={"ticker": "AAPL", "n": 5})
    assert json.loads(env["WAYFORTH_PARAMS"]) == {"ticker": "AAPL", "n": 5}


def test_no_params_injects_empty_object(monkeypatch):
    monkeypatch.delenv("AGENT_RUN_TOKENS_ENABLED", raising=False)
    env = _run_execute_capturing_env(monkeypatch, SNAPSHOT)
    assert env["WAYFORTH_PARAMS"] == "{}"
