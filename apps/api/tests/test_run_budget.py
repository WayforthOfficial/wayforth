"""test_run_budget.py — loop-aware spend budgets (the enforcement gate).

Enforcement lives in _run_core (the unified money path). These tests drive it with
a ledger-recording fake DB so `spent` is derived from recorded credit_transactions
exactly as in production — never a counter. Covered:

  • a budgeted run stops at the ceiling mid-sequence (over-cap call did NOT deduct,
    typed BudgetExhaustedError fired),
  • ledger-derived spent equals the SUM of that run's transactions (refunds net out),
  • soft-cap crosses the ceiling once (flagged), then hard-stops at ceiling+overage,
  • an unbudgeted call is unchanged (no budget block, deduct as today),
  • the A2A path defaults run_id from contextId and an exhausted run returns the
    -32010 JSON-RPC error envelope.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

import ranker_client
import routers.execute as ex
from core.credits import BudgetExhaustedError, check_run_budget, run_budget_spent

from tests.test_run_core_parity import _FakeReq, _candidate


# ── ledger-recording fake DB ──────────────────────────────────────────────────

class _Tx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *e):
        return False


class _BudgetDB:
    """Answers _run_select's service query, the run_budgets lookup, and the
    ledger-SUM spend query. `ledger` accumulates the (mocked) deducts/refunds so
    check_run_budget derives spent from it, call after call."""
    def __init__(self, candidates, budgets=None, ledger=None):
        self._candidates = candidates
        self._budgets = budgets or {}          # run_id(str) -> {ceiling, soft_cap, max_overage}
        self.ledger: list[dict] = list(ledger or [])  # {run_id, user_id, amount}

    async def fetch(self, q, *a):
        if "FROM services" in q:
            return self._candidates
        return []

    async def fetchrow(self, q, *a):
        if "FROM run_budgets" in q:
            return self._budgets.get(str(a[0]))   # scoped by run_id; None → unbudgeted
        return None

    async def fetchval(self, q, *a):
        if "SUM(-amount)" in q:
            run_id, user_id = str(a[0]), str(a[1])
            return sum(-tx["amount"] for tx in self.ledger
                       if tx["run_id"] == run_id and tx["user_id"] == user_id)
        return None

    async def execute(self, q, *a):
        return None

    def transaction(self):
        return _Tx()


def _install(monkeypatch, deducts):
    """Stub every effect _run_core touches; the deduct records into db.ledger so the
    ledger-derived budget check sees cumulative spend across calls."""
    for var in ("GROQ_API_KEY", "TOGETHER_API_KEY", "MISTRAL_API_KEY", "SERPER_API_KEY"):
        monkeypatch.setenv(var, "K")

    async def fake_resolve(db, key):
        return "user-1", "key-1", "growth"

    async def fake_rate(*a, **k):
        return None

    async def fake_rank(intent, cands):
        return list(cands)

    async def fake_exec(slug, params, key):
        return ({"content": "hi"}, None, 5)

    async def fake_deduct(db, user_id, cost, endpoint, **kw):
        rid = kw.get("run_id")
        db.ledger.append({"run_id": str(rid) if rid else None,
                          "user_id": str(user_id), "amount": -cost})
        deducts.append((kw.get("service_id"), cost))
        bal = 1000 - cost
        if kw.get("return_tx_id"):
            return True, bal, f"tx-{kw.get('service_id')}"
        return True, bal

    async def fake_incr(pool, key_id, cost=0):
        return None

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(ex, "_resolve_user", fake_resolve)
    monkeypatch.setattr(ex, "check_rate_limit", fake_rate)
    monkeypatch.setattr(ranker_client, "rank_services", fake_rank)
    monkeypatch.setattr(ex, "_try_execute_managed", fake_exec)
    monkeypatch.setattr(ex, "check_and_deduct_credits", fake_deduct)
    monkeypatch.setattr(ex, "_increment_calls", fake_incr)
    monkeypatch.setattr(ex, "_fetch_wri", _noop)
    monkeypatch.setattr(ex, "_update_search_signal", _noop)
    monkeypatch.setattr(ex, "_maybe_dispatch_credits_low", _noop)
    monkeypatch.setattr(ex, "_check_spend_anomaly", _noop)
    monkeypatch.setattr(ex, "_patch_tx_signals", _noop)
    from main import app
    if not hasattr(app.state, "pool"):
        app.state.pool = object()


_RUN_A = "11111111-1111-4111-8111-111111111111"


async def _core(db, run_id):
    return await ex._run_core(
        db, user_id="user-1", api_key_id="key-1", tier="growth",
        intent="chat hello", input={"messages": [{"role": "user", "content": "hi"}]},
        prefs={}, agent_id=None, api_key_header="wf_live_" + "x" * 43,
        request_id="req-budget", pool=object(), run_id=run_id)


# ── 1. hard stop at the ceiling, mid-sequence ─────────────────────────────────

def test_budgeted_run_hard_stops_at_ceiling(monkeypatch):
    deducts: list = []
    _install(monkeypatch, deducts)
    # ceiling 7, groq costs 3 → calls at spent 0,3 succeed; the call at spent 6
    # (6+3=9 > 7) is refused before any deduct.
    db = _BudgetDB([_candidate("groq")],
                   budgets={_RUN_A: {"ceiling": 7, "soft_cap": False, "max_overage": 0}})

    async def go():
        r1 = await _core(db, _RUN_A)
        r2 = await _core(db, _RUN_A)
        assert r1["budget"]["spent"] == 3 and r2["budget"]["spent"] == 6
        assert r1["budget"]["over_soft_cap"] is False
        with pytest.raises(BudgetExhaustedError) as ei:
            await _core(db, _RUN_A)
        return ei.value

    err = asyncio.run(go())
    assert err.status_code == 402
    assert err.detail["error"] == "run_budget_exhausted"
    assert err.detail["spent"] == 6 and err.detail["attempted"] == 3
    # The refused call did NOT deduct: only the two allowed calls hit the ledger.
    assert deducts == [("groq", 3), ("groq", 3)]
    assert len(db.ledger) == 2


# ── 2. spent is ledger-derived (and nets out refunds) ─────────────────────────

def test_spent_is_ledger_derived_and_nets_refunds(monkeypatch):
    deducts: list = []
    _install(monkeypatch, deducts)
    db = _BudgetDB([_candidate("groq")],
                   budgets={_RUN_A: {"ceiling": 100, "soft_cap": False, "max_overage": 0}})

    async def go():
        await _core(db, _RUN_A)
        await _core(db, _RUN_A)   # ledger: two -3 debits → spent 6
        spent_before = await run_budget_spent(db, _RUN_A, "user-1")
        # A refund (positive amount) for this run nets out of spend.
        db.ledger.append({"run_id": _RUN_A, "user_id": "user-1", "amount": +3})
        spent_after = await run_budget_spent(db, _RUN_A, "user-1")
        return spent_before, spent_after

    spent_before, spent_after = asyncio.run(go())
    # Derived spend == exact SUM(-amount) of that run's ledger rows, both times.
    assert spent_before == 6
    assert spent_after == 3
    assert spent_after == sum(-tx["amount"] for tx in db.ledger
                              if tx["run_id"] == _RUN_A and tx["user_id"] == "user-1")


# ── 3. soft cap: cross once (flagged), then hard-stop at ceiling+overage ───────

def test_soft_cap_crosses_once_then_hard_stops(monkeypatch):
    deducts: list = []
    _install(monkeypatch, deducts)
    # ceiling 5, soft, overage 3 → hard ceiling 8. cost 3.
    db = _BudgetDB([_candidate("groq")],
                   budgets={_RUN_A: {"ceiling": 5, "soft_cap": True, "max_overage": 3}})

    async def go():
        r1 = await _core(db, _RUN_A)                 # spent 0→3, under ceiling
        r2 = await _core(db, _RUN_A)                 # spent 3→6, crosses 5 (≤8): allowed+flagged
        with pytest.raises(BudgetExhaustedError) as ei:
            await _core(db, _RUN_A)                  # spent 6, 6+3=9 > 8: hard stop
        return r1, r2, ei.value

    r1, r2, err = asyncio.run(go())
    assert r1["budget"]["over_soft_cap"] is False        # stayed under the soft line
    assert r2["budget"]["over_soft_cap"] is True         # crossed it — flagged, not refused
    assert r2["budget"]["spent"] == 6
    assert err.detail["error"] == "run_budget_exhausted"
    assert err.detail["hard_ceiling"] == 8               # ceiling + max_overage
    assert deducts == [("groq", 3), ("groq", 3)]         # the hard-stopped call never deducted


# ── 4. unbudgeted: unchanged (no budget block, deduct as today) ───────────────

def test_unbudgeted_call_unchanged(monkeypatch):
    deducts: list = []
    _install(monkeypatch, deducts)
    db = _BudgetDB([_candidate("groq")])   # no budgets at all

    # run_id None → unbudgeted
    r_none = asyncio.run(_core(db, None))
    # run_id set but no run_budgets row → still unbudgeted
    r_unbudgeted = asyncio.run(_core(db, _RUN_A))

    for r in (r_none, r_unbudgeted):
        assert "budget" not in r                  # no budget block on unbudgeted results
        assert r["service_used"]["slug"] == "groq"
    assert deducts == [("groq", 3), ("groq", 3)]  # both deducted normally


# ── 5. A2A: run_id defaults from contextId; exhaustion → -32010 JSON-RPC error ─

def test_a2a_contextid_budget_exhaustion_is_jsonrpc_error(monkeypatch):
    import a2a.types as A
    import core.a2a.serializer as S
    import routers.a2a as a2a_router
    from core.a2a.serializer import JsonRpcError, Method

    deducts: list = []
    _install(monkeypatch, deducts)

    ctx = "22222222-2222-4222-8222-222222222222"
    # Budget keyed on the contextId, pre-seeded to the ceiling so the next call exhausts.
    db = _BudgetDB(
        [_candidate("groq")],
        budgets={ctx: {"ceiling": 5, "soft_cap": False, "max_overage": 0}},
        ledger=[{"run_id": ctx, "user_id": "user-1", "amount": -5}],
    )

    message = {
        "role": "user",
        "contextId": ctx,                       # ← becomes the run_id (loop budget id)
        "parts": [{"kind": "text", "text": "chat hello"},
                  {"kind": "data", "data": {"messages": [{"role": "user", "content": "hi"}]}}],
    }

    async def go():
        with pytest.raises(JsonRpcError) as ei:
            await a2a_router._dispatch(
                Method.SEND_MESSAGE, {"message": message}, db, _FakeReq({}), "rpc-1")
        return ei.value

    err = asyncio.run(go())
    assert err.code.value == -32010                     # INSUFFICIENT_CREDITS mapping reused
    assert deducts == []                                # exhausted run never deducted

    # And the rendered envelope is a real a2a-sdk JSONRPCErrorResponse.
    envelope = S.make_error_response("rpc-1", err)
    sdk_err = A.JSONRPCErrorResponse.model_validate(envelope)
    assert sdk_err.error.code == -32010
    assert sdk_err.error.data["error"] == "run_budget_exhausted"
