import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, Query

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


@app.get("/debug/env")
def debug_env():
    key = os.getenv("ANTHROPIC_API_KEY", "")
    return {"anthropic_key_present": bool(key), "key_prefix": key[:8]}


@app.get("/health")
def health():
    db_status = "ok" if getattr(app.state, "db_ok", False) else "unavailable"
    return {"status": "ok", "service": "wayforth-api", "version": "0.1.0", "db_status": db_status}


@app.get(
    "/search",
    summary="Semantic service search",
    description=(
        "Rank Wayforth services by relevance to a natural language query using Claude Haiku. "
        "Falls back to keyword scoring when ANTHROPIC_API_KEY is not set."
    ),
)
async def search_services(
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
        }
        for s in top
    ]
    return {"query": q, "total_results": len(top), "results": results}


@app.get("/services")
async def list_services(category: str | None = Query(default=None)):
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, description, endpoint_url, category,
                   coverage_tier, pricing_usdc, source, created_at
            FROM services
            WHERE ($1::text IS NULL OR category = $1)
            ORDER BY created_at DESC
            LIMIT 100
            """,
            category,
        )
    return [dict(r) for r in rows]
