import asyncio
import bcrypt
import hashlib
import json as json_lib
import logging
import os
import secrets
import uuid as uuid_lib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import sentry_sdk
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address  # fallback only
import stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

STRIPE_PACKAGES = {
    "starter": {"price_cents": 1900,  "credits": 20000,  "label": "Starter Pack"},
    "pro":     {"price_cents": 9900,  "credits": 120000, "label": "Pro Pack"},
    "growth":  {"price_cents": 29900, "credits": 400000, "label": "Growth Pack"},
}
from db import check_db
from notifications import send_submission_confirmation, send_tier3_application_notification, send_welcome_email
from ranker_client import rank_services

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        traces_sample_rate=0.1,
        environment=ENVIRONMENT,
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("wayforth")


async def log_query(pool, service_id: str, query_text: str, score: int):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO service_queries (service_id, query_text, score) VALUES ($1, $2, $3)",
                service_id, query_text[:200], score,
            )
    except Exception as e:
        logger.error(f"Query log error: {e}")


async def _record_search(pool, q, results, session_id="", query_id=""):
    try:
        async with pool.acquire() as conn:
            is_return = False
            if session_id:
                prev = await conn.fetchval("""
                    SELECT COUNT(*) FROM search_analytics
                    WHERE session_id = $1 AND created_at < NOW() - INTERVAL '1 hour'
                """, session_id)
                is_return = prev > 0

            await conn.execute("""
                INSERT INTO search_analytics
                (id, query, results, top_result_id, result_count, rank_scores, session_id, created_at)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, NOW())
            """,
                query_id or str(uuid_lib.uuid4()),
                q,
                json_lib.dumps([{"id": str(r.get("service_id", "")), "score": r.get("score", 0)} for r in results[:10]]),
                str(results[0].get("id", "")) if results else None,
                len(results),
                json_lib.dumps({str(r.get("service_id", "")): r.get("score", 0) for r in results[:10]}),
                session_id or None,
            )

            if is_return:
                logger.info(f"Return session: {session_id[:8]}")
    except Exception as e:
        logger.warning(f"search analytics write failed: {e}")


async def _record_payment(pool, service_id_hex: str, query_text=""):
    sid = service_id_hex.removeprefix("0x")
    try:
        async with pool.acquire() as conn:
            svc_uuid = await conn.fetchval(
                "SELECT id FROM services WHERE encode(sha256(endpoint_url::bytea), 'hex') = $1",
                sid,
            )
            if svc_uuid:
                await conn.execute("""
                    INSERT INTO search_outcomes
                    (query_text, service_id, outcome_type, created_at)
                    VALUES ($1, $2, 'payment_initiated', NOW())
                """, query_text, svc_uuid)
    except Exception as e:
        logger.warning(f"search outcome write failed: {e}")


async def _mark_search_converted(pool, query_id: str, service_id: str):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE search_analytics
                SET led_to_payment = TRUE, payment_service_id = $2::uuid
                WHERE id::text = $1
            """, query_id, service_id)
    except Exception as e:
        logger.warning(f"Failed to mark search converted: {e}")


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


async def _update_identity_payment(pool, agent_id: str, amount_usdc: float):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO agent_identities (agent_id, total_payments, total_spend_usdc, last_active_at, trust_score)
                VALUES ($1, 1, $2, NOW(), 55.0)
                ON CONFLICT (agent_id) DO UPDATE
                SET total_payments = agent_identities.total_payments + 1,
                    total_spend_usdc = agent_identities.total_spend_usdc + $2,
                    last_active_at = NOW(),
                    trust_score = LEAST(100, agent_identities.trust_score + 0.5)
            """, agent_id, amount_usdc)
    except Exception as e:
        logger.warning(f"Identity payment update failed: {e}")


async def _probe_new_service(service_id: str, endpoint_url: str):
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(endpoint_url)
            new_tier = 1 if r.status_code < 500 else 0
            async with app.state.pool.acquire() as db:
                await db.execute("""
                    UPDATE services
                    SET coverage_tier=$1, last_tested_at=NOW(), consecutive_failures=0
                    WHERE id=$2::uuid
                """, new_tier, service_id)
                logger.info(f"New service {service_id} probed: tier {new_tier} (status {r.status_code})")
    except Exception as e:
        logger.warning(f"New service probe failed for {service_id}: {e}")


_DB_URL = os.environ.get("DATABASE_URL", "")
_ASYNCPG_URL = _DB_URL.replace("postgresql+asyncpg://", "postgresql://")


async def _cleanup_anon_searches_loop(app: "FastAPI"):
    while True:
        await asyncio.sleep(3600)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stale = [k for k in list(app.state.anon_searches) if not k.endswith(f":{today}")]
        for k in stale:
            app.state.anon_searches.pop(k, None)
        if stale:
            logger.info(f"Cleaned {len(stale)} stale anon search entries")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Wayforth API starting, environment={ENVIRONMENT}")
    ok = check_db()
    if not ok:
        logger.warning("DB connection check failed — starting anyway")
    app.state.db_ok = ok
    app.state.anon_searches = {}
    try:
        app.state.pool = await asyncpg.create_pool(_ASYNCPG_URL, min_size=2, max_size=10)
        app.state.db_ok = True
    except Exception as e:
        logger.error(f"DB error: {e}")
        logger.warning(f"DB pool creation failed: {e} — /services will be unavailable")
        app.state.pool = None
    cleanup_task = asyncio.create_task(_cleanup_anon_searches_loop(app))
    yield
    cleanup_task.cancel()
    if app.state.pool:
        await app.state.pool.close()


app = FastAPI(
    title="Wayforth API",
    description="""
## The Search Engine for AI Agents

Wayforth provides semantic service discovery and credits-based payments for AI agents.

### Key Features
- **WayforthQL** — Declarative query language for agent service discovery
- **WayforthRank** — Proprietary multi-signal ranking engine
- **Coverage Tiers** — Automated reliability verification (0–3)
- **Non-custodial payments** — Agent signs, Wayforth routes, Base settles

### Authentication
Most endpoints are open with per-IP rate limits.
Add `X-Wayforth-API-Key: wf_free_...` header for higher limits.

### Quick Start
```bash
uvx wayforth-mcp
```
""",
    version="0.1.5",
    contact={"name": "Wayforth", "url": "https://wayforth.io/contact"},
    license_info={"name": "BSL 1.1", "url": "https://wayforth.io/license"},
    lifespan=lifespan,
)

def get_real_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_real_ip)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://wayforth.io",
        "https://www.wayforth.io",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
        "*",  # MCP server calls from any agent runtime
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    import uuid
    request_id = str(uuid.uuid4())
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if raw_key:
        request.state.api_key = raw_key
    response = await call_next(request)
    response.headers["X-Wayforth-Request-ID"] = request_id
    response.headers["X-Wayforth-Version"] = "0.1.5"
    response.headers["X-RateLimit-Tier"] = str(getattr(request.state, "rate_limit_tier", "free"))
    response.headers["X-RateLimit-Limit"] = str(getattr(request.state, "rate_limit_rpm", "10"))
    return response


async def get_db(request: Request):
    async with request.app.state.pool.acquire() as conn:
        yield conn


class _AuthError(Exception):
    def __init__(self, status_code: int, content: dict):
        self.status_code = status_code
        self.content = content


@app.exception_handler(_AuthError)
async def _auth_error_handler(request: Request, exc: _AuthError):
    return JSONResponse(status_code=exc.status_code, content=exc.content)


_ANON_DAILY_LIMIT = 3
_TIER_RPM = {"free": 10, "starter": 30, "pro": 100, "enterprise": 500}


async def check_auth(request: Request) -> dict:
    """Unified auth dependency for /search and /query.

    Authenticated (X-Wayforth-API-Key present):
      - Validates key, checks monthly quota, increments usage.
      - Returns authenticated=True with tier/key_id.

    Anonymous (no key):
      - Enforces 3 searches/IP/day via in-memory dict.
      - Returns authenticated=False with anonymous_count.
    """
    ip = get_real_ip(request)
    raw_key = request.headers.get("X-Wayforth-API-Key", "")

    if raw_key:
        pool = request.app.state.pool
        if not pool:
            raise HTTPException(status_code=503, detail="Database unavailable")
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        async with pool.acquire() as db:
            key = await db.fetchrow("""
                SELECT id, user_id, tier, rate_limit_per_minute, monthly_quota,
                       usage_this_month, quota_reset_at, active
                FROM api_keys WHERE key_hash = $1
            """, key_hash)

        if not key or not key["active"]:
            raise _AuthError(401, {
                "error": "invalid_key",
                "message": "Invalid API key. Get yours at wayforth.io/dashboard",
            })

        if key["monthly_quota"] > 0 and key["usage_this_month"] >= key["monthly_quota"]:
            raise _AuthError(429, {
                "error": "quota_exceeded",
                "message": "Monthly quota exceeded. Upgrade at wayforth.io/pricing",
                "upgrade_url": "https://wayforth.io/pricing",
            })

        async with pool.acquire() as db:
            await db.execute("""
                UPDATE api_keys SET usage_this_month = usage_this_month + 1,
                                    last_used_at = NOW()
                WHERE id = $1
            """, key["id"])

        rpm = _TIER_RPM.get(key["tier"], 10)
        request.state.rate_limit_tier = key["tier"]
        request.state.rate_limit_rpm = rpm
        return {
            "authenticated": True,
            "tier": key["tier"],
            "key_id": str(key["id"]),
            "user_id": str(key["user_id"]) if key["user_id"] else None,
            "usage_this_month": key["usage_this_month"] + 1,
            "monthly_quota": key["monthly_quota"],
            "anonymous_count": None,
            "ip": ip,
        }

    # Anonymous path
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    anon_key = f"{ip}:{today}"
    anon_dict = request.app.state.anon_searches
    count = anon_dict.get(anon_key, 0)

    if count >= _ANON_DAILY_LIMIT:
        raise _AuthError(429, {
            "error": "free_limit_reached",
            "message": "You've used your 3 free searches. Sign up free for 1,000 searches/month — no credit card required.",
            "signup_url": "https://wayforth.io/signup",
            "dashboard_url": "https://wayforth.io/dashboard",
        })

    anon_dict[anon_key] = count + 1
    request.state.rate_limit_tier = "anonymous"
    request.state.rate_limit_rpm = 10
    return {
        "authenticated": False,
        "tier": None,
        "key_id": None,
        "anonymous_count": count + 1,
        "ip": ip,
    }


@app.get("/health")
@limiter.limit("60/minute")
async def health(request: Request, db=Depends(get_db)):
    try:
        await db.fetchval("SELECT 1")
        db_status = "ok"
        tier2 = await db.fetchval("SELECT COUNT(*) FROM services WHERE coverage_tier >= 2") or 0
        total = await db.fetchval("SELECT COUNT(*) FROM services") or 0
    except Exception:
        db_status = "error"
        tier2 = 0
        total = 0
    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "service": "wayforth-api",
        "version": "0.1.5",
        "db_status": db_status,
        "catalog": {
            "total": total,
            "tier2": tier2,
        },
    }


@app.get("/status", tags=["System"])
async def system_status(db=Depends(get_db)):
    """Public system status — uptime, service count, last health check."""
    stats = await db.fetchrow("""
        SELECT
            COUNT(*) FILTER (WHERE coverage_tier >= 2) as tier2_services,
            COUNT(*) as total_services,
            COUNT(*) FILTER (WHERE coverage_tier >= 3) as tier3_services
        FROM services
    """)
    searches = await db.fetchval("""
        SELECT COUNT(*) FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '24h'
    """)
    return {
        "status": "operational",
        "version": "0.1.5",
        "services": {
            "total": stats["total_services"],
            "tier2": stats["tier2_services"],
            "tier3": stats["tier3_services"],
        },
        "searches_24h": searches,
        "api": "operational",
        "database": "operational",
        "billing": {
            "stripe": "active",
            "credits_per_dollar": 1000,
            "free_credits_on_signup": 1000,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/chain")
async def chain_stub():
    raise HTTPException(
        status_code=410,
        detail={"error": "endpoint_deprecated", "message": "Blockchain payment rail removed. See /billing for credits."}
    )


def compute_wri(service: dict, rank_score: float, popularity_boost: float = 0.0, payment_boost: float = 0.0) -> float:
    """WRI v2 — composite reliability score with popularity and payment signals. Range: 0-100."""
    score = rank_score * 0.5
    tier = service.get("coverage_tier", 0)
    if tier >= 2:
        score += 20
    elif tier >= 1:
        score += 5
    last_tested = service.get("last_tested_at")
    if last_tested:
        from datetime import datetime, timezone, timedelta
        try:
            if isinstance(last_tested, str):
                from dateutil.parser import parse
                last_tested = parse(last_tested)
            if last_tested.tzinfo is None:
                last_tested = last_tested.replace(tzinfo=timezone.utc)
            if last_tested > datetime.now(timezone.utc) - timedelta(hours=24):
                score += 10
        except Exception:
            pass
    if service.get("consecutive_failures", 1) == 0:
        score += 10
    score += min(popularity_boost, 5.0)
    score += min(payment_boost, 8.0)
    return round(min(score, 100), 1)


async def check_and_deduct_credits(db, user_id: str, cost: int, endpoint: str, service_id: str = None):
    """Atomically check and deduct credits. Returns (success, balance_after)."""
    async with db.transaction():
        row = await db.fetchrow(
            "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
            user_id
        )
        if not row:
            await db.execute("""
                INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier)
                VALUES ($1::uuid, 1000, 1000, 'free')
                ON CONFLICT (user_id) DO NOTHING
            """, user_id)
            row = await db.fetchrow(
                "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
                user_id
            )

        balance = row['credits_balance']
        if balance < cost:
            return False, balance

        new_balance = balance - cost
        await db.execute(
            "UPDATE user_credits SET credits_balance = $1, updated_at = NOW() WHERE user_id = $2::uuid",
            new_balance, user_id
        )
        await db.execute("""
            INSERT INTO credit_transactions
            (user_id, amount, balance_after, type, description, api_endpoint, service_id)
            VALUES ($1::uuid, $2, $3, 'usage', $4, $5, $6)
        """, user_id, -cost, new_balance, f"API call: {endpoint}", endpoint, service_id)

        return True, new_balance


@app.get(
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
    if auth.get("authenticated") and auth.get("user_id"):
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
                SELECT id, name, description, endpoint_url, category,
                       coverage_tier, pricing_usdc, source, payment_protocol, created_at,
                       last_tested_at, consecutive_failures
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
                    SELECT id, name, description, endpoint_url, category,
                           coverage_tier, pricing_usdc, source, payment_protocol,
                           last_tested_at, consecutive_failures
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
        asyncio.create_task(_record_search(pool, q, ranked, session_id, query_id))
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
            "description": s.get("description"),
            "score": s.get("score", 0),
            "wri": compute_wri(s, s.get("score", 0), popularity_boost=popular_ids.get(str(s.get("id")), 0.0), payment_boost=payment_ids.get(str(s.get("id")), 0.0)),
            "reason": s.get("reason", ""),
            "coverage_tier": s.get("coverage_tier"),
            "category": s.get("category"),
            "endpoint_url": s.get("endpoint_url"),
            "pricing": {
                "per_call_usd": s.get("pricing_usdc"),
                "credits_per_call": max(1, round((s.get("pricing_usdc") or 0.001) * 1000)),
            },
            "service_id": "0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest(),
            "wayforth_id": f"wayforth://{s.get('name','').lower().replace(' ','_').replace('/','_')[:30]}/{hashlib.sha256(s.get('endpoint_url','').encode()).hexdigest()[:8]}",
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
            response["message"] = f"{remaining} free {'search' if remaining == 1 else 'searches'} remaining. Sign up free for 1,000/month."
    return response


@app.get("/quickstart", include_in_schema=False)
async def quickstart():
    from fastapi.responses import HTMLResponse
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


@app.get("/search/suggestions")
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


@app.get("/search/popular")
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


@app.post("/query")
async def wayforthql(request: Request, body: WayforthQLQuery, auth: dict = Depends(check_auth), db=Depends(get_db)):
    """WayforthQL — declarative query language for agent service discovery."""
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

    where = " AND ".join(conditions)
    limit = min(body.limit or 5, 20)

    try:
        async with request.app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, name, description, endpoint_url, category,
                       pricing_usdc, coverage_tier, source, payment_protocol,
                       last_tested_at, consecutive_failures
                FROM services
                WHERE {where}
                ORDER BY coverage_tier DESC
                LIMIT {limit * 4}
                """,
                *params,
            )
    except Exception as e:
        logger.error(f"DB error in /query: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not rows:
        return {"query": body.query, "results": [], "total": 0, "protocol": "WayforthQL/1.0"}

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
        results_raw = [
            s for s in ranked[:limit * 2]
            if ("0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest()) not in exclude_set
        ][:limit]
    else:
        results_raw = ranked[:limit]

    results = []
    for s in results_raw:
        service_id = "0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest()
        name_slug = s.get("name", "").lower().replace(" ", "_").replace("/", "_")[:30]
        entry = {
            "name": s.get("name"),
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
        "protocol": "WayforthQL/1.0",
        "filters_applied": {
            "tier_min": body.tier_min,
            "price_max": body.price_max,
            "category": body.category,
            "protocol": body.protocol,
            "sort_by": body.sort_by,
            "exclude_ids": body.exclude_ids or [],
        },
    }
    if not auth["authenticated"]:
        remaining = _ANON_DAILY_LIMIT - auth["anonymous_count"]
        response["anonymous_searches_remaining"] = remaining
        if remaining > 0:
            response["signup_url"] = "https://wayforth.io/signup"
            response["message"] = f"{remaining} free {'search' if remaining == 1 else 'searches'} remaining. Sign up free for 1,000/month."
    return response


@app.get("/services")
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
                   pricing_usdc, coverage_tier, payment_protocol, source, created_at
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


@app.get("/services/search")
@limiter.limit("20/minute")
async def services_search_alias(request: Request, q: str = "", limit: int = 5, db=Depends(get_db)):
    """Alias for /search — same behavior."""
    return RedirectResponse(url=f"/search?q={q}&limit={limit}", status_code=307)


@app.get("/services/categories")
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


@app.get("/services/featured")
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


@app.get("/stats")
@limiter.limit("30/minute")
async def get_stats(request: Request, db=Depends(get_db)):
    try:
        row = await db.fetchrow("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE coverage_tier >= 2) as tier2,
                COUNT(*) FILTER (WHERE coverage_tier >= 3) as tier3,
                COUNT(*) FILTER (
                    WHERE endpoint_url NOT ILIKE '%github.com%'
                    AND endpoint_url NOT ILIKE '%glama.ai%'
                    AND endpoint_url NOT ILIKE '%smithery%'
                ) as real_apis,
                COUNT(DISTINCT category) as categories
            FROM services
        """)
        searches_7d = await db.fetchval("""
            SELECT COUNT(*) FROM search_analytics
            WHERE created_at > NOW() - INTERVAL '7 days'
        """)
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    return {
        "total_services": row["total"],
        "real_apis": row["real_apis"],
        "tier2_services": row["tier2"],
        "tier3_services": row["tier3"],
        "categories": row["categories"],
        "searches_7d": searches_7d,
        "mcp_tools": 9,
        "api_version": "0.1.5",
        "mcp_version": "0.1.8",
    }


@app.get("/services/count")
@limiter.limit("30/minute")
async def service_count(request: Request, db=Depends(get_db)):
    """Live service counts — use this to display accurate numbers on the website."""
    try:
        row = await db.fetchrow("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE coverage_tier >= 2) as tier2,
                COUNT(*) FILTER (WHERE coverage_tier >= 3) as tier3,
                COUNT(*) FILTER (
                    WHERE endpoint_url NOT ILIKE '%github.com%'
                    AND endpoint_url NOT ILIKE '%glama.ai%'
                    AND endpoint_url NOT ILIKE '%smithery%'
                ) as real_apis
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


@app.get("/health-report")
@limiter.limit("10/minute")
async def health_report(request: Request):
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


@app.get("/leaderboard")
@limiter.limit("20/minute")
async def leaderboard(request: Request, limit: int = 20, db=Depends(get_db)):
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


class PayRequest(BaseModel):
    service_id: str
    service_owner: str = ""
    amount_usd: float = 0.0
    query_id: str = ""
    agent_id: str = ""


class SubmitRequest(BaseModel):
    name: str
    description: str
    endpoint_url: str
    category: str
    price_per_call: float = 0.0
    contact_email: str | None = None


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


class WebhookRegistration(BaseModel):
    service_id: str
    webhook_url: str
    contact_email: str
    events: list[str] = ["tier_change", "health_alert"]


class AgentIdentityRequest(BaseModel):
    agent_id: str
    display_name: str = ""


@app.post("/pay")
@limiter.limit("20/minute")
async def pay_stub(request: Request, db=Depends(get_db)):
    raise HTTPException(
        status_code=410,
        detail={
            "error": "endpoint_deprecated",
            "message": "Direct blockchain payments removed. Use credits system instead.",
            "docs": "https://wayforth.io/docs",
            "dashboard": "https://wayforth.io/dashboard",
        }
    )


@app.post("/submit")
@limiter.limit("5/minute")
async def submit_service(request: Request, req: SubmitRequest):
    if not req.endpoint_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="endpoint_url must start with https://")
    if req.category not in ("inference", "data", "translation"):
        raise HTTPException(status_code=400, detail="category must be one of: inference, data, translation")
    if len(req.name) > 100:
        raise HTTPException(status_code=400, detail="name must be 100 characters or fewer")
    if len(req.description) > 500:
        raise HTTPException(status_code=400, detail="description must be 500 characters or fewer")
    if app.state.pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        async with app.state.pool.acquire() as conn:
            service_id = await conn.fetchval(
                """INSERT INTO services (name, description, endpoint_url, category, pricing_usdc, source, coverage_tier)
                   VALUES ($1, $2, $3, $4, $5, 'submitted', 0) RETURNING id""",
                req.name, req.description, req.endpoint_url, req.category, req.price_per_call,
            )
            await conn.execute(
                """INSERT INTO service_submissions (service_id, contact_email, ip_address)
                   VALUES ($1, $2, $3)""",
                service_id, req.contact_email, get_real_ip(request),
            )
        logger.info(f"submit name={req.name!r} category={req.category}")
        asyncio.create_task(_probe_new_service(str(service_id), req.endpoint_url))
        if req.contact_email:
            asyncio.create_task(asyncio.to_thread(
                send_submission_confirmation,
                req.contact_email, req.name, str(service_id), req.endpoint_url,
            ))
        await asyncio.sleep(3)
        async with app.state.pool.acquire() as conn2:
            service = await conn2.fetchrow("""
                SELECT coverage_tier, last_tested_at, consecutive_failures
                FROM services WHERE id = $1::uuid
            """, str(service_id))
        tier = service["coverage_tier"] if service else 0
        return {
            "status": "submitted",
            "service_id": str(service_id),
            "name": req.name,
            "initial_tier": tier,
            "message": f"Service submitted and probed. Current tier: {tier}. Tier 2 requires 90%+ uptime over 7 days.",
            "leaderboard_url": "https://wayforth.io/leaderboard",
            "tier3_url": "https://wayforth.io/tier3",
        }
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="A service with this endpoint URL already exists")
    except Exception as e:
        logger.error(f"Submit error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.get("/services/{service_id}")
@limiter.limit("30/minute")
async def get_service(request: Request, service_id: str):
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, description, endpoint_url, category,
                       coverage_tier, pricing_usdc, source, payment_protocol, created_at
                FROM services WHERE id = $1
                """,
                service_id,
            )
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    if row is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return dict(row)


@app.get("/admin/stats")
@limiter.limit("20/minute")
async def admin_stats(request: Request, key: str = ""):
    if not ADMIN_KEY or key != ADMIN_KEY:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        async with app.state.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM services")
            tier_rows = await conn.fetch(
                "SELECT coverage_tier, COUNT(*) AS cnt FROM services GROUP BY coverage_tier"
            )
            category_rows = await conn.fetch(
                "SELECT category, COUNT(*) AS cnt FROM services GROUP BY category"
            )
            queries_today = await conn.fetchval(
                "SELECT COUNT(*) FROM service_queries WHERE queried_at > NOW() - INTERVAL '1 day'"
            )
            queries_week = await conn.fetchval(
                "SELECT COUNT(*) FROM service_queries WHERE queried_at > NOW() - INTERVAL '7 days'"
            )
            top_rows = await conn.fetch(
                """
                SELECT s.name, s.category, s.coverage_tier, s.endpoint_url,
                       COUNT(q.id) AS query_count, ROUND(AVG(q.score), 1) AS avg_score
                FROM services s
                JOIN service_queries q ON s.id = q.service_id
                WHERE q.queried_at > NOW() - INTERVAL '7 days'
                GROUP BY s.id
                ORDER BY query_count DESC
                LIMIT 10
                """
            )
            sub_total = await conn.fetchval("SELECT COUNT(*) FROM service_submissions")
            sub_rows = await conn.fetch(
                """
                SELECT ss.contact_email, ss.submitted_at, ss.ip_address, s.name AS service_name
                FROM service_submissions ss
                JOIN services s ON ss.service_id = s.id
                ORDER BY ss.submitted_at DESC
                LIMIT 10
                """
            )
            platform = await conn.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '24h') as searches_24h,
                    (SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '7d') as searches_7d,
                    (SELECT COUNT(*) FROM search_outcomes WHERE outcome_type='payment_initiated') as total_payments,
                    (SELECT COUNT(*) FROM tier3_applications WHERE kyb_status='pending') as pending_tier3,
                    (SELECT COUNT(*) FROM api_keys WHERE active=TRUE) as active_api_keys,
                    (SELECT COUNT(*) FROM agent_identities) as registered_agents,
                    (SELECT COUNT(*) FROM provider_webhooks WHERE active=TRUE) as active_webhooks
            """)
    except Exception as e:
        logger.error(f"Admin stats DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    by_tier = {str(t): 0 for t in range(4)}
    for r in tier_rows:
        by_tier[str(r["coverage_tier"])] = r["cnt"]

    return {
        "services": {
            "total": total,
            "by_tier": by_tier,
            "by_category": {r["category"]: r["cnt"] for r in category_rows},
        },
        "queries": {
            "today": queries_today,
            "week": queries_week,
            "top_services": [
                {
                    "name": r["name"],
                    "query_count": r["query_count"],
                    "avg_score": r["avg_score"],
                    "category": r["category"],
                    "coverage_tier": r["coverage_tier"],
                    "endpoint_url": r["endpoint_url"],
                }
                for r in top_rows
            ],
        },
        "submissions": {
            "total": sub_total,
            "recent": [
                {
                    "service_name": r["service_name"],
                    "contact_email": r["contact_email"],
                    "submitted_at": r["submitted_at"].isoformat() + "Z",
                    "ip_address": r["ip_address"],
                }
                for r in sub_rows
            ],
        },
        "platform": {
            "active_api_keys": platform["active_api_keys"],
            "registered_agents": platform["registered_agents"],
            "active_webhooks": platform["active_webhooks"],
            "pending_tier3_applications": platform["pending_tier3"],
        },
        "usage": {
            "searches_24h": platform["searches_24h"],
            "searches_7d": platform["searches_7d"],
            "total_payments": platform["total_payments"],
        },
        "infrastructure": {
            "api": "healthy",
            "db": "healthy" if getattr(app.state, "db_ok", False) else "unavailable",
            "sentry": "connected" if SENTRY_DSN else "not configured",
        },
    }


@app.get("/admin/health")
@limiter.limit("5/minute")
async def admin_health(request: Request, key: str = "", db=Depends(get_db)):
    if key != ADMIN_KEY:
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


@app.get("/admin/services")
@limiter.limit("10/minute")
async def admin_services(request: Request, key: str = "", db=Depends(get_db)):
    if key != ADMIN_KEY:
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


@app.get("/admin")
async def admin_page(key: str = ""):
    if not ADMIN_KEY or key != ADMIN_KEY:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return FileResponse("static/admin.html")


@app.get("/demo")
async def demo():
    return FileResponse("static/demo.html")


@app.get("/leaderboard-page")
async def leaderboard_page():
    return FileResponse("static/leaderboard.html")


@app.get("/submit-page")
async def submit_page():
    return FileResponse("static/submit.html")


@app.get("/agent-demo")
async def agent_demo():
    return FileResponse("static/agent-demo.html")


@app.get("/wayforthql-spec", include_in_schema=False)
async def wayforthql_spec():
    return FileResponse("static/wayforthql.html")


@app.get("/roadmap", include_in_schema=False)
async def roadmap():
    return FileResponse("static/roadmap.html")


@app.get("/changelog", include_in_schema=False)
async def changelog_page():
    return FileResponse("static/changelog.html")


@app.get("/pricing", include_in_schema=False)
async def pricing_page():
    return FileResponse("static/pricing.html")


@app.get("/pricing/json")
@limiter.limit("30/minute")
async def pricing_json(request: Request):
    """Machine-readable pricing data."""
    return {
        "tiers": [
            {
                "name": "Free",
                "price_usd": 0,
                "price_monthly_usd": 0,
                "rate_limit_per_minute": 10,
                "monthly_quota": 1000,
                "features": ["search", "query", "services", "memory", "identity"],
                "cta": "Get Free Key",
                "cta_url": "https://gateway.wayforth.io/keys/create",
            },
            {
                "name": "Starter",
                "price_monthly_usd": 19,
                "rate_limit_per_minute": 30,
                "monthly_quota": 10000,
                "features": ["search", "query", "services", "memory", "identity", "intelligence", "webhooks"],
                "cta": "Contact Us",
                "cta_url": "https://wayforth.io/contact",
            },
            {
                "name": "Pro",
                "price_monthly_usd": 99,
                "rate_limit_per_minute": 100,
                "monthly_quota": 100000,
                "features": ["search", "query", "services", "memory", "identity", "intelligence", "webhooks", "history", "graph"],
                "cta": "Contact Us",
                "cta_url": "https://wayforth.io/contact",
            },
            {
                "name": "Enterprise",
                "price_monthly_usd": None,
                "rate_limit_per_minute": 500,
                "monthly_quota": -1,
                "features": ["everything", "sla", "private_catalog", "dedicated_infra", "custom_probing"],
                "cta": "Contact Us",
                "cta_url": "https://wayforth.io/contact",
            },
        ],
    }


@app.get("/intelligence-demo", include_in_schema=False)
async def intelligence_demo():
    return FileResponse("static/intelligence-demo.html")


@app.get("/health-page", include_in_schema=False)
async def health_page():
    return FileResponse("static/health-report.html")


@app.get("/analytics")
@limiter.limit("10/minute")
async def get_analytics(request: Request, key: str = ""):
    if key != ADMIN_KEY:
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


@app.get("/competitive")
@limiter.limit("10/minute")
async def competitive_intelligence_endpoint(request: Request, key: str = ""):
    """Admin: competitive intelligence and ecosystem growth signals."""
    if key != ADMIN_KEY:
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


@app.post("/memory")
@limiter.limit("30/minute")
async def save_memory(request: Request, body: MemoryItem):
    """Save a service to agent memory."""
    async with app.state.pool.acquire() as db:
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


@app.get("/memory")
@limiter.limit("30/minute")
async def get_memory(request: Request, agent_id: str = "anonymous", q: str = ""):
    """Retrieve agent's saved services."""
    async with app.state.pool.acquire() as db:
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


@app.post("/tier3/apply")
@limiter.limit("5/minute")
async def tier3_apply(request: Request, body: Tier3Application):
    """Apply for Tier 3 verification — KYB + SLA. Institutional-grade. Manual review required."""
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


@app.get("/tier3/status")
@limiter.limit("10/minute")
async def tier3_status(request: Request, email: str):
    """Check Tier 3 application status by email."""
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


@app.get("/tier3/admin")
@limiter.limit("10/minute")
async def tier3_admin(request: Request, key: str = "", status: str = "pending"):
    """Admin view of Tier 3 applications filtered by KYB status."""
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    async with app.state.pool.acquire() as db:
        apps = await db.fetch("""
            SELECT id, service_name, company_name, contact_email, endpoint_url,
                   monthly_volume_usdc, sla_uptime_target, kyb_status, created_at
            FROM tier3_applications WHERE kyb_status = $1
            ORDER BY created_at DESC
        """, status)
    return {
        "status_filter": status,
        "applications": [dict(a) for a in apps],
        "total": len(apps),
    }


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


@app.get("/graph/{service_id}")
@limiter.limit("20/minute")
async def get_service_graph(request: Request, service_id: str, limit: int = 10):
    """Return related services based on co-usage patterns."""
    async with app.state.pool.acquire() as db:
        return await _get_similar_services(db, service_id, limit)


@app.get("/services/similar/{service_id}")
@limiter.limit("30/minute")
async def similar_services(request: Request, service_id: str, limit: int = 5):
    """Public endpoint. Returns services commonly used alongside this one."""
    async with app.state.pool.acquire() as db:
        return await _get_similar_services(db, service_id, limit)


@app.get("/intelligence/{service_id}")
@limiter.limit("10/minute")
async def service_intelligence(request: Request, service_id: str, api_key: str = ""):
    """Wayforth Intelligence API — market data for service providers."""
    if not ADMIN_KEY or api_key != ADMIN_KEY:
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


@app.get("/services/{service_id}/wri")
@limiter.limit("30/minute")
async def service_wri(request: Request, service_id: str, db=Depends(get_db)):
    """Current WRI score and 7-day trend for a service."""
    async with app.state.pool.acquire() as conn:
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


@app.get("/services/{service_id}/history")
@limiter.limit("20/minute")
async def service_history(request: Request, service_id: str, days: int = Query(default=30, ge=1, le=90)):
    """WRI score trend for a service over time. Powers reliability trend visualization."""
    async with app.state.pool.acquire() as conn:
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


@app.post("/webhooks/register")
@limiter.limit("5/minute")
async def register_webhook(request: Request, body: WebhookRegistration):
    """Register a webhook to receive tier change or health alert events for a service."""
    if not body.webhook_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="webhook_url must use HTTPS")
    secret = secrets.token_hex(32)
    async with app.state.pool.acquire() as conn:
        wh_id = await conn.fetchval("""
            INSERT INTO provider_webhooks
            (service_id, webhook_url, contact_email, events, secret_token)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (service_id, webhook_url) DO UPDATE
            SET active = TRUE, updated_at = NOW()
            RETURNING id
        """, body.service_id, body.webhook_url, body.contact_email, body.events, secret)
    return {
        "webhook_id": str(wh_id),
        "secret_token": secret,
        "message": "Webhook registered. Store your secret_token — it won't be shown again.",
        "events": body.events,
    }


@app.delete("/webhooks/{webhook_id}")
@limiter.limit("10/minute")
async def delete_webhook(request: Request, webhook_id: str):
    """Deactivate a registered webhook."""
    async with app.state.pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE provider_webhooks SET active = FALSE WHERE id = $1::uuid", webhook_id
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {"webhook_id": webhook_id, "status": "deactivated"}


app.mount("/static", StaticFiles(directory="static"), name="static")


# ── API Key System ──────────────────────────────────────────────────────────

TIER_LIMITS = {
    "free":       {"rpm": 10,  "monthly": 1_000,    "fee_bps": 150},
    "starter":    {"rpm": 30,  "monthly": 10_000,   "fee_bps": 125},
    "pro":        {"rpm": 100, "monthly": 100_000,  "fee_bps": 100},
    "enterprise": {"rpm": 500, "monthly": -1,       "fee_bps": 75},
}

PACKAGES = {
    "starter": {"credits": 20000,  "price_usd": 19,  "label": "Starter Pack"},
    "pro":     {"credits": 120000, "price_usd": 99,  "label": "Pro Pack"},
    "growth":  {"credits": 400000, "price_usd": 299, "label": "Growth Pack"},
    "enterprise": {"credits": -1,  "price_usd": None, "label": "Enterprise"},
}

CREDIT_COSTS = {
    "search": 1,
    "query": 2,
    "intelligence": 5,
    "graph": 2,
    "wri_history": 1,
    "payment_routing": 100,  # per $1 routed
}


class ApiKeyRequest(BaseModel):
    email: str
    tier: str = "free"
    admin_key: str = ""  # Required to create non-free keys


async def get_api_key(request: Request, db=Depends(get_db)):
    """
    Optional API key auth. If provided, validates and tracks usage.
    If not provided, falls back to IP-based rate limiting.
    Returns tier info for the request.
    """
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        request.state.rate_limit_tier = "anonymous"
        request.state.rate_limit_rpm = 10
        return {"tier": "anonymous", "rpm": 10, "quota": None}

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key = await db.fetchrow("""
        SELECT id, tier, rate_limit_per_minute, monthly_quota, usage_this_month,
               quota_reset_at, active
        FROM api_keys WHERE key_hash = $1
    """, key_hash)

    if not key or not key["active"]:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    if key["monthly_quota"] > 0 and key["usage_this_month"] >= key["monthly_quota"]:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly quota of {key['monthly_quota']} requests exceeded. Resets {key['quota_reset_at'].strftime('%Y-%m-%d')}",
        )

    await db.execute("""
        UPDATE api_keys
        SET usage_this_month = usage_this_month + 1, last_used_at = NOW()
        WHERE id = $1
    """, key["id"])

    request.state.rate_limit_tier = key["tier"]
    request.state.rate_limit_rpm = key["rate_limit_per_minute"]
    return {"tier": key["tier"], "rpm": key["rate_limit_per_minute"], "key_id": str(key["id"])}


@app.get("/keys/tiers", tags=["Keys"])
async def key_tiers():
    return {
        "tiers": [
            {"tier": "free",       "price_monthly_usd": 0,    "rpm": 10,  "monthly_quota": 1000,   "features": ["search", "query", "services"]},
            {"tier": "starter",    "price_monthly_usd": 19,   "rpm": 30,  "monthly_quota": 10000,  "features": ["search", "query", "services", "intelligence", "webhooks"]},
            {"tier": "pro",        "price_monthly_usd": 99,   "rpm": 100, "monthly_quota": 100000, "features": ["search", "query", "services", "intelligence", "webhooks", "history", "graph"]},
            {"tier": "enterprise", "price_monthly_usd": None, "rpm": 500, "monthly_quota": -1,     "features": ["everything", "sla", "private_catalog", "dedicated_infra", "custom_probing"]},
        ],
    }


@app.post("/keys/create")
@limiter.limit("5/minute")
async def create_api_key(request: Request, body: ApiKeyRequest, db=Depends(get_db)):
    if body.tier != "free" and body.admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Admin key required for non-free tiers")

    if body.tier not in TIER_LIMITS:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Must be one of: {', '.join(TIER_LIMITS)}")

    existing = await db.fetchval("""
        SELECT COUNT(*) FROM api_keys WHERE owner_email = $1 AND active = TRUE
    """, body.email)
    if existing >= 3:
        raise HTTPException(status_code=429, detail="Maximum 3 active keys per email")

    raw_key = f"wf_{'live' if body.tier != 'free' else 'free'}_{secrets.token_hex(24)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]
    limits = TIER_LIMITS[body.tier]

    await db.execute("""
        INSERT INTO api_keys
        (key_hash, key_prefix, owner_email, tier, rate_limit_per_minute, monthly_quota)
        VALUES ($1, $2, $3, $4, $5, $6)
    """, key_hash, key_prefix, body.email, body.tier, limits["rpm"], limits["monthly"])

    if os.getenv("RESEND_API_KEY"):
        asyncio.create_task(asyncio.to_thread(
            send_welcome_email, body.email, key_prefix, body.tier
        ))

    return {
        "api_key": raw_key,
        "key_prefix": key_prefix,
        "tier": body.tier,
        "rate_limit_per_minute": limits["rpm"],
        "monthly_quota": limits["monthly"],
        "message": "Store this key securely — it will not be shown again.",
        "usage": f"Add header: X-Wayforth-API-Key: {raw_key}",
    }


@app.get("/keys/usage")
@limiter.limit("10/minute")
async def key_usage(request: Request, db=Depends(get_db)):
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        raise HTTPException(status_code=401, detail="X-Wayforth-API-Key header required")

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key = await db.fetchrow("""
        SELECT key_prefix, tier, rate_limit_per_minute, monthly_quota,
               usage_this_month, quota_reset_at, created_at, last_used_at
        FROM api_keys WHERE key_hash = $1 AND active = TRUE
    """, key_hash)

    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    quota_pct = (
        round(key["usage_this_month"] / key["monthly_quota"] * 100, 1)
        if key["monthly_quota"] > 0
        else 0
    )

    return {
        "key_prefix": key["key_prefix"],
        "tier": key["tier"],
        "rate_limit_per_minute": key["rate_limit_per_minute"],
        "monthly_quota": key["monthly_quota"],
        "usage_this_month": key["usage_this_month"],
        "quota_remaining": max(0, key["monthly_quota"] - key["usage_this_month"]),
        "quota_used_pct": quota_pct,
        "quota_resets_at": key["quota_reset_at"].isoformat(),
        "created_at": key["created_at"].isoformat(),
        "last_used_at": key["last_used_at"].isoformat() if key["last_used_at"] else None,
    }


@app.post("/identity/register")
@limiter.limit("10/minute")
async def register_identity(request: Request, body: AgentIdentityRequest, db=Depends(get_db)):
    """Register an agent identity. Idempotent — safe to call multiple times."""
    existing = await db.fetchrow("""
        SELECT id, trust_score, total_searches, total_payments
        FROM agent_identities WHERE agent_id = $1
    """, body.agent_id)

    if existing:
        return {
            "agent_id": body.agent_id,
            "status": "existing",
            "trust_score": existing["trust_score"],
            "total_searches": existing["total_searches"],
            "total_payments": existing["total_payments"],
            "message": "Identity already registered.",
        }

    await db.execute("""
        INSERT INTO agent_identities (agent_id, display_name, created_at, last_active_at)
        VALUES ($1, $2, NOW(), NOW())
    """, body.agent_id, body.display_name or body.agent_id[:12])

    return {
        "agent_id": body.agent_id,
        "status": "registered",
        "trust_score": 50.0,
        "message": "Identity registered. Trust score starts at 50 and improves with activity.",
    }


@app.get("/identity/{agent_id}")
@limiter.limit("30/minute")
async def get_identity(request: Request, agent_id: str, db=Depends(get_db)):
    """Get agent identity and reputation."""
    identity = await db.fetchrow("""
        SELECT agent_id, display_name, total_searches, total_payments,
               total_spend_usdc as total_spend_usd, trust_score, created_at, last_active_at
        FROM agent_identities WHERE agent_id = $1
    """, agent_id)

    if not identity:
        raise HTTPException(status_code=404, detail="Agent identity not found. Register at POST /identity/register")

    trust = identity["trust_score"]
    if trust >= 90:
        tier = "elite"
    elif trust >= 75:
        tier = "trusted"
    elif trust >= 60:
        tier = "established"
    elif trust >= 40:
        tier = "new"
    else:
        tier = "unknown"

    return {
        "agent_id": identity["agent_id"],
        "display_name": identity["display_name"],
        "trust_score": identity["trust_score"],
        "reputation_tier": tier,
        "total_searches": identity["total_searches"],
        "total_payments": identity["total_payments"],
        "total_spend_usd": identity["total_spend_usd"],
        "member_since": identity["created_at"].isoformat(),
        "last_active": identity["last_active_at"].isoformat(),
    }


@app.get("/identity/{agent_id}/history")
@limiter.limit("20/minute")
async def identity_history(request: Request, agent_id: str, db=Depends(get_db)):
    """Agent's search and payment history."""
    searches = await db.fetch("""
        SELECT query, top_result_id, created_at
        FROM search_analytics
        WHERE session_id = $1
        ORDER BY created_at DESC LIMIT 20
    """, agent_id)

    payments = await db.fetch("""
        SELECT service_id, outcome_type, created_at
        FROM search_outcomes
        WHERE session_id = $1
        ORDER BY created_at DESC LIMIT 20
    """, agent_id)

    return {
        "agent_id": agent_id,
        "recent_searches": [dict(r) for r in searches],
        "recent_payments": [dict(r) for r in payments],
    }


@app.post("/auth/register")
@limiter.limit("5/minute")
async def register_user(request: Request, db=Depends(get_db)):
    body = await request.json()
    email = body.get("email")
    supabase_id = body.get("supabase_id")

    if not email or not supabase_id:
        raise HTTPException(status_code=400, detail="email and supabase_id required")

    user = await db.fetchrow("""
        INSERT INTO users (email, supabase_id)
        VALUES ($1, $2)
        ON CONFLICT (email) DO UPDATE SET supabase_id = $2
        RETURNING id, email, created_at
    """, email, supabase_id)

    raw_key = "wf_live_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]

    await db.execute("""
        INSERT INTO api_keys (key_hash, key_prefix, tier, user_id, owner_email)
        VALUES ($1, $2, 'free', $3, $4)
        ON CONFLICT DO NOTHING
    """, key_hash, key_prefix, str(user['id']), email)

    await db.execute("""
        INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier)
        VALUES ($1, 1000, 1000, 'free')
        ON CONFLICT (user_id) DO NOTHING
    """, user['id'])

    await db.execute("""
        INSERT INTO credit_transactions
        (user_id, amount, balance_after, type, description)
        VALUES ($1, 1000, 1000, 'bonus', 'Free signup credits')
    """, user['id'])

    asyncio.create_task(asyncio.to_thread(
        send_welcome_email, email, key_prefix, 'free'
    ))

    return {
        "user_id": str(user['id']),
        "email": email,
        "api_key": raw_key,
        "tier": "free",
        "message": "Account created. Save your API key — it won't be shown again.",
    }


@app.get("/dashboard")
@limiter.limit("30/minute")
async def dashboard(request: Request, db=Depends(get_db)):
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    key = await db.fetchrow("""
        SELECT k.*, u.email, u.created_at as account_created,
               u.stripe_customer_id
        FROM api_keys k
        LEFT JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1
    """, key_hash)

    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    searches_this_month = await db.fetchval("""
        SELECT COUNT(*) FROM search_analytics
        WHERE created_at >= $1
        AND session_id ILIKE $2
    """, month_start, f"%{key['key_prefix']}%") or 0

    recent = await db.fetch("""
        SELECT query, created_at, top_result_id
        FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '7 days'
        ORDER BY created_at DESC LIMIT 10
    """)

    LIMITS = {
        'free':       {'rpm': 10,  'monthly': 1000,   'fee_pct': 1.5},
        'starter':    {'rpm': 30,  'monthly': 10000,  'fee_pct': 1.25},
        'pro':        {'rpm': 100, 'monthly': 100000, 'fee_pct': 1.0},
        'enterprise': {'rpm': 500, 'monthly': -1,     'fee_pct': 0.75},
    }
    tier = key['tier'] or 'free'
    limits = LIMITS.get(tier, LIMITS['free'])

    return {
        "account": {
            "email": key['email'],
            "tier": tier,
            "created_at": key['account_created'].isoformat() if key['account_created'] else None,
            "stripe_customer_id": key['stripe_customer_id'],
        },
        "api_key": {
            "prefix": key['key_prefix'],
            "created_at": key['created_at'].isoformat(),
            "subscription_status": key.get('subscription_status', 'active'),
            "current_period_end": key['current_period_end'].isoformat() if key.get('current_period_end') else None,
        },
        "usage": {
            "searches_this_month": searches_this_month,
            "monthly_limit": limits['monthly'],
            "pct_used": round((searches_this_month / limits['monthly'] * 100), 1) if limits['monthly'] > 0 else 0,
            "rate_limit_rpm": limits['rpm'],
        },
        "recent_searches": [
            {"query": r['query'], "at": r['created_at'].isoformat()}
            for r in recent
        ],
        "upgrade_url": "https://wayforth.io/pricing",
    }


@app.get("/billing/balance")
@limiter.limit("30/minute")
async def get_balance(request: Request, db=Depends(get_db)):
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow("""
        SELECT k.user_id, k.tier, u.email
        FROM api_keys k
        JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    credits = await db.fetchrow(
        "SELECT credits_balance, lifetime_credits, package_tier, payment_method FROM user_credits WHERE user_id = $1",
        key_record['user_id']
    )

    return {
        "credits_balance": credits['credits_balance'] if credits else 0,
        "lifetime_credits": credits['lifetime_credits'] if credits else 0,
        "package_tier": credits['package_tier'] if credits else 'free',
        "payment_method": credits['payment_method'] if credits else None,
        "email": key_record['email'],
    }


@app.get("/billing/packages")
async def get_packages(request: Request):
    result = []
    for key, pkg in PACKAGES.items():
        if pkg['price_usd'] is None:
            continue
        result.append({
            "id": key,
            "label": pkg['label'],
            "credits": pkg['credits'],
            "price_usd": pkg['price_usd'],
        })
    return {"packages": result}


@app.get("/billing/transactions")
@limiter.limit("20/minute")
async def get_transactions(request: Request, limit: int = 50, offset: int = 0, db=Depends(get_db)):
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)

    key_record = await db.fetchrow(
        "SELECT user_id FROM api_keys WHERE key_hash = $1 AND active = true",
        hashlib.sha256(api_key.encode()).hexdigest()
    )
    if not key_record:
        raise HTTPException(status_code=401)

    txs = await db.fetch("""
        SELECT id, amount, balance_after, type, description,
               api_endpoint, service_id, created_at
        FROM credit_transactions
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT $2 OFFSET $3
    """, key_record['user_id'], limit, offset)

    total = await db.fetchval(
        "SELECT COUNT(*) FROM credit_transactions WHERE user_id = $1",
        key_record['user_id']
    )

    return {
        "transactions": [dict(t) for t in txs],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/billing/purchases")
@limiter.limit("20/minute")
async def get_purchases(request: Request, db=Depends(get_db)):
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)

    key_record = await db.fetchrow(
        "SELECT user_id FROM api_keys WHERE key_hash = $1 AND active = true",
        hashlib.sha256(api_key.encode()).hexdigest()
    )
    if not key_record:
        raise HTTPException(status_code=401)

    purchases = await db.fetch("""
        SELECT id, package_name, credits_total, payment_method,
               payment_status, amount_usd, tx_hash, purchased_at
        FROM package_purchases
        WHERE user_id = $1
        ORDER BY purchased_at DESC
    """, key_record['user_id'])

    return {"purchases": [dict(p) for p in purchases]}


@app.post("/billing/deduct")
@limiter.limit("60/minute")
async def deduct_credits(request: Request, db=Depends(get_db)):
    """Deduct credits for a service payment. Called by wayforth_pay() MCP tool."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)

    key_record = await db.fetchrow(
        "SELECT user_id FROM api_keys WHERE key_hash = $1 AND active = true",
        hashlib.sha256(api_key.encode()).hexdigest()
    )
    if not key_record:
        raise HTTPException(status_code=401)

    body = await request.json()
    service_id = body.get("service_id", "unknown")
    amount_usd = float(body.get("amount_usd", 0.001))
    credits_needed = max(1, round(amount_usd * 1000))

    success, balance_after = await check_and_deduct_credits(
        db,
        str(key_record['user_id']),
        credits_needed,
        "/billing/deduct",
        service_id
    )

    if not success:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_credits",
                "balance": balance_after,
                "required": credits_needed,
                "top_up_url": "https://wayforth.io/dashboard",
            }
        )

    return {
        "status": "ok",
        "credits_deducted": credits_needed,
        "credits_remaining": balance_after,
        "amount_usd": amount_usd,
        "service_id": service_id,
    }


@app.post("/billing/checkout")
@limiter.limit("10/minute")
async def create_checkout(request: Request, db=Depends(get_db)):
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)

    key_record = await db.fetchrow("""
        SELECT k.user_id, u.email
        FROM api_keys k JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401)

    body = await request.json()
    package = body.get("package", "starter")

    if package not in STRIPE_PACKAGES:
        raise HTTPException(status_code=400, detail="Invalid package")

    pkg = STRIPE_PACKAGES[package]

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": pkg["price_cents"],
                    "product_data": {
                        "name": f"Wayforth {pkg['label']}",
                        "description": f"{pkg['credits']:,} credits · 1 credit = $0.001",
                    },
                },
                "quantity": 1,
            }],
            mode="payment",
            success_url="https://wayforth.io/dashboard?purchase=success&package=" + package,
            cancel_url="https://wayforth.io/dashboard?purchase=cancelled",
            customer_email=key_record['email'],
            metadata={
                "user_id": str(key_record['user_id']),
                "package": package,
                "credits": str(pkg["credits"]),
            }
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

    await db.execute("""
        INSERT INTO package_purchases
        (user_id, package_name, credits_purchased, credits_total,
         amount_usd, payment_method, payment_status, stripe_payment_id)
        VALUES ($1, $2, $3, $3, $4, 'card', 'pending', $5)
    """, key_record['user_id'], package, pkg['credits'],
        pkg['price_cents'] / 100, session.id)

    return {
        "checkout_url": session.url,
        "session_id": session.id,
        "package": package,
        "credits": pkg["credits"],
        "price_usd": pkg["price_cents"] / 100,
    }


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request, db=Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception:
        raise HTTPException(status_code=400)

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {})
        user_id = meta.get("user_id")
        package = meta.get("package")
        credits = int(meta.get("credits", 0))
        session_id = session.get("id")

        if not all([user_id, package, credits]):
            return {"status": "missing_metadata"}

        already = await db.fetchval(
            "SELECT id FROM package_purchases WHERE stripe_payment_id = $1 AND payment_status = 'completed'",
            session_id
        )
        if already:
            return {"status": "already_processed"}

        async with db.transaction():
            await db.execute(
                "UPDATE package_purchases SET payment_status = 'completed' WHERE stripe_payment_id = $1",
                session_id
            )

            existing = await db.fetchrow(
                "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
                user_id
            )

            if existing:
                new_balance = existing['credits_balance'] + credits
                await db.execute("""
                    UPDATE user_credits
                    SET credits_balance = $1, lifetime_credits = lifetime_credits + $2,
                        package_tier = $3, payment_method = 'card', updated_at = NOW()
                    WHERE user_id = $4::uuid
                """, new_balance, credits, package, user_id)
            else:
                new_balance = credits
                await db.execute("""
                    INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                    VALUES ($1::uuid, $2, $2, $3, 'card')
                """, user_id, credits, package)

            await db.execute("""
                INSERT INTO credit_transactions
                (user_id, amount, balance_after, type, description)
                VALUES ($1::uuid, $2, $3, 'purchase', $4)
            """, user_id, credits, new_balance,
                f"Stripe purchase: {package} pack — {credits:,} credits added")

        return {"status": "credited", "credits_added": credits, "new_balance": new_balance}

    return {"status": "ignored"}


# ── ADMIN AUTH ───────────────────────────────────────────────────────────────

ADMIN_ROLES = {
    'ceo':        ['all'],
    'operations': ['catalog', 'health', 'tier3', 'webhooks'],
    'support':    ['users', 'keys', 'tier3'],
    'analytics':  ['analytics', 'searches', 'leaderboard'],
}


async def get_admin_session(request: Request, db):
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


@app.post("/admin-api/auth/login")
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


@app.post("/admin-api/auth/logout")
async def admin_logout(request: Request, db=Depends(get_db)):
    token = request.headers.get("X-Admin-Token", "")
    if token:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        await db.execute(
            "DELETE FROM admin_sessions WHERE token_hash = $1", token_hash
        )
    return {"status": "logged out"}


@app.get("/admin-api/auth/me")
async def admin_me(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    return {
        "id": session['admin_user_id'],
        "email": session['email'],
        "full_name": session['full_name'],
        "role": session['role'],
    }


# ── ADMIN TEAM MANAGEMENT (CEO only) ─────────────────────────────────────────

@app.get("/admin-api/team")
async def admin_team(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] != 'ceo':
        raise HTTPException(status_code=403, detail="CEO access required")

    members = await db.fetch("""
        SELECT id, email, full_name, role, is_active, last_login_at, created_at
        FROM admin_users ORDER BY created_at ASC
    """)
    return {"team": [dict(m) for m in members]}


@app.post("/admin-api/team/invite")
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
            session['admin_user_id'])
        return {"member": dict(member), "temp_password": temp_password}
    except Exception:
        raise HTTPException(status_code=400, detail="Email already exists")


@app.patch("/admin-api/team/{member_id}")
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


# ── ADMIN DASHBOARD DATA ──────────────────────────────────────────────────────

@app.get("/admin-api/overview")
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


@app.get("/admin-api/users")
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
               k.subscription_status
        FROM users u
        LEFT JOIN api_keys k ON k.user_id = u.id
        ORDER BY u.created_at DESC
        LIMIT $1 OFFSET $2
    """, limit, offset)

    total = await db.fetchval("SELECT COUNT(*) FROM users")

    return {
        "users": [dict(u) for u in users],
        "total": total,
        "limit": limit,
        "offset": offset
    }


@app.get("/admin-api/catalog")
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


@app.get("/admin-api/users/{user_id}")
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

    return {
        "user": dict(user),
        "recent_searches": [dict(s) for s in searches]
    }


@app.patch("/admin-api/users/{user_id}/tier")
async def admin_change_tier(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    body = await request.json()
    new_tier = body.get("tier")
    reason = body.get("reason", "Admin manual change")

    if new_tier not in ['free', 'starter', 'pro', 'enterprise']:
        raise HTTPException(status_code=400, detail="Invalid tier")

    QUOTAS = {'free': 1000, 'starter': 10000, 'pro': 100000, 'enterprise': -1}

    await db.execute("""
        UPDATE api_keys SET tier = $1, monthly_quota = $2
        WHERE user_id = $3::uuid
    """, new_tier, QUOTAS[new_tier], user_id)

    return {"status": "updated", "tier": new_tier, "changed_by": session['email'], "reason": reason}


@app.post("/admin-api/users/{user_id}/reset-usage")
async def admin_reset_usage(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    body = await request.json()
    reason = body.get("reason", "Admin reset")

    await db.execute("""
        UPDATE api_keys SET usage_this_month = 0, quota_reset_at = NOW()
        WHERE user_id = $1::uuid
    """, user_id)

    return {"status": "reset", "changed_by": session['email'], "reason": reason}


@app.post("/admin-api/users/{user_id}/add-credits")
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


@app.post("/admin-api/users/{user_id}/regenerate-key")
async def admin_regenerate_key(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    body = await request.json()
    reason = body.get("reason", "Admin revoked")

    import secrets, hashlib
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


@app.patch("/admin-api/users/{user_id}/suspend")
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


@app.patch("/admin-api/users/{user_id}/custom-quota")
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


@app.get("/admin-api/users/{user_id}/searches")
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
