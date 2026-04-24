# Wayforth

[![PyPI wayforth-sdk](https://img.shields.io/pypi/v/wayforth-sdk?label=wayforth-sdk&color=blue)](https://pypi.org/project/wayforth-sdk/)
[![PyPI wayforth-mcp](https://img.shields.io/pypi/v/wayforth-mcp?label=wayforth-mcp&color=blue)](https://pypi.org/project/wayforth-mcp/)
[![MCP Registry](https://img.shields.io/badge/MCP-Registry-purple)](https://registry.modelcontextprotocol.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![API](https://img.shields.io/badge/API-Live-green)](https://api-production-fd71.up.railway.app/docs)

The search engine and payment rail for AI agents — discover services and pay in USDC, powered by Base.

## Install

```bash
# Python SDK
pip install wayforth-sdk

# MCP server (for Claude Code / Cursor)
uvx wayforth-mcp
```

## Quick Start

**1. Start infrastructure**
```bash
docker compose -f infra/docker/docker-compose.dev.yml up -d
```

**2. Set up environment**
```bash
cp .env.example .env
```

**3. Start the API**
```bash
cd apps/api
uv run uvicorn main:app --reload --port 8000
```

**4. Verify**
```bash
curl http://localhost:8000/health
# {"status":"ok","service":"wayforth-api","version":"0.1.0"}
```

## Production API

**Base URL:** `https://api-production-fd71.up.railway.app`

```bash
curl https://api-production-fd71.up.railway.app/health
curl "https://api-production-fd71.up.railway.app/services?category=inference"
```

Swagger UI: `https://api-production-fd71.up.railway.app/docs`

## Semantic Search

Search 2,345+ AI services by natural language via the REST API — no MCP server required:

```bash
# Translate English to Spanish
curl "https://api-production-fd71.up.railway.app/search?q=translate+english+to+spanish"

# Fast inference, filtered by category
curl "https://api-production-fd71.up.railway.app/search?q=fast+inference&category=inference&limit=3"
```

Response includes `score` (0–100) and `reason` from Claude Haiku, with keyword fallback when the API key is absent.

## Use Wayforth from Claude Code

Install the MCP server with one command:

```bash
claude mcp add wayforth -- uv run --directory ~/Code/wayforth/packages/mcp-server python server.py
```

Then ask Claude anything like:
- *"Search Wayforth for translation services"*
- *"List all inference services on Wayforth"*
- *"What's in the Wayforth catalog?"*

See [`packages/mcp-server/README.md`](packages/mcp-server/README.md) for full install docs.

## Docs

- [Architecture Decisions](DECISIONS.md)
- MCP server: [`packages/mcp-server/`](packages/mcp-server/)
- API docs: `http://localhost:8000/docs` (local) or `https://api-production-fd71.up.railway.app/docs` (production)

## Structure

```
wayforth/
├── apps/
│   ├── api/        # FastAPI service
│   └── crawler/    # Service crawler
├── packages/
│   ├── sdk-python/     # Python SDK
│   ├── sdk-typescript/ # TypeScript/JavaScript SDK
│   ├── mcp-server/     # MCP server (Claude Code / Cursor integration)
│   └── schema/         # Shared schemas
├── contracts/
│   └── base/       # Solidity contracts — Foundry (Phase 2)
└── infra/
    └── docker/     # Dev infrastructure
```
