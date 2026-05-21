"""
Catalog expander — ingests APIs from public directories into the Wayforth catalog.

Sources:
  A: APIs.guru  https://api.apis.guru/v2/list.json  (2,300+ production APIs with OpenAPI specs)
  B: public-apis https://github.com/public-apis/public-apis  (markdown table)
  C: Postman API Network  (skipped — auth-gated)

Deduplication: normalize to base domain before inserting. Skips any domain
already present in the catalog (conflict on endpoint_url column).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("catalog_expander")

# DATABASE_PUBLIC_URL (externally routable) is preferred when running outside
# Railway's private network. DATABASE_URL uses postgres.railway.internal which
# only resolves from within Railway. Both are set on the crawler service.
DB_URL = (
    os.environ.get("DATABASE_PUBLIC_URL")
    or os.environ.get("DATABASE_URL", "")
)
_ASYNCPG_URL = DB_URL.replace("postgresql+asyncpg://", "postgresql://")

_INFERENCE_KW = frozenset({
    "llm", "model", "gpt", "claude", "gemini", "mistral", "inference",
    "completion", "embedding", "openai", "anthropic", "machine-learning",
    "machine learning", "ai", "nlp", "speech", "voice", "audio",
    "image generation", "vision", "diffusion", "stable diffusion",
})
_TRANSLATION_KW = frozenset({
    "translate", "translation", "language", "localization",
    "multilingual", "i18n", "l10n", "lingua",
})


def _categorize(name: str, desc: str | None, extra_tags: list[str] | None = None) -> str:
    text = f"{name} {desc or ''} {' '.join(extra_tags or [])}".lower()
    if any(kw in text for kw in _INFERENCE_KW):
        return "inference"
    if any(kw in text for kw in _TRANSLATION_KW):
        return "translation"
    return "data"


def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and not url.startswith("http"):
        url = "https://" + url
    return url


def _domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


async def _upsert(conn: asyncpg.Connection, svc: dict) -> str:
    """Insert new service; silently skip on conflict (duplicate endpoint_url)."""
    try:
        row = await conn.fetchrow(
            """
            INSERT INTO services
                (name, description, endpoint_url, category, source,
                 pricing_usdc, metadata, payment_protocol)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (endpoint_url) DO NOTHING
            RETURNING id
            """,
            svc["name"][:255],
            svc.get("description"),
            svc["endpoint_url"],
            svc.get("category", "data"),
            svc.get("source", "external"),
            svc.get("pricing_usdc"),
            json.dumps(svc.get("metadata", {})),
            svc.get("payment_protocol", "wayforth"),
        )
        return "inserted" if row else "skipped"
    except Exception as exc:
        logger.debug("upsert skipped %s: %s", svc.get("endpoint_url"), exc)
        return "skipped"


# ── Source A: APIs.guru ───────────────────────────────────────────────────────

_APIS_GURU_URL = "https://api.apis.guru/v2/list.json"


async def ingest_apis_guru(
    conn: asyncpg.Connection,
    client: httpx.AsyncClient,
    seen_domains: set[str],
) -> tuple[int, int]:
    inserted = skipped = 0
    try:
        resp = await client.get(_APIS_GURU_URL, timeout=40.0)
        resp.raise_for_status()
        data: dict = resp.json()
    except Exception as exc:
        logger.warning("APIs.guru fetch failed: %s", exc)
        return 0, 0

    # Pre-load known domains from DB to avoid per-row queries
    existing = await conn.fetch("SELECT endpoint_url FROM services")
    db_domains = {_domain_of(r["endpoint_url"]) for r in existing}

    for api_id, api_meta in data.items():
        preferred = api_meta.get("preferred", "")
        versions: dict = api_meta.get("versions", {})
        vdata = versions.get(preferred) or (list(versions.values())[-1] if versions else None)
        if not vdata:
            skipped += 1
            continue

        info: dict = vdata.get("info", {})
        title: str = info.get("title") or api_id
        description: str = info.get("description") or ""
        raw_cats: list[str] = info.get("x-apisguru-categories") or []

        # Resolve base URL: servers > externalDocs > contact > x-origin
        servers: list[dict] = vdata.get("servers") or []
        x_origin: list[dict] = info.get("x-origin") or []
        base_url = (
            (servers[0].get("url") if servers else None)
            or (vdata.get("externalDocs") or {}).get("url")
            or (info.get("contact") or {}).get("url")
            or (x_origin[0].get("url") if x_origin else None)
        )
        if not base_url or not str(base_url).startswith("http"):
            skipped += 1
            continue

        base_url = _normalize_url(str(base_url))
        domain = _domain_of(base_url)
        if not domain or domain in seen_domains or domain in db_domains:
            skipped += 1
            continue
        seen_domains.add(domain)
        db_domains.add(domain)

        svc = {
            "name": str(title)[:255],
            "description": str(description)[:1000] if description else None,
            "endpoint_url": base_url,
            "category": _categorize(title, description, raw_cats),
            "source": "apis_guru",
            "metadata": {"api_id": api_id, "version": preferred, "categories": raw_cats},
        }
        outcome = await _upsert(conn, svc)
        if outcome == "inserted":
            inserted += 1
        else:
            skipped += 1

    logger.info("APIs.guru: inserted=%d skipped=%d (from %d entries)", inserted, skipped, len(data))
    return inserted, skipped


# ── Source B: public-apis GitHub README ──────────────────────────────────────

_PUBLIC_APIS_URL = (
    "https://raw.githubusercontent.com/public-apis/public-apis/master/README.md"
)
# Matches: | [Name](https://...) | Description | Auth | HTTPS | ...
_ROW_RE = re.compile(
    r"^\|\s*\[([^\]]+)\]\((https?://[^)]+)\)\s*\|"
    r"\s*([^|]*?)\s*\|"   # description
    r"\s*([^|]*?)\s*\|"   # auth
    r"\s*(Yes|No)\s*\|",  # HTTPS
    re.IGNORECASE,
)


async def ingest_public_apis(
    conn: asyncpg.Connection,
    client: httpx.AsyncClient,
    seen_domains: set[str],
) -> tuple[int, int]:
    inserted = skipped = 0
    try:
        resp = await client.get(_PUBLIC_APIS_URL, timeout=30.0)
        resp.raise_for_status()
        text = resp.text
    except Exception as exc:
        logger.warning("public-apis fetch failed: %s", exc)
        return 0, 0

    existing = await conn.fetch("SELECT endpoint_url FROM services")
    db_domains = {_domain_of(r["endpoint_url"]) for r in existing}

    section_category = "data"
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip().lower()
            if any(t in section for t in ("machine learning", "artificial intelligence")):
                section_category = "inference"
            elif any(t in section for t in ("translation", "language")):
                section_category = "translation"
            else:
                section_category = "data"
            continue

        m = _ROW_RE.match(line)
        if not m:
            continue

        name, url, desc_raw, auth_raw, https_flag = (
            m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        )
        if https_flag.strip().lower() != "yes":
            skipped += 1
            continue  # skip HTTP-only

        url = _normalize_url(url)
        domain = _domain_of(url)
        if not domain or domain in seen_domains or domain in db_domains:
            skipped += 1
            continue
        seen_domains.add(domain)
        db_domains.add(domain)

        desc = desc_raw.strip() or None
        auth = auth_raw.strip()
        if auth and auth.lower() != "no":
            desc = f"{desc or name}. Auth: {auth}."

        svc = {
            "name": str(name)[:255],
            "description": str(desc)[:1000] if desc else None,
            "endpoint_url": url,
            "category": _categorize(name, desc, [section_category]),
            "source": "public_apis",
            "metadata": {"auth": auth, "https": https_flag.strip()},
        }
        outcome = await _upsert(conn, svc)
        if outcome == "inserted":
            inserted += 1
        else:
            skipped += 1

    logger.info("public-apis: inserted=%d skipped=%d", inserted, skipped)
    return inserted, skipped


# ── Source C: Postman (skipped) ───────────────────────────────────────────────

async def ingest_postman(
    conn: asyncpg.Connection,
    client: httpx.AsyncClient,
    seen_domains: set[str],
) -> tuple[int, int]:
    logger.info("Postman: skipped (requires OAuth / API key)")
    return 0, 0


# ── Entry point ───────────────────────────────────────────────────────────────

async def run_catalog_expansion(db_url: str) -> dict[str, int]:
    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)

    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": "wayforth-catalog-expander/1.0"},
        timeout=30.0,
    ) as client:
        async with pool.acquire() as conn:
            before = await conn.fetchval("SELECT COUNT(*) FROM services")
            seen_domains: set[str] = set()

            guru_i, guru_s = await ingest_apis_guru(conn, client, seen_domains)
            pub_i, pub_s = await ingest_public_apis(conn, client, seen_domains)
            post_i, post_s = await ingest_postman(conn, client, seen_domains)

            after = await conn.fetchval("SELECT COUNT(*) FROM services")

    await pool.close()

    results = {
        "apis_guru_inserted": guru_i,
        "apis_guru_skipped": guru_s,
        "public_apis_inserted": pub_i,
        "public_apis_skipped": pub_s,
        "postman_inserted": 0,
        "total_before": int(before),
        "total_after": int(after),
        "net_added": int(after) - int(before),
    }
    print(
        f"\nCatalog expansion complete:"
        f"\n  APIs.guru:    +{guru_i} new  ({guru_s} skipped/dup)"
        f"\n  public-apis:  +{pub_i} new  ({pub_s} skipped/dup)"
        f"\n  Postman:      skipped (auth-gated)"
        f"\n  Total: {before} → {after} (+{int(after) - int(before)})"
    )
    return results


if __name__ == "__main__":
    asyncio.run(run_catalog_expansion(_ASYNCPG_URL))
