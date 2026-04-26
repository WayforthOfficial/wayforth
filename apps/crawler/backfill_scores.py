import asyncio
import os
import sys

import asyncpg


async def backfill():
    db = await asyncpg.connect(os.environ["DATABASE_URL"])

    services = await db.fetch("""
        SELECT id, name, endpoint_url, coverage_tier,
               consecutive_failures, payment_protocol, last_tested_at
        FROM services WHERE coverage_tier >= 1
    """)

    sys.path.insert(0, os.path.dirname(__file__))
    from health_monitor import compute_wri_simple

    inserted = 0
    for svc in services:
        wri = compute_wri_simple(dict(svc))
        for hours_ago in [18, 12, 6]:
            await db.execute("""
                INSERT INTO service_score_history
                (service_id, wri_score, tier, consecutive_failures, recorded_at)
                VALUES ($1, $2, $3, $4, NOW() - INTERVAL '1 hour' * $5)
                ON CONFLICT DO NOTHING
            """, str(svc['id']), wri, svc['coverage_tier'],
                svc.get('consecutive_failures', 0), hours_ago)
        inserted += 1

    print(f"Backfilled {inserted} services")
    await db.close()


asyncio.run(backfill())
