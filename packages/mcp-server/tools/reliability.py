"""tools/reliability.py — wayforth_reliability (real-time service reliability check)."""

import json
from typing import Optional

import httpx
from mcp.types import ToolAnnotations
from pydantic import Field

from mcp_instance import mcp, API_BASE, _get_api_key


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
async def wayforth_reliability(
    slug: Optional[str] = Field(
        default=None,
        description="Specific managed service slug to check (e.g. 'deepl', 'groq', 'serper'). "
                    "Leave blank to query by category instead.",
    ),
    category: Optional[str] = Field(
        default=None,
        description="Category name — returns top 5 services by WRI score. "
                    "Options: translation, inference, search, data, image, audio, communication. "
                    "Used when slug is not provided.",
    ),
) -> str:
    """Check real-time reliability for a service or category.
    Returns WRI score (0-100), Tier level, uptime over 7 days, last probe timestamp,
    and whether a verified failover alternative exists.
    Use to pre-check before committing to a service for a long-running task.
    """
    api_key = _get_api_key()
    headers = {"X-Wayforth-API-Key": api_key} if api_key else {}

    if not slug and not category:
        return (
            "Provide a slug or category.\n"
            "Examples:\n"
            '  wayforth_reliability(slug="deepl")\n'
            '  wayforth_reliability(category="translation")'
        )

    params: dict = {}
    if slug:
        params["slug"] = slug
    elif category:
        params["category"] = category

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{API_BASE}/reliability",
                headers=headers,
                params=params,
            )
    except Exception:
        return f"Wayforth API is not reachable at {API_BASE}."

    if r.status_code == 404:
        target = slug or category
        return f"Service not found: {target!r}. Check the slug or category name."
    if r.status_code == 422:
        return "Provide ?slug=<slug> or ?category=<category>."
    if r.status_code != 200:
        return f"Reliability check failed ({r.status_code}): {r.text[:200]}"

    data = r.json()

    def _fmt_entry(e: dict) -> str:
        status_icon = {"healthy": "✅", "degraded": "⚠️", "outage": "🔴"}.get(e.get("status", ""), "❓")
        lines = [
            f"{status_icon} {e.get('name', e.get('service'))} ({e.get('service')})",
            f"  WRI:     {e.get('wri', 'N/A')} / 100",
            f"  Tier:    {e.get('tier', 'N/A')}",
            f"  Uptime:  {e.get('uptime_7d') or 'N/A'} (7d)",
            f"  Latency: {e.get('avg_response_ms', 'N/A')} ms avg",
            f"  Probed:  {e.get('last_probe') or 'never'}",
            f"  Status:  {e.get('status', 'unknown')}",
        ]
        if e.get("failover_available"):
            lines.append(f"  Failover: ✅ {e.get('failover_candidate', 'available')}")
        else:
            lines.append("  Failover: none configured")
        return "\n".join(lines)

    if "services" in data:
        # Category response — top 5
        entries = data["services"]
        header = f"Top {len(entries)} services in category '{data.get('category')}':\n"
        return header + "\n\n".join(_fmt_entry(e) for e in entries)
    else:
        return _fmt_entry(data)
