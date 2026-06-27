import asyncio, asyncpg, os, hashlib

async def fix():
    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    services = await db.fetch("""
        SELECT name, endpoint_url, coverage_tier, consecutive_failures, payment_protocol
        FROM services WHERE coverage_tier >= 2
    """)
    inserted = 0
    for svc in services:
        hex_id = "0x" + hashlib.sha256(svc['endpoint_url'].encode()).hexdigest()
        # Interim placeholder; authoritative scoring is the private rank service
        # (RANK_SERVICE_URL), recalculated via promoter.run_rank_recalculate.
        wri = 50.0
        for h in [18, 12, 6]:
            await db.execute("""
                INSERT INTO service_score_history
                (service_id, wri_score, tier, consecutive_failures, recorded_at)
                VALUES ($1, $2, $3, $4, NOW() - ($5 || ' hours')::INTERVAL)
                ON CONFLICT DO NOTHING
            """, hex_id, wri, tier, svc.get('consecutive_failures') or 0, str(h))
        inserted += 1
    print(f"Backfilled {inserted} services")
    await db.close()

asyncio.run(fix())
