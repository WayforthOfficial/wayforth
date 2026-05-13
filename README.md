# Wayforth

[![PyPI](https://img.shields.io/pypi/v/wayforth-mcp)](https://pypi.org/project/wayforth-mcp/)
[![smithery badge](https://smithery.ai/badge/support-9ef4/Wayforth)](https://smithery.ai/servers/support-9ef4/Wayforth)

**v0.6.1 — Intelligence** · Search engine and payment rail for AI agents.

3,000+ verified API endpoints across 19 categories. Pay via card or crypto. One MCP install.

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

- **3,000+ verified API endpoints** across 19 categories
- **800+ Tier 2 verified endpoints** — probed and confirmed reachable
- **250+ x402-native services** — pay-per-call via HTTP 402 protocol
- **13 managed services** — Groq, Together AI, DeepL, Brave Search, OpenWeatherMap, NewsAPI, Serper, Tavily, Jina AI, Alpha Vantage, AssemblyAI, Stability AI, Resend
- **WayforthQL v1.1** — filter by latency, region, payment rail, price, tier, and protocol with pagination
- **WayforthRank** — payment-signal weighted service scoring
- **Live service health** — response time and reliability tracked per service, affects ranking
- **BYOK** — bring your own API key for any service, AES-128 encrypted
- **Triple-track payments** — card, USDC on Base, x402 protocol
- **3 provisional patents** filed
- **120/120 end-to-end tests** passing

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
| Free | 100 | $0 |
| Builder | 1,000 | $12/mo |
| Starter | 3,500 | $29/mo |
| Pro | 12,000 | $99/mo |
| Growth | 40,000 | $299/mo |
| Enterprise | 100,000 | Custom |

## Payment Tracks

| Track | Method | How |
|-------|--------|-----|
| A — Card | Stripe Treasury (fiat) | Buy credits, no crypto |
| B — Crypto | Base blockchain (USDC) | USDC on Base |
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
| `GET /services/{slug}/health` | Live response time, reliability, ranking impact |
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
WayforthRank (payment-signal weighted scoring, patent pending)
  ± live health overlay
↓
POST /run — intent routing (9 categories) | stream: true for LLM SSE
↓
wayforth_pay() — Track A (card) | Track B (crypto) | Track C (x402)
↓
POST /execute — managed keys | BYOK (AES-128 encrypted)
↓
Real API result + WayforthRank signal update
```

## Links

- **Dashboard:** [wayforth.io/dashboard](https://wayforth.io/dashboard)
- **Docs:** [wayforth.io/docs](https://wayforth.io/docs)
- **Whitepaper:** [wayforth.io/Wayforth_Whitepaper_v6.3.pdf](https://wayforth.io/Wayforth_Whitepaper_v6.3.pdf)
- **PyPI:** [pypi.org/project/wayforth-mcp](https://pypi.org/project/wayforth-mcp/)
- **Contact:** [wayforth.io/contact](https://wayforth.io/contact)

## License

Business Source License 1.1 (BSL 1.1)
Converts to Apache 2.0 on April 25, 2030
Licensor: Wayforth Inc.

Smart contracts: [WayforthEscrow on Base Sepolia](https://sepolia.basescan.org/address/0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809)
