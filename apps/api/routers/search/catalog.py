"""routers/search/catalog.py — /services*, /stats, /leaderboard*, /status/services, /health-report, /quickstart, /chain."""

import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from core.db import get_db
from core.rate_limit import limiter
from services.managed import SERVICE_CONFIGS, SERVICE_DISPLAY_NAMES
from services.param_mapper import MANAGED_TO_CATALOG

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


# ── Routes ────────────────────────────────────────────────────────────────────

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


@router.get("/intelligence-demo", include_in_schema=False)
async def intelligence_demo():
    return FileResponse("static/intelligence-demo.html")


@router.get("/health-page", include_in_schema=False)
async def health_page():
    return FileResponse("static/health-report.html")
