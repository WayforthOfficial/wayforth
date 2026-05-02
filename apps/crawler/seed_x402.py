"""
x402 catalog update — run via: railway run python apps/crawler/seed_x402.py
  1. Delete hallucinated 'OpenAI via x402' entry
  2. Set x402_supported=true for known x402 services already in catalog
  3. Insert 8 new verified x402 services
  4. Print verification counts
"""
import asyncio, asyncpg, os

X402_NEW_SERVICES = [
    {
        "name": "Firecrawl",
        "description": "Web scraping API that turns websites into LLM-ready data. Pay-per-call via x402 on Base. No API key needed.",
        "endpoint_url": "https://api.firecrawl.dev",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Pinata IPFS",
        "description": "Account-free IPFS uploads and retrievals using x402 crypto payments on Base. No account or API key needed.",
        "endpoint_url": "https://api.pinata.cloud",
        "category": "data",  # 'storage' is not in category constraint; data is the closest
        "pricing_usdc": 0.001,
    },
    {
        "name": "Neynar",
        "description": "Farcaster social data API for agents. Get cast info, user data, and social graph. Pay via x402.",
        "endpoint_url": "https://api.neynar.com",
        "category": "social",
        "pricing_usdc": 0.001,
    },
    {
        "name": "AsterPay Data API",
        "description": "13 pay-per-call endpoints for market data, sentiment analysis, DeFi analytics. $0.001 USDC per call via x402 on Base mainnet. No API keys needed.",
        "endpoint_url": "https://asterpay.io/api",
        "category": "analytics",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Minifetch",
        "description": "Fetch rich structured metadata and clean token-efficient content summaries from web pages. Pay-as-you-go via x402 micropayments.",
        "endpoint_url": "https://minifetch.io",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    {
        "name": "dTelecom Speech-to-Text",
        "description": "Production-grade real-time speech-to-text for AI agents. VAD, noise reduction, 99+ languages, dual-engine architecture. Usage-based billing via x402.",
        "endpoint_url": "https://api.dtelecom.org",
        "category": "audio",
        "pricing_usdc": 0.002,
    },
    {
        "name": "Zyte API",
        "description": "Unified web scraping API for unblocking, browser rendering and extraction. Pay via x402.",
        "endpoint_url": "https://api.zyte.com",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Einstein AI",
        "description": "Blockchain intelligence API. Whale tracking, smart money signals, DEX analytics, MEV detection via USDC micropayments on Base.",
        "endpoint_url": "https://einstein-ai.io/api",
        "category": "analytics",
        "pricing_usdc": 0.002,
    },
]

X402_EXISTING_NAMES = [
    "Venice AI",
    "Hyperbolic GPU Inference",
    "Social Intel MCP",
    "carbon-cashmere-mcp",
]


async def main():
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    db = await asyncpg.connect(db_url)

    # ── TASK 1: Delete hallucinated entry ─────────────────────────────────────
    deleted = await db.execute("DELETE FROM services WHERE name = 'OpenAI via x402'")
    print(f"TASK 1 — Delete 'OpenAI via x402': {deleted}")

    # ── TASK 2: Set x402_supported=true for existing services ─────────────────
    updated = await db.execute(
        """UPDATE services
           SET x402_supported = true, coverage_tier = 2
           WHERE name = ANY($1::text[])""",
        X402_EXISTING_NAMES,
    )
    print(f"TASK 2 — Updated existing x402 services: {updated}")

    # Check which names were actually found
    found = await db.fetch(
        "SELECT name, x402_supported, coverage_tier FROM services WHERE name = ANY($1::text[])",
        X402_EXISTING_NAMES,
    )
    if found:
        for row in found:
            print(f"  ✓ {row['name']} — x402={row['x402_supported']} tier={row['coverage_tier']}")
    else:
        print("  (none of those names found in catalog)")

    # ── TASK 3: Insert new verified x402 services ─────────────────────────────
    print("\nTASK 3 — Inserting new x402 services:")
    added = skipped = 0
    for svc in X402_NEW_SERVICES:
        existing = await db.fetchval(
            "SELECT id FROM services WHERE endpoint_url = $1", svc["endpoint_url"]
        )
        if existing:
            # Upsert: update x402 flags on existing entry
            await db.execute(
                """UPDATE services
                   SET x402_supported = true, coverage_tier = 2,
                       name = $2, description = $3
                   WHERE endpoint_url = $1""",
                svc["endpoint_url"], svc["name"], svc["description"],
            )
            print(f"  Updated (existed): {svc['name']}")
            skipped += 1
        else:
            await db.execute(
                """INSERT INTO services
                   (name, description, endpoint_url, category, pricing_usdc,
                    payment_protocol, x402_supported, coverage_tier, source, created_at)
                   VALUES ($1, $2, $3, $4, $5, 'x402', true, 2, 'x402_verified', NOW())""",
                svc["name"], svc["description"], svc["endpoint_url"],
                svc["category"], svc["pricing_usdc"],
            )
            print(f"  Added:   {svc['name']}")
            added += 1

    print(f"  → {added} inserted, {skipped} already existed (updated)")

    # ── TASK 4: Verify ────────────────────────────────────────────────────────
    print("\nTASK 4 — Verification:")
    total_x402 = await db.fetchval("SELECT COUNT(*) FROM services WHERE x402_supported = true")
    print(f"  Total x402_supported=true: {total_x402}")

    rows = await db.fetch(
        """SELECT name, category, coverage_tier, x402_supported
           FROM services WHERE x402_supported = true
           ORDER BY name"""
    )
    print(f"\n  {'Name':<40} {'Category':<15} {'Tier':<6} x402")
    print(f"  {'-'*40} {'-'*15} {'-'*6} ----")
    for r in rows:
        print(f"  {r['name']:<40} {r['category']:<15} {r['coverage_tier']:<6} {r['x402_supported']}")

    await db.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
