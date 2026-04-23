# Wayforth

The search engine and payment rail for AI agents — discover services and pay in USDC, powered by Base.

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
- API docs available at `http://localhost:8000/docs` when running

## Structure

```
wayforth/
├── apps/
│   ├── api/        # FastAPI service
│   └── crawler/    # Service crawler
├── packages/
│   ├── mcp-server/ # MCP server (Claude Code / Cursor integration)
│   └── schema/     # Shared schemas
├── contracts/
│   └── base/       # Solidity contracts — Foundry (Phase 2)
└── infra/
    └── docker/     # Dev infrastructure
```
