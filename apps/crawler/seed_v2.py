"""
Wayforth Catalog Seed v2 — high-quality curated services with real descriptions.
Run once: python seed_v2.py
"""
import asyncio, asyncpg, os

SERVICES = [
    # Inference
    {"name": "Groq API", "description": "Ultra-low latency LLM inference — Llama, Mixtral, Gemma. Sub-100ms response times. Best for real-time agent workflows.", "endpoint_url": "https://api.groq.com/openai/v1", "category": "inference", "pricing_usdc": 0.00001, "payment_protocol": "wayforth"},
    {"name": "Fireworks AI", "description": "Fast open-source model inference — Llama 3, Mistral, Gemma, Mixtral. Competitive pricing with batching support.", "endpoint_url": "https://api.fireworks.ai/inference/v1", "category": "inference", "pricing_usdc": 0.0000009, "payment_protocol": "wayforth"},
    {"name": "Together AI", "description": "70+ open-source models with fast inference. Supports fine-tuning and embeddings. Good for cost-sensitive agent workloads.", "endpoint_url": "https://api.together.xyz/v1", "category": "inference", "pricing_usdc": 0.0000008, "payment_protocol": "wayforth"},
    {"name": "Perplexity AI API", "description": "Search-augmented LLM inference with real-time web access. Best for agents that need current information.", "endpoint_url": "https://api.perplexity.ai", "category": "inference", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "Anthropic Claude API", "description": "Claude 3.5 Sonnet, Haiku, Opus. Best-in-class for reasoning, coding, and long-context tasks.", "endpoint_url": "https://api.anthropic.com/v1", "category": "inference", "pricing_usdc": 0.000003, "payment_protocol": "wayforth"},
    # Data
    {"name": "Serper API", "description": "Google search results via API. Real-time web search for agents. JSON results, 2500 free queries/month.", "endpoint_url": "https://google.serper.dev/search", "category": "data", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "NewsAPI", "description": "Real-time news from 80,000+ sources. Search by keyword, source, or topic. Best for agents monitoring current events.", "endpoint_url": "https://newsapi.org/v2", "category": "data", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "OpenWeatherMap API", "description": "Current weather, forecasts, and historical data for any location. 1M+ cities. Best for agents with location context.", "endpoint_url": "https://api.openweathermap.org/data/2.5", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Polygon.io", "description": "Real-time and historical stocks, forex, crypto data. WebSocket streaming. Built for algorithmic trading agents.", "endpoint_url": "https://api.polygon.io/v2", "category": "data", "pricing_usdc": 0.00001, "payment_protocol": "wayforth"},
    {"name": "Alpha Vantage", "description": "Free stock market data API. 20+ years of historical data. Fundamentals, earnings, and technical indicators.", "endpoint_url": "https://www.alphavantage.co/query", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    # Translation
    {"name": "DeepL API", "description": "Highest-quality neural translation. 29 languages. Consistently outperforms Google Translate in accuracy benchmarks.", "endpoint_url": "https://api-free.deepl.com/v2/translate", "category": "translation", "pricing_usdc": 0.0000025, "payment_protocol": "wayforth"},
    {"name": "ModernMT", "description": "Adaptive machine translation that learns from corrections. Best for domain-specific translation workflows.", "endpoint_url": "https://api.modernmt.com/translate", "category": "translation", "pricing_usdc": 0.000003, "payment_protocol": "wayforth"},
    # Image/Vision
    {"name": "Stability AI API", "description": "Stable Diffusion image generation. SDXL, SD3, Stable Video. Best for agents that need image creation.", "endpoint_url": "https://api.stability.ai/v1", "category": "image", "pricing_usdc": 0.0002, "payment_protocol": "wayforth"},
    {"name": "Replicate API", "description": "Run any open-source ML model via API. 100,000+ models including Llama, Stable Diffusion, Whisper.", "endpoint_url": "https://api.replicate.com/v1", "category": "inference", "pricing_usdc": 0.0001, "payment_protocol": "wayforth"},
    {"name": "Fal.ai", "description": "Fast image generation and computer vision. Sub-second Stable Diffusion. Real-time media processing for agents.", "endpoint_url": "https://fal.run", "category": "image", "pricing_usdc": 0.00003, "payment_protocol": "wayforth"},
    # Code
    {"name": "E2B Code Interpreter", "description": "Secure sandboxed code execution. Run Python, JS, bash in isolated containers. Best for agents that write and run code.", "endpoint_url": "https://api.e2b.dev/v1", "category": "code", "pricing_usdc": 0.00014, "payment_protocol": "wayforth"},
    # Audio
    {"name": "ElevenLabs API", "description": "Ultra-realistic text-to-speech. 1000+ voices, voice cloning, multilingual. Best for agents that produce audio.", "endpoint_url": "https://api.elevenlabs.io/v1", "category": "audio", "pricing_usdc": 0.000030, "payment_protocol": "wayforth"},
    {"name": "AssemblyAI", "description": "Speech-to-text with speaker diarization, sentiment analysis, and topic detection. Best for agents processing audio.", "endpoint_url": "https://api.assemblyai.com/v2", "category": "audio", "pricing_usdc": 0.000065, "payment_protocol": "wayforth"},
    # Embeddings
    {"name": "Cohere Embed API", "description": "State-of-the-art embeddings for search and RAG. Multilingual support. Best for agents building knowledge bases.", "endpoint_url": "https://api.cohere.ai/v1/embed", "category": "embeddings", "pricing_usdc": 0.0000001, "payment_protocol": "wayforth"},
    {"name": "Jina Embeddings", "description": "Open-source embeddings API. 8192 token context. Free tier available. Best for semantic search in agent pipelines.", "endpoint_url": "https://api.jina.ai/v1/embeddings", "category": "embeddings", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
]

async def seed():
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    db = await asyncpg.connect(db_url)
    added = updated = 0
    for svc in SERVICES:
        existing = await db.fetchval("SELECT id FROM services WHERE endpoint_url = $1", svc["endpoint_url"])
        if existing:
            await db.execute("""
                UPDATE services SET description=$1, pricing_usdc=$2, payment_protocol=$3
                WHERE endpoint_url=$4
            """, svc["description"], svc["pricing_usdc"], svc["payment_protocol"], svc["endpoint_url"])
            print(f"  Updated: {svc['name']}")
            updated += 1
        else:
            await db.execute("""
                INSERT INTO services (name, description, endpoint_url, category, pricing_usdc,
                                     payment_protocol, coverage_tier, source, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, 0, 'curated_v2', NOW())
            """, svc["name"], svc["description"], svc["endpoint_url"],
                svc["category"], svc["pricing_usdc"], svc["payment_protocol"])
            print(f"  Added:   {svc['name']}")
            added += 1
    await db.close()
    print(f"\nDone. Added {added}, updated {updated}.")

asyncio.run(seed())
