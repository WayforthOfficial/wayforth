"""
Wayforth Catalog Seed v3 — Session 61 batch of 30 curated agent-callable services.
Run once: python seed_v3.py
"""
import asyncio, asyncpg, os

SERVICES = [
    # Search — AI-optimized
    {
        "name": "Brave Search API",
        "description": "Privacy-first web search built for AI agents. Returns clean JSON with web, news, and image results. No tracking, no ads in results.",
        "endpoint_url": "https://api.search.brave.com/res/v1/web/search",
        "category": "data",
        "pricing_usdc": 0.000003,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Tavily AI Search",
        "description": "Search API purpose-built for AI agents and RAG pipelines. Returns structured, relevant results with source URLs. Optimized for LLM consumption.",
        "endpoint_url": "https://api.tavily.com/search",
        "category": "data",
        "pricing_usdc": 0.000004,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Exa Neural Search",
        "description": "Semantic web search using neural embeddings instead of keyword matching. Returns conceptually relevant results for agent research tasks.",
        "endpoint_url": "https://api.exa.ai/search",
        "category": "data",
        "pricing_usdc": 0.000001,
        "payment_protocol": "wayforth",
    },
    {
        "name": "SerpAPI",
        "description": "Scrape Google, Bing, Yahoo, and 20+ search engines via a single API. Returns structured SERP data. Used by 30,000+ developers.",
        "endpoint_url": "https://serpapi.com/search",
        "category": "data",
        "pricing_usdc": 0.000005,
        "payment_protocol": "wayforth",
    },
    # Finance / Crypto
    {
        "name": "CoinGecko API",
        "description": "Largest crypto data aggregator. Prices, market cap, volume for 10,000+ coins. Free tier available. Standard for DeFi and crypto agents.",
        "endpoint_url": "https://api.coingecko.com/api/v3/ping",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Etherscan API",
        "description": "Ethereum blockchain explorer API. Query balances, transactions, token transfers, contract ABIs, and gas prices on mainnet.",
        "endpoint_url": "https://api.etherscan.io/api",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "CoinMarketCap API",
        "description": "Institutional-grade crypto market data. Real-time prices, historical OHLCV, market cap rankings, and exchange data for 9,000+ assets.",
        "endpoint_url": "https://pro-api.coinmarketcap.com/v1/cryptocurrency/listings/latest",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    # Weather / Geo
    {
        "name": "Open-Meteo Weather",
        "description": "Free, open-source weather forecast API. No API key required. 80+ weather variables, hourly and daily forecasts for any latitude/longitude.",
        "endpoint_url": "https://api.open-meteo.com/v1/forecast",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "OpenCage Geocoding",
        "description": "Forward and reverse geocoding using OpenStreetMap and other open data. Returns structured address components, timezone, and country info.",
        "endpoint_url": "https://api.opencagedata.com/geocode/v1/json",
        "category": "data",
        "pricing_usdc": 0.000001,
        "payment_protocol": "wayforth",
    },
    # Document / Web intelligence
    {
        "name": "Diffbot Article API",
        "description": "Automatic web article extraction. Returns clean text, author, publish date, and images from any URL. Powers structured web intelligence pipelines.",
        "endpoint_url": "https://api.diffbot.com/v3/article",
        "category": "data",
        "pricing_usdc": 0.000002,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Mathpix OCR API",
        "description": "State-of-the-art OCR for math equations, scientific notation, tables, and text in images and PDFs. Outputs LaTeX, MathML, or plain text.",
        "endpoint_url": "https://api.mathpix.com/v3/text",
        "category": "data",
        "pricing_usdc": 0.000004,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Unstructured Document API",
        "description": "Parse PDFs, Word docs, HTML, and images into clean text chunks for RAG ingestion. Handles complex layouts and tables automatically.",
        "endpoint_url": "https://api.unstructured.io/general/v0/general",
        "category": "data",
        "pricing_usdc": 0.0000075,
        "payment_protocol": "wayforth",
    },
    # Business data
    {
        "name": "Hunter.io Email Finder",
        "description": "Find and verify professional email addresses by company domain. 90%+ accuracy. Best for sales agents and outreach automation.",
        "endpoint_url": "https://api.hunter.io/v2/domain-search",
        "category": "data",
        "pricing_usdc": 0.000005,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Clearbit Enrichment",
        "description": "Enrich any email or domain with company and person data — industry, headcount, funding, social profiles. Best for B2B agent workflows.",
        "endpoint_url": "https://person.clearbit.com/v2/people/find",
        "category": "data",
        "pricing_usdc": 0.00001,
        "payment_protocol": "wayforth",
    },
    # Inference — new providers
    {
        "name": "OpenAI API",
        "description": "GPT-4o, GPT-4 Turbo, GPT-3.5. Function calling, vision, structured outputs. The most widely deployed LLM API in production agent systems.",
        "endpoint_url": "https://api.openai.com/v1/chat/completions",
        "category": "inference",
        "pricing_usdc": 0.000005,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Mistral AI API",
        "description": "Mistral Large, Small, and Nemo. Function calling and JSON mode. Fast European-hosted LLM with strong coding and reasoning benchmarks.",
        "endpoint_url": "https://api.mistral.ai/v1/chat/completions",
        "category": "inference",
        "pricing_usdc": 0.000002,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Google Gemini API",
        "description": "Gemini 1.5 Pro and Flash — 1M token context, multimodal (text, image, audio, video). Best for long-document agent workflows.",
        "endpoint_url": "https://generativelanguage.googleapis.com/v1beta/models",
        "category": "inference",
        "pricing_usdc": 0.0000035,
        "payment_protocol": "wayforth",
    },
    {
        "name": "AI21 Jurassic API",
        "description": "Jurassic-2 Ultra and Mid for complex reasoning and generation. Specialized models for summarization, paraphrase, and text segmentation.",
        "endpoint_url": "https://api.ai21.com/studio/v1/j2-ultra/complete",
        "category": "inference",
        "pricing_usdc": 0.000015,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Cloudflare Workers AI",
        "description": "Run LLMs, image models, and embeddings at the edge. 100+ supported models including Llama 3.1 and Mistral. Sub-50ms latency globally.",
        "endpoint_url": "https://api.cloudflare.com/client/v4/accounts",
        "category": "inference",
        "pricing_usdc": 0.0000001,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Cohere Generate API",
        "description": "Command R+ for enterprise RAG. Grounded generation with citations, tool use, and 128k context. Optimized for retrieval-augmented workflows.",
        "endpoint_url": "https://api.cohere.ai/v1/generate",
        "category": "inference",
        "pricing_usdc": 0.000003,
        "payment_protocol": "wayforth",
    },
    # Embeddings
    {
        "name": "Voyage AI Embeddings",
        "description": "Best-in-class embeddings for retrieval. voyage-3-large outperforms OpenAI ada-002 on MTEB. Specialized models for code and finance.",
        "endpoint_url": "https://api.voyageai.com/v1/embeddings",
        "category": "embeddings",
        "pricing_usdc": 0.00000012,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Pinecone Vector DB",
        "description": "Managed vector database for semantic search at scale. Serverless tier available. Used in 80%+ of production RAG deployments.",
        "endpoint_url": "https://api.pinecone.io/indexes",
        "category": "embeddings",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    # Audio
    {
        "name": "Deepgram STT",
        "description": "Real-time and batch speech-to-text. Nova-2 model outperforms Whisper on accuracy and speed. Speaker diarization, punctuation, entity detection.",
        "endpoint_url": "https://api.deepgram.com/v1/listen",
        "category": "audio",
        "pricing_usdc": 0.0000036,
        "payment_protocol": "wayforth",
    },
    {
        "name": "OpenAI Whisper API",
        "description": "OpenAI's hosted Whisper large-v2 transcription. Supports 57 languages. Returns JSON with segments and timestamps. 25MB file limit.",
        "endpoint_url": "https://api.openai.com/v1/audio/transcriptions",
        "category": "audio",
        "pricing_usdc": 0.000006,
        "payment_protocol": "wayforth",
    },
    {
        "name": "PlayHT Text-to-Speech",
        "description": "Ultra-realistic TTS with 900+ voices across 142 languages. Real-time streaming supported. Voice cloning in under 30 seconds.",
        "endpoint_url": "https://api.play.ht/api/v2/tts",
        "category": "audio",
        "pricing_usdc": 0.000025,
        "payment_protocol": "wayforth",
    },
    # Image generation
    {
        "name": "Ideogram Text-to-Image",
        "description": "Best-in-class text rendering in AI images. Ideogram 2.0 reliably generates accurate typography inside visuals. Excellent for poster and UI generation.",
        "endpoint_url": "https://api.ideogram.ai/generate",
        "category": "image",
        "pricing_usdc": 0.00008,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Leonardo AI",
        "description": "High-quality image and video generation. Fine-tuned models for game assets, product photography, and concept art. Phoenix and Alchemy models.",
        "endpoint_url": "https://cloud.leonardo.ai/api/rest/v1/generations",
        "category": "image",
        "pricing_usdc": 0.000017,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Segmind Image API",
        "description": "Stable Diffusion XL, FLUX, and 50+ models via unified API. Fastest inference for image generation at lowest cost. Batch processing supported.",
        "endpoint_url": "https://api.segmind.com/v1/sd1.5-txt2img",
        "category": "image",
        "pricing_usdc": 0.000008,
        "payment_protocol": "wayforth",
    },
    # Code / Compute
    {
        "name": "Modal Serverless Compute",
        "description": "Run Python functions on serverless GPUs and CPUs. Cold start under 300ms. Best for agents that need scalable, on-demand compute for ML workloads.",
        "endpoint_url": "https://api.modal.com/v1",
        "category": "code",
        "pricing_usdc": 0.00006,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Fly.io Machines API",
        "description": "Launch isolated micro-VMs in 35+ regions in milliseconds. Full Linux environment with custom Docker images. Best for agents needing arbitrary code execution.",
        "endpoint_url": "https://api.machines.dev/v1/apps",
        "category": "code",
        "pricing_usdc": 0.000019,
        "payment_protocol": "wayforth",
    },
]

async def seed():
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    db = await asyncpg.connect(db_url)
    added = updated = 0
    for svc in SERVICES:
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
                   VALUES ($1, $2, $3, $4, $5, $6, 0, 'curated_v3', NOW())""",
                svc["name"], svc["description"], svc["endpoint_url"],
                svc["category"], svc["pricing_usdc"], svc["payment_protocol"],
            )
            print(f"  Added:   {svc['name']}")
            added += 1
    total = await db.fetchval("SELECT COUNT(*) FROM services")
    await db.close()
    print(f"\nDone. Added {added}, updated {updated}. Total services: {total}")

asyncio.run(seed())
