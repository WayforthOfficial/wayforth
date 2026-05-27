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
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

load_dotenv()

# ── Version and globals ───────────────────────────────────────────────────────

VERSION = "0.7.8"
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

# L10 (v0.7.8): LOG_LEVEL env override. Defaults to INFO but on-call can flip
# to DEBUG without a redeploy. Invalid values fall back to INFO so a typo
# doesn't silence the app.
_LOG_LEVEL_NAME = os.environ.get("LOG_LEVEL", "INFO").upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)
logging.basicConfig(
    level=_LOG_LEVEL,
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
    _monthly_topup_reset, _webhook_retry_loop, check_service_margins,
)
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier, _get_redis
from services.managed import SERVICE_CONFIGS, _active_managed_count
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
                    # Rolling service_health from last 10 probes
                    if service_id:
                        recent = await conn2.fetch(
                            """SELECT reachable, response_time_ms FROM service_probes
                               WHERE service_id = $1::uuid
                               ORDER BY probed_at DESC LIMIT 10""",
                            service_id,
                        )
                        if recent:
                            total_p = len(recent)
                            err_count = sum(1 for r in recent if not r["reachable"])
                            avg_ms = sum((r["response_time_ms"] or 0) for r in recent) / total_p
                            err_rate = err_count / total_p
                            await conn2.execute(
                                """INSERT INTO service_health
                                   (slug, avg_response_ms, error_rate, last_probe_at, probe_count)
                                   VALUES ($1, $2, $3, NOW(), $4)
                                   ON CONFLICT (slug) DO UPDATE SET
                                       avg_response_ms = EXCLUDED.avg_response_ms,
                                       error_rate = EXCLUDED.error_rate,
                                       last_probe_at = EXCLUDED.last_probe_at,
                                       probe_count = EXCLUDED.probe_count""",
                                catalog_slug, avg_ms, err_rate, total_p,
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

    check_service_margins()
    logger.info(f"Wayforth API starting, environment={ENVIRONMENT}")
    try:
        await get_jwks()  # S10 (v0.7.8): get_jwks is now async/httpx
        logger.info("JWKS cache pre-warmed (%d keys)", len(_jwks_cache["keys"]))
    except Exception as _jwks_err:
        logger.warning("JWKS pre-warm failed (will retry on first request): %s", _jwks_err)
    # L1 (v0.7.8): startup messages go through the logger so they pick up
    # the configured level/format and land in any aggregation we add later.
    logger.info("STARTUP: running check_db()")
    ok = check_db()
    if not ok:
        logger.warning("STARTUP: check_db failed, _DB_URL prefix=%r", _DB_URL[:20])
    app.state.db_ok = ok
    app.state.anon_searches = {}
    logger.info("STARTUP: creating asyncpg pool url_prefix=%r", _ASYNCPG_URL[:20])
    try:
        app.state.pool = await asyncpg.create_pool(
            _ASYNCPG_URL,
            # P6 (v0.7.8): bumped from 20 to 40 for production-grade throughput.
            # At ~200 req/s with ~50ms DB latency, 20 saturates quickly. 40
            # gives 2-3× headroom. Configurable via env so we can tune per
            # deploy without a code change.
            min_size=int(os.environ.get("WAYFORTH_DB_POOL_MIN", "2")),
            max_size=int(os.environ.get("WAYFORTH_DB_POOL_MAX", "40")),
            command_timeout=30.0,
            # Recycle connections idle >300s so Railway never reaches its
            # ~10-minute idle-disconnect before we do (300s = 5-min safety margin).
            max_inactive_connection_lifetime=300.0,
            server_settings={
                "tcp_keepalives_idle":     "60",
                "tcp_keepalives_interval": "10",
                "tcp_keepalives_count":    "5",
            },
        )
        app.state.db_ok = True
        async with app.state.pool.acquire() as _mconn:
            await _mconn.execute("""
                ALTER TABLE services
                    ADD COLUMN IF NOT EXISTS wri_score FLOAT,
                    ADD COLUMN IF NOT EXISTS wri_version TEXT DEFAULT 'v1',
                    ADD COLUMN IF NOT EXISTS avg_latency_ms FLOAT,
                    ADD COLUMN IF NOT EXISTS region TEXT
            """)
            # Sync api_keys.tier ↔ user_credits.package_tier so both fields
            # always agree. api_keys.tier is authoritative (controls rate limits
            # and feature gates); package_tier follows it. This self-heals any
            # divergence caused by Stripe webhooks or admin operations before the
            # two-field update was in place.
            await _mconn.execute("""
                UPDATE user_credits uc
                SET package_tier = ak.tier,
                    updated_at   = NOW()
                FROM (
                    SELECT DISTINCT ON (user_id) user_id, tier
                    FROM api_keys
                    WHERE active = true
                    ORDER BY user_id,
                             (encrypted_key IS NOT NULL) DESC,
                             created_at DESC
                ) ak
                WHERE uc.user_id = ak.user_id
                  AND uc.package_tier IS DISTINCT FROM ak.tier
                  AND ak.tier IS NOT NULL
            """)
            await _mconn.execute("""
                ALTER TABLE user_service_keys
                    ADD COLUMN IF NOT EXISTS endpoint_url TEXT,
                    ADD COLUMN IF NOT EXISTS default_method TEXT DEFAULT 'POST'
            """)
            await _mconn.execute("""
                ALTER TABLE api_keys
                    ADD COLUMN IF NOT EXISTS billing_cadence VARCHAR NOT NULL DEFAULT 'monthly'
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
            # Per-alert HMAC secret. Previously alerts were signed with the
            # api_key_id (a UUID, low entropy AND leakable via admin views), so
            # an attacker who learned the api_key_id could forge `wri.threshold_
            # crossed` callbacks to the user's notify_url. New alerts generate a
            # 32-byte random secret returned once at creation.
            await _mconn.execute("""
                ALTER TABLE wri_alerts
                    ADD COLUMN IF NOT EXISTS hmac_secret TEXT
            """)
            # S15 (v0.7.8): backfill any pre-hmac_secret rows with fresh 32-byte
            # secrets so the api_key_id fallback can be removed from the
            # delivery path. Idempotent — only touches NULL or UUID-shaped rows.
            await _mconn.execute("""
                UPDATE wri_alerts
                SET hmac_secret = encode(gen_random_bytes(32), 'hex')
                WHERE hmac_secret IS NULL
                   OR hmac_secret = api_key_id::text
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
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    webhook_id UUID REFERENCES provider_webhooks(id),
                    user_id UUID,
                    event TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'delivered', 'dead')),
                    next_retry_at TIMESTAMPTZ,
                    last_attempted_at TIMESTAMPTZ,
                    response_status INTEGER,
                    error TEXT,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS webhook_deliveries_retry_idx
                ON webhook_deliveries(next_retry_at, status)
                WHERE status = 'pending'
            """)
            # v0.7.7: track suspension state on the webhook itself
            await _mconn.execute("""
                ALTER TABLE provider_webhooks
                ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ
            """)
            # v0.7.7: index for per-webhook delivery history lookup
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS webhook_deliveries_webhook_id_idx
                ON webhook_deliveries(webhook_id, created_at DESC)
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
            # S16 (v0.7.8): only one provider may own a given service_slug
            # globally. We attempt the constraint conditionally — if existing
            # rows have dup slugs (legacy data) we log and skip so startup
            # doesn't crash. Operators reconcile dups via admin tooling and
            # restart; the constraint then takes hold.
            await _mconn.execute("""
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname = 'provider_services_slug_unique'
                    ) THEN
                        IF NOT EXISTS (
                            SELECT 1 FROM provider_services
                            GROUP BY service_slug
                            HAVING COUNT(*) > 1
                        ) THEN
                            ALTER TABLE provider_services
                              ADD CONSTRAINT provider_services_slug_unique
                              UNIQUE (service_slug);
                        ELSE
                            RAISE WARNING 'provider_services has duplicate service_slug rows; UNIQUE constraint NOT added. Reconcile and restart to enforce.';
                        END IF;
                    END IF;
                END $$;
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS provider_sessions_token_idx ON provider_sessions(token)
            """)
            # 040 (inline) — hash provider session tokens at rest. Previously the
            # raw token was stored in the `token` column and matched directly on
            # lookup, so anyone reading provider_sessions (backup leak, replica
            # access) could hijack any active provider session. We add a
            # `token_hash` column, backfill it from the existing raw token for
            # any rows that don't have one yet, and update routers/provider.py +
            # routers/mfa.py to look up by hash. New sessions write only the
            # hash; the raw `token` column is left in place but is no longer
            # consulted, and will be dropped in a follow-up migration once all
            # existing sessions have rotated out.
            # Stripe webhook idempotency: every event we successfully process
            # is recorded by event id. On replay (Stripe retries on 5xx /
            # network issues by design), we look the id up and short-circuit.
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS stripe_events (
                    event_id        TEXT PRIMARY KEY,
                    event_type      TEXT NOT NULL,
                    processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            await _mconn.execute("""
                ALTER TABLE provider_sessions
                    ADD COLUMN IF NOT EXISTS token_hash TEXT
            """)
            await _mconn.execute("""
                UPDATE provider_sessions
                SET token_hash = encode(sha256(token::bytea), 'hex')
                WHERE token_hash IS NULL AND token IS NOT NULL
            """)
            await _mconn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS provider_sessions_token_hash_uniq
                ON provider_sessions(token_hash)
                WHERE token_hash IS NOT NULL
            """)
            await _mconn.execute("""
                ALTER TABLE api_keys
                    ADD COLUMN IF NOT EXISTS calls_count INTEGER NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS monthly_calls_count INTEGER NOT NULL DEFAULT 0,
                    ADD COLUMN IF NOT EXISTS monthly_calls_reset_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS monthly_searches INTEGER NOT NULL DEFAULT 0
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS service_health (
                    slug            TEXT PRIMARY KEY,
                    avg_response_ms FLOAT,
                    error_rate      FLOAT,
                    last_probe_at   TIMESTAMPTZ,
                    probe_count     INTEGER NOT NULL DEFAULT 0
                )
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
            # v0.6.8 migrations
            await _mconn.execute("""
                ALTER TABLE api_keys
                    ADD COLUMN IF NOT EXISTS dunning_failure_count INTEGER NOT NULL DEFAULT 0
            """)
            await _mconn.execute("""
                ALTER TABLE user_credits
                    ADD COLUMN IF NOT EXISTS warning_80_sent_at TIMESTAMPTZ,
                    ADD COLUMN IF NOT EXISTS warning_95_sent_at TIMESTAMPTZ
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS service_favorites (
                    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    slug       TEXT NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (user_id, slug)
                )
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS referrals (
                    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    referrer_user_id UUID NOT NULL REFERENCES users(id),
                    referred_user_id UUID REFERENCES users(id),
                    code             TEXT UNIQUE NOT NULL,
                    redeemed_at      TIMESTAMPTZ,
                    created_at       TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS referrals_code_idx ON referrals(code)
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS organizations (
                    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name          TEXT NOT NULL,
                    owner_user_id UUID NOT NULL REFERENCES users(id),
                    created_at    TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS org_members (
                    org_id    UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                    user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    role      TEXT NOT NULL DEFAULT 'member'
                        CHECK (role IN ('owner','admin','member')),
                    joined_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (org_id, user_id)
                )
            """)
            await _mconn.execute(
                "ALTER TABLE providers ADD COLUMN IF NOT EXISTS suspended BOOLEAN NOT NULL DEFAULT FALSE"
            )
            # v0.6.9 migrations
            await _mconn.execute("""
                ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS founding_member          BOOLEAN     NOT NULL DEFAULT true,
                    ADD COLUMN IF NOT EXISTS founding_bonus_granted_at TIMESTAMPTZ
            """)
            await _mconn.execute("""
                DROP TABLE IF EXISTS wayf_points_log
            """)
            await _mconn.execute("""
                DROP TABLE IF EXISTS wayf_points
            """)
            # v0.6.10 migrations
            await _mconn.execute("""
                UPDATE services
                SET category = 'search'
                WHERE category = 'agents'
                  AND (name ILIKE '%tavily%' OR name ILIKE '%brave%' OR name ILIKE '%exa%')
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS credit_transactions_spend_anomaly_idx
                ON credit_transactions(user_id, created_at)
                WHERE type = 'execution' AND amount < 0
            """)
            # v0.6.10 — remove first-party labs summarizer from public catalog
            await _mconn.execute("""
                DELETE FROM services
                WHERE slug = 'wayforth_labs_summarizer'
                   OR (endpoint_url ILIKE '%labs-production%' AND name ILIKE '%summarizer%')
            """)
            # 039_mfa — TOTP MFA columns + challenge table
            await _mconn.execute("""
                ALTER TABLE users
                    ADD COLUMN IF NOT EXISTS mfa_secret       TEXT,
                    ADD COLUMN IF NOT EXISTS mfa_enabled      BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS mfa_backup_codes TEXT[],
                    ADD COLUMN IF NOT EXISTS mfa_enabled_at   TIMESTAMPTZ
            """)
            await _mconn.execute("""
                ALTER TABLE providers
                    ADD COLUMN IF NOT EXISTS mfa_secret       TEXT,
                    ADD COLUMN IF NOT EXISTS mfa_enabled      BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS mfa_backup_codes TEXT[],
                    ADD COLUMN IF NOT EXISTS mfa_enabled_at   TIMESTAMPTZ
            """)
            await _mconn.execute("""
                ALTER TABLE admin_users
                    ADD COLUMN IF NOT EXISTS mfa_secret       TEXT,
                    ADD COLUMN IF NOT EXISTS mfa_enabled      BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS mfa_backup_codes TEXT[],
                    ADD COLUMN IF NOT EXISTS mfa_enabled_at   TIMESTAMPTZ
            """)
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS mfa_challenges (
                    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_type  TEXT        NOT NULL CHECK (user_type IN ('user', 'provider', 'admin')),
                    user_id    UUID        NOT NULL,
                    token_hash TEXT        NOT NULL UNIQUE,
                    expires_at TIMESTAMPTZ NOT NULL,
                    used       BOOLEAN     NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS idx_mfa_challenges_token_hash ON mfa_challenges (token_hash)
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS idx_mfa_challenges_expires_at ON mfa_challenges (expires_at)
            """)
            # P3/P4/P5 (v0.7.8): three hot-path composite indexes.
            #  - api_keys(key_hash, active): every authenticated request hits
            #    this exact WHERE clause; idx_api_keys_hash_active short-circuits
            #    the UPDATE...RETURNING used by check_auth.
            #  - search_analytics(user_id, created_at DESC): /account/analytics
            #    and history queries filter by user_id with a recent-time bound;
            #    the existing single-column index on created_at can't help.
            #  - credit_transactions(user_id, type, created_at DESC): monthly
            #    spend aggregates filter by user_id+type and order by created_at.
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS idx_api_keys_hash_active
                  ON api_keys(key_hash, active)
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS idx_search_analytics_user_created
                  ON search_analytics(user_id, created_at DESC)
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS idx_credit_tx_user_type_created
                  ON credit_transactions(user_id, type, created_at DESC)
            """)
            # E8 (v0.7.8): idempotency key for refunds. Callers that have a
            # stable per-failure key pass refund_idempotency_key to _do_refund;
            # the partial unique index makes a duplicate insert raise
            # UniqueViolation so the caller knows the refund was already
            # issued. Existing rows without a key remain valid.
            await _mconn.execute("""
                ALTER TABLE credit_transactions
                    ADD COLUMN IF NOT EXISTS refund_uuid UUID
            """)
            await _mconn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_tx_refund_uuid
                  ON credit_transactions(refund_uuid)
                  WHERE refund_uuid IS NOT NULL
            """)
            # x402 settlement dedup table — added in v0.7.8 Section 9, wired in
            # v0.8.0 Item 1. The INSERT happens in routers/x402.py (x402_execute
            # and x402_search), AFTER payment verification and AFTER tier/flag
            # gates, BEFORE the service adapter call. UNIQUE(payment_hash) makes
            # a replayed X-PAYMENT header raise UniqueViolation, which is
            # translated to a 400 replay_rejected response.
            await _mconn.execute("""
                CREATE TABLE IF NOT EXISTS x402_settlements (
                    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    payment_hash  TEXT NOT NULL,
                    amount        NUMERIC NOT NULL,
                    service_slug  TEXT NOT NULL,
                    user_id       UUID REFERENCES users(id),
                    settled_at    TIMESTAMPTZ DEFAULT NOW(),
                    CONSTRAINT x402_settlements_payment_hash_unique
                        UNIQUE (payment_hash)
                )
            """)
            await _mconn.execute("""
                CREATE INDEX IF NOT EXISTS x402_settlements_user_idx
                  ON x402_settlements(user_id, settled_at DESC)
            """)
    except Exception as e:
        logger.error("STARTUP ERROR: %s: %s", type(e).__name__, e, exc_info=True)
        logger.critical("DB pool creation or migrations failed: %s — exiting so the orchestrator can restart cleanly", e)
        app.state.pool = None
        # E4 (v0.7.8): fail-fast on DB unreachable. Previously the app would
        # start with pool=None and 503 every request — Railway's health check
        # might not flag this state, leaving users staring at a broken service.
        # Exit so the orchestrator restarts; if the DB stays down, the
        # restart-crash cycle is the correct visible signal.
        import sys
        sys.exit(1)
    else:
        logger.info("STARTUP: pool created and migrations complete")
        # P6 (v0.7.8): log the configured pool sizes so saturation is easy to
        # spot in Railway logs.
        try:
            from core.db import get_pool_stats
            _stats = get_pool_stats(app.state.pool)
            logger.info(
                "DB pool ready min=%s max=%s size=%s idle=%s",
                os.environ.get("WAYFORTH_DB_POOL_MIN", "2"),
                os.environ.get("WAYFORTH_DB_POOL_MAX", "40"),
                _stats.get("size"), _stats.get("idle"),
            )
        except Exception:
            pass
    cleanup_task = asyncio.create_task(_cleanup_anon_searches_loop(app))
    watcher_task = asyncio.create_task(_usdc_payment_watcher())
    renewal_task = asyncio.create_task(_usdc_renewal_reminder())
    reset_task = asyncio.create_task(_monthly_topup_reset())
    probe_task = asyncio.create_task(_probe_managed_services_loop())
    webhook_retry_task = asyncio.create_task(_webhook_retry_loop())
    _get_redis()  # eagerly init so the rate-limiter log line appears at startup
    yield
    cleanup_task.cancel()
    watcher_task.cancel()
    renewal_task.cancel()
    reset_task.cancel()
    probe_task.cancel()
    webhook_retry_task.cancel()
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
    contact={"name": "Wayforth Technologies Inc.", "url": "https://wayforth.io/contact"},
    license_info={"name": "BSL 1.1", "url": "https://wayforth.io/license"},
    lifespan=lifespan,
)

# ── Middleware ────────────────────────────────────────────────────────────────

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
# CORS:
# - With `allow_credentials=True`, a cookie-bearing request from any origin in
#   `allow_origins` or matching `allow_origin_regex` will succeed. The previous
#   regex `r"https://[^.]+\.lovable\.app"` admitted ANY subdomain of lovable.app
#   — a shared third-party hosting service. Anyone who can publish a site there
#   could issue authenticated XHRs against the API from a browser session.
#   We keep the two explicit Lovable preview origins that the dashboard uses
#   and drop the wildcard regex. Auth is primarily header-based
#   (X-Wayforth-API-Key), so cookie CSRF surface is limited, but tightening
#   this is defense in depth.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://wayforth.io",
        "https://www.wayforth.io",
        "https://gateway.wayforth.io",
        "https://mcp.wayforth.io",
        "https://zeropointaccess.com",
        "https://www.zeropointaccess.com",
        "https://id-preview--1f7c5e7e-c191-4274-b4a6-f6e732da08d9.lovable.app",
        "https://intent-exchange.lovable.app",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "DELETE", "PUT"],
    allow_headers=["*"],
)


# Request body size limits (defense-in-depth against memory exhaustion / JSON
# bombs). Most endpoints take small JSON payloads — capped at 1 MB. /run and
# /execute (including /execute/batch and /run/intents) carry larger payloads
# (LLM messages, audio URLs, multi-call batches) and get 4 MB.
_BODY_LIMIT_DEFAULT = 1 * 1024 * 1024       # 1 MB
_BODY_LIMIT_LARGE   = 4 * 1024 * 1024       # 4 MB
_LARGE_BODY_PREFIXES = ("/run", "/execute")


class BodySizeLimitMiddleware:
    """ASGI middleware enforcing per-path request body size limits.

    Two-layer enforcement:
      1. **Content-Length header** — cheap rejection before any body is read.
         Covers >99% of real-world requests (all standard JSON clients send CL).
      2. **Stream byte counter** — for requests without Content-Length (chunked
         transfer-encoding), we wrap `receive` to count bytes as they arrive.
         When the limit is crossed mid-stream, we truncate the body delivered
         to the app so the endpoint sees a short/malformed payload and fails
         with its own 4xx — the request can't grow unbounded.

    Returns a 413 JSON body when the Content-Length check fires.
    """

    def __init__(self, app, default_limit: int, large_limit: int,
                 large_path_prefixes: tuple[str, ...]):
        self.app = app
        self.default_limit = default_limit
        self.large_limit = large_limit
        self.large_path_prefixes = large_path_prefixes

    def _limit_for(self, path: str) -> int:
        for prefix in self.large_path_prefixes:
            if path == prefix or path.startswith(prefix + "/"):
                return self.large_limit
        return self.default_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""
        limit = self._limit_for(path)

        # Layer 1: Content-Length check.
        for hname, hval in scope.get("headers", []):
            if hname == b"content-length":
                try:
                    declared = int(hval.decode("latin-1"))
                except (UnicodeDecodeError, ValueError):
                    break  # malformed; fall through to stream count
                if declared > limit:
                    await self._send_413(send, declared, limit, path)
                    return
                break

        # Layer 2: stream byte counter for chunked / missing-CL requests.
        total = 0

        async def counting_receive():
            nonlocal total
            msg = await receive()
            if msg.get("type") == "http.request":
                body = msg.get("body", b"") or b""
                total += len(body)
                if total > limit:
                    # Truncate the chunk and signal end-of-body to the app.
                    # The app will see a short payload and produce a 4xx of
                    # its own — we can't cleanly inject a 413 here because
                    # the app may have already started its response.
                    overflow = total - limit
                    return {
                        "type": "http.request",
                        "body": body[: len(body) - overflow] if overflow < len(body) else b"",
                        "more_body": False,
                    }
            return msg

        await self.app(scope, counting_receive, send)

    @staticmethod
    async def _send_413(send, size: int, limit: int, path: str) -> None:
        import json as _json
        body = _json.dumps({
            "error": "payload_too_large",
            "limit_bytes": limit,
            "size_bytes": size,
            "path": path,
            "message": f"Request body of {size} bytes exceeds the {limit}-byte limit for this endpoint.",
        }).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("latin-1")),
                (b"connection", b"close"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


app.add_middleware(
    BodySizeLimitMiddleware,
    default_limit=_BODY_LIMIT_DEFAULT,
    large_limit=_BODY_LIMIT_LARGE,
    large_path_prefixes=_LARGE_BODY_PREFIXES,
)


class SessionCookieMiddleware:
    """ASGI middleware: resolve the wf_session cookie against Redis on every
    request, stash the session record on scope for endpoints that want it.

    Never raises on missing / invalid / expired cookies — the result is just
    that `request.scope["wayforth_session"]` is unset. Endpoints that want
    cookie auth call `core.session.get_request_session(request)` and fall
    back to Bearer JWT or X-Wayforth-API-Key as before.

    Hard requirement: Redis. If Redis is unavailable we attach no session and
    the request proceeds anonymously through the existing auth code paths.
    This is the right fail-mode — refusing every request because we can't
    look up sessions would be a self-inflicted outage when the cookie is
    only one of several auth options.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw_token = _extract_wf_session_cookie(scope.get("headers", []))
        if raw_token:
            from core.tier_gates import _get_redis as _session_get_redis
            from core.session import get_session, _stash_on_scope
            redis = _session_get_redis()
            if redis is not None:
                try:
                    record = await get_session(redis, raw_token)
                    if record:
                        _stash_on_scope(scope, record, raw_token)
                except Exception as exc:
                    # Lookup failure is non-fatal — endpoints fall back to JWT
                    # or API-key auth. We just log so silent Redis flakiness
                    # is observable.
                    logger.warning("SessionCookieMiddleware redis lookup failed: %s", exc)

        await self.app(scope, receive, send)


def _extract_wf_session_cookie(headers: list) -> str | None:
    """Pull `wf_session` out of the request's Cookie header(s)."""
    from http.cookies import SimpleCookie
    raw_token: str | None = None
    for hname, hval in headers:
        if hname != b"cookie":
            continue
        try:
            cookie_str = hval.decode("latin-1")
        except UnicodeDecodeError:
            continue
        try:
            jar: SimpleCookie = SimpleCookie()
            jar.load(cookie_str)
            morsel = jar.get("wf_session")
            if morsel and morsel.value:
                raw_token = morsel.value
                break
        except Exception:
            # Malformed Cookie header → ignore this header line; another
            # one may carry a valid session.
            continue
    return raw_token


app.add_middleware(SessionCookieMiddleware)


@app.middleware("http")
async def docs_redirect(request: Request, call_next):
    if request.headers.get("host", "").startswith("docs.wayforth.io"):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("https://gateway.wayforth.io/guide/")
    return await call_next(request)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    # Accept caller-supplied trace ID or generate a new one
    trace_id = request.headers.get("X-Wayforth-Trace-ID") or str(uuid_lib.uuid4())
    request.state.trace_id = trace_id
    request.state.request_id = trace_id  # keep backward-compat alias
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if raw_key:
        request.state.api_key = raw_key
    logger.info("req_start id=%s method=%s path=%s", trace_id, request.method, request.url.path)
    response = await call_next(request)
    response.headers["X-Wayforth-Trace-ID"] = trace_id
    response.headers["X-Request-ID"] = trace_id
    response.headers["X-Wayforth-Request-ID"] = trace_id
    response.headers["X-Wayforth-Version"] = VERSION
    response.headers["X-RateLimit-Tier"] = str(getattr(request.state, "rate_limit_tier", "free"))
    response.headers["X-RateLimit-Limit"] = str(getattr(request.state, "rate_limit_rpm", "10"))
    response.headers["X-RateLimit-Remaining"] = str(getattr(request.state, "ratelimit_remaining", -1))
    response.headers["X-RateLimit-Reset"] = str(getattr(request.state, "ratelimit_reset", 0))
    credits_remaining = getattr(request.state, "credits_remaining", None)
    credits_total = getattr(request.state, "credits_total", None)
    if credits_remaining is not None:
        response.headers["X-Credits-Remaining"] = str(credits_remaining)
    if credits_total is not None:
        response.headers["X-Credits-Total"] = str(credits_total)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://gateway.wayforth.io https://*.supabase.co; "
        "frame-ancestors 'none';"
    )
    # Ensure auth and account responses are never cached by proxies or browsers
    path = request.url.path
    if path.startswith(("/auth/", "/account/")):
        response.headers["Cache-Control"] = "no-store"
    return response


# Register _AuthError exception handler
app.add_exception_handler(_AuthError, _auth_error_handler)


# v0.7.7: Normalise HTTP 402 to the canonical insufficient_credits shape.
# x402 routes return JSONResponse directly and are unaffected.
async def _payment_required_handler(request: Request, exc: HTTPException):
    if exc.status_code == 402 and isinstance(exc.detail, dict):
        d = exc.detail
        return JSONResponse(status_code=402, content={
            "error": d.get("error", "insufficient_credits"),
            "credits_remaining": (
                d.get("credits_balance")
                or d.get("current_balance_credits")
                or d.get("credits_remaining")
                or 0
            ),
            "credits_required": (
                d.get("credits_needed")
                or d.get("credits_required")
                or d.get("required_credits")
                or 1
            ),
            "upgrade_url": "https://wayforth.io/pricing",
        })
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

app.add_exception_handler(HTTPException, _payment_required_handler)

# ── Auth dependency (used by /search, /query) ─────────────────────────────────

async def check_auth(request: Request) -> dict:
    """Unified auth dependency for /search and /query."""
    from routers.billing import _credits_to_tier
    ip = get_real_ip(request)
    raw_key = request.headers.get("X-Wayforth-API-Key", "")

    if raw_key:
        # S14 (v0.7.8): tighten from 40-60 range to the two exact lengths we
        # actually mint: 56 chars for "wf_live_" + token_hex(24), 51 chars for
        # "wf_live_" + token_urlsafe(32). Both formats are in production; do
        # NOT collapse to a single length without first migrating live keys.
        if not raw_key.startswith("wf_live_") or len(raw_key) not in (51, 56):
            raise _AuthError(401, {
                "error": "invalid_key",
                "message": "Invalid API key format.",
            })
        pool = request.app.state.pool
        if not pool:
            raise HTTPException(status_code=503, detail="Database unavailable")
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        async with pool.acquire() as db:
            # Atomic check-and-increment (see core/auth.py for rationale).
            key = await db.fetchrow("""
                UPDATE api_keys
                SET usage_this_month = usage_this_month + 1,
                    last_used_at = NOW()
                WHERE key_hash = $1
                  AND active = TRUE
                  AND (monthly_quota = 0 OR usage_this_month < monthly_quota)
                RETURNING id, user_id, tier, rate_limit_per_minute, monthly_quota,
                          usage_this_month, quota_reset_at, active,
                          payment_rail, subscription_expires_at
            """, key_hash)

        if not key:
            async with pool.acquire() as db:
                existing = await db.fetchrow(
                    "SELECT active FROM api_keys WHERE key_hash = $1", key_hash,
                )
            if not existing or not existing["active"]:
                raise _AuthError(401, {
                    "error": "invalid_key",
                    "message": "Invalid API key. Get yours at wayforth.io/dashboard",
                })
            raise _AuthError(429, {
                "error": "quota_exceeded",
                "message": "Monthly quota exceeded. Upgrade at wayforth.io/pricing",
                "upgrade_url": "https://wayforth.io/pricing",
            })

        # Graceful USDC subscription expiry
        if (key.get("payment_rail") == "usdc" and key.get("subscription_expires_at")
                and key["subscription_expires_at"] < datetime.now(timezone.utc)):
            from core.credits import _downgrade_expired_usdc
            asyncio.create_task(_downgrade_expired_usdc(str(key["id"])))

        rpm = _TIER_RPM.get(key["tier"], 10)
        tier = key["tier"] or "free"
        calls_included = PLANS.get(tier, {}).get("calls_included", 100)
        usage = key["usage_this_month"]
        request.state.rate_limit_tier = tier
        request.state.rate_limit_rpm = rpm
        request.state.ratelimit_remaining = max(0, calls_included - usage)
        if key.get("quota_reset_at"):
            request.state.ratelimit_reset = int(key["quota_reset_at"].timestamp())
        else:
            from datetime import timedelta
            _now = datetime.now(timezone.utc)
            _next = (_now.replace(day=28) + timedelta(days=4)).replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            request.state.ratelimit_reset = int(_next.timestamp())
        return {
            "authenticated": True,
            "tier": tier,
            "key_id": str(key["id"]),
            "user_id": str(key["user_id"]) if key["user_id"] else None,
            "usage_this_month": usage,
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
    request.state.ratelimit_remaining = max(0, _ANON_DAILY_LIMIT - (count + 1))
    from datetime import timedelta
    _now = datetime.now(timezone.utc)
    _end_of_day = _now.replace(hour=23, minute=59, second=59, microsecond=0)
    request.state.ratelimit_reset = int(_end_of_day.timestamp())
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
from routers.org import router as org_router
from routers.mfa import router as mfa_router

app.include_router(search.router)
app.include_router(execute.router)
app.include_router(billing.router)
app.include_router(webhooks.router)
app.include_router(provider.router)
app.include_router(admin.router)
app.include_router(x402.router)
app.include_router(auth.router)
app.include_router(agent.router)
app.include_router(org_router)
app.include_router(mfa_router)


# ── OpenAPI customisation (security scheme + description) ─────────────────────

_OPENAPI_HIDDEN_PREFIXES = ("/admin", "/admin-api", "/tier3/admin")


def _public_routes():
    """Routes shown in /openapi.json. Admin and internal-provider routes are
    excluded so the public schema doesn't catalogue privileged endpoints."""
    out = []
    for r in app.routes:
        path = getattr(r, "path", "") or ""
        if any(path == p or path.startswith(p + "/") or path.startswith(p) for p in _OPENAPI_HIDDEN_PREFIXES):
            continue
        out.append(r)
    return out


def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi
    schema = get_openapi(
        title="Wayforth API",
        version=VERSION,
        description=(
            "Wayforth is the agent-native service discovery and routing layer for AI. "
            "Discover, compare, and call 300+ API services through a single endpoint.\n\n"
            "**Authentication**: All endpoints (except `/status`, `/health`, `/search`) "
            "require the `X-Wayforth-API-Key` header. "
            "Get your key at [wayforth.io/dashboard](https://wayforth.io/dashboard).\n\n"
            "**Rate-limit headers** returned on every authenticated response:\n"
            "- `X-RateLimit-Tier` — your tier (free / builder / starter / pro / growth / enterprise)\n"
            "- `X-RateLimit-Limit` — calls per minute allowed for your tier\n"
            "- `X-RateLimit-Remaining` — calls remaining this month\n"
            "- `X-RateLimit-Reset` — Unix timestamp when your monthly quota resets\n"
            "- `X-Request-ID` — unique UUID for every request, traceable in logs"
        ),
        routes=_public_routes(),
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "ApiKeyAuth": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Wayforth-API-Key",
            "description": "Wayforth API key — get yours at https://wayforth.io/dashboard",
        }
    }
    schema["security"] = [{"ApiKeyAuth": []}]
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi


# ── Static files ──────────────────────────────────────────────────────────────

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass  # static dir may not exist in all environments

try:
    app.mount("/guide", StaticFiles(directory="static/guide", html=True), name="guide")
except Exception:
    pass  # packages/docs may not exist in all environments

# ── Core health / system routes ───────────────────────────────────────────────

_ROBOTS_TXT = """\
User-agent: *
Disallow: /admin/
Disallow: /admin-api/
Disallow: /provider/
Allow: /
"""

_SECURITY_TXT = """\
Contact: mailto:security@wayforth.io
Policy: https://wayforth.io/security
Preferred-Languages: en
"""

@app.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    return PlainTextResponse(_ROBOTS_TXT)


@app.get("/.well-known/security.txt", include_in_schema=False)
async def well_known_security_txt():
    return PlainTextResponse(_SECURITY_TXT, media_type="text/plain; charset=utf-8")


@app.get("/security", tags=["System"])
async def security_policy():
    """Security disclosure policy — contact and reporting URL."""
    return PlainTextResponse(_SECURITY_TXT, media_type="text/plain; charset=utf-8")


_SECURITY_POLICY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Wayforth Security Policy</title>
  <style>
    body{font-family:system-ui,sans-serif;max-width:720px;margin:3rem auto;padding:0 1rem;color:#1a1a1a}
    h1{font-size:1.75rem;margin-bottom:.5rem}
    h2{font-size:1.1rem;margin-top:2rem;color:#444}
    a{color:#2563eb}
    code{background:#f3f4f6;padding:.1em .3em;border-radius:3px}
    .badge{display:inline-block;background:#d1fae5;color:#065f46;padding:.2em .6em;border-radius:.4em;font-size:.85rem;font-weight:600}
  </style>
</head>
<body>
  <h1>Wayforth Security Policy</h1>
  <p><span class="badge">DEPLOYED</span></p>
  <h2>Reporting a Vulnerability</h2>
  <p>Email <a href="mailto:security@wayforth.io">security@wayforth.io</a> with a description of the issue,
  steps to reproduce, and potential impact. We aim to acknowledge reports within 48 hours and provide
  a remediation timeline within 5 business days.</p>
  <h2>Scope</h2>
  <ul>
    <li>Wayforth API (<code>gateway.wayforth.io</code>)</li>
    <li>Wayforth Dashboard (<code>wayforth.io</code>)</li>
    <li>Wayforth MCP Server (<code>pypi.org/project/wayforth-mcp</code>)</li>
  </ul>
  <h2>Out of Scope</h2>
  <ul>
    <li>Third-party services accessed via the Wayforth catalog</li>
    <li>Denial-of-service attacks</li>
    <li>Social engineering</li>
  </ul>
  <h2>Disclosure Policy</h2>
  <p>We follow coordinated disclosure. Please allow us reasonable time to patch before publishing details.
  We do not currently offer a bug bounty program but will acknowledge contributors in our changelog.</p>
  <h2>Contact</h2>
  <p>Contact: <a href="mailto:security@wayforth.io">security@wayforth.io</a><br>
  Policy: <a href="https://wayforth.io/security">https://wayforth.io/security</a></p>
</body>
</html>"""


@app.get("/security-policy", include_in_schema=False)
async def security_policy_html():
    """Full security disclosure policy as HTML."""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_SECURITY_POLICY_HTML)


@app.get("/health")
@limiter.limit("60/minute")
async def health(request: Request):
    from core.db import get_pool_stats
    pool = getattr(request.app.state, "pool", None)
    # Section 6 (v0.7.8): rename catalog fields to disambiguate. The old shape
    # had `catalog.total` filtered by `consecutive_failures < 3` while the
    # admin dashboard separately read raw COUNT(*) — different definitions
    # of "total" looked like contradictory numbers. New shape:
    #   total_services   = SELECT COUNT(*) FROM services (raw, unfiltered)
    #   healthy_services = WHERE consecutive_failures < 3 (live, reachable)
    #   tier2_verified   = healthy + coverage_tier >= 2 (curated)
    #   managed_services = count of in-process managed adapters
    # Old fields `total` and `tier2` retained for one minor release as
    # backwards-compat aliases — drop in v0.9.
    managed = _active_managed_count()
    if pool is None:
        return {
            "status": "degraded",
            "service": "wayforth-api",
            "version": VERSION,
            "db_status": "unavailable",
            "catalog": {
                "total_services": 0,
                "healthy_services": 0,
                "tier2_verified": 0,
                "managed_services": managed,
                # legacy aliases — remove in v0.9
                "total": 0,
                "tier2": 0,
            },
            "managed_services": managed,
            "pool": {},
        }
    pool_stats = get_pool_stats(pool)
    try:
        async with pool.acquire(timeout=4.0) as conn:
            await conn.fetchval("SELECT 1")
            db_status = "ok"
            total_services   = await conn.fetchval("SELECT COUNT(*) FROM services") or 0
            healthy_services = await conn.fetchval("SELECT COUNT(*) FROM services WHERE consecutive_failures < 3") or 0
            tier2_verified   = await conn.fetchval("SELECT COUNT(*) FROM services WHERE coverage_tier >= 2 AND consecutive_failures < 3") or 0
    except Exception as e:
        logger.warning("/health catalog query failed: %s", e)
        db_status = "error"
        total_services = healthy_services = tier2_verified = 0
    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "service": "wayforth-api",
        "version": VERSION,
        "db_status": db_status,
        "catalog": {
            "total_services": total_services,
            "healthy_services": healthy_services,
            "tier2_verified": tier2_verified,
            "managed_services": managed,
            # legacy aliases — kept for one minor release; remove in v0.9.
            "total": healthy_services,
            "tier2": tier2_verified,
        },
        "managed_services": managed,
        "pool": pool_stats,
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
            "managed": _active_managed_count(),
        },
        "managed_services": _active_managed_count(),
        "searches_24h": searches,
        "mcp_tools": _active_managed_count(),
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
            "managed_services_x402": _active_managed_count(),
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


@app.get("/system/status", tags=["System"])
async def system_status_v075(db=Depends(get_db)):
    """Machine-readable component health — consumed by wayforth.io/status page."""
    import time as _time
    from services.managed import SERVICE_CONFIGS
    from services.param_mapper import MANAGED_TO_CATALOG

    components: dict[str, str] = {"api": "operational"}

    # catalog — DB reachability check
    try:
        t0 = _time.monotonic()
        await db.fetchval("SELECT 1 FROM services LIMIT 1")
        components["catalog"] = "operational" if (_time.monotonic() - t0) < 0.5 else "degraded"
    except Exception:
        components["catalog"] = "outage"

    # managed_services — only count services whose API key is actually configured.
    # Missing keys are expected gaps, not failures. A service without a key is simply
    # not yet active and must not contribute to the health status.
    configured_slugs = [
        slug for slug, cfg in SERVICE_CONFIGS.items()
        if os.environ.get(cfg["key_var"])
    ]
    if not configured_slugs:
        # No keys at all — early state, not an outage
        components["managed_services"] = "degraded"
    else:
        catalog_slugs = [MANAGED_TO_CATALOG.get(s, s) for s in configured_slugs]
        try:
            rows = await db.fetch(
                "SELECT slug, consecutive_failures FROM services WHERE slug = ANY($1::text[])",
                catalog_slugs,
            )
            failing = sum(1 for r in rows if (r["consecutive_failures"] or 0) > 2)
            total = len(rows)
            if failing == 0:
                components["managed_services"] = "operational"
            elif failing < total:
                components["managed_services"] = "degraded"
            else:
                # Every configured service is actively failing
                components["managed_services"] = "outage"
        except Exception:
            # Can't read probe data — degrade, don't call it an outage
            components["managed_services"] = "degraded"

    # payments
    try:
        await db.fetchval("SELECT 1 FROM package_purchases LIMIT 1")
        components["payments"] = "operational"
    except Exception:
        components["payments"] = "outage"

    # uptime_30d from service_probes if the table exists
    uptime_30d = 99.97
    try:
        row = await db.fetchrow("""
            SELECT
                COUNT(*) FILTER (WHERE outcome = 'success') AS ok,
                COUNT(*) AS total
            FROM service_probes
            WHERE created_at >= NOW() - INTERVAL '30 days'
        """)
        if row and row["total"] > 0:
            uptime_30d = round(100.0 * row["ok"] / row["total"], 2)
    except Exception:
        pass

    # Overall rollup:
    # "outage" = only when the api component itself is unreachable (gateway down).
    # If the handler is executing, api is by definition operational — so overall
    # outage can only be set explicitly if something sets components["api"] = "outage".
    # Everything else (catalog DB slow, payments DB error, some managed services
    # failing) degrades gracefully rather than declaring a full outage.
    if components.get("api") == "outage":
        overall = "outage"
    elif any(v in ("outage", "degraded") for v in components.values()):
        overall = "degraded"
    else:
        overall = "operational"

    return {
        "status": overall,
        "components": components,
        "uptime_30d": uptime_30d,
        "incidents": [],
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


# ── Changelog RSS feed ────────────────────────────────────────────────────────

_CHANGELOG_ENTRIES = [
    {
        "version": "0.7.0",
        "title": "Hardened",
        "date": "Fri, 22 May 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.7.0",
        "description": (
            "Professional penetration test completed; all findings resolved. "
            "Fail-closed USDC tx verification (was fail-open on RPC error). "
            "Stripe webhook idempotency via stripe_events(event_id PK) dedup table. "
            "Atomic check-and-increment on monthly api_key quota (TOCTOU fixed at all 3 auth sites). "
            "Provider session tokens now hashed at rest (sha256), matching admin sessions. "
            "/auth/register requires a verified Supabase Bearer JWT — body identity claims no longer trusted. "
            "MFA hardening: setup refuses overwrite when already enabled; developer disable requires Supabase JWT. "
            "BYOK /execute path: validate_external_url, header allow-list, 1 MB response cap, follow_redirects=False. "
            "Webhook delivery re-validates URLs at dispatch (DNS-rebinding defense). "
            "Admin role gates added on add-credits, regenerate-key, change-tier, reset-usage, suspend. "
            "x402: per-process replay store moved to Redis (SET NX); payment tolerance tightened 2% → 0.5%. "
            "CORS: dropped broad lovable.app wildcard regex with credentials=true. "
            "Login lockout now also keyed per-IP (credential stuffing defense). "
            "Global request body size middleware: 1 MB default, 4 MB on /run and /execute. "
            "186 tests passing, 0 failures."
        ),
    },
    {
        "version": "0.6.14",
        "title": "Economics",
        "date": "Mon, 19 May 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.6.14",
        "description": (
            "Stability AI credits: 45 → 65 (all services margin-positive at every tier). "
            "Annual billing: 2 months free, monthly credit replenishment. "
            "x402 fee model: markup on developer charge — providers receive 100% of their stated price. "
            "Cross-rail abstraction: card→x402 uses Wayforth operational wallet; "
            "USDC→managed routes via Circle CCTP. Rail is invisible to developers. "
            "Startup margin alert if any managed service falls below $0.005/call at Growth tier. "
            "216 tests passing, 0 failures."
        ),
    },
    {
        "version": "0.6.13",
        "title": "Gravity Prep",
        "date": "Sun, 17 May 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.6.13",
        "description": (
            "Provider payout engine (1.5% fee / 98.5% provider split) with earnings endpoints. "
            "Three new managed services: Firecrawl Scrape, Mistral AI, Google Gemini Flash. "
            "POST /admin/purge-test-accounts with dry_run support. "
            "Python SDK (wayforth-sdk 0.7.0). "
            "TypeScript SDK (@wayforth/sdk 0.7.0). "
            "Documentation site (packages/docs/). "
            "Public status page (wayforth.io/status). "
            "188 tests passing, 0 failures."
        ),
    },
    {
        "version": "0.6.12",
        "title": "Security Patch",
        "date": "Thu, 15 May 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.6.12",
        "description": (
            "/auth/register: blocked @wayforth.io registrations, fake UUID rejection, reserved prefix blocklist. "
            "/dashboard: search history scoped to authenticated user only. "
            "/memory: namespace keyed on user_id, not client-controlled agent_id. "
            "/search/popular: returns category counts only, never raw query strings. "
            "/submit: authentication required. "
            "/leaderboard/x402: capped at 50 results, sensitive fields stripped. "
            "/health-report: authentication required. "
            "/search: endpoint_url stripped from unauthenticated responses. "
            "188 tests passing, 0 failures."
        ),
    },
    {
        "version": "0.6.11",
        "title": "Security Hardening",
        "date": "Fri, 15 May 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.6.11",
        "description": (
            "Security hardening: x402 payment verification fail-closed, replay protection fixed, "
            "48 new security regression tests, Dockerfile pinned, XSS escaping in static HTML, "
            "non-root Docker user."
        ),
    },
    {
        "version": "0.6.10",
        "title": "Ecosystem",
        "date": "Fri, 15 May 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.6.10",
        "description": (
            "Automatic refunds: if an API call fails due to a service error, credits are restored instantly. "
            "No disputes. No support tickets. "
            "x402 native endpoint live — Wayforth is now discoverable on x402scan and Agentic.Market. "
            "133 end-to-end tests passing."
        ),
    },
    {
        "version": "0.6.9",
        "title": "WAYF token removed, Founding Developer Program, x402 search endpoint",
        "date": "Wed, 13 May 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.6.9",
        "description": "WAYF points system removed entirely. Founding Developer Program: users who join during v0.6.x receive 500 bonus calls on first paid subscription. GET /x402/search added as x402-native pay-per-call endpoint at $0.002 USDC per query.",
    },
    {
        "version": "0.6.8",
        "title": "Platform: usage alerts, dunning emails, forecasting, favorites, referrals, org accounts",
        "date": "Tue, 13 May 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.6.8",
        "description": "Usage alerts at 80%/95%, Stripe dunning with downgrade on 3rd failure, usage forecasting in /billing/balance, service favorites, referral program, and team/org accounts.",
    },
    {
        "version": "0.6.2",
        "title": "Rate limiting per tier, webhook retry loop, search analytics",
        "date": "Mon, 30 Mar 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.6.2",
        "description": "Per-tier RPM rate limits via SlowAPI, exponential-backoff webhook retry loop, search analytics table.",
    },
    {
        "version": "0.6.1",
        "title": "USDC payment rail, monthly topup reset, credits per call",
        "date": "Mon, 23 Mar 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.6.1",
        "description": "USDC subscription payment rail, automatic monthly topup reset, per-call credit deduction model.",
    },
    {
        "version": "0.6.0",
        "title": "Stripe billing, tier system, API key management",
        "date": "Mon, 16 Mar 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.6.0",
        "description": "Stripe subscription billing, 6-tier plan system (free/builder/starter/pro/growth/enterprise), API key CRUD endpoints.",
    },
    {
        "version": "0.5.4",
        "title": "Execution stability, adapter error normalisation, status page",
        "date": "Thu, 12 Mar 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.5.4",
        "description": "Hardened adapter error normalisation, retry budget on transient 5xx, public /status endpoint for uptime monitoring.",
    },
    {
        "version": "0.5.3",
        "title": "Auth hardening, rate-limit headers, API key rotation",
        "date": "Mon, 09 Mar 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.5.3",
        "description": "SHA-256 key hashing at rest, X-RateLimit headers on all responses, one-click API key rotation without downtime.",
    },
    {
        "version": "0.5.2",
        "title": "Search relevance tuning, category filters, pagination",
        "date": "Fri, 06 Mar 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.5.2",
        "description": "Improved WRI-weighted search ranking, category and tag filters on /search, cursor-based pagination.",
    },
    {
        "version": "0.5.1",
        "title": "Bug fixes: execute timeouts, credit deduction edge cases",
        "date": "Wed, 04 Mar 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.5.1",
        "description": "Fixed execute endpoint timeout handling, corrected credit deduction on partial failures, improved 429 response bodies.",
    },
    {
        "version": "0.5.0",
        "title": "Initial public release: search, execute, 270+ services",
        "date": "Mon, 02 Mar 2026 00:00:00 +0000",
        "link": "https://wayforth.io/changelog#v0.5.0",
        "description": "Public launch with semantic search across 270+ verified APIs, managed execution adapters for 11 providers, WayforthRank v1.",
    },
]


@app.get("/changelog.xml", include_in_schema=False)
async def changelog_rss():
    items = ""
    for e in _CHANGELOG_ENTRIES[:10]:
        items += (
            f"    <item>\n"
            f"      <title>v{e['version']} — {e['title']}</title>\n"
            f"      <link>{e['link']}</link>\n"
            f"      <guid>{e['link']}</guid>\n"
            f"      <pubDate>{e['date']}</pubDate>\n"
            f"      <description><![CDATA[{e['description']}]]></description>\n"
            f"    </item>\n"
        )
    rss = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0">\n'
        '  <channel>\n'
        '    <title>Wayforth API Changelog</title>\n'
        '    <link>https://wayforth.io/changelog</link>\n'
        '    <description>Release notes for the Wayforth API</description>\n'
        '    <language>en-us</language>\n'
        f'{items}'
        '  </channel>\n'
        '</rss>\n'
    )
    return Response(content=rss, media_type="application/rss+xml")
