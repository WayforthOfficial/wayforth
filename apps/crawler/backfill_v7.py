import asyncio, asyncpg, os, hashlib

async def backfill():
    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    services = await db.fetch("""
        SELECT id, name, endpoint_url, coverage_tier,
               consecutive_failures, payment_protocol
        FROM services WHERE source = 'curated_v7' AND coverage_tier >= 1
    """)
    inserted = 0
    for svc in services:
        hex_id = "0x" + hashlib.sha256(svc['endpoint_url'].encode()).hexdigest()
        score = 50.0
        tier = svc['coverage_tier']
        if tier >= 2: score += 20
        elif tier >= 1: score += 5
        if svc.get('consecutive_failures', 1) == 0: score += 20
        if svc.get('payment_protocol') == 'x402': score += 5
        wri = round(min(score, 100), 1)
        for hours_ago in [18, 12, 6]:
            await db.execute("""
                INSERT INTO service_score_history
                (service_id, wri_score, tier, consecutive_failures, recorded_at)
                VALUES ($1, $2, $3, $4, NOW() - ($5 || ' hours')::INTERVAL)
                ON CONFLICT DO NOTHING
            """, hex_id, wri, tier, svc.get('consecutive_failures', 0), str(hours_ago))
        inserted += 1
        print(f"  {svc['name']}: wri={wri}")
    print(f"\nDone. Backfilled {inserted} v7 services")
    await db.close()

asyncio.run(backfill())
