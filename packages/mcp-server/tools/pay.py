"""tools/pay.py — wayforth_pay."""

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_instance import mcp, API_BASE, _get_api_key


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def wayforth_pay(
    service_id: str = Field(description="Service ID from wayforth_search results (e.g. 'wayforth://deepl/...')"),
    amount_usd: float = Field(description="Amount to pay in USD (e.g. 0.001)"),
    track: str = Field(default="card", description="Payment track: 'card' (Stripe Treasury credits) or 'crypto' (Base USDC, non-custodial)"),
    query_id: str = Field(default=None, description="Optional query ID from wayforth_search for conversion tracking"),
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

    Args:
        service_id: Service to pay (from wayforth_search results)
        amount_usd: Amount to pay in USD (e.g. 0.001)
        track: Payment track — 'card' or 'crypto' (default: 'card')
        query_id: Optional query ID from wayforth_search for WayforthRank signal
    """
    api_key = _get_api_key()
    if not api_key:
        return "No API key provided. Get one free at wayforth.io — 100 credits, no card required."

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

    if payment_track == "card":
        credits_left = data.get("credits_remaining", 0)
        tx_ref = data.get("tx_ref", "")
        return (
            f"Card payment processed — {service_name}\n\n"
            f"Amount: ${amount_usd}\n"
            f"Routing fee (1.5%): ${routing_fee}\n"
            f"Credits deducted: {data.get('credits_deducted', 0)}\n"
            f"Credits remaining: {credits_left}\n"
            f"Reference: {tx_ref}"
        )

    if payment_track == "crypto":
        return (
            f"Crypto calldata ready — {service_name}\n\n"
            f"Amount: ${amount_usd} USDC\n"
            f"Routing fee (1.5%): ${routing_fee}\n"
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
