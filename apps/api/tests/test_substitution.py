"""test_substitution.py — deterministic substitution/failover engine (v0.9.1).

Pure-unit: the engine's external helpers (provider execution, deduct, refund,
group loader) are monkeypatched, so no DB/network is touched. Covers ordering,
depth cap, settlement classification + idempotency gate, served-only billing,
the 502-on-exhaustion shape, and the substitution_events emission.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from core import substitution as sub
from core.substitution import FailoverPolicy, run_with_failover
import routers.execute as _ex
from routers.execute import _SETTLEMENT_PRE, _SETTLEMENT_POST


# ── helpers ───────────────────────────────────────────────────────────────────

_OK_SEARCH = {"organic": [{"title": "r", "link": "u", "snippet": "s"}]}


def _fake_exec(script: dict):
    """script: slug -> (result, err, ms, settlement). Default = success."""
    async def fake(slug, params, key, **kw):
        return script.get(slug, (_OK_SEARCH, None, 5, _SETTLEMENT_PRE))
    return fake


@pytest.fixture
def harness(monkeypatch):
    # Every managed provider needs a key present, else the engine skips it as
    # "no_key" before it can be attempted.
    from services.managed import SERVICE_CONFIGS
    for _cfg in SERVICE_CONFIGS.values():
        monkeypatch.setenv(_cfg["key_var"], "TESTKEY")

    deducts: list = []
    refunds: list = []
    events: list = []

    async def fake_deduct(db, user_id, cost, endpoint, **kw):
        deducts.append((kw.get("service_id"), cost))
        return True, 1000 - cost, f"tx_{kw.get('service_id')}"

    async def fake_refund(db, user_id, cost, slug, err, ep, bal, key):
        refunds.append((slug, cost, key))
        return (bal or 0) + cost

    monkeypatch.setattr(sub, "check_and_deduct_credits", fake_deduct)
    monkeypatch.setattr(sub, "_do_refund", fake_refund)
    monkeypatch.setattr(sub, "_emit_event", lambda pool, **cols: events.append(cols))

    def set_chain(category, chain):
        async def fake_chain(db, primary):
            return category, [s for s in chain if s != primary]
        monkeypatch.setattr(sub, "get_substitution_chain", fake_chain)

    def set_exec(script):
        monkeypatch.setattr(sub, "_try_execute_managed_ex", _fake_exec(script))

    return {"deducts": deducts, "refunds": refunds, "events": events,
            "set_chain": set_chain, "set_exec": set_exec}


async def _run(primary="serper", primary_err="Service timeout",
               primary_settlement=_SETTLEMENT_PRE, rail="managed", policy=None):
    # Chain-focused tests run with retry-first OFF so they exercise substitution
    # directly; the retry-first test passes its own policy.
    if policy is None:
        policy = FailoverPolicy(retry_primary_on_transient=False)
    return await run_with_failover(
        db=None, pool=None, request_id="req1",
        user_id="u1", api_key_id="k1", agent_id=None,
        primary_slug=primary, user_params={"query": "hello"},
        primary_error=primary_err, primary_settlement=primary_settlement,
        primary_cost=3, primary_balance_after=997, primary_tx_id="tx_serper",
        primary_svc_key="KEY", rail=rail, policy=policy,
    )


# ── 0. classification matrix (the #5 idempotency core) ────────────────────────

@pytest.mark.parametrize("exc,expected", [
    (httpx.ConnectError("x"),   _SETTLEMENT_PRE),   # no TCP connection
    (httpx.ConnectTimeout("x"), _SETTLEMENT_PRE),   # never connected
    (httpx.PoolTimeout("x"),    _SETTLEMENT_PRE),   # never acquired a connection
    (httpx.ReadTimeout("x"),    _SETTLEMENT_POST),  # sent, response lost
    (httpx.WriteError("x"),     _SETTLEMENT_POST),  # partial write may have landed
    (httpx.WriteTimeout("x"),   _SETTLEMENT_POST),  # partial write may have landed
])
def test_classification_matrix_httpx(monkeypatch, exc, expected):
    async def boom(params, key):
        raise exc
    monkeypatch.setitem(_ex.ADAPTERS, "serper", boom)
    _r, err, _ms, settle = asyncio.run(_ex._try_execute_managed_ex("serper", {"query": "x"}, "K"))
    assert err and settle == expected


@pytest.mark.parametrize("msg,expected", [
    ("Brave Search error 503: down", _SETTLEMENT_PRE),   # 5xx received → fail-over-safe
    ("error 500 internal",           _SETTLEMENT_PRE),
    ("rate limited 429",             _SETTLEMENT_PRE),    # 429 received → fail-over-safe
    ("totally weird no status",      _SETTLEMENT_POST),   # unclassifiable → fail safe
])
def test_classification_matrix_http_status(monkeypatch, msg, expected):
    async def boom(params, key):
        raise Exception(msg)
    monkeypatch.setitem(_ex.ADAPTERS, "serper", boom)
    _r, err, _ms, settle = asyncio.run(_ex._try_execute_managed_ex("serper", {"query": "x"}, "K"))
    assert err and settle == expected


# ── 1. group ordering (real loader, seed + wri) ────────────────────────────────

class _OrderDB:
    """Fake DB routing the loader's queries; provides wri for some members."""
    def __init__(self, wri):
        self._wri = wri
    async def fetchrow(self, q, *a):
        return {"category": "web-search"}  # primary belongs to web-search
    async def fetch(self, q, *a):
        if "substitution_groups" in q:
            return [{"service_slug": s, "manual_rank": r}
                    for s, r in (("serper", 1), ("brave", 2), ("tavily", 3), ("perplexity", 4))]
        # services wri query — catalog slugs
        return [{"slug": k, "wri_score": v} for k, v in self._wri.items()]


def test_ordering_manual_rank_when_no_wri():
    sub._CHAIN_CACHE.clear()
    cat, chain = asyncio.run(sub.get_substitution_chain(_OrderDB({}), "serper"))
    assert cat == "web-search"
    assert chain == ["brave", "tavily", "perplexity"]  # curated manual_rank, primary excluded


def test_ordering_wri_dominates_when_present():
    sub._CHAIN_CACHE.clear()
    # perplexity catalog slug is perplexity_ai; give it the top score.
    db = _OrderDB({"perplexity_ai": 95.0, "brave_search": 60.0})
    cat, chain = asyncio.run(sub.get_substitution_chain(db, "serper"))
    # wri-having first by wri desc (perplexity, brave), then the rest by manual_rank (tavily)
    assert chain[0] == "perplexity" and chain[1] == "brave"
    assert "tavily" in chain and "serper" not in chain


def test_never_out_of_group():
    sub._CHAIN_CACHE.clear()
    _, chain = asyncio.run(sub.get_substitution_chain(_OrderDB({}), "serper"))
    assert all(s in {"brave", "tavily", "perplexity"} for s in chain)
    assert "groq" not in chain  # llm member never leaks into web-search


# ── 2. chain + billing: charge served-only ────────────────────────────────────

def test_chains_until_success_and_bills_served_only(harness):
    harness["set_chain"]("web-search", ["brave", "tavily", "perplexity"])
    harness["set_exec"]({
        "brave": ({}, "Brave Search error 503", 5, _SETTLEMENT_PRE),  # fails
        "tavily": ({"results": [{"title": "t"}]}, None, 7, _SETTLEMENT_PRE),  # serves
    })
    out = asyncio.run(_run())
    assert out.served_slug == "tavily"
    assert out.fallback_from == "serper"
    # billed: brave (refunded) + tavily (served); primary serper refunded.
    assert ("brave", 6) in harness["deducts"] and ("tavily", 10) in harness["deducts"]
    refunded_slugs = [r[0] for r in harness["refunds"]]
    assert "serper" in refunded_slugs and "brave" in refunded_slugs
    assert "tavily" not in refunded_slugs  # served provider is NOT refunded
    # per-hop refund keys are distinct (no collision)
    assert len({r[2] for r in harness["refunds"]}) == len(harness["refunds"])


# ── 3. depth cap + 502 on exhaustion ──────────────────────────────────────────

def test_depth_cap_and_502_shape(harness):
    harness["set_chain"]("web-search", ["brave", "tavily", "perplexity"])
    harness["set_exec"]({  # all fail pre_send
        "brave": ({}, "err 503", 5, _SETTLEMENT_PRE),
        "tavily": ({}, "err 503", 5, _SETTLEMENT_PRE),
        "perplexity": ({}, "err 503", 5, _SETTLEMENT_PRE),
    })
    out = asyncio.run(_run(policy=FailoverPolicy(max_depth=2, retry_primary_on_transient=False)))
    assert out.served_slug is None
    tried = [s for s, _ in out.providers_tried]
    assert tried[0] == "serper"  # primary
    assert tried[1:] == ["brave", "tavily"]  # exactly max_depth=2 candidates, not perplexity


# ── 4. idempotency gate: pre vs post settlement ───────────────────────────────

def test_pre_send_fails_over(harness):
    harness["set_chain"]("web-search", ["brave"])
    harness["set_exec"]({"brave": (_OK_SEARCH, None, 5, _SETTLEMENT_PRE)})
    out = asyncio.run(_run(primary_settlement=_SETTLEMENT_PRE))
    assert out.served_slug == "brave"


def test_post_send_managed_fails_over_under_cap(harness):
    harness["set_chain"]("web-search", ["brave"])
    harness["set_exec"]({"brave": (_OK_SEARCH, None, 5, _SETTLEMENT_PRE)})
    out = asyncio.run(_run(primary_settlement=_SETTLEMENT_POST, rail="managed",
                           policy=FailoverPolicy(retry_primary_on_transient=False)))
    assert out.served_slug == "brave"
    # the substitute hop is flagged as a possible duplicate-upstream cost
    ev = [e for e in harness["events"] if e.get("slug") == "brave"][0]
    assert ev["duplicate_upstream_cost_possible"] is True
    assert ev["second_upstream_cost_credits"] == 6


def test_post_send_x402_does_not_fail_over(harness):
    harness["set_chain"]("web-search", ["brave"])
    harness["set_exec"]({"brave": (_OK_SEARCH, None, 5, _SETTLEMENT_PRE)})
    out = asyncio.run(_run(primary_settlement=_SETTLEMENT_POST, rail="x402"))
    assert out.served_slug is None  # strict on on-chain — surfaces instead
    assert ("brave", 6) not in harness["deducts"]  # never even attempted


def test_post_send_over_cost_cap_skips_expensive(harness):
    # stability (86 cr) is over the default 25-cap → skipped on a post-send origin.
    harness["set_chain"]("media", ["stability"])
    harness["set_exec"]({"stability": ({"image_base64": "x"}, None, 5, _SETTLEMENT_PRE)})
    out = asyncio.run(_run(primary="elevenlabs", primary_settlement=_SETTLEMENT_POST,
                           policy=FailoverPolicy(retry_primary_on_transient=False)))
    assert out.served_slug is None
    assert ("stability", 86) not in harness["deducts"]
    assert ("stability", "over_post_send_cost_cap") in out.providers_tried
    # Refund safety: the primary is STILL refunded even though nothing served —
    # the user is never charged-with-no-result-and-no-refund.
    assert "elevenlabs" in [r[0] for r in harness["refunds"]]


def test_post_send_flag_off_strict(harness):
    harness["set_chain"]("web-search", ["brave"])
    harness["set_exec"]({"brave": (_OK_SEARCH, None, 5, _SETTLEMENT_PRE)})
    out = asyncio.run(_run(primary_settlement=_SETTLEMENT_POST,
                           policy=FailoverPolicy(failover_post_send=False)))
    assert out.served_slug is None


# ── 5. retry-first on read-timeout ────────────────────────────────────────────

def test_retry_first_pre_send_succeeds_keeps_primary_charge(harness):
    # pre_send retry is always safe (no upstream work happened on the first try).
    harness["set_chain"]("web-search", ["brave"])
    harness["set_exec"]({"serper": (_OK_SEARCH, None, 9, _SETTLEMENT_PRE)})
    out = asyncio.run(_run(primary_settlement=_SETTLEMENT_PRE,
                           policy=FailoverPolicy(retry_primary_on_transient=True)))
    assert out.served_slug == "serper" and out.retried_primary is True
    assert harness["refunds"] == []  # primary charge stands; nothing refunded
    assert harness["deducts"] == []  # no substitute deducted


def test_post_send_retry_skipped_without_idempotency_key(harness):
    # post_send + no provider idempotency key → retry-first is SKIPPED (it would
    # create the duplicate cost it's meant to prevent) → substitute instead. The
    # served provider is the substitute, NOT a retried primary.
    harness["set_chain"]("web-search", ["brave"])
    harness["set_exec"]({
        "serper": (_OK_SEARCH, None, 9, _SETTLEMENT_PRE),  # would serve IF retried
        "brave": (_OK_SEARCH, None, 5, _SETTLEMENT_PRE),
    })
    out = asyncio.run(_run(primary_settlement=_SETTLEMENT_POST, rail="managed",
                           policy=FailoverPolicy(retry_primary_on_transient=True)))
    assert out.served_slug == "brave"          # substitute served, not a retried serper
    assert ("brave", 6) in harness["deducts"]
    assert out.retried_primary is False        # retry never ran


# ── 6. invalid body after 200 → treated as post-send ──────────────────────────

def test_invalid_body_after_200_does_not_serve(harness):
    harness["set_chain"]("web-search", ["brave"])
    harness["set_exec"]({"brave": ({"organic": []}, None, 5, _SETTLEMENT_PRE)})  # empty → invalid
    out = asyncio.run(_run(primary_settlement=_SETTLEMENT_PRE))
    # brave returned 200 but empty → invalid → refunded, not served
    assert out.served_slug is None
    assert "brave" in [r[0] for r in harness["refunds"]]


# ── 7. event row emitted per hop with settlement_class ────────────────────────

def test_events_emitted_per_hop(harness):
    harness["set_chain"]("web-search", ["brave", "tavily"])
    harness["set_exec"]({
        "brave": ({}, "err 503", 5, _SETTLEMENT_PRE),
        "tavily": ({"results": [{"t": 1}]}, None, 6, _SETTLEMENT_PRE),
    })
    asyncio.run(_run())
    slugs = [e["slug"] for e in harness["events"]]
    assert "brave" in slugs and "tavily" in slugs
    for e in harness["events"]:
        assert e["settlement_class"] in (_SETTLEMENT_PRE, _SETTLEMENT_POST)
        assert e["primary_provider"] == "serper"
