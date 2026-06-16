"""
{{AGENT_NAME}} — fetch-process agent

Discovers the best weather/data service via Wayforth at runtime,
fetches structured data through the proxy (native upstream shape + failover),
and transforms it into a human-readable report.

Pattern: discovery via SDK search  ·  execution via GET /proxy/{slug}?city=...
"""
import os
import sys
import httpx
from wayforth import Wayforth

API_KEY = os.environ.get("WAYFORTH_API_KEY", "")
if not API_KEY:
    sys.exit("Set WAYFORTH_API_KEY in your environment. Get a key at wayforth.io/signup")

CITY = os.environ.get("CITY", "London")

_PROXY_CATALOG_SLUGS = {
    "alphavantage", "assemblyai", "brave_search", "deepl", "elevenlabs", "firecrawl",
    "gemini", "groq", "jina", "mistral", "openweather", "perplexity",
    "resend", "serper", "stability", "tavily", "together",
}
_CATALOG_TO_PROXY = {"brave_search": "brave"}


def main() -> None:
    wf = Wayforth(api_key=API_KEY)

    # ── 1. DISCOVER ────────────────────────────────────────────────────────
    print(f"Discovering best weather/data service...")
    results = wf.search("current weather conditions city", limit=10)["results"]
    candidates = [r for r in results if r.get("slug") in _PROXY_CATALOG_SLUGS]
    if not candidates:
        sys.exit("No proxy-managed data service found in discovery results.")

    # ── 2. FETCH via proxy (try candidates in WRI order) ──────────────────
    resp = None
    slug = None
    for hit in candidates:
        slug = hit["slug"]
        print(f"→ Trying {hit['name']}  WRI={hit['wri']}  slug={slug}")
        r = httpx.get(
            f"https://gateway.wayforth.io/proxy/{slug}",
            headers={"X-Wayforth-API-Key": API_KEY},
            params={"city": CITY},
            timeout=30,
        )
        if r.is_success:
            resp = r
            break
        print(f"  ✗ {slug} returned {r.status_code} — trying next service")

    if resp is None:
        sys.exit("All discovered services failed. Check your API key and credits.")

    print(f"\nFetching from /proxy/{slug}?city={CITY}...")

    print(f"[wayforth] failover  : {resp.headers.get('x-wayforth-failover')}")
    print(f"[wayforth] wri       : {resp.headers.get('x-wayforth-wri')}")
    print(f"[wayforth] credits   : {resp.headers.get('x-wayforth-credits-remaining')} remaining")

    # ── 3. PROCESS ─────────────────────────────────────────────────────────
    # Native OpenWeather adapter shape: city, temp_c, temp_f, condition, humidity, wind_kph
    data = resp.json()
    print(f"\nWeather report — {data.get('city', CITY)}:")
    print(f"  Temperature : {data.get('temp_c')}°C / {data.get('temp_f')}°F")
    print(f"  Condition   : {data.get('condition', '').capitalize()}")
    print(f"  Humidity    : {data.get('humidity')}%")
    print(f"  Wind        : {data.get('wind_kph')} km/h")

    temp_c = data.get("temp_c", 0)
    feels  = "hot" if temp_c > 25 else "mild" if temp_c > 15 else "cold"
    print(f"  Assessment  : {feels}")


if __name__ == "__main__":
    main()
