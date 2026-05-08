"""routers/admin/__init__.py — assembles combined admin router."""

import logging
import secrets

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from core.rate_limit import limiter
from .rank import router as rank_router
from .services import router as services_router
from .dashboard import router as dashboard_router

logger = logging.getLogger("wayforth")

router = APIRouter()


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


@router.get("/admin")
async def admin_page(key: str = ""):
    from main import ADMIN_KEY
    if not ADMIN_KEY or not secrets.compare_digest(key, ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return FileResponse("static/admin.html")


router.include_router(rank_router)
router.include_router(services_router)
router.include_router(dashboard_router)
