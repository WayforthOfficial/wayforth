# Wayforth

**Search engine and payment rail for AI agents.**

274+ verified APIs. Pay via card or crypto. One MCP install.

```bash
uvx wayforth-mcp
```

## What It Does

```python
# Discover
wayforth_search("translate text to Spanish")
→ DeepL  WRI:82  Tier 2 ✓  $0.00003/call  [card|crypto]

# Structured discovery (WayforthQL v1)
POST /query {"query": "translate text", "tier_min": 2, "sort_by": "wri"}
→ protocol: WayforthQL/1.0

# Pay — card or crypto
wayforth_pay("deepl", 0.001)               # card default
wayforth_pay("deepl", 0.001, track="crypto") # non-custodial Base

# Execute — 8 managed services, no API keys needed
POST /execute {"service_slug": "groq", "params": {...}, "key_source": "managed"}
→ {content: "...", credits_deducted: 3, credits_remaining: 997}
```

## Live Now

- **274+ APIs** indexed across 18 categories
- **225+ Tier 2 verified** — probed every 6h, auto-demoted after failures
- **42 x402 native services** — sourced from x402.org/ecosystem
- **8 managed services** — Groq, DeepL, OpenWeather, NewsAPI, Serper, Resend, AssemblyAI, Stability AI
- **WayforthQL v1** — structured discovery with tier/price/protocol filters
- **Dual-track payments** — Stripe Treasury (card) + Base blockchain (non-custodial)
- **BYOK** — bring your own key for any of 274+ services, encrypted at rest (Fernet AES-128)
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

## Credit Packages

| Plan | Price | Credits |
|------|-------|---------|
| Free | $0/mo | 100/month |
| Starter | $19 | 50,000 |
| Pro | $99 | 300,000 |
| Growth | $299 | 1,000,000 |

1 credit = $0.001. Credits never expire.

## Payment Tracks

| Track | Method | How |
|-------|--------|-----|
| A — Card | Stripe Treasury (fiat) | Buy credits, no crypto |
| B — Crypto | Base blockchain (USDC) | Non-custodial calldata |
| C — x402 | Native HTTP 402 | Auto-detected, Coinbase CDP |

All tracks earn Wayforth the same 1.5% routing fee.

## Execution — 8 Managed Services

| Service | Category | Credits/Call |
|---------|----------|-------------|
| Groq | LLM inference | 3 |
| DeepL | Translation | 1 |
| OpenWeatherMap | Weather data | 1 |
| NewsAPI | News search | 1 |
| Serper | Google search | 1 |
| Resend | Email | 2 |
| AssemblyAI | Speech-to-text | 5 |
| Stability AI | Image generation | 10 |

## Architecture

```
wayforth_search() / POST /query (WayforthQL v1)
↓
WayforthRank (payment-signal ranking, patent pending)
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
- **Whitepaper:** [wayforth.io/wayforth-whitepaper-v5.pdf](https://wayforth.io/wayforth-whitepaper-v5.pdf)
- **PyPI:** [pypi.org/project/wayforth-mcp](https://pypi.org/project/wayforth-mcp/)
- **Contact:** dor@wayforth.io

## License

Business Source License 1.1 (BSL 1.1)
Converts to Apache 2.0 on April 25, 2030
Licensor: Wayforth LTD

Smart contracts: [WayforthEscrow on Base Sepolia](https://sepolia.basescan.org/address/0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809)
