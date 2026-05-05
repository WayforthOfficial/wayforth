import asyncio
import os

import asyncpg


async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    result = await conn.execute("""
        UPDATE search_analytics
        SET query = LOWER(TRIM(query))
        WHERE query != LOWER(TRIM(query))
    """)
    count = result.split()[-1]
    print(f"Normalized {count} rows")
    await conn.close()


asyncio.run(main())
