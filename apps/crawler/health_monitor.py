"""
Wayforth Health Monitor
Runs every 6 hours via Railway cron (called from promoter.py).
Probes all Tier 2 services and auto-demotes if they fail 3 consecutive checks.
"""
import asyncio
import logging
import os

import asyncpg
import httpx
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("wayforth.monitor")


async def probe_service(client: httpx.AsyncClient, service: dict) -> bool:
    """Return True if service responds within 10s with non-5xx status."""
    try:
        r = await client.get(service["endpoint_url"], timeout=10.0)
        return r.status_code < 500
    except Exception as e:
        logger.warning(f"Probe failed for {service['name']}: {e}")
        return False


async def run_health_check(pool=None) -> None:
    """Probe all Tier 2 services and auto-demote after 3 consecutive failures."""
    _own_pool = pool is None
    if _own_pool:
        db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
        pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)

    try:
        async with pool.acquire() as conn:
            services = await conn.fetch(
                "SELECT id, name, endpoint_url, consecutive_failures FROM services WHERE coverage_tier = 2"
            )

        logger.info(f"Probing {len(services)} Tier 2 services")

        async with httpx.AsyncClient(follow_redirects=True) as client:
            for svc in services:
                is_up = await probe_service(client, dict(svc))

                async with pool.acquire() as conn:
                    if is_up:
                        await conn.execute(
                            """
                            UPDATE services
                            SET consecutive_failures = 0, last_tested_at = NOW(), updated_at = NOW()
                            WHERE id = $1
                            """,
                            svc["id"],
                        )
                        logger.info(f"✅ {svc['name']} — UP")
                    else:
                        failures = (svc["consecutive_failures"] or 0) + 1

                        if failures >= 3:
                            await conn.execute(
                                """
                                UPDATE services
                                SET coverage_tier = 1, consecutive_failures = $1,
                                    last_tested_at = NOW(), updated_at = NOW()
                                WHERE id = $2
                                """,
                                failures,
                                svc["id"],
                            )
                            logger.warning(f"⬇️ {svc['name']} — DEMOTED to Tier 1 after {failures} failures")
                        else:
                            await conn.execute(
                                """
                                UPDATE services
                                SET consecutive_failures = $1, last_tested_at = NOW(), updated_at = NOW()
                                WHERE id = $2
                                """,
                                failures,
                                svc["id"],
                            )
                            logger.warning(f"⚠️ {svc['name']} — DOWN ({failures}/3 failures)")

        logger.info("Health check complete")
    finally:
        if _own_pool:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(run_health_check())
