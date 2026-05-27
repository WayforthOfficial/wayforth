"""tests/test_llm_gateway.py — Unit tests for the OpenAI-compatible LLM gateway.

Pure-unit: all adapter functions and DB/pool calls are mocked.
No network, no database, no live deployment required.

Run:
    uv run --python 3.12 python3 -m pytest apps/api/tests/test_llm_gateway.py -v

Covers:
  1. Routing: groq/llama-3.3-70b routes to groq adapter
  2. Routing: together/mistral-7b routes to together adapter
  3. No prefix → auto-selects groq (first in chain with key configured)
  4. Failover: groq raises exception → falls back to together
  5. All fail → 503 with attempted list
  6. Streaming: verify SSE response content-type and [DONE] terminator
  7. Credit deduction: check_and_deduct_credits called with correct cost
  8. Tier gate: free-tier user gets 403
  9. Unknown model prefix → 400
"""
from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Allow imports from apps/api
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Constants ─────────────────────────────────────────────────────────────────

# 56-char key: "wf_live_" (8) + 48 chars
_FAKE_API_KEY = "wf_live_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # 56 chars
_USER_ID      = "00000000-0000-0000-0000-000000000001"
_KEY_ID       = "00000000-0000-0000-0000-000000000002"

_GROQ_RESPONSE     = {"content": "Hello from Groq",     "model": "llama-3.3-70b-versatile", "tokens_used": 42}
_TOGETHER_RESPONSE = {"content": "Hello from Together",  "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "tokens_used": 30}
_MISTRAL_RESPONSE  = {"content": "Hello from Mistral",  "model": "mistral-small-latest",  "tokens_used": 20}


def _make_request(model: str, stream: bool = False) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": stream,
    }


# ── DB / pool mock helpers ────────────────────────────────────────────────────

def _make_db_mock(tier: str = "builder") -> AsyncMock:
    """Return a mock asyncpg connection object."""
    db = AsyncMock()
    key_row = MagicMock()
    key_row.__getitem__ = lambda self, k: {
        "id": _KEY_ID, "user_id": _USER_ID, "tier": tier,
    }[k]

    credits_row = MagicMock()
    credits_row.__getitem__ = lambda self, k: 1000 if k == "credits_balance" else None

    async def _fetchrow(query, *args, **kwargs):
        if "credits_balance" in query:
            return credits_row
        return key_row

    db.fetchrow = AsyncMock(side_effect=_fetchrow)
    db.execute = AsyncMock(return_value=None)

    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db.transaction = MagicMock(return_value=ctx)
    return db


def _make_pool_mock(db: AsyncMock) -> MagicMock:
    """Return a mock asyncpg pool.

    get_db calls `await pool.acquire(timeout=...)` and `await pool.release(conn)`.
    _increment_calls / billing use `async with pool.acquire() as conn`.
    We satisfy both by returning an object that is both awaitable and an
    async context manager.
    """
    pool = MagicMock()

    class _DualUse:
        def __await__(self):
            async def _inner():
                return db
            return _inner().__await__()

        async def __aenter__(self):
            return db

        async def __aexit__(self, *args):
            return False

    pool.acquire = MagicMock(return_value=_DualUse())
    pool.release = AsyncMock(return_value=None)
    pool.get_size = MagicMock(return_value=2)
    pool.get_idle_size = MagicMock(return_value=2)
    pool.get_min_size = MagicMock(return_value=2)
    pool.get_max_size = MagicMock(return_value=40)
    return pool


def _make_test_app(tier: str = "builder"):
    """Build a minimal FastAPI app with the llm router and get_db overridden."""
    from fastapi import FastAPI
    from routers.llm import router
    from core.db import get_db

    db = _make_db_mock(tier)
    pool = _make_pool_mock(db)

    async def _override_get_db():
        yield db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = _override_get_db
    app.state.pool = pool
    return app, db, pool


# ── Env / adapter mock context ────────────────────────────────────────────────

def _fake_env_and_adapters(
    groq_fn=None,
    together_fn=None,
    mistral_fn=None,
    gemini_fn=None,
    perplexity_fn=None,
    providers_with_keys=("groq", "together", "mistral"),
):
    """Return a combined context manager that patches env keys and _CALL_FNS/_STREAM_FNS.

    Providers in providers_with_keys get a dummy env-var value; others are unset.
    Functions default to async raise RuntimeError (should not be called unless explicitly set).
    """
    import contextlib

    env = {}
    env_map = {
        "groq":       "GROQ_API_KEY",
        "together":   "TOGETHER_API_KEY",
        "mistral":    "MISTRAL_API_KEY",
        "gemini":     "GEMINI_API_KEY",
        "perplexity": "PERPLEXITY_API_KEY",
    }
    for p in providers_with_keys:
        env[env_map[p]] = f"fake-{p}-key"
    # Explicitly blank out any env key NOT in providers_with_keys so a real
    # key from the shell environment does not leak into the test.
    for p, ev in env_map.items():
        if p not in providers_with_keys:
            env[ev] = ""

    async def _should_not_call(params, key):
        raise RuntimeError(f"unexpected call to provider adapter")

    call_fns_patch = {
        "groq":       groq_fn or _should_not_call,
        "together":   together_fn or _should_not_call,
        "mistral":    mistral_fn or _should_not_call,
        "gemini":     gemini_fn or _should_not_call,
        "perplexity": perplexity_fn or _should_not_call,
    }

    @contextlib.contextmanager
    def _ctx():
        with patch.dict(os.environ, env, clear=False), \
             patch.dict("routers.llm._CALL_FNS", call_fns_patch):
            yield

    return _ctx()


# ── Test 1: groq/ prefix → groq adapter ──────────────────────────────────────

def test_routing_groq_prefix():
    """groq/llama-3.3-70b routes to call_groq, not call_together."""
    from fastapi.testclient import TestClient

    app, db, pool = _make_test_app("builder")
    mock_groq    = AsyncMock(return_value=_GROQ_RESPONSE)
    mock_together = AsyncMock(return_value=_TOGETHER_RESPONSE)

    with _fake_env_and_adapters(groq_fn=mock_groq, together_fn=mock_together), \
         patch("routers.llm._resolve_user", new_callable=AsyncMock,
               return_value=(_USER_ID, _KEY_ID, "builder")), \
         patch("routers.llm.check_rate_limit", new_callable=AsyncMock), \
         patch("routers.llm.check_and_deduct_credits", new_callable=AsyncMock,
               return_value=(True, 997)), \
         patch("routers.llm._increment_calls", new_callable=AsyncMock):

        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json=_make_request("groq/llama-3.3-70b"),
            headers={"X-Wayforth-API-Key": _FAKE_API_KEY},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["x-wayforth-provider"] == "groq"
    assert body["choices"][0]["message"]["content"] == "Hello from Groq"
    mock_groq.assert_called_once()
    mock_together.assert_not_called()


# ── Test 2: together/ prefix → together adapter ───────────────────────────────

def test_routing_together_prefix():
    """together/mistral-7b routes to call_together, not call_groq."""
    from fastapi.testclient import TestClient

    app, db, pool = _make_test_app("builder")
    mock_groq    = AsyncMock(return_value=_GROQ_RESPONSE)
    mock_together = AsyncMock(return_value=_TOGETHER_RESPONSE)

    with _fake_env_and_adapters(groq_fn=mock_groq, together_fn=mock_together,
                                 providers_with_keys=("together",)), \
         patch("routers.llm._resolve_user", new_callable=AsyncMock,
               return_value=(_USER_ID, _KEY_ID, "builder")), \
         patch("routers.llm.check_rate_limit", new_callable=AsyncMock), \
         patch("routers.llm.check_and_deduct_credits", new_callable=AsyncMock,
               return_value=(True, 996)), \
         patch("routers.llm._increment_calls", new_callable=AsyncMock):

        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json=_make_request("together/mistral-7b"),
            headers={"X-Wayforth-API-Key": _FAKE_API_KEY},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["x-wayforth-provider"] == "together"
    assert body["choices"][0]["message"]["content"] == "Hello from Together"
    mock_together.assert_called_once()
    mock_groq.assert_not_called()


# ── Test 3: no prefix → auto-selects groq first ───────────────────────────────

def test_no_prefix_auto_selects_groq():
    """No model prefix → groq is tried first (first in FAILOVER_CHAIN)."""
    from fastapi.testclient import TestClient

    app, db, pool = _make_test_app("builder")
    mock_groq    = AsyncMock(return_value=_GROQ_RESPONSE)
    mock_together = AsyncMock(return_value=_TOGETHER_RESPONSE)

    with _fake_env_and_adapters(groq_fn=mock_groq, together_fn=mock_together), \
         patch("routers.llm._resolve_user", new_callable=AsyncMock,
               return_value=(_USER_ID, _KEY_ID, "builder")), \
         patch("routers.llm.check_rate_limit", new_callable=AsyncMock), \
         patch("routers.llm.check_and_deduct_credits", new_callable=AsyncMock,
               return_value=(True, 997)), \
         patch("routers.llm._increment_calls", new_callable=AsyncMock):

        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json=_make_request("llama-3.3-70b-versatile"),   # no prefix
            headers={"X-Wayforth-API-Key": _FAKE_API_KEY},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["x-wayforth-provider"] == "groq"
    mock_groq.assert_called_once()
    mock_together.assert_not_called()


# ── Test 4: groq fails → falls back to together ───────────────────────────────

def test_failover_groq_to_together():
    """groq raises exception → together is tried next; x-wayforth-fallback=true."""
    from fastapi.testclient import TestClient

    app, db, pool = _make_test_app("builder")
    mock_groq    = AsyncMock(side_effect=Exception("Groq 503: service unavailable"))
    mock_together = AsyncMock(return_value=_TOGETHER_RESPONSE)

    with _fake_env_and_adapters(groq_fn=mock_groq, together_fn=mock_together), \
         patch("routers.llm._resolve_user", new_callable=AsyncMock,
               return_value=(_USER_ID, _KEY_ID, "builder")), \
         patch("routers.llm.check_rate_limit", new_callable=AsyncMock), \
         patch("routers.llm.check_and_deduct_credits", new_callable=AsyncMock,
               return_value=(True, 996)), \
         patch("routers.llm._increment_calls", new_callable=AsyncMock):

        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json=_make_request("groq/some-model"),
            headers={"X-Wayforth-API-Key": _FAKE_API_KEY},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["x-wayforth-provider"] == "together"
    assert body["x-wayforth-fallback"] is True
    mock_groq.assert_called_once()
    mock_together.assert_called_once()


# ── Test 5: all providers fail → 503 ─────────────────────────────────────────

def test_all_providers_fail_503():
    """All three failover providers raise → 503 with error=all_providers_failed."""
    from fastapi.testclient import TestClient

    app, db, pool = _make_test_app("builder")
    mock_groq    = AsyncMock(side_effect=Exception("Groq error"))
    mock_together = AsyncMock(side_effect=Exception("Together error"))
    mock_mistral  = AsyncMock(side_effect=Exception("Mistral error"))

    with _fake_env_and_adapters(groq_fn=mock_groq, together_fn=mock_together,
                                 mistral_fn=mock_mistral), \
         patch("routers.llm._resolve_user", new_callable=AsyncMock,
               return_value=(_USER_ID, _KEY_ID, "builder")), \
         patch("routers.llm.check_rate_limit", new_callable=AsyncMock):

        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json=_make_request("some-model"),   # no prefix → all three tried
            headers={"X-Wayforth-API-Key": _FAKE_API_KEY},
        )

    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"] == "all_providers_failed"
    assert "attempted" in body
    for p in ("groq", "together", "mistral"):
        assert p in body["attempted"], f"{p!r} missing from attempted: {body['attempted']}"


# ── Test 6: streaming SSE format ─────────────────────────────────────────────

def test_streaming_sse_format():
    """stream:true returns text/event-stream with [DONE] and delta chunks."""
    from fastapi.testclient import TestClient

    app, db, pool = _make_test_app("builder")

    async def _fake_stream(params, api_key):
        for token in ["Hello", " ", "world"]:
            yield token

    with _fake_env_and_adapters(providers_with_keys=("groq",)), \
         patch("routers.llm._resolve_user", new_callable=AsyncMock,
               return_value=(_USER_ID, _KEY_ID, "builder")), \
         patch("routers.llm.check_rate_limit", new_callable=AsyncMock), \
         patch.dict("routers.llm._STREAM_FNS", {"groq": _fake_stream, "together": _fake_stream}), \
         patch("routers.llm.check_and_deduct_credits", new_callable=AsyncMock,
               return_value=(True, 997)), \
         patch("routers.llm._increment_calls", new_callable=AsyncMock):

        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json=_make_request("groq/llama-3.3-70b", stream=True),
            headers={"X-Wayforth-API-Key": _FAKE_API_KEY},
        )

    assert resp.status_code == 200, resp.text
    ct = resp.headers.get("content-type", "")
    assert "text/event-stream" in ct, f"Expected text/event-stream, got: {ct}"
    raw = resp.text
    assert "data: [DONE]" in raw, f"[DONE] not found:\n{raw}"
    assert '"delta"' in raw, f"No delta chunks found:\n{raw}"


# ── Test 7: credit deduction uses correct provider cost ───────────────────────

def test_credit_deduction_correct_cost():
    """check_and_deduct_credits is called with Groq's credit cost (3)."""
    from fastapi.testclient import TestClient
    from services.managed import SERVICE_CONFIGS

    app, db, pool = _make_test_app("builder")
    mock_groq   = AsyncMock(return_value=_GROQ_RESPONSE)
    mock_deduct = AsyncMock(return_value=(True, 997))

    with _fake_env_and_adapters(groq_fn=mock_groq, providers_with_keys=("groq",)), \
         patch("routers.llm._resolve_user", new_callable=AsyncMock,
               return_value=(_USER_ID, _KEY_ID, "builder")), \
         patch("routers.llm.check_rate_limit", new_callable=AsyncMock), \
         patch("routers.llm.check_and_deduct_credits", mock_deduct), \
         patch("routers.llm._increment_calls", new_callable=AsyncMock):

        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json=_make_request("groq/llama-3.3-70b"),
            headers={"X-Wayforth-API-Key": _FAKE_API_KEY},
        )

    assert resp.status_code == 200, resp.text
    mock_deduct.assert_called_once()
    # Signature: check_and_deduct_credits(db, user_id, cost, endpoint, ...)
    cost_arg = mock_deduct.call_args[0][2]
    expected_cost = SERVICE_CONFIGS["groq"]["credits"]   # 3
    assert cost_arg == expected_cost, f"Expected {expected_cost} credits, got {cost_arg}"


# ── Test 8: free-tier → 403 ───────────────────────────────────────────────────

def test_free_tier_gets_403():
    """Free-tier user must receive 403 (byok feature gate)."""
    from fastapi.testclient import TestClient

    app, db, pool = _make_test_app("free")

    with patch.dict(os.environ, {"GROQ_API_KEY": "fake-groq"}, clear=False), \
         patch("routers.llm._resolve_user", new_callable=AsyncMock,
               return_value=(_USER_ID, _KEY_ID, "free")):

        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json=_make_request("groq/llama-3.3-70b"),
            headers={"X-Wayforth-API-Key": _FAKE_API_KEY},
        )

    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body.get("detail", {}).get("error") == "tier_required"


# ── Test 9: unknown model prefix → 400 ───────────────────────────────────────

def test_unknown_model_prefix_400():
    """openai/gpt-4 has an unknown prefix → 400 unknown_model_prefix."""
    from fastapi.testclient import TestClient

    app, db, pool = _make_test_app("builder")

    with patch.dict(os.environ, {"GROQ_API_KEY": "fake-groq"}, clear=False), \
         patch("routers.llm._resolve_user", new_callable=AsyncMock,
               return_value=(_USER_ID, _KEY_ID, "builder")), \
         patch("routers.llm.check_rate_limit", new_callable=AsyncMock):

        client = TestClient(app)
        resp = client.post(
            "/v1/chat/completions",
            json=_make_request("openai/gpt-4"),
            headers={"X-Wayforth-API-Key": _FAKE_API_KEY},
        )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"] == "unknown_model_prefix"
    assert body["model"] == "openai/gpt-4"
