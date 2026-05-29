# Wayforth — The API Runtime for AI Agents

**One tool call. Any API. No setup.**

[![Version](https://img.shields.io/badge/version-0.8.2_Gravity-4F46E5)](https://gateway.wayforth.io/docs)
[![License](https://img.shields.io/badge/license-BSL_1.1-64748B)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-190%2B_passing-10B981)](https://github.com/WayforthOfficial/wayforth)

---

## What It Is

Wayforth is the API runtime for AI agents — a single integration that gives any agent access to 4,974 indexed APIs, ranked by real usage signals and executable with credits or crypto. Agents search, select, and pay for API services in one call without managing keys, credentials, or billing integrations. Built for developers who want their agents to reach the full surface area of the internet without building the infrastructure themselves.

---

## Quick Start

**Step 1 — Install the MCP server**

```bash
uvx wayforth-mcp
```

Add to Claude Code permanently:
```bash
claude mcp add wayforth -- uvx wayforth-mcp
```

**Step 2 — Get an API key**

Sign up at [wayforth.io/signup](https://wayforth.io/signup) and copy your key from the dashboard.

**Step 3 — Set your key**

```bash
export WAYFORTH_API_KEY=wf_live_...
```

**Step 4 — Run your first call**

```python
# Search — natural language, no keys needed
wayforth_search("translate text to Spanish")
# → DeepL   WRI: 82  Tier 2 ✓  managed
# → LibreTranslate  WRI: 71  Tier 2 ✓

# Execute — managed services, zero setup
wayforth_execute("deepl", {"text": "Hello", "target_lang": "ES"})
# → {"translations": [{"text": "Hola"}]}
```

Python SDK:
```bash
pip install wayforth-sdk
```

```python
from wayforth import WayforthClient
client = WayforthClient(api_key="wf_live_...")
results = client.search("real-time stock data")
```

Full API reference: [gateway.wayforth.io/docs](https://gateway.wayforth.io/docs)

---

## Managed Services

13 managed services available with zero API key setup. Wayforth holds the credentials — you call the tool.

| Service | Category |
|---------|----------|
| Groq | LLM inference |
| Together AI | LLM inference |
| DeepL | Translation |
| Serper | Web search |
| Tavily | Web search |
| Brave | Web search |
| OpenWeather | Weather data |
| NewsAPI | News search |
| Alpha Vantage | Financial data |
| Jina AI | Content extraction |
| AssemblyAI | Speech-to-text |
| Stability AI | Image generation |
| Resend | Email |

---

## For Providers

Wayforth routes agent traffic to providers at scale.

- **Discovery** — providers are indexed and ranked by WayforthRank, a scoring system driven by real agent payment signals. The more agents pay for a service, the higher it ranks.
- **x402-native** — providers supporting the HTTP 402 micropayment protocol receive direct USDC settlement per call, non-custodial.
- **Managed** — providers integrated as managed services receive monthly ACH payouts based on routed call volume.

Register your API: [wayforth.io/for-providers](https://wayforth.io/for-providers)

---

## Catalog

| Metric | Count |
|--------|-------|
| APIs indexed | 4,974 |
| Tier 2 verified | 3,550+ |
| x402-native services | 277 |
| Categories | 19 |

**WayforthRank** scores every service 0–100 based on uptime history, probe frequency, payment conversion rate, and real agent usage patterns. Higher score = more trustworthy for agent workloads.

**Coverage tiers:**
- **Tier 0** — submitted, not yet probed
- **Tier 1** — probed, endpoint confirmed reachable
- **Tier 2** — automated reliability testing every 6 hours, score maintained
- **Tier 3** — managed integration, Wayforth holds the key

---

## Payment Rails

Three ways to pay for API calls through Wayforth:

| Rail | Method | Settlement |
|------|--------|------------|
| Card | Stripe (fiat) | Buy credits, spend as calls |
| USDC | Base blockchain | Direct crypto deposits |
| x402 | HTTP 402 protocol | Per-call micropayments |

**x402** is the open HTTP-402 micropayment standard — agents pay per call with no subscription or balance required. Non-custodial escrow pays providers on confirmed execution.

---

## MCP Tools

9 tools available via the Wayforth MCP server:

| Tool | Description |
|------|-------------|
| `wayforth_search` | Search 4,974 APIs by intent — returns ranked results with WRI scores |
| `wayforth_query` | Structured discovery with WayforthQL — filter by tier, latency, region, price, payment rail |
| `wayforth_run` | Intent-based routing: describe what you need, Wayforth picks and executes the best service |
| `wayforth_execute` | Direct execution of a managed service by slug — no API key required |
| `wayforth_pay` | Pay for a service call via card credits or USDC on Base |
| `wayforth_list` | List available services with category and tier filters |
| `wayforth_status` | Live API health check and real-time service counts |
| `wayforth_remember` | Store a persistent memory entry for agent context |
| `wayforth_recall` | Retrieve stored memories by query |

---

## WayforthQL

Structured query language for precise API discovery. Filter by tier, latency, region, price, and payment rail.

```
POST https://gateway.wayforth.io/query
```

```json
{
  "query": "fast inference for coding agents",
  "tier_min": 2,
  "sort_by": "wri",
  "latency_max": 500,
  "region": "us",
  "protocol": "x402",
  "price_max": 0.001,
  "limit": 5
}
```

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `wayforth_id` | string | Unique service ID: `wayforth://<slug>` |
| `name` | string | Service name |
| `wri` | float | WayforthRank score, 0–100 |
| `coverage_tier` | int | Verification tier (0–3) |
| `pricing_usdc` | float | Price per request in USD |
| `payment_protocol` | string | One of: `wayforth` \| `x402` \| `any` |

`protocol` filter accepts: `wayforth` \| `x402` \| `any`

`x402` is the open HTTP-402 payment standard for per-call micropayments.

Full spec: [gateway.wayforth.io/wayforthql-spec](https://gateway.wayforth.io/wayforthql-spec)

---

## Development Status

**v0.8.2 "Gravity" — current release**

- 190+ tests passing, zero failures
- 99.97% uptime
- **Pioneer Program** — developers earn daily bonus credits by helping new verified services build signal
- **LLM gateway** — `POST /v1/chat/completions` OpenAI-compatible endpoint with Groq → Together AI → Mistral failover and streaming
- **Tier 1 input caps** — DeepL 2,000 chars, AssemblyAI ~10 min, Stability AI 1 image per call
- **API key encryption versioning** — versioned Fernet keys, zero-downtime rotation
- **Provider email verification** — required before service submission
- **Admin audit log** — append-only, trigger-enforced
- **BYOK** — bring your own API key for any indexed service (AES-256-GCM encrypted at rest)
- **MFA** — TOTP-based authentication available

Pricing: [wayforth.io/pricing](https://wayforth.io/pricing)

---

## Links

- **Quickstart:** [gateway.wayforth.io/quickstart](https://gateway.wayforth.io/quickstart)
- **API Reference:** [gateway.wayforth.io/docs](https://gateway.wayforth.io/docs)
- **Whitepaper:** [wayforth.io/Wayforth_Whitepaper_v6.8.pdf](https://wayforth.io/Wayforth_Whitepaper_v6.8.pdf)
- **Dashboard:** [wayforth.io/dashboard](https://wayforth.io/dashboard)
- **For Providers:** [wayforth.io/for-providers](https://wayforth.io/for-providers)
- **PyPI (MCP):** [pypi.org/project/wayforth-mcp](https://pypi.org/project/wayforth-mcp/)
- **PyPI (SDK):** [pypi.org/project/wayforth-sdk](https://pypi.org/project/wayforth-sdk/)
- **Contact:** [wayforth.io/contact](https://wayforth.io/contact)

---

## License

Business Source License 1.1 (BSL 1.1) — converts to Apache 2.0 on April 25, 2030.

© 2026 Wayforth Technologies Inc.
