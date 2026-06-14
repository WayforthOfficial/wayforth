import asyncio
import base64
import json as _json
import logging
import os as _os
import time as _time
import httpx
from fastapi import HTTPException

from core.url_validation import validate_external_url, validate_external_url_relaxed

_httpx_log = logging.getLogger("httpx")
logger = logging.getLogger(__name__)


# ── Circuit breakers for rate-capped upstreams (FINDING-006) ──────────────────
# Some upstreams enforce a hard global daily quota on Wayforth's single shared
# key. Without a breaker, one tenant (or one anonymous x402 caller, pre-disable)
# could exhaust the quota for everyone. Global caps stop that; per-user/day
# sub-quotas stop a single tenant from monopolising the shared budget.
UPSTREAM_DAILY_CAPS: dict[str, int] = {
    "alphavantage": 25,
    "resend": 100,
    # FINDING-111: stability generates real billable images (~$0.08 each). Cap
    # the shared daily budget so a runaway loop / abuse can't burn unbounded spend.
    # Raise this cap as real Stability traffic grows.
    # Current 10/day bounds the 4 probes/day + 6 user calls/day.
    "stability": 10,
}
_USER_UPSTREAM_DAILY_CAPS: dict[str, dict[str, int]] = {
    "free":       {"alphavantage": 5,  "resend": 10},
    "builder":    {"alphavantage": 15, "resend": 50},
    "starter":    {"alphavantage": 15, "resend": 50},
    "pro":        {"alphavantage": 15, "resend": 50},
    "growth":     {"alphavantage": 25, "resend": 100},
    "enterprise": {"alphavantage": 25, "resend": 100},
}


async def check_upstream_cap(service_slug: str, user_id: str | None, tier: str | None) -> None:
    """Raise 503 (global) or 429 (per-user) if a rate-capped upstream's daily
    quota is exhausted. No-op for uncapped services. Checked BEFORE credit
    deduction so a capped call is never charged.

    Counters live in Redis with a 24h TTL. Availability protection, not auth:
    if Redis is unavailable the breaker fails OPEN (allows the call) rather than
    blocking all capped-service traffic — auth/throttle paths fail closed
    elsewhere.
    """
    cap = UPSTREAM_DAILY_CAPS.get(service_slug)
    if cap is None:
        return
    from core.tier_gates import _get_redis
    redis = _get_redis()
    if redis is None:
        return
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Per-user sub-quota first, so a rejected user doesn't consume the global budget.
    ucap = _USER_UPSTREAM_DAILY_CAPS.get(tier or "free", _USER_UPSTREAM_DAILY_CAPS["free"]).get(service_slug)
    if ucap is not None and user_id:
        ukey = f"user_upstream:{user_id}:{service_slug}:{day}"
        try:
            ucount = await redis.incr(ukey)
            if ucount == 1:
                await redis.expire(ukey, 86400)
        except Exception:
            ucount = 0  # redis hiccup → don't block
        if ucount > ucap:
            raise HTTPException(status_code=429, detail={
                "error": "user_upstream_daily_limit_reached",
                "service": service_slug,
                "your_daily_limit": ucap,
                "message": f"You've reached your daily limit for {service_slug}. "
                           "Upgrade your plan or try again tomorrow.",
            })

    gkey = f"upstream_cap:{service_slug}:{day}"
    try:
        gcount = await redis.incr(gkey)
        if gcount == 1:
            await redis.expire(gkey, 86400)
    except Exception:
        gcount = 0
    if gcount > cap:
        raise HTTPException(status_code=503, detail={
            "error": "upstream_daily_limit_reached",
            "service": service_slug,
            "message": f"{service_slug} has reached its shared daily limit. Try again tomorrow "
                       "or use an alternative service.",
        })


SERVICE_CONFIGS = {
    # Inference
    "groq":        {"key_var": "GROQ_API_KEY",          "credits": 3,   "real_cost_per_call": 0.001},
    "together":    {"key_var": "TOGETHER_API_KEY",      "credits": 4,   "real_cost_per_call": 0.00027},
    # Translation
    "deepl":       {"key_var": "DEEPL_API_KEY",         "credits": 20,  "real_cost_per_call": 0.0},
    # Search
    "serper":      {"key_var": "SERPER_API_KEY",        "credits": 3,   "real_cost_per_call": 0.001},
    "tavily":      {"key_var": "TAVILY_API_KEY",        "credits": 10,  "real_cost_per_call": 0.008},
    "brave":       {"key_var": "BRAVE_API_KEY",         "credits": 6,   "real_cost_per_call": 0.005},
    "perplexity":  {"key_var": "PERPLEXITY_API_KEY",    "credits": 10,  "real_cost_per_call": 0.006},
    # Data
    "openweather": {"key_var": "OPENWEATHER_API_KEY",   "credits": 2,   "real_cost_per_call": 0.0},
    "alphavantage":{"key_var": "ALPHA_VANTAGE_API_KEY", "credits": 4,   "real_cost_per_call": 0.0},
    "jina":        {"key_var": "JINA_API_KEY",          "credits": 4,   "real_cost_per_call": 0.0001},
    # Audio / Voice
    "assemblyai":  {"key_var": "ASSEMBLYAI_API_KEY",    "credits": 25,  "real_cost_per_call": 0.0195},
    "elevenlabs":  {"key_var": "ELEVENLABS_API_KEY",    "credits": 200, "real_cost_per_call": 0.150},
    # Image — stability credits: 86 for core (default), 150 for ultra (resolved at call time)
    "stability":   {"key_var": "STABILITY_API_KEY",     "credits": 86,  "real_cost_per_call": 0.080},
    # Email
    "resend":      {"key_var": "RESEND_API_KEY",        "credits": 3,   "real_cost_per_call": 0.0},
    # Web scraping
    "firecrawl":   {"key_var": "FIRECRAWL_API_KEY",     "credits": 6,   "real_cost_per_call": 0.00533},
    # Inference (additional)
    "mistral":     {"key_var": "MISTRAL_API_KEY",        "credits": 4,   "real_cost_per_call": 0.00025},
    "gemini":      {"key_var": "GEMINI_API_KEY",         "credits": 3,   "real_cost_per_call": 0.0002},
}

# Fallback alternatives — used when a service fails with 5xx (bidirectional pairs)
SERVICE_ALTERNATIVES = {
    "groq":        "together",
    "together":    "groq",
    "perplexity":  "tavily",
    "tavily":      "serper",
    "brave":       "serper",
    "serper":      "brave",

}

SERVICE_DISPLAY_NAMES = {
    "groq":        "Groq Inference",
    "together":    "Together AI",
    "deepl":       "DeepL Translation",
    "serper":      "Serper Search",
    "tavily":      "Tavily Search",
    "brave":       "Brave Search",
    "perplexity":  "Perplexity Sonar",
    "openweather": "OpenWeather",

    "alphavantage":"Alpha Vantage",
    "jina":        "Jina AI",
    "assemblyai":  "AssemblyAI",
    "elevenlabs":  "ElevenLabs TTS",
    "stability":   "Stability AI",
    "resend":      "Resend Email",
    "firecrawl":   "Firecrawl Scrape",
    "mistral":     "Mistral AI",
    "gemini":      "Google Gemini Flash",
}


def _active_managed_count() -> int:
    """Count SERVICE_CONFIGS entries where the env var is set (i.e. key is configured)."""
    return sum(1 for cfg in SERVICE_CONFIGS.values() if _os.environ.get(cfg["key_var"]))


async def call_groq(params: dict, api_key: str) -> dict:
    model = params.get("model", "llama-3.3-70b-versatile")
    messages = params.get("messages", [])
    max_tokens = params.get("max_tokens", 1024)
    if not messages:
        raise Exception("params.messages is required")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
    if r.status_code != 200:
        raise Exception(f"Groq error {r.status_code}: {r.text[:200]}")
    data = r.json()
    choice = data["choices"][0]
    usage = data.get("usage", {})
    return {
        "content": choice["message"]["content"],
        "model": data.get("model", model),
        "tokens_used": usage.get("total_tokens", 0),
    }


async def stream_groq(params: dict, api_key: str):
    """Async generator yielding text tokens from Groq streaming API."""
    model = params.get("model", "llama-3.3-70b-versatile")
    messages = params.get("messages", [])
    if not messages:
        raise Exception("params.messages is required")
    max_tokens = params.get("max_tokens", 1024)
    async with httpx.AsyncClient(timeout=30.0) as client:
        async with client.stream(
            "POST",
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True},
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise Exception(f"Groq error {resp.status_code}: {body[:200]!r}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    return
                try:
                    chunk = _json.loads(payload)
                    content = chunk["choices"][0]["delta"].get("content", "")
                    if content:
                        yield content
                except Exception:
                    pass  # non-critical: malformed SSE chunk from provider; skip and continue streaming


async def stream_together(params: dict, api_key: str):
    """Async generator yielding text tokens from Together AI streaming API."""
    messages = params.get("messages", [])
    if not messages:
        raise Exception("params.messages is required")
    model = params.get("model", "meta-llama/Llama-3.3-70B-Instruct-Turbo")
    max_tokens = params.get("max_tokens", 1024)
    async with httpx.AsyncClient(timeout=30.0) as client:
        async with client.stream(
            "POST",
            "https://api.together.xyz/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True},
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise Exception(f"Together AI error {resp.status_code}: {body[:200]!r}")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    return
                try:
                    chunk = _json.loads(payload)
                    content = chunk["choices"][0]["delta"].get("content", "")
                    if content:
                        yield content
                except Exception:
                    pass  # non-critical: malformed SSE chunk from provider; skip and continue streaming


async def call_deepl(params: dict, api_key: str) -> dict:
    text = params.get("text", "")
    target_lang = params.get("target_lang", "")
    if not text or not target_lang:
        raise Exception("params.text and params.target_lang are required")
    # Tier 1 input cap: 2,000 characters
    if len(text) > 2000:
        raise HTTPException(413, "Text exceeds Tier 1 limit (2,000 chars). Use BYOK for larger payloads.")
    payload = {"text": [text], "target_lang": target_lang}
    if "source_lang" in params:
        payload["source_lang"] = params["source_lang"]
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api-free.deepl.com/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    if r.status_code != 200:
        raise Exception(f"DeepL error {r.status_code}: {r.text[:200]}")
    translation = r.json()["translations"][0]
    return {
        "translated_text": translation["text"],
        "detected_source_lang": translation.get("detected_source_language", ""),
    }


async def call_openweather(params: dict, api_key: str) -> dict:
    query_params: dict = {"appid": api_key, "units": "metric"}
    city = params.get("q") or params.get("city")
    if city:
        query_params["q"] = city
    elif "lat" in params and "lon" in params:
        query_params["lat"] = params["lat"]
        query_params["lon"] = params["lon"]
    else:
        raise Exception("params.city (or params.q) or params.lat+lon are required")
    _httpx_log.setLevel(logging.WARNING)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://api.openweathermap.org/data/2.5/weather",
                params=query_params,
            )
    finally:
        _httpx_log.setLevel(logging.NOTSET)
    if r.status_code != 200:
        raise Exception(f"OpenWeather error {r.status_code}: {r.text[:200]}")
    data = r.json()
    temp_c = data["main"]["temp"]
    return {
        "city": data.get("name", ""),
        "temp_c": round(temp_c, 1),
        "temp_f": round(temp_c * 9 / 5 + 32, 1),
        "condition": data["weather"][0]["description"],
        "humidity": data["main"]["humidity"],
        "wind_kph": round(data["wind"]["speed"] * 3.6, 1),
    }



async def call_resend(params: dict, api_key: str) -> dict:
    from_addr = params.get("from", "Wayforth <noreply@wayforth.io>")
    to_addr = params.get("to", "")
    subject = params.get("subject", "")
    html = params.get("html", "")
    if not to_addr or not subject:
        raise Exception("params.to and params.subject are required")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": from_addr, "to": to_addr, "subject": subject, "html": html or subject},
        )
    if r.status_code == 403:
        raise Exception(
            "Resend 403: The 'from' address must use a verified domain. "
            "Verify your domain at resend.com/domains before sending."
        )
    if r.status_code not in (200, 201):
        raise Exception(f"Resend error {r.status_code}: {r.text[:200]}")
    data = r.json()
    return {"email_id": data.get("id", ""), "status": "sent"}


async def call_serper(params: dict, api_key: str) -> dict:
    q = params.get("q") or params.get("query", "")
    if not q:
        raise Exception("params.q or params.query is required")
    num = min(int(params.get("num", 5)), 10)
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": q, "num": num, "gl": params.get("gl", "us")},
        )
    if r.status_code != 200:
        raise Exception(f"Serper error {r.status_code}: {r.text[:200]}")
    data = r.json()
    organic = []
    for item in data.get("organic", [])[:num]:
        organic.append({
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "snippet": item.get("snippet", ""),
        })
    result: dict = {"organic": organic}
    answer_box = data.get("answerBox", {})
    if answer_box:
        result["answer_box"] = answer_box.get("answer") or answer_box.get("snippet", "")
    return result


async def call_assemblyai(params: dict, api_key: str) -> dict:
    audio_url = params.get("audio_url", "")
    if not audio_url:
        raise Exception("params.audio_url is required")
    # FINDING-101: audio_url is user-supplied and is both HEADed by us and fetched
    # server-side by AssemblyAI — reject internal/loopback/link-local targets
    # before any HTTP request to close the SSRF / metadata-probe vector.
    validate_external_url(audio_url, field_name="audio_url")
    # Tier 1 input cap: ~10 min audio (~12 MB Content-Length heuristic).
    # HEAD the URL to check size; allow through if Content-Length is absent or HEAD fails.
    try:
        async with httpx.AsyncClient(timeout=5.0) as head_client:
            head_resp = await head_client.head(audio_url)
            content_length = head_resp.headers.get("content-length")
            if content_length is not None and int(content_length) > 12_000_000:
                raise HTTPException(413, "Audio file exceeds Tier 1 limit (~10 min). Use BYOK for larger files.")
            if content_length is None:
                logger.info(
                    "assemblyai: audio size unknown (no Content-Length on HEAD %s) — allowing through", audio_url
                )
    except HTTPException:
        raise
    except Exception as _head_err:
        logger.info("assemblyai: HEAD request failed (%s) — allowing through", _head_err)
    language_code = params.get("language_code", "en")
    headers = {"authorization": api_key, "content-type": "application/json"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.assemblyai.com/v2/transcript",
            headers=headers,
            json={
                "audio_url": audio_url,
                "language_code": language_code,
                "speech_models": params.get("speech_models", ["universal-2"]),
            },
        )
    if r.status_code != 200:
        raise Exception(f"AssemblyAI submit error {r.status_code}: {r.text[:200]}")
    job = r.json()
    transcript_id = job["id"]
    poll_url = f"https://api.assemblyai.com/v2/transcript/{transcript_id}"

    deadline = asyncio.get_event_loop().time() + 30
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            await asyncio.sleep(2)
            pr = await client.get(poll_url, headers=headers)
            if pr.status_code != 200:
                raise Exception(f"AssemblyAI poll error {pr.status_code}: {pr.text[:200]}")
            status = pr.json().get("status", "")
            if status == "completed":
                return {
                    "transcript_id": transcript_id,
                    "text": pr.json().get("text", ""),
                    "status": "completed",
                }
            if status == "error":
                raise Exception(f"AssemblyAI transcription failed: {pr.json().get('error','unknown')}")
            if asyncio.get_event_loop().time() >= deadline:
                return {
                    "status": "processing",
                    "transcript_id": transcript_id,
                    "poll_url": poll_url,
                }


async def call_stability(params: dict, api_key: str) -> dict:
    prompt = params.get("prompt", "")
    if not prompt:
        raise Exception("params.prompt is required")
    # Tier 1: 1 image per call (credit cost enforces this implicitly)
    samples = int(params.get("samples", 1))
    n = int(params.get("n", 1))
    if samples > 1 or n > 1:
        raise HTTPException(413, "Stability AI Tier 1 limit: 1 image per call. Use BYOK for batch generation.")
    quality = params.get("quality", "core")  # "core" | "ultra"

    if quality == "ultra":
        # Stability AI v2beta Ultra endpoint uses multipart form
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://api.stability.ai/v2beta/stable-image/generate/ultra",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                data={
                    "prompt": prompt,
                    "output_format": "png",
                    "aspect_ratio": params.get("aspect_ratio", "1:1"),
                },
                files={"none": ("", b"")},  # multipart requires at least one file field
            )
        if r.status_code not in (200, 201):
            raise Exception(f"Stability Ultra error {r.status_code}: {r.text[:200]}")
        data = r.json()
        return {
            "image_base64": data.get("image", ""),
            "seed": data.get("seed", 0),
            "finish_reason": data.get("finish_reason", "SUCCESS"),
            "quality": "ultra",
        }
    else:
        # v1 SDXL endpoint (core quality)
        text_prompts = [{"text": prompt, "weight": 1.0}]
        negative_prompt = params.get("negative_prompt", "")
        if negative_prompt:
            text_prompts.append({"text": negative_prompt, "weight": -1.0})
        body = {
            "text_prompts": text_prompts,
            "width": int(params.get("width", 1024)),
            "height": int(params.get("height", 1024)),
            "steps": int(params.get("steps", 30)),
            "samples": 1,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json=body,
            )
        if r.status_code != 200:
            raise Exception(f"Stability AI error {r.status_code}: {r.text[:200]}")
        data = r.json()
        artifact = data["artifacts"][0]
        return {
            "image_base64": artifact["base64"],
            "seed": artifact.get("seed", 0),
            "finish_reason": artifact.get("finishReason", "SUCCESS"),
            "quality": "core",
        }


async def call_tavily(params: dict, api_key: str) -> dict:
    query = params.get("query", "")
    if not query:
        raise Exception("params.query is required")
    max_results = min(int(params.get("max_results", 5)), 10)
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.tavily.com/search",
            headers={"Content-Type": "application/json"},
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": params.get("search_depth", "basic"),
                "max_results": max_results,
            },
        )
    if r.status_code != 200:
        raise Exception(f"Tavily error {r.status_code}: {r.text[:200]}")
    data = r.json()
    results = [
        {"title": item.get("title", ""), "url": item.get("url", ""), "content": item.get("content", "")}
        for item in data.get("results", [])
    ]
    return {
        "query": data.get("query", query),
        "results": results,
        "answer": data.get("answer"),
    }


# AlphaVantage free tier: 5 calls/minute. Track timestamps to queue instead of fail.
_av_call_times: list[float] = []
_AV_WINDOW_SEC = 60
_AV_LIMIT = 5
_AV_BACKOFF_SEC = 15


async def call_alphavantage(params: dict, api_key: str) -> dict:
    global _av_call_times
    now = _time.time()
    _av_call_times = [t for t in _av_call_times if now - t < _AV_WINDOW_SEC]
    if len(_av_call_times) >= _AV_LIMIT:
        await asyncio.sleep(_AV_BACKOFF_SEC)
    _av_call_times.append(_time.time())

    symbol = params.get("symbol", "").upper()
    if not symbol:
        raise Exception("params.symbol is required (e.g. 'AAPL')")
    _httpx_log.setLevel(logging.WARNING)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # GLOBAL_QUOTE: no daily call-count cap on free tier (vs TIME_SERIES_DAILY's 25/day limit)
            r = await client.get(
                "https://www.alphavantage.co/query",
                params={
                    "function": "GLOBAL_QUOTE",
                    "symbol": symbol,
                    "apikey": api_key,
                },
            )
    finally:
        _httpx_log.setLevel(logging.NOTSET)
    if r.status_code != 200:
        raise Exception(f"Alpha Vantage error {r.status_code}: {r.text[:200]}")
    data = r.json()
    if "Error Message" in data:
        raise Exception(f"Alpha Vantage: {data['Error Message'][:200]}")
    if "Note" in data or "Information" in data:
        raise Exception("Alpha Vantage: rate limit reached — try again shortly")
    quote = data.get("Global Quote", {})
    if not quote or not quote.get("05. price"):
        raise Exception(f"Alpha Vantage: unexpected response — {list(data.keys())}")
    return {
        "symbol": quote.get("01. symbol", symbol),
        "price": float(quote["05. price"]),
        "open": float(quote.get("02. open", 0)),
        "high": float(quote.get("03. high", 0)),
        "low": float(quote.get("04. low", 0)),
        "previous_close": float(quote.get("08. previous close", 0)),
        "change": float(quote.get("09. change", 0)),
        "change_pct": quote.get("10. change percent", "0%"),
        "volume": int(quote.get("06. volume", 0)),
        "latest_trading_day": quote.get("07. latest trading day", ""),
    }


async def call_jina(params: dict, api_key: str) -> dict:
    url = params.get("url", "")
    if not url:
        raise Exception("params.url is required")
    # FINDING-101 / DECISION 1: url is user-supplied and fetched server-side by
    # Jina — reject internal/loopback/link-local targets before the request.
    # Relaxed validator: http:// pages are legitimate scrape targets; internal
    # IP/host blocking is unchanged.
    validate_external_url_relaxed(url, field_name="url")
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"https://r.jina.ai/{url}",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "X-Return-Format": "markdown",
            },
        )
    if r.status_code != 200:
        raise Exception(f"Jina error {r.status_code}: {r.text[:200]}")
    data = r.json()
    inner = data.get("data", {})
    return {
        "title": inner.get("title", ""),
        "content": inner.get("content", ""),
        "url": inner.get("url", url),
    }


async def call_elevenlabs(params: dict, api_key: str) -> dict:
    text = params.get("text", "")
    if not text:
        raise Exception("params.text is required")
    # Tier 1 input cap: 500 characters
    if len(text) > 500:
        raise HTTPException(413, "Text exceeds Tier 1 limit (500 chars). Use BYOK for larger payloads.")
    voice_id = params.get("voice_id", "21m00Tcm4TlvDq8ikWAM")  # default: Rachel
    model_id = params.get("model_id", "eleven_multilingual_v2")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": model_id,
                "voice_settings": {
                    "stability": float(params.get("stability", 0.5)),
                    "similarity_boost": float(params.get("similarity_boost", 0.75)),
                },
            },
        )
    if r.status_code != 200:
        raise Exception(f"ElevenLabs error {r.status_code}: {r.text[:200]}")
    return {
        "audio_base64": base64.b64encode(r.content).decode(),
        "content_type": "audio/mpeg",
        "voice_id": voice_id,
        "characters": len(text),
    }


async def call_together(params: dict, api_key: str) -> dict:
    messages = params.get("messages", [])
    if not messages:
        raise Exception("params.messages is required")
    model = params.get("model", "meta-llama/Llama-3.3-70B-Instruct-Turbo")
    max_tokens = params.get("max_tokens", 1024)
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.together.xyz/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
    if r.status_code != 200:
        raise Exception(f"Together AI error {r.status_code}: {r.text[:200]}")
    data = r.json()
    choice = data["choices"][0]
    usage = data.get("usage", {})
    return {
        "content": choice["message"]["content"],
        "model": data.get("model", model),
        "tokens_used": usage.get("total_tokens", 0),
    }


async def call_perplexity(params: dict, api_key: str) -> dict:
    messages = params.get("messages", [])
    if not messages:
        raise Exception("params.messages is required")
    model = params.get("model", "sonar")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            "https://api.perplexity.ai/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages},
        )
    if r.status_code != 200:
        raise Exception(f"Perplexity error {r.status_code}: {r.text[:200]}")
    data = r.json()
    choice = data["choices"][0]
    return {
        "content": choice["message"]["content"],
        "model": data.get("model", model),
        "citations": data.get("citations", []),
    }


async def call_brave(params: dict, api_key: str) -> dict:
    query = params.get("query") or params.get("q", "")
    if not query:
        raise Exception("params.query is required")
    count = min(int(params.get("count", 10)), 20)
    qp: dict = {"q": query, "count": count}
    freshness = params.get("freshness")
    if freshness in ("pd", "pw", "pm", "py"):
        qp["freshness"] = freshness
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": api_key},
            params=qp,
        )
    if r.status_code != 200:
        raise Exception(f"Brave Search error {r.status_code}: {r.text[:200]}")
    data = r.json()
    results = []
    for item in (data.get("web", {}).get("results") or [])[:count]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "description": item.get("description", ""),
        })
    return {"query": query, "results": results}


async def call_firecrawl(params: dict, api_key: str) -> dict:
    url = params.get("url", "")
    if not url:
        raise Exception("params.url is required")
    # FINDING-101 / DECISION 1: url is user-supplied and fetched server-side by
    # Firecrawl — reject internal/loopback/link-local targets before the request.
    # Relaxed validator: http:// pages are legitimate scrape targets; internal
    # IP/host blocking is unchanged.
    validate_external_url_relaxed(url, field_name="url")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            "https://api.firecrawl.dev/v1/scrape",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"url": url, "formats": params.get("formats", ["markdown"])},
        )
    if r.status_code not in (200, 201):
        raise Exception(f"Firecrawl error {r.status_code}: {r.text[:200]}")
    data = r.json()
    inner = data.get("data", data)
    return {
        "url": url,
        "markdown": inner.get("markdown", ""),
        "title": inner.get("metadata", {}).get("title", ""),
    }


async def call_mistral(params: dict, api_key: str) -> dict:
    messages = params.get("messages", [])
    if not messages:
        raise Exception("params.messages is required")
    model = params.get("model", "mistral-small-latest")
    max_tokens = params.get("max_tokens", 1024)
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": model, "messages": messages, "max_tokens": max_tokens},
        )
    if r.status_code != 200:
        raise Exception(f"Mistral error {r.status_code}: {r.text[:200]}")
    data = r.json()
    choice = data["choices"][0]
    usage = data.get("usage", {})
    return {
        "content": choice["message"]["content"],
        "model": data.get("model", model),
        "tokens_used": usage.get("total_tokens", 0),
    }


async def call_gemini(params: dict, api_key: str) -> dict:
    prompt = params.get("prompt", "")
    messages = params.get("messages", [])
    if not prompt and not messages:
        raise Exception("params.prompt or params.messages is required")
    if messages and not prompt:
        # Convert messages format to Gemini contents
        contents = [{"role": m["role"].replace("assistant", "model"), "parts": [{"text": m["content"]}]} for m in messages]
    else:
        contents = [{"parts": [{"text": prompt}]}]
    model = params.get("model", "gemini-2.5-flash")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
            headers={"Content-Type": "application/json"},
            json={"contents": contents},
        )
    if r.status_code != 200:
        raise Exception(f"Gemini error {r.status_code}: {r.text[:200]}")
    data = r.json()
    candidate = data.get("candidates", [{}])[0]
    content_parts = candidate.get("content", {}).get("parts", [{}])
    text = content_parts[0].get("text", "") if content_parts else ""
    usage = data.get("usageMetadata", {})
    return {
        "content": text,
        "model": model,
        "tokens_used": usage.get("totalTokenCount", 0),
    }


ADAPTERS = {
    "groq":        call_groq,
    "together":    call_together,
    "deepl":       call_deepl,
    "serper":      call_serper,
    "tavily":      call_tavily,
    "brave":       call_brave,
    "perplexity":  call_perplexity,
    "openweather": call_openweather,

    "alphavantage":call_alphavantage,
    "jina":        call_jina,
    "assemblyai":  call_assemblyai,
    "elevenlabs":  call_elevenlabs,
    "stability":   call_stability,
    "resend":      call_resend,
    "firecrawl":   call_firecrawl,
    "mistral":     call_mistral,
    "gemini":      call_gemini,
}
