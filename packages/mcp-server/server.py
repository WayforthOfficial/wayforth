import json
import os
import httpx
from datetime import datetime, timezone
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

API_BASE = os.getenv("WAYFORTH_API_URL", "https://gateway.wayforth.io")
WAYFORTH_API_KEY = os.getenv("WAYFORTH_API_KEY", "")

mcp = FastMCP("wayforth")

TIER_LABELS = {0: "free", 1: "basic", 2: "standard", 3: "premium"}

MEMORY_FILE = os.path.expanduser("~/.wayforth_memory.json")

_MANAGED_SLUGS = {"groq", "deepl", "openweather", "newsapi", "serper", "resend", "assemblyai", "stability"}

_CATEGORY_PARAMS = {
    "translation": '{"text": "Hello world", "target_lang": "ES"}',
    "inference": '{"messages": [{"role": "user", "content": "Say hello"}]}',
    "data": '{"q": "New York"}',
    "search": '{"q": "your search query"}',
    "image": '{"prompt": "a futuristic city at night"}',
    "audio": '{"audio_url": "https://assembly.ai/sports_injuries.mp3"}',
    "communication": '{"from": "noreply@wayforth.io", "to": "you@example.com", "subject": "Test", "html": "<p>Hello</p>"}',
}

_EXECUTE_NEXT_QUERIES = {
    "groq": ["run code analysis", "summarize a document", "answer a question"],
    "deepl": ["translate to French", "translate to Japanese", "translate to German"],
    "openweather": ["weather in London", "weather in Tokyo", "weather in Paris"],
    "newsapi": ["latest AI news", "crypto news today", "tech startup news"],
    "serper": ["search for competitors", "find API documentation", "search recent papers"],
    "resend": ["send a newsletter", "send a confirmation email", "send an alert"],
    "assemblyai": ["transcribe a podcast", "transcribe a meeting", "transcribe an interview"],
    "stability": ["generate a logo", "generate a landscape", "generate a portrait"],
}
_EXECUTE_NEXT_DEFAULT = ["search for translation APIs", "search for inference APIs", "search for data APIs"]


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
    price = (s.get("pricing") or {}).get("per_call_usd") or s.get("pricing_usdc")
    price_str = f"${float(price):.4f}/req" if price else "free"
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
    price = (s.get("pricing") or {}).get("per_call_usd") or s.get("pricing_usdc")
    price_str = f"${float(price):.4f}/req" if price else "free"
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
            f'wayforth_keys_add(\n'
            f'  service_slug="{slug}",\n'
            f'  service_name="{top_name}",\n'
            f'  api_key="your_api_key_here"\n'
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


@mcp.tool()
async def wayforth_pay(
    service_id: str,
    amount_usd: float,
    track: str = "card",
    query_id: str = None,
) -> str:
    """
    Pay for a service through Wayforth.

    Two payment tracks:

    track='card' (default — no crypto needed):
      - Deducts from your Wayforth credit balance
      - Stripe Treasury processes the payment to the service
      - Get credits at wayforth.io/dashboard → Billing → Card Track

    track='crypto' (non-custodial — your wallet):
      - Returns Base calldata for you to broadcast
      - Your USDC, your wallet, fully on-chain
      - Non-custodial — Wayforth never holds your funds

    Routing fee: 1.5% on all tracks
    30% of fee allocated to $WAYF burn

    Args:
        service_id: Service to pay (from wayforth_search results)
        amount_usd: Amount to pay in USD (e.g. 0.001)
        track: Payment track — 'card' or 'crypto' (default: 'card')
        query_id: Optional query ID from wayforth_search for WayforthRank signal
    """
    api_key = os.environ.get("WAYFORTH_API_KEY", "")
    if not api_key:
        return "Error: WAYFORTH_API_KEY not set. Get your free key at wayforth.io/dashboard"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{API_BASE}/pay",
            headers={
                "X-Wayforth-API-Key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "service_id": service_id,
                "amount_usd": amount_usd,
                "track": track,
                "query_id": query_id,
            },
        )

    if resp.status_code == 402:
        data = resp.json().get("detail", {})
        balance = data.get("credits_balance", 0)
        needed = data.get("credits_needed", 0)
        return (
            f"Insufficient credits for card track.\n"
            f"Balance: {balance} credits. Needed: {needed} credits.\n"
            f"Top up at wayforth.io/dashboard\n"
            f"Or switch to crypto track: wayforth_pay('{service_id}', {amount_usd}, track='crypto')"
        )

    if resp.status_code != 200:
        return f"Payment error {resp.status_code}: {resp.text[:200]}"

    data = resp.json()
    payment_track = data.get("payment_track", "unknown")
    service_name = data.get("service_name", service_id)
    routing_fee = data.get("routing_fee_usd", 0)
    burn = data.get("wayf_burn_allocation_usd", 0)

    if payment_track == "card":
        credits_left = data.get("credits_remaining", 0)
        tx_ref = data.get("tx_ref", "")
        return (
            f"Card payment processed — {service_name}\n\n"
            f"Amount: ${amount_usd}\n"
            f"Routing fee (1.5%): ${routing_fee}\n"
            f"$WAYF burn allocation: ${burn}\n"
            f"Credits deducted: {data.get('credits_deducted', 0)}\n"
            f"Credits remaining: {credits_left}\n"
            f"Reference: {tx_ref}"
        )

    if payment_track == "crypto":
        return (
            f"Crypto calldata ready — {service_name}\n\n"
            f"Amount: ${amount_usd} USDC\n"
            f"Routing fee (1.5%): ${routing_fee}\n"
            f"$WAYF burn allocation: ${burn}\n"
            f"Network: Base Sepolia\n\n"
            f"Step 1 — Approve USDC:\n{data.get('approve_calldata', '')}\n\n"
            f"Step 2 — Route payment:\n{data.get('payment_calldata', '')}\n\n"
            f"Broadcast both transactions from your wallet."
        )

    if payment_track == "x402":
        return (
            f"x402 payment ready — {service_name}\n\n"
            f"Amount: ${amount_usd} USDC\n"
            f"Routing fee (1.5%): ${routing_fee}\n"
            f"Protocol: x402 via Coinbase facilitator\n\n"
            f"Use x402 client library to complete payment."
        )

    return f"Payment processed: {data}"



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
async def wayforth_execute(service_slug: str, params: dict, key_source: str = "managed") -> str:
    """Execute a real API call through Wayforth managed services or your own BYOK key.
    Returns real results — translation, inference, weather, search, email, audio, images.
    Credits deducted on success only.

    Args:
        service_slug: Service to call: groq, deepl, openweather, newsapi, serper, resend, assemblyai, stability
        params: Parameters for the service. Varies by service.
        key_source: Use Wayforth managed key ('managed', default) or your own BYOK key ('byok')
    """
    api_key = os.environ.get("WAYFORTH_API_KEY", "")
    if not api_key:
        return "Error: WAYFORTH_API_KEY not set. Get your free key at wayforth.io/dashboard"
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{API_BASE}/execute",
                headers={"X-Wayforth-API-Key": api_key, "Content-Type": "application/json"},
                json={"service_slug": service_slug, "params": params, "key_source": key_source},
            )
    except Exception as e:
        return f"Wayforth API not reachable: {e}"
    if resp.status_code == 402:
        d = resp.json().get("detail", {})
        return (
            f"Insufficient credits. Balance: {d.get('credits_balance', 0)}, "
            f"needed: {d.get('credits_needed', 0)}. Top up at wayforth.io/dashboard"
        )
    if resp.status_code == 404:
        return resp.json().get("detail", {}).get("error", "Service key not found. Add one at /call/keys/add")
    if resp.status_code != 200:
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict) and detail.get("status") == "error":
            return f"Service error ({service_slug}): {detail.get('error', 'unknown')}"
        return f"Execute error {resp.status_code}: {resp.text[:300]}"
    data = resp.json()
    result_str = json.dumps({
        "service": data.get("service"),
        "result": data.get("result"),
        "credits_deducted": data.get("credits_deducted"),
        "credits_remaining": data.get("credits_remaining"),
        "execution_ms": data.get("execution_ms"),
    }, indent=2)

    credits_ded = data.get("credits_deducted", "?")
    credits_rem = data.get("credits_remaining", "—")
    queries = _EXECUTE_NEXT_QUERIES.get(service_slug, _EXECUTE_NEXT_DEFAULT)
    suggestions = (
        f"\n---\n"
        f"✅ Done · {credits_ded} credit(s) used · {credits_rem} remaining\n\n"
        f"Try next:\n"
        + "".join(f'- wayforth_search("{q}")\n' for q in queries)
        + "---"
    )
    return result_str + suggestions


@mcp.tool()
async def wayforth_quickstart() -> str:
    """
    Get the Wayforth developer quickstart guide.
    Returns step-by-step instructions for using Wayforth in an agent.
    """
    return """Wayforth — Discovery and Payment Rail for AI Agents

STEP 1 — Install (one time):
  uvx wayforth-mcp
  # or: claude mcp add wayforth -- uvx wayforth-mcp

  Set your API key:
  export WAYFORTH_API_KEY=wf_live_...
  (Get free key at wayforth.io/dashboard)

STEP 2 — Discover:
  wayforth_search("translate text to Spanish")
  → DeepL        WRI:82  Tier 2  $0.00003/call
  → LibreTranslate  WRI:71  Tier 2  Free

STEP 3 — Pay (choose your track):

  Card track (no crypto needed):
    wayforth_pay("deepl", 0.001, track="card")
    → Credits deducted, Stripe Treasury pays service

  Crypto track (your wallet, non-custodial):
    wayforth_pay("deepl", 0.001, track="crypto")
    → Returns Base calldata → broadcast from your wallet

PAYMENT SETUP:
  Card:   wayforth.io/dashboard → Billing → Card Track → Add card
  Crypto: Fund a Base wallet with USDC, connect at wayforth.io/dashboard

CREDITS (for search quota):
  1 credit = $0.001 USD
  2,000 free on signup
  Buy more: $19/50K · $99/300K · $299/1M

ROUTING FEE: 1.5% on all payments
  30% → $WAYF burn (post-mainnet)
  70% → Wayforth operations

270+ verified APIs · 232+ Tier 2 · 18 categories

All tools:
  wayforth_search      — semantic service discovery
  wayforth_pay         — pay via card or crypto (dual-track)
  wayforth_list        — browse catalog with filters
  wayforth_similar     — co-used services (Service Graph)
  wayforth_identity    — agent trust score and reputation
  wayforth_remember    — save a service to agent memory
  wayforth_recall      — retrieve saved services
  wayforth_stats       — catalog statistics
  wayforth_status      — API health check
  wayforth_quickstart  — this guide
"""


def _fetch_credits_sync() -> int | None:
    api_key = os.getenv("WAYFORTH_API_KEY", "")
    if not api_key:
        return None
    try:
        with httpx.Client(timeout=5.0) as client:
            r = client.get(
                f"{API_BASE}/keys/usage",
                headers={"X-Wayforth-API-Key": api_key},
            )
            if r.status_code == 200:
                d = r.json()
                return d.get("credits_balance") or d.get("credits_remaining")
    except Exception:
        pass
    return None


def main():
    import sys
    banner = (
        "╔════════════════════════════════════════╗\n"
        "║         WAYFORTH MCP SERVER            ║\n"
        "║   Search · Pay · Execute · Repeat      ║\n"
        "╚════════════════════════════════════════╝"
    )
    print(banner, file=sys.stderr)

    api_key = os.getenv("WAYFORTH_API_KEY", "")
    if api_key:
        credits = _fetch_credits_sync()
        if credits is not None:
            print(f"\nYour credits: {credits} · wayforth.io/dashboard", file=sys.stderr)
        else:
            print("\nCredits unavailable · wayforth.io/dashboard", file=sys.stderr)
        print(
            '\nTry this first:\n'
            '  wayforth_search("translate text to Spanish")\n\n'
            'Then:\n'
            '  wayforth_execute(service_slug="deepl",\n'
            '                   params={"text": "Hello", "target_lang": "ES"},\n'
            '                   key_source="managed")\n\n'
            'Need more credits? → wayforth.io/pricing\n',
            file=sys.stderr,
        )
    else:
        print(
            '\n  Set your API key: export WAYFORTH_API_KEY=wf_live_...\n'
            '  Get one free at: wayforth.io/signup\n',
            file=sys.stderr,
        )

    mcp.run()


if __name__ == "__main__":
    main()
