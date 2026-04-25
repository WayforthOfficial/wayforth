"""
x402 Monitor — tracks the x402 ecosystem and indexes new services.
Runs on each crawler cycle. Stores signals in competitive_intelligence table.
"""
import asyncio
import httpx
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("wayforth.x402_monitor")

X402_SERVICES = [
    {"name": "OpenAI via x402", "endpoint": "https://api.openai.com/v1", "category": "inference"},
    {"name": "Venice AI", "endpoint": "https://api.venice.ai/api/v1", "category": "inference"},
    {"name": "Hyperbolic GPU", "endpoint": "https://api.hyperbolic.xyz/v1", "category": "inference"},
    {"name": "CoinGecko via x402", "endpoint": "https://api.coingecko.com/api/v3", "category": "data"},
    {"name": "Exa Search via x402", "endpoint": "https://api.exa.ai/search", "category": "data"},
    {"name": "QuickNode via x402", "endpoint": "https://api.quicknode.com/v1", "category": "data"},
    {"name": "Alchemy via x402", "endpoint": "https://eth-mainnet.g.alchemy.com/v2", "category": "data"},
    {"name": "Bloomberg via x402", "endpoint": "https://api.bloomberg.com/v1", "category": "data"},
]


async def probe_x402_service(client: httpx.AsyncClient, service: dict) -> dict:
    """Probe an x402 service and record its status."""
    try:
        r = await client.get(service["endpoint"], timeout=8.0)
        # x402 services return 402 when no payment provided — that's success
        is_live = r.status_code in [200, 402, 401, 403]
        return {**service, "live": is_live, "status_code": r.status_code}
    except Exception as e:
        return {**service, "live": False, "error": str(e)}


async def run_x402_monitor(pool):
    async with pool.acquire() as db:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            results = []
            for svc in X402_SERVICES:
                result = await probe_x402_service(client, svc)
                results.append(result)
                logger.info(f"x402 probe: {svc['name']} — {'✅' if result['live'] else '❌'}")

            live_count = sum(1 for r in results if r["live"])

            await db.execute(
                """
                INSERT INTO competitive_intelligence (source, data, created_at)
                VALUES ('x402_monitor', $1, NOW())
                """,
                json.dumps({
                    "total_tracked": len(results),
                    "live_count": live_count,
                    "services": results,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }),
            )

            logger.info(f"x402 monitor: {live_count}/{len(results)} services live")
