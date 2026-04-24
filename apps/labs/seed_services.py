import asyncio
import json
import logging
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("seed_services")

_DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://wayforth:wayforth_dev@localhost:5432/wayforth"
)
_ASYNCPG_URL = _DB_URL.replace("postgresql+asyncpg://", "postgresql://")

_BASE_URL = "http://localhost:8001"

_LABS_SERVICES = [
    {
        "name": "Wayforth Labs Translator",
        "description": "Free text translation via MyMemory. Supports 50+ languages, 1000 req/day.",
        "endpoint_url": f"{_BASE_URL}/translate",
        "category": "translation",
        "coverage_tier": 2,
        "source": "wayforth_labs",
        "metadata": {"upstream": "mymemory", "limit": "1000/day"},
    },
    {
        "name": "Wayforth Labs Weather",
        "description": "Current weather conditions for any city via wttr.in. No API key required.",
        "endpoint_url": f"{_BASE_URL}/weather",
        "category": "data",
        "coverage_tier": 2,
        "source": "wayforth_labs",
        "metadata": {"upstream": "wttr.in", "format": "j1"},
    },
    {
        "name": "Wayforth Labs Stock",
        "description": "Real-time stock prices and daily change via Yahoo Finance. No API key required.",
        "endpoint_url": f"{_BASE_URL}/stock",
        "category": "data",
        "coverage_tier": 2,
        "source": "wayforth_labs",
        "metadata": {"upstream": "yahoo_finance", "interval": "1d"},
    },
    {
        "name": "Wayforth Labs Summarizer",
        "description": "Extractive text summarization (first N sentences). Pure Python, zero latency.",
        "endpoint_url": f"{_BASE_URL}/summarize",
        "category": "inference",
        "coverage_tier": 2,
        "source": "wayforth_labs",
        "metadata": {"method": "extractive", "max_sentences": 50},
    },
    {
        "name": "Wayforth Labs Search",
        "description": "Web search via DuckDuckGo with automatic fallback. Returns titles, URLs, snippets.",
        "endpoint_url": f"{_BASE_URL}/search",
        "category": "data",
        "coverage_tier": 2,
        "source": "wayforth_labs",
        "metadata": {"upstream": "duckduckgo", "fallback": "ddg_instant"},
    },
]

_UPSERT_SQL = """
    INSERT INTO services (name, description, endpoint_url, category, coverage_tier, source, metadata)
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (endpoint_url) DO UPDATE
        SET name          = EXCLUDED.name,
            description   = EXCLUDED.description,
            category      = EXCLUDED.category,
            coverage_tier = EXCLUDED.coverage_tier,
            source        = EXCLUDED.source,
            metadata      = EXCLUDED.metadata,
            updated_at    = NOW()
    RETURNING (xmax = 0) AS inserted
"""


async def _run() -> None:
    conn = await asyncpg.connect(_ASYNCPG_URL)
    inserted_count = updated_count = 0
    try:
        for svc in _LABS_SERVICES:
            row = await conn.fetchrow(
                _UPSERT_SQL,
                svc["name"],
                svc["description"],
                svc["endpoint_url"],
                svc["category"],
                svc["coverage_tier"],
                svc["source"],
                json.dumps(svc["metadata"]),
            )
            if row["inserted"]:
                inserted_count += 1
                logger.info("INSERTED  %s", svc["endpoint_url"])
            else:
                updated_count += 1
                logger.info("UPDATED   %s", svc["endpoint_url"])
    finally:
        await conn.close()

    print(f"\nWayforth Labs seed complete: inserted={inserted_count} updated={updated_count}")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
