# Wayforth

[![PyPI wayforth-sdk](https://img.shields.io/pypi/v/wayforth-sdk?label=wayforth-sdk&color=blue)](https://pypi.org/project/wayforth-sdk/)
[![PyPI wayforth-mcp](https://img.shields.io/pypi/v/wayforth-mcp?label=wayforth-mcp&color=blue)](https://pypi.org/project/wayforth-mcp/)
[![MCP Registry](https://img.shields.io/badge/MCP-Registry-purple)](https://registry.modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![API](https://img.shields.io/badge/API-Live-green)](https://api-production-fd71.up.railway.app/docs)

The search engine and payment rail for AI agents.

## Install

```bash
uvx wayforth-mcp  # MCP server — works in Claude Code, Cursor, Windsurf
pip install wayforth-sdk  # Python SDK
npm install wayforth-sdk  # TypeScript/JavaScript SDK
```

## What it does

One endpoint where AI agents discover services and pay for them in USDC.

- **2,346 services** across inference, data, and translation
- **9 Tier 2** services — verified, executable, always up
- **Semantic search** powered by Claude Haiku
- **Coverage tiers** 0-3 signal which services are actually transactable

## Quick start (MCP)

Add to Claude Code:
```bash
claude mcp add wayforth -- uvx wayforth-mcp
```

Then ask Claude: "find me a fast inference API for coding tasks"

## Quick start (REST API)

```bash
# Semantic search
curl "https://api-production-fd71.up.railway.app/search?q=translate+to+spanish&limit=3"

# Catalog stats
curl "https://api-production-fd71.up.railway.app/stats"

# Browse services
curl "https://api-production-fd71.up.railway.app/services?category=inference&limit=10"
```

## Quick start (Python SDK)

```python
from wayforth import WayforthClient

client = WayforthClient()
results = client.search("fast inference for coding")
for r in results["results"]:
    print(r["name"], r["score"], r["endpoint_url"])
```

## Architecture

```
apps/api/          FastAPI + semantic search + Postgres
apps/crawler/      Crawls MCP registries, promotes tiers daily
apps/labs/         5 first-party Tier 2 services
packages/mcp-server/   wayforth-mcp (uvx wayforth-mcp)
packages/sdk-python/   wayforth-sdk (pip install wayforth-sdk)
packages/sdk-typescript/ TypeScript SDK
contracts/base/    Smart contracts (coming Month 2)
```

## Coverage tiers

- **Tier 0** — Discovered (indexed, not verified)
- **Tier 1** — Testnet (passed test transaction)
- **Tier 2** — Executable (99%+ uptime, verified end-to-end) ← default results
- **Tier 3** — Verified (KYB complete, SLA signed)

Spec: https://wayforth.io/spec/coverage-tiers/v1

## Links

- Website: https://wayforth.io
- API: https://api-production-fd71.up.railway.app
- API docs: https://api-production-fd71.up.railway.app/docs
- Coverage tier spec: https://wayforth.io/spec/coverage-tiers/v1
