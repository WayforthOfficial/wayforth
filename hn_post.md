# Wayforth — HN Launch Post
## SUBMIT AT: news.ycombinator.com/submit
## TIME: Tuesday 9am Eastern

---

## TITLE (copy exactly)
Show HN: Wayforth – The search engine and payment rail for AI agents

## URL
https://github.com/WayforthOfficial/wayforth

---

## BODY (paste exactly)

AI agents that call external services — for inference, translation, data,
images, audio — currently require the developer to manually find each API,
sign up, manage keys, write integration code, and handle billing separately
for every provider. One agent workflow can touch a dozen services. That
doesn't scale.

Wayforth is one install:

    uvx wayforth-mcp

Then two tool calls from any agent:

    # Discover
    wayforth_search("translate text to Spanish")
    → DeepL API       WRI: 82  Tier 2 Verified  $0.0000025/req
    → LibreTranslate  WRI: 71  Tier 2 Verified  Free
    → ModernMT        WRI: 68  Tier 2 Verified  $0.000003/req

    # Pay
    wayforth_pay(service_id, owner_address, amount_usdc=0.001)
    → Non-custodial calldata. Settles on Base in ~2 seconds.

No API keys. No billing relationships. No integration code.

---

What's live today:

- 190+ real API endpoints indexed across 7 categories (inference, data,
  translation, image, code, audio, embeddings)
- 147 Tier 2 verified — automatically probed every 6 hours, 90%+ uptime
  required, auto-demoted after 3 consecutive failures. No paid placement.
- WayforthRank — proprietary ranking engine combining semantic relevance,
  reliability history, and real agent payment conversion signals. Rankings
  improve with every query. (Patent pending)
- WayforthQL — declarative query language for structured discovery:
  POST /query {"query": "...", "tier_min": 2, "protocol": "x402",
  "price_max": 0.001, "sort_by": "wri"}
- Smart contracts on Base Sepolia — non-custodial escrow, audited,
  Basescan verified. Mainnet Q3 2026.
- Tiered routing fee 0.75%–1.5% — the only cost

Works in Claude Code, Cursor, Windsurf, and any MCP-compatible runtime.

---

The data flywheel:

Every search query and payment links to a query_id. When an agent pays for
a service, that payment converts the search record and feeds WayforthRank's
payment conversion signal — the strongest ranking signal. Rankings improve
with every real agent payment.

---

Already being used by developers who found us before this post.

Whitepaper:  https://wayforth.io/technology
Leaderboard: https://wayforth.io/leaderboard
GitHub:      https://github.com/WayforthOfficial/wayforth
Docs:        https://gateway.wayforth.io/docs
PyPI:        https://pypi.org/project/wayforth-mcp/

Happy to go deep on the WayforthRank architecture, the payment routing
design, or the coverage tier system.

