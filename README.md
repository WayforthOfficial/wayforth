# Wayforth

[![PyPI](https://img.shields.io/pypi/v/wayforth-mcp)](https://pypi.org/project/wayforth-mcp/)
[![smithery badge](https://smithery.ai/badge/support-9ef4/Wayforth)](https://smithery.ai/servers/support-9ef4/Wayforth)

**v0.7.1 — Stable Auth** · Search engine and payment rail for AI agents.

4,974 verified API endpoints across 19 categories. Pay via card or crypto. One MCP install.

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

- **4,974 verified API endpoints** across 19 categories
- **3,552 Tier 2 verified endpoints** — probed and confirmed reachable
- **250+ x402-native services** — pay-per-call via HTTP 402 protocol
- **18 managed services (13 active with API keys)** — Groq, Together AI, DeepL, Brave Search, Perplexity, OpenWeatherMap, NewsAPI, Serper, Tavily, Jina AI, Alpha Vantage, AssemblyAI, Stability AI, Resend, Firecrawl, Mistral, Gemini, ElevenLabs
- **WayforthQL v1.1** — filter by latency, region, payment rail, price, tier, and protocol with pagination
- **WayforthRank** — payment-signal weighted service scoring
- **Live service health** — response time and reliability tracked per service, affects ranking
- **BYOK** — bring your own API key for any service, AES-128 encrypted
- **Triple-track payments** — card, USDC on Base, x402 protocol
- **MFA** — TOTP-based multi-factor authentication available across developer, provider, and admin dashboards
- **Security** — professional penetration test completed, all findings resolved (v0.7.0)
- **Session persistence** — Google OAuth and email/password login fully resolved; wf_session cookie issued reliably across all auth providers
- **3 provisional patents** filed
- **186 tests** passing

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

| Plan | Credits/month | Monthly | Annual (save) |
|------|--------------|---------|---------------|
| Free | 100 | $0 | — |
| Builder | 1,000 | $12/mo | $99/yr (save $45) |
| Starter | 3,500 | $29/mo | $290/yr (save $58) |
| Pro | 12,000 | $99/mo | $990/yr (save $198) |
| Growth | 40,000 | $299/mo | $2,990/yr (save $598) |
| Enterprise | 100,000 | Custom | Custom |

Annual plans replenish credits monthly (same monthly amount); 2 months free vs paying monthly.

## Payment Tracks

| Track | Method | How |
|-------|--------|-----|
| A — Card | Stripe Treasury (fiat) | Buy credits, no crypto |
| B — Crypto | Base blockchain (USDC) | USDC on Base |
| C — x402 | Native HTTP 402 | Auto-detected, Coinbase CDP |

All tracks earn Wayforth the same 1.5% routing fee.

## Execution — 18 Managed Services

| Service | Category | Credits/Call |
|---------|----------|-------------|
| Groq | LLM inference | 3 |
| Together AI | LLM inference | 4 |
| Mistral | LLM inference | 3 |
| Gemini | LLM inference | 3 |
| Perplexity | AI search | 10 |
| DeepL | Translation | 20 |
| Serper | Google search | 3 |
| Brave Search | Web search | 5 |
| Tavily | AI web search | 4 |
| Jina AI | URL to markdown | 4 |
| Firecrawl | Web scraping | 5 |
| OpenWeatherMap | Weather data | 2 |
| NewsAPI | News search | 5 |
| Alpha Vantage | Stock data | 4 |
| AssemblyAI | Speech-to-text | 20 |
| ElevenLabs | Text-to-speech | 200 |
| Stability AI | Image generation | 65 |
| Resend | Email | 3 |

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
- **Docs:** [gateway.wayforth.io/guide/](https://gateway.wayforth.io/guide/)
- **Whitepaper:** [wayforth.io/Wayforth_Whitepaper_v6.4.pdf](https://wayforth.io/Wayforth_Whitepaper_v6.4.pdf)
- **PyPI:** [pypi.org/project/wayforth-mcp](https://pypi.org/project/wayforth-mcp/)
- **Contact:** [wayforth.io/contact](https://wayforth.io/contact)

## License

Business Source License 1.1 (BSL 1.1)
Converts to Apache 2.0 on April 25, 2030
Licensor: Wayforth Technologies Inc.

Smart contracts: [WayforthEscrow on Base Sepolia](https://sepolia.basescan.org/address/0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809)
