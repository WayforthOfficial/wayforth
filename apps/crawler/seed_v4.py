"""
Wayforth Catalog Seed v4 — Session 62 batch of 43 curated enterprise-grade services.
Run once: DATABASE_URL=... python seed_v4.py
"""
import asyncio, asyncpg, os

SERVICES = [
    # Blockchain & Crypto Data
    {
        "name": "Etherscan API",
        "description": "Ethereum blockchain data — transactions, balances, contract ABIs, gas prices. The definitive Ethereum explorer API.",
        "endpoint_url": "https://api.etherscan.io/api",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Alchemy API",
        "description": "Enhanced blockchain APIs for Ethereum, Polygon, Solana. NFT data, transaction history, gas optimization.",
        "endpoint_url": "https://eth-mainnet.g.alchemy.com/v2",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "x402",
    },
    {
        "name": "QuickNode API",
        "description": "Fast blockchain node infrastructure. Ethereum, Solana, Polygon, BNB Chain. Sub-100ms response times.",
        "endpoint_url": "https://api.quicknode.com/v1",
        "category": "data",
        "pricing_usdc": 0.0001,
        "payment_protocol": "x402",
    },
    {
        "name": "Moralis API",
        "description": "Cross-chain web3 data — NFTs, DeFi, token prices, wallet history across 20+ blockchains.",
        "endpoint_url": "https://deep-index.moralis.io/api/v2.2",
        "category": "data",
        "pricing_usdc": 0.0001,
        "payment_protocol": "wayforth",
    },
    {
        "name": "CoinMarketCap API",
        "description": "Cryptocurrency market data, prices, and rankings for 20,000+ coins. The industry standard for crypto data.",
        "endpoint_url": "https://pro-api.coinmarketcap.com/v1",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    # Financial Data
    {
        "name": "Yahoo Finance API",
        "description": "Stock quotes, financial statements, earnings data, and market news. Broad market coverage.",
        "endpoint_url": "https://query1.finance.yahoo.com/v8",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "FRED API",
        "description": "Federal Reserve Economic Data — 800,000+ US and international economic time series. Free from St. Louis Fed.",
        "endpoint_url": "https://api.stlouisfed.org/fred",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Quandl/Nasdaq Data",
        "description": "Financial, economic, and alternative data. 30+ million datasets from hundreds of publishers.",
        "endpoint_url": "https://data.nasdaq.com/api/v3",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    # Government & Public Data
    {
        "name": "US Census Bureau API",
        "description": "US demographic data, population statistics, economic data. Free government API.",
        "endpoint_url": "https://api.census.gov/data",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "NASA API",
        "description": "Space imagery, asteroid data, Mars photos, Earth satellite images, astronomy picture of the day.",
        "endpoint_url": "https://api.nasa.gov",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Open Library API",
        "description": "10 million+ books — metadata, covers, full text for public domain works. Internet Archive.",
        "endpoint_url": "https://openlibrary.org/api",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "PubMed API",
        "description": "30+ million biomedical literature citations and abstracts. Free NIH/NCBI API.",
        "endpoint_url": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    # Social & Content
    {
        "name": "Reddit API",
        "description": "Reddit posts, comments, subreddits, and user data. Best for agents monitoring community discussions.",
        "endpoint_url": "https://oauth.reddit.com/api/v1",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Twitter/X API",
        "description": "Tweets, user profiles, trending topics. Best for agents monitoring real-time public discourse.",
        "endpoint_url": "https://api.twitter.com/2",
        "category": "data",
        "pricing_usdc": 0.001,
        "payment_protocol": "wayforth",
    },
    {
        "name": "GitHub API",
        "description": "Repositories, issues, PRs, commits, users. Best for agents working with code and open source.",
        "endpoint_url": "https://api.github.com",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Wikipedia API",
        "description": "Full Wikipedia content, search, and summaries. Free, no auth required. Best for knowledge retrieval.",
        "endpoint_url": "https://en.wikipedia.org/api/rest_v1",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Hacker News API",
        "description": "Top stories, comments, jobs, and Ask HN posts. Real-time tech community data.",
        "endpoint_url": "https://hacker-news.firebaseio.com/v0",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    # Communication & Productivity
    {
        "name": "Slack API",
        "description": "Send messages, read channels, manage workspaces. Best for agents integrated into team communications.",
        "endpoint_url": "https://slack.com/api",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Discord API",
        "description": "Send messages, manage servers, read channels. Best for agents in community management workflows.",
        "endpoint_url": "https://discord.com/api/v10",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Zoom API",
        "description": "Create meetings, manage participants, get recordings. Best for agents handling scheduling.",
        "endpoint_url": "https://api.zoom.us/v2",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Google Calendar API",
        "description": "Read and write calendar events, manage schedules. Best for agents handling time management.",
        "endpoint_url": "https://www.googleapis.com/calendar/v3",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Gmail API",
        "description": "Read and send emails, manage labels and threads. Best for agents handling email workflows.",
        "endpoint_url": "https://gmail.googleapis.com/gmail/v1",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    # E-commerce & Business
    {
        "name": "Shopify API",
        "description": "Products, orders, customers, inventory. Best for agents automating e-commerce operations.",
        "endpoint_url": "https://shopify.dev/api/admin-rest",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Amazon Product API",
        "description": "Product search, pricing, reviews, and availability. Best for agents doing market research.",
        "endpoint_url": "https://webservices.amazon.com/paapi5",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Salesforce API",
        "description": "CRM data — leads, opportunities, contacts, accounts. Best for sales automation agents.",
        "endpoint_url": "https://login.salesforce.com/services/data/v58.0",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "HubSpot API",
        "description": "Marketing, sales, and CRM data. Contacts, deals, companies, and email campaigns.",
        "endpoint_url": "https://api.hubapi.com",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    # Developer & Code
    {
        "name": "npm Registry API",
        "description": "Package metadata, downloads, versions for 2M+ npm packages. No auth required.",
        "endpoint_url": "https://registry.npmjs.org",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "PyPI API",
        "description": "Package metadata and release history for Python packages. Free, no auth required.",
        "endpoint_url": "https://pypi.org/pypi",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Stack Overflow API",
        "description": "Questions, answers, users, tags. Best for agents helping with technical problems.",
        "endpoint_url": "https://api.stackexchange.com/2.3",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Docker Hub API",
        "description": "Container images, tags, and metadata. Best for agents managing deployments.",
        "endpoint_url": "https://hub.docker.com/v2",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    # Health & Science
    {
        "name": "OpenFDA API",
        "description": "FDA drug, device, and food data. Adverse events, recalls, labeling. Free government API.",
        "endpoint_url": "https://api.fda.gov",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "RxNorm API",
        "description": "Drug name normalization and interaction data from NIH. Essential for healthcare agents.",
        "endpoint_url": "https://rxnav.nlm.nih.gov/REST",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    # Sports & Entertainment
    {
        "name": "The Movie Database API",
        "description": "Movies, TV shows, actors, ratings for 900K+ titles. Best for entertainment recommendation agents.",
        "endpoint_url": "https://api.themoviedb.org/3",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Spotify Web API",
        "description": "Music metadata, playlists, artist info, and audio features for 80M+ tracks.",
        "endpoint_url": "https://api.spotify.com/v1",
        "category": "data",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
    # Image Generation
    {
        "name": "OpenAI DALL-E API",
        "description": "Text-to-image generation. DALL-E 3 produces high-quality images from text descriptions.",
        "endpoint_url": "https://api.openai.com/v1/images/generations",
        "category": "image",
        "pricing_usdc": 0.00004,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Midjourney API",
        "description": "Artistic image generation. Best-in-class aesthetic quality for creative agent workflows.",
        "endpoint_url": "https://api.midjourney.com/v1",
        "category": "image",
        "pricing_usdc": 0.0001,
        "payment_protocol": "wayforth",
    },
    # Additional Inference
    {
        "name": "Mistral Mixtral API",
        "description": "Mixtral 8x22B — best open-source mixture-of-experts model. Fast, cost-effective, multilingual.",
        "endpoint_url": "https://api.mistral.ai/v1/chat/completions",
        "category": "inference",
        "pricing_usdc": 0.000002,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Meta Llama API",
        "description": "Official Meta Llama 3 API. Best open-weight model for general-purpose agent tasks.",
        "endpoint_url": "https://api.llama-api.com/chat/completions",
        "category": "inference",
        "pricing_usdc": 0.0000009,
        "payment_protocol": "wayforth",
    },
    {
        "name": "xAI Grok API",
        "description": "Grok-1 and Grok-2 from xAI. Real-time web access, strong coding and reasoning capabilities.",
        "endpoint_url": "https://api.x.ai/v1",
        "category": "inference",
        "pricing_usdc": 0.000005,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Mistral Codestral",
        "description": "Code-specialized model from Mistral. 80+ programming languages, FIM completion support.",
        "endpoint_url": "https://codestral.mistral.ai/v1",
        "category": "inference",
        "pricing_usdc": 0.000001,
        "payment_protocol": "wayforth",
    },
    {
        "name": "AI21 Labs API",
        "description": "Jamba and Jurassic models. Long context (256K), structured generation, enterprise-grade reliability.",
        "endpoint_url": "https://api.ai21.com/studio/v1",
        "category": "inference",
        "pricing_usdc": 0.000002,
        "payment_protocol": "wayforth",
    },
    # Embeddings
    {
        "name": "Voyage AI Embeddings",
        "description": "State-of-the-art retrieval embeddings. voyage-3 outperforms OpenAI on MTEB. Best for RAG.",
        "endpoint_url": "https://api.voyageai.com/v1/embeddings",
        "category": "embeddings",
        "pricing_usdc": 0.00000012,
        "payment_protocol": "wayforth",
    },
    {
        "name": "Nomic Embed API",
        "description": "Open-source embeddings competitive with OpenAI. 8192 token context. Local and API available.",
        "endpoint_url": "https://api-atlas.nomic.ai/v1/embedding/text",
        "category": "embeddings",
        "pricing_usdc": 0.0,
        "payment_protocol": "wayforth",
    },
]

# Well-known, reliably-up public APIs to auto-promote to Tier 2
PROMOTE_URLS = [
    "https://api.etherscan.io/api",
    "https://eth-mainnet.g.alchemy.com/v2",
    "https://deep-index.moralis.io/api/v2.2",
    "https://pro-api.coinmarketcap.com/v1",
    "https://query1.finance.yahoo.com/v8",
    "https://api.stlouisfed.org/fred",
    "https://data.nasdaq.com/api/v3",
    "https://api.census.gov/data",
    "https://api.nasa.gov",
    "https://openlibrary.org/api",
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
    "https://oauth.reddit.com/api/v1",
    "https://api.twitter.com/2",
    "https://api.github.com",
    "https://en.wikipedia.org/api/rest_v1",
    "https://hacker-news.firebaseio.com/v0",
    "https://slack.com/api",
    "https://discord.com/api/v10",
    "https://api.zoom.us/v2",
    "https://www.googleapis.com/calendar/v3",
    "https://gmail.googleapis.com/gmail/v1",
    "https://api.hubapi.com",
    "https://registry.npmjs.org",
    "https://pypi.org/pypi",
    "https://api.stackexchange.com/2.3",
    "https://hub.docker.com/v2",
    "https://api.fda.gov",
    "https://rxnav.nlm.nih.gov/REST",
    "https://api.themoviedb.org/3",
    "https://api.spotify.com/v1",
    "https://api.openai.com/v1/images/generations",
    "https://api.mistral.ai/v1/chat/completions",
    "https://api.llama-api.com/chat/completions",
    "https://api.x.ai/v1",
    "https://codestral.mistral.ai/v1",
    "https://api.ai21.com/studio/v1",
    "https://api.voyageai.com/v1/embeddings",
    "https://api-atlas.nomic.ai/v1/embedding/text",
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
                   VALUES ($1, $2, $3, $4, $5, $6, 0, 'curated_v4', NOW())""",
                svc["name"], svc["description"], svc["endpoint_url"],
                svc["category"], svc["pricing_usdc"], svc["payment_protocol"],
            )
            print(f"  Added:   {svc['name']}")
            added += 1

    print(f"\nPromoting {len(PROMOTE_URLS)} services to Tier 2...")
    promoted = 0
    for url in PROMOTE_URLS:
        result = await db.execute(
            """UPDATE services
               SET coverage_tier = 2, consecutive_failures = 0
               WHERE endpoint_url = $1 AND coverage_tier < 2""",
            url,
        )
        if result != "UPDATE 0":
            name = await db.fetchval("SELECT name FROM services WHERE endpoint_url = $1", url)
            print(f"  Promoted: {name}")
            promoted += 1
        else:
            name = await db.fetchval("SELECT name FROM services WHERE endpoint_url = $1", url)
            print(f"  Already Tier 2: {name}")

    tier2_count = await db.fetchval(
        "SELECT COUNT(*) FROM services WHERE coverage_tier >= 2"
    )
    total = await db.fetchval("SELECT COUNT(*) FROM services")
    await db.close()

    print(f"\nDone. Added {added}, updated {updated}, newly promoted {promoted}.")
    print(f"Total services: {total} | Tier 2+: {tier2_count}")


asyncio.run(seed())
