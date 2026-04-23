import logging
import os
from contextlib import asynccontextmanager

import asyncpg
from dotenv import load_dotenv
from fastapi import FastAPI

from db import check_db

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_DB_URL = os.environ.get("DATABASE_URL", "postgresql://wayforth:wayforth_dev@localhost:5432/wayforth")
_ASYNCPG_URL = _DB_URL.replace("postgresql+asyncpg://", "postgresql://")


@asynccontextmanager
async def lifespan(app: FastAPI):
    ok = check_db()
    logger.info(f"DB connection: {'ok' if ok else 'FAILED'}")
    app.state.pool = await asyncpg.create_pool(_ASYNCPG_URL, min_size=2, max_size=10)
    yield
    await app.state.pool.close()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok", "service": "wayforth-api", "version": "0.1.0"}


@app.get("/services")
async def list_services():
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, name, description, endpoint_url, category,
                   coverage_tier, pricing_usdc, source, created_at
            FROM services
            ORDER BY created_at DESC
            LIMIT 20
            """
        )
    return [dict(r) for r in rows]
