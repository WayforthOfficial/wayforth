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
