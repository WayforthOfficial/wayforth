import asyncio
import json
import logging
import os
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
# Upsert
# ---------------------------------------------------------------------------

async def upsert_service(conn: asyncpg.Connection, svc: dict[str, Any]) -> str:
    """Insert or update a service record. Returns 'inserted' | 'updated' | 'skipped'."""
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO services (name, description, endpoint_url, category, source, metadata)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (endpoint_url) DO UPDATE
                SET name        = EXCLUDED.name,
                    description = EXCLUDED.description,
                    category    = EXCLUDED.category,
                    source      = EXCLUDED.source,
                    metadata    = EXCLUDED.metadata,
                    updated_at  = NOW()
            RETURNING (xmax = 0) AS inserted
            """,
            svc["name"],
            svc.get("description"),
            svc["endpoint_url"],
            svc.get("category"),
            svc.get("source"),
            json.dumps(svc.get("metadata", {})),
        )
        return "inserted" if row["inserted"] else "updated"
    except Exception as exc:
        logger.warning("upsert failed for %s: %s", svc.get("endpoint_url"), exc)
        return "skipped"


# ---------------------------------------------------------------------------
# MCP Registry crawler
# ---------------------------------------------------------------------------

_MCP_REGISTRY_URL = "https://registry.mcp.so/api/servers"


def _parse_mcp_server(entry: dict) -> dict[str, Any] | None:
    url = (
        entry.get("url")
        or entry.get("endpoint_url")
        or entry.get("homepage")
        or entry.get("repository")
    )
    name = entry.get("name") or entry.get("title")
    if not url or not name:
        return None
    return {
        "name": str(name)[:255],
        "description": entry.get("description") or entry.get("summary"),
        "endpoint_url": str(url),
        "category": "data",
        "source": "mcp_registry",
        "metadata": {
            k: v
            for k, v in entry.items()
            if k not in ("name", "title", "description", "summary", "url", "endpoint_url")
        },
    }


async def crawl_mcp_registry(conn: asyncpg.Connection) -> tuple[int, int, int]:
    inserted = updated = skipped = 0
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(_MCP_REGISTRY_URL, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("MCP registry fetch failed: %s", exc)
        return 0, 0, 0

    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("servers") or data.get("results") or data.get("data") or []
        if not isinstance(entries, list):
            logger.warning("MCP registry: unexpected shape %s", type(entries))
            return 0, 0, 0
    else:
        logger.warning("MCP registry: unexpected top-level type %s", type(data))
        return 0, 0, 0

    for raw in entries:
        if not isinstance(raw, dict):
            continue
        svc = _parse_mcp_server(raw)
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

    logger.info("MCP registry: inserted=%d updated=%d skipped=%d", inserted, updated, skipped)
    return inserted, updated, skipped


# ---------------------------------------------------------------------------
# Bankr x402 crawler (with mock fallback)
# ---------------------------------------------------------------------------

_BANKR_URL = "https://bankr.io/api/x402/services"

_BANKR_MOCK: list[dict[str, Any]] = [
    {
        "name": "Bankr Inference — GPT-4o Router",
        "description": "Route inference requests across major LLM providers with automatic failover.",
        "endpoint_url": "https://bankr.io/x402/inference/gpt4o-router",
        "category": "inference",
        "source": "x402_bankr",
        "metadata": {"model": "gpt-4o", "provider": "openai", "x402": True},
    },
    {
        "name": "Bankr Inference — Claude Sonnet",
        "description": "Pay-per-call access to Claude Sonnet via the x402 payment protocol.",
        "endpoint_url": "https://bankr.io/x402/inference/claude-sonnet",
        "category": "inference",
        "source": "x402_bankr",
        "metadata": {"model": "claude-sonnet-4-6", "provider": "anthropic", "x402": True},
    },
    {
        "name": "Bankr Data — News Feed Aggregator",
        "description": "Real-time news aggregation from 5,000+ sources, tokenised per article batch.",
        "endpoint_url": "https://bankr.io/x402/data/news-feed",
        "category": "data",
        "source": "x402_bankr",
        "metadata": {"sources": 5000, "latency_ms": 120, "x402": True},
    },
    {
        "name": "Bankr Translation — DeepL Pro Gateway",
        "description": "High-quality neural translation via DeepL Pro, billed per 1k characters.",
        "endpoint_url": "https://bankr.io/x402/translation/deepl-pro",
        "category": "translation",
        "source": "x402_bankr",
        "metadata": {"engine": "deepl-pro", "langs": 29, "x402": True},
    },
    {
        "name": "Bankr Data — On-Chain Price Oracle",
        "description": "Signed spot prices for 500+ ERC-20 tokens, updated every block.",
        "endpoint_url": "https://bankr.io/x402/data/price-oracle",
        "category": "data",
        "source": "x402_bankr",
        "metadata": {"tokens": 500, "chain": "base", "x402": True},
    },
]


async def crawl_bankr_x402(conn: asyncpg.Connection) -> tuple[int, int, int]:
    inserted = updated = skipped = 0
    entries: list[dict[str, Any]] = []

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_BANKR_URL, headers={"Accept": "application/json"})
            resp.raise_for_status()
            raw = resp.json()
            entries = raw if isinstance(raw, list) else raw.get("services", [])
            logger.info("Bankr x402: fetched %d entries from live API", len(entries))
    except Exception as exc:
        logger.info("Bankr x402 API unavailable (%s) — seeding mock data", exc)
        entries = _BANKR_MOCK

    for raw in entries:
        if not isinstance(raw, dict):
            continue
        svc = {
            "name": raw.get("name", "Unknown"),
            "description": raw.get("description"),
            "endpoint_url": raw.get("endpoint_url") or raw.get("url", ""),
            "category": raw.get("category", "inference"),
            "source": raw.get("source", "x402_bankr"),
            "metadata": raw.get("metadata", {}),
        }
        if not svc["endpoint_url"]:
            skipped += 1
            continue
        outcome = await upsert_service(conn, svc)
        if outcome == "inserted":
            inserted += 1
        elif outcome == "updated":
            updated += 1
        else:
            skipped += 1

    logger.info("Bankr x402: inserted=%d updated=%d skipped=%d", inserted, updated, skipped)
    return inserted, updated, skipped


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _run() -> None:
    conn = await asyncpg.connect(_ASYNCPG_URL)
    try:
        mcp_i, mcp_u, mcp_s = await crawl_mcp_registry(conn)
        bankr_i, bankr_u, bankr_s = await crawl_bankr_x402(conn)
    finally:
        await conn.close()

    total = mcp_i + mcp_u + mcp_s + bankr_i + bankr_u + bankr_s
    new_total = mcp_i + bankr_i
    updated_total = mcp_u + bankr_u
    print(f"\nCrawled {total} services, inserted {new_total} new, updated {updated_total} existing")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
