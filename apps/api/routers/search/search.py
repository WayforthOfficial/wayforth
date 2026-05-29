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
from core.tier_gates import FREE_TIER_MONTHLY_SEARCH_LIMIT, check_rate_limit, check_anon_rate_limit
from services.wayforthrank import compute_wri

logger = logging.getLogger("wayforth")

router = APIRouter()


def _apply_health_wri(wri, health_row) -> float:
    """Reduce WRI score based on live service health data."""
    if wri is None or not health_row:
        return wri
    adj = 0
    if (health_row.get("error_rate") or 0) > 0.3:
        adj -= 10
    if (health_row.get("avg_response_ms") or 0) > 5000:
        adj -= 5
    return max(0.0, float(wri) + adj) if adj else wri


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
async def search_services(
    request: Request,
    q: str = Query(min_length=1, max_length=500, description="Natural language query, e.g. 'fast cheap inference for coding'"),
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
    if not q:
        raise HTTPException(status_code=400, detail={"error": "query_required"})
    if auth.get("authenticated"):
        await check_rate_limit(auth["key_id"], auth["tier"])
    else:
        await check_anon_rate_limit(auth["ip"])
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
              AND source != 'demo'
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

    # Category relevance adjustment — only when caller did not supply an explicit
    # category filter (they've already narrowed the result set in that case).
    if not category and ranked:
        from services.param_mapper import detect_category_hint, INTENT_CATEGORY_MAP
        _detected = detect_category_hint(q)
        if _detected:
            _compat = set(INTENT_CATEGORY_MAP.get(_detected, [_detected]))
            for _s in ranked:
                _svc_cat = _s.get("category") or ""
                if _svc_cat in _compat:
                    _s["score"] = (_s.get("score") or 0) + 15
                else:
                    _s["score"] = (_s.get("score") or 0) - 20
            ranked.sort(key=lambda _s: (_s.get("score") or 0), reverse=True)

    # ── Pioneer routing (60/40 split) ────────────────────────────────────────
    # For users with pioneer_opt_in=TRUE: if boosted providers exist in the
    # result category, promote them to the front on 60% of calls.
    # Deterministic per-search via MD5 of query_id — not purely random,
    # consistent within a request.
    _pioneer_routing = False
    _pioneer_routed_to_boosted = False
    _pioneer_signal_weight = 1.0
    _pioneer_boosted_slugs: set = set()

    if auth.get("authenticated") and auth.get("user_id"):
        try:
            _pioneer_row = await db.fetchrow(
                "SELECT pioneer_opt_in FROM users WHERE id = $1::uuid",
                auth["user_id"],
            )
            if _pioneer_row and _pioneer_row["pioneer_opt_in"]:
                _pioneer_routing = True
                # Use explicit category or fall back to no filter
                _cat_param = category  # None means any category
                _proto_query_id = str(uuid_lib.uuid4())  # temporary — final set below
                _boosted = await db.fetch("""
                    SELECT ps.service_slug
                      FROM provider_services ps
                      JOIN providers p ON p.id = ps.provider_id
                      JOIN services s ON s.slug = ps.service_slug
                     WHERE p.boost_used    = TRUE
                       AND p.boost_paused  = FALSE
                       AND p.boost_expires_at > NOW()
                       AND ($1::text IS NULL OR s.category = $1)
                """, _cat_param)
                _pioneer_boosted_slugs = {r["service_slug"] for r in _boosted}

                if _pioneer_boosted_slugs:
                    # Seed the 60/40 split from the SERVER-generated query id only.
                    # The previous seed mixed in the client-controlled query text
                    # (`q`), letting a caller enumerate phrasings offline (MD5 is
                    # local) to deterministically land in (or avoid) the boosted
                    # bucket. A server-generated UUID is uniform and not
                    # client-influenceable.
                    import hashlib as _hl
                    _seed = int(_hl.md5(_proto_query_id.encode()).hexdigest()[:8], 16)
                    if _seed % 10 < 6:  # 60% path — route to boosted first
                        _pioneer_routed_to_boosted = True
                        _pioneer_signal_weight = 0.75
                        _boosted_first = [s for s in ranked if s.get("slug") in _pioneer_boosted_slugs]
                        _others = [s for s in ranked if s.get("slug") not in _pioneer_boosted_slugs]
                        ranked = _boosted_first + _others
        except Exception:
            pass
    # ── end pioneer routing ───────────────────────────────────────────────────

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
                  AND source != 'demo'
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

    # Load service health for WRI adjustment
    _health_map: dict = {}
    try:
        _top_slugs = [s.get("slug") for s in top if s.get("slug")]
        if _top_slugs:
            _health_rows = await db.fetch(
                "SELECT slug, avg_response_ms, error_rate FROM service_health WHERE slug = ANY($1::text[])",
                _top_slugs,
            )
            _health_map = {r["slug"]: r for r in _health_rows}
    except Exception:
        pass

    # Load Pioneer Boost metadata for each result slug.
    # Joins provider_services → providers to find active (non-paused, non-expired) boosts.
    _boost_map: dict = {}  # slug → {wri_bonus, expires_at, new_provider}
    try:
        _top_slugs = [s.get("slug") for s in top if s.get("slug")]
        if _top_slugs:
            from datetime import datetime, timezone as _tz
            _boost_rows = await db.fetch("""
                SELECT ps.service_slug,
                       p.boost_wri_bonus,
                       p.boost_expires_at,
                       p.created_at AS provider_created_at
                  FROM provider_services ps
                  JOIN providers p ON p.id = ps.provider_id
                 WHERE ps.service_slug = ANY($1::text[])
                   AND p.boost_used    = TRUE
                   AND p.boost_paused  = FALSE
                   AND p.boost_expires_at > NOW()
            """, _top_slugs)
            _now = datetime.now(_tz.utc)
            for br in _boost_rows:
                expires = br["boost_expires_at"]
                days_left = max(0, (expires - _now).days) if expires else 0
                _provider_created = br["provider_created_at"]
                is_new = (
                    _provider_created is not None
                    and (_now - (_provider_created if _provider_created.tzinfo else _provider_created.replace(tzinfo=_tz.utc))).days <= 30
                )
                _boost_map[br["service_slug"]] = {
                    "wri_bonus": int(br["boost_wri_bonus"] or 0),
                    "expires_in_days": days_left,
                    "new_provider": is_new,
                }
    except Exception:
        pass

    logger.info(f"search q={q!r} results={len(top)} fallback={fallback_used}")
    results = []
    for s in top:
        slug = s.get("slug")
        _boost = _boost_map.get(slug, {})
        _boost_bonus = _boost.get("wri_bonus", 0)
        _base_wri = _apply_health_wri(
            s["wri_score"] if (s.get("wri_score") is not None and s.get("wri_version") == "v2") else compute_wri(s, s.get("score", 0), popularity_boost=popular_ids.get(str(s.get("id")), 0.0), payment_boost=payment_ids.get(str(s.get("id")), 0.0)),
            _health_map.get(slug),
        )
        _boosted_wri = min(round((_base_wri or 0) + _boost_bonus, 1), 100.0)
        result = {
            "name": s.get("name"),
            "slug": slug,
            "description": s.get("description"),
            "score": s.get("score", 0),
            "wri": _boosted_wri,
            "ranking_version": "v2" if (s.get("wri_score") is not None and s.get("wri_version") == "v2") else "v1",
            "reason": s.get("reason", ""),
            "coverage_tier": s.get("coverage_tier"),
            "category": s.get("category"),
            "endpoint_url": s.get("endpoint_url"),
            "pricing": {
                "per_call_usd": s.get("pricing_usdc"),
            },
            "service_id": "0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest(),
            "wayforth_id": f"wayforth://{slug or s.get('name','').lower().replace(' ','_').replace('/','_')[:30]}/{hashlib.sha256(s.get('endpoint_url','').encode()).hexdigest()[:8]}",
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
        if _boost:
            result["boosted"] = True
            result["boost_expires_in_days"] = _boost["expires_in_days"]
            result["new_provider"] = _boost["new_provider"]
        else:
            result["boosted"] = False
        results.append(result)
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
        # Strip endpoint_url from unauthenticated responses — service_id (hashed) is sufficient
        for result in response.get("results", []):
            result.pop("endpoint_url", None)
        remaining = _ANON_DAILY_LIMIT - auth["anonymous_count"]
        response["anonymous_searches_remaining"] = remaining
        if remaining > 0:
            response["signup_url"] = "https://wayforth.io/signup"
            response["message"] = f"{remaining} free {'search' if remaining == 1 else 'searches'} remaining. Sign up free for 100/month."

    # Pioneer routing metadata — always present for pioneer users
    if _pioneer_routing:
        response["pioneer_routing"] = True
        response["pioneer_routed_to_boosted"] = _pioneer_routed_to_boosted
        response["signal_weight"] = _pioneer_signal_weight
        response["boost_active"] = len(_pioneer_boosted_slugs) > 0

        # Record pioneer routing outcome in search_outcomes (background)
        if pool:
            async def _record_pioneer_outcome(
                _qid=query_id, _q=q, _top=top, _sid=session_id,
                _routed=_pioneer_routed_to_boosted, _sw=_pioneer_signal_weight,
            ):
                try:
                    async with pool.acquire() as conn:
                        _service_id = None
                        if _top:
                            _slug = _top[0].get("slug")
                            if _slug:
                                _srow = await conn.fetchrow(
                                    "SELECT id FROM services WHERE slug = $1", _slug
                                )
                                if _srow:
                                    _service_id = str(_srow["id"])
                        await conn.execute("""
                            INSERT INTO search_outcomes
                              (query_id, query_text, service_id, outcome_type,
                               session_id, signal_weight, pioneer_routed)
                            VALUES ($1::uuid, $2, $3::uuid, 'result_viewed',
                                    $4, $5, $6)
                        """, _qid, _q, _service_id, _sid or None, _sw, _routed)
                except Exception:
                    pass
            asyncio.create_task(_record_pioneer_outcome())

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


_POPULAR_BLOCKLIST = (
    "or 1=1", "1=1", "<script", "alert(", "javascript:",
    "rate limit", " test", "admin", "' or ", "\" or ",
    "drop table", "select *", "union select", "xss",
    "inject", "curl ", "wget ", "eval(",
)

_POPULAR_CURATED = [
    "fast inference for coding",
    "translate text to Spanish",
    "real-time stock data",
    "web search for agents",
    "generate images from text",
    "speech to text transcription",
    "embed documents for RAG",
    "crypto market prices",
]


def _is_clean_query(q: str) -> bool:
    """Return False if the query looks like a test string or injection attempt."""
    lower = q.strip().lower()
    if not lower or len(lower) < 4:
        return False
    return not any(blocked in lower for blocked in _POPULAR_BLOCKLIST)


@router.get("/search/popular")
@limiter.limit("30/minute")
async def popular_searches(request: Request, limit: int = 8, db=Depends(get_db)):
    """Aggregated category counts from the last 7 days. Never exposes raw query strings."""
    rows = await db.fetch("""
        SELECT s.category, COUNT(*) AS count
        FROM search_analytics sa
        JOIN services s ON s.id = sa.top_result_id
        WHERE sa.created_at > NOW() - INTERVAL '7 days'
          AND sa.top_result_id IS NOT NULL
          AND s.category IS NOT NULL
        GROUP BY s.category
        ORDER BY count DESC
        LIMIT $1
    """, limit)
    if not rows:
        return {"categories": [], "source": "live", "period": "7d"}
    return {
        "categories": [{"category": r["category"], "count": int(r["count"])} for r in rows],
        "source": "live",
        "period": "7d",
    }


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
            "rate_limit_per_minute": "unlimited" if rpm == 0 else rpm,
            "features": p["features"],
        })
    return {"tiers": tiers}
