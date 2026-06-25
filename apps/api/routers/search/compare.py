"""routers/search/compare.py — /compare, /intelligence/*, /graph/*, /similar/*, /services/{id}/wri, /services/{id}/history, /analytics, /competitive."""

import hashlib
import json as json_lib
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from core.auth import check_auth
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier
from services.managed import SERVICE_CONFIGS, SERVICE_DISPLAY_NAMES
from services.param_mapper import MANAGED_TO_CATALOG

logger = logging.getLogger("wayforth")

router = APIRouter()

_CATALOG_SLUG_TO_MANAGED = {v: k for k, v in MANAGED_TO_CATALOG.items()}


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


@router.get("/compare", tags=["Discovery"])
@limiter.limit("20/minute")
async def compare_services(
    request: Request,
    slugs: str = "",
    query: str = "",
    auth: dict = Depends(check_auth),
    db=Depends(get_db),
):
    """Compare 2-5 services side by side: reliability score, cost, signals, response time, recommendation."""
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
            pass  # non-critical: relevance ranking falls back to positional scores

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
            "_credits": credits,  # internal only — popped before response
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
    min_credits = min((s["_credits"] or 9999) for s in services_out)
    max_signals = max((s["total_signals"] or 0) for s in services_out)
    min_ms = min((s["avg_response_ms"] or 9999) for s in services_out)

    for i, svc in enumerate(services_out):
        svc["rank"] = i + 1
        if i == 0:
            svc["verdict"] = "best_overall"
        elif (svc["wri_score"] or 0) > 0 and (svc["wri_score"] or 0) == best_wri_val and i > 0:
            svc["verdict"] = "best_wri"
        elif svc["_credits"] == min_credits and min_credits < 9999:
            svc["verdict"] = "best_value"
        elif svc["avg_response_ms"] and svc["avg_response_ms"] == min_ms and min_ms < 9999:
            svc["verdict"] = "fastest"
        elif svc["total_signals"] == max_signals and max_signals > 0:
            svc["verdict"] = "most_proven"
        else:
            svc["verdict"] = None

    # Step 5 — Recommendation
    top = services_out[0]
    reason_parts = [f"{top['name']} leads with reliability score {top['wri_score']}"]
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
    cheapest_svc = min(services_out, key=lambda x: x["_credits"] or 9999)
    signals_svc = max(services_out, key=lambda x: x["total_signals"] or 0)
    wri_svc = max(services_out, key=lambda x: x["wri_score"] or 0)
    x402_svcs = [s for s in services_out if s["x402_supported"]]

    comparison_matrix = {
        "fastest": fastest_svc["slug"] if fastest_svc["avg_response_ms"] else None,
        "cheapest": cheapest_svc["slug"] if (cheapest_svc["_credits"] or 9999) < 9999 else None,
        "most_signals": signals_svc["slug"] if signals_svc["total_signals"] else None,
        "best_wri": wri_svc["slug"] if wri_svc["wri_score"] else None,
        "x402_native": x402_svcs[0]["slug"] if x402_svcs else None,
    }

    # Strip internal computation keys before serialization
    for svc in services_out:
        svc.pop("_credits", None)

    from datetime import datetime, timezone
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
    """Current reliability score and 7-day trend for a service."""
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
    """reliability score trend for a service over time. Powers reliability trend visualization."""
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


@router.get("/.well-known/mcp/server-card.json", include_in_schema=False)
async def mcp_server_card():
    """Smithery discovery endpoint — skip auto-scan and advertise tools directly."""
    return {
        "name": "wayforth",
        "version": "0.2.3",
        "description": "The search engine AI agents use to find and pay for APIs. Search 300+ verified APIs ranked by merit-based routing (no paid placement).",
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
            {"name": "wayforth_search", "description": "Search 300+ verified APIs ranked by merit-based ranking signals"},
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
        "description": "Search engine and payment rail for AI agents. 300 verified APIs, 11 managed services, merit-based ranking.",
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
                "description": "Semantic search across 300 APIs ranked by merit-based ranking.",
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
