import logging
import os
from fastapi import Request, HTTPException

logger = logging.getLogger("wayforth.db")

_DB_URL = os.environ.get("DATABASE_URL", "")
_ASYNCPG_URL = _DB_URL.replace("postgresql+asyncpg://", "postgresql://")


async def get_db(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        logger.error("get_db: pool is None — startup may have failed")
        raise HTTPException(status_code=503, detail={"error": "db_unavailable", "message": "Database pool not initialized"})
    try:
        async with pool.acquire(timeout=8.0) as conn:
            yield conn
    except TimeoutError:
        raise HTTPException(status_code=503, detail={"error": "service_overloaded"})
