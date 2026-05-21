"""
Bulk prober — probes all services with coverage_tier < 2 that have never had
a successful probe. Accepts HTTP 200-299 or 401/403 (auth required, server alive)
as a live signal. Logs results to service_probes.

Configuration:
  - 50 concurrent requests
  - 1 request per domain per 2 seconds (per-domain rate limiting)
  - 5 second timeout per request
  - HEAD first; falls back to GET on 405

The updated promoter.py uses these probe records to bulk-promote Tier 1 → Tier 2.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("bulk_prober")

# DATABASE_PUBLIC_URL (externally routable) is preferred when running outside
# Railway's private network. DATABASE_URL uses postgres.railway.internal which
# only resolves from within Railway. Both are set on the crawler service.
DB_URL = (
    os.environ.get("DATABASE_PUBLIC_URL")
    or os.environ.get("DATABASE_URL", "")
)
_ASYNCPG_URL = DB_URL.replace("postgresql+asyncpg://", "postgresql://")

_CONCURRENCY = 50
_DOMAIN_RATE_S = 2.0
_TIMEOUT_S = 5.0
# 2xx = success; 401/403 = auth required but server is alive
_ALIVE_STATUSES = frozenset({*range(200, 300), 401, 403})


def _extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return url


class _DomainRateLimiter:
    """One request per domain per `interval_s` seconds (asyncio-safe)."""

    def __init__(self, interval_s: float) -> None:
        self._interval = interval_s
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        return self._locks[domain]

    async def acquire(self, domain: str) -> None:
        async with self._get_lock(domain):
            now = time.monotonic()
            gap = self._interval - (now - self._last.get(domain, 0.0))
            if gap > 0:
                await asyncio.sleep(gap)
            self._last[domain] = time.monotonic()


async def _probe_one_url(
    url: str,
    client: httpx.AsyncClient,
    limiter: _DomainRateLimiter,
) -> tuple[bool, int | None, float, str | None]:
    """
    Returns (alive, status_code, response_time_ms, error_message).
    Tries HEAD first; falls back to GET if server returns 405.
    """
    domain = _extract_domain(url)
    await limiter.acquire(domain)

    t0 = time.monotonic()
    try:
        resp = await client.head(url, timeout=_TIMEOUT_S)
        if resp.status_code == 405:
            resp = await client.get(url, timeout=_TIMEOUT_S)
        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return resp.status_code in _ALIVE_STATUSES, resp.status_code, elapsed, None
    except Exception as exc:
        elapsed = round((time.monotonic() - t0) * 1000, 2)
        return False, None, elapsed, str(exc)[:500]


async def run_bulk_probe(db_url: str) -> dict[str, int]:
    """
    Probe all unverified services (coverage_tier < 2, never had a successful probe).
    Returns summary counts.
    """
    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)
    limiter = _DomainRateLimiter(_DOMAIN_RATE_S)
    sem = asyncio.Semaphore(_CONCURRENCY)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id, s.endpoint_url, s.coverage_tier
            FROM services s
            WHERE s.coverage_tier < 2
              AND NOT EXISTS (
                  SELECT 1 FROM service_probes p
                  WHERE p.service_id = s.id AND p.reachable = TRUE
              )
            ORDER BY s.coverage_tier DESC, s.created_at ASC
            """
        )
    services = [dict(r) for r in rows]
    logger.info("Bulk prober: %d unverified services queued", len(services))

    alive_count = 0
    dead_count = 0
    error_count = 0
    upgraded_count = 0  # Tier 0 → 1 (because alive)
    _lock = asyncio.Lock()

    async with httpx.AsyncClient(follow_redirects=True, timeout=_TIMEOUT_S) as client:

        async def _handle(svc: dict) -> None:
            nonlocal alive_count, dead_count, error_count, upgraded_count
            async with sem:
                url = svc["endpoint_url"]
                alive, status, elapsed_ms, error = await _probe_one_url(url, client, limiter)

                async with pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO service_probes
                            (service_id, reachable, response_time_ms, status_code, error_message)
                        VALUES ($1, $2, $3, $4, $5)
                        """,
                        svc["id"], alive, elapsed_ms, status, error,
                    )
                    if alive:
                        if svc["coverage_tier"] == 0:
                            await conn.execute(
                                """
                                UPDATE services
                                SET coverage_tier = 1,
                                    schema_validated = TRUE,
                                    last_tested_at = NOW(),
                                    updated_at = NOW()
                                WHERE id = $1
                                """,
                                svc["id"],
                            )
                        else:
                            await conn.execute(
                                """
                                UPDATE services
                                SET last_tested_at = NOW(), updated_at = NOW()
                                WHERE id = $1
                                """,
                                svc["id"],
                            )

                async with _lock:
                    if alive:
                        alive_count += 1
                        if svc["coverage_tier"] == 0:
                            upgraded_count += 1
                    elif error:
                        error_count += 1
                    else:
                        dead_count += 1

                level = logging.DEBUG if alive else logging.DEBUG
                logger.log(level, "%s %s %s", "ALIVE" if alive else "DEAD ", status, url)

        await asyncio.gather(*[_handle(s) for s in services], return_exceptions=True)

    await pool.close()

    counts = {
        "total": len(services),
        "alive": alive_count,
        "dead": dead_count,
        "errors": error_count,
        "tier0_upgraded": upgraded_count,
    }
    print(
        f"\nBulk probe complete:"
        f"\n  Probed:        {counts['total']}"
        f"\n  Alive:         {counts['alive']}  (200-299 or 401/403)"
        f"\n  Dead/timeout:  {counts['dead'] + counts['errors']}"
        f"\n  Tier 0 → 1:    {counts['tier0_upgraded']}"
    )
    return counts


if __name__ == "__main__":
    asyncio.run(run_bulk_probe(_ASYNCPG_URL))
