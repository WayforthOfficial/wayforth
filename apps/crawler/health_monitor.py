"""
Wayforth Health Monitor
Runs every 6 hours via Railway cron (called from promoter.py).
Probes all Tier 2 services and auto-demotes if they fail 3 consecutive checks.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone

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


def compute_wri_simple(svc: dict) -> float:
    score = 50.0
    tier = svc.get("coverage_tier", 0)
    if tier >= 2:
        score += 20
    elif tier >= 1:
        score += 5
    if svc.get("consecutive_failures", 1) == 0:
        score += 20
    if svc.get("payment_protocol") == "x402":
        score += 5
    return round(min(score, 100), 1)


async def fire_tier_change_webhook(pool, service_id: str, old_tier: int, new_tier: int, service_name: str) -> None:
    async with pool.acquire() as conn:
        webhooks = await conn.fetch("""
            SELECT webhook_url, secret_token FROM provider_webhooks
            WHERE service_id = $1 AND active = TRUE AND 'tier_change' = ANY(events)
        """, service_id)

    for wh in webhooks:
        payload = {
            "event": "tier_change",
            "service_id": service_id,
            "service_name": service_name,
            "old_tier": old_tier,
            "new_tier": new_tier,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        body = json.dumps(payload)
        sig = hmac.new(wh["secret_token"].encode(), body.encode(), hashlib.sha256).hexdigest()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    wh["webhook_url"],
                    content=body,
                    headers={"Content-Type": "application/json", "X-Wayforth-Signature": f"sha256={sig}"},
                )
        except Exception as e:
            logger.warning(f"Webhook failed for {service_id}: {e}")


async def run_health_check(pool=None) -> None:
    """Probe all Tier 2 services and auto-demote after 3 consecutive failures."""
    _own_pool = pool is None
    if _own_pool:
        db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
        pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5)

    try:
        async with pool.acquire() as conn:
            services = await conn.fetch(
                "SELECT id, name, endpoint_url, consecutive_failures, coverage_tier, payment_protocol FROM services WHERE coverage_tier = 2"
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
                        snapshot = dict(svc)
                        snapshot["consecutive_failures"] = 0
                        await conn.execute("""
                            INSERT INTO service_score_history
                            (service_id, wri_score, tier, consecutive_failures, recorded_at)
                            VALUES ($1, $2, $3, $4, NOW())
                        """, str(svc["id"]), compute_wri_simple(snapshot), svc["coverage_tier"], 0)
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
                            snapshot = dict(svc)
                            snapshot["consecutive_failures"] = failures
                            snapshot["coverage_tier"] = 1
                            await conn.execute("""
                                INSERT INTO service_score_history
                                (service_id, wri_score, tier, consecutive_failures, recorded_at)
                                VALUES ($1, $2, $3, $4, NOW())
                            """, str(svc["id"]), compute_wri_simple(snapshot), 1, failures)
                            await fire_tier_change_webhook(pool, str(svc["id"]), 2, 1, svc["name"])
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
                            snapshot = dict(svc)
                            snapshot["consecutive_failures"] = failures
                            await conn.execute("""
                                INSERT INTO service_score_history
                                (service_id, wri_score, tier, consecutive_failures, recorded_at)
                                VALUES ($1, $2, $3, $4, NOW())
                            """, str(svc["id"]), compute_wri_simple(snapshot), svc["coverage_tier"], failures)
                            logger.warning(f"⚠️ {svc['name']} — DOWN ({failures}/3 failures)")

        logger.info("Health check complete")
    finally:
        if _own_pool:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(run_health_check())
