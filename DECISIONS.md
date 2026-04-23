# Architecture Decision Records

## ADR-001: Core Stack Selection

**Date:** 2026-04-23  
**Status:** Accepted

### Decision

- **API:** Python + FastAPI
- **Storage:** PostgreSQL 16
- **Queues:** Redis 7
- **Smart Contracts:** Foundry (Phase 2, Base blockchain)

### Rationale

**FastAPI** is async-native (built on Starlette/asyncio), provides automatic OpenAPI docs, and has excellent type-safety via Pydantic. Critical for an agent-facing API that needs low-latency, well-documented endpoints.

**PostgreSQL 16** ships with `pg_trgm` for trigram-based full-text search — the backbone of Phase 1 service discovery. JSONB support handles semi-structured service metadata without a separate document store.

**Redis 7** for job queues (crawler pipeline) and caching. Lightweight, battle-tested, pairs well with async workers (ARQ/Celery).

**Foundry** for EVM smart contract development. Superior testing ergonomics vs Hardhat for Solidity; native Base (L2) support. Deferred to Phase 2 while the off-chain layer stabilizes.

**uv** as the Python package manager: 10–100× faster than pip, lockfile-first, handles Python version management.

### Trade-offs

- Python over Go/Rust: slower raw throughput, but faster iteration and richer ML/AI ecosystem for Phase 2 agent intelligence features.
- Postgres over a vector DB: avoids operational complexity in Phase 1; `pgvector` extension available if embeddings are needed later.

---

## ADR-002: Crawler Architecture

**Date:** 2026-04-23
**Status:** Accepted

### Decision

The crawler (`apps/crawler/`) is a **standalone Python project** (its own `uv` environment) that runs as a one-shot process rather than a long-lived service. It uses **asyncpg** directly (no ORM) and writes discovered services into the `services` table via `INSERT … ON CONFLICT DO UPDATE`.

Two crawl sources are implemented:

| Source | Strategy |
|---|---|
| `mcp_registry` | HTTP GET to `https://registry.mcp.so/api/servers`; parses JSON list; category defaults to `data` |
| `x402_bankr` | HTTP GET to `https://bankr.io/api/x402/services`; falls back to 5 realistic mock entries when the endpoint is unavailable |

Each source is its own async function. A failed fetch or failed upsert is logged and skipped — it never aborts the rest of the crawl.

### Rationale

- **Standalone project**: isolates crawler deps (feedparser, pyyaml, beautifulsoup4) from the API; each can evolve independently and be deployed/scheduled separately.
- **One-shot process**: simplest unit to run via cron, a Redis queue (ARQ), or a CI job. No persistent state needed in Phase 1.
- **asyncpg direct**: crawler is write-heavy and latency-tolerant; the ORM overhead of SQLAlchemy buys nothing here. The `xmax = 0` trick distinguishes inserts from updates in the `RETURNING` clause without a second round-trip.
- **Mock fallback**: lets the crawler produce realistic data during development and offline CI runs without depending on third-party availability.

### Trade-offs

- asyncpg requires JSON values to be passed as strings (`json.dumps`), unlike SQLAlchemy's codec layer — minor ergonomic cost, explicit in the code.
- No deduplication beyond `endpoint_url` uniqueness; if a service moves endpoints it will create a second row. Acceptable for Phase 1 where all sources are authoritative registries.
- MCP registry DNS may be unavailable in restricted environments; the crawl degrades gracefully to zero rows rather than failing.

---

## ADR-003: MCP Server Architecture

**Date:** 2026-04-23
**Status:** Accepted

### Decision

A dedicated MCP server (`packages/mcp-server/`) exposes three tools to any MCP-compatible client (Claude Code, Cursor, etc.):

| Tool | Purpose |
|---|---|
| `wayforth_search` | Intent-based keyword search with category and tier filters, returns top 5 |
| `wayforth_list` | Enumerate services, optionally filtered by category |
| `wayforth_status` | Catalog stats (counts by tier and category) and API health |

The server is a standalone `uv` project that calls the Wayforth REST API over HTTP rather than connecting to Postgres directly. Phase 1 search is keyword matching (token presence in name + description); semantic search is deferred to Phase 3.

### Why three tools and not one

Agents benefit from narrow, well-scoped tools. `search` handles discovery ("find me X"), `list` handles enumeration ("what's available in Y"), and `status` handles diagnostics ("is the catalog healthy"). Combining them into one tool would force the agent to parse ambiguous intent from a single string and would break tool-use caching.

### Why REST API rather than direct Postgres

- **Separation of concerns**: the MCP server is a thin client that stays decoupled from the data layer. Future schema changes, connection pooling, or auth middleware in the API require no MCP changes.
- **Operational simplicity**: the MCP process needs zero database credentials; it only needs `WAYFORTH_API_URL`. This is safer for end-users who install the server locally.
- **Reusability**: the same REST endpoint serves the web UI, the crawler, and now the MCP server — the source of truth is in one place.
- **Phase 1 scope**: the catalog is small (O(100) services). An extra HTTP hop adds <5 ms and is irrelevant at this scale.

### Why keyword matching in Phase 1

Full-text or vector search requires either Postgres `pg_trgm`/`tsvector` queries (which would push ranking logic into SQL) or an embedding model (operational cost, latency). Keyword matching on token presence is deterministic, requires no infrastructure, and is sufficient for the seeded catalog where names and descriptions are descriptive. Phase 3 introduces embeddings and `pgvector` once the catalog grows past ~1,000 services.

### Trade-offs

- The MCP server has no caching: every tool call hits the REST API. Acceptable for Phase 1 traffic; a short TTL in-process cache can be added when needed.
- Keyword ranking is order-insensitive and ignores synonyms — a search for "LLM" won't match "large language model". Mitigated in practice by descriptive service names and deferred to Phase 3 for a proper fix.
- Running `server.py` requires the Wayforth API to be up. The server handles this gracefully by returning a human-readable message with start instructions rather than raising an exception.
