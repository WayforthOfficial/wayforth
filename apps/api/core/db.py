import os
from fastapi import Request, HTTPException


_DB_URL = os.environ.get("DATABASE_URL", "")
_ASYNCPG_URL = _DB_URL.replace("postgresql+asyncpg://", "postgresql://")


async def get_db(request: Request):
    try:
        async with request.app.state.pool.acquire(timeout=8.0) as conn:
            yield conn
    except TimeoutError:
        raise HTTPException(status_code=503, detail={"error": "service_overloaded"})
