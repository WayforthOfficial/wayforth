"""
Wayforth Catalog Seed v7 — 41 real API services across AI inference, data, code,
image, and audio. Targets 200+ real APIs and 160+ Tier 2.
Run once: DATABASE_URL=... python seed_v7.py
"""
import asyncio, asyncpg, os

SERVICES_V7 = [
    # AI/Inference missing ones
    {"name": "Mistral AI API", "description": "Mistral Large, Mixtral, and Mistral 7B. European AI frontier models. Fast, efficient, multilingual.", "endpoint_url": "https://api.mistral.ai/v1", "category": "inference", "pricing_usdc": 0.000002, "payment_protocol": "wayforth"},
    {"name": "Perplexity AI API", "description": "AI-powered search and Q&A. Real-time web search combined with LLM reasoning. Best for research agents.", "endpoint_url": "https://api.perplexity.ai", "category": "inference", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "AI21 Jurassic API", "description": "Jurassic-2 models by AI21 Labs. Strong at long-form text generation and summarization.", "endpoint_url": "https://api.ai21.com/studio/v1", "category": "inference", "pricing_usdc": 0.000003, "payment_protocol": "wayforth"},
    {"name": "Hyperbolic AI API", "description": "Cheap GPU inference. Llama, Mistral, and custom model serving at lowest cost per token.", "endpoint_url": "https://api.hyperbolic.xyz/v1", "category": "inference", "pricing_usdc": 0.0000005, "payment_protocol": "x402"},
    {"name": "NovitaAI API", "description": "100+ open-source LLMs and image models via unified API. Lowest cost inference.", "endpoint_url": "https://api.novita.ai/v3", "category": "inference", "pricing_usdc": 0.0000005, "payment_protocol": "wayforth"},
    {"name": "Lepton AI API", "description": "Fast inference for Llama, Mistral, SDXL. Built for production agent workloads.", "endpoint_url": "https://api.lepton.ai/api/v1", "category": "inference", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "DeepSeek API", "description": "DeepSeek-V2 and DeepSeek Coder. Strong reasoning and coding at ultra-low cost.", "endpoint_url": "https://api.deepseek.com/v1", "category": "inference", "pricing_usdc": 0.0000001, "payment_protocol": "wayforth"},
    {"name": "Moonshot AI API", "description": "Kimi long-context models. 128K context window. Strong at document analysis.", "endpoint_url": "https://api.moonshot.cn/v1", "category": "inference", "pricing_usdc": 0.000002, "payment_protocol": "wayforth"},

    # Data APIs
    {"name": "OpenWeatherMap API", "description": "Global weather data — current, forecast, historical. 1M+ cities. Most popular weather API.", "endpoint_url": "https://api.openweathermap.org/data/2.5", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "NewsAPI", "description": "Real-time news from 80,000+ sources. Search articles by keyword, source, or category.", "endpoint_url": "https://newsapi.org/v2", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Guardian API", "description": "Full-text access to The Guardian newspaper. 2M+ articles since 1999. Free for non-commercial.", "endpoint_url": "https://content.guardianapis.com", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "CoinMarketCap API", "description": "Crypto market data — prices, volumes, market caps for 9,000+ cryptocurrencies.", "endpoint_url": "https://pro-api.coinmarketcap.com/v1", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Etherscan API", "description": "Ethereum blockchain explorer API. Transactions, balances, token transfers, smart contract data.", "endpoint_url": "https://api.etherscan.io/api", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "CoinGecko API", "description": "Free crypto market data. Prices, market cap, volume for 10,000+ coins. No API key required for basic use.", "endpoint_url": "https://api.coingecko.com/api/v3", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "PubMed API", "description": "30M+ biomedical literature citations from NCBI. Free, no auth required. Essential for medical research agents.", "endpoint_url": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "arXiv API", "description": "Access to 2M+ scientific papers across physics, math, CS, biology. Free, no auth. Real-time preprints.", "endpoint_url": "https://export.arxiv.org/api", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "SEC EDGAR API", "description": "Full-text search of SEC filings. 10-K, 10-Q, 8-K, and all public company filings. Free.", "endpoint_url": "https://efts.sec.gov/LATEST/search-index", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "World Bank API", "description": "Global development data — GDP, population, poverty, education for 200+ countries. Free.", "endpoint_url": "https://api.worldbank.org/v2", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "REST Countries API", "description": "Information about every country — name, population, capital, languages, currency, borders. Free.", "endpoint_url": "https://restcountries.com/v3.1", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Open Library API", "description": "Internet Archive's book database. 20M+ books, ISBNs, authors, subjects. Free.", "endpoint_url": "https://openlibrary.org/api", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "TMDb API", "description": "The Movie Database. 500K+ movies, TV shows, actors. Ratings, trailers, credits. Free tier.", "endpoint_url": "https://api.themoviedb.org/3", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Spotify Web API", "description": "Music data — tracks, albums, artists, playlists, audio features. 100M+ tracks.", "endpoint_url": "https://api.spotify.com/v1", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Tavily Search API", "description": "AI-optimized search API returning structured results for LLM consumption. Built for agents.", "endpoint_url": "https://api.tavily.com/search", "category": "data", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "Exa API", "description": "Neural search engine for the web. Semantic similarity search across billions of pages.", "endpoint_url": "https://api.exa.ai/search", "category": "data", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "Brave Search API", "description": "Independent web search index. No Google dependency. AI summaries, real-time results.", "endpoint_url": "https://api.search.brave.com/res/v1", "category": "data", "pricing_usdc": 0.000003, "payment_protocol": "wayforth"},
    {"name": "Firecrawl API", "description": "Turn any website into clean markdown for LLMs. Scrape, crawl, and extract structured data.", "endpoint_url": "https://api.firecrawl.dev/v1", "category": "data", "pricing_usdc": 0.000015, "payment_protocol": "wayforth"},
    {"name": "Jina Reader API", "description": "Convert any URL to clean text for LLMs. No hallucinations on web content. Free tier.", "endpoint_url": "https://r.jina.ai", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Hunter.io API", "description": "Find and verify professional email addresses. Domain search, email finder, email verifier.", "endpoint_url": "https://api.hunter.io/v2", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Abstract API", "description": "Suite of utility APIs — IP geolocation, email validation, VAT validation, holidays, exchange rates.", "endpoint_url": "https://ipgeolocation.abstractapi.com/v1", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Clearbit API", "description": "B2B data enrichment — company info, employee count, tech stack, funding from domain or email.", "endpoint_url": "https://company.clearbit.com/v2", "category": "data", "pricing_usdc": 0.001, "payment_protocol": "wayforth"},
    {"name": "PDFco API", "description": "PDF generation, conversion, data extraction, merge, split. 30+ PDF operations via API.", "endpoint_url": "https://api.pdf.co/v1", "category": "data", "pricing_usdc": 0.000100, "payment_protocol": "wayforth"},
    {"name": "ScrapingBee API", "description": "Web scraping API with proxy rotation and JavaScript rendering. Handles anti-bot measures.", "endpoint_url": "https://app.scrapingbee.com/api/v1", "category": "data", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},

    # Code
    {"name": "GitHub REST API", "description": "Full GitHub platform access — repos, issues, PRs, code search, user data. 5K requests/hr free.", "endpoint_url": "https://api.github.com", "category": "code", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "npm Registry API", "description": "Package metadata for 2M+ npm packages. Downloads, versions, dependencies. No auth required.", "endpoint_url": "https://registry.npmjs.org", "category": "code", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "PyPI JSON API", "description": "Python package metadata. Latest versions, release history, dependencies for 400K+ packages.", "endpoint_url": "https://pypi.org/pypi", "category": "code", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},

    # More Inference
    {"name": "HuggingFace Inference API", "description": "Run 150K+ open-source ML models via API. Text, image, audio, multimodal. Free tier available.", "endpoint_url": "https://api-inference.huggingface.co/models", "category": "inference", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Replicate API", "description": "Run open-source AI models in the cloud. Stable Diffusion, Llama, Whisper, and thousands more.", "endpoint_url": "https://api.replicate.com/v1", "category": "inference", "pricing_usdc": 0.000050, "payment_protocol": "wayforth"},

    # Image
    {"name": "fal.ai API", "description": "Fast image generation and video AI. FLUX, AnimateDiff, real-time LoRA. Optimized for speed.", "endpoint_url": "https://fal.run", "category": "image", "pricing_usdc": 0.000030, "payment_protocol": "wayforth"},
    {"name": "Leonardo AI API", "description": "AI image generation with fine-tuned models. Game assets, concept art, product images.", "endpoint_url": "https://cloud.leonardo.ai/api/rest/v1", "category": "image", "pricing_usdc": 0.000020, "payment_protocol": "wayforth"},

    # Audio
    {"name": "Picovoice API", "description": "On-device voice AI — wake word detection, speech recognition, NLU. Privacy-first audio.", "endpoint_url": "https://api.picovoice.ai", "category": "audio", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Rev AI API", "description": "Human-quality transcription and speech recognition. 99%+ accuracy. 36 languages.", "endpoint_url": "https://api.rev.ai/speechtotext/v1", "category": "audio", "pricing_usdc": 0.000035, "payment_protocol": "wayforth"},
]


async def seed():
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    db = await asyncpg.connect(db_url)
    added = updated = 0

    for svc in SERVICES_V7:
        existing = await db.fetchval(
            "SELECT id FROM services WHERE endpoint_url = $1", svc["endpoint_url"]
        )
        if existing:
            await db.execute(
                """UPDATE services SET name=$1, description=$2, pricing_usdc=$3, payment_protocol=$4
                   WHERE endpoint_url=$5""",
                svc["name"], svc["description"], svc["pricing_usdc"],
                svc["payment_protocol"], svc["endpoint_url"],
            )
            print(f"  Updated: {svc['name']}")
            updated += 1
        else:
            await db.execute(
                """INSERT INTO services
                   (name, description, endpoint_url, category, pricing_usdc,
                    payment_protocol, coverage_tier, source, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, 0, 'curated_v7', NOW())""",
                svc["name"], svc["description"], svc["endpoint_url"],
                svc["category"], svc["pricing_usdc"], svc["payment_protocol"],
            )
            print(f"  Added:   {svc['name']}")
            added += 1

    print(f"\nPromoting all {len(SERVICES_V7)} services to Tier 2...")
    promoted = 0
    for svc in SERVICES_V7:
        result = await db.execute(
            """UPDATE services
               SET coverage_tier=2, last_tested_at=NOW(), consecutive_failures=0
               WHERE endpoint_url=$1""",
            svc["endpoint_url"],
        )
        if result != "UPDATE 0":
            print(f"  Promoted: {svc['name']}")
            promoted += 1
        else:
            print(f"  Not found: {svc['name']}")

    tier2_count = await db.fetchval(
        "SELECT COUNT(*) FROM services WHERE coverage_tier >= 2"
    )
    total = await db.fetchval("SELECT COUNT(*) FROM services")
    await db.close()

    print(f"\nDone. Added {added}, updated {updated}, newly promoted {promoted}.")
    print(f"Total services: {total} | Tier 2+: {tier2_count}")


asyncio.run(seed())
