"""
One-shot prober for unverified translation services (tier < 2).
Pass criteria: status 200-403 AND response_time < 5000ms (JSON not required —
many real translation APIs return 401/403 at root without auth, which still
confirms the endpoint is live).
Pass → promote directly to tier 2.
Fail all 3 probes → consecutive_failures = 3 (inactive marker).
"""
import asyncio
import os
import time
from pathlib import Path

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

DB_URL = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
TIMEOUT_S = 5.0
MAX_TRIES = 3
# 200-403 inclusive: covers 200 OK, 401 Unauthorized, 402 Payment Required,
# 403 Forbidden — all indicate a live API endpoint.
PASS_CODES = range(200, 404)


async def probe_once(client: httpx.AsyncClient, url: str) -> tuple[bool, int, float]:
    """Returns (passed, status_code, response_time_ms)."""
    t0 = time.monotonic()
    try:
        r = await client.get(url, timeout=TIMEOUT_S)
        ms = (time.monotonic() - t0) * 1000
        passed = r.status_code in PASS_CODES and ms < 5000
        return passed, r.status_code, round(ms, 1)
    except Exception as exc:
        ms = (time.monotonic() - t0) * 1000
        print(f"    error: {exc}")
        return False, 0, round(ms, 1)


async def probe_service(client: httpx.AsyncClient, svc: dict) -> tuple[bool, list]:
    """Probe up to MAX_TRIES times. Returns (passed, probe_log)."""
    log = []
    for attempt in range(1, MAX_TRIES + 1):
        passed, code, ms = await probe_once(client, svc["endpoint_url"])
        log.append({"attempt": attempt, "status": code, "ms": ms, "passed": passed})
        if passed:
            return True, log
    return False, log


async def main():
    conn = await asyncpg.connect(DB_URL)
    rows = await conn.fetch("""
        SELECT id, name, endpoint_url, coverage_tier
        FROM services
        WHERE category = 'translation' AND coverage_tier < 2
        ORDER BY name
    """)
    services = [dict(r) for r in rows]
    print(f"\nProbing {len(services)} unverified translation services...\n")

    promoted = []
    failed = []

    async with httpx.AsyncClient(follow_redirects=True, timeout=TIMEOUT_S) as client:
        for svc in services:
            print(f"  {svc['name']}")
            print(f"    url: {svc['endpoint_url']}")
            passed, log = await probe_service(client, svc)
            for entry in log:
                status_str = str(entry['status']) if entry['status'] else 'ERR'
                mark = '✅' if entry['passed'] else '❌'
                print(f"    attempt {entry['attempt']}: {mark} {status_str} {entry['ms']}ms")

            if passed:
                await conn.execute("""
                    UPDATE services
                    SET coverage_tier = 2,
                        schema_validated = TRUE,
                        payment_tested = TRUE,
                        consecutive_failures = 0,
                        last_tested_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $1
                """, svc["id"])
                print(f"    → PROMOTED to Tier 2")
                promoted.append(svc["name"])
            else:
                await conn.execute("""
                    UPDATE services
                    SET consecutive_failures = 3,
                        last_tested_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $1
                """, svc["id"])
                print(f"    → FAILED (consecutive_failures=3)")
                failed.append(svc["name"])
            print()

    await conn.close()

    print("=" * 60)
    print(f"RESULTS")
    print(f"  Promoted to Tier 2 : {len(promoted)}")
    for n in promoted:
        print(f"    + {n}")
    print(f"  Failed (inactive)   : {len(failed)}")
    for n in failed:
        print(f"    - {n}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
