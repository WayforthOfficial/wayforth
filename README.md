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

## Docs

- [Architecture Decisions](DECISIONS.md)
- API docs available at `http://localhost:8000/docs` when running

## Structure

```
wayforth/
├── apps/
│   ├── api/        # FastAPI service
│   └── crawler/    # Service crawler (Phase 2)
├── packages/
│   └── schema/     # Shared schemas
├── contracts/
│   └── base/       # Solidity contracts — Foundry (Phase 2)
└── infra/
    └── docker/     # Dev infrastructure
```
