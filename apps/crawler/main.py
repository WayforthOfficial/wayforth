import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("crawler")

DB_URL = os.environ.get("DATABASE_URL", "postgresql://wayforth:wayforth_dev@localhost:5432/wayforth")
_ASYNCPG_URL = DB_URL.replace("postgresql+asyncpg://", "postgresql://")

# ---------------------------------------------------------------------------
# Category inference
# ---------------------------------------------------------------------------

_INFERENCE_KW = {"llm", "model", "gpt", "claude", "gemini", "mistral",
                 "inference", "completion", "embedding", "openai", "anthropic"}
_TRANSLATION_KW = {"translate", "translation", "language", "localization",
                   "multilingual", "i18n", "l10n"}


def categorize_service(name: str, description: str | None) -> str:
    text = f"{name} {description or ''}".lower()
    if any(kw in text for kw in _INFERENCE_KW):
        return "inference"
    if any(kw in text for kw in _TRANSLATION_KW):
        return "translation"
    return "data"


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

async def upsert_service(conn: asyncpg.Connection, svc: dict[str, Any]) -> str:
    """Insert or update a service record. Returns 'inserted' | 'updated' | 'skipped'."""
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO services (name, description, endpoint_url, category, source, pricing_usdc, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (endpoint_url) DO UPDATE
                SET name        = EXCLUDED.name,
                    description = EXCLUDED.description,
                    category    = EXCLUDED.category,
                    source      = EXCLUDED.source,
                    pricing_usdc = EXCLUDED.pricing_usdc,
                    metadata    = EXCLUDED.metadata,
                    updated_at  = NOW()
            RETURNING (xmax = 0) AS inserted
            """,
            svc["name"],
            svc.get("description"),
            svc["endpoint_url"],
            svc.get("category"),
            svc.get("source"),
            svc.get("pricing_usdc"),
            json.dumps(svc.get("metadata", {})),
        )
        return "inserted" if row["inserted"] else "updated"
    except Exception as exc:
        logger.warning("upsert failed for %s: %s", svc.get("endpoint_url"), exc)
        return "skipped"


# ---------------------------------------------------------------------------
# Step 0: delete low-quality Tier 0 services from old sources
# ---------------------------------------------------------------------------

async def delete_low_quality_tier0(conn: asyncpg.Connection) -> int:
    result = await conn.execute(
        """
        DELETE FROM services
        WHERE source IN ('mcp_registry', 'smithery', 'mcp_get')
          AND coverage_tier = 0
        """
    )
    # result is a string like "DELETE 114"
    count = int(result.split()[-1])
    return count


# ---------------------------------------------------------------------------
# Source A: Awesome MCP Servers README (GitHub)
# ---------------------------------------------------------------------------

_AWESOME_URL = (
    "https://raw.githubusercontent.com/punkpeye/awesome-mcp-servers/main/README.md"
)
_ITEM_RE = re.compile(r"^-\s+\[([^\]]+)\]\(([^)]+)\)(?:\s+-\s+(.+))?")


async def crawl_awesome_mcp(conn: asyncpg.Connection) -> tuple[int, int, int]:
    inserted = updated = skipped = 0
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(_AWESOME_URL)
            resp.raise_for_status()
            text = resp.text
    except Exception as exc:
        logger.warning("awesome-mcp fetch failed: %s", exc)
        return 0, 0, 0

    for line in text.splitlines():
        m = _ITEM_RE.match(line.strip())
        if not m:
            continue
        name, url, desc = m.group(1), m.group(2), m.group(3)
        if not url.startswith("http") or not name:
            skipped += 1
            continue
        desc = desc.strip() if desc else None
        svc = {
            "name": name[:255],
            "description": desc,
            "endpoint_url": url,
            "category": categorize_service(name, desc),
            "source": "awesome_mcp",
            "pricing_usdc": None,
            "metadata": {},
        }
        outcome = await upsert_service(conn, svc)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "updated":
            updated += 1
        else:
            skipped += 1

    logger.info("awesome-mcp: inserted=%d updated=%d skipped=%d", inserted, updated, skipped)
    return inserted, updated, skipped


# ---------------------------------------------------------------------------
# Source B: Glama MCP registry — paginated up to 200 services
# ---------------------------------------------------------------------------

_GLAMA_URL = "https://glama.ai/api/mcp/v1/servers"
_GLAMA_PAGE_SIZE = 50
_GLAMA_MAX = 200


def _parse_glama_entry(entry: dict) -> dict[str, Any] | None:
    name = entry.get("name")
    url = (
        entry.get("url")
        or (entry.get("repository") or {}).get("url")
    )
    if not name or not url:
        return None
    desc = entry.get("description")
    return {
        "name": str(name)[:255],
        "description": desc,
        "endpoint_url": str(url),
        "category": categorize_service(str(name), desc),
        "source": "glama",
        "pricing_usdc": None,
        "metadata": {
            k: v for k, v in entry.items()
            if k not in ("name", "description", "url", "repository")
        },
    }


async def crawl_glama(conn: asyncpg.Connection) -> tuple[int, int, int]:
    inserted = updated = skipped = 0
    cursor: str | None = None
    total_fetched = 0

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            while total_fetched < _GLAMA_MAX:
                params: dict[str, Any] = {"limit": _GLAMA_PAGE_SIZE}
                if cursor:
                    params["after"] = cursor

                try:
                    resp = await client.get(_GLAMA_URL, params=params,
                                            headers={"Accept": "application/json"})
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as exc:
                    logger.warning("Glama page fetch failed: %s", exc)
                    break

                # Extract entries from various possible response shapes
                if isinstance(data, list):
                    entries = data
                    page_info: dict = {}
                elif isinstance(data, dict):
                    entries = (
                        data.get("data")
                        or data.get("servers")
                        or data.get("results")
                        or []
                    )
                    page_info = data.get("pageInfo") or {}
                else:
                    logger.warning("Glama: unexpected response type %s", type(data))
                    break

                if not entries:
                    break

                for raw in entries:
                    if not isinstance(raw, dict):
                        continue
                    svc = _parse_glama_entry(raw)
                    if svc is None:
                        skipped += 1
                        continue
                    outcome = await upsert_service(conn, svc)
                    if outcome == "inserted":
                        inserted += 1
                    elif outcome == "updated":
                        updated += 1
                    else:
                        skipped += 1

                total_fetched += len(entries)

                cursor = page_info.get("endCursor")
                if not page_info.get("hasNextPage") or not cursor:
                    break

    except Exception as exc:
        logger.warning("Glama crawl error: %s", exc)

    logger.info("Glama: inserted=%d updated=%d skipped=%d (fetched ~%d)",
                inserted, updated, skipped, total_fetched)
    return inserted, updated, skipped


# ---------------------------------------------------------------------------
# Source C: Hardcoded high-quality seed list
# ---------------------------------------------------------------------------

_SEED_SERVICES: list[dict[str, Any]] = [
    # inference
    {
        "name": "OpenRouter",
        "description": "Unified API for 200+ LLMs including GPT-4, Claude, Llama. Pay per token.",
        "endpoint_url": "https://openrouter.ai/api/v1",
        "category": "inference",
        "pricing_usdc": 0.000001,
    },
    {
        "name": "Together AI",
        "description": "Fast inference for open-source models including Llama, Mistral, Qwen.",
        "endpoint_url": "https://api.together.xyz/v1",
        "category": "inference",
        "pricing_usdc": 0.000001,
    },
    {
        "name": "Groq",
        "description": "Ultra-fast LLM inference. Llama 3 at 800 tokens/second.",
        "endpoint_url": "https://api.groq.com/openai/v1",
        "category": "inference",
        "pricing_usdc": 0.0000001,
    },
    {
        "name": "Replicate",
        "description": "Run open-source ML models via API. Image, video, audio, text generation.",
        "endpoint_url": "https://api.replicate.com/v1",
        "category": "inference",
        "pricing_usdc": 0.0001,
    },
    {
        "name": "Fireworks AI",
        "description": "Production inference for open-source LLMs. Fast, cheap, reliable.",
        "endpoint_url": "https://api.fireworks.ai/inference/v1",
        "category": "inference",
        "pricing_usdc": 0.0000002,
    },
    {
        "name": "Hugging Face Inference",
        "description": "Inference API for 200,000+ models hosted on Hugging Face.",
        "endpoint_url": "https://api-inference.huggingface.co/models",
        "category": "inference",
        "pricing_usdc": 0.000001,
    },
    {
        "name": "Voyage AI",
        "description": "State-of-the-art text embeddings for RAG and semantic search.",
        "endpoint_url": "https://api.voyageai.com/v1",
        "category": "inference",
        "pricing_usdc": 0.0000001,
    },
    {
        "name": "fal.ai",
        "description": "Fast inference for image and video generation models. Flux, SDXL, Sora.",
        "endpoint_url": "https://fal.run",
        "category": "inference",
        "pricing_usdc": 0.001,
    },
    # data
    {
        "name": "Polygon.io",
        "description": "Real-time and historical stock, options, forex, and crypto market data.",
        "endpoint_url": "https://api.polygon.io/v2",
        "category": "data",
        "pricing_usdc": 0.0001,
    },
    {
        "name": "Alpha Vantage",
        "description": "Free stock market API. Equities, forex, crypto, economic indicators.",
        "endpoint_url": "https://www.alphavantage.co/query",
        "category": "data",
        "pricing_usdc": 0.00001,
    },
    {
        "name": "Clearbit",
        "description": "Company and person enrichment API. Firmographics, tech stack, contacts.",
        "endpoint_url": "https://person.clearbit.com/v2",
        "category": "data",
        "pricing_usdc": 0.01,
    },
    {
        "name": "Hunter.io",
        "description": "Find and verify professional email addresses by domain or name.",
        "endpoint_url": "https://api.hunter.io/v2",
        "category": "data",
        "pricing_usdc": 0.005,
    },
    {
        "name": "NewsAPI",
        "description": "Live and historical news articles from 150,000+ sources worldwide.",
        "endpoint_url": "https://newsapi.org/v2",
        "category": "data",
        "pricing_usdc": 0.0001,
    },
    {
        "name": "OpenWeatherMap",
        "description": "Current weather and 16-day forecast for any city. 60 calls/minute free.",
        "endpoint_url": "https://api.openweathermap.org/data/2.5",
        "category": "data",
        "pricing_usdc": 0.0001,
    },
    {
        "name": "Mapbox",
        "description": "Maps, geocoding, routing, and search APIs for location-aware applications.",
        "endpoint_url": "https://api.mapbox.com",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    # translation
    {
        "name": "DeepL API",
        "description": "Highest quality neural machine translation. 31 languages. GDPR compliant.",
        "endpoint_url": "https://api-free.deepl.com/v2",
        "category": "translation",
        "pricing_usdc": 0.0008,
    },
    {
        "name": "Google Cloud Translation",
        "description": "Neural machine translation across 130+ languages. AutoML support.",
        "endpoint_url": "https://translation.googleapis.com/language/translate/v2",
        "category": "translation",
        "pricing_usdc": 0.00002,
    },
    {
        "name": "Azure Translator",
        "description": "Microsoft neural translation. 100+ languages. Custom glossary support.",
        "endpoint_url": "https://api.cognitive.microsofttranslator.com",
        "category": "translation",
        "pricing_usdc": 0.00001,
    },
    {
        "name": "LibreTranslate",
        "description": "Open-source self-hostable machine translation API. Free to use.",
        "endpoint_url": "https://libretranslate.com/translate",
        "category": "translation",
        "pricing_usdc": 0.0,
    },
    {
        "name": "Lingvanex",
        "description": "Translation API with 112 languages. Supports text, documents, and voice.",
        "endpoint_url": "https://api-b2b.backenster.com/b1/api/v3/translate",
        "category": "translation",
        "pricing_usdc": 0.0001,
    },
]


async def crawl_seeds(conn: asyncpg.Connection) -> tuple[int, int, int]:
    inserted = updated = skipped = 0
    for entry in _SEED_SERVICES:
        svc = {**entry, "source": "seed", "metadata": {}}
        outcome = await upsert_service(conn, svc)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "updated":
            updated += 1
        else:
            skipped += 1
    logger.info("seeds: inserted=%d updated=%d skipped=%d", inserted, updated, skipped)
    return inserted, updated, skipped


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run() -> None:
    conn = await asyncpg.connect(_ASYNCPG_URL)
    try:
        deleted = await delete_low_quality_tier0(conn)

        awesome_i, awesome_u, awesome_s = await crawl_awesome_mcp(conn)
        glama_i, glama_u, glama_s = await crawl_glama(conn)
        seed_i, seed_u, seed_s = await crawl_seeds(conn)

        sample = await conn.fetch(
            "SELECT name FROM services WHERE source = 'seed' ORDER BY name LIMIT 5"
        )
    finally:
        await conn.close()

    print(f"\n--- Deleted {deleted} low-quality Tier 0 services (mcp_get/smithery/mcp_registry)")
    print(f"--- Awesome MCP : inserted={awesome_i} updated={awesome_u} skipped={awesome_s}")
    print(f"--- Glama       : inserted={glama_i} updated={glama_u} skipped={glama_s}")
    print(f"--- Seeds       : inserted={seed_i} updated={seed_u} skipped={seed_s}")
    if sample:
        print("\nSample seed services:")
        for r in sample:
            print(f"  {r['name']}")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
