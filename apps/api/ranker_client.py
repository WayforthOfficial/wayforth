"""
Ranking client — proxies to the private ranking service.

When RANK_SERVICE_URL is set, ranking is delegated to the private service and its
results are returned verbatim. When it is not set (e.g. local dev), candidates are
ordered by a simple keyword-relevance heuristic only. No reliability-score formula
is computed here — score values originate from the database or the private service.
"""
import httpx, os, logging
logger = logging.getLogger("wayforth")

RANK_SERVICE_URL = os.getenv("RANK_SERVICE_URL", "")


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
    """Order candidates for a query. Delegates to the private ranking service when
    RANK_SERVICE_URL is configured; otherwise falls back to keyword relevance only.
    `db` is accepted for call-site compatibility but unused — signal aggregation and
    scoring live entirely in the private service, never here."""
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
            logger.warning(f"ranking service unavailable, using keyword fallback: {e}")

    return _keyword_rank(query, list(candidates))
