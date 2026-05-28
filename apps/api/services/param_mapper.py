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
    "firecrawl":    ["url"],
    "mistral":      ["messages"],
    "gemini":       ["prompt"],
}

# Default values injected before validation; user values override these
SERVICE_DEFAULTS: dict[str, dict] = {
    "deepl":        {"source_lang": "EN"},
    "groq":         {"model": "llama-3.3-70b-versatile", "max_tokens": 1024},
    "together":     {"model": "meta-llama/Llama-3.3-70B-Instruct-Turbo", "max_tokens": 1024},
    "stability":    {"steps": 30, "cfg_scale": 7},
    "newsapi":      {"pageSize": 5},
    "alphavantage": {"function": "GLOBAL_QUOTE"},
    "tavily":       {"max_results": 5},
    "brave":        {"count": 10},
    "mistral":      {"model": "mistral-small-latest", "max_tokens": 1024},
    "gemini":       {"model": "gemini-2.5-flash"},
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
    "firecrawl":        "firecrawl",
    "mistral":          "mistral",
    "mistral_ai":       "mistral",
    "gemini":           "gemini",
    "gemini_flash":     "gemini",
    "google_gemini":    "gemini",
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
    if service_slug in ("groq", "together", "perplexity", "mistral") and "messages" not in mapped:
        text = mapped.get("text") or mapped.get("content") or mapped.get("prompt")
        if text:
            mapped["messages"] = [{"role": "user", "content": str(text)}]

    required = SERVICE_REQUIRED_PARAMS.get(service_slug, [])
    missing = [p for p in required if p not in mapped]

    return mapped, missing


def missing_param_hint(missing: list[str]) -> str:
    """Build a human-readable hint for missing params."""
    return " ".join(_PARAM_HINTS.get(p, f"Add '{p}' to your input.") for p in missing)


# Keywords that unambiguously signal LLM inference intent.
# When ANY of these appear in the intent string, "inference" is returned
# immediately — before scanning other categories — so weak TTS/audio
# signals like "say" can never override an explicit LLM request.
_STRONG_INFERENCE_SIGNALS: frozenset[str] = frozenset([
    "inference", "fast inference", "llm", "groq", "together",
    "together ai", "generate text", "language model", "prompt",
    "run inference", "llm inference", "run model", "run llm", "model inference",
])

INTENT_CATEGORY_HINTS: dict[str, list[str]] = {
    # Ordered from most-specific to most-general so detect_category_hint
    # returns the tightest match first.
    "translation": [
        "translat", "spanish", "french", "german", "italian",
        "japanese", "portuguese", "chinese", "korean", "arabic",
        "language", "languages", "english to", "to english",
        "into english", "into spanish", "into french",
    ],
    "inference": [
        "inference", "fast inference", "run inference", "llm inference",
        "run model", "run llm", "model inference",
        "summarize", "summarise", "explain",
        "write", "generate text", "rewrite", "paraphrase", "chat",
        "llm", "gpt", "ask", "complete", "draft", "language model",
    ],
    "research": [
        "research", "research this", "find research",
        "perplexity", "deep dive", "in-depth",
        "fact check", "comprehensive answer", "explain in detail",
        "what does", "how does", "why does", "investigate",
        "background on", "overview of", "question and answer", "q&a",
    ],
    "image": [
        "image", "picture", "photo", "generate image", "generate an image",
        "draw", "stable diffusion", "dalle", "illustration",
        "stability ai", "midjourney", "render a", "create an image",
    ],
    "tts": [
        "text to speech", "tts", "say this", "speak this", "read aloud",
        "voice over", "narrate", "elevenlabs", "synthesize speech",
        "generate speech", "audio from text", "convert to audio",
    ],
    "weather": [
        "weather", "temperature", "forecast",
        "rain today", "is it raining", "sunny today", "humidity", "wind speed", "meteorolog",
        "weather in", "weather for", "what's the weather", "how's the weather",
    ],
    "financial": [
        "stock price", "stock quote", "share price", "market cap",
        "financial data", "market data", "alpha vantage", "alphavantage",
        "equity price", "ticker symbol", "trading price", "stock market",
        "get stock", "look up stock",
    ],
    "search": [
        "search the web", "web search", "find on the web", "google",
        "brave", "serper", "tavily", "browse", "look up",
        "find articles", "latest news", "news articles",
    ],
    "data": [
        "stock", "price", "market", "financial", "crypto",
        "trading", "ticker", "shares", "news", "latest", "headlines",
    ],
    "finance": [
        "alphavantage", "alpha vantage", "stock quote",
    ],
    "audio": [
        "transcribe", "transcription", "speech to text",
        "audio", "recording", "podcast", "mp3", "wav",
        "convert audio", "speech recognition",
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
    "firecrawl":   "firecrawl",
    "mistral":     "mistral_ai",
    "gemini":      "gemini_flash",
}


# Maps detected intent category → DB service categories considered compatible.
# Serper/Tavily/Brave live in "data" not "search" in the DB, so "search" intent
# must allow both. Keep each list tight — too broad defeats the purpose.
INTENT_CATEGORY_MAP: dict[str, list[str]] = {
    "translation": ["translation"],
    "inference":   ["inference", "llm", "ai"],
    "research":    ["inference", "search", "data", "ai", "llm"],
    "image":       ["image", "media"],
    "tts":         ["audio", "media"],
    "weather":     ["data"],
    "financial":   ["finance", "data"],
    "search":      ["search", "data"],
    "data":        ["data", "search"],
    "finance":     ["finance", "data"],
    "audio":       ["audio", "media"],
    "email":       ["email", "communication"],
}


def detect_category_hint(intent: str) -> str | None:
    """Return a category name if intent strongly signals one, else None."""
    intent_lower = intent.lower()
    if any(sig in intent_lower for sig in _STRONG_INFERENCE_SIGNALS):
        return "inference"
    for category, keywords in INTENT_CATEGORY_HINTS.items():
        if any(kw in intent_lower for kw in keywords):
            return category
    return None


_LANGUAGE_CODES: dict[str, str] = {
    "spanish": "ES", "french": "FR", "german": "DE", "italian": "IT",
    "portuguese": "PT", "dutch": "NL", "polish": "PL", "russian": "RU",
    "japanese": "JA", "chinese": "ZH", "korean": "KO", "arabic": "AR",
    "turkish": "TR", "swedish": "SV", "danish": "DA", "norwegian": "NB",
    "finnish": "FI", "czech": "CS", "romanian": "RO", "hungarian": "HU",
    "greek": "EL", "bulgarian": "BG", "croatian": "HR", "slovak": "SK",
    "slovenian": "SL", "estonian": "ET", "latvian": "LV", "lithuanian": "LT",
    "ukrainian": "UK", "indonesian": "ID", "english": "EN",
}


_CITY_ALIASES: dict[str, str] = {
    "nyc":       "New York",
    "new york city": "New York",
    "la":        "Los Angeles",
    "l.a.":      "Los Angeles",
    "sf":        "San Francisco",
    "san fran":  "San Francisco",
    "dc":        "Washington DC",
    "washington d.c.": "Washington DC",
    "ldn":       "London",
    "chi":       "Chicago",
    "windy city": "Chicago",
    "mia":       "Miami",
    "las":       "Las Vegas",
    "sin city":  "Las Vegas",
    "phx":       "Phoenix",
    "pdx":       "Portland",
    "dfw":       "Dallas",
    "atl":       "Atlanta",
    "bos":       "Boston",
    "sea":       "Seattle",
    "den":       "Denver",
    "msp":       "Minneapolis",
}


def _normalize_city(city: str) -> str:
    """Replace common city abbreviations/aliases with full names."""
    return _CITY_ALIASES.get(city.lower().strip(), city)


def extract_params_from_intent(intent: str) -> dict:
    """Best-effort extraction of structured params from a plain-English intent string.

    Handles translation, weather, financial, web search, image, and TTS intents.
    Returns an empty dict when nothing can be extracted.
    """
    import re
    lower = intent.lower()

    # Translation: "translate <text> to/into <language>"
    m = re.search(
        r"translat\w*\s+(.+?)\s+(?:to|into|in)\s+([a-z]+)",
        lower,
        re.IGNORECASE,
    )
    if m:
        raw_text = m.group(1).strip()
        lang_word = m.group(2).strip().lower()
        lang_code = _LANGUAGE_CODES.get(lang_word)
        if lang_code and raw_text:
            orig_m = re.search(
                r"translat\w*\s+(.+?)\s+(?:to|into|in)\s+[a-z]+",
                intent,
                re.IGNORECASE,
            )
            text = orig_m.group(1).strip() if orig_m else raw_text
            return {"text": text, "target_lang": lang_code}

    # Weather: "weather in Tokyo" / "forecast for London" / "temperature in Paris"
    m = re.search(
        r"(?:weather|forecast|temperature|rain|humidity|climate|sunny|wind)\s+(?:in|for|at)\s+([A-Za-z][A-Za-z\s]{1,30}?)(?:\s*[?,.]|$)",
        intent,
        re.IGNORECASE,
    )
    if m:
        city = _normalize_city(m.group(1).strip())
        if city:
            return {"city": city}

    # Financial: ticker must appear as truly uppercase letters (no re.IGNORECASE on the
    # capture group) so common words like "QUOTE" or "PRICE" are never captured.
    # Pattern 1: "stock price for AAPL" / "quote for MSFT"
    m = re.search(
        r"(?:stock|price|quote|ticker|shares?|equity)\s+(?:price\s+)?(?:for\s+)?([A-Z]{1,5})\b",
        intent,
    )
    if not m:
        # Pattern 2: "TSLA stock" / "AAPL quote"
        m = re.search(r"\b([A-Z]{2,5})\s+(?:stock\s+)?(?:price|quote|ticker)", intent)
    if m:
        return {"symbol": m.group(1).upper()}

    # Web search: "search the web for X" / "find articles about X" / "look up X"
    m = re.search(
        r"(?:search(?:\s+the\s+web)?|find\s+articles?|look\s+up|browse|web\s+search)\s+(?:for|about|on)?\s+(.+?)(?:\s*[?.]|$)",
        intent,
        re.IGNORECASE,
    )
    if m:
        query = m.group(1).strip()
        if query:
            return {"query": query}

    # Image generation: "generate an image of X" / "draw X" / "create a photo of X"
    m = re.search(
        r"(?:generate|create|draw|render|make|produce)\s+(?:an?\s+)?(?:image|photo|picture|illustration|artwork|painting)\s+(?:of|showing|depicting|with)?\s*(.+?)(?:\s*[?.]|$)",
        intent,
        re.IGNORECASE,
    )
    if m:
        prompt = m.group(1).strip()
        if prompt:
            return {"prompt": prompt}

    # TTS: "say this: X" / "speak this text: X" / "read aloud: X"
    m = re.search(
        r"(?:say|speak|read\s+aloud|narrate|voice\s+over)\s*(?:this\s*)?(?:text\s*)?[:—]\s*(.+)",
        intent,
        re.IGNORECASE,
    )
    if m:
        text = m.group(1).strip()
        if text:
            return {"text": text}

    return {}
