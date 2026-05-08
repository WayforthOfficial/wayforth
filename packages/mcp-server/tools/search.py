"""tools/search.py — wayforth_search, wayforth_list, wayforth_stats, wayforth_status."""

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_instance import (
    mcp, API_BASE,
    TIER_LABELS, _MANAGED_SLUGS, _CATEGORY_PARAMS,
    _fetch_services, _format_service,
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def wayforth_search(
    query: str = Field(description="Natural language description of the API service you need (e.g. 'translate text to Spanish', 'generate images')"),
    limit: int = Field(default=5, description="Number of results to return (1–20)"),
    tier_min: int = Field(default=2, description="Minimum coverage tier: 0=all, 1=tested, 2=verified (default), 3=premium"),
    category: str = Field(default=None, description="Optional category filter: inference, data, translation, image, code, audio, embeddings"),
) -> str:
    """Search 300+ verified APIs ranked by
    WayforthRank — real agent payment signals,
    not ads. Use this when you don't know
    which service to execute.
    """
    params = {"q": query, "limit": min(limit, 20), "tier": tier_min}
    if category:
        params["category"] = category
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{API_BASE}/search", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return f"Wayforth API is not reachable at {API_BASE}."

    results = data.get("results", [])
    if not results:
        return f"No services found for '{query}'. Try broader terms or set tier_min=0."

    lines = [f"Found {len(results)} services for '{query}':\n"]
    for i, svc in enumerate(results, 1):
        tier = svc.get("coverage_tier", 0)
        tier_label = "✅ Tier 2 Verified" if tier >= 2 else f"Tier {tier}"
        price = (svc.get("pricing") or {}).get("per_call_usd") or svc.get("pricing_usdc")
        price_str = f"${float(price):.6f}/req" if price else "Free"
        wri = svc.get("wri", "N/A")
        wayforth_id = svc.get("wayforth_id", "")
        lines.append(
            f"{i}. {svc['name']} — Score: {svc.get('score', 0)}/100 | WRI: {wri} | {tier_label}\n"
            f"   {svc.get('reason', svc.get('description', ''))[:120]}\n"
            f"   Price: {price_str} | ID: {wayforth_id}\n"
            f"   To pay: wayforth_pay(service_id='{svc.get('service_id', '')}', ...)\n"
        )

    lines.append(f"\nQuery ID: {data.get('query_id', '')} (use in wayforth_pay for conversion tracking)")

    top = results[0]
    wayforth_id = top.get("wayforth_id", "")
    slug = wayforth_id.split("://")[1].split("/")[0] if "://" in wayforth_id else top.get("service_id", "unknown")
    top_name = top.get("name", "")
    top_wri = top.get("wri", "N/A")
    cat_key = (top.get("category") or "").lower().split("/")[0]
    raw_credits = data.get("credits_remaining") or data.get("credits_balance")

    if slug in _MANAGED_SLUGS:
        example_params = _CATEGORY_PARAMS.get(cat_key, "{}")
        credits_per_call = (top.get("pricing") or {}).get("credits_per_call", 1)
        credits_line = (
            f"💳 Cost: {credits_per_call} credit(s) · You have {raw_credits} remaining."
            if raw_credits is not None
            else f"💳 Cost: {credits_per_call} credit(s) · Check balance at wayforth.io/dashboard"
        )
        next_step = (
            f"\n---\n"
            f"⚡ Top pick: {top_name} (WRI: {top_wri})\n\n"
            f"Run this to execute instantly:\n"
            f'wayforth_execute(\n'
            f'  service_slug="{slug}",\n'
            f'  params={example_params},\n'
            f'  key_source="managed"\n'
            f')\n\n'
            f"{credits_line}\n"
            f"---"
        )
    else:
        next_step = (
            f"\n---\n"
            f"⚡ Top pick: {top_name} (WRI: {top_wri})\n\n"
            f"To execute this service, add your own API key:\n"
            f'wayforth_keys(\n'
            f'  action="add",\n'
            f'  service_slug="{slug}",\n'
            f'  service_name="{top_name}",\n'
            f'  api_key_value="your_api_key_here"\n'
            f')\n'
            f'Then call: wayforth_execute(service_slug="{slug}",\n'
            f'                            params={{...}},\n'
            f'                            key_source="byok")\n\n'
            f'Or search for a managed alternative:\n'
            f'wayforth_search("{cat_key} API managed")\n'
            f"---"
        )

    lines.append(next_step)
    return "\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def wayforth_list(
    category: str = Field(default=None, description="Filter by category: inference, data, translation, image, code, audio, embeddings"),
    tier_min: int = Field(default=2, description="Minimum coverage tier: 0=all, 1=tested, 2=verified (default), 3=premium"),
    limit: int = Field(default=10, description="Number of services to return (1–100)"),
) -> str:
    """Browse the Wayforth service catalog."""
    services = await _fetch_services(category=category, tier_min=tier_min, limit=limit)
    if services is None:
        return f"Wayforth API is not reachable at {API_BASE}."

    if not services:
        return "No services found" + (f" in category '{category}'" if category else "")

    header = "Wayforth services" + (f" — category: {category}" if category else "")
    lines = [f"{header} ({len(services)} total):\n"]
    lines += [_format_service(s) for s in services]
    return "\n\n".join(lines)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def wayforth_stats() -> str:
    """Get current Wayforth catalog statistics."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{API_BASE}/stats")
            r.raise_for_status()
            data = r.json()
    except Exception:
        return f"Wayforth API is not reachable at {API_BASE}."

    total = data.get("total_services", 0)
    by_tier = data.get("by_tier", {})
    by_category = data.get("by_category", {})
    last_updated = data.get("last_updated", "unknown")
    tier2_count = by_tier.get("2", 0)

    tier_lines = "\n".join(
        f"  Tier {t} ({TIER_LABELS.get(int(t), '?')}): {count}"
        for t, count in sorted(by_tier.items(), key=lambda x: int(x[0]))
    )
    cat_lines = "\n".join(
        f"  {cat}: {count}" for cat, count in sorted(by_category.items())
    )

    return (
        f"Wayforth catalog: {total:,} services total\n"
        f"{tier2_count} Tier 2 (executable)\n\n"
        f"By tier:\n{tier_lines}\n\n"
        f"By category:\n{cat_lines}\n\n"
        f"Last updated: {last_updated}"
    )


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def wayforth_status() -> str:
    """Return API health and catalog statistics."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            health_r = await client.get(f"{API_BASE}/health")
            health = health_r.json().get("status", "unknown") if health_r.status_code == 200 else "degraded"
            stats_r = await client.get(f"{API_BASE}/stats")
            stats_r.raise_for_status()
            data = stats_r.json()
    except Exception:
        return f"API health: UNREACHABLE ({API_BASE})"

    total = data.get("total_services", 0)
    by_category = data.get("by_category", {})
    by_tier = data.get("by_tier", {})

    cat_lines = "\n".join(
        f"  {cat}: {count}" for cat, count in sorted(by_category.items())
    )
    tier_lines = "\n".join(
        f"  tier {t} ({TIER_LABELS.get(int(t), '?')}): {count}"
        for t, count in sorted(by_tier.items(), key=lambda x: int(x[0]))
    )

    return (
        f"API health: {health} ({API_BASE})\n"
        f"Total services: {total}\n\n"
        f"By category:\n{cat_lines}\n\n"
        f"By tier:\n{tier_lines}"
    )
