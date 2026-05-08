"""routers/search.py — Search, discovery, memory, tier3, graph, intelligence, pricing, MCP routes."""

import asyncio
import hashlib
import json as json_lib
import logging
import os
import secrets
import uuid as uuid_lib

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.auth import _resolve_user, check_auth, _ANON_DAILY_LIMIT
from core.credits import (
    PLANS, CREDIT_COSTS, CREDITS_PER_CALL, check_and_deduct_credits,
    _dispatch_webhooks,
)
from core.db import get_db
from core.auth import _TIER_RPM
from core.rate_limit import limiter
from core.tier_gates import require_tier, FREE_TIER_MONTHLY_SEARCH_LIMIT
from services.managed import SERVICE_CONFIGS, SERVICE_DISPLAY_NAMES
from services.param_mapper import MANAGED_TO_CATALOG
from services.wayforthrank import compute_wri

logger = logging.getLogger("wayforth")

router = APIRouter()

# ── Catalog slug mappings ─────────────────────────────────────────────────────

_CATALOG_SLUGS = list(MANAGED_TO_CATALOG.values())
_CATALOG_SLUG_TO_MANAGED = {v: k for k, v in MANAGED_TO_CATALOG.items()}


def _service_status(consecutive_failures: int) -> tuple[str, str]:
    if consecutive_failures == 0:
        return "operational", "Operational"
    if consecutive_failures <= 2:
        return "degraded", "Degraded"
    return "outage", "Outage"


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


async def _get_similar_services(db, service_id: str, limit: int) -> dict:
    """Shared helper: resolve service_id and return co-usage graph neighbours."""
    internal_id = service_id
    if service_id.startswith("0x"):
        sha = service_id[2:]
        row = await db.fetchrow(
            "SELECT id FROM services WHERE encode(sha256(endpoint_url::bytea), 'hex') = $1", sha
        )
        if row:
            internal_id = str(row["id"])

    rows = await db.fetch(
        """
        SELECT
            CASE WHEN service_a_id = $1 THEN service_b_id ELSE service_a_id END AS related_id,
            co_search_count, co_payment_count
        FROM service_graph
        WHERE service_a_id = $1 OR service_b_id = $1
        ORDER BY co_search_count DESC
        LIMIT $2
        """,
        internal_id, limit,
    )

    related = []
    for row in rows:
        svc = await db.fetchrow(
            "SELECT name, category, coverage_tier FROM services WHERE id::text = $1",
            row["related_id"],
        )
        related.append({
            "service_id": row["related_id"],
            "name": svc["name"] if svc else "Unknown",
            "category": svc["category"] if svc else None,
            "tier": svc["coverage_tier"] if svc else None,
            "co_search_count": row["co_search_count"],
            "co_payment_count": row["co_payment_count"],
        })

    return {
        "service_id": service_id,
        "related_services": related,
        "total": len(related),
        "note": "Co-usage patterns from real agent search sessions",
    }


# ── Models ────────────────────────────────────────────────────────────────────

class WayforthQLQuery(BaseModel):
    query: str
    tier_min: int | None = 2
    price_max: float | None = None
    uptime_min: float | None = None  # reserved — no column yet
    category: str | None = None
    protocol: str | None = None       # 'wayforth' | 'any'
    exclude_ids: list[str] | None = []  # service_id SHA256 hashes to exclude
    sort_by: str | None = "wri"       # 'wri' | 'score' | 'price' | 'tier'
    limit: int | None = 5
    with_similar: bool | None = False  # include similar services for top result
    x402_only: bool = False            # only x402-native services
    provider: str | None = None        # filter by provider name substring
    verified_only: bool = False        # only tier-2+ verified services
    offset: int = 0                    # pagination offset


class MemoryItem(BaseModel):
    service_id: str
    service_name: str
    note: str = ""
    agent_id: str = ""


class Tier3Application(BaseModel):
    service_name: str
    company_name: str
    contact_email: str
    website: str = ""
    endpoint_url: str
    monthly_volume_usd: float = 0.0
    sla_uptime_target: float = 99.9


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
    q: str = Query(description="Natural language query, e.g. 'fast cheap inference for coding'"),
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

    q = q.strip().lower()
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
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
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
    ranked = await rank_services(q, services, db=db)
    top = ranked[:limit]

    fallback_used = False
    fallback_reason = None
    if not top:
        try:
            async with app.state.pool.acquire() as conn:
                fb_rows = await conn.fetch(
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
        async with app.state.pool.acquire() as conn:
            pop_rows = await conn.fetch("""
                SELECT top_result_id, COUNT(*) as c
                FROM search_analytics
                WHERE created_at > NOW() - INTERVAL '7 days'
                  AND top_result_id IS NOT NULL
                GROUP BY top_result_id
                ORDER BY c DESC LIMIT 50
            """)
            max_count = max((r["c"] for r in pop_rows), default=1)
            popular_ids = {str(r["top_result_id"]): (r["c"] / max_count) * 5 for r in pop_rows}

            pay_rows = await conn.fetch("""
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
                "credits_per_call": max(1, round((s.get("pricing_usdc") or 0.001) * 1000)),
            },
            "service_id": "0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest(),
            "wayforth_id": f"wayforth://{s.get('slug') or s.get('name','').lower().replace(' ','_').replace('/','_')[:30]}/{hashlib.sha256(s.get('endpoint_url','').encode()).hexdigest()[:8]}",
            "payment_options": {
                "track_a": {
                    "method": "card",
                    "processor": "Stripe Treasury",
                    "credits_needed": max(1, round((s.get("pricing_usdc") or 0.001) * 1000)),
                    "fee_pct": 1.5,
                },
                "track_b": {
                    "method": "crypto",
                    "network": "base-sepolia",
                    "amount_usdc": s.get("pricing_usdc") or 0.001,
                    "fee_pct": 1.5,
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


@router.get("/quickstart", include_in_schema=False)
async def quickstart():
    html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Wayforth — Developer Quickstart</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0F172A; color: #E2E8F0; padding: 40px 20px; line-height: 1.6; }
  .container { max-width: 800px; margin: 0 auto; }
  h1 { color: #4F46E5; font-size: 2rem; margin-bottom: 8px; }
  .subtitle { color: #64748B; margin-bottom: 48px; }
  .step { margin-bottom: 40px; }
  .step-num { color: #4F46E5; font-weight: bold; font-size: 0.85rem;
              text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
  h2 { color: #E2E8F0; font-size: 1.25rem; margin-bottom: 12px; }
  pre { background: #1E293B; border: 1px solid #334155; border-left: 3px solid #4F46E5;
        padding: 16px 20px; border-radius: 6px; overflow-x: auto;
        font-family: 'Courier New', monospace; font-size: 13px;
        color: #94A3B8; margin-bottom: 12px; }
  .comment { color: #475569; }
  .keyword { color: #4F46E5; }
  .string { color: #10B981; }
  .note { background: #1E293B; border: 1px solid #334155; border-radius: 6px;
          padding: 12px 16px; color: #64748B; font-size: 0.875rem; }
  .note a { color: #4F46E5; }
  .divider { border: none; border-top: 1px solid #1E293B; margin: 40px 0; }
  .links { display: flex; gap: 16px; flex-wrap: wrap; margin-top: 40px; }
  .link { color: #4F46E5; text-decoration: none; font-size: 0.9rem; }
  .link:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="container">
  <h1>Wayforth Quickstart</h1>
  <p class="subtitle">From zero to searching 200+ verified APIs in 60 seconds.</p>

  <div class="step">
    <div class="step-num">Step 1 of 3</div>
    <h2>Install the MCP server</h2>
    <pre>uvx wayforth-mcp</pre>
    <p class="note">Works with Claude Code, Cursor, Windsurf, and any MCP-compatible runtime.
    Or add explicitly: <code>claude mcp add wayforth -- uvx wayforth-mcp</code></p>
  </div>

  <div class="step">
    <div class="step-num">Step 2 of 3</div>
    <h2>Search the catalog</h2>
    <pre><span class="comment"># In your agent — natural language, no API keys needed</span>
wayforth_search(<span class="string">"translate text to Spanish"</span>)

<span class="comment"># Returns ranked results with WRI scores</span>
<span class="comment"># → DeepL API      WRI: 82  Tier 2 Verified  $0.0000025/req</span>
<span class="comment"># → LibreTranslate  WRI: 71  Tier 2 Verified  Free</span>
<span class="comment"># → ModernMT        WRI: 68  Tier 2 Verified  $0.000003/req</span></pre>
    <p class="note">WRI (Wayforth Reliability Index) is a 0–100 score based on uptime history,
    probe frequency, and real agent usage. Higher = more trustworthy.</p>
  </div>

  <div class="step">
    <div class="step-num">Step 3 of 3</div>
    <h2>Pay with credits</h2>
    <pre><span class="comment"># Deduct credits for a service call</span>
wayforth_pay(
  service_id=<span class="string">"service_id_from_search"</span>,
  amount_usd=<span class="string">0.001</span>
)

<span class="comment"># 1 credit = $0.001</span>
<span class="comment"># Credits deducted instantly from your balance</span>
<span class="comment"># Buy credits at wayforth.io/dashboard</span></pre>
  </div>

  <hr class="divider">

  <div class="step">
    <div class="step-num">WayforthQL — structured queries</div>
    <h2>For more control, use WayforthQL</h2>
    <pre>POST /query
{
  <span class="string">"query"</span>: <span class="string">"fast inference for coding agents"</span>,
  <span class="string">"tier_min"</span>: 2,
  <span class="string">"sort_by"</span>: <span class="string">"wri"</span>,
  <span class="string">"price_max"</span>: 0.001,
  <span class="string">"limit"</span>: 5
}</pre>
  </div>

  <div class="step">
    <div class="step-num">Python SDK</div>
    <h2>Or use the Python SDK directly</h2>
    <pre>pip install wayforth-sdk

<span class="keyword">from</span> wayforth.client <span class="keyword">import</span> WayforthClient

client = WayforthClient()
results = client.query(
    query=<span class="string">"real-time stock data"</span>,
    tier_min=2,
    sort_by=<span class="string">"wri"</span>
)
<span class="keyword">for</span> r <span class="keyword">in</span> results[<span class="string">"results"</span>]:
    print(r[<span class="string">"name"</span>], <span class="string">"WRI:"</span>, r[<span class="string">"wri"</span>])</pre>
  </div>

  <hr class="divider">

  <div class="links">
    <a class="link" href="/docs">API Reference →</a>
    <a class="link" href="https://wayforth.io/demo">Live Demo →</a>
    <a class="link" href="https://wayforth.io/leaderboard">Leaderboard →</a>
    <a class="link" href="/wayforthql-spec">WayforthQL Spec →</a>
    <a class="link" href="https://github.com/WayforthOfficial/wayforth">GitHub →</a>
    <a class="link" href="https://wayforth.io/contact">Contact Us</a>
  </div>
</div>
</body>
</html>"""
    return HTMLResponse(content=html)


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


@router.post("/query")
async def wayforthql(request: Request, body: WayforthQLQuery, auth: dict = Depends(check_auth), db=Depends(get_db)):
    """WayforthQL — declarative query language for agent service discovery."""
    from ranker_client import rank_services

    require_tier(auth.get("tier") or "free", "wayforthql")
    if auth.get("authenticated") and auth.get("user_id"):
        success, balance = await check_and_deduct_credits(
            db, auth["user_id"], CREDIT_COSTS["query"], "/query"
        )
        if not success:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "insufficient_credits",
                    "message": "You've run out of credits. Top up to continue.",
                    "balance": balance,
                    "required": CREDIT_COSTS["query"],
                    "top_up_url": "https://wayforth.io/dashboard/billing",
                    "packages_url": "https://wayforth.io/pricing",
                }
            )

    conditions = ["coverage_tier >= $1"]
    params: list = [body.tier_min if body.tier_min is not None else 0]
    idx = 2

    if body.price_max is not None:
        conditions.append(f"(pricing_usdc IS NULL OR pricing_usdc <= ${idx})")
        params.append(body.price_max)
        idx += 1

    if body.category:
        conditions.append(f"category = ${idx}")
        params.append(body.category)
        idx += 1

    if body.protocol and body.protocol != "any":
        conditions.append(f"payment_protocol = ${idx}")
        params.append(body.protocol)
        idx += 1

    if body.x402_only:
        conditions.append("x402_supported = true")

    if body.verified_only:
        conditions.append("coverage_tier >= 2")

    if body.provider:
        conditions.append(f"LOWER(name) LIKE ${idx}")
        params.append(f"%{body.provider.lower()}%")
        idx += 1

    where = " AND ".join(conditions)
    limit = min(body.limit or 5, 50)
    offset = max(body.offset or 0, 0)

    fetch_n = (offset + limit) * 4
    try:
        async with request.app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, name, slug, description, endpoint_url, category,
                       pricing_usdc, coverage_tier, source, payment_protocol,
                       last_tested_at, consecutive_failures, x402_supported
                FROM services
                WHERE {where}
                ORDER BY coverage_tier DESC
                LIMIT {fetch_n}
                """,
                *params,
            )
    except Exception as e:
        logger.error(f"DB error in /query: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not rows:
        return {"query": body.query, "results": [], "total": 0, "protocol": "WayforthQL/2.0"}

    candidates = [dict(r) for r in rows]
    ranked = await rank_services(body.query, candidates)

    # Secondary sort before slicing
    if body.sort_by == "price":
        ranked.sort(key=lambda s: (s.get("pricing_usdc") is None, s.get("pricing_usdc") or 0))
    elif body.sort_by == "tier":
        ranked.sort(key=lambda s: s.get("coverage_tier", 0), reverse=True)

    # Exclude specific service IDs
    if body.exclude_ids:
        exclude_set = set(body.exclude_ids)
        ranked = [
            s for s in ranked
            if ("0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest()) not in exclude_set
        ]

    results_raw = ranked[offset:offset + limit]

    results = []
    for s in results_raw:
        service_id = "0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest()
        name_slug = s.get("slug") or s.get("name", "").lower().replace(" ", "_").replace("/", "_")[:30]
        entry = {
            "name": s.get("name"),
            "slug": s.get("slug"),
            "score": s.get("score", 0),
            "wri": compute_wri(s, s.get("score", 0)),
            "reason": s.get("reason", ""),
            "coverage_tier": s.get("coverage_tier"),
            "category": s.get("category"),
            "endpoint_url": s.get("endpoint_url"),
            "pricing": {
                "per_call_usd": s.get("pricing_usdc"),
                "credits_per_call": max(1, round((s.get("pricing_usdc") or 0.001) * 1000)),
            },
            "service_id": service_id,
            "wayforth_id": f"wayforth://{name_slug}/{service_id[2:10]}",
            "payment_options": {
                "track_a": {
                    "method": "card",
                    "processor": "Stripe Treasury",
                    "credits_needed": max(1, round((s.get("pricing_usdc") or 0.001) * 1000)),
                    "fee_pct": 1.5,
                },
                "track_b": {
                    "method": "crypto",
                    "network": "base-sepolia",
                    "amount_usdc": s.get("pricing_usdc") or 0.001,
                    "fee_pct": 1.5,
                    "calldata_via": "wayforth_pay(service_id, amount_usd, track='crypto')",
                },
                "x402_supported": bool(s.get("x402_supported", False)),
            },
        }
        results.append(entry)

    # Attach similar services for top result when requested
    if body.with_similar and results_raw:
        top_id = str(results_raw[0].get("id", ""))
        try:
            async with request.app.state.pool.acquire() as conn:
                graph_rows = await conn.fetch(
                    """
                    SELECT
                        CASE WHEN service_a_id = $1 THEN service_b_id ELSE service_a_id END AS related_id,
                        co_search_count
                    FROM service_graph
                    WHERE service_a_id = $1 OR service_b_id = $1
                    ORDER BY co_search_count DESC LIMIT 5
                    """,
                    top_id,
                )
                similar = []
                for gr in graph_rows:
                    svc = await conn.fetchrow(
                        "SELECT name, category, coverage_tier FROM services WHERE id::text = $1",
                        gr["related_id"],
                    )
                    if svc:
                        similar.append({
                            "service_id": gr["related_id"],
                            "name": svc["name"],
                            "category": svc["category"],
                            "tier": svc["coverage_tier"],
                            "co_search_count": gr["co_search_count"],
                        })
            results[0]["similar_services"] = similar
        except Exception as e:
            logger.warning(f"with_similar failed: {e}")

    response: dict = {
        "query": body.query,
        "results": results,
        "total": len(results),
        "offset": offset,
        "protocol": "WayforthQL/2.0",
        "filters_applied": {
            "tier_min": body.tier_min,
            "price_max": body.price_max,
            "category": body.category,
            "protocol": body.protocol,
            "sort_by": body.sort_by,
            "exclude_ids": body.exclude_ids or [],
            "x402_only": body.x402_only,
            "verified_only": body.verified_only,
            "provider": body.provider,
        },
    }
    if not auth["authenticated"]:
        remaining = _ANON_DAILY_LIMIT - auth["anonymous_count"]
        response["anonymous_searches_remaining"] = remaining
        if remaining > 0:
            response["signup_url"] = "https://wayforth.io/signup"
            response["message"] = f"{remaining} free {'search' if remaining == 1 else 'searches'} remaining. Sign up free for 100/month."
    return response


@router.get("/services")
@limiter.limit("20/minute")
async def list_services(
    request: Request,
    category: str = None,
    tier: int = None,
    protocol: str = None,
    real_only: bool = True,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort: str = "tier",
    db=Depends(get_db),
):
    conditions = ["1=1"]
    params = []
    idx = 1

    if real_only:
        conditions.append("""(
            endpoint_url NOT ILIKE '%github.com%'
            AND endpoint_url NOT ILIKE '%glama.ai%'
            AND endpoint_url NOT ILIKE '%smithery%'
        )""")

    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1
    if tier is not None:
        conditions.append(f"coverage_tier >= ${idx}")
        params.append(tier)
        idx += 1
    if protocol:
        conditions.append(f"payment_protocol = ${idx}")
        params.append(protocol)
        idx += 1

    order = "coverage_tier DESC, name ASC" if sort == "tier" else "name ASC"

    try:
        rows = await db.fetch(f"""
            SELECT id, name, description, endpoint_url, category,
                   pricing_usdc, coverage_tier, payment_protocol, source, created_at,
                   last_tested_at, consecutive_failures, x402_supported
            FROM services
            WHERE {' AND '.join(conditions)}
            ORDER BY {order}
            LIMIT ${idx} OFFSET ${idx + 1}
        """, *params, min(limit, 100), offset)

        total = await db.fetchval(f"""
            SELECT COUNT(*) FROM services WHERE {' AND '.join(conditions)}
        """, *params)
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    return {
        "services": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "filters": {"category": category, "tier": tier, "protocol": protocol, "real_only": real_only},
    }


@router.get("/services/search")
@limiter.limit("20/minute")
async def services_search_alias(request: Request, q: str = "", limit: int = 5, db=Depends(get_db)):
    """Alias for /search — same behavior."""
    return RedirectResponse(url=f"/search?q={q}&limit={limit}", status_code=307)


@router.get("/services/categories")
@limiter.limit("20/minute")
async def list_categories(request: Request, db=Depends(get_db)):
    """All service categories with counts."""
    try:
        rows = await db.fetch("""
            SELECT category, COUNT(*) as count,
                   COUNT(*) FILTER (WHERE coverage_tier >= 2) as tier2_count
            FROM services
            WHERE category IS NOT NULL
            GROUP BY category ORDER BY count DESC
        """)
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"categories": [dict(r) for r in rows], "total": len(rows)}


@router.get("/services/featured")
@limiter.limit("30/minute")
async def featured_services(request: Request, db=Depends(get_db)):
    """Featured services — one per category, Tier 2 only, best WRI score. Powers the homepage inline search default state."""
    try:
        rows = await db.fetch("""
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY category ORDER BY coverage_tier DESC, name ASC
                ) as rn
                FROM services
                WHERE coverage_tier >= 2
            )
            SELECT name, description, category, pricing_usdc,
                   coverage_tier, payment_protocol,
                   encode(sha256(endpoint_url::bytea), 'hex') as service_id
            FROM ranked WHERE rn = 1
            ORDER BY category
        """)
    except Exception as e:
        logger.error(f"DB error in featured_services: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {
        "featured": [dict(r) for r in rows],
        "total": len(rows),
        "note": "One Tier 2 verified service per category",
    }


@router.get("/stats")
@limiter.limit("30/minute")
async def get_stats(request: Request, db=Depends(get_db)):
    from main import app

    try:
        row = await db.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE consecutive_failures < 3) as total,
                COUNT(*) FILTER (WHERE coverage_tier >= 2 AND consecutive_failures < 3) as tier2,
                COUNT(*) FILTER (WHERE coverage_tier >= 3 AND consecutive_failures < 3) as tier3,
                COUNT(*) FILTER (WHERE consecutive_failures < 3) as real_apis,
                COUNT(DISTINCT category) FILTER (WHERE consecutive_failures < 3) as categories
            FROM services
        """)
        searches_7d = await db.fetchval("""
            SELECT COUNT(*) FROM search_analytics
            WHERE created_at > NOW() - INTERVAL '7 days'
        """)
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    from main import VERSION
    return {
        "total_services": row["total"],
        "real_apis": row["real_apis"],
        "tier2_services": row["tier2"],
        "tier3_services": row["tier3"],
        "categories": row["categories"],
        "searches_7d": searches_7d,
        "mcp_tools": 16,
        "api_version": VERSION,
        "mcp_version": VERSION,
    }


@router.get("/services/count")
@limiter.limit("30/minute")
async def service_count(request: Request, db=Depends(get_db)):
    """Live service counts — use this to display accurate numbers on the website."""
    try:
        row = await db.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE consecutive_failures < 3) as total,
                COUNT(*) FILTER (WHERE coverage_tier >= 2 AND consecutive_failures < 3) as tier2,
                COUNT(*) FILTER (WHERE coverage_tier >= 3 AND consecutive_failures < 3) as tier3,
                COUNT(*) FILTER (WHERE consecutive_failures < 3) as real_apis
            FROM services
        """)
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    return {
        "total": row["total"],
        "real_apis": row["real_apis"],
        "tier2": row["tier2"],
        "tier3": row["tier3"],
        "display": {
            "total": f"{row['real_apis']:,}+",
            "tier2": f"{row['tier2']}+",
        },
    }


@router.get("/health-report")
@limiter.limit("10/minute")
async def health_report(request: Request):
    from main import app

    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT name, consecutive_failures, last_tested_at
                FROM services WHERE coverage_tier = 2
                ORDER BY name
                """
            )
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    services = [
        {
            "name": r["name"],
            "status": "up" if (r["consecutive_failures"] or 0) == 0 else "degraded",
            "consecutive_failures": r["consecutive_failures"] or 0,
            "last_checked": r["last_tested_at"].isoformat() if r["last_tested_at"] else None,
        }
        for r in rows
    ]
    return {
        "tier2_services": services,
        "total_tier2": len(services),
        "all_healthy": all(s["status"] == "up" for s in services),
    }


@router.get("/leaderboard/x402")
@limiter.limit("20/minute")
async def leaderboard_x402(request: Request, limit: int = 20, db=Depends(get_db)):
    rows = await db.fetch("""
        SELECT
            s.name,
            s.category,
            s.coverage_tier,
            s.payment_protocol,
            s.pricing_usdc,
            s.consecutive_failures,
            s.last_tested_at,
            encode(sha256(s.endpoint_url::bytea), 'hex') as service_id,
            COUNT(DISTINCT sa.id) as search_count,
            COUNT(DISTINCT so.id) as payment_count
        FROM services s
        LEFT JOIN search_analytics sa ON
            sa.top_result_id::text = '0x' || encode(sha256(s.endpoint_url::bytea), 'hex')
            AND sa.created_at > NOW() - INTERVAL '7 days'
        LEFT JOIN search_outcomes so ON
            so.service_id::text = '0x' || encode(sha256(s.endpoint_url::bytea), 'hex')
            AND so.outcome_type = 'payment_initiated'
            AND so.created_at > NOW() - INTERVAL '7 days'
        WHERE s.coverage_tier >= 2
        GROUP BY s.name, s.category, s.coverage_tier, s.payment_protocol,
                 s.pricing_usdc, s.consecutive_failures, s.last_tested_at, s.endpoint_url
        ORDER BY s.coverage_tier DESC, payment_count DESC, search_count DESC, s.name ASC
        LIMIT $1
    """, limit)

    results = []
    for r in rows:
        svc = dict(r)
        service_id = '0x' + svc['service_id']
        svc['service_id'] = service_id

        score = 50.0
        tier = svc.get('coverage_tier', 0)
        if tier >= 2: score += 20
        elif tier >= 1: score += 5
        if svc.get('consecutive_failures', 1) == 0: score += 10
        if svc.get('payment_protocol') == 'x402': score += 5
        if svc.get('payment_count', 0) > 0: score += min(svc['payment_count'] * 2, 8)
        svc['wri'] = round(min(score, 100), 1)

        price = svc.get('pricing_usdc')
        svc['price_display'] = f"${price:.7f}/req".rstrip('0').rstrip('.') + '/req' if price and price > 0 else "Free"

        results.append(svc)

    results.sort(key=lambda x: (x.get('wri', 0), x.get('payment_count', 0)), reverse=True)
    for i, r in enumerate(results, 1):
        r['rank'] = i

    return {
        "leaderboard": results,
        "total": len(results),
        "period": "7d"
    }


@router.get("/compare", tags=["Discovery"])
@limiter.limit("20/minute")
async def compare_services(
    request: Request,
    slugs: str = "",
    query: str = "",
    auth: dict = Depends(check_auth),
    db=Depends(get_db),
):
    """Compare 2-5 services side by side: WRI, cost, signals, response time, recommendation."""
    from datetime import datetime, timezone
    from ranker_client import rank_services
    from services.x402_pricing import X402_PRICES_USDC

    require_tier(auth.get("tier") or "free", "compare")
    slug_list = [s.strip().lower() for s in slugs.split(",") if s.strip()]

    if len(slug_list) < 2:
        raise HTTPException(status_code=422, detail={
            "error": "too_few_services",
            "message": "Compare requires at least 2 services. Provide slugs=a,b",
        })
    if len(slug_list) > 5:
        raise HTTPException(status_code=422, detail={
            "error": "too_many_services",
            "message": "Compare supports up to 5 services at a time.",
        })

    # Build the catalog-slug lookup list: accept both managed and catalog slugs
    catalog_lookup = []
    managed_lookup = []
    for s in slug_list:
        cat = MANAGED_TO_CATALOG.get(s, s)  # if it's a managed slug, map it; else use as-is
        catalog_lookup.append(cat)
        managed_lookup.append(s)

    slug_map_values = ", ".join(
        f"('{cat}', '{mgd}')" for mgd, cat in MANAGED_TO_CATALOG.items()
    )
    all_managed = list(MANAGED_TO_CATALOG.keys())

    rows = await db.fetch(
        f"""
        WITH slug_map(catalog_slug, managed_slug) AS (
            VALUES {slug_map_values}
        ),
        sig AS (
            SELECT clicked_slug, COUNT(*) AS total_signals
            FROM search_analytics
            WHERE clicked_slug = ANY($3::text[])
            GROUP BY clicked_slug
        ),
        probe_agg AS (
            SELECT sp.service_id::text,
                   AVG(sp.response_time_ms)::float AS avg_ms,
                   COUNT(*) AS total_probes,
                   COUNT(*) FILTER (WHERE sp.reachable) AS success_probes
            FROM service_probes sp
            WHERE sp.probed_at > NOW() - INTERVAL '7 days'
            GROUP BY sp.service_id
        )
        SELECT s.id::text, s.slug, s.name, s.category, s.wri_score,
               s.x402_supported, s.consecutive_failures, s.last_tested_at,
               s.pricing_usdc,
               COALESCE(sm.managed_slug, s.slug) AS managed_slug,
               COALESCE(sig.total_signals, 0) AS total_signals,
               pa.avg_ms AS avg_response_ms,
               pa.total_probes, pa.success_probes
        FROM services s
        LEFT JOIN slug_map sm ON sm.catalog_slug = s.slug
        LEFT JOIN sig ON sig.clicked_slug = COALESCE(sm.managed_slug, s.slug)
        LEFT JOIN probe_agg pa ON pa.service_id = s.id::text
        WHERE s.slug = ANY($1::text[])
           OR COALESCE(sm.managed_slug, s.slug) = ANY($2::text[])
        """,
        catalog_lookup, slug_list, all_managed,
    )

    found_managed = {r["managed_slug"] for r in rows}
    not_found = [s for s in slug_list if s not in found_managed]

    if not rows:
        raise HTTPException(status_code=404, detail={
            "error": "no_services_found",
            "message": "None of the requested slugs were found in the catalog.",
            "not_found": slug_list,
        })

    # Step 2 — Relevance scoring
    relevance: dict[str, float] = {}
    if query.strip() and rows:
        try:
            candidates = [dict(r) for r in rows]
            ranked = await rank_services(query, candidates)
            for i, svc in enumerate(ranked):
                ms = svc.get("managed_slug") or _CATALOG_SLUG_TO_MANAGED.get(svc.get("slug", ""), svc.get("slug", ""))
                relevance[ms] = float(svc.get("score", max(0, 80 - i * 10)))
        except Exception:
            pass

    # Step 3 — Build service objects and rank
    services_out = []
    for row in rows:
        ms = row["managed_slug"]
        cfg = SERVICE_CONFIGS.get(ms, {})
        credits = cfg.get("credits")
        cost_usd = cfg.get("real_cost_per_call")
        x402_price_str = X402_PRICES_USDC.get(ms)

        total_probes = row["total_probes"] or 0
        success_probes = row["success_probes"] or 0
        if total_probes > 0:
            uptime_pct = round(success_probes / total_probes * 100, 1)
        else:
            uptime_pct = 99.9 if (row["consecutive_failures"] or 0) == 0 else None

        wri = row["wri_score"]
        rel = relevance.get(ms, wri or 0.0)

        services_out.append({
            "slug": ms,
            "name": SERVICE_DISPLAY_NAMES.get(ms, row["name"]),
            "category": row["category"],
            "wri_score": wri,
            "total_signals": row["total_signals"],
            "payment_rate": 100.0 if ms in SERVICE_CONFIGS else None,
            "credits_per_call": credits,
            "cost_per_call_usd": cost_usd,
            "x402_price_usd": float(x402_price_str) if x402_price_str else None,
            "x402_supported": bool(row["x402_supported"]),
            "managed": ms in SERVICE_CONFIGS,
            "zero_setup": ms in SERVICE_CONFIGS,
            "avg_response_ms": round(row["avg_response_ms"]) if row["avg_response_ms"] else None,
            "uptime_7d_pct": uptime_pct,
            "relevance_score": round(rel, 1),
            "_sort_key": (rel, wri or 0.0),
        })

    services_out.sort(key=lambda x: x.pop("_sort_key"), reverse=True)

    # Assign rank and verdict
    best_wri_val = max((s["wri_score"] or 0) for s in services_out)
    min_credits = min((s["credits_per_call"] or 9999) for s in services_out)
    max_signals = max((s["total_signals"] or 0) for s in services_out)
    min_ms = min((s["avg_response_ms"] or 9999) for s in services_out)

    for i, svc in enumerate(services_out):
        svc["rank"] = i + 1
        if i == 0:
            svc["verdict"] = "best_overall"
        elif (svc["wri_score"] or 0) > 0 and (svc["wri_score"] or 0) == best_wri_val and i > 0:
            svc["verdict"] = "best_wri"
        elif svc["credits_per_call"] == min_credits and min_credits < 9999:
            svc["verdict"] = "best_value"
        elif svc["avg_response_ms"] and svc["avg_response_ms"] == min_ms and min_ms < 9999:
            svc["verdict"] = "fastest"
        elif svc["total_signals"] == max_signals and max_signals > 0:
            svc["verdict"] = "most_proven"
        else:
            svc["verdict"] = None

    # Step 5 — Recommendation
    top = services_out[0]
    reason_parts = [f"{top['name']} leads with WRI {top['wri_score']}"]
    if top["total_signals"]:
        reason_parts.append(f"{top['total_signals']} search signals")
    if top["avg_response_ms"]:
        reason_parts.append(f"{top['avg_response_ms']}ms avg response")
    if top["payment_rate"] == 100.0:
        reason_parts.append("100% payment conversion")
    if top["zero_setup"]:
        reason_parts.append("zero setup")
    recommendation = {
        "slug": top["slug"],
        "reason": ". ".join(reason_parts) + ".",
    }

    # Step 6 — Comparison matrix
    fastest_svc = min(services_out, key=lambda x: x["avg_response_ms"] or 9999)
    cheapest_svc = min(services_out, key=lambda x: x["credits_per_call"] or 9999)
    signals_svc = max(services_out, key=lambda x: x["total_signals"] or 0)
    wri_svc = max(services_out, key=lambda x: x["wri_score"] or 0)
    x402_svcs = [s for s in services_out if s["x402_supported"]]

    comparison_matrix = {
        "fastest": fastest_svc["slug"] if fastest_svc["avg_response_ms"] else None,
        "cheapest": cheapest_svc["slug"] if (cheapest_svc["credits_per_call"] or 9999) < 9999 else None,
        "most_signals": signals_svc["slug"] if signals_svc["total_signals"] else None,
        "best_wri": wri_svc["slug"] if wri_svc["wri_score"] else None,
        "x402_native": x402_svcs[0]["slug"] if x402_svcs else None,
    }

    result: dict = {
        "query": query or None,
        "compared_at": datetime.now(timezone.utc).isoformat(),
        "services": services_out,
        "recommendation": recommendation,
        "comparison_matrix": comparison_matrix,
    }
    if not_found:
        result["not_found"] = not_found

    return result


@router.get("/status/services", tags=["Public"])
async def status_services():
    """Public health status for all 15 managed services. No auth required."""
    from datetime import datetime, timezone
    from main import app

    pool = app.state.pool
    if not pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id::text, s.slug, s.name, s.category,
                   s.wri_score, s.consecutive_failures, s.last_tested_at,
                   sp_agg.avg_ms, sp_agg.total_probes, sp_agg.success_probes
            FROM services s
            LEFT JOIN LATERAL (
                SELECT AVG(sp.response_time_ms)::float AS avg_ms,
                       COUNT(*) AS total_probes,
                       COUNT(*) FILTER (WHERE sp.reachable) AS success_probes
                FROM service_probes sp
                WHERE sp.service_id = s.id
                  AND sp.probed_at > NOW() - INTERVAL '7 days'
            ) sp_agg ON true
            WHERE s.slug = ANY($1::text[])
            """,
            _CATALOG_SLUGS,
        )

    now = datetime.now(timezone.utc)
    services_out = []
    for row in sorted(rows, key=lambda r: (r["wri_score"] or 0), reverse=True):
        managed_slug = _CATALOG_SLUG_TO_MANAGED.get(row["slug"], row["slug"])
        status, status_label = _service_status(row["consecutive_failures"] or 0)
        total_probes = row["total_probes"] or 0
        success_probes = row["success_probes"] or 0
        if total_probes > 0:
            uptime_pct = round(success_probes / total_probes * 100, 1)
        elif (row["consecutive_failures"] or 0) == 0:
            uptime_pct = 99.9
        else:
            uptime_pct = max(0.0, round(100 - (row["consecutive_failures"] / max(total_probes, 1)) * 100, 1))

        services_out.append({
            "slug": managed_slug,
            "name": SERVICE_DISPLAY_NAMES.get(managed_slug, row["name"]),
            "category": row["category"],
            "status": status,
            "status_label": status_label,
            "last_tested_at": row["last_tested_at"].isoformat() if row["last_tested_at"] else None,
            "avg_response_ms": round(row["avg_ms"]) if row["avg_ms"] else None,
            "uptime_7d_pct": uptime_pct,
            "consecutive_failures": row["consecutive_failures"] or 0,
            "wri_score": row["wri_score"],
        })

    # Add any managed services missing from DB with defaults
    present = {s["slug"] for s in services_out}
    for managed_slug in MANAGED_TO_CATALOG:
        if managed_slug not in present:
            services_out.append({
                "slug": managed_slug,
                "name": SERVICE_DISPLAY_NAMES.get(managed_slug, managed_slug),
                "category": None,
                "status": "unknown",
                "status_label": "Unknown",
                "last_tested_at": None,
                "avg_response_ms": None,
                "uptime_7d_pct": None,
                "consecutive_failures": 0,
                "wri_score": None,
            })

    statuses = {s["status"] for s in services_out}
    if "outage" in statuses:
        overall = "outage"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "operational"

    return JSONResponse(
        content={
            "updated_at": now.isoformat(),
            "overall_status": overall,
            "services": services_out,
            "incidents": [],
        },
        headers={"Cache-Control": "public, max-age=60", "Access-Control-Allow-Origin": "*"},
    )


@router.get("/leaderboard", tags=["Public"])
async def leaderboard_managed():
    """Public WRI leaderboard for all managed services. No auth required."""
    from datetime import datetime, timezone, timedelta
    from main import app

    pool = app.state.pool
    if not pool:
        raise HTTPException(status_code=503, detail="Database unavailable")

    slug_map_values = ", ".join(
        f"('{cat}', '{mgd}')" for mgd, cat in MANAGED_TO_CATALOG.items()
    )
    managed_slugs = list(MANAGED_TO_CATALOG.keys())

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            WITH slug_map(catalog_slug, managed_slug) AS (
                VALUES {slug_map_values}
            ),
            sig AS (
                SELECT clicked_slug, COUNT(*) AS total_signals
                FROM search_analytics
                WHERE clicked_slug = ANY($2::text[])
                GROUP BY clicked_slug
            )
            SELECT s.slug, s.name, s.category, s.wri_score, s.x402_supported,
                   s.consecutive_failures,
                   COALESCE(sig.total_signals, 0) AS total_signals
            FROM services s
            JOIN slug_map sm ON sm.catalog_slug = s.slug
            LEFT JOIN sig ON sig.clicked_slug = sm.managed_slug
            WHERE s.slug = ANY($1::text[])
            ORDER BY s.wri_score DESC NULLS LAST
            """,
            _CATALOG_SLUGS,
            managed_slugs,
        )

    now = datetime.now(timezone.utc)
    services_out = []
    rank = 1
    for row in rows:
        managed_slug = _CATALOG_SLUG_TO_MANAGED.get(row["slug"], row["slug"])
        cfg = SERVICE_CONFIGS.get(managed_slug, {})
        services_out.append({
            "rank": rank,
            "slug": managed_slug,
            "name": SERVICE_DISPLAY_NAMES.get(managed_slug, row["name"]),
            "category": row["category"],
            "wri_score": row["wri_score"],
            "total_signals": row["total_signals"],
            "managed": True,
            "zero_setup": True,
            "credits_per_call": cfg.get("credits"),
            "x402_supported": bool(row["x402_supported"]),
            "payment_rate": 100.0,
        })
        rank += 1

    # Append any managed services not in DB
    present_slugs = {s["slug"] for s in services_out}
    for managed_slug, catalog_slug in MANAGED_TO_CATALOG.items():
        if managed_slug not in present_slugs:
            cfg = SERVICE_CONFIGS.get(managed_slug, {})
            services_out.append({
                "rank": rank,
                "slug": managed_slug,
                "name": SERVICE_DISPLAY_NAMES.get(managed_slug, managed_slug),
                "category": None,
                "wri_score": None,
                "managed": True,
                "zero_setup": True,
                "credits_per_call": cfg.get("credits"),
                "x402_supported": False,
                "payment_rate": 100.0,
            })
            rank += 1

    return JSONResponse(
        content={
            "updated_at": now.isoformat(),
            "version": "2.0",
            "services": services_out,
            "total_ranked": len(services_out),
            "next_update": (now + timedelta(hours=24)).replace(
                hour=20, minute=0, second=0, microsecond=0
            ).isoformat(),
        },
        headers={"Cache-Control": "public, max-age=60", "Access-Control-Allow-Origin": "*"},
    )


# ── Memory ────────────────────────────────────────────────────────────────────

@router.post("/memory")
@limiter.limit("30/minute")
async def save_memory(request: Request, body: MemoryItem, db=Depends(get_db)):
    """Save a service to agent memory. Requires X-Wayforth-API-Key."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "api_key_required"})
    await _resolve_user(db, api_key)
    await db.execute(
        """
        INSERT INTO agent_memory (agent_id, service_id, service_name, note, created_at, updated_at)
        VALUES ($1, $2, $3, $4, NOW(), NOW())
        ON CONFLICT (agent_id, service_id)
        DO UPDATE SET note=$4, updated_at=NOW()
        """,
        body.agent_id or "anonymous", body.service_id, body.service_name, body.note,
    )
    return {"status": "saved", "service_id": body.service_id, "service_name": body.service_name}


@router.get("/memory")
@limiter.limit("30/minute")
async def get_memory(request: Request, agent_id: str = "anonymous", q: str = "", db=Depends(get_db)):
    """Retrieve agent's saved services. Requires X-Wayforth-API-Key."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "api_key_required"})
    await _resolve_user(db, api_key)
    if q:
        rows = await db.fetch(
            """
            SELECT service_id, service_name, note, created_at
            FROM agent_memory
            WHERE agent_id = $1
            AND (LOWER(service_name) LIKE $2 OR LOWER(note) LIKE $2)
            ORDER BY created_at DESC LIMIT 20
            """,
            agent_id, f"%{q.lower()}%",
        )
    else:
        rows = await db.fetch(
            """
            SELECT service_id, service_name, note, created_at
            FROM agent_memory WHERE agent_id = $1
            ORDER BY created_at DESC LIMIT 20
            """,
            agent_id,
        )
    return {"agent_id": agent_id, "services": [dict(r) for r in rows], "total": len(rows)}


# ── Tier3 ─────────────────────────────────────────────────────────────────────

@router.post("/tier3/apply")
@limiter.limit("5/minute")
async def tier3_apply(request: Request, body: Tier3Application):
    """Apply for Tier 3 verification — KYB + SLA. Institutional-grade. Manual review required."""
    from main import app
    from notifications import send_tier3_application_notification

    async with app.state.pool.acquire() as db:
        existing = await db.fetchrow("""
            SELECT id, kyb_status FROM tier3_applications
            WHERE contact_email = $1 AND endpoint_url = $2
        """, body.contact_email, body.endpoint_url)

        if existing:
            return {
                "status": "already_applied",
                "kyb_status": existing["kyb_status"],
                "message": "Application already on file. We'll contact you at the email provided.",
            }

        app_id = await db.fetchval("""
            INSERT INTO tier3_applications
            (service_name, company_name, contact_email, website, endpoint_url,
             monthly_volume_usdc, sla_uptime_target, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            RETURNING id
        """, body.service_name, body.company_name, body.contact_email,
            body.website, body.endpoint_url, body.monthly_volume_usd,
            body.sla_uptime_target)

    if os.getenv("RESEND_API_KEY"):
        asyncio.create_task(asyncio.to_thread(
            send_tier3_application_notification,
            body.contact_email, body.service_name, body.company_name, str(app_id),
        ))

    return {
        "status": "submitted",
        "application_id": str(app_id),
        "message": "Application received. Our team will review your KYB documentation and contact you within 2 business days.",
        "next_steps": [
            "We will email you a KYB documentation checklist",
            "SLA terms will be negotiated based on your uptime target",
            "Tier 3 badge appears on your service within 24h of approval",
        ],
    }


@router.get("/tier3/status")
@limiter.limit("10/minute")
async def tier3_status(request: Request, email: str):
    """Check Tier 3 application status by email."""
    from main import app

    async with app.state.pool.acquire() as db:
        apps = await db.fetch("""
            SELECT id, service_name, company_name, kyb_status, created_at
            FROM tier3_applications WHERE contact_email = $1
            ORDER BY created_at DESC
        """, email)
    if not apps:
        return {"status": "not_found", "message": "No application found for this email."}
    return {
        "applications": [dict(a) for a in apps],
        "total": len(apps),
    }


@router.get("/tier3/admin")
@limiter.limit("10/minute")
async def tier3_admin(request: Request, key: str = ""):
    """Admin view of Tier 3 applications filtered by KYB status."""
    from main import app, ADMIN_KEY

    if not ADMIN_KEY or not secrets.compare_digest(key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    async with app.state.pool.acquire() as db:
        apps = await db.fetch("""
            SELECT id, service_name, company_name, contact_email, endpoint_url,
                   monthly_volume_usdc, sla_uptime_target, kyb_status, created_at
            FROM tier3_applications WHERE kyb_status = $1
            ORDER BY created_at DESC
        """, request.query_params.get("status", "pending"))
    return {
        "status_filter": request.query_params.get("status", "pending"),
        "applications": [dict(a) for a in apps],
        "total": len(apps),
    }


# ── Graph / Similar services ──────────────────────────────────────────────────

@router.get("/graph/{service_id}")
@limiter.limit("20/minute")
async def get_service_graph(request: Request, service_id: str, limit: int = 10):
    """Return related services based on co-usage patterns."""
    from main import app

    async with app.state.pool.acquire() as db:
        return await _get_similar_services(db, service_id, limit)


@router.get("/services/similar/{service_id}")
@limiter.limit("30/minute")
async def similar_services(request: Request, service_id: str, limit: int = 5):
    """Public endpoint. Returns services commonly used alongside this one."""
    from main import app

    async with app.state.pool.acquire() as db:
        return await _get_similar_services(db, service_id, limit)


# ── Intelligence / WRI history ────────────────────────────────────────────────

@router.get("/intelligence/{service_id}")
@limiter.limit("10/minute")
async def service_intelligence(request: Request, service_id: str, api_key: str = ""):
    """Wayforth Intelligence API — market data for service providers."""
    from main import app, ADMIN_KEY

    if not ADMIN_KEY or not secrets.compare_digest(api_key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Intelligence API key required. Contact us at https://wayforth.io/contact")

    async with app.state.pool.acquire() as db:
        internal_id = service_id
        if service_id.startswith("0x"):
            sha = service_id[2:]
            row = await db.fetchrow(
                "SELECT id FROM services WHERE encode(sha256(endpoint_url::bytea), 'hex') = $1", sha
            )
            if row:
                internal_id = str(row["id"])

        volume = await db.fetchrow(
            """
            SELECT COUNT(*) AS appearances, AVG((elem->>'score')::float) AS avg_score
            FROM search_analytics, jsonb_array_elements(results) AS elem
            WHERE elem->>'id' = $1
            AND created_at > NOW() - INTERVAL '7 days'
            """,
            internal_id,
        )

        rank_dist = await db.fetch(
            """
            SELECT position, COUNT(*) AS count FROM (
                SELECT ordinality - 1 AS position
                FROM search_analytics,
                     jsonb_array_elements(results) WITH ORDINALITY AS elem
                WHERE elem->>'id' = $1
                AND created_at > NOW() - INTERVAL '7 days'
            ) t GROUP BY position ORDER BY position
            """,
            internal_id,
        )

        conversions = await db.fetchval(
            """
            SELECT COUNT(*) FROM search_outcomes
            WHERE service_id::text = $1
            AND outcome_type = 'payment_initiated'
            AND created_at > NOW() - INTERVAL '7 days'
            """,
            internal_id,
        )

    return {
        "service_id": service_id,
        "period": "7d",
        "search_appearances": volume["appearances"] or 0,
        "avg_rank_score": round(volume["avg_score"] or 0, 1),
        "payment_conversions": conversions or 0,
        "rank_position_distribution": [dict(r) for r in rank_dist],
        "note": "Wayforth Intelligence API v1 — powered by real agent usage data",
    }


@router.get("/services/{service_id}/wri")
@limiter.limit("30/minute")
async def service_wri(request: Request, service_id: str, db=Depends(get_db)):
    """Current WRI score and 7-day trend for a service."""
    async with request.app.state.pool.acquire() as conn:
        history = await conn.fetch("""
            SELECT wri_score, tier, recorded_at
            FROM service_score_history
            WHERE service_id = $1
              AND recorded_at > NOW() - INTERVAL '7 days'
            ORDER BY recorded_at DESC LIMIT 30
        """, service_id)

    if not history:
        return {"service_id": service_id, "wri": None, "trend": "no_data", "history": []}

    scores = [r["wri_score"] for r in history]
    current = scores[0]
    trend = "stable"
    if len(scores) >= 4:
        recent = sum(scores[:2]) / 2
        older = sum(scores[-2:]) / 2
        if recent > older + 3:
            trend = "improving"
        elif recent < older - 3:
            trend = "declining"

    return {
        "service_id": service_id,
        "wri": current,
        "trend": trend,
        "avg_7d": round(sum(scores) / len(scores), 1),
        "history": [{"wri": r["wri_score"], "at": r["recorded_at"].isoformat()} for r in history],
    }


@router.get("/services/{service_id}/history")
@limiter.limit("20/minute")
async def service_history(request: Request, service_id: str, days: int = Query(default=30, ge=1, le=90)):
    """WRI score trend for a service over time. Powers reliability trend visualization."""
    async with request.app.state.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT wri_score, tier, consecutive_failures, recorded_at
            FROM service_score_history
            WHERE service_id = $1
              AND recorded_at > NOW() - ($2 * INTERVAL '1 day')
            ORDER BY recorded_at ASC
        """, service_id, days)

    if not rows:
        return {"service_id": service_id, "history": [], "trend": "insufficient_data"}

    scores = [r["wri_score"] for r in rows]
    trend = "stable"
    if len(scores) >= 3:
        recent_avg = sum(scores[-3:]) / 3
        older_avg = sum(scores[:3]) / 3
        if recent_avg > older_avg + 5:
            trend = "improving"
        elif recent_avg < older_avg - 5:
            trend = "declining"

    return {
        "service_id": service_id,
        "history": [{"wri": r["wri_score"], "tier": r["tier"], "at": r["recorded_at"].isoformat()} for r in rows],
        "current_wri": scores[-1],
        "avg_wri_30d": round(sum(scores) / len(scores), 1),
        "trend": trend,
        "data_points": len(scores),
    }


# ── MCP / static routes ───────────────────────────────────────────────────────

@router.get("/.well-known/mcp/server-card.json", include_in_schema=False)
async def mcp_server_card():
    """Smithery discovery endpoint — skip auto-scan and advertise tools directly."""
    return {
        "name": "wayforth",
        "version": "0.2.3",
        "description": "The search engine AI agents use to find and pay for APIs. Search 300+ verified APIs ranked by WayforthRank v2.",
        "repository": "https://github.com/WayforthOfficial/wayforth",
        "homepage": "https://wayforth.io",
        "license": "BSL-1.1",
        "runtime": "http",
        "url": "https://mcp.wayforth.io",
        "transport": "streamable-http",
        "configSchema": {
            "type": "object",
            "properties": {
                "WAYFORTH_API_KEY": {
                    "type": "string",
                    "description": "Your Wayforth API key — get one free at wayforth.io",
                    "required": True,
                }
            },
        },
        "tools": [
            {"name": "wayforth_search", "description": "Search 300+ verified APIs ranked by WayforthRank v2 payment signals"},
            {"name": "wayforth_execute", "description": "Execute any managed service with zero API keys"},
            {"name": "wayforth_query", "description": "WayforthQL structured query with filters"},
            {"name": "wayforth_pay", "description": "Pay for API services via card (Stripe Treasury) or crypto (Base USDC, non-custodial)"},
            {"name": "wayforth_keys", "description": "Manage BYOK encrypted service keys — store, list, delete"},
            {"name": "wayforth_list", "description": "Browse catalog with category and tier filters"},
            {"name": "wayforth_similar", "description": "Find co-used services from real agent behavior (Service Graph)"},
            {"name": "wayforth_identity", "description": "Agent trust score and reputation tracking"},
            {"name": "wayforth_remember", "description": "Save a service to persistent agent memory"},
            {"name": "wayforth_recall", "description": "Retrieve services saved to agent memory"},
            {"name": "wayforth_stats", "description": "Catalog statistics — totals, tier breakdown, category distribution"},
            {"name": "wayforth_status", "description": "API health check and live service counts"},
            {"name": "wayforth_quickstart", "description": "Step-by-step developer guide for using Wayforth in an agent"},
        ],
    }


@router.get("/mcp", include_in_schema=False)
async def mcp_manifest():
    """MCP server manifest — machine-readable tool registry for AI clients."""
    return {
        "schema_version": "v1",
        "name": "wayforth",
        "version": "0.2.3",
        "description": "Search engine and payment rail for AI agents. 300 verified APIs, 11 managed services, WayforthRank v2.",
        "homepage": "https://wayforth.io",
        "icon": "https://wayforth.io/logo.png",
        "transport": {
            "type": "stdio",
            "command": "uvx",
            "args": ["wayforth-mcp"],
        },
        "config": {
            "type": "object",
            "properties": {
                "WAYFORTH_API_KEY": {
                    "type": "string",
                    "description": "Your Wayforth API key — get one free at wayforth.io/signup",
                }
            },
            "required": ["WAYFORTH_API_KEY"],
        },
        "tools": [
            {
                "name": "wayforth_search",
                "description": "Semantic search across 300 APIs ranked by WayforthRank v2 payment-signal scoring.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural-language description of the API you need"},
                        "limit": {"type": "integer", "description": "Max results (default 5, max 20)"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "wayforth_query",
                "description": "WayforthQL structured discovery — tier/price/protocol filters, deterministic ranking.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "tier_min": {"type": "integer", "description": "Minimum coverage tier (1–3)"},
                        "price_max": {"type": "number", "description": "Max price in USDC"},
                        "sort_by": {"type": "string", "enum": ["wri", "price", "tier"]},
                        "protocol": {"type": "string", "enum": ["wayforth", "x402"]},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "wayforth_execute",
                "description": "Execute 11 managed services: groq, deepl, openweather, newsapi, serper, resend, assemblyai, stability, tavily, jina, alphavantage.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service_slug": {"type": "string", "description": "Service identifier (e.g. 'groq', 'deepl')"},
                        "params": {"type": "object", "description": "Service-specific parameters"},
                        "key_source": {"type": "string", "enum": ["managed", "byok"], "default": "managed"},
                    },
                    "required": ["service_slug", "params"],
                },
            },
            {
                "name": "wayforth_pay",
                "description": "Pay for API services via card (Stripe Treasury) or crypto (Base USDC, non-custodial).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service_slug": {"type": "string"},
                        "amount": {"type": "number", "description": "Amount in USDC"},
                        "track": {"type": "string", "enum": ["card", "crypto"], "default": "card"},
                    },
                    "required": ["service_slug", "amount"],
                },
            },
            {
                "name": "wayforth_keys",
                "description": "Manage BYOK encrypted service keys — store, list, delete.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["add", "list", "delete"]},
                        "service_slug": {"type": "string"},
                        "api_key": {"type": "string", "description": "Required for action='add'"},
                        "endpoint_url": {"type": "string", "description": "Default endpoint URL for universal BYOK"},
                        "default_method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
                    },
                    "required": ["action"],
                },
            },
            {
                "name": "wayforth_list",
                "description": "Browse catalog with category and tier filters.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"},
                        "tier": {"type": "integer"},
                        "limit": {"type": "integer"},
                    },
                },
            },
            {
                "name": "wayforth_similar",
                "description": "Find co-used services from real agent behavior (Service Graph).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service_slug": {"type": "string"},
                    },
                    "required": ["service_slug"],
                },
            },
            {
                "name": "wayforth_identity",
                "description": "Agent trust score and reputation tracking.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "wayforth_remember",
                "description": "Save a service to persistent agent memory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service_slug": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["service_slug"],
                },
            },
            {
                "name": "wayforth_recall",
                "description": "Retrieve services saved to agent memory.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "wayforth_stats",
                "description": "Catalog statistics — totals, tier breakdown, category distribution.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "wayforth_status",
                "description": "API health check and live service counts.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "wayforth_quickstart",
                "description": "Step-by-step developer guide for using Wayforth in an agent.",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ],
    }


@router.get("/demo")
async def demo():
    return FileResponse("static/demo.html")


@router.get("/leaderboard-page")
async def leaderboard_page():
    return FileResponse("static/leaderboard.html")


@router.get("/submit-page")
async def submit_page():
    return FileResponse("static/submit.html")


@router.get("/agent-demo")
async def agent_demo():
    return FileResponse("static/agent-demo.html")


@router.get("/wayforthql-spec", include_in_schema=False)
async def wayforthql_spec():
    return FileResponse("static/wayforthql.html")


@router.get("/roadmap", include_in_schema=False)
async def roadmap():
    return FileResponse("static/roadmap.html")


@router.get("/changelog", include_in_schema=False)
async def changelog_page():
    return FileResponse("static/changelog.html")


@router.get("/pricing", include_in_schema=False)
async def pricing_page():
    return FileResponse("static/pricing.html")


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


@router.get("/intelligence-demo", include_in_schema=False)
async def intelligence_demo():
    return FileResponse("static/intelligence-demo.html")


@router.get("/health-page", include_in_schema=False)
async def health_page():
    return FileResponse("static/health-report.html")


@router.get("/analytics")
@limiter.limit("10/minute")
async def get_analytics(request: Request, key: str = ""):
    from main import app, ADMIN_KEY

    if not ADMIN_KEY or not secrets.compare_digest(key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        async with app.state.pool.acquire() as conn:
            top_queries = await conn.fetch("""
                SELECT query, COUNT(*) as count,
                       AVG(result_count) as avg_results,
                       SUM(CASE WHEN led_to_payment THEN 1 ELSE 0 END) as payment_conversions
                FROM search_analytics
                WHERE created_at > NOW() - INTERVAL '7 days'
                GROUP BY query ORDER BY count DESC LIMIT 20
            """)
            stats = await conn.fetchrow("""
                SELECT
                    COUNT(*) as total_searches,
                    SUM(CASE WHEN led_to_payment THEN 1 ELSE 0 END) as paid_searches,
                    COUNT(DISTINCT service_id) as services_paid_for
                FROM search_analytics sa
                LEFT JOIN search_outcomes so ON so.query_text = sa.query
                WHERE sa.created_at > NOW() - INTERVAL '7 days'
            """)
            return_sessions = await conn.fetchval("""
                SELECT COUNT(DISTINCT session_id) FROM search_analytics
                WHERE session_id IS NOT NULL
                AND session_id IN (
                    SELECT session_id FROM search_analytics
                    WHERE created_at > NOW() - INTERVAL '7 days'
                    GROUP BY session_id HAVING COUNT(*) > 1
                )
            """)
            unique_sessions = await conn.fetchval("""
                SELECT COUNT(DISTINCT session_id) FROM search_analytics
                WHERE session_id IS NOT NULL
                AND created_at > NOW() - INTERVAL '7 days'
            """)
            top_services = await conn.fetch("""
                SELECT top_result_id, COUNT(*) as times_top_result
                FROM search_analytics
                WHERE top_result_id IS NOT NULL
                AND created_at > NOW() - INTERVAL '7 days'
                GROUP BY top_result_id
                ORDER BY times_top_result DESC
                LIMIT 10
            """)
    except Exception as e:
        logger.error(f"Analytics DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    return {
        "period": "7d",
        "top_queries": [dict(r) for r in top_queries],
        "total_searches": stats["total_searches"],
        "payment_conversions": stats["paid_searches"],
        "conversion_rate": round((stats["paid_searches"] or 0) / max(stats["total_searches"] or 1, 1) * 100, 2),
        "services_paid_for": stats["services_paid_for"],
        "return_sessions": return_sessions,
        "unique_sessions": unique_sessions,
        "top_services_by_search": [dict(r) for r in top_services],
    }


@router.get("/competitive")
@limiter.limit("10/minute")
async def competitive_intelligence_endpoint(request: Request, key: str = ""):
    """Admin: competitive intelligence and ecosystem growth signals."""
    from main import app, ADMIN_KEY

    if not ADMIN_KEY or not secrets.compare_digest(key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        async with app.state.pool.acquire() as conn:
            latest = await conn.fetchrow("""
                SELECT data, created_at FROM competitive_intelligence
                WHERE source = 'x402_monitor'
                ORDER BY created_at DESC LIMIT 1
            """)
            trend = await conn.fetch("""
                SELECT created_at, (data->>'live_count')::int as live_count
                FROM competitive_intelligence
                WHERE source = 'x402_monitor'
                ORDER BY created_at DESC LIMIT 30
            """)
    except Exception as e:
        logger.error(f"Competitive intelligence DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {
        "latest": json_lib.loads(latest["data"]) if latest else None,
        "last_checked": latest["created_at"].isoformat() if latest else None,
        "trend": [{"date": r["created_at"].isoformat(), "live_count": r["live_count"]} for r in trend],
    }
