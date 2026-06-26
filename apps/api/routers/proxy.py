"""routers/proxy.py — Reliability Proxy (v0.9.0).

Transparent base-URL swap: POST /proxy/{slug} or GET /proxy/{slug}
Returns the upstream API's native response shape; Wayforth metadata
goes into response headers. Callers get failover + WRI routing +
metered signal write with zero code changes beyond the base URL.

Migration:
    Before:  POST https://api.groq.com/v1/chat/completions
             Authorization: Bearer $GROQ_KEY

    After:   POST https://gateway.wayforth.io/proxy/groq
             X-Wayforth-API-Key: $WAYFORTH_KEY

Response headers:
    X-Wayforth-Failover: true|false
    X-Wayforth-Original-Service / X-Wayforth-Routed-To  (when failover)
    X-Wayforth-Reason                                    (when failover)
    X-Wayforth-Original-WRI                             (when failover)
    X-Wayforth-WRI
    X-Wayforth-Cost
    X-Wayforth-Rail
    X-Wayforth-Credits-Remaining

Optional: ?wayforth_wrap=true returns the full /execute-style envelope.
"""

import asyncio
import json as _json
import logging
import os
import time as _time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from core.auth import _resolve_user, _validate_agent_id
from core.run_token import RUN_TOKEN_PREFIX, SCOPE_PROXY, token_has_scope, verify_run_token
from core.credits import (
    _check_spend_anomaly,
    _increment_calls,
    _maybe_dispatch_credits_low,
    check_and_deduct_credits,
)
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import check_rate_limit
from services.managed import ADAPTERS, SERVICE_ALTERNATIVES, SERVICE_CONFIGS, check_upstream_cap
from services.param_mapper import map_params, missing_param_hint
from routers.execute import (
    _FAILURE_REASON_LABELS,
    _classify_error,
    _classify_failure,
    _do_refund,
    _fetch_wri,
    _mk_refund_key,
    _patch_tx_signals,
    _try_execute_managed_ex,
    _update_search_signal,
)
from core.substitution import run_with_failover

logger = logging.getLogger("wayforth")
router = APIRouter()

_LLM_SLUGS = frozenset({"groq", "together", "mistral", "gemini"})

_ACTIVE_RUN_STATES = ("queued", "running")


async def _resolve_proxy_caller(api_key_header: str, db):
    """Resolve the /proxy caller from either a wf_live_ key or a wf_run_ run token.

    Returns (user_id, api_key_id, tier, forced_agent_id, run_prefetch):
      • user_id, api_key_id (str or None), tier — identity for billing/rate-limit.
      • forced_agent_id — for run tokens, the SIGNED run_id (authoritative; overrides
        any X-Wayforth-Agent-ID header so attribution can't be spoofed). None for keys.
      • run_prefetch — the agent_runs (credits_reserved, status) row for run tokens, so
        the cap check costs no extra read (Guardrail 2). None for keys.

    Raises HTTPException(401) on any failure. The wf_live_ path is unchanged.
    """
    # Run token (agent sandbox) — verify signature/scope, then bind to the live run.
    if api_key_header.startswith(RUN_TOKEN_PREFIX):
        claims = verify_run_token(api_key_header)
        if not claims or not token_has_scope(claims, SCOPE_PROXY):
            raise HTTPException(status_code=401, detail={"error": "invalid_run_token"})
        run_id = str(claims.get("run_id") or "")
        try:
            # Single consolidated load: the run row (binding + cap prefetch) plus the
            # user's tier/key for rate-limit + attribution. ::uuid cast guards a bad id.
            row = await db.fetchrow(
                """
                SELECT ar.user_id, ar.hosted_agent_id, ar.status, ar.credits_reserved,
                       ak.id AS api_key_id, ak.tier
                FROM agent_runs ar
                LEFT JOIN api_keys ak
                       ON ak.user_id = ar.user_id AND ak.active = TRUE
                WHERE ar.id = $1::uuid
                ORDER BY ak.created_at DESC NULLS LAST
                LIMIT 1
                """,
                run_id,
            )
        except Exception:
            raise HTTPException(status_code=401, detail={"error": "invalid_run_token"})
        if not row:
            raise HTTPException(status_code=401, detail={"error": "run_not_found"})
        # Binding: a leaked token can't be replayed for a different user/agent.
        if str(row["user_id"]) != str(claims.get("sub")) or \
           str(row["hosted_agent_id"]) != str(claims.get("agent_id")):
            raise HTTPException(status_code=401, detail={"error": "run_binding_mismatch"})
        # Revocation: the token dies the moment the run leaves an active state.
        if row["status"] not in _ACTIVE_RUN_STATES:
            raise HTTPException(status_code=401, detail={"error": "run_not_active"})
        api_key_id = str(row["api_key_id"]) if row["api_key_id"] else None
        tier = row["tier"] or "free"
        run_prefetch = {"credits_reserved": row["credits_reserved"], "status": row["status"]}
        return str(row["user_id"]), api_key_id, tier, run_id, run_prefetch

    # wf_live_ key (SDK / human / non-agent) — unchanged path.
    user_id, api_key_id, tier = await _resolve_user(db, api_key_header)
    return str(user_id), (str(api_key_id) if api_key_id else None), tier, None, None


@router.api_route("/proxy/{slug}", methods=["GET", "POST"])
@limiter.limit("60/minute")
async def proxy_call(request: Request, slug: str, db=Depends(get_db)):
    """Reliability Proxy — transparent base-URL swap with Wayforth failover + WRI routing.

    POST /proxy/{slug} — JSON body is the native service params dict.
    GET  /proxy/{slug} — query string is mapped to the params dict (OpenWeather-style).

    Returns the upstream API's native response shape. Wayforth metadata in headers.
    Pass ?wayforth_wrap=true for the full /execute-style envelope.
    """
    # ── Auth ──────────────────────────────────────────────────────────────────
    api_key_header = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key_header:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key header required"})

    user_id, _api_key_id, _tier, _forced_agent_id, _run_prefetch = \
        await _resolve_proxy_caller(api_key_header, db)
    await check_rate_limit(_api_key_id or str(user_id), _tier)

    # ── Slug validation (managed-only, v0.9.0) ────────────────────────────────
    slug = slug.strip().lower()
    if slug not in SERVICE_CONFIGS:
        raise HTTPException(status_code=404, detail={
            "error": "service_not_found",
            "service": slug,
            "message": "Unknown managed service slug. Use GET /search?q=<intent> to discover services.",
            "search_endpoint": f"GET /search?q={slug}",
        })

    # ── Params: JSON body (POST) or query string (GET) ─────────────────────────
    wayforth_wrap = request.query_params.get("wayforth_wrap", "").lower() == "true"

    if request.method == "GET":
        params = dict(request.query_params)
        params.pop("wayforth_wrap", None)
        agent_id = _validate_agent_id(params.pop("agent_id", None))
    else:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        agent_id = _validate_agent_id(body.pop("agent_id", None))
        params = body

    # X-Wayforth-Agent-ID header takes priority over body/query param
    header_agent_id = _validate_agent_id(request.headers.get("X-Wayforth-Agent-ID"))
    if header_agent_id:
        agent_id = header_agent_id
    # Run-token auth: the SIGNED run_id is authoritative — it overrides any header so
    # billing/cap attribution can't be spoofed via X-Wayforth-Agent-ID.
    if _forced_agent_id:
        agent_id = _forced_agent_id

    # ── Param validation ──────────────────────────────────────────────────────
    # Save user-supplied params before service-specific defaults are injected so
    # failover can re-map cleanly without leaking the primary's default model name.
    _user_params = dict(params)
    params, _missing = map_params(slug, params)
    if _missing:
        raise HTTPException(status_code=422, detail={
            "error": "missing_param",
            "missing": _missing,
            "hint": missing_param_hint(_missing),
        })

    # ── Resolve credit cost ───────────────────────────────────────────────────
    config = SERVICE_CONFIGS[slug]
    if slug == "stability":
        credit_cost = 150 if params.get("quality") == "ultra" else 86
    else:
        credit_cost = config["credits"]

    # ── Key availability check ────────────────────────────────────────────────
    svc_key = os.environ.get(config["key_var"], "")
    if not svc_key:
        alt = SERVICE_ALTERNATIVES.get(slug)
        alt_msg = f" Try '{alt}' for similar functionality." if alt else ""
        raise HTTPException(status_code=503, detail={
            "error": f"'{slug}' is not yet available on this server.{alt_msg}"
        })

    # ── Circuit breaker ───────────────────────────────────────────────────────
    await check_upstream_cap(slug, str(user_id), _tier)

    # ── Deduct credits (creates credit_transactions row with tx_id) ───────────
    success, balance_after, _tx_id = await check_and_deduct_credits(
        db, str(user_id), credit_cost, "/proxy",
        service_id=slug, tx_type="execution",
        agent_id=agent_id, api_key_id=_api_key_id,
        return_tx_id=True, run_prefetch=_run_prefetch,
    )
    if not success:
        raise HTTPException(status_code=402, detail={
            "error": "insufficient_credits",
            "message": (
                f"You need {credit_cost - balance_after} more credits for this call. "
                "Top up at wayforth.io/billing"
            ),
            "credits_needed": credit_cost,
            "credits_balance": balance_after,
            "top_up_url": "https://wayforth.io/billing",
        })

    # ── Primary execution ─────────────────────────────────────────────────────
    # _ex variant also yields the settlement_class (pre_send vs post_send_ambiguous)
    # the failover engine needs for the idempotency gate.
    result, error_msg, execution_ms, _primary_settlement = await _try_execute_managed_ex(
        slug, params, svc_key
    )

    # ── Failover on service-side failures (delegated to the substitution engine) ──
    # ZERO-OVERHEAD on the happy path: the engine is only ever invoked here, inside
    # the failure branch. A successful primary skips everything below.
    _proxy_fallback_from: str | None = None
    _proxy_substituted_model: tuple[str, str] | None = None
    _original_failure_code: str | None = None

    if error_msg and _classify_error(error_msg) == "service_failure":
        from main import app as _app_ref
        outcome = await run_with_failover(
            db, pool=_app_ref.state.pool,
            request_id=getattr(request.state, "request_id", ""),
            user_id=str(user_id), api_key_id=_api_key_id, agent_id=agent_id,
            primary_slug=slug, user_params=_user_params,
            primary_error=error_msg, primary_settlement=_primary_settlement,
            primary_cost=credit_cost, primary_balance_after=balance_after,
            primary_tx_id=_tx_id, primary_svc_key=svc_key,
            rail="managed",
        )
        if outcome.client_error:
            raise HTTPException(status_code=400, detail={
                "error": outcome.client_error, "refunded": True, "credits_restored": credit_cost,
            })
        if outcome.served_slug is None:
            # Group exhausted (or post-send-ambiguous and not eligible to fail over).
            asyncio.create_task(_patch_tx_signals(
                _app_ref.state.pool, _tx_id, failure_code=outcome.original_failure_code,
            ))
            raise HTTPException(status_code=502, detail={
                "error": "all_providers_failed",
                "category": outcome.category,
                "providers_tried": [
                    {"provider": s, "reason": r} for s, r in outcome.providers_tried
                ],
                "refunded": True,
                "credits_restored": credit_cost,
                "credits_remaining": outcome.balance_after,
            })
        # Adopt the provider that actually served for the rest of the handler.
        _proxy_fallback_from = outcome.fallback_from
        _proxy_substituted_model = outcome.substituted_model
        _original_failure_code = outcome.original_failure_code
        slug = outcome.served_slug
        credit_cost = outcome.cost
        balance_after = outcome.balance_after
        _tx_id = outcome.tx_id
        result = outcome.result
        execution_ms = outcome.execution_ms

    elif error_msg:
        raise HTTPException(status_code=400, detail={
            "error": error_msg, "refunded": False, "credits_restored": 0,
        })

    # ── Fire-and-forget: signal writes ────────────────────────────────────────
    from main import app
    _model_slug = params.get("model") if slug in _LLM_SLUGS else None
    asyncio.create_task(_patch_tx_signals(
        app.state.pool, _tx_id,
        failure_code=None,
        output_length_chars=len(str(result)) if result is not None else 0,
        model_routing_attempted=_json.dumps([_model_slug]) if _model_slug else None,
        model_routing_selected=_model_slug,
        substitution_from=_proxy_fallback_from,
        substitution_to=slug if _proxy_fallback_from else None,
        substitution_reason=_original_failure_code if _proxy_fallback_from else None,
    ))
    asyncio.create_task(_update_search_signal(app.state.pool, str(user_id), slug))
    asyncio.create_task(_check_spend_anomaly(app.state.pool, str(user_id)))
    asyncio.create_task(
        _maybe_dispatch_credits_low(app.state.pool, str(user_id), api_key_header, balance_after)
    )
    if _api_key_id:
        await _increment_calls(app.state.pool, str(_api_key_id), cost=credit_cost)

    # ── Build response headers ─────────────────────────────────────────────────
    wri = await _fetch_wri(db, slug)
    headers: dict[str, str] = {
        "X-Wayforth-Failover":          "true" if _proxy_fallback_from else "false",
        # Visible self-heal surface: which provider actually served, and whether
        # it came via fallback. X-Wayforth-Fallback mirrors -Failover under the
        # name the spec calls out; both kept for back-compat.
        "X-Wayforth-Served-By":         slug,
        "X-Wayforth-Fallback":          "true" if _proxy_fallback_from else "false",
        "X-Wayforth-WRI":               str(wri) if wri is not None else "unknown",
        "X-Wayforth-Cost":              str(credit_cost),
        "X-Wayforth-Rail":              "managed",
        "X-Wayforth-Credits-Remaining": str(balance_after),
    }
    if _proxy_fallback_from:
        orig_wri = await _fetch_wri(db, _proxy_fallback_from)
        headers["X-Wayforth-Original-Service"] = _proxy_fallback_from
        headers["X-Wayforth-Routed-To"]        = slug
        headers["X-Wayforth-Reason"]           = _FAILURE_REASON_LABELS.get(
            _original_failure_code, "Service unavailable"
        )
        if orig_wri is not None:
            headers["X-Wayforth-Original-WRI"] = str(orig_wri)
    # Honest model self-heal surface: the pinned model was tier-substituted on the
    # serving provider. Set only when an actual remap happened.
    if _proxy_substituted_model:
        headers["X-Wayforth-Substituted-Model"] = (
            f"{_proxy_substituted_model[0]} -> {_proxy_substituted_model[1]}"
        )

    # ── Response ──────────────────────────────────────────────────────────────
    if wayforth_wrap:
        failover_block: dict = {"triggered": bool(_proxy_fallback_from)}
        if _proxy_fallback_from:
            failover_block.update({
                "original_service": _proxy_fallback_from,
                "routed_to":        slug,
                "reason":           _FAILURE_REASON_LABELS.get(_original_failure_code, "Service unavailable"),
                "original_wri":     headers.get("X-Wayforth-Original-WRI"),
                "fallback_wri":     wri,
            })
        body = {
            "status":           "ok",
            "service":          slug,
            "served_by":        slug,
            "fallback":         bool(_proxy_fallback_from),
            "result":           result,
            "credits_deducted": credit_cost,
            "execution_ms":     execution_ms,
            "failover":         failover_block,
        }
        if _proxy_substituted_model:
            body["substituted_model"] = {
                "pinned": _proxy_substituted_model[0], "served": _proxy_substituted_model[1],
            }
        return JSONResponse(content=body, headers=headers)

    return JSONResponse(content=result, headers=headers)
