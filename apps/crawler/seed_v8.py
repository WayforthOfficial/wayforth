"""
Wayforth Catalog Seed v8 — 90+ real API services across 10 new categories:
communication, location, identity, payments, productivity, devops, legal,
healthcare, real_estate, social, analytics (+ blockchain additions to data).
Run once: DATABASE_URL=... python seed_v8.py
"""
import asyncio, asyncpg, os

SERVICES_V8 = [
    # COMMUNICATION
    {"name": "Twilio SMS API", "description": "Send and receive SMS globally. 180+ countries, 99.95% uptime SLA. Most reliable SMS API.", "endpoint_url": "https://api.twilio.com/2010-04-01", "category": "communication", "pricing_usdc": 0.000075, "payment_protocol": "wayforth"},
    {"name": "SendGrid Email API", "description": "Transactional email at scale. 100B+ emails delivered per month. Deliverability analytics included.", "endpoint_url": "https://api.sendgrid.com/v3", "category": "communication", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "Mailgun API", "description": "Email API for developers. Inbound email parsing, tracking, analytics. 99.99% uptime.", "endpoint_url": "https://api.mailgun.net/v3", "category": "communication", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "Postmark API", "description": "Fastest transactional email delivery. Avg 5s delivery time. Detailed open and click tracking.", "endpoint_url": "https://api.postmarkapp.com", "category": "communication", "pricing_usdc": 0.0000015, "payment_protocol": "wayforth"},
    {"name": "Vonage SMS API", "description": "SMS, voice, and video APIs. Global coverage, programmable communications platform.", "endpoint_url": "https://rest.nexmo.com", "category": "communication", "pricing_usdc": 0.000063, "payment_protocol": "wayforth"},
    {"name": "Slack Web API", "description": "Post messages, manage channels, interact with Slack workspaces programmatically.", "endpoint_url": "https://slack.com/api", "category": "communication", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Discord API", "description": "Send messages, manage servers, bots, webhooks. 500M+ users.", "endpoint_url": "https://discord.com/api/v10", "category": "communication", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Telegram Bot API", "description": "Build Telegram bots. Send messages, files, inline keyboards. 900M+ active users.", "endpoint_url": "https://api.telegram.org", "category": "communication", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "WhatsApp Business API", "description": "Send WhatsApp messages via Meta's official Business API. 2B+ users globally.", "endpoint_url": "https://graph.facebook.com/v18.0", "category": "communication", "pricing_usdc": 0.000050, "payment_protocol": "wayforth"},
    {"name": "Resend API", "description": "Email API built for developers. React email templates, webhooks, 100% deliverability focus.", "endpoint_url": "https://api.resend.com", "category": "communication", "pricing_usdc": 0.0000004, "payment_protocol": "wayforth"},

    # MAPS & LOCATION
    {"name": "Google Maps API", "description": "Maps, geocoding, directions, places, distance matrix. Most comprehensive location API.", "endpoint_url": "https://maps.googleapis.com/maps/api", "category": "location", "pricing_usdc": 0.000005, "payment_protocol": "wayforth"},
    {"name": "Mapbox API", "description": "Custom maps, navigation, geocoding, search. Developer-first alternative to Google Maps.", "endpoint_url": "https://api.mapbox.com", "category": "location", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "HERE Maps API", "description": "Enterprise mapping, routing, geocoding. Strong in logistics and automotive use cases.", "endpoint_url": "https://router.hereapi.com/v8", "category": "location", "pricing_usdc": 0.000002, "payment_protocol": "wayforth"},
    {"name": "ipinfo.io API", "description": "IP geolocation, ASN, carrier, privacy detection. 50B+ monthly requests. Free tier.", "endpoint_url": "https://ipinfo.io", "category": "location", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "OpenCage Geocoding API", "description": "Forward and reverse geocoding. Worldwide coverage, multiple languages, free tier.", "endpoint_url": "https://api.opencagedata.com/geocode/v1", "category": "location", "pricing_usdc": 0.000001, "payment_protocol": "wayforth"},
    {"name": "Nominatim API", "description": "Free open-source geocoding from OpenStreetMap. No API key required. Worldwide coverage.", "endpoint_url": "https://nominatim.openstreetmap.org", "category": "location", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Positionstack API", "description": "Real-time geocoding. 25M+ locations. 3B+ geocoding requests per month.", "endpoint_url": "https://api.positionstack.com/v1", "category": "location", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},

    # IDENTITY & AUTH
    {"name": "Auth0 Management API", "description": "User authentication and authorization. Social login, MFA, SSO. 100M+ users managed.", "endpoint_url": "https://auth0.com/api/v2", "category": "identity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Clerk API", "description": "Drop-in auth for modern apps. User management, sessions, organizations. React-first.", "endpoint_url": "https://api.clerk.com/v1", "category": "identity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Okta API", "description": "Enterprise identity platform. SSO, MFA, lifecycle management, universal directory.", "endpoint_url": "https://api.okta.com/api/v1", "category": "identity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Supabase Auth API", "description": "Open-source Firebase alternative. Auth, database, storage, realtime. Free tier generous.", "endpoint_url": "https://supabase.com/dashboard/project", "category": "identity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Persona API", "description": "Identity verification — ID verification, selfie checks, database verifications. KYC/KYB.", "endpoint_url": "https://withpersona.com/api/v1", "category": "identity", "pricing_usdc": 0.001, "payment_protocol": "wayforth"},
    {"name": "Jumio API", "description": "AI-powered identity verification. Document verification, biometric authentication. Enterprise grade.", "endpoint_url": "https://netverify.com/api/v4", "category": "identity", "pricing_usdc": 0.002, "payment_protocol": "wayforth"},

    # E-COMMERCE & PAYMENTS
    {"name": "Stripe API", "description": "Payment processing, subscriptions, invoicing. 135+ currencies. Most developer-friendly payments API.", "endpoint_url": "https://api.stripe.com/v1", "category": "payments", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "PayPal REST API", "description": "Global payment processing. PayPal, cards, bank transfers. 435M+ active accounts.", "endpoint_url": "https://api.paypal.com/v2", "category": "payments", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Square API", "description": "Payments, POS, inventory, appointments. Strongest for in-person + online unified commerce.", "endpoint_url": "https://connect.squareup.com/v2", "category": "payments", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Shopify Admin API", "description": "Build Shopify apps. Products, orders, customers, inventory. 1.7M+ merchants.", "endpoint_url": "https://shopify.dev/api/admin-rest", "category": "payments", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "WooCommerce REST API", "description": "Manage WooCommerce stores. Products, orders, customers, reports. Open source.", "endpoint_url": "https://woocommerce.github.io/woocommerce-rest-api-docs", "category": "payments", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Plaid API", "description": "Bank account linking, balance checks, transaction history. 12,000+ financial institutions.", "endpoint_url": "https://production.plaid.com", "category": "payments", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},

    # PRODUCTIVITY & DOCS
    {"name": "Notion API", "description": "Read and write Notion pages, databases, blocks. Build Notion integrations and automations.", "endpoint_url": "https://api.notion.com/v1", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Airtable API", "description": "Read/write Airtable bases. Flexible database with spreadsheet UX. 300K+ businesses use it.", "endpoint_url": "https://api.airtable.com/v0", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Google Sheets API", "description": "Read and write Google Sheets programmatically. Real-time collaboration, formulas, charts.", "endpoint_url": "https://sheets.googleapis.com/v4", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Google Drive API", "description": "Upload, download, organize files in Google Drive. 1B+ users. Free tier generous.", "endpoint_url": "https://www.googleapis.com/drive/v3", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Dropbox API", "description": "File storage, sharing, sync. Upload/download files, manage folders, share links.", "endpoint_url": "https://api.dropboxapi.com/2", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Box API", "description": "Enterprise cloud storage. File management, collaboration, metadata, workflow automation.", "endpoint_url": "https://api.box.com/2.0", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Trello API", "description": "Project management boards, cards, lists. Kanban for teams. Atlassian owned.", "endpoint_url": "https://api.trello.com/1", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Asana API", "description": "Task and project management. Create tasks, assign work, track progress programmatically.", "endpoint_url": "https://app.asana.com/api/1.0", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Linear API", "description": "Issue tracking for software teams. Fast, keyboard-first. GraphQL API. Growing fast.", "endpoint_url": "https://api.linear.app/graphql", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Jira REST API", "description": "Atlassian's issue and project tracking. Most used in enterprise engineering teams.", "endpoint_url": "https://your-domain.atlassian.net/rest/api/3", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Confluence API", "description": "Team wiki and documentation platform by Atlassian. Create, read, update pages programmatically.", "endpoint_url": "https://your-domain.atlassian.net/wiki/rest/api", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Google Calendar API", "description": "Create, read, update calendar events. Scheduling, availability checking, reminders.", "endpoint_url": "https://www.googleapis.com/calendar/v3", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Calendly API", "description": "Scheduling automation. Read/create meetings, get availability, manage event types.", "endpoint_url": "https://api.calendly.com", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Zoom API", "description": "Create meetings, manage users, get recordings. 300M+ daily meeting participants.", "endpoint_url": "https://api.zoom.us/v2", "category": "productivity", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},

    # MONITORING & DEVOPS
    {"name": "Datadog API", "description": "Infrastructure monitoring, APM, logs, dashboards. Most comprehensive observability platform.", "endpoint_url": "https://api.datadoghq.com/api/v1", "category": "devops", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "PagerDuty API", "description": "Incident management and on-call scheduling. Alert routing, escalation, postmortems.", "endpoint_url": "https://api.pagerduty.com", "category": "devops", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Grafana API", "description": "Dashboards, alerts, data source management. Open source observability platform.", "endpoint_url": "https://grafana.com/api", "category": "devops", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "New Relic API", "description": "Full-stack observability. APM, infrastructure, browser, mobile, synthetics monitoring.", "endpoint_url": "https://api.newrelic.com/v2", "category": "devops", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Sentry API", "description": "Error tracking and performance monitoring. 4M+ developers, 90K+ organizations.", "endpoint_url": "https://sentry.io/api/0", "category": "devops", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "StatusPage API", "description": "Incident communication and status page management. Atlassian product.", "endpoint_url": "https://api.statuspage.io/v1", "category": "devops", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Vercel API", "description": "Deploy frontends, manage projects, domains, environment variables programmatically.", "endpoint_url": "https://api.vercel.com", "category": "devops", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Netlify API", "description": "Deploy sites, manage DNS, forms, functions. JAMstack hosting platform.", "endpoint_url": "https://api.netlify.com/api/v1", "category": "devops", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "CircleCI API", "description": "CI/CD pipeline management. Trigger builds, get artifacts, manage workflows.", "endpoint_url": "https://circleci.com/api/v2", "category": "devops", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "GitHub Actions API", "description": "Trigger and manage GitHub Actions workflows, artifacts, runners programmatically.", "endpoint_url": "https://api.github.com/repos", "category": "devops", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},

    # LEGAL & COMPLIANCE
    {"name": "Onfido API", "description": "Identity verification and background checks. Document + biometric verification. Global coverage.", "endpoint_url": "https://api.onfido.com/v3.6", "category": "legal", "pricing_usdc": 0.002, "payment_protocol": "wayforth"},
    {"name": "Stripe Identity API", "description": "Verify identities globally. Government ID, selfie check, address verification.", "endpoint_url": "https://api.stripe.com/v1/identity", "category": "legal", "pricing_usdc": 0.0015, "payment_protocol": "wayforth"},
    {"name": "OpenSanctions API", "description": "Sanctions lists, PEPs, criminal watchlists. AML/KYC compliance data. Open source.", "endpoint_url": "https://api.opensanctions.org", "category": "legal", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "ComplyAdvantage API", "description": "AML data — sanctions, PEPs, adverse media. Real-time financial crime risk detection.", "endpoint_url": "https://app.complyadvantage.com/api", "category": "legal", "pricing_usdc": 0.001, "payment_protocol": "wayforth"},
    {"name": "Docusign API", "description": "Electronic signature API. Send, sign, track documents. 1B+ transactions processed.", "endpoint_url": "https://na1.docusign.net/restapi/v2.1", "category": "legal", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "HelloSign API", "description": "eSignature and document workflow API. Dropbox owned. Simple REST API.", "endpoint_url": "https://api.hellosign.com/v3", "category": "legal", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},

    # HEALTHCARE
    {"name": "OpenFDA API", "description": "FDA data on drugs, devices, foods, adverse events. Free, no auth required.", "endpoint_url": "https://api.fda.gov", "category": "healthcare", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "ClinicalTrials.gov API", "description": "Search 400K+ clinical trials worldwide. Free, no auth. Real-time trial data from NIH.", "endpoint_url": "https://clinicaltrials.gov/api/v2", "category": "healthcare", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Infermedica API", "description": "Symptom checker and medical diagnosis AI. Triage, risk factors, interview engine.", "endpoint_url": "https://api.infermedica.com/v3", "category": "healthcare", "pricing_usdc": 0.001, "payment_protocol": "wayforth"},
    {"name": "Human API", "description": "Aggregate health data from 400+ sources — wearables, EHRs, labs, pharmacies.", "endpoint_url": "https://user.humanapi.co/v1", "category": "healthcare", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Veeva Vault API", "description": "Life sciences content management. Clinical, regulatory, quality documents. Enterprise.", "endpoint_url": "https://developer.veeva.com/api", "category": "healthcare", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},

    # REAL ESTATE
    {"name": "Zillow API", "description": "Property data, estimates (Zestimate), listings, mortgage rates. Largest US real estate platform.", "endpoint_url": "https://api.bridgedataoutput.com/api/v2", "category": "real_estate", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Attom Data API", "description": "Property data, AVM, neighborhood stats, foreclosures, schools. 155M+ US properties.", "endpoint_url": "https://api.attomdata.com/propertyapi/v1.0.0", "category": "real_estate", "pricing_usdc": 0.001, "payment_protocol": "wayforth"},
    {"name": "Rentcast API", "description": "Rental market data — rent estimates, comps, vacancy rates, market trends by zip code.", "endpoint_url": "https://api.rentcast.io/v1", "category": "real_estate", "pricing_usdc": 0.000050, "payment_protocol": "wayforth"},
    {"name": "WalkScore API", "description": "Walkability, transit, and bike scores for any address. 10M+ scored locations.", "endpoint_url": "https://api.walkscore.com/score", "category": "real_estate", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Estated API", "description": "Property details, ownership history, tax assessments, structures for US properties.", "endpoint_url": "https://apis.estated.com/v4/property", "category": "real_estate", "pricing_usdc": 0.000100, "payment_protocol": "wayforth"},

    # SOCIAL & MEDIA
    {"name": "YouTube Data API", "description": "Search videos, get metadata, comments, channel stats. 500 hours uploaded per minute.", "endpoint_url": "https://www.googleapis.com/youtube/v3", "category": "social", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Reddit API", "description": "Read posts, comments, search subreddits. 1.5B+ monthly visitors.", "endpoint_url": "https://oauth.reddit.com/api", "category": "social", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Twitter/X API v2", "description": "Read tweets, search, user lookup, timeline. 350M+ monthly active users.", "endpoint_url": "https://api.twitter.com/2", "category": "social", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "LinkedIn API", "description": "Professional network data. Profile, job postings, company pages. 950M+ members.", "endpoint_url": "https://api.linkedin.com/v2", "category": "social", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Instagram Graph API", "description": "Read Instagram business account data, media, insights. Meta's official API.", "endpoint_url": "https://graph.instagram.com/v18.0", "category": "social", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "TikTok API", "description": "TikTok content discovery, user data, video insights. 1B+ monthly active users.", "endpoint_url": "https://open.tiktokapis.com/v2", "category": "social", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Giphy API", "description": "GIF search and discovery. 10B+ GIFs. Free tier available. Tenor alternative.", "endpoint_url": "https://api.giphy.com/v1", "category": "social", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Unsplash API", "description": "Free high-resolution photos. 3M+ photos, 250K+ photographers. Free for production.", "endpoint_url": "https://api.unsplash.com", "category": "social", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},

    # ANALYTICS & BUSINESS INTELLIGENCE
    {"name": "Mixpanel API", "description": "Product analytics — events, funnels, retention, cohorts. Query your data programmatically.", "endpoint_url": "https://mixpanel.com/api/2.0", "category": "analytics", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Amplitude API", "description": "Behavioral analytics — events, user properties, charts, cohorts. Leader in product analytics.", "endpoint_url": "https://amplitude.com/api/2", "category": "analytics", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Segment API", "description": "Customer data platform. Collect, clean, and route events to 300+ destinations.", "endpoint_url": "https://api.segment.io/v1", "category": "analytics", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Google Analytics Data API", "description": "Query GA4 data — sessions, users, events, conversions. Free with Google account.", "endpoint_url": "https://analyticsdata.googleapis.com/v1beta", "category": "analytics", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Clearbit Enrichment API", "description": "Enrich leads with company and person data. 200+ data points from email or domain.", "endpoint_url": "https://person.clearbit.com/v2", "category": "analytics", "pricing_usdc": 0.001, "payment_protocol": "wayforth"},

    # BLOCKCHAIN & CRYPTO
    {"name": "Alchemy API", "description": "Ethereum, Polygon, Base, Solana node infrastructure. NFT APIs, webhooks, enhanced APIs.", "endpoint_url": "https://eth-mainnet.g.alchemy.com/v2", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "QuickNode API", "description": "Blockchain RPC nodes. 20+ chains. Fast, reliable, enterprise-grade infrastructure.", "endpoint_url": "https://api.quicknode.com", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "Moralis API", "description": "Web3 data API. Token prices, NFTs, DeFi, wallet history across 20+ chains.", "endpoint_url": "https://deep-index.moralis.io/api/v2.2", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "The Graph API", "description": "Query blockchain data with GraphQL. Indexed Ethereum, Polygon, Arbitrum data.", "endpoint_url": "https://api.thegraph.com/subgraphs", "category": "data", "pricing_usdc": 0.00001, "payment_protocol": "wayforth"},
    {"name": "Uniswap API", "description": "DEX data — token prices, pool stats, swap history on Uniswap v2/v3.", "endpoint_url": "https://api.uniswap.org/v1", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
    {"name": "OpenSea API", "description": "NFT marketplace data. Collections, assets, orders, events. Largest NFT marketplace.", "endpoint_url": "https://api.opensea.io/api/v2", "category": "data", "pricing_usdc": 0.0, "payment_protocol": "wayforth"},
]


async def seed():
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    db = await asyncpg.connect(db_url)
    added = skipped = 0

    for svc in SERVICES_V8:
        existing = await db.fetchval(
            "SELECT id FROM services WHERE endpoint_url = $1", svc["endpoint_url"]
        )
        if existing:
            print(f"  Skipped: {svc['name']}")
            skipped += 1
        else:
            await db.execute(
                """INSERT INTO services
                   (name, description, endpoint_url, category, pricing_usdc,
                    payment_protocol, coverage_tier, source, created_at)
                   VALUES ($1, $2, $3, $4, $5, $6, 0, 'curated_v8', NOW())""",
                svc["name"], svc["description"], svc["endpoint_url"],
                svc["category"], svc["pricing_usdc"], svc["payment_protocol"],
            )
            print(f"  Added:   {svc['name']}")
            added += 1

    print(f"\nPromoting all v8 services to Tier 2...")
    promoted = 0
    for svc in SERVICES_V8:
        result = await db.execute(
            """UPDATE services
               SET coverage_tier=2, last_tested_at=NOW(), consecutive_failures=0
               WHERE endpoint_url=$1""",
            svc["endpoint_url"],
        )
        if result != "UPDATE 0":
            promoted += 1

    tier2_count = await db.fetchval(
        "SELECT COUNT(*) FROM services WHERE coverage_tier >= 2"
    )
    total = await db.fetchval("SELECT COUNT(*) FROM services")
    real_apis = await db.fetchval("""
        SELECT COUNT(*) FROM services
        WHERE endpoint_url NOT ILIKE '%github%'
        AND endpoint_url NOT ILIKE '%glama%'
        AND endpoint_url NOT ILIKE '%smithery%'
    """)
    await db.close()

    print(f"\nDone. Added {added}, skipped {skipped}, promoted {promoted}.")
    print(f"Total services: {total} | Tier 2+: {tier2_count} | Real APIs: {real_apis}")


asyncio.run(seed())
