import os
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

API_BASE = os.getenv("WAYFORTH_API_URL", "https://api-production-fd71.up.railway.app")

mcp = FastMCP("wayforth")

TIER_LABELS = {0: "free", 1: "basic", 2: "standard", 3: "premium"}


async def _fetch_services(category: str = None) -> list[dict] | None:
    """Paginate through /services to collect all results."""
    all_results: list[dict] = []
    offset = 0
    limit = 100
    params: dict = {"limit": limit}
    if category:
        params["category"] = category
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                params["offset"] = offset
                r = await client.get(f"{API_BASE}/services", params=params)
                r.raise_for_status()
                data = r.json()
                page = data.get("results", [])
                all_results.extend(page)
                if len(all_results) >= data.get("total", 0) or not page:
                    break
                offset += limit
    except Exception:
        return None
    return all_results


def _format_ranked_service(idx: int, s: dict) -> str:
    tier = TIER_LABELS.get(s.get("coverage_tier", 0), str(s.get("coverage_tier")))
    price = s.get("pricing_usdc")
    price_str = f"${float(price):.4f}" if price is not None else "$0"
    score = s.get("score", 0)
    reason = s.get("reason", "")
    service_id = s.get("service_id", "")
    sid_line = f"\n   Service ID: {service_id} (use with wayforth_pay)" if service_id else ""
    return (
        f"{idx}. {s['name']} (score: {score}) — Reason: {reason}\n"
        f"   Tier: {tier} | Price: {price_str} | Endpoint: {s.get('endpoint_url', 'N/A')}"
        f"{sid_line}"
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
async def wayforth_search(intent: str, category: str = None, limit: int = 3) -> str:
    """Search for agent-payable services by natural language intent.

    Args:
        intent: What you're looking for, e.g. "translate Spanish documents"
        category: Optional filter — inference, data, or translation
        limit: Number of results to return (default 3, max 20)
    """
    params = {"q": intent, "limit": limit}
    if category:
        params["category"] = category
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{API_BASE}/search", params=params)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return f"Wayforth API is not reachable at {API_BASE}."

    results = data.get("results", [])
    if not results:
        return f"No services found matching '{intent}'" + (
            f" in category '{category}'" if category else ""
        )

    lines = [f"Top {len(results)} result(s) for \"{intent}\":\n"]
    lines += [_format_ranked_service(i + 1, s) for i, s in enumerate(results)]
    return "\n\n".join(lines)


@mcp.tool()
async def wayforth_pay(
    service_id: str,
    service_owner: str,
    amount_usdc: float,
) -> str:
    """
    Get payment calldata to route USDC through Wayforth Escrow.
    Returns two transactions to broadcast: approve USDC, then routePayment.

    Args:
        service_id: bytes32 service identifier from wayforth_search
        service_owner: Ethereum address of the service owner
        amount_usdc: Amount in USDC (e.g. 1.0 for 1 USDC)
    """
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.post(
                f"{API_BASE}/pay",
                json={
                    "service_id": service_id,
                    "service_owner": service_owner,
                    "amount_usdc": amount_usdc,
                },
            )
            response.raise_for_status()
            data = response.json()

            steps = data["steps"]
            summary = data["summary"]

            return (
                f"Payment calldata for {summary['gross_usdc']} USDC "
                f"(fee: {summary['fee_usdc']} USDC, net: {summary['net_usdc']} USDC):\n\n"
                f"Step 1 — Approve USDC:\n"
                f"  To: {steps[0]['to']}\n"
                f"  Calldata: {steps[0]['calldata']}\n\n"
                f"Step 2 — Route Payment:\n"
                f"  To: {steps[1]['to']}\n"
                f"  Calldata: {steps[1]['calldata']}\n\n"
                f"Network: {summary['network']} | "
                f"Fee: {summary['fee_pct']}% | "
                f"USDC decimals: {summary['usdc_decimals']}"
            )
        except Exception as e:
            return f"Payment calldata error: {str(e)}"


@mcp.tool()
async def wayforth_list(category: str = None) -> str:
    """List services in the Wayforth catalog.

    Args:
        category: Optional filter — inference, data, or translation
    """
    services = await _fetch_services(category=category)
    if services is None:
        return f"Wayforth API is not reachable at {API_BASE}."

    if not services:
        return "No services found" + (f" in category '{category}'" if category else "")

    header = "Wayforth services" + (f" — category: {category}" if category else "")
    lines = [f"{header} ({len(services)} total):\n"]
    lines += [_format_service(s) for s in services]
    return "\n\n".join(lines)


@mcp.tool()
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


@mcp.tool()
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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
