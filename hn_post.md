# HN Post — Tuesday April 29, 2026 — 9am Eastern

**Title:** Show HN: Wayforth – search engine and payment rail for AI agents (MCP server)

**Body:**
Wayforth is live. One MCP install, and your agent can search 2,500+ external APIs by intent and pay for them on-chain in a single tool call — no hardcoded endpoints, no manual API key management.

Install in Claude Code, Cursor, or Windsurf:

    uvx wayforth-mcp

Real example from agents already using it this week:

    result = wayforth_search("translate text to Spanish")
    # → DeepL API        (WRI: 78, Tier 2 Verified, $0.03/req)
    # → Azure Translator (WRI: 71, Tier 2 Verified, $0.01/req)
    # → LibreTranslate   (WRI: 44, Tier 1, free)

    calldata = wayforth_pay(result[0]["id"], my_wallet, 10_00)
    # → Returns approve + routePayment calldata
    # → Settles on Base in ~2 seconds. Tiered routing fee from 0.75%.

**What's live:**
- 2,501 services cataloged, 147 Tier 2 verified across 7 categories (inference, data, translation, image, code, audio, embeddings)
- 38 real searches already happened this week — before this post
- Probed every 6 hours. Auto-demoted on failure. No paid placement — pure uptime ranking.
- Smart contracts on Base Sepolia (Registry + Escrow, 54 tests passing)
- Free API tier: 1,000 searches/month, 10 req/min

**Why a search engine, not a registry:**

Most agent service registries are static lists you browse manually. Wayforth lets agents describe intent in natural language and get results ranked by real reliability signals — semantic match + uptime history + usage patterns.

The coverage tier system is the part I think is genuinely novel: services are automatically promoted Tier 0 (discovered) → Tier 1 (tested) → Tier 2 (verified, 90%+ uptime over 7 days). Agents only see Tier 2 by default. The data flywheel — every search, payment, and probe feeds back into ranking — is the long-term moat.

WayforthQL is the other piece: agents can declaratively query by protocol, uptime threshold, and price cap without knowing service names:

    POST /query {"query": "fast inference", "protocol": "x402", "tier_min": 2}

**Links:**

Live demo: https://wayforth.io/demo
Search: https://wayforth.io/search
Reference agent: https://github.com/WayforthOfficial/wayforth/blob/main/examples/research_agent.py
GitHub: https://github.com/WayforthOfficial/wayforth

**One question for the thread:**

MCP is winning the agent tool protocol race, but the payment layer is still wide open. We went with x402 (HTTP 402 + on-chain settlement on Base) because it's stateless and composable — but it's still early. Has anyone else shipped an agent that pays for external APIs in production? Curious what patterns people have found, especially around managing wallet keys inside agent runtimes.
