import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI, Query

from db import check_db

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


@app.get("/health")
def health():
    db_status = "ok" if getattr(app.state, "db_ok", False) else "unavailable"
    return {"status": "ok", "service": "wayforth-api", "version": "0.1.0", "db_status": db_status}


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
