"""test_run_core_parity.py — /run behavior + ledger lock (PR B merge gate).

GOLDEN MASTER for the _run_core extraction. These assertions capture run_endpoint's
exact response AND its exact ledger call sequence on the current code; the SAME
tests must stay green after run_endpoint is split into a thin shell + pure
_run_core, proving the refactor changes neither the returned bytes nor the billing.

Covered (per the PR B gate): happy, non-service (client) error, auth failure,
and failover (the inline 2-retry path — primary refunded, substitute charged).
test_a2a_run_billing_parity (added with PR B) asserts A2A message/send and /run
hit _run_core and bill identically for the same intent — the no-fork guarantee.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import ranker_client
import routers.execute as ex
from routers.execute import run_endpoint


# ── fakes ─────────────────────────────────────────────────────────────────────

class _FakeState:
    request_id = "req-parity"


class _FakeReq:
    def __init__(self, body, api_key="wf_live_" + "x" * 43):
        self._body = body
        self.headers = {"X-Wayforth-API-Key": api_key}
        self.state = _FakeState()

    async def json(self):
        return self._body


class _FakeResp:
    def __init__(self):
        self.headers: dict[str, str] = {}


class _RunDB:
    """Answers run_endpoint's queries deterministically from a candidate list."""
    def __init__(self, candidates):
        self._candidates = candidates
        self.stream_refunds: list = []   # streaming refunds (direct user_credits UPDATE)

    async def fetch(self, q, *a):
        if "FROM services" in q and "ORDER BY coverage_tier" in q:
            return self._candidates
        if "FROM services" in q and "slug = ANY" in q:   # category-free fallback
            return self._candidates
        return []

    async def fetchrow(self, q, *a):
        if "UPDATE user_credits" in q:                    # streaming refund path
            self.stream_refunds.append(a[0])             # credit_cost restored
            return {"credits_balance": 1000}
        if "service_health" in q:
            return None                                   # skip WRI-health adjust
        if "FROM services" in q:                          # best-service hint
            return None
        return None

    async def execute(self, q, *a):
        return None

    def transaction(self):
        class _Tx:
            async def __aenter__(self_):
                return None

            async def __aexit__(self_, *e):
                return False
        return _Tx()


def _candidate(slug, category="inference", wri=80.0):
    return {"id": "1", "name": slug.title(), "slug": slug, "description": "d",
            "endpoint_url": "https://x", "category": category, "pricing_usdc": None,
            "coverage_tier": 2, "source": "s", "payment_protocol": "none",
            "last_tested_at": None, "consecutive_failures": 0, "x402_supported": False,
            "wri_score": wri, "wri_version": "v2"}


# ── harness ───────────────────────────────────────────────────────────────────

def _install_mocks(monkeypatch, *, exec_script, auth_raises, deduct_ok):
    """Stub every external effect run_endpoint touches. Returns (deducts, refunds)."""
    monkeypatch.setenv("GROQ_API_KEY", "K")
    monkeypatch.setenv("TOGETHER_API_KEY", "K")
    monkeypatch.setenv("MISTRAL_API_KEY", "K")
    monkeypatch.setenv("SERPER_API_KEY", "K")

    deducts: list = []
    refunds: list = []

    async def fake_resolve(db, key):
        if auth_raises:
            raise auth_raises
        return "user-1", "key-1", "growth"

    async def fake_rate(*a, **k):
        return None

    async def fake_rank(intent, cands):
        return list(cands)

    async def fake_exec(slug, params, key):
        return exec_script.get(slug, ({"ok": True}, None, 5))

    async def fake_deduct(db, user_id, cost, endpoint, **kw):
        deducts.append((kw.get("service_id"), cost))
        bal = 1000 - cost
        if not deduct_ok:
            return (False, bal, None) if kw.get("return_tx_id") else (False, bal)
        return (True, bal, f"tx-{kw.get('service_id')}") if kw.get("return_tx_id") else (True, bal)

    async def fake_refund(db, user_id, cost, slug, err, endpoint, bal, key):
        refunds.append((slug, cost))
        return (bal or 0) + cost

    async def fake_incr(pool, key_id, cost=0):
        return 1000 - cost

    async def fake_fetch_wri(db, slug):
        return 77.0

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(ex, "_resolve_user", fake_resolve)
    monkeypatch.setattr(ex, "check_rate_limit", fake_rate)
    monkeypatch.setattr(ranker_client, "rank_services", fake_rank)
    monkeypatch.setattr(ex, "_try_execute_managed", fake_exec)
    monkeypatch.setattr(ex, "check_and_deduct_credits", fake_deduct)
    monkeypatch.setattr(ex, "_do_refund", fake_refund)
    monkeypatch.setattr(ex, "_increment_calls", fake_incr)
    monkeypatch.setattr(ex, "_fetch_wri", fake_fetch_wri)
    monkeypatch.setattr(ex, "_update_search_signal", _noop)
    monkeypatch.setattr(ex, "_maybe_dispatch_credits_low", _noop)
    monkeypatch.setattr(ex, "_check_spend_anomaly", _noop)
    monkeypatch.setattr(ex, "_patch_tx_signals", _noop)
    # app.state.pool is only handed to the (mocked) fire-and-forget signals.
    from main import app
    if not hasattr(app.state, "pool"):
        app.state.pool = object()
    return deducts, refunds


def run_once(monkeypatch, *, candidates, exec_script, body=None,
             auth_raises=None, deduct_ok=True):
    """Invoke run_endpoint (non-streaming) with all effects mocked. Returns
    (result_or_exception, deducts, refunds). exec_script: slug -> (result, err, ms)."""
    deducts, refunds = _install_mocks(
        monkeypatch, exec_script=exec_script, auth_raises=auth_raises, deduct_ok=deduct_ok)
    req = _FakeReq(body or {"intent": "chat hello", "input": {"messages": [{"role": "user", "content": "hi"}]}})
    resp = _FakeResp()
    ex._RUN_CACHE.clear()   # bust the in-process run cache between cases
    try:
        result = asyncio.run(run_endpoint(req, resp, _RunDB(candidates)))
        return result, deducts, refunds
    except HTTPException as e:
        return e, deducts, refunds


def run_stream(monkeypatch, *, candidates, body=None):
    """Invoke run_endpoint with stream=True. Mocks _run_sse_stream and captures
    the streaming refund (a direct user_credits UPDATE, not _do_refund). Returns
    (result, deducts, refunds, stream_refunds)."""
    deducts, refunds = _install_mocks(
        monkeypatch, exec_script={}, auth_raises=None, deduct_ok=True)

    async def fake_sse(*a, **k):
        yield b"data: {}\n\n"
    monkeypatch.setattr(ex, "_run_sse_stream", fake_sse)
    monkeypatch.setattr(ex, "_active_streams", {})   # fresh per case

    db = _RunDB(candidates)
    req = _FakeReq({**(body or {"intent": "chat hello",
                                "input": {"messages": [{"role": "user", "content": "hi"}]}}),
                    "stream": True})
    resp = _FakeResp()
    ex._RUN_CACHE.clear()
    result = asyncio.run(run_endpoint(req, resp, db))
    return result, deducts, refunds, db.stream_refunds


# ── cases ─────────────────────────────────────────────────────────────────────

def test_happy_path_result_and_single_charge(monkeypatch):
    result, deducts, refunds = run_once(
        monkeypatch,
        candidates=[_candidate("groq")],
        exec_script={"groq": ({"content": "hi"}, None, 7)},
    )
    assert not isinstance(result, HTTPException)
    assert result["service_used"]["slug"] == "groq"
    assert result["failover"] == {"triggered": False}
    assert result["result"] == {"content": "hi"}
    assert deducts == [("groq", 3)]      # single charge, groq cost
    assert refunds == []                 # nothing refunded on success


def test_client_error_surfaces_no_refund(monkeypatch):
    # A non-service (client) error → 400, no refund (caller sent bad params).
    result, deducts, refunds = run_once(
        monkeypatch,
        candidates=[_candidate("groq")],
        exec_script={"groq": ({}, "400 invalid request body", 5)},
    )
    assert isinstance(result, HTTPException) and result.status_code == 400
    assert result.detail.get("refunded") is False
    assert refunds == []


def test_auth_failure_propagates_no_deduct(monkeypatch):
    result, deducts, refunds = run_once(
        monkeypatch,
        candidates=[_candidate("groq")],
        exec_script={},
        auth_raises=HTTPException(status_code=401, detail={"error": "bad key"}),
    )
    assert isinstance(result, HTTPException) and result.status_code == 401
    assert deducts == [] and refunds == []   # never reached billing


def test_insufficient_credits_402_no_execute(monkeypatch):
    result, deducts, refunds = run_once(
        monkeypatch,
        candidates=[_candidate("groq")],
        exec_script={"groq": ({"content": "hi"}, None, 5)},
        deduct_ok=False,
    )
    assert isinstance(result, HTTPException) and result.status_code == 402
    assert deducts == [("groq", 3)]   # attempted the deduct, was refused
    assert refunds == []


def test_no_managed_service_422(monkeypatch):
    # A candidate with no managed mapping → no service selected → 422, no billing.
    result, deducts, refunds = run_once(
        monkeypatch,
        candidates=[_candidate("some-unmanaged-catalog-slug")],
        exec_script={},
    )
    assert isinstance(result, HTTPException) and result.status_code == 422
    assert result.detail.get("error") == "no_managed_service"
    assert deducts == [] and refunds == []


def test_failover_refunds_primary_charges_substitute(monkeypatch):
    # Primary groq fails (service_failure) → refunded; substitute together serves.
    result, deducts, refunds = run_once(
        monkeypatch,
        candidates=[_candidate("groq"), _candidate("together")],
        exec_script={
            "groq": ({}, "503 upstream unavailable", 5),
            "together": ({"content": "ok"}, None, 6),
        },
    )
    assert not isinstance(result, HTTPException)
    assert result["failover"]["triggered"] is True
    assert result["failover"]["original_service"] == "groq"
    assert result["service_used"]["slug"] == "together"
    # Ledger: groq deducted then refunded (nets zero); together deducted, not refunded.
    assert ("groq", 3) in deducts and ("together", 4) in deducts
    assert refunds == [("groq", 3)]


# ── streaming smoke (the one unguarded billing path, before the cut) ──────────

def test_streaming_happy_deducts_once_no_refund(monkeypatch):
    from starlette.responses import StreamingResponse
    result, deducts, refunds, stream_refunds = run_stream(
        monkeypatch, candidates=[_candidate("groq")])
    assert isinstance(result, StreamingResponse)   # opened the SSE stream
    assert deducts == [("groq", 3)]                # charged once up front
    assert refunds == [] and stream_refunds == []  # no refund at handler level


def test_streaming_unsupported_slug_deducts_then_refunds(monkeypatch):
    # stream=True but the selected service can't stream → 400 + the deposit is
    # refunded via the direct user_credits UPDATE (not _do_refund).
    from starlette.responses import JSONResponse
    result, deducts, refunds, stream_refunds = run_stream(
        monkeypatch, candidates=[_candidate("serper", category="search")],
        body={"intent": "search the web", "input": {"query": "hi"}})
    assert isinstance(result, JSONResponse) and result.status_code == 400
    assert deducts == [("serper", 3)]   # charged, then…
    assert stream_refunds == [3]        # …fully refunded via user_credits UPDATE
    assert refunds == []                # not the _do_refund path


# ── no-fork guarantee: A2A message/send and /run share ONE money path ─────────

def test_a2a_run_billing_parity(monkeypatch):
    """A2A message/send and POST /run both route through _run_core and bill
    identically for the same intent. Proves there is one money path, not two."""
    import routers.a2a as a2a
    from core.a2a.serializer import Method

    intent = "chat hello"
    user_input = {"messages": [{"role": "user", "content": "hi"}]}
    exec_script = {"groq": ({"content": "hi"}, None, 7)}

    # ── POST /run (non-streaming) ──
    res_run, deducts_run, refunds_run = run_once(
        monkeypatch,
        candidates=[_candidate("groq")],
        exec_script=exec_script,
        body={"intent": intent, "input": user_input},
    )
    assert not isinstance(res_run, HTTPException)
    assert res_run["service_used"]["slug"] == "groq"

    # ── A2A message/send (same intent) — fresh mocks + a spy on _run_core ──
    deducts_a2a, refunds_a2a = _install_mocks(
        monkeypatch, exec_script=exec_script, auth_raises=None, deduct_ok=True)

    core_calls = {"n": 0}
    _real_core = ex._run_core

    async def _spy_core(*a, **k):
        core_calls["n"] += 1
        return await _real_core(*a, **k)
    monkeypatch.setattr(ex, "_run_core", _spy_core)

    message = {
        "role": "user",
        "parts": [
            {"kind": "text", "text": intent},
            {"kind": "data", "data": user_input},
        ],
    }
    req = _FakeReq({})   # carries the API key header + request.state
    out = asyncio.run(
        a2a._dispatch(Method.SEND_MESSAGE, {"message": message},
                      _RunDB([_candidate("groq")]), req))

    # Routed through the SHARED core (the no-fork proof), and the wire result
    # carries the same run payload as POST /run.
    assert core_calls["n"] == 1
    assert out["role"] == "agent"
    assert out["parts"][0]["data"]["service_used"]["slug"] == "groq"

    # Identical billing: same deduct sequence, same (empty) refund sequence.
    assert deducts_a2a == deducts_run == [("groq", 3)]
    assert refunds_a2a == refunds_run == []
