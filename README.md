# Wayforth

**The runtime that keeps AI agents running in production.**

`v0.9.2`

One install gives an agent a single place to discover, use, and pay for thousands of services — and when one fails mid-run, Wayforth reroutes so the agent keeps going. Agents break in production. Yours won't.

## Why Wayforth

Agents demo well and fail in production. Real APIs rate-limit, expire keys, return garbage, and go down mid-task — and a long-running agent falls over the first time a dependency hiccups. Wayforth is the operational layer between your agent and that mess.

- **It reroutes itself** — when a service fails mid-run, Wayforth fails over to an interchangeable provider. Live today for search and model inference, expanding as coverage grows.
- **It stays on budget** — give any run a hard credit ceiling; the call that would exceed it is refused before it spends. No runaway loops.
- **One install, every service** — MCP-native, managed credentials, one balance. No per-service signups.
- **Speaks the standards** — MCP-native, and live A2A interop (signed agent card + JWKS + streaming).

## Quick start

```bash
# 1. Install the MCP server
uvx wayforth-mcp

# Add to Claude Code permanently
claude mcp add wayforth -- uvx wayforth-mcp

# 2. Get a key at wayforth.io/signup, then:
export WAYFORTH_API_KEY=wf_live_...
```

**Python**
```python
pip install wayforth-sdk
```
```python
from wayforth import Wayforth
client = Wayforth(api_key="wf_live_...")
results = client.search("real-time stock data")
```

**TypeScript**
```ts
import { WayforthClient } from "wayforth-sdk";
const wf = new WayforthClient("wf_live_...");
```

Full reference: gateway.wayforth.io/guide/

## What's live

- **Self-heal reliability** — automatic failover across interchangeable providers (search + model inference today).
- **Loop budgets** — per-run credit ceilings, enforced before spend, ledger-derived.
- **Hosted runtime** — deploy and run agents in isolated environments.
- **A2A interop** — the gateway serves a signed agent card and JWKS and supports streaming, verified against `a2a-sdk==0.3.26`.
- **Managed access** — a catalog of thousands of services across 19 categories, reachable through one install, with managed keys for a set of live managed services.
- **Multi-rail payments (built, in sandbox)** — designed to pay across card, USDC, and x402 under one balance. Live settlement is gated on incorporation.

## Pricing

| Plan | Credits/month | Price |
|---|---|---|
| Free | 100 | Free |
| Starter | 6,000 | $12/mo |
| Builder | 21,000 | $29/mo |
| Pro | 72,000 | $99/mo |
| Growth | 240,000 | $299/mo |
| Enterprise | 1,000,000 | Custom |

Details: wayforth.io/pricing

## On the roadmap

AP2 (verified payment authorization), live payment settlement across all rails, and expanding self-heal to more service categories.

## Links

- Quickstart — wayforth.io/quickstart
- API reference — gateway.wayforth.io/guide/
- For providers — wayforth.io/providers
- PyPI (MCP) — pypi.org/project/wayforth-mcp
- PyPI (SDK) — pypi.org/project/wayforth-sdk
- Contact — wayforth.io/contact

## License

Business Source License 1.1 — converts to Apache 2.0 on 2030-04-25.

© 2026 Wayforth Technologies Inc.
