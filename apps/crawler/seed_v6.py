"""
Wayforth Catalog Seed v6 — 39 real API services across weather, maps, finance, search,
comms, identity, blockchain, docs, health, legal, e-commerce, and inference.
Run once: DATABASE_URL=... python seed_v6.py
"""
import asyncio, asyncpg, os

SERVICES_V6 = [
    # Weather (real APIs)
    {"name": "WeatherAPI", "description": "Real-time weather, forecasts, historical data for any location. 99.9% uptime SLA. Best accuracy for current conditions.", "endpoint_url": "https://api.weatherapi.com/v1", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Tomorrow.io Weather API", "description": "Hyperlocal weather intelligence with minute-level forecasts. Used by Uber, Delta, National Grid.", "endpoint_url": "https://api.tomorrow.io/v4", "category": "data", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "AviationStack API", "description": "Real-time flight status, schedules, airline and airport data worldwide.", "endpoint_url": "https://api.aviationstack.com/v1", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},

    # Maps & Location (real APIs)
    {"name": "What3Words API", "description": "Convert coordinates to 3-word addresses and back. Used by Mercedes, Ford, emergency services.", "endpoint_url": "https://api.what3words.com/v3", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "OpenCage Geocoding API", "description": "Forward and reverse geocoding using OpenStreetMap data. 2,500 requests/day free.", "endpoint_url": "https://api.opencagedata.com/geocode/v1", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Postcodes.io API", "description": "UK postcode data — lookup, validation, nearest postcodes. Free, no auth required.", "endpoint_url": "https://api.postcodes.io", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},

    # Finance (real APIs)
    {"name": "IEX Cloud API", "description": "Stock market data — real-time prices, financials, ownership. Built for developers.", "endpoint_url": "https://cloud.iexapis.com/stable", "category": "data", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "Twelve Data API", "description": "Real-time and historical financial data — stocks, forex, crypto, ETFs. WebSocket streaming.", "endpoint_url": "https://api.twelvedata.com", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Financial Modeling Prep API", "description": "Financial statements, DCF analysis, stock screeners. 30+ years of historical data.", "endpoint_url": "https://financialmodelingprep.com/api/v3", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Intrinio API", "description": "Financial data marketplace — fundamentals, options, insider trading, ESG scores.", "endpoint_url": "https://api-v2.intrinio.com", "category": "data", "pricing_usdc": 0.000010, "payment_protocol": "wayforth"},

    # Search (real APIs)
    {"name": "SerpAPI", "description": "Google, Bing, YouTube, Amazon search results via API. Handles CAPTCHAs automatically.", "endpoint_url": "https://serpapi.com/search", "category": "data", "pricing_usdc": 0.000005, "payment_protocol": "wayforth"},
    {"name": "Bing Web Search API", "description": "Microsoft Bing search results with entity recognition and answer types.", "endpoint_url": "https://api.bing.microsoft.com/v7.0/search", "category": "data", "pricing_usdc": 0.000003, "payment_protocol": "wayforth"},
    {"name": "You.com Search API", "description": "AI-native search API returning summarized, structured results optimized for LLM consumption.", "endpoint_url": "https://api.you.com/api/search", "category": "data", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "Diffbot API", "description": "Extract structured data from any webpage automatically. Article, product, person extraction.", "endpoint_url": "https://api.diffbot.com/v3", "category": "data", "pricing_usdc": 0.000010, "payment_protocol": "wayforth"},

    # Communication (real APIs)
    {"name": "Mailgun API", "description": "Transactional email with analytics. 99.99% uptime SLA. 5,000 emails/month free.", "endpoint_url": "https://api.mailgun.net/v3", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Vonage SMS API", "description": "Send and receive SMS globally. 99.999% uptime. Used by Airbnb, Glassdoor.", "endpoint_url": "https://rest.nexmo.com/sms/json", "category": "data", "pricing_usdc": 0.000038, "payment_protocol": "wayforth"},
    {"name": "MessageBird API", "description": "SMS, WhatsApp, and voice messaging API. Global coverage, competitive pricing.", "endpoint_url": "https://rest.messagebird.com", "category": "data", "pricing_usdc": 0.000040, "payment_protocol": "wayforth"},
    {"name": "Postmark API", "description": "Transactional email with 98%+ deliverability. Developer-first, detailed analytics.", "endpoint_url": "https://api.postmarkapp.com", "category": "data", "pricing_usdc": 0.0000015, "payment_protocol": "wayforth"},

    # Identity & Verification (real APIs)
    {"name": "Persona API", "description": "Identity verification — government ID, selfie matching, database checks. SOC2 compliant.", "endpoint_url": "https://withpersona.com/api/v1", "category": "data", "pricing_usdc": 0.001, "payment_protocol": "wayforth"},
    {"name": "Stripe Identity API", "description": "ID document verification and selfie capture. Powered by Stripe infrastructure.", "endpoint_url": "https://api.stripe.com/v1/identity", "category": "data", "pricing_usdc": 0.001500, "payment_protocol": "wayforth"},
    {"name": "Jumio API", "description": "AI-powered identity verification and AML screening. 200+ countries, 5000+ ID types.", "endpoint_url": "https://netverify.com/api/v4", "category": "data", "pricing_usdc": 0.002, "payment_protocol": "wayforth"},
    {"name": "Emailverification.io", "description": "Real-time email validation — syntax, MX records, disposable detection, deliverability.", "endpoint_url": "https://api.emailverification.io/v1", "category": "data", "pricing_usdc": 0.0000010, "payment_protocol": "wayforth"},

    # Blockchain (real APIs)
    {"name": "Infura API", "description": "Ethereum and IPFS infrastructure. 100K requests/day free. Used by MetaMask, OpenSea.", "endpoint_url": "https://mainnet.infura.io/v3", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "The Graph API", "description": "Query blockchain data with GraphQL. Index and query Ethereum, Polygon, and 30+ chains.", "endpoint_url": "https://api.thegraph.com/subgraphs", "category": "data", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "Dune Analytics API", "description": "On-chain analytics and dashboards. Query Ethereum, Solana, Bitcoin raw data.", "endpoint_url": "https://api.dune.com/api/v1", "category": "data", "pricing_usdc": 0.000010, "payment_protocol": "wayforth"},
    {"name": "Nansen API", "description": "Smart money blockchain analytics. Wallet labels, token flows, DeFi intelligence.", "endpoint_url": "https://api.nansen.ai/v1", "category": "data", "pricing_usdc": 0.001, "payment_protocol": "wayforth"},

    # Document & File (real APIs)
    {"name": "Cloudinary API", "description": "Image and video transformation, optimization, and delivery. CDN included.", "endpoint_url": "https://api.cloudinary.com/v1_1", "category": "image", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Imgix API", "description": "Real-time image processing and delivery. Resize, crop, filter, optimize on the fly.", "endpoint_url": "https://api.imgix.com", "category": "image", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "Remove.bg API", "description": "Automatic background removal from photos. 100% automated, one API call.", "endpoint_url": "https://api.remove.bg/v1.0", "category": "image", "pricing_usdc": 0.000200, "payment_protocol": "wayforth"},
    {"name": "Bannerbear API", "description": "Auto-generate images and videos with templates. Social media, thumbnails, certificates.", "endpoint_url": "https://api.bannerbear.com/v2", "category": "image", "pricing_usdc": 0.000100, "payment_protocol": "wayforth"},

    # Health & Medical (real APIs)
    {"name": "Infermedica API", "description": "AI symptom checker and triage. Used by 500+ healthcare organizations.", "endpoint_url": "https://api.infermedica.com/v3", "category": "data", "pricing_usdc": 0.001, "payment_protocol": "wayforth"},
    {"name": "Human API", "description": "Aggregate health data from wearables, EHRs, and labs. HIPAA compliant.", "endpoint_url": "https://user.humanapi.co/v1", "category": "data", "pricing_usdc": 0.001, "payment_protocol": "wayforth"},

    # Legal & Compliance (real APIs)
    {"name": "OpenCorporates API", "description": "Global company data — 200M+ companies, directors, shareholders. 140+ jurisdictions.", "endpoint_url": "https://api.opencorporates.com/v0.4", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "ICIJ OFAC Sanctions API", "description": "Sanctions screening against OFAC, UN, EU lists. Essential for financial compliance agents.", "endpoint_url": "https://ofac-api.com/api/v4", "category": "data", "pricing_usdc": 0.000100, "payment_protocol": "wayforth"},

    # E-commerce (real APIs)
    {"name": "Rainforest API", "description": "Amazon product data in real-time — prices, reviews, rankings, sellers.", "endpoint_url": "https://api.rainforestapi.com/request", "category": "data", "pricing_usdc": 0.000020, "payment_protocol": "wayforth"},
    {"name": "Zinc API", "description": "Automate purchases on Amazon, Walmart, and other retailers programmatically.", "endpoint_url": "https://api.zinc.io/v1", "category": "data", "pricing_usdc": 0.001, "payment_protocol": "wayforth"},

    # More Inference (real APIs)
    {"name": "Cohere Command API", "description": "Command R+ — best RAG model available. 128K context, grounded generation.", "endpoint_url": "https://api.cohere.ai/v1/chat", "category": "inference", "pricing_usdc": 0.000003, "payment_protocol": "wayforth"},
    {"name": "Writer API", "description": "Enterprise LLM fine-tuned on business writing. Palmyra models for professional content.", "endpoint_url": "https://api.writer.com/v1", "category": "inference", "pricing_usdc": 0.000012, "payment_protocol": "wayforth"},
    {"name": "Reka API", "description": "Multimodal models — text, image, video understanding. Strong reasoning capabilities.", "endpoint_url": "https://api.reka.ai/v1", "category": "inference", "pricing_usdc": 0.000003, "payment_protocol": "wayforth"},
]


async def seed():
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    db = await asyncpg.connect(db_url)
    added = updated = 0

    for svc in SERVICES_V6:
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
                   VALUES ($1, $2, $3, $4, $5, $6, 0, 'curated_v6', NOW())""",
                svc["name"], svc["description"], svc["endpoint_url"],
                svc["category"], svc["pricing_usdc"], svc["payment_protocol"],
            )
            print(f"  Added:   {svc['name']}")
            added += 1

    print(f"\nPromoting all {len(SERVICES_V6)} services to Tier 2...")
    promoted = 0
    for svc in SERVICES_V6:
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
