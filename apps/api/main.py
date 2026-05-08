"""main.py — Wayforth API: startup, middleware, lifespan, app init, background tasks."""

import asyncio
import hashlib
import logging
import os
import uuid as uuid_lib
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import sentry_sdk
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

load_dotenv()

# ── Version and globals ───────────────────────────────────────────────────────

VERSION = "0.4.2"
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
SENTRY_DSN = os.getenv("SENTRY_DSN", "")

if SENTRY_DSN:
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.starlette import StarletteIntegration
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

# ── Internal imports (safe — no circular risk) ────────────────────────────────

import stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")

from db import check_db
from core.auth import _AuthError, _auth_error_handler, _ANON_DAILY_LIMIT, _TIER_RPM
from core.rate_limit import get_real_ip
from core.credits import (
    PLANS, CREDITS_PER_CALL, ROUTING_FEE, STRIPE_PACKAGES,
    check_and_deduct_credits, compute_calls_remaining, _dispatch_webhooks,
    _monthly_topup_reset,
)
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier
from services.managed import SERVICE_CONFIGS
from services.param_mapper import MANAGED_TO_CATALOG
from services.wayforthrank import compute_wri

_DB_URL = os.environ.get("DATABASE_URL", "")
_ASYNCPG_URL = _DB_URL.replace("postgresql+asyncpg://", "postgresql://")

# ── Background tasks ──────────────────────────────────────────────────────────

async def _cleanup_anon_searches_loop(app: "FastAPI"):
    while True:
        await asyncio.sleep(3600)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stale = [k for k in list(app.state.anon_searches) if not k.endswith(f":{today}")]
        for k in stale:
            app.state.anon_searches.pop(k, None)
        if stale:
            logger.info(f"Cleaned {len(stale)} stale anon search entries")


# Probe payloads for each probeable managed service (skip resend, stability, assemblyai, elevenlabs)
_PROBE_PARAMS: dict[str, dict] = {
    "groq":        {"messages": [{"role": "user", "content": "Hi"}]},
    "together":    {"messages": [{"role": "user", "content": "Hi"}]},
    "deepl":       {"text": "Hi", "target_lang": "ES"},
    "serper":      {"query": "test"},
    "tavily":      {"query": "test"},
    "brave":       {"query": "test"},
    "perplexity":  {"messages": [{"role": "user", "content": "Hi"}]},
    "openweather": {"city": "London"},
    "newsapi":     {"query": "test"},
    "alphavantage":{"symbol": "AAPL"},
    "jina":        {"url": "https://example.com"},
}


async def _probe_managed_services_loop():
    """Probe all probeable managed services every 30 minutes."""
    import time as _time
    from services.managed import ADAPTERS
    from services.param_mapper import map_params
    await asyncio.sleep(60)  # brief startup delay
    while True:
        pool = getattr(app.state, "pool", None)
        if not pool:
            await asyncio.sleep(1800)
            continue

        async with pool.acquire() as conn:
            catalog_slugs = list(MANAGED_TO_CATALOG.values())
            slug_to_id = {
                r["slug"]: str(r["id"])
                for r in await conn.fetch(
                    "SELECT id, slug FROM services WHERE slug = ANY($1::text[])",
                    catalog_slugs,
                )
            }

        for managed_slug, probe_params in _PROBE_PARAMS.items():
            cfg = SERVICE_CONFIGS.get(managed_slug)
            if not cfg:
                continue
            api_key = os.environ.get(cfg["key_var"], "")
            if not api_key:
                continue

            catalog_slug = MANAGED_TO_CATALOG.get(managed_slug, managed_slug)
            service_id = slug_to_id.get(catalog_slug)

            t0 = _time.time()
            success = False
            error_msg = None
            try:
                adapter = ADAPTERS.get(managed_slug)
                if adapter:
                    mapped, _ = map_params(managed_slug, probe_params)
                    await adapter(mapped, api_key)
                    success = True
            except Exception as exc:
                error_msg = str(exc)[:200]

            response_ms = round((_time.time() - t0) * 1000)

            try:
                pool2 = getattr(app.state, "pool", None)
                if not pool2:
                    continue
                async with pool2.acquire() as conn2:
                    if service_id:
                        await conn2.execute(
                            """
                            INSERT INTO service_probes
                              (service_id, reachable, response_time_ms, status_code, error_message)
                            VALUES ($1::uuid, $2, $3, $4, $5)
                            """,
                            service_id, success, float(response_ms),
                            200 if success else 500, error_msg,
                        )
                    # Update consecutive_failures and last_tested_at on the services row
                    if success:
                        await conn2.execute(
                            "UPDATE services SET consecutive_failures=0, last_tested_at=NOW() WHERE slug=$1",
                            catalog_slug,
                        )
                    else:
                        await conn2.execute(
                            "UPDATE services SET consecutive_failures=consecutive_failures+1, last_tested_at=NOW() WHERE slug=$1",
                            catalog_slug,
                        )
                    # Purge probes older than 7 days
                    await conn2.execute(
                        "DELETE FROM service_probes WHERE probed_at < NOW() - INTERVAL '7 days'"
                    )
            except Exception as db_err:
                logger.warning("probe db write failed for %s: %s", managed_slug, db_err)

        logger.info("managed service probe cycle complete")
        await asyncio.sleep(1800)  # 30 minutes


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    from core.auth import get_jwks, _jwks_cache
    from routers.billing import _usdc_payment_watcher, _usdc_renewal_reminder

    logger.info(f"Wayforth API starting, environment={ENVIRONMENT}")
    try:
        await asyncio.to_thread(get_jwks)
        logger.info("JWKS cache pre-warmed (%d keys)", len(_jwks_cache["keys"]))
    except Exception as _jwks_err:
        logger.warning("JWKS pre-warm failed (will retry on first request): %s", _jwks_err)
    ok = check_db()
    if not ok:
        logger.warning("DB connection check failed — starting anyway")
    app.state.db_ok = ok
    app.state.anon_searches = {}
    try:
        app.state.pool = await asyncpg.create_pool(_ASYNCPG_URL, min_size=2, max_size=10)
        app.state.db_ok = True
        async with app.state.pool.acquire() as _mconn:
            await _mconn.execute("""
                ALTER TABLE services
                    ADD COLUMN IF NOT EXISTS wri_score FLOAT,
                    ADD COLUMN IF NOT EXISTS wri_version TEXT DEFAULT 'v1'
            """)
            await _mconn.execute("""
                ALTER TABLE user_service_keys
                    ADD COLUMN IF NOT EXISTS endpoint_url TEXT,
                    ADD COLUMN IF NOT EXISTS default_method TEXT DEFAULT 'POST'
            """)
            await _mconn.execute("""
                INSERT INTO services (name, description, endpoint_url, category, coverage_tier, pricing_usdc, source, payment_protocol, x402_supported, metadata)
                VALUES
                  ('Solvr World News', 'Real-time global news feed from Solvr. Free tier — no per-call cost. Returns latest world news headlines and summaries.', 'https://api.solvrbot.com/api/v1/news', 'data', 1, 0.0, 'catalog', 'wayforth', false, '{"auth":"bearer_eip191","provider":"Solvr","provider_url":"https://solvrbot.com","pricing_tier":"free"}'),
                  ('Solvr World Data', 'Global economic and macroeconomic data from Solvr. Free tier — covers GDP, inflation, trade, and country-level indicators.', 'https://api.solvrbot.com/api/v1/worlddata', 'data', 1, 0.0, 'catalog', 'wayforth', false, '{"auth":"bearer_eip191","provider":"Solvr","provider_url":"https://solvrbot.com","pricing_tier":"free"}'),
                  ('Solvr Token Intelligence', 'On-chain token intelligence by contract address (CA). Returns holder analysis, liquidity, volume trends, and risk signals. Standard tier.', 'https://api.solvrbot.com/api/v1/intel/{ca}', 'analytics', 1, 0.001, 'catalog', 'wayforth', false, '{"auth":"bearer_eip191","provider":"Solvr","provider_url":"https://solvrbot.com","pricing_tier":"standard","path_param":"ca"}'),
                  ('Solvr Token Security Scan', 'Security scan for a token contract. Detects honeypots, rug pull indicators, ownership renouncement, and contract vulnerabilities. Standard tier.', 'https://api.solvrbot.com/api/v1/security/scan', 'analytics', 1, 0.001, 'catalog', 'wayforth', false, '{"auth":"bearer_eip191","provider":"Solvr","provider_url":"https://solvrbot.com","pricing_tier":"standard","method":"POST"}'),
                  ('Solvr Quick Technical Analysis', 'Fast technical analysis snapshot: RSI, MACD, moving averages in one low-latency call. Standard tier.', 'https://api.solvrbot.com/api/v1/ta/quick', 'analytics', 1, 0.001, 'catalog', 'wayforth', false, '{"auth":"bearer_eip191","provider":"Solvr","provider_url":"https://solvrbot.com","pricing_tier":"standard","method":"POST"}'),
                  ('Solvr Full TA Stack', 'Comprehensive technical analysis: Bollinger Bands, Ichimoku, Fibonacci, volume profile, and pattern recognition. Full tier.', 'https://api.solvrbot.com/api/v1/ta/analysis', 'analytics', 1, 0.003, 'catalog', 'wayforth', false, '{"auth":"bearer_eip191","provider":"Solvr","provider_url":"https://solvrbot.com","pricing_tier":"full","method":"POST"}')
                ON CONFLICT (endpoint_url) DO UPDATE SET
                  name = EXCLUDED.name, description = EXCLUDED.description,
                  category = EXCLUDED.category, coverage_tier = EXCLUDED.coverage_tier,
                  pricing_usdc = EXCLUDED.pricing_usdc, metadata = EXCLUDED.metadata
            """)
            await _mconn.execute("""
                ALTER TABLE credit_transactions
                    ADD COLUMN IF NOT EXISTS agent_id TEXT,
                    ADD COLUMN IF NOT EXISTS api_key_id UUID
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS credit_transactions_agent_id_idx
                ON credit_transactions(user_id, agent_id)
                WHERE agent_id IS NOT NULL
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS wri_alerts (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    api_key_id UUID REFERENCES api_keys(id),
                    category TEXT,
                    threshold_score DECIMAL(5,2) NOT NULL,
                    min_signals INTEGER DEFAULT 5,
                    notify_url TEXT NOT NULL,
                    active BOOLEAN DEFAULT true,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    last_fired_at TIMESTAMPTZ,
                    fired_count INTEGER DEFAULT 0
                )
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS wri_alerts_api_key_idx
                ON wri_alerts(api_key_id) WHERE active = true
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS wri_alert_logs (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    alert_id UUID REFERENCES wri_alerts(id),
                    service_slug TEXT,
                    old_wri DECIMAL(5,2),
                    new_wri DECIMAL(5,2),
                    fired_at TIMESTAMPTZ DEFAULT NOW(),
                    response_status INTEGER,
                    success BOOLEAN
                )
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS x402_agent_identities (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    wallet_address TEXT UNIQUE NOT NULL,
                    network TEXT NOT NULL DEFAULT 'base',
                    tier TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (tier IN ('unknown','emerging','established','trusted','elite')),
                    trust_score DECIMAL(5,2) DEFAULT 0,
                    total_calls INTEGER DEFAULT 0,
                    total_spent_usdc DECIMAL(18,6) DEFAULT 0,
                    first_seen TIMESTAMPTZ DEFAULT NOW(),
                    last_seen TIMESTAMPTZ DEFAULT NOW(),
                    flagged BOOLEAN DEFAULT false,
                    flag_reason TEXT
                )
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS x402_agent_identities_wallet_idx
                ON x402_agent_identities(wallet_address)
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS providers (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    company_name TEXT NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    tier TEXT NOT NULL DEFAULT 'observer'
                        CHECK (tier IN ('observer','intelligence','premium')),
                    verified BOOLEAN DEFAULT false,
                    verification_method TEXT
                        CHECK (verification_method IN ('dns_txt','header','manual')),
                    stripe_customer_id TEXT,
                    stripe_subscription_id TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    last_login_at TIMESTAMPTZ
                )
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS provider_services (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    provider_id UUID REFERENCES providers(id),
                    service_slug TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    verified BOOLEAN DEFAULT false,
                    verified_at TIMESTAMPTZ,
                    verification_code TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    UNIQUE(provider_id, service_slug)
                )
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS provider_sessions (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    provider_id UUID REFERENCES providers(id),
                    token TEXT UNIQUE NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS providers_email_idx ON providers(email)
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS provider_services_slug_idx ON provider_services(service_slug)
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS provider_sessions_token_idx ON provider_sessions(token)
            """)
            await _mconn.execute("""
                ALTER TABLE api_keys
                    ADD COLUMN IF NOT EXISTS calls_count INTEGER NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS monthly_calls_count INTEGER NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS monthly_calls_reset_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS monthly_searches INTEGER NOT NULL DEFAULT 0
            """)
            await _mconn.execute("""
                UPDATE services
                SET consecutive_failures = 0
                WHERE slug = ANY($1::text[])
                  AND consecutive_failures >= 3
            """, list(MANAGED_TO_CATALOG.values()))
            await _mconn.execute("""
                UPDATE api_keys ak
                SET calls_count = sub.total,
                    monthly_calls_count = sub.monthly,
                    monthly_calls_reset_at = COALESCE(ak.monthly_calls_reset_at,
                        date_trunc('month', NOW()) + INTERVAL '1 month')
                FROM (
                    SELECT api_key_id,
                        COUNT(*) AS total,
                        COUNT(*) FILTER (WHERE created_at >= date_trunc('month', NOW())) AS monthly
                    FROM credit_transactions
                    WHERE api_key_id IS NOT NULL
                      AND type IN ('execution', 'cross_rail')
                    GROUP BY api_key_id
                ) sub
                WHERE ak.id = sub.api_key_id
                  AND ak.calls_count = 0
            """)
    except Exception as e:
        logger.error(f"DB error: {e}")
        logger.warning(f"DB pool creation failed: {e} — /services will be unavailable")
        app.state.pool = None
    cleanup_task = asyncio.create_task(_cleanup_anon_searches_loop(app))
    watcher_task = asyncio.create_task(_usdc_payment_watcher())
    renewal_task = asyncio.create_task(_usdc_renewal_reminder())
    reset_task = asyncio.create_task(_monthly_topup_reset())
    probe_task = asyncio.create_task(_probe_managed_services_loop())
    yield
    cleanup_task.cancel()
    watcher_task.cancel()
    renewal_task.cancel()
    reset_task.cancel()
    probe_task.cancel()
    if app.state.pool:
        await app.state.pool.close()


# ── App creation ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Wayforth API",
    description="""
## Wayforth — The search engine and API execution layer for AI agents

270+ verified APIs across 18 categories. Credits-based billing.

### Authentication
All endpoints require `X-Wayforth-API-Key` header.
Get your free API key at https://wayforth.io/dashboard

### Credits
- 1 credit = $0.001 USD
- 100 free credits on signup
- Packages: $19/50K · $99/300K · $299/1M

### Quick Start
```bash
pip install wayforth-sdk
# or
uvx wayforth-mcp
```

### Support
https://wayforth.io/contact
""",
    version=VERSION,
    contact={"name": "Wayforth", "url": "https://wayforth.io/contact"},
    license_info={"name": "BSL 1.1", "url": "https://wayforth.io/license"},
    lifespan=lifespan,
)

# ── Middleware ────────────────────────────────────────────────────────────────

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
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = str(uuid_lib.uuid4())
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if raw_key:
        request.state.api_key = raw_key
    response = await call_next(request)
    response.headers["X-Wayforth-Request-ID"] = request_id
    response.headers["X-Wayforth-Version"] = VERSION
    response.headers["X-RateLimit-Tier"] = str(getattr(request.state, "rate_limit_tier", "free"))
    response.headers["X-RateLimit-Limit"] = str(getattr(request.state, "rate_limit_rpm", "10"))
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=()"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


# Register _AuthError exception handler
app.add_exception_handler(_AuthError, _auth_error_handler)

# ── Auth dependency (used by /search, /query) ─────────────────────────────────

async def check_auth(request: Request) -> dict:
    """Unified auth dependency for /search and /query."""
    from routers.billing import _credits_to_tier
    ip = get_real_ip(request)
    raw_key = request.headers.get("X-Wayforth-API-Key", "")

    if raw_key:
        pool = request.app.state.pool
        if not pool:
            raise HTTPException(status_code=503, detail="Database unavailable")
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        async with pool.acquire() as db:
            key = await db.fetchrow("""
                SELECT id, user_id, tier, rate_limit_per_minute, monthly_quota,
                       usage_this_month, quota_reset_at, active,
                       payment_rail, subscription_expires_at
                FROM api_keys WHERE key_hash = $1
            """, key_hash)

        if not key or not key["active"]:
            raise _AuthError(401, {
                "error": "invalid_key",
                "message": "Invalid API key. Get yours at wayforth.io/dashboard",
            })

        if key["monthly_quota"] > 0 and key["usage_this_month"] >= key["monthly_quota"]:
            raise _AuthError(429, {
                "error": "quota_exceeded",
                "message": "Monthly quota exceeded. Upgrade at wayforth.io/pricing",
                "upgrade_url": "https://wayforth.io/pricing",
            })

        # Graceful USDC subscription expiry
        if (key.get("payment_rail") == "usdc" and key.get("subscription_expires_at")
                and key["subscription_expires_at"] < datetime.now(timezone.utc)):
            from routers.billing import _activate_usdc_subscription
            # _downgrade_expired_usdc uses app.state.pool — defer to billing module
            from core.credits import _downgrade_expired_usdc
            asyncio.create_task(_downgrade_expired_usdc(str(key["id"])))

        async with pool.acquire() as db:
            await db.execute("""
                UPDATE api_keys SET usage_this_month = usage_this_month + 1,
                                    last_used_at = NOW()
                WHERE id = $1
            """, key["id"])

        rpm = _TIER_RPM.get(key["tier"], 10)
        request.state.rate_limit_tier = key["tier"]
        request.state.rate_limit_rpm = rpm
        return {
            "authenticated": True,
            "tier": key["tier"],
            "key_id": str(key["id"]),
            "user_id": str(key["user_id"]) if key["user_id"] else None,
            "usage_this_month": key["usage_this_month"] + 1,
            "monthly_quota": key["monthly_quota"],
            "anonymous_count": None,
            "ip": ip,
        }

    # Anonymous path
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    anon_key = f"{ip}:{today}"
    anon_dict = request.app.state.anon_searches
    count = anon_dict.get(anon_key, 0)

    if count >= _ANON_DAILY_LIMIT:
        raise _AuthError(429, {
            "error": "free_limit_reached",
            "message": "You've used your 3 free searches. Sign up free for 100 searches/month — no credit card required.",
            "signup_url": "https://wayforth.io/signup",
            "dashboard_url": "https://wayforth.io/dashboard",
        })

    anon_dict[anon_key] = count + 1
    request.state.rate_limit_tier = "anonymous"
    request.state.rate_limit_rpm = 10
    return {
        "authenticated": False,
        "tier": None,
        "key_id": None,
        "anonymous_count": count + 1,
        "ip": ip,
    }


# ── Include routers ───────────────────────────────────────────────────────────

from routers import (
    search, execute, billing, webhooks, provider, admin, x402, auth, agent
)

app.include_router(search.router)
app.include_router(execute.router)
app.include_router(billing.router)
app.include_router(webhooks.router)
app.include_router(provider.router)
app.include_router(admin.router)
app.include_router(x402.router)
app.include_router(auth.router)
app.include_router(agent.router)

# ── Static files ──────────────────────────────────────────────────────────────

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass  # static dir may not exist in all environments

# ── Core health / system routes ───────────────────────────────────────────────

@app.get("/health")
@limiter.limit("60/minute")
async def health(request: Request, db=Depends(get_db)):
    try:
        await db.fetchval("SELECT 1")
        db_status = "ok"
        tier2 = await db.fetchval("SELECT COUNT(*) FROM services WHERE coverage_tier >= 2 AND consecutive_failures < 3") or 0
        total = await db.fetchval("SELECT COUNT(*) FROM services WHERE consecutive_failures < 3") or 0
    except Exception:
        db_status = "error"
        tier2 = 0
        total = 0
    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "service": "wayforth-api",
        "version": VERSION,
        "db_status": db_status,
        "catalog": {
            "total": total,
            "tier2": tier2,
        },
        "managed_services": len(SERVICE_CONFIGS),
    }


@app.get("/status", tags=["System"])
async def system_status(db=Depends(get_db)):
    """Public system status — uptime, service count, last health check."""
    stats = await db.fetchrow("""
        SELECT
            COUNT(*) FILTER (WHERE coverage_tier >= 2 AND consecutive_failures < 3) as tier2_services,
            COUNT(*) FILTER (WHERE consecutive_failures < 3) as total_services,
            COUNT(*) FILTER (WHERE coverage_tier >= 3 AND consecutive_failures < 3) as tier3_services
        FROM services
    """)
    searches = await db.fetchval("""
        SELECT COUNT(*) FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '24h'
    """)
    return {
        "status": "operational",
        "version": VERSION,
        "wayforthrank_version": "2.0",
        "services": {
            "total": stats["total_services"],
            "tier2": stats["tier2_services"],
            "tier3": stats["tier3_services"],
            "managed": len(SERVICE_CONFIGS),
        },
        "managed_services": len(SERVICE_CONFIGS),
        "searches_24h": searches,
        "mcp_tools": 16,
        "pypi_version": VERSION,
        "api": "operational",
        "database": "operational",
        "payment_rails": {
            "card": True,
            "usdc_subscription": True,
            "x402_pay_per_call": bool(os.environ.get("WAYFORTH_BASE_WALLET")),
            "cross_rail_conversion": True,
            "agent_auto_topup": True,
        },
        "agent_billing_permissions": ["none", "auto_topup", "full"],
        "x402": {
            "network": "Base (eip155:8453)",
            "testnet_active": True,
            "mainnet_active": False,
            "services_in_catalog": stats["total_services"],
            "managed_services_x402": len(SERVICE_CONFIGS),
        },
        "pricing": {
            "routing_fee": "1.5%",
            "usdc_bonus": "5% extra calls",
        },
        "contracts": {
            "network": "base-sepolia",
            "escrow": "0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809",
            "mainnet_eta": "Q3 2026",
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/chain")
async def get_chain_info():
    """
    Wayforth smart contract addresses and blockchain infrastructure info.
    Current deployment: Base Sepolia testnet.
    Mainnet deployment: Q3 2026 (pending audit).
    """
    return {
        "network": "base-sepolia",
        "chain_id": 84532,
        "status": "testnet_live",
        "mainnet_eta": "Q3 2026",
        "contracts": {
            "escrow": {
                "address": "0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809",
                "name": "WayforthEscrow",
                "basescan": "https://sepolia.basescan.org/address/0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809",
            },
        },
        "usdc": {
            "base_sepolia": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
            "base_mainnet": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        },
        "payment_tracks": {
            "track_a": "Stripe Treasury (fiat, card-funded, FDIC insured)",
            "track_b": "Base blockchain (USDC, non-custodial, calldata)",
            "track_c": "x402 protocol (native services, Coinbase facilitator)",
        },
        "routing_fee": {
            "rate_pct": 1.5,
        },
    }
