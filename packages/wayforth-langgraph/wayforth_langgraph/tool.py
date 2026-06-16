"""wayforth_langgraph.tool — WayforthTool for LangGraph agents.

One import, one init:

    from wayforth_langgraph import WayforthTool
    wayforth = WayforthTool(api_key=os.environ["WAYFORTH_API_KEY"])
    agent = create_react_agent(llm, tools=[wayforth])

The tool discovers the best available service at runtime via the Wayforth
search API, then executes through /proxy/{slug} for native upstream response
shape plus automatic failover and WayforthRank signal capture.
"""
from __future__ import annotations

import json
from typing import Any, Optional, Type

import httpx
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

_GATEWAY = "https://gateway.wayforth.io"

# Catalog slugs returned by /search that correspond to Wayforth managed proxies.
# Call GET /services?tier=3 or run `wayforth services --tier 3` to refresh.
_PROXY_CATALOG_SLUGS: frozenset[str] = frozenset({
    "alphavantage", "assemblyai", "brave_search", "deepl", "elevenlabs", "firecrawl",
    "gemini", "groq", "jina", "mistral", "openweather", "perplexity",
    "resend", "serper", "stability", "tavily", "together",
})

# Some catalog slugs differ from the proxy endpoint slug.
_CATALOG_TO_PROXY: dict[str, str] = {"brave_search": "brave"}

# Category hint → fallback queries that reliably surface managed slugs.
# The search API category filter narrows to catalog entries that don't overlap
# with managed proxy slugs (e.g. category='search' returns kagi_search, not serper).
# We pass NO category to the API; instead try these service-specific queries.
_CATEGORY_FALLBACK: dict[str, list[str]] = {
    "search":        ["serper web search results API", "tavily search API", "brave search API"],
    "translation":   ["deepl translate text API"],
    "inference":     ["groq LLM chat completions API", "together AI inference API", "mistral chat API"],
    "image":         ["stability image generation API"],
    "data":          ["openweather current weather API", "alphavantage financial data API", "jina reader API"],
    "audio":         ["elevenlabs text to speech API", "assemblyai transcription API"],
    "communication": ["resend email send API"],
    "scraping":      ["firecrawl web scraper API", "jina reader API"],
}


class _Input(BaseModel):
    intent: str = Field(
        description="Natural language description of what you want to accomplish. "
                    "Examples: 'search for recent AI news', 'translate this text to Spanish', "
                    "'get the current weather in Tokyo', 'generate an image of a city at night'."
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters to pass to the discovered service. "
                    "Examples: {'query': 'AI agents'}, {'text': 'Hello', 'target_lang': 'ES'}, "
                    "{'city': 'Tokyo'}, {'prompt': 'futuristic city'}.",
    )
    category: Optional[str] = Field(
        None,
        description="Optional service category hint to narrow discovery. "
                    "One of: search, translation, inference, image, data, audio, communication.",
    )


class WayforthTool(BaseTool):
    """LangGraph tool that routes any API call through Wayforth's managed proxy.

    Discovers the best service for a given intent using Wayforth's search API,
    then executes through /proxy/{slug} for native upstream response shape,
    automatic failover, and WayforthRank signal capture.

    Usage::

        from wayforth_langgraph import WayforthTool
        from langgraph.prebuilt import create_react_agent

        wayforth = WayforthTool(api_key=os.environ["WAYFORTH_API_KEY"])
        agent    = create_react_agent(llm, tools=[wayforth])
    """

    name: str = "wayforth"
    description: str = (
        "Access 5,000+ APIs through Wayforth's managed marketplace. "
        "Discovers the best service at runtime and executes with automatic failover. "
        "Use for: web search, translation, LLM inference, weather data, image generation, "
        "email, financial data, and thousands of other APIs. "
        "Pass 'intent' describing what you want, 'params' with the service-specific inputs "
        "(e.g. query=, text=+target_lang=, city=, prompt=), and an optional 'category' hint."
    )
    args_schema: Type[BaseModel] = _Input

    _api_key: str

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        object.__setattr__(self, "_api_key", api_key)

    def _headers(self) -> dict[str, str]:
        return {"X-Wayforth-API-Key": self._api_key, "Content-Type": "application/json"}

    def _discover(self, intent: str, category: str | None) -> str | None:
        queries = [intent] + (_CATEGORY_FALLBACK.get(category, []) if category else [])
        for q in queries:
            resp = httpx.get(
                f"{_GATEWAY}/search", headers=self._headers(),
                params={"q": q, "limit": 10}, timeout=20,
            )
            if not resp.is_success:
                continue
            hit = next((r for r in resp.json().get("results", [])
                        if r.get("slug") in _PROXY_CATALOG_SLUGS), None)
            if hit:
                catalog_slug = hit["slug"]
                return _CATALOG_TO_PROXY.get(catalog_slug, catalog_slug)
        return None

    def _execute_proxy(self, slug: str, params: dict[str, Any]) -> tuple[Any, dict[str, str]]:
        method = "GET" if slug == "openweather" else "POST"
        if method == "GET":
            resp = httpx.get(
                f"{_GATEWAY}/proxy/{slug}",
                headers={"X-Wayforth-API-Key": self._api_key},
                params=params, timeout=30,
            )
        else:
            resp = httpx.post(
                f"{_GATEWAY}/proxy/{slug}",
                headers=self._headers(),
                json=params, timeout=30,
            )
        resp.raise_for_status()
        meta = {
            "service":  slug,
            "wri":      resp.headers.get("x-wayforth-wri", ""),
            "failover": resp.headers.get("x-wayforth-failover", "false"),
            "credits":  resp.headers.get("x-wayforth-credits-remaining", ""),
        }
        if resp.headers.get("x-wayforth-failover") == "true":
            meta["original_service"] = resp.headers.get("x-wayforth-original-service", "")
            meta["routed_to"]        = resp.headers.get("x-wayforth-routed-to", "")
            meta["reason"]           = resp.headers.get("x-wayforth-reason", "")
        return resp.json(), meta

    def _run(
        self,
        intent: str,
        params: dict[str, Any] | None = None,
        category: str | None = None,
        **_: Any,
    ) -> str:
        params = params or {}
        slug = self._discover(intent, category)
        if not slug:
            return json.dumps({
                "error": "no_managed_service_found",
                "intent": intent,
                "hint": "Rephrase intent or specify category (search/translation/inference/image/data).",
            })
        try:
            result, meta = self._execute_proxy(slug, params)
            return json.dumps({"result": result, **meta})
        except httpx.HTTPStatusError as exc:
            return json.dumps({"error": str(exc), "service": slug})

    async def _arun(
        self,
        intent: str,
        params: dict[str, Any] | None = None,
        category: str | None = None,
        **_: Any,
    ) -> str:
        params = params or {}
        slug: str | None = None
        queries = [intent] + (_CATEGORY_FALLBACK.get(category, []) if category else [])
        async with httpx.AsyncClient(timeout=20) as client:
            for q in queries:
                sresp = await client.get(
                    f"{_GATEWAY}/search", headers=self._headers(),
                    params={"q": q, "limit": 10},
                )
                if not sresp.is_success:
                    continue
                hit = next((r for r in sresp.json().get("results", [])
                            if r.get("slug") in _PROXY_CATALOG_SLUGS), None)
                if hit:
                    catalog_slug = hit["slug"]
                    slug = _CATALOG_TO_PROXY.get(catalog_slug, catalog_slug)
                    break
        if not slug:
            return json.dumps({"error": "no_managed_service_found", "intent": intent})

        async with httpx.AsyncClient(timeout=30) as client:
            method = "GET" if slug == "openweather" else "POST"
            if method == "GET":
                resp = await client.get(
                    f"{_GATEWAY}/proxy/{slug}",
                    headers={"X-Wayforth-API-Key": self._api_key},
                    params=params,
                )
            else:
                resp = await client.post(
                    f"{_GATEWAY}/proxy/{slug}", headers=self._headers(), json=params,
                )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                return json.dumps({"error": str(exc), "service": slug})

            meta = {
                "service":  slug,
                "wri":      resp.headers.get("x-wayforth-wri", ""),
                "failover": resp.headers.get("x-wayforth-failover", "false"),
                "credits":  resp.headers.get("x-wayforth-credits-remaining", ""),
            }
            if resp.headers.get("x-wayforth-failover") == "true":
                meta["original_service"] = resp.headers.get("x-wayforth-original-service", "")
                meta["routed_to"]        = resp.headers.get("x-wayforth-routed-to", "")
                meta["reason"]           = resp.headers.get("x-wayforth-reason", "")
            return json.dumps({"result": resp.json(), **meta})
