"""tools/compare.py — wayforth_compare, wayforth_similar."""

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_instance import mcp, API_BASE, _get_api_key


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def wayforth_compare(
    slugs: str = Field(
        description="Comma-separated service slugs to compare. Example: 'groq,together,perplexity'. Compare 2-5 services at a time."
    ),
    query: str = Field(
        default="",
        description="Optional: describe your use case to get a relevance score per service. Example: 'fast inference for my chatbot'",
    ),
) -> str:
    """
    Compare 2-5 API services side by side.
    Returns reliability scores, response times, costs, signal counts and a plain-English recommendation.
    Use this before committing to a service.
    """
    api_key = _get_api_key()
    if not api_key:
        return "No API key set. Export WAYFORTH_API_KEY=wf_live_..."
    params: dict = {"slugs": slugs}
    if query:
        params["query"] = query
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{API_BASE}/compare",
            params=params,
            headers={"X-Wayforth-API-Key": api_key},
        )
    if r.status_code != 200:
        return f"Compare error {r.status_code}: {r.text[:300]}"
    d = r.json()
    lines = [f"Comparing {len(d['services'])} services" + (f" for: \"{d['query']}\"" if d.get("query") else "")]
    lines.append("")
    for svc in d["services"]:
        verdict = f"  [{svc['verdict']}]" if svc.get("verdict") else ""
        wri = f"Reliability {svc['wri_score']}" if svc.get("wri_score") else "Reliability —"
        signals = f"  signals={svc['total_signals']}" if svc.get("total_signals") else ""
        ms = f"  {svc['avg_response_ms']}ms" if svc.get("avg_response_ms") else ""
        credits = f"  {svc['credits_per_call']} credits/call" if svc.get("credits_per_call") else ""
        rel = f"  relevance={svc['relevance_score']}" if svc.get("relevance_score") else ""
        lines.append(f"  #{svc['rank']} {svc['name']:<20} {wri}{signals}{ms}{credits}{rel}{verdict}")
    rec = d.get("recommendation", {})
    if rec:
        lines.append("")
        lines.append(f"Recommendation: {rec['reason']}")
    matrix = d.get("comparison_matrix", {})
    if any(matrix.values()):
        lines.append("")
        lines.append("Matrix:")
        for k, v in matrix.items():
            if v:
                lines.append(f"  {k}: {v}")
    if d.get("not_found"):
        lines.append(f"\nNot found: {', '.join(d['not_found'])}")
    return "\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def wayforth_similar(
    service_id: str = Field(description="Service ID or wayforth:// identifier to find co-used services for"),
) -> str:
    """Find services similar to or commonly used alongside a given service.
    Returns co-usage patterns from real agent behavior.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{API_BASE}/graph/{service_id}")
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        return f"Could not fetch graph for {service_id}: {e}"

    related = data.get("related_services", [])
    if not related:
        return f"No co-usage data found for service {service_id}."

    lines = [f"Services co-used with {service_id}:"]
    for svc in related:
        lines.append(
            f"  • {svc['name']} ({svc.get('category', '')}) — {svc.get('co_search_count', 0)} co-searches"
        )
    return "\n".join(lines)
