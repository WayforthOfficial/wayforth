"""tools/query.py — wayforth_query (WayforthQL)."""

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_instance import mcp, API_BASE, _get_api_key, _format_ranked_service


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def wayforth_query(
    query: str = Field(description="Natural language query describing the API you need (e.g. 'fast cheap translation', 'image generation under $0.01')"),
    tier_min: int = Field(default=2, description="Minimum coverage tier: 0=all, 1=tested, 2=verified (default), 3=premium"),
    category: str = Field(default=None, description="Filter by category: inference, translation, data, search, audio, image, etc."),
    price_max: float = Field(default=None, description="Maximum price per API call in USD (e.g. 0.001 for $0.001/call)"),
    protocol: str = Field(default=None, description="Payment protocol filter (e.g. 'x402' for HTTP 402 native services)"),
    sort_by: str = Field(default="wri", description="Sort order: 'wri' (WayforthRank score, default), 'price' (cheapest first), 'tier' (highest tier first)"),
    limit: int = Field(default=5, description="Number of results to return (1–50)"),
    x402_only: bool = Field(default=False, description="When true, return only x402-native services"),
    provider: str = Field(default=None, description="Filter by provider name substring (e.g. 'openai', 'google')"),
    verified_only: bool = Field(default=False, description="When true, return only tier-2+ verified services"),
    offset: int = Field(default=0, description="Pagination offset — skip this many results"),
) -> str:
    """Structured service discovery using WayforthQL v2.
    More precise than wayforth_search — supports price caps, protocol filters,
    x402-only, provider filtering, and explicit sort order.
    Use when you need deterministic filtering rather than pure semantic ranking.
    """
    api_key = _get_api_key()
    if not api_key:
        return "No API key provided. Get one free at wayforth.io — 100 credits, no card required."
    body: dict = {
        "query": query,
        "tier_min": tier_min,
        "limit": limit,
        "sort_by": sort_by,
        "x402_only": x402_only,
        "verified_only": verified_only,
        "offset": offset,
    }
    if category:
        body["category"] = category
    if price_max is not None:
        body["price_max"] = price_max
    if protocol:
        body["protocol"] = protocol
    if provider:
        body["provider"] = provider
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{API_BASE}/query",
                headers={"X-Wayforth-API-Key": api_key, "Content-Type": "application/json"},
                json=body,
            )
    except Exception as e:
        return f"Wayforth API not reachable: {e}"
    if resp.status_code == 402:
        d = resp.json().get("detail", {})
        return f"Insufficient credits. Balance: {d.get('balance', 0)}. Top up at wayforth.io/dashboard"
    if resp.status_code != 200:
        return f"WayforthQL error {resp.status_code}: {resp.text[:300]}"
    data = resp.json()
    results = data.get("results", [])
    if not results:
        return "No services matched your WayforthQL query. Try relaxing tier_min or price_max."
    lines = [f"WayforthQL results for: {query!r}\n"]
    for i, s in enumerate(results, 1):
        lines.append(_format_ranked_service(i, s))
    lines.append(f"\n{len(results)} result(s) · offset: {data.get('offset', 0)} · protocol: {data.get('protocol', 'WayforthQL/2.0')}")
    return "\n".join(lines)
