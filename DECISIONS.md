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

---

## ADR-004: Claude Haiku for MCP Search Ranking

**Date:** 2026-04-23
**Status:** Accepted

### Decision

Replace Phase 1 keyword ranking in `wayforth_search` with a Claude Haiku call (`claude-haiku-4-5`) that returns services ranked by semantic relevance with a 0–100 score and one-sentence reason per result. Return top 3 results (down from 5). Degrade silently to keyword ranking if `ANTHROPIC_API_KEY` is absent or the API call fails.

### Rationale

- **Semantic understanding**: Haiku understands that "fast cheap inference" is relevant to a "Claude Sonnet pay-per-call" service even if neither word appears verbatim — a gap keyword matching cannot bridge.
- **Low cost**: Each ranking call processes ~10 service names/descriptions. At Haiku pricing (~$0.25/M input tokens), a call costs roughly $0.0001 — negligible.
- **Low latency**: Haiku is Anthropic's fastest model. A ranking call adds ~300–500 ms to search, acceptable for an agent tool that already awaits an HTTP fetch.
- **Graceful degradation**: If `ANTHROPIC_API_KEY` is unset or the call fails, the server falls back to token-count keyword ranking with no user-visible error. This preserves the Phase 1 reliability guarantee.
- **No infrastructure change**: All ranking logic stays in the MCP server process; no new services, vector DBs, or embedding pipelines are introduced.

### Trade-offs

- Adds Anthropic API dependency and a per-call cost (~$0.0001). Acceptable at Phase 1 catalog scale; revisit if call volume exceeds ~10,000/day.
- Haiku's ranking is non-deterministic — same query may return different orderings across calls. Acceptable for discovery; determinism can be enforced with a fixed seed if needed.
- Requires `ANTHROPIC_API_KEY` in the MCP server environment. Documented in `.env.example`; Claude Code sessions inherit it automatically.

---

## ADR-005: Real Crawler Sources — mcp-get.com Primary, Glama as Backup

**Date:** 2026-04-23
**Status:** Accepted

### Decision

Replace the mock/dead crawler sources with two real public JSON APIs:

| Source | URL | Role |
|---|---|---|
| mcp-get.com | `https://mcp-get.com/api/packages` | Primary — 15,937 packages, simple flat JSON array |
| Glama | `https://glama.ai/api/mcp/v1/servers` | Secondary — curated set with richer metadata |

Crawl the first 100 entries from mcp-get.com and one page (10) from Glama per run. Add a `categorize_service(name, description)` function that classifies each service into `inference`, `translation`, or `data` using keyword sets. Add `?category=` query parameter to `GET /services`.

### Why mcp-get.com as primary

- Returns a flat JSON array — no auth, no pagination complexity, no API key required
- 15,937 packages as of 2026-04-23 — the largest open catalog of MCP servers
- Each entry has `name`, `description`, `sourceUrl` (GitHub link), `homepage`, `vendor`, `runtime` — enough to populate the schema and run category inference
- Discovered via direct API probe; registry.mcp.so (original plan) was NXDOMAIN

### Why Glama as backup

- Different catalog with curated entries; provides diversification vs mcp-get
- Consistent JSON structure (`id`, `name`, `description`, `url`, `repository`) with cursor-based pagination for future multi-page crawls
- Discovered as a working alternative after Smithery's API returned HTML (no public JSON endpoint)

### Why keyword-based category inference

- Zero latency, zero API cost — runs inline during the crawl before any DB write
- Three categories is a coarse distinction; keyword matching on `{name} {description}` is accurate enough for O(100) entries and the seeded demo catalog
- Semantics at query time are handled by Claude Haiku (ADR-004), so the category label is a coarse filter, not a precision signal
- Revisit with an embedding model if category accuracy becomes a product requirement

### Trade-offs

- The 100-entry cap on mcp-get keeps each crawl run fast (<5 s) but leaves 15,800+ entries unprocessed; increase `_MCP_GET_LIMIT` or add pagination when the catalog needs to grow
- Keyword inference misclassifies packages with ambiguous names (e.g. a "language server" that's not about human language translation); acceptable at Phase 1 scale
- Glama returns only 10 entries per page; future multi-page support requires cursor-based pagination using `pageInfo.endCursor`

---

## ADR-006: Wayforth Labs — First-Party Tier 2 Service Layer

**Date:** 2026-04-23
**Status:** Accepted

### Decision

Add `apps/labs/` as a standalone `uv` FastAPI project on port 8001 exposing five first-party services, all seeded into `services` with `coverage_tier=2` and `source="wayforth_labs"`:

| Path | Category | Upstream |
|---|---|---|
| POST /translate | translation | MyMemory free API (1000 req/day, no key) |
| GET /weather | data | wttr.in JSON API (no key) |
| GET /stock | data | Yahoo Finance v8 chart (no key) |
| POST /summarize | inference | Pure Python extractive (no external call) |
| GET /search | data | ddg-api.herokuapp.com → DuckDuckGo Instant Answer fallback |

Each service is a `router = APIRouter()` in its own file under `services/`. All five are mounted in `main.py` via `include_router`. A `seed_services.py` script upserts all five into the `services` table using the same `ON CONFLICT (endpoint_url) DO UPDATE ... RETURNING (xmax = 0) AS inserted` pattern as the crawler.

### Rationale

- **Cold-start**: without Labs, the catalog has zero guaranteed-uptime services. An agent searching for "translation" might get no results or flaky Tier 0/1 entries.
- **Tier 2 from day one**: we control the endpoints, so uptime is under our SLA. Labs services are the reference baseline for evaluating Tier 0/1 entries.
- **Demo reliability**: demos and integration tests need at least one working service per category that will never 404. Labs provides this without mocking.
- **Reference implementations**: new providers onboarding to the catalog can inspect Labs code to see the expected request/response contract for each category.

### Why a separate `apps/labs/` project

Same reasoning as ADR-002 for the crawler: isolated dependencies, independent deployment, independent failure domain. Labs has zero production DB reads at runtime — adding it to `apps/api/` would couple two very different operational profiles (DB-backed REST catalog vs. external-API proxy).

### Trade-offs

- All free-tier upstreams (MyMemory, wttr.in, Yahoo Finance, DDG) have undocumented rate limits and may change response shapes without notice. Labs is explicitly demo-tier and not suitable as production dependencies.
- Yahoo Finance v8 is an unofficial endpoint; may require a paid provider long-term.
- MyMemory 1000 req/day quota is per-IP; shared dev environments may exhaust it (HTTP 429 returned). MyMemory also rejects `"auto"` as a source language — the service defaults `source_language="auto"` to `"en"` at the API call boundary.
- DDG Instant Answer returns empty results for most general web queries — it is an entity/disambiguation API, not a web search index. Named-entity lookups work best.
- `endpoint_url` values are `http://localhost:8001/...`. Re-run `seed_services.py` with the deployed host URL before any cloud deployment.
