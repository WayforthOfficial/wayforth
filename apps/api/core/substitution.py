"""core/substitution.py — deterministic substitution / failover engine (v0.9.1).

Extends the Reliability Proxy from single-hop into a config-driven, multi-hop
failover across an equivalence GROUP of interchangeable providers. The proxy
calls run_with_failover() ONLY on the failure branch — the primary success path
never touches this module (zero-overhead guarantee).

What it does, per the approved plan:
  * classify the primary failure as pre_send vs post_send_ambiguous (idempotency);
  * optional retry-first on the SAME provider for a read-timeout before substituting;
  * chain through the group (ordered by wri_score when present, else curated
    manual_rank — never sorts on nulls, never random), depth-capped;
  * bill ONLY the provider that actually served (deduct→run→refund-on-fail→next),
    so the surviving ledger row is the served provider + its embedded 1.5% fee;
  * gate post-send-ambiguous failover (managed rail only, under a cost cap, with
    full instrumentation; x402/on-chain is strict — never fails over post-send);
  * log every attempt to substitution_events (future learned-layer training signal).

ML-ranked selection is a later phase; this is the deterministic layer.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time as _time
from dataclasses import dataclass, field

from services.managed import SERVICE_CONFIGS
from services.param_mapper import MANAGED_TO_CATALOG, map_params

# Helpers reused from the execute path. Imported at module level so tests can
# monkeypatch core.substitution.<name>.
from core.credits import check_and_deduct_credits
from routers.execute import (
    _classify_failure,
    _do_refund,
    _mk_refund_key,
    _try_execute_managed_ex,
    _SETTLEMENT_PRE,
    _SETTLEMENT_POST,
)

logger = logging.getLogger("wayforth")

# Curated seed (mirrors migration 064). Used when the DB table is empty/unreachable
# and by unit tests. DB rows override this. (category -> [(slug, manual_rank), ...])
_SEED_GROUPS: dict[str, list[tuple[str, int]]] = {
    "web-search":    [("serper", 1), ("brave", 2), ("tavily", 3), ("perplexity", 4)],
    "llm-inference": [("groq", 1), ("together", 2), ("mistral", 3), ("gemini", 4)],
}

_LLM_SLUGS = frozenset({"groq", "together", "mistral", "gemini", "perplexity"})

# Providers that honor a client idempotency key on retry (so a same-provider
# retry after a post-send-ambiguous failure is server-side deduped, not a second
# billable call). EMPTY today — no managed adapter passes one yet. When a provider
# gains support, add its slug here AND pass the key in the retry call below. Until
# then, a post-send retry would CREATE the duplicate cost it is meant to prevent,
# so it is skipped (we go straight to the cost-capped, instrumented substitution).
_IDEMPOTENCY_KEY_PROVIDERS: frozenset[str] = frozenset()


def _supports_idempotency_key(slug: str) -> bool:
    return slug in _IDEMPOTENCY_KEY_PROVIDERS


# Provider-specific params that CANNOT carry verbatim to a different provider in
# the group. For llm-inference, an explicitly-pinned `model` is a vendor model
# name a substitute cannot run as-is. Rather than strip it (silent wrong model)
# or always surface (no self-heal), we resolve a curated TIER-equivalent on the
# substitute when one exists (see _MODEL_TIER / _TIER_MODEL), and only surface
# when no substitute has a known equivalent. Unpinned calls self-heal as before.
def _pinned_unhonorable_param(category: str | None, user_params: dict) -> tuple[str, str] | None:
    if category == "llm-inference" and user_params.get("model"):
        return ("model", str(user_params["model"]))
    return None


# Curated model-equivalence map (llm-inference). A pinned vendor model maps to a
# coarse capability TIER; each provider in the group has a representative model
# per tier. This lets a pinned-model call self-heal to a tier-equivalent model on
# a substitute provider — surfaced honestly via X-Wayforth-Substituted-Model —
# instead of hard-failing. A pinned model NOT in this table has no known
# equivalent, so we still surface (never silently serve a guessed model). Keys
# are lowercased for lookup; _TIER_MODEL values keep exact vendor casing for the
# actual upstream call. Defaults the adapters use today live in services/managed.py.
_MODEL_TIER: dict[str, str] = {
    # large / ~70B-class instruct
    "llama-3.3-70b-versatile": "large",
    "llama-3.1-70b-versatile": "large",
    "meta-llama/llama-3.3-70b-instruct-turbo": "large",
    "meta-llama/llama-3.1-70b-instruct-turbo": "large",
    "mistral-large-latest": "large",
    "gemini-2.5-pro": "large",
    # small / fast
    "llama-3.1-8b-instant": "small",
    "meta-llama/llama-3.1-8b-instruct-turbo": "small",
    "mistral-small-latest": "small",
    "gemini-2.5-flash": "small",
}

_TIER_MODEL: dict[str, dict[str, str]] = {
    "large": {
        "groq": "llama-3.3-70b-versatile",
        "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
        "mistral": "mistral-large-latest",
        "gemini": "gemini-2.5-pro",
    },
    "small": {
        "groq": "llama-3.1-8b-instant",
        "together": "meta-llama/Llama-3.1-8B-Instruct-Turbo",
        "mistral": "mistral-small-latest",
        "gemini": "gemini-2.5-flash",
    },
}


def _model_equivalent_for(pinned_model: str, candidate_slug: str) -> str | None:
    """Tier-equivalent model on candidate_slug for a pinned vendor model, or None
    when the pinned model has no known tier or the candidate has no model in it."""
    tier = _MODEL_TIER.get(pinned_model.strip().lower())
    if not tier:
        return None
    return _TIER_MODEL.get(tier, {}).get(candidate_slug)

# Per-category in-process cache of the ordered chain (TTL bounded). Keyed by
# category; value = (expiry_monotonic, [ordered_slugs]).
_CHAIN_CACHE: dict[str, tuple[float, list[str]]] = {}
_CACHE_TTL = 300.0


@dataclass
class FailoverPolicy:
    max_depth: int = int(os.environ.get("WAYFORTH_FAILOVER_MAX_DEPTH", "3"))
    retry_primary_on_transient: bool = (
        os.environ.get("WAYFORTH_FAILOVER_RETRY_PRIMARY", "true").lower() == "true"
    )
    # Managed-rail default: DO fail over on post-send-ambiguous (user is always
    # refunded). Flip to false to be strict. x402/on-chain is ALWAYS strict
    # regardless of this flag (see the rail check in run_with_failover).
    failover_post_send: bool = (
        os.environ.get("WAYFORTH_FAILOVER_POST_SEND", "true").lower() == "true"
    )
    # Cap the duplicate-upstream risk: only auto-fail-over post-send when the
    # candidate's cost is below this (credits). Micro-calls clear it; an expensive
    # outlier never gets double-paid.
    failover_post_send_max_cost: int = int(
        os.environ.get("WAYFORTH_FAILOVER_POST_SEND_MAX_COST", "25")
    )


DEFAULT_POLICY = FailoverPolicy()


@dataclass
class FailoverOutcome:
    served_slug: str | None = None
    result: object = None
    cost: int = 0
    balance_after: int = 0
    tx_id: object = None
    fallback_from: str | None = None
    category: str | None = None
    original_failure_code: str | None = None
    settlement_class: str = _SETTLEMENT_PRE
    execution_ms: int = 0
    retried_primary: bool = False
    client_error: str | None = None  # set when a hop failed with a non-service (bad-param) error
    substituted_model: tuple[str, str] | None = None  # (pinned, served tier-equivalent) when remapped
    providers_tried: list[tuple[str, str]] = field(default_factory=list)


# ── group loader ──────────────────────────────────────────────────────────────


def _seed_category_of(slug: str) -> str | None:
    for cat, members in _SEED_GROUPS.items():
        if any(m[0] == slug for m in members):
            return cat
    return None


async def get_substitution_chain(db, primary_slug: str) -> tuple[str | None, list[str]]:
    """Return (category, ordered substitute slugs excluding the primary).

    Order: COALESCE(wri_score, -1) DESC, manual_rank ASC, slug ASC — wri dominates
    WHEN present (post-launch); pre-launch all wri are null so the curated
    manual_rank is the deterministic baseline. Cached per category. Falls back to
    the in-module seed if the DB is empty/unreachable, so the engine + tests run
    without a populated table.
    """
    # Resolve the primary's category (DB first, then seed).
    category: str | None = None
    members: list[tuple[str, int | None]] = []  # (slug, manual_rank)
    try:
        cat_row = await db.fetchrow(
            "SELECT category FROM substitution_groups WHERE service_slug = $1 AND active = TRUE LIMIT 1",
            primary_slug,
        )
        if cat_row:
            category = cat_row["category"]
            rows = await db.fetch(
                "SELECT service_slug, manual_rank FROM substitution_groups "
                "WHERE category = $1 AND active = TRUE",
                category,
            )
            members = [(r["service_slug"], r["manual_rank"]) for r in rows]
    except Exception as exc:  # DB unreachable / table missing → seed fallback
        logger.warning("substitution group DB lookup failed (%s); using seed", exc)
        category = None

    if not members:
        category = _seed_category_of(primary_slug)
        if not category:
            return None, []
        members = [(s, r) for s, r in _SEED_GROUPS[category]]

    now = _time.monotonic()
    cached = _CHAIN_CACHE.get(category)
    if cached and cached[0] > now:
        return category, [s for s in cached[1] if s != primary_slug]

    # WRI per member from the services table (catalog slug via MANAGED_TO_CATALOG).
    wri: dict[str, float] = {}
    try:
        cat_slugs = {s: MANAGED_TO_CATALOG.get(s, s) for s, _ in members}
        rows = await db.fetch(
            "SELECT slug, wri_score FROM services WHERE slug = ANY($1::text[])",
            list(cat_slugs.values()),
        )
        by_catalog = {r["slug"]: r["wri_score"] for r in rows}
        for s, cslug in cat_slugs.items():
            v = by_catalog.get(cslug)
            if v is not None:
                wri[s] = float(v)
    except Exception:
        wri = {}  # no rank data yet → ordering falls back to manual_rank

    ordered = sorted(
        members,
        key=lambda m: (-(wri.get(m[0], -1.0)), (m[1] if m[1] is not None else 999), m[0]),
    )
    ordered_slugs = [m[0] for m in ordered]
    _CHAIN_CACHE[category] = (now + _CACHE_TTL, ordered_slugs)
    return category, [s for s in ordered_slugs if s != primary_slug]


# ── category validity checks (empty/malformed body detection) ──────────────────


def _validate_result(category: str | None, slug: str, result) -> bool:
    """True if the HTTP-200 result is a usable body for its category."""
    if result is None:
        return False
    if not isinstance(result, dict):
        return bool(result)
    if category == "web-search":
        items = result.get("organic") or result.get("results") or result.get("web", {}).get("results")
        return bool(items)
    if category == "llm-inference":
        return bool(result.get("content") or result.get("choices"))
    return True  # unknown category → don't reject


# ── event log (fire-and-forget) ────────────────────────────────────────────────


async def _record_event(pool, **cols) -> None:
    if pool is None:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO substitution_events
                  (slug, category, primary_provider, failure_reason, substitute_chosen,
                   latency_ms, success, cost_credits, settlement_class, rail,
                   duplicate_upstream_cost_possible, second_upstream_cost_credits,
                   retried_primary)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """,
                cols.get("slug"), cols.get("category"), cols.get("primary_provider"),
                cols.get("failure_reason"), cols.get("substitute_chosen"),
                cols.get("latency_ms"), bool(cols.get("success")), cols.get("cost_credits"),
                cols.get("settlement_class", _SETTLEMENT_PRE), cols.get("rail", "managed"),
                bool(cols.get("duplicate_upstream_cost_possible")),
                cols.get("second_upstream_cost_credits"), bool(cols.get("retried_primary")),
            )
    except Exception as exc:
        logger.warning("substitution_events write failed: %s", exc)


def _emit_event(pool, **cols) -> None:
    """Schedule an event write without blocking the request."""
    try:
        asyncio.create_task(_record_event(pool, **cols))
    except RuntimeError:
        pass  # no running loop (e.g. unit context) — non-critical


# ── the engine ──────────────────────────────────────────────────────────────


async def run_with_failover(
    db, *, pool, request_id: str,
    user_id: str, api_key_id: str | None, agent_id: str | None,
    primary_slug: str, user_params: dict,
    primary_error: str, primary_settlement: str,
    primary_cost: int, primary_balance_after: int, primary_tx_id,
    primary_svc_key: str,
    rail: str = "managed",
    policy: FailoverPolicy | None = None,
) -> FailoverOutcome:
    """Classify → (retry-first) → idempotency-gate → chain → bill → log.

    The primary has already been deducted and run (and failed with a service-side
    error). On entry the primary deduction still stands; we only refund it once we
    commit to substituting (or after a failed retry). Returns a FailoverOutcome the
    caller adopts (served provider) or turns into a 502 (served_slug is None).
    """
    policy = policy or DEFAULT_POLICY
    failure_code = _classify_failure(None, primary_error)
    category, chain = await get_substitution_chain(db, primary_slug)
    settlement = primary_settlement
    retried = False
    out = FailoverOutcome(category=category, original_failure_code=failure_code,
                          balance_after=primary_balance_after, settlement_class=settlement)
    out.providers_tried.append((primary_slug, failure_code))

    def _may_act(s: str) -> bool:
        # pre_send is always safe to act on. post_send may double-charge upstream
        # / double-settle on-chain → only act on the MANAGED rail with the flag on.
        # (Even a same-provider retry repeats the upstream call, so it's gated too.)
        return s == _SETTLEMENT_PRE or (rail == "managed" and policy.failover_post_send)

    # 1. Hard idempotency gate on the primary's settlement. x402/on-chain or
    #    flag-off + post-send → surface immediately (no retry, no substitution).
    if not _may_act(settlement):
        out.settlement_class = settlement
        return out  # served_slug None → caller surfaces

    # 2. Retry-first on the SAME provider, BEFORE refunding — the primary charge
    #    stands, so a successful retry never double-charges and avoids touching a
    #    substitute (and its duplicate-upstream risk). A pre_send retry is always
    #    safe (no upstream work happened). A POST-send retry repeats a call the
    #    upstream may have already run+billed, so it is only safe when the provider
    #    honors an idempotency key — otherwise we SKIP it (going straight to the
    #    cost-capped, instrumented substitution) rather than create duplicate cost.
    _retry_safe = settlement == _SETTLEMENT_PRE or _supports_idempotency_key(primary_slug)
    if policy.retry_primary_on_transient and _retry_safe:
        retried = True
        _mapped, _miss = map_params(primary_slug, user_params)
        r_res, r_err, r_ms, r_settle = await _try_execute_managed_ex(primary_slug, _mapped, primary_svc_key)
        if not r_err and _validate_result(category, primary_slug, r_res):
            _emit_event(pool, slug=primary_slug, category=category, primary_provider=primary_slug,
                        failure_reason=failure_code, substitute_chosen=primary_slug, latency_ms=r_ms,
                        success=True, cost_credits=primary_cost, settlement_class=settlement,
                        rail=rail, retried_primary=True)
            out.served_slug, out.result, out.cost = primary_slug, r_res, primary_cost
            out.tx_id, out.execution_ms, out.retried_primary = primary_tx_id, r_ms, True
            return out
        settlement = r_settle if r_err else _SETTLEMENT_POST  # retry also failed
        if not _may_act(settlement):  # retry turned it ambiguous on a strict rail
            new_bal = await _do_refund(
                db, user_id, primary_cost, primary_slug, primary_error, "/proxy",
                primary_balance_after, _mk_refund_key(request_id, primary_slug, "proxy_primary"))
            out.balance_after = new_bal if new_bal is not None else primary_balance_after
            out.settlement_class, out.retried_primary = settlement, retried
            return out

    # 3. Refund the primary (failed, no successful retry). User never double-charged.
    new_bal = await _do_refund(
        db, user_id, primary_cost, primary_slug, primary_error, "/proxy",
        primary_balance_after, _mk_refund_key(request_id, primary_slug, "proxy_primary"),
    )
    out.balance_after = new_bal if new_bal is not None else primary_balance_after

    origin_post_send = settlement == _SETTLEMENT_POST

    # 3b. Pinned-model handling. A caller-pinned `model` is a vendor name a
    #     substitute cannot run verbatim. If the pinned model has a curated tier
    #     equivalent on at least one substitute, we self-heal to that equivalent
    #     (per-candidate remap happens in the chain loop, surfaced via
    #     out.substituted_model). If NO substitute has a known equivalent, surface
    #     a clean error rather than silently serving a different-tier model.
    _pinned = _pinned_unhonorable_param(category, user_params)
    pinned_model = _pinned[1] if _pinned else None
    if pinned_model and chain:
        has_equiv = any(
            _model_equivalent_for(pinned_model, c) for c in chain[: policy.max_depth]
        )
        if not has_equiv:
            out.client_error = (
                f"primary '{primary_slug}' failed and your request pins model='{pinned_model}', "
                f"which has no curated equivalent on any substitute in the '{category}' group. "
                f"Omit 'model' to self-heal on each provider's default, or pin a model with a known equivalent."
            )
            out.providers_tried.append((primary_slug, "pinned_model_unhonorable"))
            out.settlement_class, out.retried_primary = settlement, retried
            return out

    # 4. Chain through the group, depth-capped.
    for candidate in chain[: policy.max_depth]:
        cfg = SERVICE_CONFIGS.get(candidate)
        if not cfg:
            out.providers_tried.append((candidate, "unknown_service"))
            continue
        cand_key = os.environ.get(cfg["key_var"], "")
        if not cand_key:
            out.providers_tried.append((candidate, "no_key"))
            continue
        cand_cost = cfg["credits"]
        # Cost cap only bites when the ORIGIN failure was post-send (duplicate risk).
        if origin_post_send and cand_cost >= policy.failover_post_send_max_cost:
            out.providers_tried.append((candidate, "over_post_send_cost_cap"))
            continue
        # Pinned-model self-heal: resolve this candidate's curated tier-equivalent
        # BEFORE param mapping. A candidate with no known equivalent is skipped (we
        # never serve a guessed model under a pinned request).
        equiv_model = None
        if pinned_model:
            equiv_model = _model_equivalent_for(pinned_model, candidate)
            if not equiv_model:
                out.providers_tried.append((candidate, "no_model_equivalent"))
                continue
        mapped, missing = map_params(candidate, user_params)
        if missing:
            out.providers_tried.append((candidate, "missing_param"))
            continue
        if equiv_model:
            mapped["model"] = equiv_model

        ok, bal, tx_id = await check_and_deduct_credits(
            db, user_id, cand_cost, "/proxy", service_id=candidate, tx_type="execution",
            agent_id=agent_id, api_key_id=api_key_id, return_tx_id=True,
        )
        if not ok:
            out.providers_tried.append((candidate, "insufficient_credits"))
            out.balance_after = bal
            break

        result, err, ms, hop_settle = await _try_execute_managed_ex(candidate, mapped, cand_key)
        valid = (not err) and _validate_result(category, candidate, result)
        if not valid and not err:
            err, hop_settle = "invalid_body_after_200", _SETTLEMENT_POST

        _emit_event(pool, slug=candidate, category=category, primary_provider=primary_slug,
                    failure_reason=failure_code, substitute_chosen=candidate, latency_ms=ms,
                    success=valid, cost_credits=cand_cost, settlement_class=hop_settle, rail=rail,
                    duplicate_upstream_cost_possible=origin_post_send,
                    second_upstream_cost_credits=cand_cost if origin_post_send else None,
                    retried_primary=retried)

        if valid:
            out.served_slug, out.result, out.cost = candidate, result, cand_cost
            out.balance_after, out.tx_id = bal, tx_id
            out.fallback_from, out.execution_ms, out.retried_primary = primary_slug, ms, retried
            if pinned_model and mapped.get("model") and mapped["model"] != pinned_model:
                out.substituted_model = (pinned_model, mapped["model"])
            return out

        # Hop failed → refund it (per-hop idempotency key) and continue.
        refunded = await _do_refund(
            db, user_id, cand_cost, candidate, err or "failed", "/proxy", bal,
            _mk_refund_key(request_id, candidate, f"proxy_fb_{len(out.providers_tried)}"),
        )
        out.balance_after = refunded if refunded is not None else bal
        hop_code = _classify_failure(None, err)
        out.providers_tried.append((candidate, hop_code))
        # A non-service (bad-param/client) error won't be fixed by another provider.
        from routers.execute import _classify_error
        if _classify_error(err or "") != "service_failure":
            out.client_error = err
            break
        # A post-send hop tightens the gate for subsequent candidates.
        if hop_settle == _SETTLEMENT_POST:
            origin_post_send = True
            if rail != "managed" or not policy.failover_post_send:
                break

    # 5. Exhausted (or stopped) — no provider served.
    out.retried_primary = retried
    return out
