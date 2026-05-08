"""tools/keys.py — wayforth_keys (BYOK key management)."""

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_instance import mcp, API_BASE, _get_api_key


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
async def wayforth_keys(
    action: str = Field(description="Action to perform: 'list' (show stored keys), 'add' (store a new key), or 'delete' (remove a key)"),
    service_slug: str = Field(default=None, description="Service identifier slug (required for 'add'/'delete'), e.g. 'openai', 'anthropic', 'stripe'"),
    api_key_value: str = Field(default=None, description="The API key to encrypt and store (required for action='add')"),
    service_name: str = Field(default=None, description="Human-readable label for the service (optional for 'add', defaults to service_slug)"),
) -> str:
    """Manage BYOK (Bring Your Own Key) stored service keys.
    Store encrypted API keys for any service and call them via wayforth_execute with key_source='byok'.
    Keys are encrypted at rest with AES-128 and never returned in plaintext.
    """
    wayforth_key = _get_api_key()
    if not wayforth_key:
        return "No API key provided. Get one free at wayforth.io — 100 credits, no card required."
    headers = {"X-Wayforth-API-Key": wayforth_key, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if action == "list":
                resp = await client.get(f"{API_BASE}/call/keys", headers=headers)
                if resp.status_code != 200:
                    return f"Error listing keys: {resp.status_code} {resp.text[:200]}"
                data = resp.json()
                keys = data.get("service_keys", [])
                if not keys:
                    return "No BYOK keys stored. Use action='add' to store one."
                lines = [f"{len(keys)} stored key(s):\n"]
                for k in keys:
                    last = k.get("last_used_at") or "never"
                    lines.append(
                        f"  {k['service_slug']} ({k.get('service_name', '')}) "
                        f"— preview: {k.get('key_preview', '???')} "
                        f"— calls: {k.get('total_calls', 0)} — last used: {last}"
                    )
                lines.append("\nCall with: wayforth_execute(service_slug=..., key_source='byok')")
                return "\n".join(lines)

            elif action == "add":
                if not service_slug or not api_key_value:
                    return "Error: service_slug and api_key_value are required for action='add'"
                resp = await client.post(
                    f"{API_BASE}/call/keys/add",
                    headers=headers,
                    json={"service_slug": service_slug, "service_name": service_name or service_slug, "api_key": api_key_value},
                )
                if resp.status_code != 200:
                    return f"Error storing key: {resp.status_code} {resp.text[:200]}"
                data = resp.json()
                return (
                    f"Key stored for '{service_slug}' · preview: {data.get('key_preview', '???')}\n"
                    f"Call it: wayforth_execute(service_slug='{service_slug}', params={{...}}, key_source='byok')"
                )

            elif action == "delete":
                if not service_slug:
                    return "Error: service_slug is required for action='delete'"
                resp = await client.delete(f"{API_BASE}/call/keys/{service_slug}", headers=headers)
                if resp.status_code == 404:
                    return f"No active key found for '{service_slug}'"
                if resp.status_code != 200:
                    return f"Error deleting key: {resp.status_code} {resp.text[:200]}"
                return f"Key for '{service_slug}' deactivated."

            else:
                return "Error: action must be 'list', 'add', or 'delete'"
    except Exception as e:
        return f"Wayforth API not reachable: {e}"
