# Wayforth

**The search engine for AI agents.**

```bash
uvx wayforth-mcp

# Or visit the dashboard
open https://wayforth.io/dashboard
```

Works with Claude Code, Cursor, Windsurf, and any MCP-compatible runtime.

**Full quickstart guide:** https://gateway.wayforth.io/quickstart

## What It Does

```python
# Find the best service for your need
wayforth_search("translate text to Spanish")
# → DeepL API (score: 98, wri: 82), Azure Translator (96), LibreTranslate (90)

# Pay using credits
wayforth_pay(service_id, amount_usd)
# → Deducts from your credit balance (1 credit = $0.001 USD)
# → Returns credits_remaining after deduction.
```

## What's Live

- **200+ real API endpoints indexed** — MCP servers, REST APIs, verified services
- **147 Tier 2 verified** — automated 90%+ uptime, probed every 6 hours, auto-demoted on failure
- **WayforthRank** — proprietary multi-signal ranking engine
- **WayforthQL** — declarative query language for structured service discovery
- **Coverage tiers 0–3** — the only automated reliability verification system in any agent registry
- **Credits-based payments** — pre-paid credits, Stripe checkout, 1 credit = $0.001 USD
- **API keys** — free tier included, paid tiers at wayforth.io/pricing

## MCP Tools

| Tool | Description |
|------|-------------|
| `wayforth_search` | Semantic search — ranked results 0–100 with WRI scores |
| `wayforth_pay` | Pay for a service using credits |
| `wayforth_list` | Browse catalog with filters |
| `wayforth_stats` | Catalog statistics |
| `wayforth_status` | API health check |
| `wayforth_remember` | Save a service to agent memory |
| `wayforth_recall` | Retrieve saved services |
| `wayforth_similar` | Services co-used with a given service |
| `wayforth_identity` | Get or create agent identity with trust score and reputation tier |

## WayforthQL

```http
POST https://gateway.wayforth.io/query
Content-Type: application/json

{
  "query": "fast inference for coding",
  "tier_min": 2,
  "sort_by": "wri",
  "limit": 5
}
```

Full spec: https://gateway.wayforth.io/wayforthql-spec

## Coverage Tiers

| Tier | Name | Criteria |
|------|------|----------|
| 0 | Discovered | Indexed, not yet verified |
| 1 | Tested | Endpoint responds |
| 2 | Executable | 90%+ uptime, 7-day verified — default search results |
| 3 | Verified | KYB complete, SLA signed |

Verification runs automatically every 6 hours. No manual review. No paid placement. Ever.

## REST API

Base URL: `https://gateway.wayforth.io`

| Endpoint | Description |
|----------|-------------|
| `GET /search?q=...` | Semantic search |
| `POST /query` | WayforthQL structured query |
| `GET /billing/balance` | Credit balance and tier |
| `GET /billing/packages` | Available credit packages |
| `GET /stats` | Catalog stats |
| `GET /leaderboard` | Most searched by agents |
| `GET /services/similar/{id}` | Co-usage recommendations |
| `GET /services/{id}/history` | WRI trend over time |
| `POST /memory` | Save agent memory |
| `POST /webhooks/register` | Register tier change webhooks |
| `POST /tier3/apply` | Apply for Tier 3 verification |
| `GET /keys/tiers` | API key tier limits |

Full docs: https://gateway.wayforth.io/docs

## SDKs

```bash
pip install wayforth-sdk       # Python
npm install wayforth-sdk       # TypeScript / JavaScript
uvx wayforth-mcp               # MCP server
```

## Examples

### Research Agent
A complete working example — an agent that discovers and uses multiple services through Wayforth:

```bash
pip install wayforth-sdk
python examples/research_agent.py "What are the best vector databases for RAG in 2026?"
```

See [`examples/research_agent.py`](examples/research_agent.py) for the full implementation.

**What it demonstrates:**
- Natural language service discovery across 200+ real API endpoints
- WayforthRank scoring — best service rises to the top automatically  
- WayforthQL structured queries with tier filters
- Credits-based payment via wayforth_pay()
- The full search → pay attribution loop via query_id

## Credits

1 credit = $0.001 USD. Every search costs 1 credit. wayforth_pay() deducts credits equal to the service's per-request price.

New accounts receive 1,000 free credits. Top up at [wayforth.io/dashboard](https://wayforth.io/dashboard).

| Package | Credits | Price |
|---------|---------|-------|
| Starter | 10,000 | $10 |
| Pro | 60,000 | $50 |
| Growth | 150,000 | $100 |

## License

Core: BSL 1.1 — source visible, no competing use for 4 years. OpenAPI spec: MIT.

---

[wayforth.io](https://wayforth.io) · [dashboard](https://wayforth.io/dashboard) · [docs](https://gateway.wayforth.io/docs) · [Contact Us](https://wayforth.io/contact)
