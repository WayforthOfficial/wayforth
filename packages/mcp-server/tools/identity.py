"""tools/identity.py — wayforth_identity, wayforth_check_agent_identity, wayforth_set_wri_alert, wayforth_quickstart."""

import os

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_instance import mcp, API_BASE, _get_api_key

# NOTE: GATEWAY_URL is not defined in this codebase — this is a pre-existing condition
# preserved from the original server.py. These tools reference GATEWAY_URL from the
# outer scope at call time; if not set, a NameError will occur at runtime.


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def wayforth_identity(
    agent_id: str = Field(description="Your unique agent identifier — wallet address or any stable session ID"),
) -> str:
    """Get or create your agent identity on Wayforth.
    Returns your trust score, reputation tier, and usage history.
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def wayforth_check_agent_identity(
    wallet_address: str = Field(
        description="Base wallet address to look up (0x...). Returns tier, trust score, and activity history."
    ),
) -> str:
    """
    Look up an agent's identity and reputation on Wayforth.

    Returns tier (unknown/emerging/established/trusted/elite), trust score (0-100),
    call history, and cumulative spend.

    Agents build reputation automatically through x402 pay-per-call usage. No signup required.
    Tiers unlock higher rate limits:
      unknown:     10 calls/min
      emerging:    30 calls/min
      established: 60 calls/min
      trusted:     120 calls/min
      elite:       unlimited
    """
    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(f"{GATEWAY_URL}/agent/identity/{wallet_address}")
        data = resp.json()
        if "message" in data and data.get("total_calls", 0) == 0:
            return (
                f"Wallet: {wallet_address}\n"
                f"Tier: unknown (⚪ New Agent)\n"
                f"No activity recorded for this wallet on Wayforth yet.\n"
                f"Make x402 calls to build reputation and unlock higher rate limits."
            )
        lines = [
            f"Wallet:           {wallet_address}",
            f"Badge:            {data.get('badge', '⚪ New Agent')}",
            f"Tier:             {data.get('tier', 'unknown')}",
            f"Trust Score:      {data.get('trust_score', 0)}/100",
            f"Total Calls:      {data.get('total_calls', 0)}",
            f"Total Spent:      ${data.get('total_spent_usdc', '0.000000')} USDC",
            f"Network:          {data.get('network', 'base')}",
            f"Member Since:     {data.get('member_since', 'N/A')}",
            f"Last Active:      {data.get('last_active', 'N/A')}",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def wayforth_set_wri_alert(
    notify_url: str = Field(
        description="HTTPS webhook URL to POST to when a service crosses the WRI threshold."
    ),
    threshold_score: float = Field(
        description="WRI score threshold (50.0 – 99.9). Alert fires when any matching service crosses this score upward."
    ),
    category: str = Field(
        default=None,
        description=(
            "Optional: only alert for services in this category. "
            "Options: translation, inference, search, image, audio, finance, weather, email, data. "
            "Leave unset to watch all categories."
        ),
    ),
    min_signals: int = Field(
        default=5,
        description=(
            "Minimum signal count before alerting. Prevents noise from newly-added services "
            "with very few data points. Default: 5."
        ),
    ),
) -> str:
    """
    Register a webhook to be notified when any API service crosses a WayforthRank WRI score threshold.

    The webhook fires once per service per 24 hours when the score crosses upward through the threshold.
    Payload is HMAC-SHA256 signed with X-Wayforth-Signature for verification.

    Use this to automatically discover new high-quality services as they accumulate payment signals.
    """
    api_key = os.getenv("WAYFORTH_API_KEY", "")
    if not api_key:
        return "Error: WAYFORTH_API_KEY not set."
    body: dict = {
        "threshold_score": threshold_score,
        "notify_url": notify_url,
        "min_signals": min_signals,
    }
    if category:
        body["category"] = category
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{GATEWAY_URL}/webhooks/wri-alerts",
                json=body,
                headers={"X-Wayforth-API-Key": api_key},
            )
        data = resp.json()
        if resp.status_code != 200:
            return f"Error {resp.status_code}: {data}"
        lines = [
            f"WRI alert registered.",
            f"  ID:              {data.get('id')}",
            f"  Threshold:       {data.get('threshold_score')}",
            f"  Category:        {data.get('category') or 'all categories'}",
            f"  Min signals:     {data.get('min_signals')}",
            f"  Notify URL:      {data.get('notify_url')}",
            f"  Created:         {data.get('created_at')}",
            "",
            "The webhook fires when any service crosses this WRI threshold upward (24hr cooldown).",
            "Payload is HMAC-SHA256 signed. Verify with X-Wayforth-Signature header.",
        ]
        return "\n".join(lines)
    except Exception as exc:
        return f"Error: {exc}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
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

STEP 2 — Execute instantly (12 managed services, no API keys needed):
  wayforth_execute(
    service_slug="deepl",
    params={"text": "Hello world", "target_lang": "ES"},
    key_source="managed"
  )
  → {"translated_text": "Hola mundo", ...}

STEP 3 — Discover (when you don't know which service to use):
  wayforth_search("translate text to Spanish")
  → DeepL        WRI:82  Tier 2  $0.00003/call
  → LibreTranslate  WRI:71  Tier 2  Free

STEP 4 — Pay (choose your track):

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
  100 free on signup
  Buy more: $19/50K · $99/300K · $299/1M

ROUTING FEE: 1.5% flat on all payments

300+ verified APIs · 256 Tier 2 · 18 categories

All tools (in recommended order):
  wayforth_run         — one-call runtime: intent → search → execute
  wayforth_execute     — call 15 managed services instantly (no API key needed)
  wayforth_compare     — side-by-side comparison of 2-5 services
  wayforth_search      — semantic service discovery
  wayforth_query       — WayforthQL v2 structured discovery (tier/price/x402/provider filters)
  wayforth_pay         — pay via card or crypto (dual-track)
  wayforth_keys        — manage BYOK encrypted service keys
  wayforth_list        — browse catalog with filters
  wayforth_similar     — co-used services (Service Graph)
  wayforth_identity    — agent trust score and reputation
  wayforth_remember    — save a service to agent memory
  wayforth_recall      — retrieve saved services
  wayforth_check_agent_identity — look up x402 wallet reputation and tier
  wayforth_set_wri_alert — register webhook for WRI threshold alerts
  wayforth_stats       — catalog statistics
  wayforth_status      — API health check
  wayforth_quickstart  — this guide
"""
