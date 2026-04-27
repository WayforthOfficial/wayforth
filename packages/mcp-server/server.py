import json
import os
import httpx
from datetime import datetime, timezone
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

API_BASE = os.getenv("WAYFORTH_API_URL", "https://gateway.wayforth.io")

mcp = FastMCP("wayforth")

TIER_LABELS = {0: "free", 1: "basic", 2: "standard", 3: "premium"}

MEMORY_FILE = os.path.expanduser("~/.wayforth_memory.json")


def _load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE) as f:
            return json.load(f)
    return {"services": []}


def _save_memory(data: dict) -> None:
    with open(MEMORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


async def _fetch_services(category: str = None, tier_min: int = None, limit: int = 100) -> list[dict] | None:
    """Paginate through /services to collect all results."""
    all_results: list[dict] = []
    offset = 0
    page_size = min(limit, 100)
    params: dict = {"limit": page_size}
    if category:
        params["category"] = category
    if tier_min is not None:
        params["tier"] = tier_min
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                params["offset"] = offset
                r = await client.get(f"{API_BASE}/services", params=params)
                r.raise_for_status()
                data = r.json()
                page = data.get("results", [])
                all_results.extend(page)
                if len(all_results) >= limit or len(all_results) >= data.get("total", 0) or not page:
                    break
                offset += page_size
    except Exception:
        return None
    return all_results[:limit]


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
async def wayforth_search(query: str, limit: int = 5, tier_min: int = 2, category: str = None) -> str:
    """Search the Wayforth catalog for agent-callable services.
    Returns services ranked by WayforthRank — combining semantic relevance,
    reliability history, and real agent usage signals.

    Args:
        query: Natural language description of what your agent needs
        limit: Number of results (default 5, max 20)
        tier_min: Minimum coverage tier (default 2 = verified only)
        category: Optional filter (inference/data/translation/image/code/audio/embeddings)
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
        price = svc.get("pricing_usdc")
        price_str = f"${price:.6f}/req" if price else "Free"
        wri = svc.get("wri", "N/A")
        wayforth_id = svc.get("wayforth_id", "")
        lines.append(
            f"{i}. {svc['name']} — Score: {svc.get('score', 0)}/100 | WRI: {wri} | {tier_label}\n"
            f"   {svc.get('reason', svc.get('description', ''))[:120]}\n"
            f"   Price: {price_str} | ID: {wayforth_id}\n"
            f"   To pay: wayforth_pay(service_id='{svc.get('service_id', '')}', ...)\n"
        )

    lines.append(f"\nQuery ID: {data.get('query_id', '')} (use in wayforth_pay for conversion tracking)")
    return "\n".join(lines)


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
async def wayforth_list(category: str = None, tier_min: int = 2, limit: int = 10) -> str:
    """Browse the Wayforth service catalog.

    Args:
        category: Filter by category (inference/data/translation/image/code/audio/embeddings)
        tier_min: Minimum tier (default 2 = verified only)
        limit: Results count (default 10)
    """
    services = await _fetch_services(category=category, tier_min=tier_min, limit=limit)
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


@mcp.tool()
async def wayforth_remember(service_id: str, service_name: str, note: str = "", agent_id: str = "") -> str:
    """Save a service to agent memory for quick access later.

    Args:
        service_id: The service ID or wayforth:// identifier
        service_name: Human-readable service name
        note: Optional note about why you saved this service
        agent_id: Agent identifier (wallet address or session ID) for cross-session persistence
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{API_BASE}/memory",
                json={"service_id": service_id, "service_name": service_name, "note": note, "agent_id": agent_id},
            )
            r.raise_for_status()
            data = r.json()
            return f"Saved '{data['service_name']}' to memory (persisted in Wayforth DB)."
    except Exception:
        # Fallback to local file if API unavailable
        mem = _load_memory()
        mem["services"] = [s for s in mem["services"] if s["service_id"] != service_id]
        mem["services"].append({
            "service_id": service_id,
            "service_name": service_name,
            "note": note,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        })
        _save_memory(mem)
        return f"Saved '{service_name}' to local memory (API unavailable). You have {len(mem['services'])} saved service(s)."


@mcp.tool()
async def wayforth_recall(query: str = "", agent_id: str = "") -> str:
    """Recall services previously saved to memory.

    Args:
        query: Optional filter — search saved services by name or note
        agent_id: Agent identifier used when saving — required to retrieve cross-session memories
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            params: dict = {}
            if agent_id:
                params["agent_id"] = agent_id
            if query:
                params["q"] = query
            r = await client.get(f"{API_BASE}/memory", params=params)
            r.raise_for_status()
            data = r.json()
            services = data.get("services", [])
            if not services:
                return "No saved services found. Use wayforth_search to find services and wayforth_remember to save them."
            lines = [f"- {s['service_name']} ({s['service_id']}) — {s.get('note', 'no note')}" for s in services]
            return f"Your saved services ({len(services)}):\n" + "\n".join(lines)
    except Exception:
        # Fallback to local file if API unavailable
        mem = _load_memory()
        services = mem["services"]
        if query:
            q = query.lower()
            services = [s for s in services if q in s["service_name"].lower() or q in s.get("note", "").lower()]
        if not services:
            return "No saved services found. Use wayforth_search to find services and wayforth_remember to save them."
        lines = [f"- {s['service_name']} ({s['service_id']}) — {s.get('note', 'no note')}" for s in services]
        return f"Your saved services ({len(services)}) [local fallback]:\n" + "\n".join(lines)


@mcp.tool()
async def wayforth_similar(service_id: str) -> str:
    """
    Find services similar to or commonly used alongside a given service.
    Returns co-usage patterns from real agent behavior.

    Args:
        service_id: The service ID or wayforth:// identifier to find similar services for
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


@mcp.tool()
async def wayforth_identity(agent_id: str) -> str:
    """
    Get or create your agent identity on Wayforth.
    Returns your trust score, reputation tier, and usage history.

    Args:
        agent_id: Your unique agent identifier (wallet address or any stable ID)
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{API_BASE}/identity/{agent_id}")
        if r.status_code == 200:
            d = r.json()
            return (
                f"Agent Identity: {d['agent_id'][:16]}...\n"
                f"Trust Score: {d['trust_score']}/100 ({d['reputation_tier']})\n"
                f"Searches: {d['total_searches']} | Payments: {d['total_payments']}\n"
                f"Member since: {d['member_since'][:10]}"
            )
        r2 = await client.post(
            f"{API_BASE}/identity/register",
            json={"agent_id": agent_id},
        )
        d2 = r2.json()
        return f"New identity registered. Trust score: {d2['trust_score']}/100. Start searching to build reputation."


@mcp.tool()
async def wayforth_quickstart() -> str:
    """
    Get the Wayforth developer quickstart guide.
    Returns step-by-step instructions for using Wayforth in an agent.
    """
    return """# Wayforth Quickstart

## What is Wayforth?
The search engine and payment rail for AI agents.
190+ verified API endpoints. 154 Tier 2 verified. Semantic intent ranking.

## Install
uvx wayforth-mcp

## Step 1 — Search
wayforth_search("translate text to Spanish")
→ Returns ranked services with WRI scores (0-100)
→ WRI = Wayforth Reliability Index — uptime + usage signals

## Step 2 — Pay
wayforth_pay(service_id, owner_address, amount_usdc)
→ Returns non-custodial Base transaction calldata
→ Settles in ~2 seconds. Routing fee: 0.75%-1.5%.
→ Currently on Base Sepolia testnet — no real funds needed.

## WayforthQL (structured queries)
POST https://gateway.wayforth.io/query
{"query": "fast inference", "tier_min": 2, "sort_by": "wri", "limit": 5}

## All 10 tools
wayforth_search      — semantic service discovery
wayforth_pay         — non-custodial payment calldata
wayforth_list        — browse catalog with filters
wayforth_similar     — co-used services (Service Graph)
wayforth_identity    — agent trust score and reputation
wayforth_remember    — save a service to agent memory
wayforth_recall      — retrieve saved services
wayforth_stats       — catalog statistics
wayforth_status      — API health check
wayforth_quickstart  — this guide

## Links
Demo: https://wayforth.io/demo
Docs: https://gateway.wayforth.io/docs
GitHub: https://github.com/WayforthOfficial/wayforth
"""


def main():
    mcp.run()


if __name__ == "__main__":
    main()
