"""mcp_instance.py — shared FastMCP instance, middleware, helpers, and constants."""

import contextvars
import json
import logging
import os
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse as _JSONResponse

logger = logging.getLogger("wayforth-mcp")

VERSION = "0.2.5"

load_dotenv()

API_BASE = os.getenv("WAYFORTH_API_URL", "https://gateway.wayforth.io")
WAYFORTH_API_KEY = os.getenv("WAYFORTH_API_KEY", "")

_PORT = int(os.getenv("PORT", "8080"))
_HOST = os.getenv("HOST", "0.0.0.0")

# Per-request API key extracted by ApiKeyMiddleware (HTTP transports only)
_api_key_var: contextvars.ContextVar[str] = contextvars.ContextVar("wayforth_api_key")


def _get_api_key() -> str:
    """Return the API key for the current request, falling back to env var."""
    try:
        return _api_key_var.get()
    except LookupError:
        return WAYFORTH_API_KEY


class ApiKeyMiddleware:
    """ASGI middleware that extracts WAYFORTH_API_KEY from query params or headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            from urllib.parse import parse_qs
            method = scope.get("method", "")
            path = scope.get("path", "")
            query = scope.get("query_string", b"").decode()
            raw_headers = scope.get("headers", [])
            header_keys = [k.decode(errors="replace") for k, _ in raw_headers]
            logger.info("REQUEST: %s %s%s headers=%s", method, path, f"?{query}" if query else "", header_keys)

            qs = parse_qs(query)
            headers = {k.lower(): v for k, v in raw_headers}
            key = (
                qs.get("WAYFORTH_API_KEY", [None])[0]
                or headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
                or headers.get(b"x-api-key", b"").decode()
                or headers.get(b"x-wayforth-api-key", b"").decode()
                or WAYFORTH_API_KEY
            )
            key_source = "query" if qs.get("WAYFORTH_API_KEY") else \
                         "auth-header" if headers.get(b"authorization") else \
                         "x-api-key" if headers.get(b"x-api-key") else \
                         "env"
            logger.info("API_KEY source=%s present=%s", key_source, bool(key))
            token = _api_key_var.set(key)
            try:
                await self.app(scope, receive, send)
            finally:
                _api_key_var.reset(token)
        else:
            await self.app(scope, receive, send)


mcp = FastMCP(
    "wayforth",
    host=_HOST,
    port=_PORT,
    streamable_http_path="/",
    instructions="Search 300+ verified APIs, pay via card or crypto, execute with managed keys. Get your API key at wayforth.io/signup.",
)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "service": "wayforth-mcp"})


@mcp.custom_route("/.well-known/mcp/server-card.json", methods=["GET"])
async def server_card(request):
    from starlette.responses import JSONResponse
    return JSONResponse({
        "name": "wayforth",
        "version": VERSION,
        "description": "The search engine AI agents use to find and pay for APIs. 300+ verified APIs ranked by merit-based routing (no paid placement).",
        "icon": "https://wayforth.io/favicon.png",
        "repository": "https://github.com/WayforthOfficial/wayforth",
        "homepage": "https://wayforth.io",
        "license": "BSL-1.1",
        "runtime": "http",
        "url": "https://mcp.wayforth.io",
        "transport": "streamable-http",
        "configSchema": {
            "type": "object",
            "properties": {
                "WAYFORTH_API_KEY": {
                    "type": "string",
                    "description": "Your Wayforth API key — get one free at wayforth.io",
                    "required": True,
                }
            },
        },
    })


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_server(request):
    from starlette.responses import JSONResponse
    return JSONResponse({
        "issuer": "https://mcp.wayforth.io",
        "authorization_endpoint": "https://wayforth.io/login",
        "token_endpoint": "https://wayforth.io/api/token",
        "response_types_supported": ["code"],
    })


TIER_LABELS = {0: "free", 1: "basic", 2: "standard", 3: "premium"}

MEMORY_FILE = os.path.expanduser("~/.wayforth_memory.json")

_MANAGED_SLUGS = {
    "groq", "deepl", "openweather", "newsapi", "serper", "resend",
    "assemblyai", "stability", "tavily", "jina", "alphavantage", "elevenlabs",
}

_CATEGORY_PARAMS = {
    "translation": '{"text": "Hello world", "target_lang": "ES"}',
    "inference": '{"messages": [{"role": "user", "content": "Say hello"}]}',
    "data": '{"q": "New York"}',
    "search": '{"q": "your search query"}',
    "image": '{"prompt": "a futuristic city at night"}',
    "audio": '{"audio_url": "https://assembly.ai/sports_injuries.mp3"}',
    "communication": '{"from": "noreply@wayforth.io", "to": "you@example.com", "subject": "Test", "html": "<p>Hello</p>"}',
    "text-to-speech": '{"text": "Hello, I am your AI assistant.", "voice_id": "21m00Tcm4TlvDq8ikWAM"}',
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
    "tavily": ["search for AI agent frameworks", "find recent research papers", "search competitor pricing"],
    "jina": ["read a documentation page", "extract content from a blog post", "parse a landing page"],
    "alphavantage": ["get MSFT stock price", "get GOOGL stock price", "get TSLA stock data"],
    "elevenlabs": ["generate audio for a podcast intro", "convert blog post to audio", "create a voice notification"],
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
