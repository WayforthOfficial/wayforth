"""
{{AGENT_NAME}} — search-summarize agent

Discovers the best available search service via Wayforth at runtime,
executes through the proxy (native upstream shape + automatic failover),
and prints top results.

Pattern: discovery via SDK search  ·  execution via /proxy/{slug}
"""
import os
import sys
import httpx
from wayforth import Wayforth

API_KEY = os.environ.get("WAYFORTH_API_KEY", "")
if not API_KEY:
    sys.exit("Set WAYFORTH_API_KEY in your environment. Get a key at wayforth.io/signup")

QUERY = "latest developments in AI agents"

# Catalog slugs returned by /search that correspond to Wayforth managed proxies.
# Run `wayforth services --tier 3` to see the current list.
_PROXY_CATALOG_SLUGS = {
    "alphavantage", "assemblyai", "brave_search", "deepl", "elevenlabs", "firecrawl",
    "gemini", "groq", "jina", "mistral", "openweather", "perplexity",
    "resend", "serper", "stability", "tavily", "together",
}

# Some catalog slugs differ from the proxy endpoint slug.
_CATALOG_TO_PROXY = {"brave_search": "brave"}


def main() -> None:
    wf = Wayforth(api_key=API_KEY)

    # ── 1. DISCOVER ────────────────────────────────────────────────────────
    print("Discovering best search service...")
    results = wf.search("web search recent results", limit=10)["results"]
    candidates = [r for r in results if r.get("slug") in _PROXY_CATALOG_SLUGS]
    if not candidates:
        sys.exit("No proxy-managed search service found in discovery results.")

    # ── 2. EXECUTE (try candidates in ranked order) ────────────────────────
    resp = None
    proxy_slug = None
    for hit in candidates:
        catalog_slug = hit["slug"]
        proxy_slug = _CATALOG_TO_PROXY.get(catalog_slug, catalog_slug)
        print(f"→ Trying {hit['name']}  WRI={hit['wri']}  slug={proxy_slug}")
        r = httpx.post(
            f"https://gateway.wayforth.io/proxy/{proxy_slug}",
            headers={"X-Wayforth-API-Key": API_KEY},
            json={"query": QUERY},
            timeout=30,
        )
        if r.is_success:
            resp = r
            break
        print(f"  ✗ {proxy_slug} returned {r.status_code} — trying next service")

    if resp is None:
        sys.exit("All discovered services failed. Check your API key and credits.")

    print(f"[wayforth] failover  : {resp.headers.get('x-wayforth-failover')}")
    print(f"[wayforth] wri       : {resp.headers.get('x-wayforth-wri')}")
    print(f"[wayforth] credits   : {resp.headers.get('x-wayforth-credits-remaining')} remaining")

    # ── 3. PROCESS ─────────────────────────────────────────────────────────
    # Native upstream shape — no Wayforth envelope (serper: organic[], brave/tavily: results[])
    data    = resp.json()
    organic = data.get("organic") or data.get("results") or []
    print(f"\nTop results for '{QUERY}':")
    for item in organic[:3]:
        title   = item.get("title") or item.get("name", "")
        url     = item.get("link") or item.get("url", "")
        snippet = item.get("snippet", "")[:120]
        print(f"  · {title}")
        if url:
            print(f"    {url}")
        if snippet:
            print(f"    {snippet}...")
        print()


if __name__ == "__main__":
    main()
