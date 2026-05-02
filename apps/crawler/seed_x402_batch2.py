"""
x402 catalog batch 2 — 30 verified services from x402.org/ecosystem
Run via: railway run python apps/crawler/seed_x402_batch2.py
"""
import asyncio, asyncpg, os

SERVICES = [
    {
        "name": "AdEx AURA API",
        "description": "x402 micropayments API for portfolio data, tokens, DeFi positions, yield strategies, and transaction payloads on Base.",
        "endpoint_url": "https://api.adex.network",
        "category": "analytics",
        "pricing_usdc": 0.001,
    },
    {
        "name": "AdPrompt x402 API",
        "description": "Pay-per-request advertising and marketing APIs via x402. Brand analysis, marketing strategy, ad creatives, copy variants. Returns 402 with machine-readable offer.",
        "endpoint_url": "https://adprompt.ai/api",
        "category": "data",
        "pricing_usdc": 0.002,
    },
    {
        "name": "AEON",
        "description": "Omnichain settlement layer enabling AI agents to pay real-world merchants across SEA, LATAM, and Africa via x402 and USDC.",
        "endpoint_url": "https://aeon.xyz/api",
        "category": "payments",
        "pricing_usdc": 0.001,
    },
    {
        "name": "AiMo Network",
        "description": "Permissionless pay-per-inference API via x402. Connects humans, AI agents, and service providers without censorship or borders.",
        "endpoint_url": "https://aimonetwork.com/api",
        "category": "inference",
        "pricing_usdc": 0.001,
    },
    {
        "name": "AIsa Marketplace",
        "description": "Resource marketplace aggregating LLMs and data APIs based on HTTP 402 standard. Pay per request.",
        "endpoint_url": "https://aisa.ai/api",
        "category": "inference",
        "pricing_usdc": 0.001,
    },
    {
        "name": "auor.io Research Toolkit",
        "description": "x402 AI agent research toolkit. Access multiple APIs through x402 payment protocol. No accounts, no subscription, just USDC.",
        "endpoint_url": "https://auor.io/api",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    {
        "name": "AurraCloud",
        "description": "AI agents hosting and tooling platform with MCP, smart wallets, OpenAI API compatibility and x402 support.",
        "endpoint_url": "https://aurracloud.com/api",
        "category": "inference",
        "pricing_usdc": 0.002,
    },
    {
        "name": "BlackSwan Risk Intelligence",
        "description": "Real-time risk intelligence infrastructure for autonomous AI agents. Pay via x402.",
        "endpoint_url": "https://blackswan.ai/api",
        "category": "analytics",
        "pricing_usdc": 0.002,
    },
    {
        "name": "BlockRun.AI LLM Gateway",
        "description": "Pay-as-you-go AI gateway providing ChatGPT and all major LLMs (Anthropic, Google, DeepSeek, xAI) via x402 on Base.",
        "endpoint_url": "https://blockrun.ai/api",
        "category": "inference",
        "pricing_usdc": 0.002,
    },
    {
        "name": "ClawdVine",
        "description": "Short-form video network for AI agents. Pay per request via x402.",
        "endpoint_url": "https://clawdvine.com/api",
        "category": "social",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Cybercentry Security API",
        "description": "AI-powered cybersecurity endpoints via x402. Compliance, intelligence, and protection pillars. Pay per request.",
        "endpoint_url": "https://cybercentry.com/api",
        "category": "devops",
        "pricing_usdc": 0.002,
    },
    {
        "name": "Elsa DeFi API",
        "description": "DeFi API endpoints via x402 micropayments. Portfolio data, token prices, swap quotes, wallet analytics, yield suggestions. Pay per request with USDC on Base.",
        "endpoint_url": "https://elsa.finance/api",
        "category": "analytics",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Gloria AI News API",
        "description": "Real-time, high-signal, customizable news data for AI agents. Structured signals for any use case. Pay via x402.",
        "endpoint_url": "https://gloria.ai/api",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Grove API",
        "description": "Unified API you can fund using x402 to tip anyone on the internet. Pay per request.",
        "endpoint_url": "https://grove.town/api",
        "category": "payments",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Moltalyzer",
        "description": "Environmental awareness API for AI agents. Hourly digests of trending topics, sentiment, and emerging narratives. Pay via x402.",
        "endpoint_url": "https://moltbook.com/api",
        "category": "analytics",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Onchain x402 Layer",
        "description": "x402 intelligent intermediary layer for aggregating facilitators. Pay per request on Base.",
        "endpoint_url": "https://onchain.xyz/api",
        "category": "payments",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Otto AI Crypto Intelligence",
        "description": "AI-powered crypto intelligence for agents. Real-time crypto news, token analysis, market alpha signals via USDC on Base.",
        "endpoint_url": "https://otto.ai/api",
        "category": "analytics",
        "pricing_usdc": 0.002,
    },
    {
        "name": "Proofivy Attestation",
        "description": "Attestation and x402 paywalled publishing. Publish attestations by paying USDC on Base chain via API.",
        "endpoint_url": "https://proofivy.com/api",
        "category": "identity",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Questflow",
        "description": "Orchestration layer for the multi-agent economy. Orchestrate multiple AI agents to research, take action and earn rewards on-chain.",
        "endpoint_url": "https://questflow.ai/api",
        "category": "inference",
        "pricing_usdc": 0.002,
    },
    {
        "name": "QuickSilver",
        "description": "Bridge between physical systems and AI. Real-world applications with perception, reasoning, and adaptability via x402.",
        "endpoint_url": "https://quicksilver.ai/api",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    {
        "name": "RelAI API Marketplace",
        "description": "Monetize and consume APIs with x402 micropayments across multiple networks. Pay per request without API keys.",
        "endpoint_url": "https://relai.dev/api",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Rencom x402 Search",
        "description": "Search for x402 resources. Ranks endpoints by historical agent outcomes to minimize execution failure. Sort by price, popularity or reliability.",
        "endpoint_url": "https://rencom.ai/api",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    {
        "name": "SerenAI x402 Gateway",
        "description": "Production payment gateway enabling AI agents to pay for database queries and API access using USDC on Base via x402.",
        "endpoint_url": "https://serenai.dev/api",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    {
        "name": "SLAMai Smart Money Intelligence",
        "description": "Best-in-class smart money intelligence data. Live on Base and Ethereum. Open APIs with MCP layer for autonomous agents. Pay via x402.",
        "endpoint_url": "https://slamai.com/api",
        "category": "analytics",
        "pricing_usdc": 0.002,
    },
    {
        "name": "Slinky Layer",
        "description": "Open market for APIs — turning any API into an on-chain x402 pay-per-use resource with ERC-8004 portable reputation.",
        "endpoint_url": "https://slinkylayer.com/api",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Snack Money API",
        "description": "Micropayment platform for X, Farcaster, baseapp and verifiable identities. Pay via x402.",
        "endpoint_url": "https://snackmoney.com/api",
        "category": "payments",
        "pricing_usdc": 0.001,
    },
    {
        "name": "SocioLogic RNG API",
        "description": "Cryptographically secure, verifiable randomness for AI agents, smart contracts, and applications. Pay-per-use entropy via x402.",
        "endpoint_url": "https://sociologic.ai/api",
        "category": "data",
        "pricing_usdc": 0.001,
    },
    {
        "name": "tip.md",
        "description": "Crypto tipping service enabling AI assistants to send cryptocurrency tips to content creators. USDC tips via MCP powered by x402.",
        "endpoint_url": "https://tip.md/api",
        "category": "payments",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Trusta.AI Attestation",
        "description": "Publish attestations by paying USDC on Base chain via x402 API interface. No ETH or wallet interaction needed.",
        "endpoint_url": "https://trusta.network/api",
        "category": "identity",
        "pricing_usdc": 0.001,
    },
    {
        "name": "Ubounty",
        "description": "AI agents and developers earn USDC by solving GitHub issues. Automated bounty creation, PR verification, instant x402 settlement.",
        "endpoint_url": "https://ubounty.dev/api",
        "category": "devops",
        "pricing_usdc": 0.001,
    },
]


async def main():
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    db = await asyncpg.connect(db_url)

    print(f"Inserting {len(SERVICES)} x402 services...\n")
    added = skipped = 0
    for svc in SERVICES:
        existing = await db.fetchval(
            "SELECT id FROM services WHERE name = $1 OR endpoint_url = $2",
            svc["name"], svc["endpoint_url"],
        )
        if existing:
            print(f"  SKIP (exists): {svc['name']}")
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
            print(f"  Added: {svc['name']}")
            added += 1

    print(f"\n→ {added} inserted, {skipped} skipped")

    total_x402 = await db.fetchval("SELECT COUNT(*) FROM services WHERE x402_supported = true")
    print(f"Total x402_supported=true: {total_x402}")

    await db.close()
    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
