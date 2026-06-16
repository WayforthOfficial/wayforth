"""
{{AGENT_NAME}} — translate-notify agent

Discovers the best translation service via Wayforth at runtime,
translates text through the proxy (native upstream shape + automatic failover),
and optionally sends a notification email.

Pattern: discovery via SDK search  ·  execution via /proxy/{slug}
"""
import os
import sys
import httpx
from wayforth import Wayforth

API_KEY = os.environ.get("WAYFORTH_API_KEY", "")
if not API_KEY:
    sys.exit("Set WAYFORTH_API_KEY in your environment. Get a key at wayforth.io/signup")

TEXT_TO_TRANSLATE = "Hello from Wayforth — your agent is live."
TARGET_LANG       = "FR"   # DeepL language code

# Optional: set to send a notification email via Resend
NOTIFY_EMAIL = os.environ.get("NOTIFY_EMAIL", "")

_PROXY_CATALOG_SLUGS = {
    "alphavantage", "assemblyai", "brave_search", "deepl", "elevenlabs", "firecrawl",
    "gemini", "groq", "jina", "mistral", "openweather", "perplexity",
    "resend", "serper", "stability", "tavily", "together",
}
_CATALOG_TO_PROXY = {"brave_search": "brave"}


def proxy_post(slug: str, params: dict) -> httpx.Response:
    resp = httpx.post(
        f"https://gateway.wayforth.io/proxy/{slug}",
        headers={"X-Wayforth-API-Key": API_KEY},
        json=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp


def main() -> None:
    wf = Wayforth(api_key=API_KEY)

    # ── 1. DISCOVER translation service ────────────────────────────────────
    print("Discovering best translation service...")
    results = wf.search("translate text to another language", limit=10)["results"]
    candidates = [r for r in results if r.get("slug") in _PROXY_CATALOG_SLUGS]
    if not candidates:
        sys.exit("No proxy-managed translation service found in discovery results.")

    # ── 2. TRANSLATE via proxy (try candidates in WRI order) ───────────────
    resp = None
    slug = None
    for hit in candidates:
        slug = hit["slug"]
        print(f"→ Trying {hit['name']}  WRI={hit['wri']}  slug={slug}")
        r = httpx.post(
            f"https://gateway.wayforth.io/proxy/{slug}",
            headers={"X-Wayforth-API-Key": API_KEY},
            json={"text": TEXT_TO_TRANSLATE, "target_lang": TARGET_LANG},
            timeout=30,
        )
        if r.is_success:
            resp = r
            break
        print(f"  ✗ {slug} returned {r.status_code} — trying next service")

    if resp is None:
        sys.exit("All discovered services failed. Check your API key and credits.")

    print(f"\nTranslating via /proxy/{slug}...")

    print(f"[wayforth] failover  : {resp.headers.get('x-wayforth-failover')}")
    print(f"[wayforth] wri       : {resp.headers.get('x-wayforth-wri')}")
    print(f"[wayforth] credits   : {resp.headers.get('x-wayforth-credits-remaining')} remaining")

    # Native DeepL adapter shape: {"translated_text": "...", "detected_source_lang": "..."}
    data            = resp.json()
    translated_text = data.get("translated_text", "")
    source_lang     = data.get("detected_source_lang", "?")

    print(f"\nOriginal  ({source_lang}): {TEXT_TO_TRANSLATE}")
    print(f"Translated ({TARGET_LANG}): {translated_text}")

    # ── 3. NOTIFY (optional) ───────────────────────────────────────────────
    if not NOTIFY_EMAIL:
        print("\n[skip] Set NOTIFY_EMAIL to also send a notification via Resend.")
        return

    print(f"\nDiscovering notification service...")
    notif_results = wf.search("send email notification", limit=10)["results"]
    notif_hit = next((r for r in notif_results if r.get("slug") in _PROXY_CATALOG_SLUGS), None)
    if not notif_hit:
        print("[skip] No proxy-managed email service found.")
        return

    notif_slug = notif_hit["slug"]
    print(f"→ {notif_hit['name']}  slug={notif_slug}")

    notif_resp = proxy_post(notif_slug, {
        "to":      NOTIFY_EMAIL,
        "subject": f"Translation complete: {TARGET_LANG}",
        "html":    f"<p><strong>Original:</strong> {TEXT_TO_TRANSLATE}</p>"
                   f"<p><strong>Translated:</strong> {translated_text}</p>",
    })
    print(f"Notification sent: {notif_resp.json()}")


if __name__ == "__main__":
    main()
