"""routers/search/search.py — /search, /search/suggestions, /search/popular, signal helpers."""

import asyncio
import hashlib
import json as json_lib
import logging
import uuid as uuid_lib

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse

from core.auth import check_auth, _ANON_DAILY_LIMIT
from core.credits import (
    PLANS, CREDIT_COSTS, CREDITS_PER_CALL, check_and_deduct_credits,
)
from core.db import get_db
from core.auth import _TIER_RPM
from core.rate_limit import limiter
from core.tier_gates import FREE_TIER_MONTHLY_SEARCH_LIMIT
from services.wayforthrank import compute_wri

logger = logging.getLogger("wayforth")

router = APIRouter()


# ── Analytics helpers (pool-parameter pattern) ────────────────────────────────

async def log_query(pool, service_id: str, query_text: str, score: int):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO service_queries (service_id, query_text, score) VALUES ($1, $2, $3)",
                service_id, query_text[:200], score,
            )
    except Exception as e:
        logger.error(f"Query log error: {e}")


async def _record_search(pool, q, results, session_id="", query_id="", user_id=None):
    try:
        async with pool.acquire() as conn:
            is_return = False
            if session_id:
                prev = await conn.fetchval("""
                    SELECT COUNT(*) FROM search_analytics
                    WHERE session_id = $1 AND created_at < NOW() - INTERVAL '1 hour'
                """, session_id)
                is_return = prev > 0

            q = q.strip().lower()
            top_slug = None
            top_wri = None
            if results:
                ep = results[0].get("endpoint_url", "")
                top_slug = "0x" + hashlib.sha256(ep.encode()).hexdigest() if ep else None
                top_wri = int(compute_wri(results[0], results[0].get("score", 0)))

            await conn.execute("""
                INSERT INTO search_analytics
                (id, query, results, top_result_id, result_count,
                 top_result_slug, top_result_wri, results_count,
                 rank_scores, session_id, user_id, created_at)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::uuid, NOW())
            """,
                query_id or str(uuid_lib.uuid4()),
                q,
                json_lib.dumps([{"id": str(r.get("service_id", "")), "score": r.get("score", 0)} for r in results[:10]]),
                str(results[0].get("id", "")) if results else None,
                len(results),
                top_slug,
                top_wri,
                len(results),
                json_lib.dumps({str(r.get("service_id", "")): r.get("score", 0) for r in results[:10]}),
                session_id or None,
                user_id or None,
            )

            if is_return:
                logger.info(f"Return session: {session_id[:8]}")
    except Exception as e:
        logger.warning(f"search analytics write failed: {e}")


async def _update_identity_search(pool, agent_id: str):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO agent_identities (agent_id, total_searches, last_active_at)
                VALUES ($1, 1, NOW())
                ON CONFLICT (agent_id) DO UPDATE
                SET total_searches = agent_identities.total_searches + 1,
                    last_active_at = NOW()
            """, agent_id)
    except Exception as e:
        logger.warning(f"Identity update failed: {e}")


# ── /search ───────────────────────────────────────────────────────────────────

@router.get(
    "/search",
    summary="Semantic service search",
    description=(
        "Rank Wayforth services by relevance to a natural language query using Claude Haiku. "
        "Falls back to keyword scoring when ANTHROPIC_API_KEY is not set."
    ),
)
@limiter.limit("15/minute")
async def search_services(
    request: Request,
    q: str = Query(max_length=500, description="Natural language query, e.g. 'fast cheap inference for coding'"),
    category: str | None = Query(default=None, description="Filter by category: inference, data, translation, …"),
    tier: int | None = Query(default=None, description="Filter by exact coverage tier (0=free, 1=basic, 2=standard, 3=premium)"),
    limit: int = Query(default=5, ge=1, le=20, description="Number of results to return (1–20)"),
    session_id: str = Query(default="", description="Optional agent session ID for return-visit tracking"),
    agent_id: str = Query(default="", description="Optional agent identity ID for reputation tracking"),
    db=Depends(get_db),
    auth: dict = Depends(check_auth),
):
    from main import app
    from ranker_client import rank_services
    import html as _html

    q = _html.escape(q.strip().lower())
    if auth.get("authenticated") and auth.get("user_id"):
        if auth.get("tier") == "free" and auth.get("user_id"):
            from datetime import datetime, timezone
            _month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            _searches_this_month = await db.fetchval(
                "SELECT COUNT(*) FROM credit_transactions "
                "WHERE user_id = $1::uuid AND api_endpoint = '/search' AND created_at >= $2",
                auth["user_id"], _month_start,
            ) or 0
            if _searches_this_month >= FREE_TIER_MONTHLY_SEARCH_LIMIT:
                raise HTTPException(status_code=429, detail={
                    "error": "monthly_search_limit",
                    "limit": FREE_TIER_MONTHLY_SEARCH_LIMIT,
                    "searches_used": int(_searches_this_month),
                    "message": f"Free tier: {FREE_TIER_MONTHLY_SEARCH_LIMIT} searches/month reached. Upgrade to continue.",
                    "upgrade_url": "https://wayforth.io/pricing",
                })
        success, balance = await check_and_deduct_credits(
            db, auth["user_id"], CREDIT_COSTS["search"], "/search"
        )
        if not success:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "insufficient_credits",
                    "message": "You've run out of credits. Top up to continue.",
                    "balance": balance,
                    "required": CREDIT_COSTS["search"],
                    "top_up_url": "https://wayforth.io/dashboard/billing",
                    "packages_url": "https://wayforth.io/pricing",
                }
            )

    try:
        rows = await db.fetch(
            """
            SELECT id, name, slug, description, endpoint_url, category,
                   coverage_tier, pricing_usdc, source, payment_protocol, created_at,
                   last_tested_at, consecutive_failures, x402_supported,
                   wri_score, wri_version
            FROM services
            WHERE ($1::text IS NULL OR category = $1)
              AND ($2::int IS NULL OR coverage_tier = $2)
            ORDER BY created_at DESC
            """,
            category,
            tier,
        )
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    services = [dict(r) for r in rows]
    try:
        ranked = await rank_services(q, services, db=db)
    except Exception as _re:
        logger.error("search ranker error: %s", _re)
        raise HTTPException(status_code=503, detail={"error": "ranker_unavailable"})
    top = ranked[:limit]

    fallback_used = False
    fallback_reason = None
    if not top:
        try:
            fb_rows = await db.fetch(
                """
                SELECT id, name, slug, description, endpoint_url, category,
                       coverage_tier, pricing_usdc, source, payment_protocol,
                       last_tested_at, consecutive_failures, x402_supported,
                       wri_score, wri_version
                FROM services
                WHERE coverage_tier >= 0
                  AND (name ILIKE $1 OR description ILIKE $1 OR category ILIKE $1)
                ORDER BY coverage_tier DESC LIMIT 50
                """,
                f"%{q}%",
            )
            if fb_rows:
                fb_ranked = await rank_services(q, [dict(r) for r in fb_rows], db=db)
                top = fb_ranked[:limit]
                fallback_used = True
                fallback_reason = "No Tier 2 results — showing all tiers"
        except Exception:
            pass

    query_id = str(uuid_lib.uuid4())
    pool = app.state.pool
    if ranked and pool:
        asyncio.create_task(log_query(pool, str(ranked[0]["id"]), q, ranked[0].get("score", 0)))
    if pool:
        asyncio.create_task(_record_search(pool, q, ranked, session_id, query_id, auth.get("user_id")))
    if pool and agent_id:
        asyncio.create_task(_update_identity_search(pool, agent_id))
    popular_ids: dict = {}
    payment_ids: dict = {}
    try:
        pop_rows = await db.fetch("""
            SELECT top_result_id, COUNT(*) as c
            FROM search_analytics
            WHERE created_at > NOW() - INTERVAL '7 days'
              AND top_result_id IS NOT NULL
            GROUP BY top_result_id
            ORDER BY c DESC LIMIT 50
        """)
        max_count = max((r["c"] for r in pop_rows), default=1)
        popular_ids = {str(r["top_result_id"]): (r["c"] / max_count) * 5 for r in pop_rows}

        pay_rows = await db.fetch("""
            SELECT service_id, COUNT(*) as c
            FROM search_outcomes
            WHERE outcome_type = 'payment_initiated'
              AND created_at > NOW() - INTERVAL '7 days'
              AND service_id IS NOT NULL
            GROUP BY service_id ORDER BY c DESC LIMIT 50
        """)
        max_pay = max((r["c"] for r in pay_rows), default=1)
        payment_ids = {str(r["service_id"]): (r["c"] / max_pay) * 8 for r in pay_rows}
    except Exception:
        pass

    logger.info(f"search q={q!r} results={len(top)} fallback={fallback_used}")
    results = [
        {
            "name": s.get("name"),
            "slug": s.get("slug"),
            "description": s.get("description"),
            "score": s.get("score", 0),
            "wri": s["wri_score"] if (s.get("wri_score") is not None and s.get("wri_version") == "v2") else compute_wri(s, s.get("score", 0), popularity_boost=popular_ids.get(str(s.get("id")), 0.0), payment_boost=payment_ids.get(str(s.get("id")), 0.0)),
            "ranking_version": "v2" if (s.get("wri_score") is not None and s.get("wri_version") == "v2") else "v1",
            "reason": s.get("reason", ""),
            "coverage_tier": s.get("coverage_tier"),
            "category": s.get("category"),
            "endpoint_url": s.get("endpoint_url"),
            "pricing": {
                "per_call_usd": s.get("pricing_usdc"),
            },
            "service_id": "0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest(),
            "wayforth_id": f"wayforth://{s.get('slug') or s.get('name','').lower().replace(' ','_').replace('/','_')[:30]}/{hashlib.sha256(s.get('endpoint_url','').encode()).hexdigest()[:8]}",
            "payment_options": {
                "track_a": {
                    "method": "card",
                    "processor": "Stripe Treasury",
                    "credits_needed": max(1, round((s.get("pricing_usdc") or 0.001) * 1000)),
                },
                "track_b": {
                    "method": "crypto",
                    "network": "base-sepolia",
                    "amount_usdc": s.get("pricing_usdc") or 0.001,
                    "calldata_via": "wayforth_pay(service_id, amount_usd, track='crypto')",
                },
                "x402_supported": bool(s.get("x402_supported", False)),
            },
        }
        for s in top
    ]
    response: dict = {
        "query_id": query_id,
        "query": q,
        "total_results": len(top),
        "total_matches": len(ranked),
        "results": results,
        "fallback": fallback_used,
        "fallback_reason": fallback_reason,
    }
    if auth["authenticated"]:
        response["tier"] = auth["tier"]
        response["usage_this_month"] = auth["usage_this_month"]
        response["monthly_quota"] = auth["monthly_quota"]
    else:
        remaining = _ANON_DAILY_LIMIT - auth["anonymous_count"]
        response["anonymous_searches_remaining"] = remaining
        if remaining > 0:
            response["signup_url"] = "https://wayforth.io/signup"
            response["message"] = f"{remaining} free {'search' if remaining == 1 else 'searches'} remaining. Sign up free for 100/month."
    return response


@router.get("/search/suggestions")
@limiter.limit("30/minute")
async def search_suggestions(request: Request, db=Depends(get_db)):
    """Top queries from real agent usage. Falls back to curated list."""
    rows = await db.fetch("""
        SELECT query, COUNT(*) as count
        FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '7 days'
        AND query IS NOT NULL
        AND LENGTH(query) > 3
        GROUP BY query
        ORDER BY count DESC
        LIMIT 8
    """)
    curated = [
        "fast inference for coding",
        "translate text to Spanish",
        "real-time stock data",
        "web search for agents",
        "generate images from text",
        "speech to text API",
        "embed documents for RAG",
        "crypto market prices",
    ]
    if rows and len(rows) >= 4:
        return {"suggestions": [r['query'] for r in rows], "source": "live"}
    return {"suggestions": curated, "source": "curated"}


@router.get("/search/popular")
@limiter.limit("30/minute")
async def popular_searches(request: Request, limit: int = 8, db=Depends(get_db)):
    """Real queries from the last 7 days. Powers homepage suggestion chips."""
    rows = await db.fetch("""
        SELECT query, COUNT(*) as count
        FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '7 days'
        AND query IS NOT NULL
        AND LENGTH(query) > 3
        GROUP BY query
        ORDER BY count DESC
        LIMIT $1
    """, limit)
    if not rows or len(rows) < 4:
        return {
            "queries": [
                "fast inference for coding",
                "translate text to Spanish",
                "real-time stock data",
                "web search for agents",
                "generate images from text",
                "speech to text transcription",
                "embed documents for RAG",
                "crypto market prices",
            ],
            "source": "curated",
        }
    return {"queries": [r['query'] for r in rows], "source": "live", "period": "7d"}


@router.get("/pricing/json")
@limiter.limit("30/minute")
async def pricing_json(request: Request):
    """Machine-readable pricing data."""
    tiers = []
    for name, p in PLANS.items():
        rpm = _TIER_RPM.get(name, 10)
        bonus_calls = p["usdc_bonus_credits"] // CREDITS_PER_CALL if p["usdc_bonus_credits"] else 0
        tiers.append({
            "name": name.capitalize(),
            "price_monthly_usd": p["price_usd"],
            "calls_included": p["calls_included"],
            "usdc_bonus_calls": bonus_calls,
            "rate_limit_per_minute": rpm,
            "features": p["features"],
        })
    return {"tiers": tiers}
