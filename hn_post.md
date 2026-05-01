# Wayforth — HN Launch Post
## SUBMIT AT: news.ycombinator.com/submit
## TIME: Tuesday 9am Eastern

---

## TITLE (copy exactly)
Show HN: Wayforth – The search engine for AI agents

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

    # Pay — card or crypto
    wayforth_pay(service_id, amount_usd=0.001)               # card default
    wayforth_pay(service_id, amount_usd=0.001, track="crypto") # non-custodial Base
    → 1.5% routing fee. No fixed fee — works for micropayments.

No API keys per provider. No billing relationships. No integration code.

---

What's live today:

- 274+ real API endpoints indexed across 18 categories (inference, translation,
  data, image, audio, communication, payments, productivity, maps, identity,
  devops, legal, healthcare, real_estate, social, analytics, code, embeddings)
- 225+ Tier 2 verified — automatically probed every 6 hours, 90%+ uptime
  required, auto-demoted after 3 consecutive failures. No paid placement.
- WayforthRank — proprietary ranking engine combining semantic relevance,
  reliability history, and real agent payment conversion signals. Rankings
  improve with every real payment. (3 patents pending: WF-2026-001/002/003)
- Dual-track payment rail — Track A: card credits via Stripe Treasury (fiat,
  no crypto needed). Track B: non-custodial Base blockchain calldata. Track C:
  x402 native. Same 1.5% routing fee on all tracks.
- BYOK — add your own API key for any of 274+ catalog services. Wayforth
  manages, proxies, retries. Keys encrypted at rest (Fernet AES-128).
- Credits system — pre-paid credits via Stripe. 1 credit = $0.001.
  100 free credits/month on signup. Starter $19 → 50K credits.
- WayforthQL v1 — structured discovery with filters:
  POST /query {"query": "...", "tier_min": 2, "price_max": 0.001, "sort_by": "wri"}
  Returns ranked results with WRI scores, pricing, and payment options per service.

Works in Claude Code, Cursor, Windsurf, and any MCP-compatible runtime.

---

The data flywheel:

Every search query and payment links to a query_id. When an agent pays for
a service, that payment converts the search record and feeds WayforthRank's
payment conversion signal — the strongest ranking signal. Rankings improve
with every real agent payment.

---

Whitepaper:  https://wayforth.io/wayforth-whitepaper-v5.pdf
Leaderboard: https://wayforth.io/leaderboard
GitHub:      https://github.com/WayforthOfficial/wayforth
Docs:        https://wayforth.io/docs
PyPI:        https://pypi.org/project/wayforth-mcp/

Happy to go deep on the WayforthRank architecture, the dual-track payment
design, the BYOK system, or the coverage tier system.
