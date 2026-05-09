"""
WayforthRank client — abstracts the ranking engine.
When RANK_SERVICE_URL is set, calls the private WayforthRank service.
Falls back to local ranking if unavailable.
"""
import httpx, os, logging
logger = logging.getLogger("wayforth")

RANK_SERVICE_URL = os.getenv("RANK_SERVICE_URL", "")

try:
    from ranker import rank_services_local as _rank_services_local
except ImportError:
    _rank_services_local = None


def _keyword_rank(query: str, services: list[dict]) -> list[dict]:
    tokens = [w.lower() for w in query.split() if len(w) > 2]

    def _score(s: dict) -> int:
        haystack = f"{s.get('name', '')} {s.get('description', '')}".lower()
        return sum(1 for t in tokens if t in haystack)

    ranked = sorted(services, key=_score, reverse=True)
    for s in ranked:
        s["score"] = _score(s) * 10
        s.setdefault("reason", "keyword match")
    return ranked


async def rank_services(query: str, candidates: list[dict], db=None) -> list[dict]:
    popularity_signals = {}
    payment_signals = {}
    if db and not RANK_SERVICE_URL:
        try:
            rows = await db.fetch("""
                SELECT top_result_id::text, COUNT(*) as c
                FROM search_analytics
                WHERE created_at > NOW() - INTERVAL '7 days'
                AND top_result_id IS NOT NULL
                GROUP BY top_result_id
                ORDER BY c DESC LIMIT 100
            """)
            if rows:
                max_c = max(r['c'] for r in rows)
                popularity_signals = {r['top_result_id']: (r['c'] / max_c) * 5.0 for r in rows}

            pay_rows = await db.fetch("""
                SELECT service_id::text, COUNT(*) as c
                FROM search_outcomes
                WHERE outcome_type = 'payment_initiated'
                AND created_at > NOW() - INTERVAL '7 days'
                AND service_id IS NOT NULL
                GROUP BY service_id ORDER BY c DESC LIMIT 100
            """)
            if pay_rows:
                max_p = max(r['c'] for r in pay_rows)
                payment_signals = {r['service_id']: (r['c'] / max_p) * 8.0 for r in pay_rows}
        except Exception as e:
            logger.warning(f"WayforthRank signal fetch failed: {e}")

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

    if _rank_services_local is not None:
        return await _rank_services_local(query, candidates, popularity_signals, payment_signals)

    return _keyword_rank(query, list(candidates))
