"""routers/admin/services.py — /admin/health, /admin/services, /admin/catalog/*, /admin/api-health, /admin/signals, /admin/revenue, /admin/subscriptions/debug."""

import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from core.db import get_db
from core.rate_limit import limiter
from services.managed import SERVICE_CONFIGS


async def _admin_ok(request: Request, db, key: str = "") -> bool:
    """Accept either static ADMIN_KEY or a valid X-Admin-Token session."""
    from main import ADMIN_KEY
    provided = request.headers.get("X-Admin-Key", "") or key
    if ADMIN_KEY and provided and secrets.compare_digest(provided, ADMIN_KEY):
        return True
    token = request.headers.get("X-Admin-Token", "")
    if token:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = await db.fetchrow(
            "SELECT s.expires_at, u.is_active FROM admin_sessions s "
            "JOIN admin_users u ON u.id = s.admin_user_id "
            "WHERE s.token_hash = $1 AND s.expires_at > NOW()",
            token_hash,
        )
        if row and row["is_active"]:
            return True
    return False

logger = logging.getLogger("wayforth")

router = APIRouter()

_CATALOG_SUGGESTED: dict = {
    "communication": ["Twilio", "Vonage", "Sinch", "MessageBird", "Plivo"],
    "payments":      ["Stripe", "PayPal", "Square", "Braintree", "Adyen"],
    "identity":      ["Auth0", "Okta", "Persona", "Jumio", "Onfido"],
    "inference":     ["OpenAI", "Anthropic", "Google Gemini", "Cohere", "Mistral"],
    "image":         ["Stability AI", "DALL-E", "Replicate", "fal.ai"],
    "audio":         ["ElevenLabs", "Deepgram", "AssemblyAI", "PlayHT"],
    "translation":   ["DeepL", "Google Translate", "Azure Translator"],
    "data":          ["SerpAPI", "Browserless", "Apify", "ScraperAPI"],
    "code":          ["GitHub Copilot API", "Tabnine", "Codeium"],
    "embeddings":    ["OpenAI Embeddings", "Cohere Embed", "Voyage AI"],
    "location":      ["Google Maps", "Mapbox", "HERE", "TomTom"],
    "devops":        ["GitHub Actions", "CircleCI", "Datadog", "PagerDuty"],
    "legal":         ["LexisNexis", "Westlaw", "Clio", "ContractPodAi"],
    "healthcare":    ["Redox", "Veeva", "Epic FHIR", "Healthix"],
    "real_estate":   ["ATTOM Data", "CoreLogic", "Estated", "Regrid"],
    "social":        ["Twitter/X API", "Meta Graph API", "LinkedIn API", "Reddit API"],
    "analytics":     ["Mixpanel", "Amplitude", "Segment", "PostHog"],
    "productivity":  ["Notion API", "Airtable API", "Zapier", "Make"],
}


@router.get("/admin/health")
@limiter.limit("5/minute")
async def admin_health(request: Request, key: str = "", db=Depends(get_db)):
    if not await _admin_ok(request, db, key):
        raise HTTPException(status_code=401, detail="Unauthorized")

    checks = {}

    try:
        await db.fetchval("SELECT 1")
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"

    for table in ["services", "search_analytics", "search_outcomes",
                  "agent_identities", "api_keys", "service_score_history"]:
        try:
            count = await db.fetchval(f"SELECT COUNT(*) FROM {table}")
            checks[table] = count
        except Exception:
            checks[table] = "error"

    recent = await db.fetchval("""
        SELECT COUNT(*) FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '1 hour'
    """)
    checks["searches_last_hour"] = recent

    return {
        "status": "operational" if checks["database"] == "ok" else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


@router.get("/admin/services")
@limiter.limit("10/minute")
async def admin_services(request: Request, key: str = "", db=Depends(get_db)):
    if not await _admin_ok(request, db, key):
        raise HTTPException(status_code=401, detail="Unauthorized")
    rows = await db.fetch("""
        SELECT
            category,
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE coverage_tier >= 2 AND consecutive_failures < 3) as tier2,
            COUNT(*) FILTER (WHERE coverage_tier >= 1) as tier1,
            COUNT(*) FILTER (
                WHERE endpoint_url NOT ILIKE '%github.com%'
                AND endpoint_url NOT ILIKE '%glama.ai%'
                AND endpoint_url NOT ILIKE '%smithery%'
            ) as real_apis
        FROM services
        GROUP BY category
        ORDER BY total DESC
    """)
    return {
        "by_category": [dict(r) for r in rows],
        "summary": {
            "total": sum(r['total'] for r in rows),
            "real_apis": sum(r['real_apis'] for r in rows),
            "tier2": sum(r['tier2'] for r in rows),
        }
    }


@router.get("/admin/catalog/misses")
@limiter.limit("10/minute")
async def catalog_misses(request: Request, key: str = "", db=Depends(get_db)):
    from main import ADMIN_KEY
    provided_key = request.headers.get("X-Admin-Key", "") or key
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        total = await db.fetchval("""
            SELECT COUNT(*) FROM search_analytics
            WHERE created_at > NOW() - INTERVAL '30 days'
        """)
        zero_results = await db.fetchval("""
            SELECT COUNT(*) FROM search_analytics
            WHERE created_at > NOW() - INTERVAL '30 days'
              AND result_count = 0
        """)
        top_misses_rows = await db.fetch("""
            SELECT query, COUNT(*) AS count, MAX(created_at) AS last_searched
            FROM search_analytics
            WHERE created_at > NOW() - INTERVAL '30 days'
              AND (
                result_count = 0
                OR (results IS NOT NULL
                    AND jsonb_array_length(results) > 0
                    AND (results->0->>'score')::float < 40)
              )
            GROUP BY query
            ORDER BY count DESC
            LIMIT 20
        """)
        low_conf_total = await db.fetchval("""
            SELECT COUNT(*) FROM search_analytics
            WHERE created_at > NOW() - INTERVAL '30 days'
              AND result_count > 0
              AND results IS NOT NULL
              AND jsonb_array_length(results) > 0
              AND (results->0->>'score')::float < 40
        """)
        cat_rows = await db.fetch("""
            SELECT s.category, COUNT(*) AS cnt
            FROM search_analytics sa
            JOIN services s ON s.id = sa.top_result_id
            WHERE sa.created_at > NOW() - INTERVAL '30 days'
              AND sa.result_count > 0
              AND sa.results IS NOT NULL
              AND jsonb_array_length(sa.results) > 0
              AND (sa.results->0->>'score')::float < 40
            GROUP BY s.category
            ORDER BY cnt DESC
        """)
        total_misses = (zero_results or 0) + (low_conf_total or 0)
        miss_rate = round(total_misses / total * 100, 1) if total else 0.0
        return {
            "period_days": 30,
            "total_searches": total or 0,
            "zero_result_searches": zero_results or 0,
            "miss_rate_pct": miss_rate,
            "top_misses": [
                {
                    "query": r["query"],
                    "count": r["count"],
                    "last_searched": r["last_searched"].isoformat() + "Z" if r["last_searched"] else None,
                }
                for r in top_misses_rows
            ],
            "miss_by_category": {r["category"]: r["cnt"] for r in cat_rows if r["category"]},
        }
    except Exception as e:
        logger.error(f"catalog_misses error: {e}")
        raise HTTPException(status_code=500, detail="query failed")


@router.get("/admin/catalog/gaps")
@limiter.limit("10/minute")
async def catalog_gaps(request: Request, key: str = "", db=Depends(get_db)):
    from main import ADMIN_KEY
    provided_key = request.headers.get("X-Admin-Key", "") or key
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        svc_rows = await db.fetch("""
            SELECT category, COUNT(*) AS svc_count
            FROM services
            WHERE category IS NOT NULL
            GROUP BY category
        """)
        search_rows = await db.fetch("""
            SELECT s.category, COUNT(*) AS search_count
            FROM search_analytics sa
            JOIN services s ON s.id = sa.top_result_id
            WHERE sa.created_at > NOW() - INTERVAL '7 days'
              AND s.category IS NOT NULL
            GROUP BY s.category
        """)
        svc_map = {r["category"]: r["svc_count"] for r in svc_rows}
        search_map = {r["category"]: r["search_count"] for r in search_rows}
        gaps = []
        for cat, searches in search_map.items():
            svc_count = svc_map.get(cat, 0)
            if svc_count == 0:
                continue
            ratio = round(searches / svc_count, 1)
            if ratio > 10:
                gaps.append({
                    "category": cat,
                    "searches_7d": searches,
                    "services_available": svc_count,
                    "searches_per_service": ratio,
                    "suggested_services": _CATALOG_SUGGESTED.get(cat, [])[:3],
                })
        gaps.sort(key=lambda x: x["searches_per_service"], reverse=True)
        return {"gaps": gaps}
    except Exception as e:
        logger.error(f"catalog_gaps error: {e}")
        raise HTTPException(status_code=500, detail="query failed")


@router.get("/admin/subscriptions/debug", tags=["Admin"])
@limiter.limit("10/minute")
async def admin_subscriptions_debug(request: Request, db=Depends(get_db)):
    """Show all api_keys rows where subscription_status='active', with stripe_subscription_id."""
    from main import ADMIN_KEY
    provided_key = request.headers.get("X-Admin-Key", "") or request.query_params.get("key", "")
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    rows = await db.fetch("""
        SELECT owner_email, subscription_status, stripe_subscription_id, tier, created_at::date AS joined
        FROM api_keys
        WHERE subscription_status = 'active'
        ORDER BY created_at DESC
    """)
    return {
        "total": len(rows),
        "rows": [dict(r) for r in rows],
    }


@router.get("/admin/revenue", tags=["Admin"])
@limiter.limit("20/minute")
async def admin_revenue(request: Request, db=Depends(get_db)):
    """Revenue summary: credits sold, MRR, top users by spend."""
    from main import ADMIN_KEY
    provided_key = request.headers.get("X-Admin-Key", "") or request.query_params.get("key", "")
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    total_credits_sold = await db.fetchval(
        "SELECT COALESCE(SUM(credits_total), 0) FROM package_purchases WHERE payment_status = 'completed'"
    ) or 0
    total_revenue_usd = await db.fetchval(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM package_purchases WHERE payment_status = 'completed'"
    ) or 0.0

    credits_used_30d = await db.fetchval(
        "SELECT COALESCE(SUM(ABS(amount)), 0) FROM credit_transactions "
        "WHERE amount < 0 AND created_at > NOW() - INTERVAL '30 days'"
    ) or 0

    active_subs = await db.fetchval(
        "SELECT COUNT(*) FROM api_keys WHERE subscription_status = 'active' AND stripe_subscription_id IS NOT NULL"
    ) or 0
    past_due_subs = await db.fetchval(
        "SELECT COUNT(*) FROM api_keys WHERE subscription_status = 'past_due' AND stripe_subscription_id IS NOT NULL"
    ) or 0

    mrr_30d = await db.fetchval(
        "SELECT COALESCE(SUM(amount_usd), 0) FROM package_purchases "
        "WHERE payment_status = 'completed' AND purchased_at > NOW() - INTERVAL '30 days'"
    ) or 0.0

    top_users = await db.fetch("""
        SELECT u.email, COALESCE(SUM(pp.amount_usd), 0) AS total_spent,
               COUNT(pp.id) AS purchases
        FROM package_purchases pp
        JOIN users u ON u.id = pp.user_id
        WHERE pp.payment_status = 'completed'
        GROUP BY u.email
        ORDER BY total_spent DESC
        LIMIT 10
    """)

    purchases_by_package = await db.fetch("""
        SELECT package_name, COUNT(*) AS count,
               SUM(credits_total) AS credits, SUM(amount_usd) AS revenue
        FROM package_purchases
        WHERE payment_status = 'completed'
        GROUP BY package_name
        ORDER BY revenue DESC
    """)

    return {
        "total_credits_sold": int(total_credits_sold),
        "total_revenue_usd": round(float(total_revenue_usd), 2),
        "credits_used_30d": int(credits_used_30d),
        "mrr_30d_usd": round(float(mrr_30d), 2),
        "active_subscriptions": int(active_subs),
        "past_due_subscriptions": int(past_due_subs),
        "top_users": [
            {"email": r["email"], "total_spent_usd": round(float(r["total_spent"]), 2), "purchases": r["purchases"]}
            for r in top_users
        ],
        "by_package": [
            {
                "package": r["package_name"],
                "count": r["count"],
                "credits_sold": int(r["credits"] or 0),
                "revenue_usd": round(float(r["revenue"] or 0), 2),
            }
            for r in purchases_by_package
        ],
    }


@router.get("/admin/signals", tags=["Admin"])
@limiter.limit("20/minute")
async def admin_signals(request: Request, db=Depends(get_db)):
    """Search signal analytics: per-service conversion rates and WRI v2 scores."""
    from main import ADMIN_KEY
    provided_key = request.headers.get("X-Admin-Key", "") or request.query_params.get("key", "")
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    total_searches = await db.fetchval("SELECT COUNT(*) FROM search_analytics") or 0
    total_clicks = await db.fetchval(
        "SELECT COUNT(*) FROM search_analytics WHERE clicked_slug IS NOT NULL"
    ) or 0
    total_payments = await db.fetchval(
        "SELECT COUNT(*) FROM search_analytics WHERE payment_followed = true"
    ) or 0

    per_service = await db.fetch("""
        SELECT
            clicked_slug,
            COUNT(*) AS clicks,
            SUM(CASE WHEN payment_followed THEN 1 ELSE 0 END) AS payments,
            ROUND(
                SUM(CASE WHEN payment_followed THEN 1 ELSE 0 END)::numeric
                / NULLIF(COUNT(*), 0) * 100, 1
            ) AS conversion_pct,
            MAX(created_at) AS last_seen
        FROM search_analytics
        WHERE clicked_slug IS NOT NULL
        GROUP BY clicked_slug
        ORDER BY payments DESC
        LIMIT 50
    """)

    # Enrich with stored WRI v2 scores
    enriched = []
    for row in per_service:
        slug = row["clicked_slug"]
        svc = await db.fetchrow(
            "SELECT wri_score, wri_version FROM services "
            "WHERE LOWER(name) = $1 OR LOWER(REPLACE(name, ' ', '_')) = $1 LIMIT 1",
            slug.lower()
        )
        enriched.append({
            "service": slug,
            "clicks": row["clicks"],
            "payments": row["payments"],
            "conversion_pct": float(row["conversion_pct"] or 0),
            "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
            "wri_v2": round(float(svc["wri_score"]), 1) if svc and svc["wri_score"] is not None else None,
            "wri_version": svc["wri_version"] if svc else None,
        })

    global_conversion = round(total_payments * 100.0 / max(total_clicks, 1), 2)

    return {
        "total_searches": int(total_searches),
        "total_clicks": int(total_clicks),
        "total_payment_conversions": int(total_payments),
        "global_conversion_pct": global_conversion,
        "per_service": enriched,
    }


@router.get("/admin/api-health", tags=["Admin"])
@limiter.limit("20/minute")
async def admin_api_health(request: Request, db=Depends(get_db)):
    """Managed service health: last tested, success rate, avg response time."""
    from main import ADMIN_KEY
    provided_key = request.headers.get("X-Admin-Key", "") or request.query_params.get("key", "")
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Execution stats per managed service from credit_transactions
    exec_rows = await db.fetch("""
        SELECT
            service_id,
            COUNT(*) AS total_calls,
            COUNT(*) FILTER (WHERE created_at > NOW() - INTERVAL '7 days') AS calls_7d,
            MIN(created_at) AS first_call,
            MAX(created_at) AS last_call
        FROM credit_transactions
        WHERE type = 'execution' AND service_id IS NOT NULL
        GROUP BY service_id
        ORDER BY calls_7d DESC
    """)

    # Service catalog health
    catalog_rows = await db.fetch("""
        SELECT name, last_tested_at, consecutive_failures, coverage_tier, x402_supported
        FROM services
        WHERE name ILIKE ANY(ARRAY[
            '%groq%','%deepl%','%openweather%','%newsapi%',
            '%resend%','%serper%','%assemblyai%','%stability%',
            '%tavily%','%jina%','%alphavantage%','%elevenlabs%'
        ])
        ORDER BY name
    """)

    managed_slugs = list(SERVICE_CONFIGS.keys())
    health_map = {slug: {"slug": slug, "configured": bool(os.environ.get(SERVICE_CONFIGS[slug]["key_var"]))} for slug in managed_slugs}

    for row in exec_rows:
        slug = (row["service_id"] or "").lower()
        if slug in health_map:
            health_map[slug].update({
                "total_calls": row["total_calls"],
                "calls_7d": row["calls_7d"],
                "first_call": row["first_call"].isoformat() if row["first_call"] else None,
                "last_call": row["last_call"].isoformat() if row["last_call"] else None,
            })

    for row in catalog_rows:
        name_lower = row["name"].lower().replace(" ", "").replace("-", "")
        for slug in managed_slugs:
            if slug.replace("_", "") in name_lower or name_lower.startswith(slug):
                health_map[slug].update({
                    "last_tested_at": row["last_tested_at"].isoformat() if row["last_tested_at"] else None,
                    "consecutive_failures": row["consecutive_failures"],
                    "coverage_tier": row["coverage_tier"],
                })
                break

    return {
        "managed_services": len(managed_slugs),
        "services": list(health_map.values()),
    }


@router.post("/admin/catalog/probe", tags=["Admin"])
async def catalog_probe(request: Request, db=Depends(get_db)):
    """Probe real service endpoints in a category and update tier/health."""
    from main import ADMIN_KEY
    provided_key = request.headers.get("X-Admin-Key", "")
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    category = body.get("category", "audio")

    # 1. Deactivate glama/github entries in this category
    deactivated_rows = await db.fetch("""
        UPDATE services
        SET consecutive_failures = 10, last_tested_at = NOW()
        WHERE category = $1
          AND (endpoint_url LIKE '%glama.ai%' OR endpoint_url LIKE '%github.com%')
          AND consecutive_failures < 10
        RETURNING name, endpoint_url
    """, category)
    deactivated = [{"name": r["name"], "endpoint_url": r["endpoint_url"]} for r in deactivated_rows]

    # 2. Fetch real candidates (tier < 2, healthy, no glama/github)
    candidates = await db.fetch("""
        SELECT id, name, endpoint_url, coverage_tier, consecutive_failures
        FROM services
        WHERE category = $1
          AND coverage_tier < 2
          AND consecutive_failures < 3
          AND endpoint_url NOT LIKE '%glama.ai%'
          AND endpoint_url NOT LIKE '%github.com%'
        ORDER BY name
    """, category)

    # 3. Probe each endpoint
    results = []
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                  headers={"User-Agent": "WayforthCrawler/1.0"}) as client:
        for svc in candidates:
            svc_id = svc["id"]
            url = svc["endpoint_url"]
            name = svc["name"]
            old_tier = svc["coverage_tier"]
            status_code = None
            outcome = None

            try:
                r = await client.get(url)
                status_code = r.status_code
                if status_code in (200, 401, 403):
                    outcome = "tier2"
                    await db.execute(
                        "UPDATE services SET coverage_tier = 2, consecutive_failures = 0, last_tested_at = NOW() WHERE id = $1",
                        svc_id)
                else:
                    outcome = "fail"
                    await db.execute(
                        "UPDATE services SET consecutive_failures = consecutive_failures + 1, last_tested_at = NOW() WHERE id = $1",
                        svc_id)
            except Exception:
                status_code = 0
                outcome = "timeout"
                await db.execute(
                    "UPDATE services SET consecutive_failures = 10, last_tested_at = NOW() WHERE id = $1",
                    svc_id)

            results.append({
                "name": name,
                "endpoint_url": url,
                "status_code": status_code,
                "outcome": outcome,
                "old_tier": old_tier,
                "new_tier": 2 if outcome == "tier2" else old_tier,
            })
            logger.info(f"probe [{category}] {name}: {status_code} → {outcome}")

    promoted = sum(1 for r in results if r["outcome"] == "tier2")
    failed = sum(1 for r in results if r["outcome"] in ("fail", "timeout"))

    return {
        "category": category,
        "probed": len(results),
        "promoted_to_tier2": promoted,
        "failed": failed,
        "deactivated_junk": len(deactivated),
        "deactivated": deactivated,
        "results": results,
    }


# ── Pentest account cleanup ───────────────────────────────────────────────────

_PENTEST_KEEP = [
    'dorassulin1@gmail.com', 'assulindor@gmail.com',
    'support@wayforth.io', 'demo_free@wayforth.io',
    'demo_starter@wayforth.io', 'demo_growth@wayforth.io',
    'demo_pro@wayforth.io', 'demo_provider@wayforth.io',
]

_PENTEST_CHILD_TABLES = [
    'agent_memory', 'agent_identities', 'x402_agent_identities',
    'x402_payment_receipts', 'usdc_payments', 'package_purchases',
    'wri_alerts', 'service_favorites', 'referrals', 'org_members',
    'user_service_keys', 'search_analytics', 'credit_transactions',
    'api_keys', 'user_credits',
]


@router.post("/admin/cleanup-pentest-accounts")
@limiter.limit("5/minute")
async def cleanup_pentest_accounts(request: Request, db=Depends(get_db)):
    """Delete all pentest artifact accounts. Admin-only, requires X-Admin-Token."""
    if not await _admin_ok(request, db):
        raise HTTPException(status_code=401, detail="Unauthorized")

    rows = await db.fetch(
        """
        SELECT id FROM users
        WHERE (
            email LIKE 'ratelimit-test-%'
            OR email LIKE 'audit-%@audit-research.io'
            OR email IN (
                'victim@example.com', 'some-other-email@example.com',
                'founders@wayforth.io', 'legal@wayforth.io',
                'info@wayforth.io', 'dev@wayforth.io',
                'team@wayforth.io', 'test@test.com'
            )
        )
        AND email != ALL($1::text[])
        """,
        _PENTEST_KEEP,
    )

    if not rows:
        return {"deleted_users": 0, "message": "No pentest accounts found"}

    ids = [r["id"] for r in rows]

    async with db.transaction():
        for tbl in _PENTEST_CHILD_TABLES:
            exists = await db.fetchval(
                "SELECT 1 FROM information_schema.columns"
                " WHERE table_name = $1 AND column_name = 'user_id'",
                tbl,
            )
            if exists:
                await db.execute(
                    f"DELETE FROM {tbl} WHERE user_id = ANY($1)",  # noqa: S608
                    ids,
                )
        await db.execute("DELETE FROM users WHERE id = ANY($1)", ids)

    logger.info(f"Admin cleanup: deleted {len(ids)} pentest accounts")
    return {"deleted_users": len(ids), "emails_deleted": len(ids)}
