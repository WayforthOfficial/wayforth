import logging

import httpx
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
logger = logging.getLogger(__name__)

_DDG_PRIMARY = "https://ddg-api.herokuapp.com/search"
_DDG_FALLBACK = "https://api.duckduckgo.com/"


def _parse_ddg_instant(data: dict, limit: int) -> list[dict]:
    results = []
    for item in data.get("RelatedTopics", []):
        if len(results) >= limit:
            break
        if "Topics" in item:
            continue
        url = item.get("FirstURL", "")
        text = item.get("Text", "")
        if not url or not text:
            continue
        if " - " in text:
            title, snippet = text.split(" - ", 1)
        else:
            title, snippet = text, text
        results.append({"title": title.strip(), "url": url, "snippet": snippet.strip()})
    return results


@router.get("/search")
async def search(
    q: str = Query(...),
    limit: int = Query(default=5, ge=1, le=20),
):
    results = None

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(_DDG_PRIMARY, params={"query": q, "limit": limit})
            resp.raise_for_status()
            primary_data = resp.json()
            if isinstance(primary_data, list) and primary_data:
                results = [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("link", ""),
                        "snippet": r.get("snippet", ""),
                    }
                    for r in primary_data[:limit]
                ]
    except Exception as exc:
        logger.warning("Primary DDG API failed (%s), falling back", exc)

    if not results:
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(
                    _DDG_FALLBACK,
                    params={"q": q, "format": "json", "no_html": "1"},
                )
                resp.raise_for_status()
                fallback_data = resp.json()
                results = _parse_ddg_instant(fallback_data, limit)
        except Exception as exc:
            logger.warning("Fallback DDG API also failed: %s", exc)

    if results is None:
        raise HTTPException(
            status_code=503,
            detail="Search unavailable: both DDG endpoints failed",
        )

    return {
        "query": q,
        "results": results or [],
        "service": "wayforth-labs-search",
    }
