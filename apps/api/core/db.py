import asyncio
import logging
import os
from fastapi import Request, HTTPException

logger = logging.getLogger("wayforth.db")

_DB_URL = os.environ.get("DATABASE_URL", "")
_ASYNCPG_URL = _DB_URL.replace("postgresql+asyncpg://", "postgresql://")

# How many times to retry acquiring a connection when the pool returns a dead one.
# Each retry waits 300ms, 600ms — total max delay ~0.9s before 503.
_ACQUIRE_RETRIES = 2


def get_pool_stats(pool) -> dict:
    """Return current asyncpg pool state for diagnostics."""
    try:
        return {
            "size":      pool.get_size(),
            "idle":      pool.get_idle_size(),
            "min_size":  pool.get_min_size(),
            "max_size":  pool.get_max_size(),
        }
    except Exception:
        return {}


async def get_db(request: Request):
    pool = getattr(request.app.state, "pool", None)
    if pool is None:
        logger.error("get_db: pool is None — startup may have failed")
        raise HTTPException(
            status_code=503,
            detail={"error": "db_unavailable", "message": "Database pool not initialized"},
        )

    last_exc: Exception | None = None
    for attempt in range(_ACQUIRE_RETRIES + 1):
        try:
            async with pool.acquire(timeout=8.0) as conn:
                yield conn
                return
        except TimeoutError as exc:
            stats = get_pool_stats(pool)
            logger.warning("get_db: acquire timeout (attempt %d) pool=%s", attempt + 1, stats)
            raise HTTPException(status_code=503, detail={"error": "service_overloaded"}) from exc
        except Exception as exc:
            last_exc = exc
            stats = get_pool_stats(pool)
            logger.warning(
                "get_db: connection error attempt %d/%d — %s — pool=%s",
                attempt + 1, _ACQUIRE_RETRIES + 1, exc, stats,
            )
            if attempt < _ACQUIRE_RETRIES:
                await asyncio.sleep(0.3 * (attempt + 1))

    logger.error("get_db: all %d attempts failed: %s", _ACQUIRE_RETRIES + 1, last_exc)
    raise HTTPException(status_code=503, detail={"error": "db_unavailable"})
