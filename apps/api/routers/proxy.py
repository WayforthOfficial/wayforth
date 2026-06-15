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
    _try_execute_managed,
    _update_search_signal,
)

logger = logging.getLogger("wayforth")
router = APIRouter()

_LLM_SLUGS = frozenset({"groq", "together", "mistral", "gemini"})


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

    user_id, _api_key_id, _tier = await _resolve_user(db, api_key_header)
    await check_rate_limit(str(_api_key_id), _tier)

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

    # ── Param validation ──────────────────────────────────────────────────────
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
        agent_id=agent_id, api_key_id=str(_api_key_id),
        return_tx_id=True,
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
    result, error_msg, execution_ms = await _try_execute_managed(slug, params, svc_key)

    # ── Failover on service-side failures ────────────────────────────────────
    _proxy_fallback_from: str | None = None
    _original_failure_code: str | None = None

    if error_msg and _classify_error(error_msg) == "service_failure":
        _original_failure_code = _classify_failure(None, error_msg)
        new_bal = await _do_refund(
            db, user_id, credit_cost, slug, error_msg, "/proxy", balance_after,
            _mk_refund_key(getattr(request.state, "request_id", ""), slug, "proxy_managed"),
        )

        _fb_slug = SERVICE_ALTERNATIVES.get(slug)
        if _fb_slug and _fb_slug in SERVICE_CONFIGS:
            _fb_cfg = SERVICE_CONFIGS[_fb_slug]
            _fb_key = os.environ.get(_fb_cfg["key_var"], "")
            if _fb_key:
                _fb_mapped, _fb_miss = map_params(_fb_slug, params)
                if not _fb_miss:
                    _fb_cost = _fb_cfg["credits"]
                    _fb_ok, _fb_bal, _fb_tx_id = await check_and_deduct_credits(
                        db, str(user_id), _fb_cost, "/proxy",
                        service_id=_fb_slug, tx_type="execution",
                        agent_id=agent_id, api_key_id=str(_api_key_id),
                        return_tx_id=True,
                    )
                    if _fb_ok:
                        result, _fb_err, execution_ms = await _try_execute_managed(
                            _fb_slug, _fb_mapped, _fb_key,
                        )
                        if _fb_err and _classify_error(_fb_err) == "service_failure":
                            await _do_refund(
                                db, user_id, _fb_cost, _fb_slug, _fb_err, "/proxy", _fb_bal,
                                _mk_refund_key(
                                    getattr(request.state, "request_id", ""),
                                    _fb_slug, "proxy_managed_fb",
                                ),
                            )
                            result = None
                        elif _fb_err:
                            raise HTTPException(status_code=400, detail={
                                "error": _fb_err, "refunded": False, "credits_restored": 0,
                            })
                        else:
                            _proxy_fallback_from = slug
                            slug = _fb_slug
                            credit_cost = _fb_cost
                            balance_after = _fb_bal
                            _tx_id = _fb_tx_id

        if result is None:
            from main import app as _app_ref
            asyncio.create_task(_patch_tx_signals(
                _app_ref.state.pool, _tx_id,
                failure_code=_original_failure_code,
            ))
            raise HTTPException(status_code=503, detail={
                "error": "Service unavailable",
                "refunded": True,
                "credits_restored": credit_cost,
                "credits_remaining": new_bal,
            })

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
            "result":           result,
            "credits_deducted": credit_cost,
            "execution_ms":     execution_ms,
            "failover":         failover_block,
        }
        return JSONResponse(content=body, headers=headers)

    return JSONResponse(content=result, headers=headers)
