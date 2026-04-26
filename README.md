# Wayforth

**The search engine and payment rail for AI agents.**

```bash
uvx wayforth-mcp
```

Works with Claude Code, Cursor, Windsurf, and any MCP-compatible runtime.

## What It Does

```python
# Find the best service for your need
wayforth_search("translate text to Spanish")
# → DeepL API (score: 98, wri: 82), Azure Translator (96), LibreTranslate (90)

# Pay non-custodially on Base
wayforth_pay(service_id, owner_address, amount_usdc)
# → Returns approve + routePayment calldata
# → Settles in ~2 seconds. 1.5% routing fee. Agent signs, Wayforth routes.
```

## What's Live

- **2,463+ services indexed** — MCP servers, REST APIs, x402-enabled services
- **115 Tier 2 verified** — automated 90%+ uptime, probed every 6 hours, auto-demoted on failure
- **WayforthRank** — proprietary multi-signal ranking engine
- **WayforthQL** — declarative query language for structured service discovery
- **Coverage tiers 0–3** — the only automated reliability verification system in any agent registry
- **Non-custodial payments** — smart contracts on Base Sepolia (39 tests, 256-run fuzz)
- **API keys** — free tier included, paid tiers at wayforth.io/pricing

## MCP Tools

| Tool | Description |
|------|-------------|
| `wayforth_search` | Semantic search — ranked results 0–100 with WRI scores |
| `wayforth_pay` | Non-custodial payment calldata for any service |
| `wayforth_list` | Browse catalog with filters |
| `wayforth_stats` | Catalog statistics |
| `wayforth_status` | API health check |
| `wayforth_remember` | Save a service to agent memory |
| `wayforth_recall` | Retrieve saved services |
| `wayforth_similar` | Services co-used with a given service |
| `wayforth_identity` | Get or create agent identity with trust score and reputation tier |

## WayforthQL

```http
POST https://api-production-fd71.up.railway.app/query
Content-Type: application/json

{
  "query": "fast inference for coding",
  "tier_min": 2,
  "protocol": "x402",
  "sort_by": "wri",
  "limit": 5
}
```

Full spec: https://api-production-fd71.up.railway.app/wayforthql-spec

## Coverage Tiers

| Tier | Name | Criteria |
|------|------|----------|
| 0 | Discovered | Indexed, not yet verified |
| 1 | Tested | Endpoint responds |
| 2 | Executable | 90%+ uptime, 7-day verified — default search results |
| 3 | Verified | KYB complete, SLA signed |

Verification runs automatically every 6 hours. No manual review. No paid placement. Ever.

## REST API

Base URL: `https://api-production-fd71.up.railway.app`

| Endpoint | Description |
|----------|-------------|
| `GET /search?q=...` | Semantic search |
| `POST /query` | WayforthQL structured query |
| `POST /pay` | Payment calldata |
| `GET /stats` | Catalog stats |
| `GET /leaderboard` | Most searched by agents |
| `GET /services/similar/{id}` | Co-usage recommendations |
| `GET /services/{id}/history` | WRI trend over time |
| `POST /memory` | Save agent memory |
| `POST /webhooks/register` | Register tier change webhooks |
| `POST /tier3/apply` | Apply for Tier 3 verification |
| `GET /keys/tiers` | API key tier limits |

Full docs: https://api-production-fd71.up.railway.app/docs

## SDKs

```bash
pip install wayforth-sdk       # Python
npm install wayforth-sdk       # TypeScript / JavaScript
uvx wayforth-mcp               # MCP server
```

## Smart Contracts (Base Sepolia)

| Contract | Address |
|----------|---------|
| WayforthRegistry | `0x55810EfB3444A693556C3f9910dbFbF2dDaC369C` |
| WayforthEscrow | `0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809` |

39 Foundry tests passing. 256-run fuzz suite. Mainnet deployment follows independent security audit.

## License

Core: BSL 1.1 — source visible, no competing use for 4 years. OpenAPI spec + contract ABIs: MIT.

---

[wayforth.io](https://wayforth.io) · [docs](https://api-production-fd71.up.railway.app/docs) · [whitepaper](https://github.com/WayforthOfficial/wayforth/blob/main/docs/whitepaper-v2.pdf) · [hello@wayforth.io](mailto:hello@wayforth.io)
