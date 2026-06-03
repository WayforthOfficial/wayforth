import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from health_monitor import run_health_check, fire_tier_promotion_email
from graph_builder import build_service_graph
from x402_monitor import run_x402_monitor

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("promoter")

# DATABASE_PUBLIC_URL (externally routable) is preferred when running outside
# Railway's private network. DATABASE_URL uses postgres.railway.internal which
# only resolves from within Railway. Both are set on the crawler service.
DB_URL = (
    os.environ.get("DATABASE_PUBLIC_URL")
    or os.environ.get("DATABASE_URL", "")
)
_ASYNCPG_URL = DB_URL.replace("postgresql+asyncpg://", "postgresql://")

_UPTIME_THRESHOLD = 90.0  # Phase 1: tighten to 99.0 post-seed

WAYFORTH_API_KEY = os.environ.get("WAYFORTH_TEST_API_KEY", "")
WAYFORTH_BASE_URL = os.environ.get("WAYFORTH_BASE_URL", "https://gateway.wayforth.io")
RANK_SERVICE_URL = os.environ.get("RANK_SERVICE_URL", "")
RANK_SERVICE_KEY = os.environ.get("RANK_SERVICE_KEY", "")


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
        await fire_tier_promotion_email(db_conn, str(service["id"]), service.get("name", ""), 2)
        return True
    except Exception as exc:
        logger.warning("promote_tier1_to_tier2 failed for %s: %s", service.get("name"), exc)
        return False


async def bulk_promote_to_tier2(db_conn: asyncpg.Connection) -> int:
    """
    Promote all Tier 1 services with ≥1 successful probe in the last 7 days to Tier 2.
    Called after bulk_prober.py runs to batch-upgrade the catalog.
    Returns count of newly promoted services.
    """
    result = await db_conn.execute(
        """
        UPDATE services
        SET coverage_tier = 2,
            payment_tested = TRUE,
            updated_at = NOW()
        WHERE coverage_tier = 1
          AND schema_validated = TRUE
          AND EXISTS (
              SELECT 1 FROM service_probes
              WHERE service_id = services.id
                AND reachable = TRUE
                AND probed_at >= NOW() - INTERVAL '7 days'
          )
        """
    )
    count = int(result.split()[-1])
    logger.info("bulk_promote_to_tier2: %d services Tier 1 → 2", count)
    return count


async def bulk_demote_stale_tier2(db_conn: asyncpg.Connection) -> int:
    """
    Demote Tier 2 services with 0 successful probes in the last 14 days back to Tier 1.
    Returns count of demoted services.
    """
    result = await db_conn.execute(
        """
        UPDATE services
        SET coverage_tier = 1,
            updated_at = NOW()
        WHERE coverage_tier = 2
          AND NOT EXISTS (
              SELECT 1 FROM service_probes
              WHERE service_id = services.id
                AND reachable = TRUE
                AND probed_at >= NOW() - INTERVAL '14 days'
          )
        """
    )
    count = int(result.split()[-1])
    logger.info("bulk_demote_stale_tier2: %d services Tier 2 → 1", count)
    return count


async def run_wri_recalculate() -> None:
    """Trigger WRI score recalculation via the wayforth-rank private service.

    No-ops if RANK_SERVICE_URL or RANK_SERVICE_KEY is not configured.
    The v2 formula lives in wayforth-rank; this call keeps formula weights
    out of the public crawler code.
    """
    if not RANK_SERVICE_URL or not RANK_SERVICE_KEY:
        logger.info("run_wri_recalculate: RANK_SERVICE_URL/KEY not set, skipping")
        return
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(
                f"{RANK_SERVICE_URL}/v1/rank/recalculate",
                headers={"X-Rank-Service-Key": RANK_SERVICE_KEY},
            )
            r.raise_for_status()
            data = r.json()
            logger.info("run_wri_recalculate: updated=%d unmatched=%d",
                        data.get("updated", 0), len(data.get("unmatched_slugs", [])))
    except Exception as exc:
        logger.error("run_wri_recalculate failed: %s", exc)


# Representative search→execute pairs for daily signal seeding.
# Covers inference/translation/search/data/web categories at low credit cost (~40 credits/run).
# Heavy services (stability, elevenlabs) excluded — run feed_signal.py manually for full coverage.
_SIGNAL_QUERIES: list[tuple[str, str, dict]] = [
    ("fast llm inference", "groq",
     {"messages": [{"role": "user", "content": "What is 2+2?"}], "model": "llama-3.3-70b-versatile"}),
    ("fast chat inference", "mistral",
     {"messages": [{"role": "user", "content": "Say hello"}], "model": "mistral-small-latest"}),
    ("translate to spanish", "deepl", {"text": "Hello world", "target_lang": "ES"}),
    ("web search", "serper", {"query": "best MCP servers 2026"}),
    ("search the web", "tavily", {"query": "AI agent payment infrastructure", "max_results": 3}),
    ("weather forecast", "openweather", {"city": "San Francisco"}),
    ("read webpage", "jina", {"url": "https://wayforth.io"}),
]


async def run_signal_feed(api_key: str, base_url: str) -> None:
    """Feed search→execute pairs to generate WayforthRank signal data.

    Runs daily at 06:00 UTC only (gated in run_promotion_cycle).
    Requires WAYFORTH_TEST_API_KEY and WAYFORTH_BASE_URL in the crawler service env.
    """
    headers = {"X-Wayforth-API-Key": api_key, "Content-Type": "application/json"}
    ok = fail = 0
    async with httpx.AsyncClient() as client:
        for query, slug, params in _SIGNAL_QUERIES:
            try:
                await client.get(
                    f"{base_url}/search",
                    params={"q": query, "limit": 3},
                    headers=headers,
                    timeout=15.0,
                )
            except Exception:
                pass
            await asyncio.sleep(1)
            try:
                r = await client.post(
                    f"{base_url}/execute",
                    headers=headers,
                    json={"service_slug": slug, "params": params},
                    timeout=30.0,
                )
                if r.status_code in (200, 201):
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
            await asyncio.sleep(2)
    logger.info("run_signal_feed: %d ok, %d failed", ok, fail)


async def run_promotion_cycle(db_url: str) -> None:
    """
    Main entry point. Run one full promotion cycle:
    1. Fetch all Tier 0 services (limit 50 per run)
    2. Probe each one concurrently (asyncio.gather with semaphore of 10)
    3. Promote passing services to Tier 1
    4. Fetch all Tier 1 services
    5. Update their uptime stats
    6. Promote qualifying services to Tier 2 (uptime-based)
    7. Bulk-promote Tier 1 services with any recent successful probe to Tier 2
    8. Demote stale Tier 2 services with no recent probes
    9. Print summary
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

    # Bulk promotion: upgrade any Tier 1 with recent probe to Tier 2
    async with pool.acquire() as conn:
        bulk_promoted = await bulk_promote_to_tier2(conn)
        bulk_demoted = await bulk_demote_stale_tier2(conn)

    async with pool.acquire() as conn:
        tier2_total = await conn.fetchval(
            "SELECT COUNT(*) FROM services WHERE coverage_tier = 2"
        )

    print(
        f"Cycle complete: "
        f"{tier0_promoted} Tier0→1, "
        f"{tier1_promoted} Tier1→2 (uptime), "
        f"{bulk_promoted} Tier1→2 (bulk), "
        f"{bulk_demoted} Tier2→1 (stale), "
        f"{failed} failed probes | "
        f"Tier 2 total: {tier2_total}"
    )

    await run_health_check(pool)
    await run_wri_recalculate()
    await build_service_graph(pool)
    logger.info("Service graph updated")
    await run_x402_monitor(pool)
    logger.info("x402 monitor complete")

    if datetime.utcnow().hour == 6 and WAYFORTH_API_KEY:
        logger.info("Daily signal feed: running (06:00 UTC tick)")
        await run_signal_feed(WAYFORTH_API_KEY, WAYFORTH_BASE_URL)

    await pool.close()


if __name__ == "__main__":
    asyncio.run(run_promotion_cycle(_ASYNCPG_URL))
