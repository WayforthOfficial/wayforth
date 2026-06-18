"""services/templates_specs.py — compact specs for the generated catalog.

One dict per template; templates_catalog.py turns each into a full dual-runtime
template. Specs are present-tense and ready-to-go: every input has a sensible
default so the agent runs green with zero configuration.
"""
from __future__ import annotations

# Managed credit cost per service (+1 compute credit per run).
_CRED = {
    "alphavantage": 4, "assemblyai": 25, "brave": 6, "deepl": 20, "elevenlabs": 200,
    "firecrawl": 6, "gemini": 3, "groq": 3, "jina": 4, "mistral": 4, "openweather": 2,
    "perplexity": 10, "resend": 3, "serper": 3, "stability": 86, "tavily": 10, "together": 4,
}
_MODEL = {
    "groq": "llama-3.3-70b-versatile",
    "together": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
    "mistral": "mistral-small-latest",
    "perplexity": "sonar",
    "gemini": "gemini-2.5-flash",
}


def _cred(*slugs: str) -> int:
    return sum(_CRED[s] for s in slugs) + 1


def _bd(*slugs: str) -> str:
    return " + ".join(f"{_CRED[s]} {s}" for s in slugs) + " + 1 compute"


def _env(name, desc, default, required=False):
    return {"name": name, "description": desc, "required": required, "default": default}


# ── LLM templates (groq / together / mistral / perplexity accept messages) ─────

def _llm(id, name, tagline, slug, system, default_prompt, tags, category="ai"):
    return {
        "id": id, "name": name, "tagline": tagline, "category": category, "shape": "llm",
        "slug": slug, "system": system, "default_prompt": default_prompt,
        "model": _MODEL[slug], "services_used": [slug],
        "credits": _cred(slug), "breakdown": _bd(slug),
        "env_vars": [
            _env("PROMPT", "Input text the agent processes", default_prompt),
            _env("MODEL", f"{slug} model id", _MODEL[slug]),
        ],
        "tags": tags,
    }


_LLM_SPECS = [
    _llm("summarizer", "Text Summarizer", "Summarizes any text into a tight set of bullet points via Groq.",
         "groq", "You are a concise summarizer. Reduce the user's text to 3-5 factual bullet points.",
         "Paste any long article or document here and the agent returns the key points.",
         ["summary", "groq", "ai", "text"], "content"),
    _llm("classifier", "Text Classifier", "Classifies input text into a labeled category with a one-line rationale.",
         "groq", "You are a classifier. Return the single best category label and a one-sentence reason.",
         "Customer message: 'My invoice is wrong and I want a refund.'",
         ["classification", "groq", "ai"], "content"),
    _llm("sentiment-analyzer", "Sentiment Analyzer", "Scores the sentiment of text as positive, negative, or neutral with confidence.",
         "groq", "You are a sentiment analyzer. Return sentiment (positive/negative/neutral) and a 0-1 confidence.",
         "I absolutely love this product, it changed how I work!",
         ["sentiment", "groq", "ai"], "content"),
    _llm("code-explainer", "Code Explainer", "Explains a code snippet in plain language, step by step.",
         "groq", "You are a senior engineer. Explain the given code clearly, step by step.",
         "def fib(n):\\n    return n if n < 2 else fib(n-1) + fib(n-2)",
         ["code", "groq", "developer", "ai"], "developer"),
    _llm("rewriter", "Text Rewriter", "Rewrites text to be clearer and more polished while keeping the meaning.",
         "together", "You rewrite text to be clearer, tighter, and more professional. Keep the meaning.",
         "we was thinking maybe you could possibly send the report when you get a chance thx",
         ["rewrite", "together", "ai", "editing"]),
    _llm("email-writer", "Email Writer", "Drafts a professional email from a short brief via Mistral.",
         "mistral", "You write concise, professional emails. Output subject and body.",
         "Ask a client to reschedule Thursday's call to next Tuesday at 10am.",
         ["email", "mistral", "writing", "ai"], "content"),
    _llm("tweet-thread", "Thread Writer", "Turns an idea into a punchy 5-tweet social thread.",
         "groq", "You write engaging social threads. Produce 5 numbered posts, each under 280 chars.",
         "Why AI agents will change how small businesses operate",
         ["social", "groq", "marketing", "ai"], "content"),
    _llm("meeting-notes", "Meeting Notes", "Converts a raw meeting transcript into notes, decisions, and action items.",
         "groq", "You summarize meetings into Notes, Decisions, and Action Items (with owners).",
         "Alice: we should ship Friday. Bob: I'll finish the API by Thursday. Carol: I'll handle QA.",
         ["meeting", "groq", "productivity", "ai"], "productivity"),
    _llm("product-describer", "Product Describer", "Writes compelling e-commerce product descriptions from feature bullets.",
         "together", "You write persuasive, accurate product descriptions from feature lists.",
         "Stainless steel water bottle, 750ml, vacuum insulated, keeps cold 24h, BPA-free",
         ["ecommerce", "together", "marketing", "ai"], "content"),
    _llm("faq-generator", "FAQ Generator", "Generates a customer FAQ from a product or service description.",
         "groq", "You generate a 5-question FAQ with concise answers from the given description.",
         "Wayforth is an API marketplace where AI agents discover, pay for, and call 300+ APIs.",
         ["faq", "groq", "support", "ai"], "content"),
    _llm("keyword-extractor", "Keyword Extractor", "Extracts the key topics and entities from a block of text.",
         "mistral", "You extract the 8-12 most important keywords/entities. Return a comma-separated list.",
         "OpenAI and Anthropic are racing to build agentic models that can use tools autonomously.",
         ["keywords", "mistral", "nlp", "ai"], "ai"),
    _llm("answer-engine", "Answer Engine", "Answers a question with up-to-date reasoning via Perplexity Sonar.",
         "perplexity", "You are a precise answer engine. Answer the question factually and cite reasoning.",
         "What is the difference between EIP-3009 and EIP-2612?",
         ["qa", "perplexity", "research", "ai"], "research"),
    _llm("json-extractor", "JSON Extractor", "Extracts structured JSON fields from messy text via Mistral.",
         "mistral", "You extract structured data. Return ONLY valid JSON with the fields you can find.",
         "Invoice #4471 from Acme Corp, due 2026-07-01, total $1,240.00, contact ar@acme.com",
         ["extraction", "mistral", "json", "ai"], "ai"),
    _llm("study-flashcards", "Study Flashcards", "Turns study material into Q&A flashcards via Groq.",
         "groq", "You create study flashcards. Return 5 question/answer pairs from the material.",
         "Photosynthesis converts light, water, and CO2 into glucose and oxygen in the chloroplast.",
         ["education", "groq", "study", "ai"], "ai"),
    _llm("outline-generator", "Outline Generator", "Produces a structured content outline from a topic via Together.",
         "together", "You produce clear hierarchical outlines (sections + sub-points) for the given topic.",
         "A beginner's guide to paying for APIs with stablecoins",
         ["outline", "together", "writing", "ai"], "content"),
]


# ── Search templates ───────────────────────────────────────────────────────────

def _search(id, name, tagline, slug, default_query, tags, category="research"):
    bodies = {
        "serper": ('{"query": QUERY, "num": NUM}', "resp_data.get(\"organic\", [])",
                   "{ query: QUERY, num: NUM }", "respData.organic"),
        "tavily": ('{"query": QUERY, "max_results": NUM}', "resp_data.get(\"results\", [])",
                   "{ query: QUERY, max_results: NUM }", "respData.results"),
        "brave":  ('{"query": QUERY, "count": NUM}', "resp_data.get(\"results\", [])",
                   "{ query: QUERY, count: NUM }", "respData.results"),
    }
    body, py_extract, ts_body, ts_extract = bodies[slug]
    return {
        "id": id, "name": name, "tagline": tagline, "category": category, "shape": "search",
        "slug": slug, "default_query": default_query, "body": body, "py_extract": py_extract,
        "ts_body": ts_body, "ts_extract": ts_extract, "services_used": [slug],
        "credits": _cred(slug), "breakdown": _bd(slug),
        "env_vars": [
            _env("QUERY", "Search query", default_query),
            _env("NUM_RESULTS", "Number of results (1-10)", "5"),
        ],
        "tags": tags,
    }


_SEARCH_SPECS = [
    _search("web-search", "Web Search", "Searches the web via Serper and returns ranked results.",
            "serper", "best practices for AI agent observability", ["search", "serper", "web"]),
    _search("deep-research", "Deep Research", "Runs a deep research query via Tavily and returns sourced results.",
            "tavily", "state of autonomous AI agents 2025", ["research", "tavily", "web"]),
    _search("brave-search", "Privacy Search", "Searches the web via Brave and returns independent results.",
            "brave", "open source LLM inference benchmarks", ["search", "brave", "web", "privacy"]),
    _search("news-monitor", "News Monitor", "Tracks fresh news on a topic via Serper. Schedule it daily.",
            "serper", "AI regulation news this week", ["news", "serper", "monitoring", "scheduled"]),
    _search("competitor-finder", "Competitor Finder", "Finds competitors and alternatives for any product via Tavily.",
            "tavily", "alternatives to Zapier for AI workflows", ["competitive", "tavily", "research"]),
    _search("serp-tracker", "SERP Tracker", "Captures the current search-results page for a keyword via Serper.",
            "serper", "ai api marketplace", ["seo", "serper", "monitoring", "scheduled"]),
    _search("scholar-search", "Scholar Search", "Finds in-depth sources on a research topic via Tavily.",
            "tavily", "peer-reviewed work on agent-based payment systems", ["research", "tavily", "academic"]),
]


# ── Simple single-service templates ─────────────────────────────────────────────

_SIMPLE_SPECS = [
    {
        "id": "translator", "name": "Translator", "tagline": "Translates text into any language via DeepL.",
        "category": "translation", "shape": "simple", "slug": "deepl",
        "env_reads": [{"var": "TEXT", "env": "TEXT", "default": "Hello, world! Welcome to Wayforth."},
                      {"var": "TARGET", "env": "TARGET_LANG", "default": "ES"}],
        "body": '{"text": TEXT, "target_lang": TARGET}',
        "py_result": 'resp_data.get("translated_text", resp_data)',
        "ts_body": "{ text: TEXT, target_lang: TARGET }",
        "ts_result": "respData.translated_text ?? respData",
        "services_used": ["deepl"], "credits": _cred("deepl"), "breakdown": _bd("deepl"),
        "env_vars": [_env("TEXT", "Text to translate", "Hello, world! Welcome to Wayforth."),
                     _env("TARGET_LANG", "DeepL target language code (ES, FR, DE, JA...)", "ES")],
        "tags": ["translation", "deepl", "i18n"],
    },
    {
        "id": "weather-report", "name": "Weather Report", "tagline": "Fetches current weather for any city via OpenWeather.",
        "category": "data", "shape": "simple", "slug": "openweather",
        "env_reads": [{"var": "CITY", "env": "CITY", "default": "London"}],
        "body": '{"city": CITY}',
        "py_result": '{"city": resp_data.get("city"), "temp_c": resp_data.get("temp_c"), "condition": resp_data.get("condition")}',
        "ts_body": "{ city: CITY }",
        "ts_result": "{ city: respData.city, temp_c: respData.temp_c, condition: respData.condition }",
        "services_used": ["openweather"], "credits": _cred("openweather"), "breakdown": _bd("openweather"),
        "env_vars": [_env("CITY", "City name", "London")],
        "tags": ["weather", "openweather", "data"],
    },
    {
        "id": "stock-quote", "name": "Stock Quote", "tagline": "Fetches the latest quote for a ticker via Alpha Vantage.",
        "category": "data", "shape": "simple", "slug": "alphavantage",
        "env_reads": [{"var": "SYMBOL", "env": "SYMBOL", "default": "AAPL"}],
        "body": '{"symbol": SYMBOL}',
        "py_result": '{"symbol": resp_data.get("symbol"), "price": resp_data.get("price"), "change_pct": resp_data.get("change_pct")}',
        "ts_body": "{ symbol: SYMBOL }",
        "ts_result": "{ symbol: respData.symbol, price: respData.price, change_pct: respData.change_pct }",
        "services_used": ["alphavantage"], "credits": _cred("alphavantage"), "breakdown": _bd("alphavantage"),
        "env_vars": [_env("SYMBOL", "Stock ticker (AAPL, TSLA, MSFT...)", "AAPL")],
        "tags": ["finance", "alphavantage", "data", "stocks"],
    },
    {
        "id": "url-reader", "name": "URL Reader", "tagline": "Reads any web page and returns clean text via Jina.",
        "category": "data", "shape": "simple", "slug": "jina",
        "env_reads": [{"var": "URL", "env": "URL", "default": "https://wayforth.io"}],
        "body": '{"url": URL}',
        "py_result": '(resp_data.get("data", {}).get("content") if isinstance(resp_data.get("data"), dict) else None) or resp_data.get("content") or resp_data',
        "ts_body": "{ url: URL }",
        "ts_result": "respData?.data?.content ?? respData?.content ?? respData",
        "services_used": ["jina"], "credits": _cred("jina"), "breakdown": _bd("jina"),
        "env_vars": [_env("URL", "URL to read", "https://wayforth.io")],
        "tags": ["scrape", "jina", "reader", "web"],
    },
    {
        "id": "site-scraper", "name": "Site Scraper", "tagline": "Scrapes a URL to clean markdown via Firecrawl.",
        "category": "data", "shape": "simple", "slug": "firecrawl",
        "env_reads": [{"var": "URL", "env": "URL", "default": "https://wayforth.io"}],
        "body": '{"url": URL}',
        "py_result": '(resp_data.get("data", {}).get("markdown") if isinstance(resp_data.get("data"), dict) else None) or resp_data.get("markdown") or resp_data',
        "ts_body": "{ url: URL }",
        "ts_result": "respData?.data?.markdown ?? respData?.markdown ?? respData",
        "services_used": ["firecrawl"], "credits": _cred("firecrawl"), "breakdown": _bd("firecrawl"),
        "env_vars": [_env("URL", "URL to scrape", "https://wayforth.io")],
        "tags": ["scrape", "firecrawl", "markdown", "web"],
    },
    {
        "id": "image-creator", "name": "Image Creator", "tagline": "Generates an image from a text prompt via Stability.",
        "category": "media", "shape": "simple", "slug": "stability",
        "env_reads": [{"var": "PROMPT", "env": "PROMPT", "default": "a serene mountain lake at sunrise, photorealistic"}],
        "body": '{"prompt": PROMPT}',
        "py_result": '{"image_bytes": len(str(resp_data.get("image_base64", "")))}',
        "ts_body": "{ prompt: PROMPT }",
        "ts_result": "{ image_bytes: String(respData.image_base64 ?? '').length }",
        "services_used": ["stability"], "credits": _cred("stability"), "breakdown": _bd("stability"),
        "env_vars": [_env("PROMPT", "Image description", "a serene mountain lake at sunrise, photorealistic")],
        "tags": ["image", "stability", "media", "generation"],
    },
    {
        "id": "voiceover", "name": "Voiceover", "tagline": "Turns text into natural speech via ElevenLabs.",
        "category": "media", "shape": "simple", "slug": "elevenlabs",
        "env_reads": [{"var": "TEXT", "env": "TEXT", "default": "Welcome to Wayforth, the API marketplace for AI agents."},
                      {"var": "VOICE", "env": "VOICE_ID", "default": "21m00Tcm4TlvDq8ikWAM"}],
        "body": '{"text": TEXT, "voice_id": VOICE}',
        "py_result": '{"audio_bytes": len(str(resp_data.get("audio_base64", resp_data)))}',
        "ts_body": "{ text: TEXT, voice_id: VOICE }",
        "ts_result": "{ audio_bytes: String(respData.audio_base64 ?? '').length }",
        "services_used": ["elevenlabs"], "credits": _cred("elevenlabs"), "breakdown": _bd("elevenlabs"),
        "env_vars": [_env("TEXT", "Text to speak", "Welcome to Wayforth, the API marketplace for AI agents."),
                     _env("VOICE_ID", "ElevenLabs voice id", "21m00Tcm4TlvDq8ikWAM")],
        "tags": ["audio", "elevenlabs", "tts", "media"],
    },
    {
        "id": "transcriber", "name": "Audio Transcriber", "tagline": "Transcribes an audio file to text via AssemblyAI.",
        "category": "media", "shape": "simple", "slug": "assemblyai",
        "env_reads": [{"var": "AUDIO_URL", "env": "AUDIO_URL", "default": "https://example.com/audio.mp3"}],
        "body": '{"audio_url": AUDIO_URL}',
        "py_result": 'resp_data.get("text") or resp_data.get("transcript") or resp_data',
        "ts_body": "{ audio_url: AUDIO_URL }",
        "ts_result": "respData.text ?? respData.transcript ?? respData",
        "services_used": ["assemblyai"], "credits": _cred("assemblyai"), "breakdown": _bd("assemblyai"),
        "env_vars": [_env("AUDIO_URL", "Public URL of an MP3/WAV file", "https://example.com/audio.mp3")],
        "tags": ["audio", "assemblyai", "transcription", "media"],
    },
    {
        "id": "email-sender", "name": "Email Sender", "tagline": "Sends a transactional email via Resend.",
        "category": "communication", "shape": "simple", "slug": "resend",
        "env_reads": [{"var": "TO", "env": "TO_EMAIL", "default": "you@example.com"},
                      {"var": "SUBJECT", "env": "SUBJECT", "default": "Hello from Wayforth Cloud"},
                      {"var": "HTML", "env": "HTML_BODY", "default": "<p>Your agent is live.</p>"},
                      {"var": "FROM", "env": "FROM_EMAIL", "default": "noreply@wayforth.io"}],
        "body": '{"to": TO, "subject": SUBJECT, "html": HTML, "from": FROM}',
        "py_result": 'resp_data.get("email_id", resp_data)',
        "ts_body": "{ to: TO, subject: SUBJECT, html: HTML, from: FROM }",
        "ts_result": "respData.email_id ?? respData",
        "services_used": ["resend"], "credits": _cred("resend"), "breakdown": _bd("resend"),
        "env_vars": [_env("TO_EMAIL", "Recipient address", "you@example.com"),
                     _env("SUBJECT", "Email subject", "Hello from Wayforth Cloud"),
                     _env("HTML_BODY", "HTML email body", "<p>Your agent is live.</p>"),
                     _env("FROM_EMAIL", "Sender (Resend-verified domain)", "noreply@wayforth.io")],
        "tags": ["email", "resend", "notification", "communication"],
    },
    {
        "id": "gemini-assistant", "name": "Gemini Assistant", "tagline": "Answers a prompt via Google Gemini Flash.",
        "category": "ai", "shape": "simple", "slug": "gemini",
        "env_reads": [{"var": "PROMPT", "env": "PROMPT", "default": "Explain the x402 payment protocol in two sentences."},
                      {"var": "MODEL", "env": "MODEL", "default": "gemini-2.5-flash"}],
        "body": '{"prompt": PROMPT, "model": MODEL}',
        "py_result": 'resp_data.get("content") or resp_data.get("text") or resp_data',
        "ts_body": "{ prompt: PROMPT, model: MODEL }",
        "ts_result": "respData.content ?? respData.text ?? respData",
        "services_used": ["gemini"], "credits": _cred("gemini"), "breakdown": _bd("gemini"),
        "env_vars": [_env("PROMPT", "Prompt for Gemini", "Explain the x402 payment protocol in two sentences."),
                     _env("MODEL", "Gemini model id", "gemini-2.5-flash")],
        "tags": ["ai", "gemini", "google", "assistant"],
    },
]


# ── Chain templates (fetch → LLM) ───────────────────────────────────────────────

def _chain(id, name, tagline, fetch_slug, llm_slug, input_env, default_input,
           system, tags, category="research"):
    fetch = {
        "serper": ('{"query": TOPIC, "num": 5}',
                   '"\\n".join(f"{i+1}. " + (r.get("title") or "") + ": " + (r.get("snippet") or "") for i, r in enumerate(fr_data.get("organic", [])[:5]))',
                   "{ query: TOPIC, num: 5 }",
                   "(frData.organic ?? []).slice(0, 5).map((r: any, i: number) => `${i+1}. ${r.title ?? ''}: ${r.snippet ?? ''}`).join('\\n')"),
        "tavily": ('{"query": TOPIC, "max_results": 5}',
                   '"\\n".join(f"{i+1}. " + (r.get("title") or "") + ": " + (r.get("content") or "") for i, r in enumerate(fr_data.get("results", [])[:5]))',
                   "{ query: TOPIC, max_results: 5 }",
                   "(frData.results ?? []).slice(0, 5).map((r: any, i: number) => `${i+1}. ${r.title ?? ''}: ${r.content ?? ''}`).join('\\n')"),
        "jina":   ('{"url": TOPIC}',
                   'str((fr_data.get("data", {}) or {}).get("content") or fr_data.get("content") or "")[:3000]',
                   "{ url: TOPIC }",
                   "String(frData?.data?.content ?? frData?.content ?? '').slice(0, 3000)"),
        "firecrawl": ('{"url": TOPIC}',
                      'str((fr_data.get("data", {}) or {}).get("markdown") or fr_data.get("markdown") or "")[:3000]',
                      "{ url: TOPIC }",
                      "String(frData?.data?.markdown ?? frData?.markdown ?? '').slice(0, 3000)"),
        "assemblyai": ('{"audio_url": TOPIC}',
                       'str(fr_data.get("text") or fr_data.get("transcript") or "")[:3000]',
                       "{ audio_url: TOPIC }",
                       "String(frData.text ?? frData.transcript ?? '').slice(0, 3000)"),
    }
    fetch_body, py_context, ts_fetch_body, ts_context = fetch[fetch_slug]
    return {
        "id": id, "name": name, "tagline": tagline, "category": category, "shape": "chain",
        "fetch_slug": fetch_slug, "llm_slug": llm_slug, "input_env": input_env,
        "default_input": default_input, "model": _MODEL[llm_slug], "system": system,
        "fetch_body": fetch_body, "py_context": py_context,
        "ts_fetch_body": ts_fetch_body, "ts_context": ts_context,
        "services_used": [fetch_slug, llm_slug], "credits": _cred(fetch_slug, llm_slug),
        "breakdown": _bd(fetch_slug, llm_slug),
        "env_vars": [_env(input_env, "Primary input (topic or URL)", default_input),
                     _env("MODEL", f"{llm_slug} model id", _MODEL[llm_slug])],
        "tags": tags,
    }


_CHAIN_SPECS = [
    _chain("research-summarizer", "Research Summarizer",
           "Searches the web via Serper, then summarizes the findings via Groq.",
           "serper", "groq", "TOPIC", "impact of AI agents on customer support",
           "You summarize web results into a cited 3-5 bullet briefing.",
           ["research", "serper", "groq", "briefing", "scheduled"]),
    _chain("page-summarizer", "Page Summarizer",
           "Reads a web page via Jina, then summarizes it via Groq.",
           "jina", "groq", "URL", "https://wayforth.io",
           "You summarize the page content into 3-5 key points.",
           ["scrape", "jina", "groq", "summary"], "content"),
    _chain("site-digest", "Site Digest",
           "Scrapes a URL via Firecrawl, then writes a digest via Groq.",
           "firecrawl", "groq", "URL", "https://wayforth.io",
           "You write a short digest of the scraped page content.",
           ["scrape", "firecrawl", "groq", "digest"], "content"),
    _chain("transcript-summarizer", "Transcript Summarizer",
           "Transcribes audio via AssemblyAI, then summarizes it via Groq.",
           "assemblyai", "groq", "AUDIO_URL", "https://example.com/audio.mp3",
           "You summarize the transcript into notes and action items.",
           ["audio", "assemblyai", "groq", "summary"], "media"),
    _chain("competitive-brief", "Competitive Brief",
           "Deep-researches a market via Tavily, then drafts a brief via Groq.",
           "tavily", "groq", "TOPIC", "AI API marketplaces and agent payment rails",
           "You write a competitive brief: players, positioning, gaps.",
           ["competitive", "tavily", "groq", "brief"]),
]


# ── Payment-native templates (read /payments/rails, use x402 when live) ─────────

def _payment(id, name, tagline, slug, input_env, default_input, params, ts_params, tags, category="payments"):
    return {
        "id": id, "name": name, "tagline": tagline, "category": category, "shape": "payment",
        "slug": slug, "input_env": input_env, "default_input": default_input,
        "params": params, "ts_params": ts_params, "services_used": [slug],
        "credits": _cred(slug), "breakdown": _bd(slug) + " (or USDC via x402 when live)",
        "env_vars": [_env(input_env, "Primary input", default_input)],
        "tags": tags,
    }


_PAYMENT_SPECS = [
    _payment("x402-search", "x402 Pay-Per-Call Search",
             "Searches the web and pays per call in USDC via x402 when the rail is live, else uses the managed key rail.",
             "serper", "QUERY", "agent-native payment protocols",
             '{"query": QUERY, "num": 5}', "{ query: QUERY, num: 5 }",
             ["x402", "usdc", "serper", "payments", "agent-native"]),
    _payment("x402-translate", "x402 Pay-Per-Call Translate",
             "Translates text and settles in USDC via x402 when live, else managed.",
             "deepl", "TEXT", "The future of money is programmable.",
             '{"text": QUERY, "target_lang": "ES"}', "{ text: QUERY, target_lang: 'ES' }",
             ["x402", "usdc", "deepl", "payments", "agent-native"]),
    _payment("x402-summarize", "x402 Pay-Per-Call Summarize",
             "Summarizes text and pays per call in USDC via x402 when live, else managed.",
             "groq", "PROMPT", "Summarize the case for stablecoin-native API payments.",
             '{"messages": [{"role": "user", "content": QUERY}], "model": "llama-3.3-70b-versatile"}',
             "{ messages: [{ role: 'user', content: QUERY }], model: 'llama-3.3-70b-versatile' }",
             ["x402", "usdc", "groq", "payments", "agent-native"]),
    _payment("x402-scrape", "x402 Pay-Per-Call Scrape",
             "Scrapes a URL and settles in USDC via x402 when live, else managed.",
             "firecrawl", "URL", "https://wayforth.io",
             '{"url": QUERY}', "{ url: QUERY }",
             ["x402", "usdc", "firecrawl", "payments", "agent-native"]),
    _payment("x402-image", "x402 Pay-Per-Call Image",
             "Generates an image and settles in USDC via x402 when live, else managed.",
             "stability", "PROMPT", "a neon city skyline at dusk, cinematic",
             '{"prompt": QUERY}', "{ prompt: QUERY }",
             ["x402", "usdc", "stability", "payments", "agent-native"]),
]


SPECS = _LLM_SPECS + _SEARCH_SPECS + _SIMPLE_SPECS + _CHAIN_SPECS + _PAYMENT_SPECS
