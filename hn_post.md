# HN Post — Tuesday April 29, 2026 — 9am Eastern

**Title:** Show HN: Wayforth – search engine and payment rail for AI agents (MCP server)

**Body:**
I built Wayforth — an MCP server that lets AI agents find and pay for external APIs in one step.

Install in Claude Code, Cursor, or Windsurf:

    uvx wayforth-mcp

Then in your agent:

    # Find the best service for your need
    wayforth_search("translate text to Spanish")
    → DeepL API (WRI: 78, Tier 2 Verified), Azure Translator, LibreTranslate...

    # Get non-custodial payment calldata
    wayforth_pay(service_id, owner_address, amount_usdc)
    → Returns approve + routePayment calldata
    → Settles on Base in ~2 seconds. Tiered routing fee from 0.75%.

**What's live:**
- 190+ real API endpoints, 154 Tier 2 verified across 7 categories (inference, data, translation, image, code, audio, embeddings)
- 154 Tier 2 verified — automated 90%+ uptime check, probed every 6 hours, auto-demoted on failure
- WayforthRank — proprietary multi-signal ranking engine (semantic + reliability + usage signals)
- WayforthQL — declarative query language: POST /query {"query": "fast inference", "protocol": "x402"}
- Smart contracts on Base Sepolia (Registry + Escrow, 54 tests passing)
- API keys with free tier — 10 req/min, 1,000 searches/month

**What makes it different from existing registries:**

Most agent service registries are lists. You browse them. Wayforth is a search engine — agents describe intent in natural language and get ranked results based on reliability, not alphabetical order.

The coverage tier system is the part I think is genuinely novel: services are automatically promoted from Tier 0 (discovered) → Tier 1 (tested) → Tier 2 (verified, 90%+ uptime over 7 days). Agents only see Tier 2 by default. No ads, no paid placement, pure uptime-based ranking.

**The data flywheel:**
Every search query, payment conversion, and service probe produces proprietary signals that improve ranking over time. The longer Wayforth runs, the better the rankings get. This is the moat — not the code.

Live demo: https://wayforth.io/demo
Search: https://wayforth.io/search
GitHub: https://github.com/WayforthOfficial/wayforth

Happy to answer questions about the architecture, MCP integration, or the payment routing system.
