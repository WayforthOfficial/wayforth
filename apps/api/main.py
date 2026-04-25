import asyncio
import hashlib
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
import sentry_sdk
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address  # fallback only
from web3 import Web3

from chain import ESCROW_ADDRESS, PAYMENT_INFO, REGISTRY_ADDRESS, build_payment_calldata, get_chain_stats
from db import check_db
from notifications import send_submission_confirmation
from ranker import rank_services

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


_DB_URL = os.environ.get("DATABASE_URL", "postgresql://wayforth:wayforth_dev@localhost:5432/wayforth")
_ASYNCPG_URL = _DB_URL.replace("postgresql+asyncpg://", "postgresql://")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Wayforth API starting, environment={ENVIRONMENT}")
    ok = check_db()
    if not ok:
        logger.warning("DB connection check failed — starting anyway")
    app.state.db_ok = ok
    try:
        app.state.pool = await asyncpg.create_pool(_ASYNCPG_URL, min_size=2, max_size=10)
        app.state.db_ok = True
    except Exception as e:
        logger.error(f"DB error: {e}")
        logger.warning(f"DB pool creation failed: {e} — /services will be unavailable")
        app.state.pool = None
    yield
    if app.state.pool:
        await app.state.pool.close()


app = FastAPI(lifespan=lifespan)

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
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
@limiter.limit("60/minute")
def health(request: Request):
    db_status = "ok" if getattr(app.state, "db_ok", False) else "unavailable"
    return {"status": "ok", "service": "wayforth-api", "version": "0.1.0", "db_status": db_status}


@app.get("/chain")
@limiter.limit("10/minute")
def chain_info(request: Request):
    """On-chain contract addresses, stats, and supported payment protocols"""
    chain = get_chain_stats()
    return {
        "wayforth_escrow": {
            "address": chain.get("escrow"),
            "fee_pct": chain.get("fee_pct"),
            "use_for": "services without x402 support",
            **{k: v for k, v in chain.items() if k not in ("escrow", "fee_pct")},
        },
        "x402": {
            "protocol": "HTTP 402",
            "fee_pct": 0,
            "use_for": "services with native x402 support",
            "docs": "https://x402.org",
        },
    }


@app.get(
    "/search",
    summary="Semantic service search",
    description=(
        "Rank Wayforth services by relevance to a natural language query using Claude Haiku. "
        "Falls back to keyword scoring when ANTHROPIC_API_KEY is not set."
    ),
)
@limiter.limit("10/minute")
async def search_services(
    request: Request,
    q: str = Query(description="Natural language query, e.g. 'fast cheap inference for coding'"),
    category: str | None = Query(default=None, description="Filter by category: inference, data, translation, …"),
    tier: int | None = Query(default=None, description="Filter by exact coverage tier (0=free, 1=basic, 2=standard, 3=premium)"),
    limit: int = Query(default=5, ge=1, le=20, description="Number of results to return (1–20)"),
):
    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name, description, endpoint_url, category,
                       coverage_tier, pricing_usdc, source, payment_protocol, created_at
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
    ranked = await rank_services(q, services)
    top = ranked[:limit]
    pool = app.state.pool
    if ranked and pool:
        asyncio.create_task(log_query(pool, str(ranked[0]["id"]), q, ranked[0].get("score", 0)))
    logger.info(f"search q={q!r} results={len(top)}")
    results = [
        {
            "name": s.get("name"),
            "score": s.get("score", 0),
            "reason": s.get("reason", ""),
            "coverage_tier": s.get("coverage_tier"),
            "category": s.get("category"),
            "endpoint_url": s.get("endpoint_url"),
            "pricing_usdc": s.get("pricing_usdc"),
            "payment_protocol": s.get("payment_protocol", "wayforth"),
            "service_id": "0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest(),
            "payment": PAYMENT_INFO,
        }
        for s in top
    ]
    return {"query": q, "total_results": len(top), "results": results}


@app.get("/services")
@limiter.limit("30/minute")
async def list_services(
    request: Request,
    category: str | None = Query(default=None),
    tier: int | None = Query(default=None, description="Filter by coverage tier (0=free, 1=basic, 2=standard, 3=premium)"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    try:
        async with app.state.pool.acquire() as conn:
            total = await conn.fetchval(
                """
                SELECT COUNT(*) FROM services
                WHERE ($1::text IS NULL OR category = $1)
                  AND ($2::int IS NULL OR coverage_tier = $2)
                """,
                category,
                tier,
            )
            rows = await conn.fetch(
                """
                SELECT id, name, description, endpoint_url, category,
                       coverage_tier, pricing_usdc, source, payment_protocol, created_at
                FROM services
                WHERE ($1::text IS NULL OR category = $1)
                  AND ($2::int IS NULL OR coverage_tier = $2)
                ORDER BY created_at DESC
                LIMIT $3 OFFSET $4
                """,
                category,
                tier,
                limit,
                offset,
            )
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"total": total, "offset": offset, "limit": limit, "results": [dict(r) for r in rows]}


@app.get("/stats")
@limiter.limit("30/minute")
async def get_stats(request: Request):
    try:
        async with app.state.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM services")
            tier_rows = await conn.fetch(
                "SELECT coverage_tier, COUNT(*) AS cnt FROM services GROUP BY coverage_tier"
            )
            category_rows = await conn.fetch(
                "SELECT category, COUNT(*) AS cnt FROM services GROUP BY category"
            )
            tier2_rows = await conn.fetch(
                "SELECT name FROM services WHERE coverage_tier = 2 ORDER BY name"
            )
            last_updated = await conn.fetchval("SELECT MAX(created_at) FROM services")
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    by_tier = {str(t): 0 for t in range(4)}
    for r in tier_rows:
        by_tier[str(r["coverage_tier"])] = r["cnt"]

    by_category = {r["category"]: r["cnt"] for r in category_rows}
    tier2_services = [r["name"] for r in tier2_rows]
    last_updated_str = last_updated.isoformat() + "Z" if last_updated else None

    return {
        "total_services": total,
        "by_tier": by_tier,
        "by_category": by_category,
        "tier2_services": tier2_services,
        "last_updated": last_updated_str,
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
async def leaderboard(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
    period: str = Query(default="7d", description="Time window: 1d, 7d, 30d"),
):
    days = 7
    if period.endswith("d"):
        try:
            days = max(1, min(int(period[:-1]), 90))
        except ValueError:
            pass

    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT s.id, s.name, s.category, s.coverage_tier, s.endpoint_url,
                       COUNT(q.id)            AS query_count,
                       ROUND(AVG(q.score), 1) AS avg_score
                FROM services s
                JOIN service_queries q ON s.id = q.service_id
                WHERE q.queried_at > NOW() - ($2 * INTERVAL '1 day')
                GROUP BY s.id
                ORDER BY query_count DESC
                LIMIT $1
                """,
                limit,
                days,
            )
            total_queries = await conn.fetchval(
                "SELECT COUNT(*) FROM service_queries WHERE queried_at > NOW() - ($1 * INTERVAL '1 day')",
                days,
            )
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    return {
        "period": period,
        "total_queries": total_queries,
        "leaderboard": [
            {
                "rank": i + 1,
                "name": r["name"],
                "query_count": r["query_count"],
                "avg_score": r["avg_score"],
                "category": r["category"],
                "coverage_tier": r["coverage_tier"],
                "endpoint_url": r["endpoint_url"],
            }
            for i, r in enumerate(rows)
        ],
    }


class PayRequest(BaseModel):
    service_id: str
    service_owner: str
    amount_usdc: float


class SubmitRequest(BaseModel):
    name: str
    description: str
    endpoint_url: str
    category: str
    pricing_usdc: float = 0.0
    contact_email: str | None = None


@app.post("/pay")
@limiter.limit("20/minute")
async def pay(request: Request, req: PayRequest):
    if req.amount_usdc <= 0.000066:
        raise HTTPException(
            status_code=400,
            detail="amount_usdc must be > 0.000066 (minimum to generate non-zero fee)",
        )
    if not Web3.is_address(req.service_owner):
        raise HTTPException(status_code=400, detail="service_owner is not a valid Ethereum address")
    sid = req.service_id.removeprefix("0x")
    if len(sid) != 64 or not all(c in "0123456789abcdefABCDEF" for c in sid):
        raise HTTPException(
            status_code=400,
            detail="service_id must be a valid bytes32 hex string (0x + 64 hex chars)",
        )
    logger.info(f"pay amount={req.amount_usdc} service={req.service_id[:10]}")
    return build_payment_calldata(req.service_id, req.service_owner, req.amount_usdc)


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
                req.name, req.description, req.endpoint_url, req.category, req.pricing_usdc,
            )
            await conn.execute(
                """INSERT INTO service_submissions (service_id, contact_email, ip_address)
                   VALUES ($1, $2, $3)""",
                service_id, req.contact_email, get_real_ip(request),
            )
        logger.info(f"submit name={req.name!r} category={req.category}")
        if req.contact_email:
            asyncio.create_task(asyncio.to_thread(
                send_submission_confirmation,
                req.contact_email, req.name, str(service_id), req.endpoint_url,
            ))
        return {
            "id": str(service_id),
            "name": req.name,
            "coverage_tier": 0,
            "message": "Service submitted successfully. Our crawler will verify it within 24 hours. You'll start at Tier 0 and be promoted automatically as uptime data accumulates.",
            "basescan": "https://sepolia.basescan.org/address/0xE0596DbF37Fd9e3e5E39822602732CC0865E49C7",
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
        "infrastructure": {
            "api": "healthy",
            "db": "healthy" if getattr(app.state, "db_ok", False) else "unavailable",
            "sentry": "connected" if SENTRY_DSN else "not configured",
            "contracts": {
                "registry": REGISTRY_ADDRESS,
                "escrow": ESCROW_ADDRESS,
            },
        },
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


app.mount("/static", StaticFiles(directory="static"), name="static")
