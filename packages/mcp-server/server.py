import os
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from ranker import rank_services

load_dotenv()

API_BASE = os.getenv("WAYFORTH_API_URL", "https://api-production-fd71.up.railway.app")

mcp = FastMCP("wayforth")

TIER_LABELS = {0: "free", 1: "basic", 2: "standard", 3: "premium"}


async def _fetch_services() -> list[dict] | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{API_BASE}/services")
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


def _score(service: dict, tokens: list[str]) -> int:
    haystack = f"{service.get('name', '')} {service.get('description', '')}".lower()
    return sum(1 for t in tokens if t in haystack)


def _format_ranked_service(idx: int, s: dict) -> str:
    tier = TIER_LABELS.get(s.get("coverage_tier", 0), str(s.get("coverage_tier")))
    price = s.get("pricing_usdc")
    price_str = f"${float(price):.4f}" if price is not None else "$0"
    score = s.get("score", 0)
    reason = s.get("reason", "")
    return (
        f"{idx}. {s['name']} (score: {score}) — Reason: {reason}\n"
        f"   Tier: {tier} | Price: {price_str} | Endpoint: {s.get('endpoint_url', 'N/A')}"
    )


def _format_service(s: dict) -> str:
    tier = TIER_LABELS.get(s.get("coverage_tier", 0), str(s.get("coverage_tier")))
    price = s.get("pricing_usdc")
    price_str = f"${float(price):.4f} USDC" if price is not None else "free"
    return (
        f"**{s['name']}** [{s.get('category', 'unknown')} / tier {tier}]\n"
        f"  {s.get('description') or 'No description'}\n"
        f"  Pricing: {price_str} | Endpoint: {s.get('endpoint_url', 'N/A')}"
    )


@mcp.tool()
async def wayforth_search(intent: str, category: str = None, max_tier: int = 2) -> str:
    """Search Wayforth for AI services matching a natural language intent.

    Args:
        intent: What you're looking for, e.g. "translate Spanish documents"
        category: Optional filter — inference, data, or translation
        max_tier: Maximum coverage tier to include (0=free, 1=basic, 2=standard, 3=premium)
    """
    services = await _fetch_services()
    if services is None:
        return (
            "Wayforth API is not reachable at "
            f"{API_BASE}. Start the API with:\n"
            "  cd apps/api && uv run uvicorn main:app --port 8000"
        )

    candidates = [
        s for s in services
        if (category is None or s.get("category") == category)
        and s.get("coverage_tier", 0) <= max_tier
    ]

    ranked = await rank_services(intent, candidates)
    top = ranked[:3]

    if not top:
        return f"No services found matching '{intent}'" + (
            f" in category '{category}'" if category else ""
        )

    lines = [f"Top {len(top)} result(s) for \"{intent}\":\n"]
    lines += [_format_ranked_service(i + 1, s) for i, s in enumerate(top)]
    return "\n\n".join(lines)


@mcp.tool()
async def wayforth_list(category: str = None) -> str:
    """List all services in the Wayforth catalog.

    Args:
        category: Optional filter — inference, data, or translation
    """
    services = await _fetch_services()
    if services is None:
        return (
            "Wayforth API is not reachable at "
            f"{API_BASE}. Start the API with:\n"
            "  cd apps/api && uv run uvicorn main:app --port 8000"
        )

    filtered = [
        s for s in services
        if category is None or s.get("category") == category
    ]

    if not filtered:
        return f"No services found" + (f" in category '{category}'" if category else "")

    header = "All Wayforth services" + (f" — category: {category}" if category else "")
    lines = [f"{header} ({len(filtered)} total):\n"]
    lines += [_format_service(s) for s in filtered]
    return "\n\n".join(lines)


@mcp.tool()
async def wayforth_status() -> str:
    """Return catalog stats: service counts by tier and category, plus API health."""
    services = await _fetch_services()
    if services is None:
        return (
            f"API health: UNREACHABLE ({API_BASE})\n"
            "Start with: cd apps/api && uv run uvicorn main:app --port 8000"
        )

    total = len(services)
    by_category: dict[str, int] = {}
    by_tier: dict[int, int] = {}

    for s in services:
        cat = s.get("category") or "unknown"
        by_category[cat] = by_category.get(cat, 0) + 1
        tier = s.get("coverage_tier", 0)
        by_tier[tier] = by_tier.get(tier, 0) + 1

    cat_lines = "\n".join(
        f"  {cat}: {count}" for cat, count in sorted(by_category.items())
    )
    tier_lines = "\n".join(
        f"  tier {t} ({TIER_LABELS.get(t, '?')}): {count}"
        for t, count in sorted(by_tier.items())
    )

    return (
        f"API health: OK ({API_BASE})\n"
        f"Total services: {total}\n\n"
        f"By category:\n{cat_lines}\n\n"
        f"By tier:\n{tier_lines}"
    )


if __name__ == "__main__":
    mcp.run()
