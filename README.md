# Wayforth

[![PyPI](https://img.shields.io/pypi/v/wayforth-mcp)](https://pypi.org/project/wayforth-mcp/)
[![smithery badge](https://smithery.ai/badge/support-9ef4/Wayforth)](https://smithery.ai/servers/support-9ef4/Wayforth)

**v0.6.0 — Intelligence** · Search engine and payment rail for AI agents.

2,629 indexed APIs. Pay via card or crypto. One MCP install.

```bash
uvx wayforth-mcp
```

![Wayforth Demo](./docs/demo.gif)

## What It Does

```python
# Discover
wayforth_search("translate text to Spanish")
→ DeepL  WRI:82  Tier 2 ✓  $0.00003/call  [card|crypto]

# Intent-based routing (9 categories, streaming LLM support)
POST /run {"intent": "summarize this article", "input": {...}}
POST /run {"intent": "fast llm inference", "input": {...}, "stream": true}

# Structured discovery (WayforthQL v1.1)
POST /query {"query": "translate text", "tier_min": 2, "sort_by": "wri",
             "latency_max": 500, "region": "eu", "payment_rail": "x402"}
→ protocol: WayforthQL/1.1

# Parallel batch execution
POST /execute/batch {"slugs": ["groq", "deepl"], "params": {...}}

# Execute — managed services, no API keys needed
POST /execute {"service_slug": "groq", "params": {...}, "key_source": "managed"}
→ {content: "...", credits_deducted: 3, credits_remaining: 997}
```

## Live Now

- **2,629 APIs** indexed across 18 categories
- **254 Tier 2 verified** — probed every 6h, auto-demoted after failures
- **42 x402 native services** — sourced from x402.org/ecosystem
- **13 managed services** — Groq, Together AI, DeepL, OpenWeatherMap, NewsAPI, Serper, Resend, AssemblyAI, Stability AI, Tavily, Jina AI, Alpha Vantage, ElevenLabs
- **WayforthRank v2** — payment-signal weighted scoring (payment rate × 35%, base WRI × 40%, volume × 15%, recency × 10%)
- **WayforthQL v1.1** — structured discovery with tier/price/protocol/latency/region filters and pagination
- **Dual-track payments** — Stripe Treasury (card) + Base blockchain (non-custodial)
- **BYOK** — bring your own key for any of 2,629 services, encrypted at rest (Fernet AES-128)
- **Live service health** — rolling avg_response_ms and error_rate per service, WRI-adjusted
- **3 provisional patents** filed (WF-2026-001, WF-2026-002, WF-2026-003)

## Install

```bash
# Run directly
uvx wayforth-mcp

# Add to Claude Code
claude mcp add wayforth -- uvx wayforth-mcp

# Set API key
export WAYFORTH_API_KEY=wf_live_...
```

Get your API key: [wayforth.io/signup](https://wayforth.io/signup)

## Plans

| Plan | Calls/month | Price |
|------|-------------|-------|
| Free | 100 | $0/mo |
| Builder | 1,000 | $12/mo |
| Starter | 3,500 | $29/mo |
| Pro | 12,000 | $99/mo |
| Growth | 40,000 | $299/mo |
| Enterprise | 100,000 | custom |

## Payment Tracks

| Track | Method | How |
|-------|--------|-----|
| A — Card | Stripe Treasury (fiat) | Buy credits, no crypto |
| B — Crypto | Base blockchain (USDC) | Non-custodial calldata |
| C — x402 | Native HTTP 402 | Auto-detected, Coinbase CDP |

All tracks earn Wayforth the same 1.5% routing fee.

## Execution — 13 Managed Services

| Service | Category | Credits/Call |
|---------|----------|-------------|
| Groq | LLM inference | 3 |
| Together AI | LLM inference | 3 |
| DeepL | Translation | 1 |
| OpenWeatherMap | Weather data | 1 |
| NewsAPI | News search | 1 |
| Serper | Google search | 1 |
| Resend | Email | 2 |
| AssemblyAI | Speech-to-text | 5 |
| Stability AI | Image generation | 10 |
| Tavily | AI web search | 3 |
| Jina AI | URL to markdown | 2 |
| Alpha Vantage | Stock data | 2 |
| ElevenLabs | Text-to-speech | 5 |

## Key Endpoints

| Endpoint | Description |
|----------|-------------|
| `POST /run` | Intent-based routing across 9 categories; `"stream": true` for LLM SSE |
| `POST /execute` | Direct managed-service execution by slug |
| `POST /execute/batch` | Parallel execution, up to 5 slugs |
| `POST /query` | WayforthQL v1.1 — latency_max, region, payment_rail filters + pagination |
| `GET /run/intents` | Intent catalog (9 entries) |
| `GET /openapi.json` | Full OpenAPI 3.1.0 spec |
| `GET /services/{slug}/health` | Live avg_response_ms, error_rate, WRI penalty |
| `GET /account/usage/history` | 30-day call breakdown |
| `GET /account/wayf-points/history` | Points timeline |
| `GET /health` | System health (DB, Redis, managed services) |

## Rate Limits

Every response includes rate-limit headers:

```
X-RateLimit-Tier: free
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 73
X-RateLimit-Reset: 1748736000
```

## Architecture

```
wayforth_search() / POST /query (WayforthQL v1.1)
↓
WayforthRank v2 (payment-signal weighted scoring, patent pending)
  base_wri×0.40 + payment_rate×0.35 + volume×0.15 + recency×0.10
  ± live health overlay (−10 if error_rate > 30%, −5 if p50 > 5s)
↓
POST /run — intent routing (9 categories) | stream: true for LLM SSE
↓
wayforth_pay() — Track A (card) | Track B (crypto) | Track C (x402)
↓
POST /execute — managed keys | BYOK (encrypted)
↓
Real API result + WayforthRank signal update
```

## Links

- **Dashboard:** [wayforth.io/dashboard](https://wayforth.io/dashboard)
- **Docs:** [wayforth.io/docs](https://wayforth.io/docs)
- **Whitepaper:** [wayforth.io/Wayforth_Whitepaper_v6.1.pdf](https://wayforth.io/Wayforth_Whitepaper_v6.1.pdf)
- **PyPI:** [pypi.org/project/wayforth-mcp](https://pypi.org/project/wayforth-mcp/)
- **Contact:** dor@wayforth.io

## License

Business Source License 1.1 (BSL 1.1)
Converts to Apache 2.0 on April 25, 2030
Licensor: Wayforth LTD

Smart contracts: [WayforthEscrow on Base Sepolia](https://sepolia.basescan.org/address/0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809)
