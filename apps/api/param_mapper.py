"""param_mapper.py — normalise agent input dicts to service-specific param schemas."""

# Maps canonical param names to recognised aliases from user input
PARAM_ALIASES: dict[str, list[str]] = {
    "text":        ["text", "content", "message", "query", "prompt", "input"],
    "target_lang": ["target_lang", "target_language", "lang", "language", "to"],
    "source_lang": ["source_lang", "source_language", "from"],
    "city":        ["city", "location", "place", "where"],
    "url":         ["url", "link", "href", "uri"],
    "query":       ["query", "q", "search", "term", "question"],
    "prompt":      ["prompt", "description", "image_prompt", "generate"],
    "audio_url":   ["audio_url", "audio", "file", "recording"],
    "symbol":      ["symbol", "ticker", "stock"],
    "voice_id":    ["voice_id", "voice"],
}

# Required params per managed service slug
SERVICE_REQUIRED_PARAMS: dict[str, list[str]] = {
    "deepl":        ["text", "target_lang"],
    "groq":         ["messages"],
    "together":     ["messages"],
    "stability":    ["prompt"],
    "assemblyai":   ["audio_url"],
    "elevenlabs":   ["text", "voice_id"],
    "serper":       ["query"],
    "tavily":       ["query"],
    "brave":        ["query"],
    "perplexity":   ["messages"],
    "openweather":  ["city"],
    "newsapi":      ["query"],
    "alphavantage": ["symbol"],
    "jina":         ["url"],
    "resend":       ["to", "subject", "html"],
}

# Default values injected before validation; user values override these
SERVICE_DEFAULTS: dict[str, dict] = {
    "deepl":        {"source_lang": "EN"},
    "groq":         {"model": "llama-3.3-70b-versatile", "max_tokens": 1024},
    "together":     {"model": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo", "max_tokens": 1024},
    "stability":    {"steps": 30, "cfg_scale": 7},
    "newsapi":      {"pageSize": 5},
    "alphavantage": {"function": "GLOBAL_QUOTE"},
    "tavily":       {"max_results": 5},
    "brave":        {"count": 10},
}

# Catalog slug → SERVICE_CONFIGS key (handles naming mismatches from migration 033)
CATALOG_TO_MANAGED: dict[str, str] = {
    "groq":             "groq",
    "together_ai":      "together",
    "together":         "together",
    "deepl":            "deepl",
    "serper":           "serper",
    "serper_api":       "serper",
    "tavily_ai_search": "tavily",
    "tavily":           "tavily",
    "brave_search":     "brave",
    "brave_search_2":   "brave",
    "brave":            "brave",
    "perplexity_ai":    "perplexity",
    "perplexity":       "perplexity",
    "openweathermap":   "openweather",
    "openweather":      "openweather",
    "newsapi":          "newsapi",
    "alpha_vantage":    "alphavantage",
    "alphavantage":     "alphavantage",
    "jina_embeddings":  "jina",
    "jina":             "jina",
    "assemblyai":       "assemblyai",
    "elevenlabs":       "elevenlabs",
    "stability_ai":     "stability",
    "stability":        "stability",
    "resend":           "resend",
}

# One-line hints shown in 422 missing_param errors
_PARAM_HINTS: dict[str, str] = {
    "target_lang": "Add target_lang. Example: 'ES' for Spanish, 'FR' for French, 'DE' for German.",
    "messages":    "Add messages array. Example: [{'role': 'user', 'content': 'Hello'}]",
    "symbol":      "Add stock symbol. Example: 'AAPL' for Apple, 'TSLA' for Tesla.",
    "audio_url":   "Add audio_url pointing to an MP3/WAV file. Example: 'https://example.com/audio.mp3'",
    "city":        "Add city name. Example: 'Tokyo', 'London', 'New York'.",
    "query":       "Add search query. Example: 'latest AI news'.",
    "url":         "Add the URL to read. Example: 'https://example.com/article'.",
    "prompt":      "Add image prompt. Example: 'a sunset over the ocean, photorealistic'.",
    "voice_id":    "Add ElevenLabs voice_id. Example: '21m00Tcm4TlvDq8ikWAM' (Rachel).",
    "to":          "Add recipient email. Example: 'user@example.com'.",
    "subject":     "Add email subject line.",
    "html":        "Add HTML email body.",
}


def map_params(service_slug: str, input_dict: dict) -> tuple[dict, list[str]]:
    """Map agent's input dict to the service's expected param schema.

    Returns (mapped_params, missing_required).
    mapped_params includes defaults + user values + any alias-resolved values.
    missing_required lists params that are required but not provided.
    """
    # Start with defaults, then overlay user-supplied values (user wins)
    mapped: dict = dict(SERVICE_DEFAULTS.get(service_slug, {}))
    mapped.update(input_dict)

    # Resolve aliases: if canonical key is absent, check aliases
    for canonical, aliases in PARAM_ALIASES.items():
        if canonical not in mapped:
            for alias in aliases:
                if alias in input_dict and alias != canonical:
                    mapped[canonical] = input_dict[alias]
                    break

    # Special case: inference services accept plain text → wrap as messages
    if service_slug in ("groq", "together", "perplexity") and "messages" not in mapped:
        text = mapped.get("text") or mapped.get("content") or mapped.get("prompt")
        if text:
            mapped["messages"] = [{"role": "user", "content": str(text)}]

    required = SERVICE_REQUIRED_PARAMS.get(service_slug, [])
    missing = [p for p in required if p not in mapped]

    return mapped, missing


def missing_param_hint(missing: list[str]) -> str:
    """Build a human-readable hint for missing params."""
    return " ".join(_PARAM_HINTS.get(p, f"Add '{p}' to your input.") for p in missing)


INTENT_CATEGORY_HINTS: dict[str, list[str]] = {
    "translation": [
        "translat", "spanish", "french", "german", "italian",
        "japanese", "portuguese", "chinese", "korean", "arabic",
        "language", "languages", "english to", "to english",
        "into english", "into spanish", "into french",
    ],
    "inference": [
        "summarize", "summarise", "explain", "write", "generate text",
        "rewrite", "paraphrase", "chat", "llm", "gpt", "ask",
        "answer", "complete", "draft",
    ],
    "search": [
        "search", "find", "look up", "google", "web search",
        "news", "browse", "latest", "articles",
    ],
    "data": [
        "weather", "temperature", "forecast", "climate",
        "rain", "sunny", "humidity", "wind",
        "stock", "price", "market", "financial", "crypto",
        "trading", "ticker", "shares", "equity",
        "news", "latest", "headlines", "articles",
    ],
    "finance": [
        "alphavantage", "alpha vantage", "stock quote",
    ],
    "image": [
        "image", "picture", "photo", "generate image",
        "draw", "stable diffusion", "dalle", "illustration",
    ],
    "audio": [
        "transcribe", "transcription", "speech", "audio",
        "voice", "recording", "podcast", "mp3",
    ],
}


# Managed slug → canonical catalog slug in the services table
MANAGED_TO_CATALOG: dict[str, str] = {
    "groq":        "groq",
    "together":    "together_ai",
    "deepl":       "deepl",
    "serper":      "serper",
    "tavily":      "tavily_ai_search",
    "brave":       "brave_search_2",
    "perplexity":  "perplexity_ai",
    "openweather": "openweathermap",
    "newsapi":     "newsapi",
    "alphavantage":"alpha_vantage",
    "jina":        "jina_reader",
    "assemblyai":  "assemblyai",
    "elevenlabs":  "elevenlabs",
    "stability":   "stability_ai",
    "resend":      "resend",
}


# Maps detected intent category → DB service categories considered compatible.
# Serper/Tavily/Brave live in "data" not "search" in the DB, so "search" intent
# must allow both. Keep each list tight — too broad defeats the purpose.
INTENT_CATEGORY_MAP: dict[str, list[str]] = {
    "translation": ["translation"],
    "inference":   ["inference", "llm", "ai"],
    "search":      ["search", "data"],
    "data":        ["data", "search"],
    "finance":     ["finance", "data"],
    "image":       ["image", "media"],
    "audio":       ["audio", "media"],
    "email":       ["email", "communication"],
}


def detect_category_hint(intent: str) -> str | None:
    """Return a category name if intent strongly signals one, else None."""
    intent_lower = intent.lower()
    for category, keywords in INTENT_CATEGORY_HINTS.items():
        if any(kw in intent_lower for kw in keywords):
            return category
    return None
