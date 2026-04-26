import asyncio
import hashlib
import json as json_lib
import logging
import os
import secrets
import uuid as uuid_lib
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import httpx
import sentry_sdk
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address  # fallback only
from web3 import Web3

from chain import ESCROW_ADDRESS, PAYMENT_INFO, REGISTRY_ADDRESS, build_payment_calldata, get_chain_stats
from db import check_db
from notifications import send_submission_confirmation, send_tier3_application_notification, send_welcome_email
from ranker_client import rank_services

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        traces_sample_rate=0.1,
        environment=ENVIRONMENT,
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("wayforth")


async def log_query(pool, service_id: str, query_text: str, score: int):
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO service_queries (service_id, query_text, score) VALUES ($1, $2, $3)",
                service_id, query_text[:200], score,
            )
    except Exception as e:
        logger.error(f"Query log error: {e}")


async def _record_search(pool, q, results, session_id="", query_id=""):
    try:
        async with pool.acquire() as conn:
            is_return = False
            if session_id:
                prev = await conn.fetchval("""
                    SELECT COUNT(*) FROM search_analytics
                    WHERE session_id = $1 AND created_at < NOW() - INTERVAL '1 hour'
                """, session_id)
                is_return = prev > 0

            await conn.execute("""
                INSERT INTO search_analytics
                (id, query, results, top_result_id, result_count, rank_scores, session_id, created_at)
                VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, NOW())
            """,
                query_id or str(uuid_lib.uuid4()),
                q,
                json_lib.dumps([{"id": str(r.get("service_id", "")), "score": r.get("score", 0)} for r in results[:10]]),
                str(results[0].get("id", "")) if results else None,
                len(results),
                json_lib.dumps({str(r.get("service_id", "")): r.get("score", 0) for r in results[:10]}),
                session_id or None,
            )

            if is_return:
                logger.info(f"Return session: {session_id[:8]}")
    except Exception as e:
        logger.warning(f"search analytics write failed: {e}")


async def _record_payment(pool, service_id_hex: str, query_text=""):
    sid = service_id_hex.removeprefix("0x")
    try:
        async with pool.acquire() as conn:
            svc_uuid = await conn.fetchval(
                "SELECT id FROM services WHERE encode(sha256(endpoint_url::bytea), 'hex') = $1",
                sid,
            )
            if svc_uuid:
                await conn.execute("""
                    INSERT INTO search_outcomes
                    (query_text, service_id, outcome_type, created_at)
                    VALUES ($1, $2, 'payment_initiated', NOW())
                """, query_text, svc_uuid)
    except Exception as e:
        logger.warning(f"search outcome write failed: {e}")


async def _mark_search_converted(pool, query_id: str, service_id: str):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE search_analytics
                SET led_to_payment = TRUE, payment_service_id = $2::uuid
                WHERE id::text = $1
            """, query_id, service_id)
    except Exception as e:
        logger.warning(f"Failed to mark search converted: {e}")


async def _update_identity_search(pool, agent_id: str):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO agent_identities (agent_id, total_searches, last_active_at)
                VALUES ($1, 1, NOW())
                ON CONFLICT (agent_id) DO UPDATE
                SET total_searches = agent_identities.total_searches + 1,
                    last_active_at = NOW()
            """, agent_id)
    except Exception as e:
        logger.warning(f"Identity update failed: {e}")


async def _update_identity_payment(pool, agent_id: str, amount_usdc: float):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO agent_identities (agent_id, total_payments, total_spend_usdc, last_active_at, trust_score)
                VALUES ($1, 1, $2, NOW(), 55.0)
                ON CONFLICT (agent_id) DO UPDATE
                SET total_payments = agent_identities.total_payments + 1,
                    total_spend_usdc = agent_identities.total_spend_usdc + $2,
                    last_active_at = NOW(),
                    trust_score = LEAST(100, agent_identities.trust_score + 0.5)
            """, agent_id, amount_usdc)
    except Exception as e:
        logger.warning(f"Identity payment update failed: {e}")


async def _probe_new_service(service_id: str, endpoint_url: str):
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(endpoint_url)
            new_tier = 1 if r.status_code < 500 else 0
            async with app.state.pool.acquire() as db:
                await db.execute("""
                    UPDATE services
                    SET coverage_tier=$1, last_tested_at=NOW(), consecutive_failures=0
                    WHERE id=$2::uuid
                """, new_tier, service_id)
                logger.info(f"New service {service_id} probed: tier {new_tier} (status {r.status_code})")
    except Exception as e:
        logger.warning(f"New service probe failed for {service_id}: {e}")


_DB_URL = os.environ.get("DATABASE_URL", "")
_ASYNCPG_URL = _DB_URL.replace("postgresql+asyncpg://", "postgresql://")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Wayforth API starting, environment={ENVIRONMENT}")
    ok = check_db()
    if not ok:
        logger.warning("DB connection check failed — starting anyway")
    app.state.db_ok = ok
    try:
        app.state.pool = await asyncpg.create_pool(_ASYNCPG_URL, min_size=2, max_size=10)
        app.state.db_ok = True
    except Exception as e:
        logger.error(f"DB error: {e}")
        logger.warning(f"DB pool creation failed: {e} — /services will be unavailable")
        app.state.pool = None
    yield
    if app.state.pool:
        await app.state.pool.close()


app = FastAPI(
    title="Wayforth API",
    description="""
## The Search Engine and Payment Rail for AI Agents

Wayforth provides semantic service discovery and non-custodial payment routing for AI agents.

### Key Features
- **WayforthQL** — Declarative query language for agent service discovery
- **WayforthRank** — Proprietary multi-signal ranking engine
- **Coverage Tiers** — Automated reliability verification (0–3)
- **Non-custodial payments** — Agent signs, Wayforth routes, Base settles

### Authentication
Most endpoints are open with per-IP rate limits.
Add `X-Wayforth-API-Key: wf_free_...` header for higher limits.

### Quick Start
```bash
uvx wayforth-mcp
```
""",
    version="0.1.5",
    contact={"name": "Wayforth", "url": "https://wayforth.io", "email": "hello@wayforth.io"},
    license_info={"name": "BSL 1.1", "url": "https://wayforth.io/license"},
    lifespan=lifespan,
)

def get_real_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_real_ip)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://wayforth.io",
        "https://www.wayforth.io",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8080",
        "*",  # MCP server calls from any agent runtime
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    import uuid
    request_id = str(uuid.uuid4())
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if raw_key:
        request.state.api_key = raw_key
    response = await call_next(request)
    response.headers["X-Wayforth-Request-ID"] = request_id
    response.headers["X-Wayforth-Version"] = "0.1.5"
    response.headers["X-RateLimit-Tier"] = str(getattr(request.state, "rate_limit_tier", "free"))
    response.headers["X-RateLimit-Limit"] = str(getattr(request.state, "rate_limit_rpm", "10"))
    return response


async def get_db(request: Request):
    async with request.app.state.pool.acquire() as conn:
        yield conn


@app.get("/health")
@limiter.limit("60/minute")
def health(request: Request):
    db_status = "ok" if getattr(app.state, "db_ok", False) else "unavailable"
    return {"status": "ok", "service": "wayforth-api", "version": "0.1.5", "db_status": db_status}


@app.get("/status", tags=["System"])
async def system_status(db=Depends(get_db)):
    """Public system status — uptime, service count, last health check."""
    stats = await db.fetchrow("""
        SELECT
            COUNT(*) FILTER (WHERE coverage_tier >= 2) as tier2_services,
            COUNT(*) as total_services,
            COUNT(*) FILTER (WHERE coverage_tier >= 3) as tier3_services
        FROM services
    """)
    searches = await db.fetchval("""
        SELECT COUNT(*) FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '24h'
    """)
    return {
        "status": "operational",
        "version": "0.1.5",
        "services": {
            "total": stats["total_services"],
            "tier2": stats["tier2_services"],
            "tier3": stats["tier3_services"],
        },
        "searches_24h": searches,
        "api": "operational",
        "database": "operational",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/chain")
@limiter.limit("10/minute")
def chain_info(request: Request):
    """On-chain contract addresses, stats, and supported payment protocols"""
    chain = get_chain_stats()
    return {
        "wayforth_escrow": {
            "address": chain.get("escrow"),
            "fee_pct": chain.get("fee_pct"),
            "use_for": "services without x402 support",
            **{k: v for k, v in chain.items() if k not in ("escrow", "fee_pct")},
        },
        "x402": {
            "protocol": "HTTP 402",
            "fee_pct": 0,
            "use_for": "services with native x402 support",
            "docs": "https://x402.org",
        },
    }


def compute_wri(service: dict, rank_score: float, popularity_boost: float = 0.0, payment_boost: float = 0.0) -> float:
    """WRI v2 — composite reliability score with popularity and payment signals. Range: 0-100."""
    score = rank_score * 0.5
    tier = service.get("coverage_tier", 0)
    if tier >= 2:
        score += 20
    elif tier >= 1:
        score += 5
    last_tested = service.get("last_tested_at")
    if last_tested:
        from datetime import datetime, timezone, timedelta
        try:
            if isinstance(last_tested, str):
                from dateutil.parser import parse
                last_tested = parse(last_tested)
            if last_tested.tzinfo is None:
                last_tested = last_tested.replace(tzinfo=timezone.utc)
            if last_tested > datetime.now(timezone.utc) - timedelta(hours=24):
                score += 10
        except Exception:
            pass
    if service.get("consecutive_failures", 1) == 0:
        score += 10
    if service.get("payment_protocol") == "x402":
        score += 5
    score += min(popularity_boost, 5.0)
    score += min(payment_boost, 8.0)
    return round(min(score, 100), 1)


@app.get(
    "/search",
    summary="Semantic service search",
    description=(
        "Rank Wayforth services by relevance to a natural language query using Claude Haiku. "
        "Falls back to keyword scoring when ANTHROPIC_API_KEY is not set."
    ),
)
@limiter.limit("10/minute")
async def search_services(
    request: Request,
    q: str = Query(description="Natural language query, e.g. 'fast cheap inference for coding'"),
    category: str | None = Query(default=None, description="Filter by category: inference, data, translation, …"),
    tier: int | None = Query(default=None, description="Filter by exact coverage tier (0=free, 1=basic, 2=standard, 3=premium)"),
    limit: int = Query(default=5, ge=1, le=20, description="Number of results to return (1–20)"),
    session_id: str = Query(default="", description="Optional agent session ID for return-visit tracking"),
    agent_id: str = Query(default="", description="Optional agent identity ID for reputation tracking"),
    db=Depends(get_db),
):
    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, name, description, endpoint_url, category,
                       coverage_tier, pricing_usdc, source, payment_protocol, created_at,
                       last_tested_at, consecutive_failures
                FROM services
                WHERE ($1::text IS NULL OR category = $1)
                  AND ($2::int IS NULL OR coverage_tier = $2)
                ORDER BY created_at DESC
                """,
                category,
                tier,
            )
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    services = [dict(r) for r in rows]
    ranked = await rank_services(q, services, db=db)
    top = ranked[:limit]

    fallback_used = False
    fallback_reason = None
    if not top:
        try:
            async with app.state.pool.acquire() as conn:
                fb_rows = await conn.fetch(
                    """
                    SELECT id, name, description, endpoint_url, category,
                           coverage_tier, pricing_usdc, source, payment_protocol,
                           last_tested_at, consecutive_failures
                    FROM services
                    WHERE coverage_tier >= 0
                      AND (name ILIKE $1 OR description ILIKE $1 OR category ILIKE $1)
                    ORDER BY coverage_tier DESC LIMIT 50
                    """,
                    f"%{q}%",
                )
            if fb_rows:
                fb_ranked = await rank_services(q, [dict(r) for r in fb_rows], db=db)
                top = fb_ranked[:limit]
                fallback_used = True
                fallback_reason = "No Tier 2 results — showing all tiers"
        except Exception:
            pass

    query_id = str(uuid_lib.uuid4())
    pool = app.state.pool
    if ranked and pool:
        asyncio.create_task(log_query(pool, str(ranked[0]["id"]), q, ranked[0].get("score", 0)))
    if pool:
        asyncio.create_task(_record_search(pool, q, ranked, session_id, query_id))
    if pool and agent_id:
        asyncio.create_task(_update_identity_search(pool, agent_id))
    popular_ids: dict = {}
    payment_ids: dict = {}
    try:
        async with app.state.pool.acquire() as conn:
            pop_rows = await conn.fetch("""
                SELECT top_result_id, COUNT(*) as c
                FROM search_analytics
                WHERE created_at > NOW() - INTERVAL '7 days'
                  AND top_result_id IS NOT NULL
                GROUP BY top_result_id
                ORDER BY c DESC LIMIT 50
            """)
            max_count = max((r["c"] for r in pop_rows), default=1)
            popular_ids = {str(r["top_result_id"]): (r["c"] / max_count) * 5 for r in pop_rows}

            pay_rows = await conn.fetch("""
                SELECT service_id, COUNT(*) as c
                FROM search_outcomes
                WHERE outcome_type = 'payment_initiated'
                  AND created_at > NOW() - INTERVAL '7 days'
                  AND service_id IS NOT NULL
                GROUP BY service_id ORDER BY c DESC LIMIT 50
            """)
            max_pay = max((r["c"] for r in pay_rows), default=1)
            payment_ids = {str(r["service_id"]): (r["c"] / max_pay) * 8 for r in pay_rows}
    except Exception:
        pass

    logger.info(f"search q={q!r} results={len(top)} fallback={fallback_used}")
    results = [
        {
            "name": s.get("name"),
            "score": s.get("score", 0),
            "wri": compute_wri(s, s.get("score", 0), popularity_boost=popular_ids.get(str(s.get("id")), 0.0), payment_boost=payment_ids.get(str(s.get("id")), 0.0)),
            "reason": s.get("reason", ""),
            "coverage_tier": s.get("coverage_tier"),
            "category": s.get("category"),
            "endpoint_url": s.get("endpoint_url"),
            "pricing_usdc": s.get("pricing_usdc"),
            "payment_protocol": s.get("payment_protocol", "wayforth"),
            "service_id": "0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest(),
            "wayforth_id": f"wayforth://{s.get('name','').lower().replace(' ','_').replace('/','_')[:30]}/{hashlib.sha256(s.get('endpoint_url','').encode()).hexdigest()[:8]}",
            "payment": PAYMENT_INFO,
        }
        for s in top
    ]
    return {
        "query_id": query_id,
        "query": q,
        "total_results": len(top),
        "results": results,
        "fallback": fallback_used,
        "fallback_reason": fallback_reason,
    }


@app.get("/search/suggestions")
@limiter.limit("30/minute")
async def search_suggestions(request: Request, db=Depends(get_db)):
    """Top queries from real agent usage — helps agents discover what's searchable."""
    rows = await db.fetch("""
        SELECT query, COUNT(*) as count
        FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '7 days'
        AND query IS NOT NULL AND query != ''
        GROUP BY query ORDER BY count DESC LIMIT 20
    """)
    if not rows:
        return {
            "suggestions": [
                "fast inference for coding",
                "translate to spanish",
                "real-time stock prices",
                "web search for agents",
                "image generation",
                "weather data",
                "text summarization",
                "cryptocurrency prices"
            ],
            "source": "curated"
        }
    return {
        "suggestions": [r['query'] for r in rows],
        "source": "real_usage",
        "period": "7d"
    }


class WayforthQLQuery(BaseModel):
    query: str
    tier_min: int | None = 2
    price_max: float | None = None
    uptime_min: float | None = None  # reserved — no column yet
    category: str | None = None
    protocol: str | None = None       # 'wayforth' | 'x402' | 'any'
    exclude_ids: list[str] | None = []  # service_id SHA256 hashes to exclude
    sort_by: str | None = "wri"       # 'wri' | 'score' | 'price' | 'tier'
    limit: int | None = 5
    with_payment_calldata: bool | None = False
    with_similar: bool | None = False  # include similar services for top result


@app.post("/query")
@limiter.limit("10/minute")
async def wayforthql(request: Request, body: WayforthQLQuery):
    """WayforthQL — declarative query language for agent service discovery."""
    conditions = ["coverage_tier >= $1"]
    params: list = [body.tier_min if body.tier_min is not None else 0]
    idx = 2

    if body.price_max is not None:
        conditions.append(f"(pricing_usdc IS NULL OR pricing_usdc <= ${idx})")
        params.append(body.price_max)
        idx += 1

    if body.category:
        conditions.append(f"category = ${idx}")
        params.append(body.category)
        idx += 1

    if body.protocol and body.protocol != "any":
        conditions.append(f"payment_protocol = ${idx}")
        params.append(body.protocol)
        idx += 1

    where = " AND ".join(conditions)
    limit = min(body.limit or 5, 20)

    try:
        async with request.app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id, name, description, endpoint_url, category,
                       pricing_usdc, coverage_tier, source, payment_protocol,
                       last_tested_at, consecutive_failures
                FROM services
                WHERE {where}
                ORDER BY coverage_tier DESC
                LIMIT {limit * 4}
                """,
                *params,
            )
    except Exception as e:
        logger.error(f"DB error in /query: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not rows:
        return {"query": body.query, "results": [], "total": 0, "protocol": "WayforthQL/1.0"}

    candidates = [dict(r) for r in rows]
    ranked = await rank_services(body.query, candidates)

    # Secondary sort before slicing
    if body.sort_by == "price":
        ranked.sort(key=lambda s: (s.get("pricing_usdc") is None, s.get("pricing_usdc") or 0))
    elif body.sort_by == "tier":
        ranked.sort(key=lambda s: s.get("coverage_tier", 0), reverse=True)

    # Exclude specific service IDs
    if body.exclude_ids:
        exclude_set = set(body.exclude_ids)
        results_raw = [
            s for s in ranked[:limit * 2]
            if ("0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest()) not in exclude_set
        ][:limit]
    else:
        results_raw = ranked[:limit]

    results = []
    for s in results_raw:
        service_id = "0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest()
        name_slug = s.get("name", "").lower().replace(" ", "_").replace("/", "_")[:30]
        entry = {
            "name": s.get("name"),
            "score": s.get("score", 0),
            "wri": compute_wri(s, s.get("score", 0)),
            "reason": s.get("reason", ""),
            "coverage_tier": s.get("coverage_tier"),
            "category": s.get("category"),
            "endpoint_url": s.get("endpoint_url"),
            "pricing_usdc": s.get("pricing_usdc"),
            "payment_protocol": s.get("payment_protocol", "wayforth"),
            "service_id": service_id,
            "wayforth_id": f"wayforth://{name_slug}/{service_id[2:10]}",
        }
        if body.with_payment_calldata:
            entry["payment"] = PAYMENT_INFO
        results.append(entry)

    # Attach similar services for top result when requested
    if body.with_similar and results_raw:
        top_id = str(results_raw[0].get("id", ""))
        try:
            async with request.app.state.pool.acquire() as conn:
                graph_rows = await conn.fetch(
                    """
                    SELECT
                        CASE WHEN service_a_id = $1 THEN service_b_id ELSE service_a_id END AS related_id,
                        co_search_count
                    FROM service_graph
                    WHERE service_a_id = $1 OR service_b_id = $1
                    ORDER BY co_search_count DESC LIMIT 5
                    """,
                    top_id,
                )
                similar = []
                for gr in graph_rows:
                    svc = await conn.fetchrow(
                        "SELECT name, category, coverage_tier FROM services WHERE id::text = $1",
                        gr["related_id"],
                    )
                    if svc:
                        similar.append({
                            "service_id": gr["related_id"],
                            "name": svc["name"],
                            "category": svc["category"],
                            "tier": svc["coverage_tier"],
                            "co_search_count": gr["co_search_count"],
                        })
            results[0]["similar_services"] = similar
        except Exception as e:
            logger.warning(f"with_similar failed: {e}")

    return {
        "query": body.query,
        "results": results,
        "total": len(results),
        "protocol": "WayforthQL/1.0",
        "filters_applied": {
            "tier_min": body.tier_min,
            "price_max": body.price_max,
            "category": body.category,
            "protocol": body.protocol,
            "sort_by": body.sort_by,
            "exclude_ids": body.exclude_ids or [],
        },
    }


@app.get("/services")
@limiter.limit("20/minute")
async def list_services(
    request: Request,
    category: str = None,
    tier: int = None,
    protocol: str = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    sort: str = "tier",
    db=Depends(get_db),
):
    conditions = ["1=1"]
    params = []
    idx = 1

    if category:
        conditions.append(f"category = ${idx}")
        params.append(category)
        idx += 1
    if tier is not None:
        conditions.append(f"coverage_tier >= ${idx}")
        params.append(tier)
        idx += 1
    if protocol:
        conditions.append(f"payment_protocol = ${idx}")
        params.append(protocol)
        idx += 1

    order = "coverage_tier DESC, name ASC" if sort == "tier" else "name ASC"

    try:
        rows = await db.fetch(f"""
            SELECT id, name, description, endpoint_url, category,
                   pricing_usdc, coverage_tier, payment_protocol, source, created_at
            FROM services
            WHERE {' AND '.join(conditions)}
            ORDER BY {order}
            LIMIT ${idx} OFFSET ${idx + 1}
        """, *params, min(limit, 100), offset)

        total = await db.fetchval(f"""
            SELECT COUNT(*) FROM services WHERE {' AND '.join(conditions)}
        """, *params)
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    return {
        "services": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "filters": {"category": category, "tier": tier, "protocol": protocol},
    }


@app.get("/services/categories")
@limiter.limit("20/minute")
async def list_categories(request: Request, db=Depends(get_db)):
    """All service categories with counts."""
    try:
        rows = await db.fetch("""
            SELECT category, COUNT(*) as count,
                   COUNT(*) FILTER (WHERE coverage_tier >= 2) as tier2_count
            FROM services
            WHERE category IS NOT NULL
            GROUP BY category ORDER BY count DESC
        """)
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {"categories": [dict(r) for r in rows], "total": len(rows)}


@app.get("/services/featured")
@limiter.limit("30/minute")
async def featured_services(request: Request, db=Depends(get_db)):
    """Featured services — one per category, Tier 2 only, best WRI score. Powers the homepage inline search default state."""
    try:
        rows = await db.fetch("""
            WITH ranked AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY category ORDER BY coverage_tier DESC, name ASC
                ) as rn
                FROM services
                WHERE coverage_tier >= 2
            )
            SELECT name, description, category, pricing_usdc,
                   coverage_tier, payment_protocol,
                   encode(sha256(endpoint_url::bytea), 'hex') as service_id
            FROM ranked WHERE rn = 1
            ORDER BY category
        """)
    except Exception as e:
        logger.error(f"DB error in featured_services: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {
        "featured": [dict(r) for r in rows],
        "total": len(rows),
        "note": "One Tier 2 verified service per category",
    }


@app.get("/stats")
@limiter.limit("30/minute")
async def get_stats(request: Request):
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT
                    COUNT(*) as total,
                    COUNT(*) FILTER (WHERE coverage_tier >= 2) as tier2,
                    COUNT(*) FILTER (WHERE coverage_tier >= 3) as tier3,
                    COUNT(*) FILTER (WHERE payment_protocol = 'x402') as x402_count,
                    COUNT(DISTINCT category) as categories
                FROM services
            """)
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    return {
        "total_services": row["total"],
        "tier2_services": row["tier2"],
        "tier3_services": row["tier3"],
        "x402_services": row["x402_count"],
        "categories": row["categories"],
        "mcp_tools": 9,
        "api_version": "0.1.5",
    }


@app.get("/health-report")
@limiter.limit("10/minute")
async def health_report(request: Request):
    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT name, consecutive_failures, last_tested_at
                FROM services WHERE coverage_tier = 2
                ORDER BY name
                """
            )
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    services = [
        {
            "name": r["name"],
            "status": "up" if (r["consecutive_failures"] or 0) == 0 else "degraded",
            "consecutive_failures": r["consecutive_failures"] or 0,
            "last_checked": r["last_tested_at"].isoformat() if r["last_tested_at"] else None,
        }
        for r in rows
    ]
    return {
        "tier2_services": services,
        "total_tier2": len(services),
        "all_healthy": all(s["status"] == "up" for s in services),
    }


@app.get("/leaderboard")
@limiter.limit("20/minute")
async def leaderboard(
    request: Request,
    limit: int = Query(default=10, ge=1, le=50),
    period: str = Query(default="7d", description="Time window: 1d, 7d, 30d"),
):
    days = 7
    if period.endswith("d"):
        try:
            days = max(1, min(int(period[:-1]), 90))
        except ValueError:
            pass

    try:
        async with app.state.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT
                    s.name,
                    s.category,
                    s.coverage_tier,
                    s.payment_protocol,
                    encode(sha256(s.endpoint_url::bytea), 'hex') as service_id,
                    COUNT(DISTINCT sa.id) as search_count,
                    COUNT(DISTINCT so.id) as payment_count
                FROM services s
                LEFT JOIN search_analytics sa
                    ON sa.top_result_id = s.id
                    AND sa.created_at > NOW() - ($2 * INTERVAL '1 day')
                LEFT JOIN search_outcomes so
                    ON so.service_id = s.id
                    AND so.outcome_type = 'payment_initiated'
                    AND so.created_at > NOW() - ($2 * INTERVAL '1 day')
                GROUP BY s.name, s.category, s.coverage_tier, s.payment_protocol, s.endpoint_url
                ORDER BY coverage_tier DESC, search_count DESC, payment_count DESC
                LIMIT $1
            """, limit, days)

            if not rows or all(r["search_count"] == 0 and r["payment_count"] == 0 for r in rows):
                rows = await conn.fetch("""
                    SELECT name, category, coverage_tier, payment_protocol,
                           encode(sha256(endpoint_url::bytea), 'hex') as service_id,
                           0 as search_count, 0 as payment_count
                    FROM services WHERE coverage_tier >= 2
                    ORDER BY coverage_tier DESC, name ASC LIMIT $1
                """, limit)

            total_queries = await conn.fetchval(
                "SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - ($1 * INTERVAL '1 day')",
                days,
            )
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    return {
        "period": period,
        "total_queries": total_queries,
        "leaderboard": [
            {
                "rank": i + 1,
                "name": r["name"],
                "search_count": r["search_count"],
                "payment_count": r["payment_count"],
                "category": r["category"],
                "coverage_tier": r["coverage_tier"],
                "payment_protocol": r["payment_protocol"],
                "service_id": "0x" + r["service_id"],
            }
            for i, r in enumerate(rows)
        ],
    }


class PayRequest(BaseModel):
    service_id: str
    service_owner: str
    amount_usdc: float
    query_id: str = ""
    agent_id: str = ""


class SubmitRequest(BaseModel):
    name: str
    description: str
    endpoint_url: str
    category: str
    pricing_usdc: float = 0.0
    contact_email: str | None = None


class MemoryItem(BaseModel):
    service_id: str
    service_name: str
    note: str = ""
    agent_id: str = ""


class Tier3Application(BaseModel):
    service_name: str
    company_name: str
    contact_email: str
    website: str = ""
    endpoint_url: str
    monthly_volume_usdc: float = 0.0
    sla_uptime_target: float = 99.9


class WebhookRegistration(BaseModel):
    service_id: str
    webhook_url: str
    contact_email: str
    events: list[str] = ["tier_change", "health_alert"]


class AgentIdentityRequest(BaseModel):
    agent_id: str
    display_name: str = ""


@app.post("/pay")
@limiter.limit("20/minute")
async def pay(request: Request, req: PayRequest):
    if req.amount_usdc <= 0.000066:
        raise HTTPException(
            status_code=400,
            detail="amount_usdc must be > 0.000066 (minimum to generate non-zero fee)",
        )
    if not Web3.is_address(req.service_owner):
        raise HTTPException(status_code=400, detail="service_owner is not a valid Ethereum address")
    sid = req.service_id.removeprefix("0x")
    if len(sid) != 64 or not all(c in "0123456789abcdefABCDEF" for c in sid):
        raise HTTPException(
            status_code=400,
            detail="service_id must be a valid bytes32 hex string (0x + 64 hex chars)",
        )
    logger.info(f"pay amount={req.amount_usdc} service={req.service_id[:10]}")
    result = build_payment_calldata(req.service_id, req.service_owner, req.amount_usdc)
    if app.state.pool:
        asyncio.create_task(_record_payment(app.state.pool, req.service_id))
    if app.state.pool and req.query_id:
        asyncio.create_task(_mark_search_converted(app.state.pool, req.query_id, req.service_id))
    if app.state.pool and req.agent_id:
        asyncio.create_task(_update_identity_payment(app.state.pool, req.agent_id, req.amount_usdc))
    fee_bps = 150
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if raw_key and app.state.pool:
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        async with app.state.pool.acquire() as conn:
            key_row = await conn.fetchrow(
                "SELECT tier FROM api_keys WHERE key_hash=$1 AND active=TRUE", key_hash
            )
            if key_row:
                fee_bps = TIER_LIMITS.get(key_row["tier"], {}).get("fee_bps", 150)
    result["fee_bps"] = fee_bps
    result["fee_pct"] = fee_bps / 100
    result["payment"] = {"fee_bps": fee_bps}
    return result


@app.post("/submit")
@limiter.limit("5/minute")
async def submit_service(request: Request, req: SubmitRequest):
    if not req.endpoint_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="endpoint_url must start with https://")
    if req.category not in ("inference", "data", "translation"):
        raise HTTPException(status_code=400, detail="category must be one of: inference, data, translation")
    if len(req.name) > 100:
        raise HTTPException(status_code=400, detail="name must be 100 characters or fewer")
    if len(req.description) > 500:
        raise HTTPException(status_code=400, detail="description must be 500 characters or fewer")
    if app.state.pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        async with app.state.pool.acquire() as conn:
            service_id = await conn.fetchval(
                """INSERT INTO services (name, description, endpoint_url, category, pricing_usdc, source, coverage_tier)
                   VALUES ($1, $2, $3, $4, $5, 'submitted', 0) RETURNING id""",
                req.name, req.description, req.endpoint_url, req.category, req.pricing_usdc,
            )
            await conn.execute(
                """INSERT INTO service_submissions (service_id, contact_email, ip_address)
                   VALUES ($1, $2, $3)""",
                service_id, req.contact_email, get_real_ip(request),
            )
        logger.info(f"submit name={req.name!r} category={req.category}")
        asyncio.create_task(_probe_new_service(str(service_id), req.endpoint_url))
        if req.contact_email:
            asyncio.create_task(asyncio.to_thread(
                send_submission_confirmation,
                req.contact_email, req.name, str(service_id), req.endpoint_url,
            ))
        await asyncio.sleep(3)
        async with app.state.pool.acquire() as conn2:
            service = await conn2.fetchrow("""
                SELECT coverage_tier, last_tested_at, consecutive_failures
                FROM services WHERE id = $1::uuid
            """, str(service_id))
        tier = service["coverage_tier"] if service else 0
        return {
            "status": "submitted",
            "service_id": str(service_id),
            "name": req.name,
            "initial_tier": tier,
            "message": f"Service submitted and probed. Current tier: {tier}. Tier 2 requires 90%+ uptime over 7 days.",
            "leaderboard_url": "https://wayforth.io/leaderboard",
            "tier3_url": "https://wayforth.io/tier3",
        }
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="A service with this endpoint URL already exists")
    except Exception as e:
        logger.error(f"Submit error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.get("/services/{service_id}")
@limiter.limit("30/minute")
async def get_service(request: Request, service_id: str):
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, description, endpoint_url, category,
                       coverage_tier, pricing_usdc, source, payment_protocol, created_at
                FROM services WHERE id = $1
                """,
                service_id,
            )
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    if row is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return dict(row)


@app.get("/admin/stats")
@limiter.limit("20/minute")
async def admin_stats(request: Request, key: str = ""):
    if not ADMIN_KEY or key != ADMIN_KEY:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        async with app.state.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM services")
            tier_rows = await conn.fetch(
                "SELECT coverage_tier, COUNT(*) AS cnt FROM services GROUP BY coverage_tier"
            )
            category_rows = await conn.fetch(
                "SELECT category, COUNT(*) AS cnt FROM services GROUP BY category"
            )
            queries_today = await conn.fetchval(
                "SELECT COUNT(*) FROM service_queries WHERE queried_at > NOW() - INTERVAL '1 day'"
            )
            queries_week = await conn.fetchval(
                "SELECT COUNT(*) FROM service_queries WHERE queried_at > NOW() - INTERVAL '7 days'"
            )
            top_rows = await conn.fetch(
                """
                SELECT s.name, s.category, s.coverage_tier, s.endpoint_url,
                       COUNT(q.id) AS query_count, ROUND(AVG(q.score), 1) AS avg_score
                FROM services s
                JOIN service_queries q ON s.id = q.service_id
                WHERE q.queried_at > NOW() - INTERVAL '7 days'
                GROUP BY s.id
                ORDER BY query_count DESC
                LIMIT 10
                """
            )
            sub_total = await conn.fetchval("SELECT COUNT(*) FROM service_submissions")
            sub_rows = await conn.fetch(
                """
                SELECT ss.contact_email, ss.submitted_at, ss.ip_address, s.name AS service_name
                FROM service_submissions ss
                JOIN services s ON ss.service_id = s.id
                ORDER BY ss.submitted_at DESC
                LIMIT 10
                """
            )
            platform = await conn.fetchrow("""
                SELECT
                    (SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '24h') as searches_24h,
                    (SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '7d') as searches_7d,
                    (SELECT COUNT(*) FROM search_outcomes WHERE outcome_type='payment_initiated') as total_payments,
                    (SELECT COUNT(*) FROM tier3_applications WHERE kyb_status='pending') as pending_tier3,
                    (SELECT COUNT(*) FROM api_keys WHERE active=TRUE) as active_api_keys,
                    (SELECT COUNT(*) FROM agent_identities) as registered_agents,
                    (SELECT COUNT(*) FROM provider_webhooks WHERE active=TRUE) as active_webhooks
            """)
    except Exception as e:
        logger.error(f"Admin stats DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    by_tier = {str(t): 0 for t in range(4)}
    for r in tier_rows:
        by_tier[str(r["coverage_tier"])] = r["cnt"]

    return {
        "services": {
            "total": total,
            "by_tier": by_tier,
            "by_category": {r["category"]: r["cnt"] for r in category_rows},
        },
        "queries": {
            "today": queries_today,
            "week": queries_week,
            "top_services": [
                {
                    "name": r["name"],
                    "query_count": r["query_count"],
                    "avg_score": r["avg_score"],
                    "category": r["category"],
                    "coverage_tier": r["coverage_tier"],
                    "endpoint_url": r["endpoint_url"],
                }
                for r in top_rows
            ],
        },
        "submissions": {
            "total": sub_total,
            "recent": [
                {
                    "service_name": r["service_name"],
                    "contact_email": r["contact_email"],
                    "submitted_at": r["submitted_at"].isoformat() + "Z",
                    "ip_address": r["ip_address"],
                }
                for r in sub_rows
            ],
        },
        "platform": {
            "active_api_keys": platform["active_api_keys"],
            "registered_agents": platform["registered_agents"],
            "active_webhooks": platform["active_webhooks"],
            "pending_tier3_applications": platform["pending_tier3"],
        },
        "usage": {
            "searches_24h": platform["searches_24h"],
            "searches_7d": platform["searches_7d"],
            "total_payments": platform["total_payments"],
        },
        "infrastructure": {
            "api": "healthy",
            "db": "healthy" if getattr(app.state, "db_ok", False) else "unavailable",
            "sentry": "connected" if SENTRY_DSN else "not configured",
            "contracts": {
                "registry": REGISTRY_ADDRESS,
                "escrow": ESCROW_ADDRESS,
            },
        },
    }


@app.get("/admin/health")
@limiter.limit("5/minute")
async def admin_health(request: Request, key: str = "", db=Depends(get_db)):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

    checks = {}

    try:
        await db.fetchval("SELECT 1")
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"

    for table in ["services", "search_analytics", "search_outcomes",
                  "agent_identities", "api_keys", "service_score_history"]:
        try:
            count = await db.fetchval(f"SELECT COUNT(*) FROM {table}")
            checks[table] = count
        except Exception:
            checks[table] = "error"

    recent = await db.fetchval("""
        SELECT COUNT(*) FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '1 hour'
    """)
    checks["searches_last_hour"] = recent

    return {
        "status": "operational" if checks["database"] == "ok" else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


@app.get("/admin")
async def admin_page(key: str = ""):
    if not ADMIN_KEY or key != ADMIN_KEY:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return FileResponse("static/admin.html")


@app.get("/demo")
async def demo():
    return FileResponse("static/demo.html")


@app.get("/leaderboard-page")
async def leaderboard_page():
    return FileResponse("static/leaderboard.html")


@app.get("/submit-page")
async def submit_page():
    return FileResponse("static/submit.html")


@app.get("/agent-demo")
async def agent_demo():
    return FileResponse("static/agent-demo.html")


@app.get("/wayforthql-spec", include_in_schema=False)
async def wayforthql_spec():
    return FileResponse("static/wayforthql.html")


@app.get("/roadmap", include_in_schema=False)
async def roadmap():
    return FileResponse("static/roadmap.html")


@app.get("/changelog", include_in_schema=False)
async def changelog_page():
    return FileResponse("static/changelog.html")


@app.get("/pricing", include_in_schema=False)
async def pricing_page():
    return FileResponse("static/pricing.html")


@app.get("/pricing/json")
@limiter.limit("30/minute")
async def pricing_json(request: Request):
    """Machine-readable pricing data."""
    return {
        "tiers": [
            {
                "name": "Free",
                "price_usd": 0,
                "price_monthly_usd": 0,
                "rate_limit_per_minute": 10,
                "monthly_quota": 1000,
                "routing_fee_pct": 1.5,
                "routing_fee_bps": 150,
                "features": ["search", "query", "pay", "services", "memory", "identity"],
                "cta": "Get Free Key",
                "cta_url": "https://api-production-fd71.up.railway.app/keys/create",
            },
            {
                "name": "Starter",
                "price_monthly_usd": 29,
                "rate_limit_per_minute": 30,
                "monthly_quota": 10000,
                "routing_fee_pct": 1.25,
                "routing_fee_bps": 125,
                "features": ["search", "query", "pay", "services", "memory", "identity", "intelligence", "webhooks"],
                "cta": "Contact Us",
                "cta_url": "https://wayforth.io/contact",
            },
            {
                "name": "Pro",
                "price_monthly_usd": 149,
                "rate_limit_per_minute": 100,
                "monthly_quota": 100000,
                "routing_fee_pct": 1.0,
                "routing_fee_bps": 100,
                "features": ["search", "query", "pay", "services", "memory", "identity", "intelligence", "webhooks", "history", "graph"],
                "cta": "Contact Us",
                "cta_url": "https://wayforth.io/contact",
            },
            {
                "name": "Enterprise",
                "price_monthly_usd": None,
                "rate_limit_per_minute": 500,
                "monthly_quota": -1,
                "routing_fee_pct": 0.75,
                "routing_fee_bps": 75,
                "features": ["everything", "sla", "private_catalog", "dedicated_infra", "custom_probing"],
                "cta": "Contact Us",
                "cta_url": "https://wayforth.io/contact",
            },
        ],
        "routing_fee_note": "Routing fee applies to all payment transactions. Higher tiers receive reduced fees.",
        "contracts": {
            "registry": "0x55810EfB3444A693556C3f9910dbFbF2dDaC369C",
            "escrow": "0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809",
            "network": "base-sepolia",
            "usdc": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        },
    }


@app.get("/intelligence-demo", include_in_schema=False)
async def intelligence_demo():
    return FileResponse("static/intelligence-demo.html")


@app.get("/health-page", include_in_schema=False)
async def health_page():
    return FileResponse("static/health-report.html")


@app.get("/analytics")
@limiter.limit("10/minute")
async def get_analytics(request: Request, key: str = ""):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        async with app.state.pool.acquire() as conn:
            top_queries = await conn.fetch("""
                SELECT query, COUNT(*) as count,
                       AVG(result_count) as avg_results,
                       SUM(CASE WHEN led_to_payment THEN 1 ELSE 0 END) as payment_conversions
                FROM search_analytics
                WHERE created_at > NOW() - INTERVAL '7 days'
                GROUP BY query ORDER BY count DESC LIMIT 20
            """)
            stats = await conn.fetchrow("""
                SELECT
                    COUNT(*) as total_searches,
                    SUM(CASE WHEN led_to_payment THEN 1 ELSE 0 END) as paid_searches,
                    COUNT(DISTINCT service_id) as services_paid_for
                FROM search_analytics sa
                LEFT JOIN search_outcomes so ON so.query_text = sa.query
                WHERE sa.created_at > NOW() - INTERVAL '7 days'
            """)
            return_sessions = await conn.fetchval("""
                SELECT COUNT(DISTINCT session_id) FROM search_analytics
                WHERE session_id IS NOT NULL
                AND session_id IN (
                    SELECT session_id FROM search_analytics
                    WHERE created_at > NOW() - INTERVAL '7 days'
                    GROUP BY session_id HAVING COUNT(*) > 1
                )
            """)
            unique_sessions = await conn.fetchval("""
                SELECT COUNT(DISTINCT session_id) FROM search_analytics
                WHERE session_id IS NOT NULL
                AND created_at > NOW() - INTERVAL '7 days'
            """)
            top_services = await conn.fetch("""
                SELECT top_result_id, COUNT(*) as times_top_result
                FROM search_analytics
                WHERE top_result_id IS NOT NULL
                AND created_at > NOW() - INTERVAL '7 days'
                GROUP BY top_result_id
                ORDER BY times_top_result DESC
                LIMIT 10
            """)
    except Exception as e:
        logger.error(f"Analytics DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    return {
        "period": "7d",
        "top_queries": [dict(r) for r in top_queries],
        "total_searches": stats["total_searches"],
        "payment_conversions": stats["paid_searches"],
        "conversion_rate": round((stats["paid_searches"] or 0) / max(stats["total_searches"] or 1, 1) * 100, 2),
        "services_paid_for": stats["services_paid_for"],
        "return_sessions": return_sessions,
        "unique_sessions": unique_sessions,
        "top_services_by_search": [dict(r) for r in top_services],
    }


@app.get("/competitive")
@limiter.limit("10/minute")
async def competitive_intelligence_endpoint(request: Request, key: str = ""):
    """Admin: x402 ecosystem growth signals and competitive intelligence."""
    if key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        async with app.state.pool.acquire() as conn:
            latest = await conn.fetchrow("""
                SELECT data, created_at FROM competitive_intelligence
                WHERE source = 'x402_monitor'
                ORDER BY created_at DESC LIMIT 1
            """)
            trend = await conn.fetch("""
                SELECT created_at, (data->>'live_count')::int as live_count
                FROM competitive_intelligence
                WHERE source = 'x402_monitor'
                ORDER BY created_at DESC LIMIT 30
            """)
    except Exception as e:
        logger.error(f"Competitive intelligence DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    return {
        "latest": json_lib.loads(latest["data"]) if latest else None,
        "last_checked": latest["created_at"].isoformat() if latest else None,
        "trend": [{"date": r["created_at"].isoformat(), "live_count": r["live_count"]} for r in trend],
    }


@app.post("/memory")
@limiter.limit("30/minute")
async def save_memory(request: Request, body: MemoryItem):
    """Save a service to agent memory."""
    async with app.state.pool.acquire() as db:
        await db.execute(
            """
            INSERT INTO agent_memory (agent_id, service_id, service_name, note, created_at, updated_at)
            VALUES ($1, $2, $3, $4, NOW(), NOW())
            ON CONFLICT (agent_id, service_id)
            DO UPDATE SET note=$4, updated_at=NOW()
            """,
            body.agent_id or "anonymous", body.service_id, body.service_name, body.note,
        )
    return {"status": "saved", "service_id": body.service_id, "service_name": body.service_name}


@app.get("/memory")
@limiter.limit("30/minute")
async def get_memory(request: Request, agent_id: str = "anonymous", q: str = ""):
    """Retrieve agent's saved services."""
    async with app.state.pool.acquire() as db:
        if q:
            rows = await db.fetch(
                """
                SELECT service_id, service_name, note, created_at
                FROM agent_memory
                WHERE agent_id = $1
                AND (LOWER(service_name) LIKE $2 OR LOWER(note) LIKE $2)
                ORDER BY created_at DESC LIMIT 20
                """,
                agent_id, f"%{q.lower()}%",
            )
        else:
            rows = await db.fetch(
                """
                SELECT service_id, service_name, note, created_at
                FROM agent_memory WHERE agent_id = $1
                ORDER BY created_at DESC LIMIT 20
                """,
                agent_id,
            )
    return {"agent_id": agent_id, "services": [dict(r) for r in rows], "total": len(rows)}


@app.post("/tier3/apply")
@limiter.limit("5/minute")
async def tier3_apply(request: Request, body: Tier3Application):
    """Apply for Tier 3 verification — KYB + SLA. Institutional-grade. Manual review required."""
    async with app.state.pool.acquire() as db:
        existing = await db.fetchrow("""
            SELECT id, kyb_status FROM tier3_applications
            WHERE contact_email = $1 AND endpoint_url = $2
        """, body.contact_email, body.endpoint_url)

        if existing:
            return {
                "status": "already_applied",
                "kyb_status": existing["kyb_status"],
                "message": "Application already on file. We'll contact you at the email provided.",
            }

        app_id = await db.fetchval("""
            INSERT INTO tier3_applications
            (service_name, company_name, contact_email, website, endpoint_url,
             monthly_volume_usdc, sla_uptime_target, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            RETURNING id
        """, body.service_name, body.company_name, body.contact_email,
            body.website, body.endpoint_url, body.monthly_volume_usdc,
            body.sla_uptime_target)

    if os.getenv("RESEND_API_KEY"):
        asyncio.create_task(asyncio.to_thread(
            send_tier3_application_notification,
            body.contact_email, body.service_name, body.company_name, str(app_id),
        ))

    return {
        "status": "submitted",
        "application_id": str(app_id),
        "message": "Application received. Our team will review your KYB documentation and contact you within 2 business days.",
        "next_steps": [
            "We will email you a KYB documentation checklist",
            "SLA terms will be negotiated based on your uptime target",
            "Tier 3 badge appears on your service within 24h of approval",
        ],
    }


@app.get("/tier3/status")
@limiter.limit("10/minute")
async def tier3_status(request: Request, email: str):
    """Check Tier 3 application status by email."""
    async with app.state.pool.acquire() as db:
        apps = await db.fetch("""
            SELECT id, service_name, company_name, kyb_status, created_at
            FROM tier3_applications WHERE contact_email = $1
            ORDER BY created_at DESC
        """, email)
    if not apps:
        return {"status": "not_found", "message": "No application found for this email."}
    return {
        "applications": [dict(a) for a in apps],
        "total": len(apps),
    }


@app.get("/tier3/admin")
@limiter.limit("10/minute")
async def tier3_admin(request: Request, key: str = "", status: str = "pending"):
    """Admin view of Tier 3 applications filtered by KYB status."""
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")
    async with app.state.pool.acquire() as db:
        apps = await db.fetch("""
            SELECT id, service_name, company_name, contact_email, endpoint_url,
                   monthly_volume_usdc, sla_uptime_target, kyb_status, created_at
            FROM tier3_applications WHERE kyb_status = $1
            ORDER BY created_at DESC
        """, status)
    return {
        "status_filter": status,
        "applications": [dict(a) for a in apps],
        "total": len(apps),
    }


async def _get_similar_services(db, service_id: str, limit: int) -> dict:
    """Shared helper: resolve service_id and return co-usage graph neighbours."""
    internal_id = service_id
    if service_id.startswith("0x"):
        sha = service_id[2:]
        row = await db.fetchrow(
            "SELECT id FROM services WHERE encode(sha256(endpoint_url::bytea), 'hex') = $1", sha
        )
        if row:
            internal_id = str(row["id"])

    rows = await db.fetch(
        """
        SELECT
            CASE WHEN service_a_id = $1 THEN service_b_id ELSE service_a_id END AS related_id,
            co_search_count, co_payment_count
        FROM service_graph
        WHERE service_a_id = $1 OR service_b_id = $1
        ORDER BY co_search_count DESC
        LIMIT $2
        """,
        internal_id, limit,
    )

    related = []
    for row in rows:
        svc = await db.fetchrow(
            "SELECT name, category, coverage_tier FROM services WHERE id::text = $1",
            row["related_id"],
        )
        related.append({
            "service_id": row["related_id"],
            "name": svc["name"] if svc else "Unknown",
            "category": svc["category"] if svc else None,
            "tier": svc["coverage_tier"] if svc else None,
            "co_search_count": row["co_search_count"],
            "co_payment_count": row["co_payment_count"],
        })

    return {
        "service_id": service_id,
        "related_services": related,
        "total": len(related),
        "note": "Co-usage patterns from real agent search sessions",
    }


@app.get("/graph/{service_id}")
@limiter.limit("20/minute")
async def get_service_graph(request: Request, service_id: str, limit: int = 10):
    """Return related services based on co-usage patterns."""
    async with app.state.pool.acquire() as db:
        return await _get_similar_services(db, service_id, limit)


@app.get("/services/similar/{service_id}")
@limiter.limit("30/minute")
async def similar_services(request: Request, service_id: str, limit: int = 5):
    """Public endpoint. Returns services commonly used alongside this one."""
    async with app.state.pool.acquire() as db:
        return await _get_similar_services(db, service_id, limit)


@app.get("/intelligence/{service_id}")
@limiter.limit("10/minute")
async def service_intelligence(request: Request, service_id: str, api_key: str = ""):
    """Wayforth Intelligence API — market data for service providers."""
    if not ADMIN_KEY or api_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Intelligence API key required. Contact hello@wayforth.io")

    async with app.state.pool.acquire() as db:
        internal_id = service_id
        if service_id.startswith("0x"):
            sha = service_id[2:]
            row = await db.fetchrow(
                "SELECT id FROM services WHERE encode(sha256(endpoint_url::bytea), 'hex') = $1", sha
            )
            if row:
                internal_id = str(row["id"])

        volume = await db.fetchrow(
            """
            SELECT COUNT(*) AS appearances, AVG((elem->>'score')::float) AS avg_score
            FROM search_analytics, jsonb_array_elements(results) AS elem
            WHERE elem->>'id' = $1
            AND created_at > NOW() - INTERVAL '7 days'
            """,
            internal_id,
        )

        rank_dist = await db.fetch(
            """
            SELECT position, COUNT(*) AS count FROM (
                SELECT ordinality - 1 AS position
                FROM search_analytics,
                     jsonb_array_elements(results) WITH ORDINALITY AS elem
                WHERE elem->>'id' = $1
                AND created_at > NOW() - INTERVAL '7 days'
            ) t GROUP BY position ORDER BY position
            """,
            internal_id,
        )

        conversions = await db.fetchval(
            """
            SELECT COUNT(*) FROM search_outcomes
            WHERE service_id::text = $1
            AND outcome_type = 'payment_initiated'
            AND created_at > NOW() - INTERVAL '7 days'
            """,
            internal_id,
        )

    return {
        "service_id": service_id,
        "period": "7d",
        "search_appearances": volume["appearances"] or 0,
        "avg_rank_score": round(volume["avg_score"] or 0, 1),
        "payment_conversions": conversions or 0,
        "rank_position_distribution": [dict(r) for r in rank_dist],
        "note": "Wayforth Intelligence API v1 — powered by real agent usage data",
    }


@app.get("/services/{service_id}/wri")
@limiter.limit("30/minute")
async def service_wri(request: Request, service_id: str, db=Depends(get_db)):
    """Current WRI score and 7-day trend for a service."""
    async with app.state.pool.acquire() as conn:
        history = await conn.fetch("""
            SELECT wri_score, tier, recorded_at
            FROM service_score_history
            WHERE service_id = $1
              AND recorded_at > NOW() - INTERVAL '7 days'
            ORDER BY recorded_at DESC LIMIT 30
        """, service_id)

    if not history:
        return {"service_id": service_id, "wri": None, "trend": "no_data", "history": []}

    scores = [r["wri_score"] for r in history]
    current = scores[0]
    trend = "stable"
    if len(scores) >= 4:
        recent = sum(scores[:2]) / 2
        older = sum(scores[-2:]) / 2
        if recent > older + 3:
            trend = "improving"
        elif recent < older - 3:
            trend = "declining"

    return {
        "service_id": service_id,
        "wri": current,
        "trend": trend,
        "avg_7d": round(sum(scores) / len(scores), 1),
        "history": [{"wri": r["wri_score"], "at": r["recorded_at"].isoformat()} for r in history],
    }


@app.get("/services/{service_id}/history")
@limiter.limit("20/minute")
async def service_history(request: Request, service_id: str, days: int = Query(default=30, ge=1, le=90)):
    """WRI score trend for a service over time. Powers reliability trend visualization."""
    async with app.state.pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT wri_score, tier, consecutive_failures, recorded_at
            FROM service_score_history
            WHERE service_id = $1
              AND recorded_at > NOW() - ($2 * INTERVAL '1 day')
            ORDER BY recorded_at ASC
        """, service_id, days)

    if not rows:
        return {"service_id": service_id, "history": [], "trend": "insufficient_data"}

    scores = [r["wri_score"] for r in rows]
    trend = "stable"
    if len(scores) >= 3:
        recent_avg = sum(scores[-3:]) / 3
        older_avg = sum(scores[:3]) / 3
        if recent_avg > older_avg + 5:
            trend = "improving"
        elif recent_avg < older_avg - 5:
            trend = "declining"

    return {
        "service_id": service_id,
        "history": [{"wri": r["wri_score"], "tier": r["tier"], "at": r["recorded_at"].isoformat()} for r in rows],
        "current_wri": scores[-1],
        "avg_wri_30d": round(sum(scores) / len(scores), 1),
        "trend": trend,
        "data_points": len(scores),
    }


@app.post("/webhooks/register")
@limiter.limit("5/minute")
async def register_webhook(request: Request, body: WebhookRegistration):
    """Register a webhook to receive tier change or health alert events for a service."""
    if not body.webhook_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="webhook_url must use HTTPS")
    secret = secrets.token_hex(32)
    async with app.state.pool.acquire() as conn:
        wh_id = await conn.fetchval("""
            INSERT INTO provider_webhooks
            (service_id, webhook_url, contact_email, events, secret_token)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (service_id, webhook_url) DO UPDATE
            SET active = TRUE, updated_at = NOW()
            RETURNING id
        """, body.service_id, body.webhook_url, body.contact_email, body.events, secret)
    return {
        "webhook_id": str(wh_id),
        "secret_token": secret,
        "message": "Webhook registered. Store your secret_token — it won't be shown again.",
        "events": body.events,
    }


@app.delete("/webhooks/{webhook_id}")
@limiter.limit("10/minute")
async def delete_webhook(request: Request, webhook_id: str):
    """Deactivate a registered webhook."""
    async with app.state.pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE provider_webhooks SET active = FALSE WHERE id = $1::uuid", webhook_id
        )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {"webhook_id": webhook_id, "status": "deactivated"}


app.mount("/static", StaticFiles(directory="static"), name="static")


# ── API Key System ──────────────────────────────────────────────────────────

TIER_LIMITS = {
    "free":       {"rpm": 10,  "monthly": 1_000,    "fee_bps": 150},
    "starter":    {"rpm": 30,  "monthly": 10_000,   "fee_bps": 125},
    "pro":        {"rpm": 100, "monthly": 100_000,  "fee_bps": 100},
    "enterprise": {"rpm": 500, "monthly": -1,       "fee_bps": 75},
}


class ApiKeyRequest(BaseModel):
    email: str
    tier: str = "free"
    admin_key: str = ""  # Required to create non-free keys


async def get_api_key(request: Request, db=Depends(get_db)):
    """
    Optional API key auth. If provided, validates and tracks usage.
    If not provided, falls back to IP-based rate limiting.
    Returns tier info for the request.
    """
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        request.state.rate_limit_tier = "anonymous"
        request.state.rate_limit_rpm = 10
        return {"tier": "anonymous", "rpm": 10, "quota": None}

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key = await db.fetchrow("""
        SELECT id, tier, rate_limit_per_minute, monthly_quota, usage_this_month,
               quota_reset_at, active
        FROM api_keys WHERE key_hash = $1
    """, key_hash)

    if not key or not key["active"]:
        raise HTTPException(status_code=401, detail="Invalid or inactive API key")

    if key["monthly_quota"] > 0 and key["usage_this_month"] >= key["monthly_quota"]:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly quota of {key['monthly_quota']} requests exceeded. Resets {key['quota_reset_at'].strftime('%Y-%m-%d')}",
        )

    await db.execute("""
        UPDATE api_keys
        SET usage_this_month = usage_this_month + 1, last_used_at = NOW()
        WHERE id = $1
    """, key["id"])

    request.state.rate_limit_tier = key["tier"]
    request.state.rate_limit_rpm = key["rate_limit_per_minute"]
    return {"tier": key["tier"], "rpm": key["rate_limit_per_minute"], "key_id": str(key["id"])}


@app.get("/keys/tiers", tags=["Keys"])
async def key_tiers():
    return {
        "tiers": [
            {"tier": "free",       "price_monthly_usd": 0,    "rpm": 10,  "monthly_quota": 1000,   "routing_fee_pct": 1.5,  "features": ["search", "query", "pay", "services"]},
            {"tier": "starter",    "price_monthly_usd": 29,   "rpm": 30,  "monthly_quota": 10000,  "routing_fee_pct": 1.25, "features": ["search", "query", "pay", "services", "intelligence", "webhooks"]},
            {"tier": "pro",        "price_monthly_usd": 149,  "rpm": 100, "monthly_quota": 100000, "routing_fee_pct": 1.0,  "features": ["search", "query", "pay", "services", "intelligence", "webhooks", "history", "graph"]},
            {"tier": "enterprise", "price_monthly_usd": None, "rpm": 500, "monthly_quota": -1,     "routing_fee_pct": 0.75, "features": ["everything", "sla", "private_catalog", "dedicated_infra", "custom_probing"]},
        ],
        "note": "Routing fee applies to all payment transactions. Higher tiers receive reduced fees."
    }


@app.post("/keys/create")
@limiter.limit("5/minute")
async def create_api_key(request: Request, body: ApiKeyRequest, db=Depends(get_db)):
    if body.tier != "free" and body.admin_key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Admin key required for non-free tiers")

    if body.tier not in TIER_LIMITS:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Must be one of: {', '.join(TIER_LIMITS)}")

    existing = await db.fetchval("""
        SELECT COUNT(*) FROM api_keys WHERE owner_email = $1 AND active = TRUE
    """, body.email)
    if existing >= 3:
        raise HTTPException(status_code=429, detail="Maximum 3 active keys per email")

    raw_key = f"wf_{'live' if body.tier != 'free' else 'free'}_{secrets.token_hex(24)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]
    limits = TIER_LIMITS[body.tier]

    await db.execute("""
        INSERT INTO api_keys
        (key_hash, key_prefix, owner_email, tier, rate_limit_per_minute, monthly_quota)
        VALUES ($1, $2, $3, $4, $5, $6)
    """, key_hash, key_prefix, body.email, body.tier, limits["rpm"], limits["monthly"])

    if os.getenv("RESEND_API_KEY"):
        asyncio.create_task(asyncio.to_thread(
            send_welcome_email, body.email, key_prefix, body.tier
        ))

    return {
        "api_key": raw_key,
        "key_prefix": key_prefix,
        "tier": body.tier,
        "rate_limit_per_minute": limits["rpm"],
        "monthly_quota": limits["monthly"],
        "message": "Store this key securely — it will not be shown again.",
        "usage": f"Add header: X-Wayforth-API-Key: {raw_key}",
    }


@app.get("/keys/usage")
@limiter.limit("10/minute")
async def key_usage(request: Request, db=Depends(get_db)):
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        raise HTTPException(status_code=401, detail="X-Wayforth-API-Key header required")

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key = await db.fetchrow("""
        SELECT key_prefix, tier, rate_limit_per_minute, monthly_quota,
               usage_this_month, quota_reset_at, created_at, last_used_at
        FROM api_keys WHERE key_hash = $1 AND active = TRUE
    """, key_hash)

    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    quota_pct = (
        round(key["usage_this_month"] / key["monthly_quota"] * 100, 1)
        if key["monthly_quota"] > 0
        else 0
    )

    return {
        "key_prefix": key["key_prefix"],
        "tier": key["tier"],
        "rate_limit_per_minute": key["rate_limit_per_minute"],
        "monthly_quota": key["monthly_quota"],
        "usage_this_month": key["usage_this_month"],
        "quota_remaining": max(0, key["monthly_quota"] - key["usage_this_month"]),
        "quota_used_pct": quota_pct,
        "quota_resets_at": key["quota_reset_at"].isoformat(),
        "created_at": key["created_at"].isoformat(),
        "last_used_at": key["last_used_at"].isoformat() if key["last_used_at"] else None,
    }


@app.post("/identity/register")
@limiter.limit("10/minute")
async def register_identity(request: Request, body: AgentIdentityRequest, db=Depends(get_db)):
    """Register an agent identity. Idempotent — safe to call multiple times."""
    existing = await db.fetchrow("""
        SELECT id, trust_score, total_searches, total_payments
        FROM agent_identities WHERE agent_id = $1
    """, body.agent_id)

    if existing:
        return {
            "agent_id": body.agent_id,
            "status": "existing",
            "trust_score": existing["trust_score"],
            "total_searches": existing["total_searches"],
            "total_payments": existing["total_payments"],
            "message": "Identity already registered.",
        }

    await db.execute("""
        INSERT INTO agent_identities (agent_id, display_name, created_at, last_active_at)
        VALUES ($1, $2, NOW(), NOW())
    """, body.agent_id, body.display_name or body.agent_id[:12])

    return {
        "agent_id": body.agent_id,
        "status": "registered",
        "trust_score": 50.0,
        "message": "Identity registered. Trust score starts at 50 and improves with activity.",
    }


@app.get("/identity/{agent_id}")
@limiter.limit("30/minute")
async def get_identity(request: Request, agent_id: str, db=Depends(get_db)):
    """Get agent identity and reputation."""
    identity = await db.fetchrow("""
        SELECT agent_id, display_name, total_searches, total_payments,
               total_spend_usdc, trust_score, created_at, last_active_at
        FROM agent_identities WHERE agent_id = $1
    """, agent_id)

    if not identity:
        raise HTTPException(status_code=404, detail="Agent identity not found. Register at POST /identity/register")

    trust = identity["trust_score"]
    if trust >= 90:
        tier = "elite"
    elif trust >= 75:
        tier = "trusted"
    elif trust >= 60:
        tier = "established"
    elif trust >= 40:
        tier = "new"
    else:
        tier = "unknown"

    return {
        "agent_id": identity["agent_id"],
        "display_name": identity["display_name"],
        "trust_score": identity["trust_score"],
        "reputation_tier": tier,
        "total_searches": identity["total_searches"],
        "total_payments": identity["total_payments"],
        "total_spend_usdc": identity["total_spend_usdc"],
        "member_since": identity["created_at"].isoformat(),
        "last_active": identity["last_active_at"].isoformat(),
    }


@app.get("/identity/{agent_id}/history")
@limiter.limit("20/minute")
async def identity_history(request: Request, agent_id: str, db=Depends(get_db)):
    """Agent's search and payment history."""
    searches = await db.fetch("""
        SELECT query, top_result_id, created_at
        FROM search_analytics
        WHERE session_id = $1
        ORDER BY created_at DESC LIMIT 20
    """, agent_id)

    payments = await db.fetch("""
        SELECT service_id, outcome_type, created_at
        FROM search_outcomes
        WHERE session_id = $1
        ORDER BY created_at DESC LIMIT 20
    """, agent_id)

    return {
        "agent_id": agent_id,
        "recent_searches": [dict(r) for r in searches],
        "recent_payments": [dict(r) for r in payments],
    }
# s44 Sat Apr 25 12:29:13 PDT 2026
