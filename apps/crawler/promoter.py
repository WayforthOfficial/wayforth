import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from health_monitor import run_health_check
from graph_builder import build_service_graph
from x402_monitor import run_x402_monitor

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("promoter")

DB_URL = os.environ.get("DATABASE_URL", "")
_ASYNCPG_URL = DB_URL.replace("postgresql+asyncpg://", "postgresql://")

_UPTIME_THRESHOLD = 90.0  # Phase 1: tighten to 99.0 post-seed


async def probe_service(service: dict, *, _client: httpx.AsyncClient | None = None) -> dict:
    """
    Probe a service endpoint to check if it's alive and returns valid JSON.
    Returns: {"reachable": bool, "response_time_ms": float, "has_valid_response": bool,
              "error": str|None, "status_code": int|None}
    """
    url = service.get("endpoint_url", "")
    start = time.monotonic()

    async def _do(client: httpx.AsyncClient) -> dict:
        try:
            resp = await client.get(url, timeout=5.0)
            elapsed_ms = (time.monotonic() - start) * 1000
            reachable = 200 <= resp.status_code < 400
            has_valid_response = False
            if reachable:
                try:
                    resp.json()
                    has_valid_response = True
                except Exception:
                    pass
            return {
                "reachable": reachable,
                "response_time_ms": round(elapsed_ms, 2),
                "has_valid_response": has_valid_response,
                "error": None,
                "status_code": resp.status_code,
            }
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            return {
                "reachable": False,
                "response_time_ms": round(elapsed_ms, 2),
                "has_valid_response": False,
                "error": str(exc),
                "status_code": None,
            }

    if _client is not None:
        return await _do(_client)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(5.0), follow_redirects=True
    ) as client:
        return await _do(client)


async def promote_tier0_to_tier1(
    service: dict, db_conn: asyncpg.Connection, *, _client: httpx.AsyncClient | None = None
) -> bool:
    """
    Promote a Tier 0 service to Tier 1 if it passes a probe.
    Requirements: reachable=True AND has_valid_response=True
    On success: UPDATE coverage_tier=1, last_tested_at=NOW(), schema_validated=TRUE
    Returns True if promoted.
    """
    try:
        result = await probe_service(service, _client=_client)
        if not (result["reachable"] and result["has_valid_response"]):
            return False
        await db_conn.execute(
            """
            UPDATE services
            SET coverage_tier=1, last_tested_at=NOW(), schema_validated=TRUE, updated_at=NOW()
            WHERE id=$1
            """,
            service["id"],
        )
        logger.info("Promoted Tier0→1: %s", service.get("name", service["id"]))
        return True
    except Exception as exc:
        logger.warning("promote_tier0_to_tier1 failed for %s: %s", service.get("name"), exc)
        return False


async def update_uptime_stats(
    service: dict, db_conn: asyncpg.Connection, *, _client: httpx.AsyncClient | None = None
) -> float:
    """
    For Tier 1 services: probe the endpoint, record the result in service_probes,
    calculate uptime_7d from the last 7 days, and update services.uptime_7d.
    Returns the uptime percentage.
    """
    try:
        result = await probe_service(service, _client=_client)

        await db_conn.execute(
            """
            INSERT INTO service_probes (service_id, reachable, response_time_ms, status_code, error_message)
            VALUES ($1, $2, $3, $4, $5)
            """,
            service["id"],
            result["reachable"],
            result["response_time_ms"],
            result["status_code"],
            result["error"],
        )

        uptime = await db_conn.fetchval(
            """
            SELECT COUNT(*) FILTER (WHERE reachable) * 100.0 / COUNT(*)
            FROM service_probes
            WHERE service_id=$1 AND probed_at >= NOW() - INTERVAL '7 days'
            """,
            service["id"],
        ) or 0.0

        await db_conn.execute(
            """
            UPDATE services SET uptime_7d=$2, last_tested_at=NOW(), updated_at=NOW() WHERE id=$1
            """,
            service["id"],
            uptime,
        )

        return float(uptime)
    except Exception as exc:
        logger.warning("update_uptime_stats failed for %s: %s", service.get("name"), exc)
        return 0.0


async def promote_tier1_to_tier2(service: dict, db_conn: asyncpg.Connection) -> bool:
    """
    Promote a Tier 1 service to Tier 2 if:
    - uptime_7d >= 90.0
    - last_tested_at within last 48 hours
    - schema_validated = True
    On success: UPDATE coverage_tier=2, payment_tested=True (simulated for Phase 1)
    Returns True if promoted.
    """
    try:
        uptime = service.get("uptime_7d")
        last_tested = service.get("last_tested_at")
        schema_ok = service.get("schema_validated", False)

        if uptime is None or float(uptime) < _UPTIME_THRESHOLD:
            return False
        if last_tested is None:
            return False
        if datetime.now(timezone.utc) - last_tested > timedelta(hours=48):
            return False
        if not schema_ok:
            return False

        await db_conn.execute(
            """
            UPDATE services SET coverage_tier=2, payment_tested=TRUE, updated_at=NOW() WHERE id=$1
            """,
            service["id"],
        )
        logger.info("Promoted Tier1→2: %s", service.get("name", service["id"]))
        return True
    except Exception as exc:
        logger.warning("promote_tier1_to_tier2 failed for %s: %s", service.get("name"), exc)
        return False


async def run_promotion_cycle(db_url: str) -> None:
    """
    Main entry point. Run one full promotion cycle:
    1. Fetch all Tier 0 services (limit 50 per run)
    2. Probe each one concurrently (asyncio.gather with semaphore of 10)
    3. Promote passing services to Tier 1
    4. Fetch all Tier 1 services
    5. Update their uptime stats
    6. Promote qualifying services to Tier 2
    7. Print summary
    """
    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)
    sem = asyncio.Semaphore(10)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(5.0), follow_redirects=True
    ) as client:

        # --- Tier 0 → 1 ---
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM services WHERE coverage_tier=0 LIMIT 50"
            )
        tier0 = [dict(r) for r in rows]
        logger.info("Fetched %d Tier 0 services", len(tier0))

        async def _do_tier0(svc: dict) -> bool:
            async with sem:
                async with pool.acquire() as conn:
                    return await promote_tier0_to_tier1(svc, conn, _client=client)

        t0_results = await asyncio.gather(
            *[_do_tier0(s) for s in tier0], return_exceptions=True
        )

        # --- Tier 1 uptime + Tier 1 → 2 ---
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM services WHERE coverage_tier=1"
            )
        tier1 = [dict(r) for r in rows]
        logger.info("Fetched %d Tier 1 services", len(tier1))

        async def _do_tier1(svc: dict) -> bool:
            async with sem:
                async with pool.acquire() as conn:
                    await update_uptime_stats(svc, conn, _client=client)
                    # Re-fetch so promote_tier1_to_tier2 sees the uptime_7d we just wrote
                    fresh_row = await conn.fetchrow(
                        "SELECT * FROM services WHERE id=$1", svc["id"]
                    )
                    fresh = dict(fresh_row)
                    return await promote_tier1_to_tier2(fresh, conn)

        t1_results = await asyncio.gather(
            *[_do_tier1(s) for s in tier1], return_exceptions=True
        )

    tier0_promoted = sum(1 for r in t0_results if r is True)
    tier1_promoted = sum(1 for r in t1_results if r is True)
    failed = (
        sum(1 for r in t0_results if r is not True)
        + sum(1 for r in t1_results if r is not True)
    )
    print(
        f"Cycle complete: {tier0_promoted} Tier0→1, {tier1_promoted} Tier1→2, {failed} failed probes"
    )

    await run_health_check(pool)
    await build_service_graph(pool)
    logger.info("Service graph updated")
    await run_x402_monitor(pool)
    logger.info("x402 monitor complete")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(run_promotion_cycle(_ASYNCPG_URL))
