"""tools/execute.py — wayforth_execute (direct, slug required)."""

import json

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_instance import mcp, API_BASE, _get_api_key, _EXECUTE_NEXT_QUERIES, _EXECUTE_NEXT_DEFAULT


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def wayforth_execute(
    service_slug: str = Field(description="Service to call: groq, deepl, openweather, newsapi, serper, resend, assemblyai, stability, tavily, jina, alphavantage, elevenlabs — or any custom slug for BYOK"),
    params: dict = Field(description="Service-specific parameters as a JSON object (e.g. {'text': 'Hello', 'target_lang': 'ES'} for DeepL)"),
    key_source: str = Field(default="managed", description="Key source: 'managed' (use Wayforth's key, default) or 'byok' (use your stored key)"),
    agent_id: str = Field(default="", description="Optional: tag this call with your agent's name for per-agent analytics. Example: 'translation-agent'. Max 64 chars, alphanumeric/hyphens/underscores."),
) -> str:
    """Execute any API service instantly.
    15 managed services run with zero API keys —
    Wayforth holds the credentials.
    The fastest way to add any API capability
    to your agent.

    Optional: include agent_id='my-agent-name' to tag this call for per-agent analytics
    in your dashboard at wayforth.io/dashboard/agents.
    """
    api_key = _get_api_key()
    if not api_key:
        return "No API key provided. Get one free at wayforth.io — 100 credits, no card required."
    req_body: dict = {"service_slug": service_slug, "params": params, "key_source": key_source}
    if agent_id:
        req_body["agent_id"] = agent_id
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(
                f"{API_BASE}/execute",
                headers={"X-Wayforth-API-Key": api_key, "Content-Type": "application/json"},
                json=req_body,
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
