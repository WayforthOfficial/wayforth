"""tools/memory.py — wayforth_remember, wayforth_recall."""

from datetime import datetime, timezone

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_instance import mcp, API_BASE, _load_memory, _save_memory


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
async def wayforth_remember(
    service_id: str = Field(description="Service ID or wayforth:// identifier from search results"),
    service_name: str = Field(description="Human-readable name of the service to save"),
    note: str = Field(default="", description="Optional note about why you saved this service"),
    agent_id: str = Field(default="", description="Agent identifier (wallet address or session ID) for cross-session persistence"),
) -> str:
    """Save a service to agent memory for quick access later."""
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


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def wayforth_recall(
    query: str = Field(default="", description="Optional filter to search saved services by name or note"),
    agent_id: str = Field(default="", description="Agent identifier used when saving — required for cross-session memory retrieval"),
) -> str:
    """Recall services previously saved to memory."""
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
