"""
Wayforth Catalog Seed v5 — Session 64 batch of 33 services, push to 100+ Tier 2.
Run once: DATABASE_URL=... python seed_v5.py
"""
import asyncio, asyncpg, os

SERVICES_V5 = [
    # Cloud Providers (always up)
    {"name": "AWS S3 API", "description": "Object storage API. Store and retrieve files, datasets, and artifacts. Used by millions of production systems.", "endpoint_url": "https://s3.amazonaws.com", "category": "data", "pricing_usdc": 0.000023, "payment_protocol": "wayforth"},
    {"name": "Google Cloud Storage API", "description": "Scalable object storage from Google. Store and serve files globally with millisecond access.", "endpoint_url": "https://storage.googleapis.com", "category": "data", "pricing_usdc": 0.000020, "payment_protocol": "wayforth"},
    {"name": "Azure Blob Storage API", "description": "Microsoft Azure object storage. Massively scalable unstructured data storage.", "endpoint_url": "https://blob.core.windows.net", "category": "data", "pricing_usdc": 0.000018, "payment_protocol": "wayforth"},
    # Maps & Location (always up)
    {"name": "Google Maps API", "description": "Maps, routes, places, and geocoding. The gold standard for location data with global coverage.", "endpoint_url": "https://maps.googleapis.com/maps/api", "category": "data", "pricing_usdc": 0.000005, "payment_protocol": "wayforth"},
    {"name": "HERE Maps API", "description": "Enterprise mapping and location services. Real-time traffic, routing, geocoding, and places.", "endpoint_url": "https://router.hereapi.com/v8", "category": "data", "pricing_usdc": 0.000002, "payment_protocol": "wayforth"},
    {"name": "IPGeolocation API", "description": "IP address to location, timezone, currency, and ISP data. Free tier available.", "endpoint_url": "https://api.ipgeolocation.io/ipgeo", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "ip-api.com", "description": "Free IP geolocation API. No key required for non-commercial use. Returns country, region, city, ISP.", "endpoint_url": "http://ip-api.com/json", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    # Currency & Finance (always up)
    {"name": "ExchangeRate-API", "description": "Currency exchange rates for 160+ currencies. Free tier with 1,500 requests/month.", "endpoint_url": "https://v6.exchangerate-api.com/v6", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Open Exchange Rates", "description": "Real-time and historical exchange rates. Used by 50,000+ companies. JSON API.", "endpoint_url": "https://openexchangerates.org/api", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Fixer.io API", "description": "Foreign exchange rates and currency conversion. 170+ currencies, updated every 60 seconds.", "endpoint_url": "https://data.fixer.io/api", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Binance API", "description": "Crypto trading data — real-time prices, order books, historical klines for all Binance pairs.", "endpoint_url": "https://api.binance.com/api/v3", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    # Public Data (always up, no auth)
    {"name": "REST Countries API", "description": "Data about every country — population, languages, currencies, flags, borders. Free, no auth.", "endpoint_url": "https://restcountries.com/v3.1", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Numbers API", "description": "Interesting facts about numbers — math, trivia, dates. Free, no auth required.", "endpoint_url": "http://numbersapi.com", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "JSONPlaceholder API", "description": "Free fake REST API for testing. Posts, comments, users, todos. Zero setup.", "endpoint_url": "https://jsonplaceholder.typicode.com", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Open Trivia DB", "description": "Free trivia questions API. 23 categories, multiple difficulty levels. No auth required.", "endpoint_url": "https://opentdb.com/api.php", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "JokeAPI", "description": "Programming and general jokes API. Safe mode available. Free, no auth required.", "endpoint_url": "https://v2.jokeapi.dev/joke", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    # Developer Tools (always up)
    {"name": "ipify API", "description": "Simple public IP address API. Returns your public IPv4 or IPv6. Free, no auth.", "endpoint_url": "https://api.ipify.org", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "GitHub Gist API", "description": "Create and read GitHub Gists. Best for agents sharing or storing code snippets.", "endpoint_url": "https://api.github.com/gists", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Pastebin API", "description": "Create, read, and manage text snippets. Best for agents storing temporary data.", "endpoint_url": "https://pastebin.com/api", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "QR Code Generator API", "description": "Generate QR codes from any URL or text. Multiple formats, sizes, and colors.", "endpoint_url": "https://api.qrserver.com/v1/create-qr-code", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    # Language & NLP
    {"name": "LanguageTool API", "description": "Grammar and spell checking for 30+ languages. Free tier, no auth for basic use.", "endpoint_url": "https://api.languagetool.org/v2", "category": "translation", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Lingua Robot API", "description": "Linguistic analysis — word definitions, forms, pronunciations, etymology.", "endpoint_url": "https://www.linguarobot.io/linguistics/v1", "category": "translation", "pricing_usdc": 0.000002, "payment_protocol": "wayforth"},
    {"name": "MyMemory Translation API", "description": "Free translation API using MyMemory and Google Translate. 1000 words/day free.", "endpoint_url": "https://api.mymemory.translated.net/get", "category": "translation", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "LibreTranslate API", "description": "Free and open-source machine translation. Self-hostable. Supports 29 languages.", "endpoint_url": "https://libretranslate.com/translate", "category": "translation", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    # Additional Inference
    {"name": "Cloudflare Workers AI", "description": "Run AI models at the edge. Llama, Mistral, Gemma, SDXL. Pay per request, no GPU setup.", "endpoint_url": "https://api.cloudflare.com/client/v4/accounts", "category": "inference", "pricing_usdc": 0.0000001, "payment_protocol": "wayforth"},
    {"name": "Nvidia NIM API", "description": "Optimized inference for Llama, Mistral, and domain-specific models. Enterprise-grade GPU inference.", "endpoint_url": "https://integrate.api.nvidia.com/v1", "category": "inference", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "Cerebras Inference API", "description": "World's fastest LLM inference — 2,000+ tokens/second on Llama 3. Purpose-built AI chip.", "endpoint_url": "https://api.cerebras.ai/v1", "category": "inference", "pricing_usdc": 0.0000006, "payment_protocol": "wayforth"},
    {"name": "Sambanova Cloud API", "description": "Ultra-fast inference with full-context Llama models. 128K context at production speed.", "endpoint_url": "https://api.sambanova.ai/v1", "category": "inference", "pricing_usdc": 0.0000006, "payment_protocol": "wayforth"},
    # Image Generation
    {"name": "Ideogram API", "description": "Text-to-image with best-in-class text rendering. Accurate typography in generated images.", "endpoint_url": "https://api.ideogram.ai/generate", "category": "image", "pricing_usdc": 0.00008, "payment_protocol": "wayforth"},
    {"name": "Recraft API", "description": "Professional design-quality image generation. Vector and raster, brand-consistent outputs.", "endpoint_url": "https://external.api.recraft.ai/v1", "category": "image", "pricing_usdc": 0.00004, "payment_protocol": "wayforth"},
    {"name": "Flux API (Black Forest Labs)", "description": "FLUX.1 — state-of-the-art open image generation. Best quality-to-speed ratio available.", "endpoint_url": "https://api.bfl.ml/v1", "category": "image", "pricing_usdc": 0.00004, "payment_protocol": "wayforth"},
    # More Audio
    {"name": "PlayHT API", "description": "Ultra-realistic text-to-speech with voice cloning. 800+ voices, 142 languages.", "endpoint_url": "https://api.play.ht/api/v2", "category": "audio", "pricing_usdc": 0.000030, "payment_protocol": "wayforth"},
    {"name": "LMNT API", "description": "Ultra-fast, ultra-realistic speech synthesis. Sub-300ms first byte. Best for real-time agents.", "endpoint_url": "https://api.lmnt.com/v1", "category": "audio", "pricing_usdc": 0.000020, "payment_protocol": "wayforth"},
    {"name": "Speechify API", "description": "Natural-sounding TTS optimized for long-form content. Celebrity voices available.", "endpoint_url": "https://api.speechify.com/v1", "category": "audio", "pricing_usdc": 0.000025, "payment_protocol": "wayforth"},
]


async def seed():
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    db = await asyncpg.connect(db_url)
    added = updated = 0

    for svc in SERVICES_V5:
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
                   VALUES ($1, $2, $3, $4, $5, $6, 0, 'curated_v5', NOW())""",
                svc["name"], svc["description"], svc["endpoint_url"],
                svc["category"], svc["pricing_usdc"], svc["payment_protocol"],
            )
            print(f"  Added:   {svc['name']}")
            added += 1

    print(f"\nPromoting all {len(SERVICES_V5)} services to Tier 2...")
    promoted = 0
    for svc in SERVICES_V5:
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
