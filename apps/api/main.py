import hashlib
import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address  # fallback only
from web3 import Web3

from chain import PAYMENT_INFO, build_payment_calldata, get_chain_stats
from db import check_db
from ranker import rank_services

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_DB_URL = os.environ.get("DATABASE_URL", "postgresql://wayforth:wayforth_dev@localhost:5432/wayforth")
_ASYNCPG_URL = _DB_URL.replace("postgresql+asyncpg://", "postgresql://")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ok = check_db()
    if not ok:
        logger.warning("DB connection check failed — starting anyway")
    app.state.db_ok = ok
    try:
        app.state.pool = await asyncpg.create_pool(_ASYNCPG_URL, min_size=2, max_size=10)
        app.state.db_ok = True
    except Exception as e:
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


@app.get("/test-limit")
@limiter.limit("3/minute")
def test_limit(request: Request):
    return {"status": "ok", "message": "rate limit not hit"}


@app.get("/debug/env")
@limiter.limit("10/minute")
def debug_env(request: Request):
    key = os.getenv("ANTHROPIC_API_KEY", "")
    return {"anthropic_key_present": bool(key), "key_prefix": key[:8]}


@app.get("/health")
@limiter.limit("60/minute")
def health(request: Request):
    db_status = "ok" if getattr(app.state, "db_ok", False) else "unavailable"
    return {"status": "ok", "service": "wayforth-api", "version": "0.1.0", "db_status": db_status}


@app.get("/chain")
@limiter.limit("10/minute")
def chain_info(request: Request):
    """On-chain contract addresses and stats"""
    return get_chain_stats()


@app.get(
    "/search",
    summary="Semantic service search",
    description=(
        "Rank Wayforth services by relevance to a natural language query using Claude Haiku. "
        "Falls back to keyword scoring when ANTHROPIC_API_KEY is not set."
    ),
)
@limiter.limit("5/minute")
async def search_services(
    request: Request,
    q: str = Query(description="Natural language query, e.g. 'fast cheap inference for coding'"),
    category: str | None = Query(default=None, description="Filter by category: inference, data, translation, …"),
    tier: int | None = Query(default=None, description="Filter by exact coverage tier (0=free, 1=basic, 2=standard, 3=premium)"),
    limit: int = Query(default=5, ge=1, le=20, description="Number of results to return (1–20)"),
):
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, description, endpoint_url, category,
                   coverage_tier, pricing_usdc, source, created_at
            FROM services
            WHERE ($1::text IS NULL OR category = $1)
              AND ($2::int IS NULL OR coverage_tier = $2)
            ORDER BY created_at DESC
            """,
            category,
            tier,
        )
    services = [dict(r) for r in rows]
    ranked = await rank_services(q, services)
    top = ranked[:limit]
    results = [
        {
            "name": s.get("name"),
            "score": s.get("score", 0),
            "reason": s.get("reason", ""),
            "coverage_tier": s.get("coverage_tier"),
            "category": s.get("category"),
            "endpoint_url": s.get("endpoint_url"),
            "pricing_usdc": s.get("pricing_usdc"),
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
                   coverage_tier, pricing_usdc, source, created_at
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
    return {"total": total, "offset": offset, "limit": limit, "results": [dict(r) for r in rows]}


@app.get("/stats")
@limiter.limit("30/minute")
async def get_stats(request: Request):
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


class PayRequest(BaseModel):
    service_id: str
    service_owner: str
    amount_usdc: float


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
    return build_payment_calldata(req.service_id, req.service_owner, req.amount_usdc)


@app.get("/services/{service_id}")
@limiter.limit("30/minute")
async def get_service(request: Request, service_id: str):
    async with app.state.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, name, description, endpoint_url, category,
                   coverage_tier, pricing_usdc, source, created_at
            FROM services WHERE id = $1
            """,
            service_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return dict(row)
