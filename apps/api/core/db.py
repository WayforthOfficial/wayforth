import os
from fastapi import Request


_DB_URL = os.environ.get("DATABASE_URL", "")
_ASYNCPG_URL = _DB_URL.replace("postgresql+asyncpg://", "postgresql://")


async def get_db(request: Request):
    async with request.app.state.pool.acquire() as conn:
        yield conn
