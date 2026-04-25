"""
WayforthRank client — abstracts the ranking engine.
When RANK_SERVICE_URL is set, calls the private WayforthRank service.
Falls back to local ranking if unavailable.
"""
import httpx, os, logging
logger = logging.getLogger("wayforth")

RANK_SERVICE_URL = os.getenv("RANK_SERVICE_URL", "")

async def rank_services(query: str, candidates: list[dict]) -> list[dict]:
    if RANK_SERVICE_URL:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.post(
                    f"{RANK_SERVICE_URL}/rank",
                    json={"query": query, "candidates": candidates}
                )
                r.raise_for_status()
                return r.json()["results"]
        except Exception as e:
            logger.warning(f"WayforthRank service unavailable, using local: {e}")
    from ranker import rank_services_local
    return await rank_services_local(query, candidates)
