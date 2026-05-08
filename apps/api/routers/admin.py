"""routers/admin.py — /admin/* and /admin-api/* routes."""

import asyncio
import bcrypt
import hashlib
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from core.credits import _dispatch_webhooks
from core.db import get_db
from core.rate_limit import limiter
from services.managed import SERVICE_CONFIGS, SERVICE_DISPLAY_NAMES
from routers.webhooks import _fire_wri_alerts

logger = logging.getLogger("wayforth")

router = APIRouter()

# ── Constants ─────────────────────────────────────────────────────────────────

ADMIN_ROLES = {
    'ceo':        ['all'],
    'operations': ['catalog', 'health', 'tier3', 'webhooks'],
    'support':    ['users', 'keys', 'tier3'],
    'analytics':  ['analytics', 'searches', 'leaderboard'],
}

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

# ── Admin session helper ──────────────────────────────────────────────────────

async def get_admin_session(request: Request, db):
    from main import ADMIN_KEY
    # X-Admin-Key grants full ceo-level access without a JWT session
    admin_key = request.headers.get("X-Admin-Key", "")
    if admin_key and ADMIN_KEY and secrets.compare_digest(admin_key, ADMIN_KEY):
        return {"role": "ceo", "email": "admin", "full_name": "Admin", "is_active": True,
                "admin_user_id": None}

    token = request.headers.get("X-Admin-Token", "")
    if not token:
        raise HTTPException(status_code=401, detail="Admin token required")

    token_hash = hashlib.sha256(token.encode()).hexdigest()

    session = await db.fetchrow("""
        SELECT s.*, u.email, u.role, u.full_name, u.is_active
        FROM admin_sessions s
        JOIN admin_users u ON u.id = s.admin_user_id
        WHERE s.token_hash = $1 AND s.expires_at > NOW()
    """, token_hash)

    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not session['is_active']:
        raise HTTPException(status_code=403, detail="Account deactivated")

    return dict(session)


# ── /admin/* routes ───────────────────────────────────────────────────────────

@router.get("/admin/stats")
@limiter.limit("20/minute")
async def admin_stats(request: Request, key: str = ""):
    from main import app, ADMIN_KEY
    admin_key_header = request.headers.get("X-Admin-Key", "")
    provided_key = admin_key_header or key
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        async with app.state.pool.acquire() as conn:
            # --- developers ---
            _probe_filter = """
                AND owner_email NOT LIKE '%@wayforth.test'
                AND owner_email NOT LIKE 'probe-%'
            """
            _probe_user_ids = """
                AND user_id NOT IN (
                    SELECT id FROM users
                    WHERE email LIKE '%@wayforth.test' OR email LIKE 'probe-%'
                )
            """
            total_accounts = await conn.fetchval(
                f"SELECT COUNT(*) FROM api_keys WHERE active=true {_probe_filter}"
            ) or 0
            accounts_with_searches = await conn.fetchval(
                f"SELECT COUNT(DISTINCT user_id) FROM credit_transactions WHERE api_endpoint='/search' {_probe_user_ids}"
            ) or 0
            accounts_with_executions = await conn.fetchval(
                f"SELECT COUNT(DISTINCT user_id) FROM credit_transactions WHERE type='execution' {_probe_user_ids}"
            ) or 0
            accounts_with_purchases = await conn.fetchval(
                f"SELECT COUNT(DISTINCT user_id) FROM package_purchases WHERE payment_status='completed' {_probe_user_ids}"
            ) or 0

            # --- searches ---
            searches_all = await conn.fetchval("SELECT COUNT(*) FROM search_analytics") or 0
            searches_7d = await conn.fetchval(
                "SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '7 days'"
            ) or 0
            searches_24h = await conn.fetchval(
                "SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '24 hours'"
            ) or 0
            top_query_rows = await conn.fetch(
                """
                SELECT query, COUNT(*) as count
                FROM search_analytics
                WHERE query IS NOT NULL AND query != ''
                GROUP BY query
                ORDER BY count DESC
                LIMIT 10
                """
            )

            # --- executions ---
            exec_all = await conn.fetchval(
                "SELECT COUNT(*) FROM credit_transactions WHERE type='execution'"
            ) or 0
            exec_7d = await conn.fetchval(
                "SELECT COUNT(*) FROM credit_transactions WHERE type='execution' AND created_at > NOW() - INTERVAL '7 days'"
            ) or 0
            exec_24h = await conn.fetchval(
                "SELECT COUNT(*) FROM credit_transactions WHERE type='execution' AND created_at > NOW() - INTERVAL '24 hours'"
            ) or 0
            top_svc_rows = await conn.fetch(
                """
                SELECT service_id as service, COUNT(*) as count
                FROM credit_transactions
                WHERE type='execution' AND service_id IS NOT NULL
                GROUP BY service_id
                ORDER BY count DESC
                LIMIT 10
                """
            )

            # --- payments ---
            total_credits_purchased = await conn.fetchval(
                "SELECT COALESCE(SUM(credits_total), 0) FROM package_purchases WHERE payment_status='completed'"
            ) or 0
            total_credits_used = await conn.fetchval(
                "SELECT COALESCE(SUM(ABS(amount)), 0) FROM credit_transactions WHERE amount < 0 AND type IN ('usage', 'execution')"
            ) or 0
            total_volume_usd = await conn.fetchval(
                "SELECT COALESCE(SUM(amount_usd), 0) FROM package_purchases WHERE payment_status='completed'"
            ) or 0
            track_a = await conn.fetchval(
                "SELECT COUNT(*) FROM search_outcomes WHERE payment_track='card'"
            ) or 0
            track_b = await conn.fetchval(
                "SELECT COUNT(*) FROM search_outcomes WHERE payment_track='crypto'"
            ) or 0
            track_c = await conn.fetchval(
                "SELECT COUNT(*) FROM search_outcomes WHERE payment_track='x402'"
            ) or 0

            # --- catalog ---
            total_services = await conn.fetchval("SELECT COUNT(*) FROM services WHERE consecutive_failures < 3") or 0
            tier2_count = await conn.fetchval(
                "SELECT COUNT(*) FROM services WHERE coverage_tier >= 2 AND consecutive_failures < 3"
            ) or 0
            x402_count = await conn.fetchval(
                "SELECT COUNT(*) FROM services WHERE x402_supported=true AND consecutive_failures < 3"
            ) or 0

    except Exception as e:
        logger.error(f"Admin stats DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    # --- pypi ---
    pypi_version = "unknown"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("https://pypi.org/pypi/wayforth-mcp/json")
            if r.status_code == 200:
                pypi_version = r.json()["info"]["version"]
    except Exception:
        pass

    return {
        "developers": {
            "total_accounts": total_accounts,
            "accounts_with_searches": accounts_with_searches,
            "accounts_with_executions": accounts_with_executions,
            "accounts_with_purchases": accounts_with_purchases,
        },
        "searches": {
            "all_time": searches_all,
            "last_7_days": searches_7d,
            "last_24h": searches_24h,
            "top_queries": [{"query": r["query"], "count": r["count"]} for r in top_query_rows],
        },
        "executions": {
            "all_time": exec_all,
            "last_7_days": exec_7d,
            "last_24h": exec_24h,
            "top_services": [{"service": r["service"], "count": r["count"]} for r in top_svc_rows],
        },
        "payments": {
            "total_credits_purchased": int(total_credits_purchased),
            "total_credits_used": int(total_credits_used),
            "total_payment_volume_usd": float(total_volume_usd),
            "track_a_payments": track_a,
            "track_b_payments": track_b,
            "track_c_payments": track_c,
        },
        "catalog": {
            "total_services": total_services,
            "tier2_verified": tier2_count,
            "x402_native": x402_count,
        },
        "pypi": {
            "package": "wayforth-mcp",
            "latest_version": pypi_version,
        },
    }


@router.get("/admin/health")
@limiter.limit("5/minute")
async def admin_health(request: Request, key: str = "", db=Depends(get_db)):
    from main import ADMIN_KEY
    if not ADMIN_KEY or not secrets.compare_digest(key, ADMIN_KEY):
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
    from main import ADMIN_KEY
    if not ADMIN_KEY or not secrets.compare_digest(key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    rows = await db.fetch("""
        SELECT
            category,
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE coverage_tier >= 2) as tier2,
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


@router.get("/admin")
async def admin_page(key: str = ""):
    from main import ADMIN_KEY
    if not ADMIN_KEY or not secrets.compare_digest(key, ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return FileResponse("static/admin.html")


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


@router.post("/admin/rank/recalculate", tags=["Admin"])
async def rank_recalculate(request: Request, db=Depends(get_db)):
    """Recompute WayforthRank v2 scores for all services with payment signal data."""
    from main import app, ADMIN_KEY
    provided_key = request.headers.get("X-Admin-Key", "")
    if not ADMIN_KEY or not secrets.compare_digest(provided_key, ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    from wayforth_rank_v2 import compute_wri_v2

    signal_rows = await db.fetch("""
        SELECT
            clicked_slug,
            COUNT(*) AS total_clicks,
            SUM(CASE WHEN payment_followed THEN 1 ELSE 0 END) AS payments,
            MAX(created_at) AS last_seen
        FROM search_analytics
        WHERE clicked_slug IS NOT NULL
        GROUP BY clicked_slug
    """)

    services = await db.fetch("SELECT id, name, category, wri_score FROM services")

    def _slug(name: str) -> str:
        return name.lower().replace(" ", "_").replace("-", "_").replace("/", "_")

    def _norm(name: str) -> str:
        import re as _re
        return _re.sub(r'[^a-z0-9]', '', name.lower())

    svc_map = {_slug(s["name"]): s for s in services}
    norm_map = {_norm(s["name"]): s for s in services}

    results = []
    unmatched = []
    for sig in signal_rows:
        key = sig["clicked_slug"].lower().replace("-", "_")
        # Exact slug match, then prefix match, then normalized match
        svc = svc_map.get(key)
        if not svc:
            for svc_key, s in svc_map.items():
                if svc_key.startswith(key + "_"):
                    svc = s
                    break
        if not svc:
            norm_key = _norm(sig["clicked_slug"])
            svc = norm_map.get(norm_key)
        if not svc:
            for norm_svc_key, s in norm_map.items():
                if norm_svc_key.startswith(norm_key):
                    svc = s
                    break
        if not svc:
            unmatched.append({
                "clicked_slug": sig["clicked_slug"],
                "total_clicks": int(sig["total_clicks"] or 0),
                "payments": int(sig["payments"] or 0),
                "tried_key": key,
            })
            continue

        hist = await db.fetchrow(
            "SELECT wri_score FROM service_score_history "
            "WHERE service_id = $1 ORDER BY recorded_at DESC LIMIT 1",
            str(svc["id"])
        )
        base_wri = float(hist["wri_score"]) if hist else 60.0

        payments = int(sig["payments"] or 0)
        total_clicks = int(sig["total_clicks"] or 0)
        new_wri = compute_wri_v2(base_wri, payments, total_clicks, sig["last_seen"])
        pay_rate = round(payments * 100.0 / max(total_clicks, 1), 1)
        old_wri = float(svc["wri_score"]) if svc["wri_score"] is not None else None

        await db.execute(
            "UPDATE services SET wri_score = $1, wri_version = 'v2' WHERE id = $2",
            new_wri, svc["id"]
        )
        results.append({
            "service": sig["clicked_slug"],
            "old_wri": old_wri,
            "new_wri": new_wri,
            "payment_rate": pay_rate,
            "total_signals": total_clicks,
            "category": svc.get("category") or "",
        })

    alerts_fired = await _fire_wri_alerts(app.state.pool, results)
    return {
        "updated": len(results),
        "scores": results,
        "alerts_fired": alerts_fired,
        "unmatched_slugs": unmatched,
    }


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


# ── /admin-api/* routes ───────────────────────────────────────────────────────

@router.post("/admin-api/auth/login")
@limiter.limit("10/minute")
async def admin_login(request: Request, db=Depends(get_db)):
    body = await request.json()
    email = body.get("email", "").lower().strip()
    password = body.get("password", "")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    user = await db.fetchrow(
        "SELECT * FROM admin_users WHERE email = $1 AND is_active = true", email
    )

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=12)

    await db.execute("""
        INSERT INTO admin_sessions (admin_user_id, token_hash, expires_at, ip_address)
        VALUES ($1, $2, $3, $4)
    """, user['id'], token_hash, expires_at,
        request.client.host if request.client else None)

    await db.execute(
        "UPDATE admin_users SET last_login_at = NOW() WHERE id = $1", user['id']
    )

    return {
        "token": raw_token,
        "expires_at": expires_at.isoformat(),
        "admin": {
            "id": str(user['id']),
            "email": user['email'],
            "full_name": user['full_name'],
            "role": user['role'],
        }
    }


@router.post("/admin-api/auth/logout")
async def admin_logout(request: Request, db=Depends(get_db)):
    token = request.headers.get("X-Admin-Token", "")
    if token:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        await db.execute(
            "DELETE FROM admin_sessions WHERE token_hash = $1", token_hash
        )
    return {"status": "logged out"}


@router.get("/admin-api/auth/me")
async def admin_me(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    return {
        "id": session.get('admin_user_id'),
        "email": session['email'],
        "full_name": session['full_name'],
        "role": session['role'],
    }


@router.get("/admin-api/team")
async def admin_team(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] != 'ceo':
        raise HTTPException(status_code=403, detail="CEO access required")

    members = await db.fetch("""
        SELECT id, email, full_name, role, is_active, last_login_at, created_at
        FROM admin_users ORDER BY created_at ASC
    """)
    return {"team": [dict(m) for m in members]}


@router.post("/admin-api/team/invite")
async def admin_invite(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] != 'ceo':
        raise HTTPException(status_code=403, detail="CEO access required")

    body = await request.json()
    email = body.get("email", "").lower().strip()
    full_name = body.get("full_name", "")
    role = body.get("role", "support")
    temp_password = body.get("password", "")

    if not all([email, full_name, role, temp_password]):
        raise HTTPException(status_code=400, detail="All fields required")
    if role not in ['support', 'operations', 'analytics', 'ceo']:
        raise HTTPException(status_code=400, detail="Invalid role")

    password_hash = bcrypt.hashpw(
        temp_password.encode(), bcrypt.gensalt()
    ).decode()

    try:
        member = await db.fetchrow("""
            INSERT INTO admin_users (email, password_hash, full_name, role, created_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, email, full_name, role, created_at
        """, email, password_hash, full_name, role,
            session.get('admin_user_id'))
        return {"member": dict(member), "temp_password": temp_password}
    except Exception:
        raise HTTPException(status_code=400, detail="Email already exists")


@router.patch("/admin-api/team/{member_id}")
async def admin_update_member(
    request: Request, member_id: str, db=Depends(get_db)
):
    session = await get_admin_session(request, db)
    if session['role'] != 'ceo':
        raise HTTPException(status_code=403, detail="CEO access required")

    body = await request.json()

    if 'is_active' in body:
        await db.execute(
            "UPDATE admin_users SET is_active=$1 WHERE id=$2",
            body['is_active'], member_id
        )
    if 'role' in body:
        await db.execute(
            "UPDATE admin_users SET role=$1 WHERE id=$2",
            body['role'], member_id
        )
    return {"status": "updated"}


@router.get("/admin-api/overview")
async def admin_overview(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)

    try:
        total_services = await db.fetchval("SELECT COUNT(*) FROM services") or 0
    except: total_services = 0

    try:
        tier2 = await db.fetchval("SELECT COUNT(*) FROM services WHERE coverage_tier >= 2") or 0
    except: tier2 = 0

    try:
        total_users = await db.fetchval("SELECT COUNT(*) FROM users") or 0
    except: total_users = 0

    try:
        total_keys = await db.fetchval("SELECT COUNT(*) FROM api_keys") or 0
    except: total_keys = 0

    try:
        searches_24h = await db.fetchval(
            "SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '24h'"
        ) or 0
    except: searches_24h = 0

    try:
        searches_7d = await db.fetchval(
            "SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '7 days'"
        ) or 0
    except: searches_7d = 0

    try:
        pending_tier3 = await db.fetchval(
            "SELECT COUNT(*) FROM tier3_applications WHERE kyb_status = 'pending'"
        ) or 0
    except: pending_tier3 = 0

    try:
        total_agents = await db.fetchval("SELECT COUNT(*) FROM agent_identities") or 0
    except: total_agents = 0

    try:
        daily = await db.fetch("""
            SELECT DATE(created_at) as date, COUNT(*) as count
            FROM search_analytics
            WHERE created_at > NOW() - INTERVAL '30 days'
            GROUP BY DATE(created_at)
            ORDER BY date ASC
        """)
    except: daily = []

    try:
        signups = await db.fetch("""
            SELECT DATE(created_at) as date, COUNT(*) as count
            FROM users
            WHERE created_at > NOW() - INTERVAL '30 days'
            GROUP BY DATE(created_at)
            ORDER BY date ASC
        """)
    except: signups = []

    return {
        "stats": {
            "total_services": total_services,
            "tier2": tier2,
            "total_users": total_users,
            "total_keys": total_keys,
            "searches_24h": searches_24h,
            "searches_7d": searches_7d,
            "pending_tier3": pending_tier3,
            "total_agents": total_agents,
        },
        "daily_searches": [{"date": str(r['date']), "count": r['count']} for r in daily],
        "daily_signups": [{"date": str(r['date']), "count": r['count']} for r in signups],
        "admin": {
            "email": session['email'],
            "role": session['role'],
            "full_name": session['full_name'],
        }
    }


@router.get("/admin-api/users")
async def admin_users_list(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    db=Depends(get_db)
):
    session = await get_admin_session(request, db)
    if session['role'] not in ['ceo', 'support']:
        raise HTTPException(status_code=403)

    users = await db.fetch("""
        SELECT u.id, u.email, u.created_at,
               k.tier, k.owner_email, k.key_prefix,
               k.usage_this_month, k.monthly_quota,
               k.subscription_status,
               uc.package_tier, uc.credits_balance, uc.lifetime_credits,
               GREATEST(MAX(s.created_at), MAX(ct.created_at)) as last_active
        FROM users u
        LEFT JOIN LATERAL (
            SELECT tier, owner_email, key_prefix, usage_this_month, monthly_quota, subscription_status
            FROM api_keys
            WHERE user_id = u.id AND active = true
            ORDER BY (encrypted_key IS NOT NULL) DESC, created_at DESC
            LIMIT 1
        ) k ON true
        LEFT JOIN user_credits uc ON uc.user_id = u.id
        LEFT JOIN search_analytics s ON s.user_id = u.id
        LEFT JOIN credit_transactions ct ON ct.user_id = u.id AND ct.type = 'execution'
        WHERE u.email NOT LIKE '%@wayforth.test'
          AND u.email NOT LIKE 'probe-%'
        GROUP BY u.id, u.email, u.created_at,
                 k.tier, k.owner_email, k.key_prefix,
                 k.usage_this_month, k.monthly_quota, k.subscription_status,
                 uc.package_tier, uc.credits_balance, uc.lifetime_credits
        ORDER BY last_active DESC NULLS LAST
        LIMIT $1 OFFSET $2
    """, limit, offset)

    total = await db.fetchval("""
        SELECT COUNT(*) FROM users
        WHERE email NOT LIKE '%@wayforth.test'
          AND email NOT LIKE 'probe-%'
    """)

    return {
        "users": [dict(u) for u in users],
        "total": total,
        "limit": limit,
        "offset": offset
    }


@router.get("/admin-api/catalog")
async def admin_catalog(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] not in ['ceo', 'operations']:
        raise HTTPException(status_code=403)

    rows = await db.fetch("""
        SELECT category,
               COUNT(*) as total,
               COUNT(*) FILTER (WHERE coverage_tier >= 2) as tier2,
               COUNT(*) FILTER (WHERE endpoint_url NOT ILIKE '%github%') as real_apis
        FROM services
        GROUP BY category ORDER BY total DESC
    """)

    recent_promotions = await db.fetch("""
        SELECT name, coverage_tier, last_tested_at
        FROM services
        WHERE coverage_tier >= 2
        ORDER BY last_tested_at DESC LIMIT 10
    """)

    return {
        "by_category": [dict(r) for r in rows],
        "recent_promotions": [dict(r) for r in recent_promotions]
    }


@router.get("/admin-api/users/{user_id}")
async def admin_get_user(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    user = await db.fetchrow("""
        SELECT u.id, u.email, u.created_at, u.stripe_customer_id,
               k.tier, k.key_prefix, k.usage_this_month, k.monthly_quota,
               k.subscription_status, k.stripe_subscription_id,
               k.created_at as key_created_at, k.last_used_at,
               COUNT(sa.id) as total_searches,
               MAX(sa.created_at) as last_search_at
        FROM users u
        LEFT JOIN api_keys k ON k.user_id = u.id
        LEFT JOIN search_analytics sa ON sa.session_id ILIKE '%' || k.key_prefix || '%'
        WHERE u.id = $1::uuid
        GROUP BY u.id, u.email, u.created_at, u.stripe_customer_id,
                 k.tier, k.key_prefix, k.usage_this_month, k.monthly_quota,
                 k.subscription_status, k.stripe_subscription_id,
                 k.created_at, k.last_used_at
    """, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    searches = await db.fetch("""
        SELECT query, created_at, top_result_id
        FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '30 days'
        ORDER BY created_at DESC LIMIT 10
    """)

    service_keys = await db.fetch("""
        SELECT service_slug, service_name, key_preview,
               total_calls, last_used_at, active, created_at
        FROM user_service_keys
        WHERE user_id=$1::uuid
        ORDER BY created_at DESC
    """, user_id)

    result = {
        "user": dict(user),
        "recent_searches": [dict(s) for s in searches],
        "service_keys": [dict(k) for k in service_keys],
    }
    return result


@router.patch("/admin-api/users/{user_id}/tier")
async def admin_change_tier(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    body = await request.json()
    new_tier = body.get("tier")
    reason = body.get("reason", "Admin manual change")

    VALID_TIERS = ['free', 'starter', 'pro', 'growth', 'enterprise']
    if new_tier not in VALID_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Valid: {VALID_TIERS}")

    QUOTAS   = {'free': 1000,  'starter': 10000,  'pro': 100000,   'growth': 500000,    'enterprise': -1}
    CREDITS  = {'free': 100,   'starter': 50000,  'pro': 300000,   'growth': 1000000,   'enterprise': 5000000}

    old_key = await db.fetchrow(
        "SELECT tier FROM api_keys WHERE user_id=$1::uuid AND active=true LIMIT 1", user_id
    )
    old_tier = old_key["tier"] if old_key else "free"

    new_credits = CREDITS[new_tier]

    async with db.transaction():
        await db.execute("""
            UPDATE api_keys SET tier = $1, monthly_quota = $2
            WHERE user_id = $3::uuid
        """, new_tier, QUOTAS[new_tier], user_id)

        existing = await db.fetchrow(
            "SELECT user_id FROM user_credits WHERE user_id = $1::uuid", user_id
        )
        if existing:
            await db.execute("""
                UPDATE user_credits
                SET credits_balance = $1, lifetime_credits = $1,
                    package_tier = $2, updated_at = NOW()
                WHERE user_id = $3::uuid
            """, new_credits, new_tier, user_id)
        else:
            await db.execute("""
                INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier)
                VALUES ($1::uuid, $2, $2, $3)
            """, user_id, new_credits, new_tier)

        await db.execute("""
            INSERT INTO credit_transactions (user_id, amount, balance_after, type, description)
            VALUES ($1::uuid, $2, $2, 'tier_change', $3)
        """, user_id, new_credits, f"Tier changed {old_tier} → {new_tier} by admin")

    asyncio.create_task(_dispatch_webhooks(
        user_id, "tier.changed", {
            "old_tier": old_tier,
            "new_tier": new_tier,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    ))

    return {
        "status": "updated",
        "tier": new_tier,
        "credits_reset_to": new_credits,
        "changed_by": session['email'],
        "reason": reason,
    }


@router.post("/admin-api/users/{user_id}/reset-usage")
async def admin_reset_usage(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    body = await request.json()
    reason = body.get("reason", "Admin reset")

    await db.execute("""
        UPDATE api_keys SET usage_this_month = 0, quota_reset_at = NOW()
        WHERE user_id = $1::uuid
    """, user_id)

    return {"status": "reset", "changed_by": session['email'], "reason": reason}


@router.post("/admin-api/users/{user_id}/add-credits")
async def admin_add_credits(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    body = await request.json()
    credits = int(body.get("credits", 0))
    reason = body.get("reason", "Admin grant")
    payment_method = body.get("payment_method", "admin")

    if credits <= 0 or credits > 1000000:
        raise HTTPException(status_code=400, detail="Credits must be 1-1,000,000")

    async with db.transaction():
        row = await db.fetchrow(
            "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
            user_id
        )
        if not row:
            await db.execute("""
                INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier)
                VALUES ($1::uuid, $2, $2, 'free')
            """, user_id, credits)
            new_balance = credits
        else:
            new_balance = row['credits_balance'] + credits
            await db.execute("""
                UPDATE user_credits
                SET credits_balance = $1, lifetime_credits = lifetime_credits + $2, updated_at = NOW()
                WHERE user_id = $3::uuid
            """, new_balance, credits, user_id)

        await db.execute("""
            INSERT INTO credit_transactions
            (user_id, amount, balance_after, type, description)
            VALUES ($1::uuid, $2, $3, 'admin_grant', $4)
        """, user_id, credits, new_balance, reason)

    return {
        "status": "credits_added",
        "credits_added": credits,
        "new_balance": new_balance,
        "granted_by": session['email'],
        "reason": reason,
    }


@router.post("/admin-api/users/{user_id}/regenerate-key")
async def admin_regenerate_key(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    body = await request.json()
    reason = body.get("reason", "Admin revoked")

    raw_key = "wf_live_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]

    await db.execute("""
        UPDATE api_keys SET key_hash = $1, key_prefix = $2, last_used_at = NULL
        WHERE user_id = $3::uuid
    """, key_hash, key_prefix, user_id)

    return {
        "status": "regenerated",
        "new_key": raw_key,
        "new_prefix": key_prefix,
        "changed_by": session['email'],
        "reason": reason,
        "warning": "Send this key to the user securely. It will not be shown again."
    }


@router.patch("/admin-api/users/{user_id}/suspend")
async def admin_suspend_user(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    body = await request.json()
    suspended = body.get("suspended", True)
    reason = body.get("reason", "")

    await db.execute("""
        UPDATE api_keys SET active = $1 WHERE user_id = $2::uuid
    """, not suspended, user_id)

    return {
        "status": "suspended" if suspended else "unsuspended",
        "changed_by": session['email'],
        "reason": reason
    }


@router.patch("/admin-api/users/{user_id}/custom-quota")
async def admin_custom_quota(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] not in ['ceo', 'operations']:
        raise HTTPException(status_code=403)
    body = await request.json()
    quota = int(body.get("quota", 0))
    reason = body.get("reason", "")

    await db.execute("""
        UPDATE api_keys SET monthly_quota = $1 WHERE user_id = $2::uuid
    """, quota, user_id)

    return {"status": "quota_set", "quota": quota, "changed_by": session['email'], "reason": reason}


@router.get("/admin-api/users/{user_id}/searches")
async def admin_user_searches(request: Request, user_id: str, limit: int = 50, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] not in ['ceo', 'support']:
        raise HTTPException(status_code=403)

    key = await db.fetchrow("SELECT key_prefix FROM api_keys WHERE user_id = $1::uuid", user_id)
    if not key:
        return {"searches": [], "total": 0}

    searches = await db.fetch("""
        SELECT query, created_at, top_result_id, led_to_payment
        FROM search_analytics
        ORDER BY created_at DESC LIMIT $1
    """, limit)

    return {
        "searches": [dict(s) for s in searches],
        "total": len(searches)
    }


@router.get("/admin-api/users/{user_id}/service-keys")
async def admin_get_user_service_keys(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] not in ['ceo', 'support']:
        raise HTTPException(status_code=403)
    keys = await db.fetch("""
        SELECT service_slug, service_name, key_preview,
               total_calls, last_used_at, active, created_at
        FROM user_service_keys
        WHERE user_id=$1::uuid
        ORDER BY created_at DESC
    """, user_id)
    return {"service_keys": [dict(k) for k in keys], "total": len(keys)}
