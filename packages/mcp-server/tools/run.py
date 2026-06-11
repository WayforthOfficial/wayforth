"""tools/run.py — wayforth_run (one-call runtime)."""

import json

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_instance import mcp, API_BASE, _get_api_key


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False))
async def wayforth_run(
    intent: str = Field(
        description="Natural language description of what you need. Example: 'translate this text to Spanish', 'get weather in Tokyo', 'search the web for AI news', 'get stock price for AAPL'"
    ),
    input: dict = Field(
        default={},
        description="The actual content to process. Example: {'text': 'Hello world'} for translation, {'city': 'Tokyo'} for weather, {'query': 'AI news'} for search, {'symbol': 'AAPL'} for stocks",
    ),
    category: str = Field(
        default=None,
        description="Optional: filter by service category. Options: translation, inference, data, image, audio, communication",
    ),
    rail: str = Field(
        default="managed",
        description="Payment rail. 'managed' uses Wayforth's keys (recommended, zero setup). 'byok' uses your stored key.",
    ),
    agent_id: str = Field(
        default="",
        description="Optional: tag this call with your agent's name for per-agent analytics in your dashboard. Example: 'translation-agent', 'my-chatbot'. Max 64 chars, alphanumeric/hyphens/underscores.",
    ),
) -> str:
    """Intent-based routing with automatic reliability failover.
    Describe what you need — Wayforth selects the highest-WRI verified service
    and executes it. If the primary service degrades mid-session, automatically
    reroutes to the next verified equivalent. Returns result + failover status.

    Supports: translation, weather, news, stock prices, web search, image generation,
    speech-to-text, text-to-speech, email, and 300+ more catalog services.

    Optional: include agent_id='my-agent-name' to tag this call for per-agent analytics
    in your dashboard at wayforth.io/dashboard/agents.
    """
    api_key = _get_api_key()
    if not api_key:
        return "No API key provided. Get one free at wayforth.io — 100 credits, no card required."

    body: dict = {"intent": intent, "input": input}
    if agent_id:
        body["agent_id"] = agent_id
    if category or rail != "managed":
        body["preferences"] = {}
        if category:
            body["preferences"]["category"] = category
        if rail != "managed":
            body["preferences"]["rail"] = rail

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{API_BASE}/run",
                headers={"X-Wayforth-API-Key": api_key, "Content-Type": "application/json"},
                json=body,
            )
    except Exception as e:
        return f"Wayforth API is not reachable at {API_BASE}."

    if resp.status_code == 402:
        d = resp.json().get("detail", {})
        return (
            f"Insufficient credits. Balance: {d.get('current_balance_credits', 0)} credits "
            f"({d.get('current_balance_calls', 0)} calls). Top up at wayforth.io/billing"
        )
    if resp.status_code == 422:
        d = resp.json().get("detail", {})
        err = d.get("error", "")
        if err == "missing_param":
            return (
                f"Missing required params for {d.get('service_selected', 'service')}.\n"
                f"Missing: {d.get('missing', [])}\n"
                f"Hint: {d.get('hint', '')}"
            )
        if err == "no_managed_service":
            top = d.get("top_result", {})
            return (
                f"No managed service found for: {intent!r}\n"
                f"Top catalog result: {top.get('name')} (slug: {top.get('slug')})\n"
                f"Add your API key via: POST /call/keys/add"
            )
        return f"Run error 422: {resp.text[:300]}"
    if resp.status_code == 503:
        d = resp.json().get("detail", {})
        fb = d.get("fallback", {})
        msg = d.get("message", "Service unavailable.")
        if fb:
            msg += f" Fallback: try {fb.get('slug')} ({fb.get('message','')})"
        return msg
    if resp.status_code != 200:
        return f"Run error {resp.status_code}: {resp.text[:300]}"

    data = resp.json()
    svc = data.get("service_used", {})
    ctx = data.get("search_context", {})
    failover = data.get("failover", {"triggered": False})

    out: dict = {
        "result": data.get("result"),
        "service_used": svc,
        "search_context": ctx,
        "failover": failover,
        "credits_remaining": data.get("credits_remaining"),
        "execution_ms": data.get("execution_ms"),
    }
    if failover.get("triggered"):
        out["_failover_note"] = (
            f"⚡ Failover: {failover.get('original_service')} → {failover.get('routed_to')} "
            f"({failover.get('reason')}, WRI {failover.get('original_wri')} → {failover.get('fallback_wri')})"
        )
    return json.dumps(out, indent=2)
