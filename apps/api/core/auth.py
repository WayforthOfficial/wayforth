import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timezone

import httpx
import jwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("wayforth")


def get_fernet():
    from cryptography.fernet import Fernet
    raw = os.environ.get("ENCRYPTION_KEY", "")
    if not raw:
        raise ValueError("ENCRYPTION_KEY not set")
    try:
        return Fernet(raw.encode())
    except Exception:
        raise ValueError(
            "ENCRYPTION_KEY is not a valid Fernet key. "
            "Generate one with: python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )


_JWKS_URL = "https://oafqjvdvamcygiqbnoby.supabase.co/auth/v1/.well-known/jwks.json"
_jwks_cache: dict = {"keys": [], "fetched_at": 0}


async def get_jwks() -> list:
    """Fetch Supabase JWKS, cached for 1 hour.

    S10 (v0.7.8): switched from sync requests.get to httpx.AsyncClient so a
    cache miss doesn't block the event loop for the duration of the network
    round-trip. Callers must `await get_jwks()`.
    """
    import time
    if time.time() - _jwks_cache["fetched_at"] > 3600:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(_JWKS_URL)
            resp.raise_for_status()
            _jwks_cache["keys"] = resp.json()["keys"]
            _jwks_cache["fetched_at"] = time.time()
    return _jwks_cache["keys"]


async def verify_supabase_jwt(token: str) -> dict:
    """Asymmetric verification via Supabase JWKS. Supports RS256 and ES256.
    Checks signature, expiry, and audience.

    S10 (v0.7.8): async to let JWKS refresh happen without blocking.
    """
    from jwt.algorithms import RSAAlgorithm, ECAlgorithm
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    keys = await get_jwks()
    key = next((k for k in keys if k["kid"] == kid), None)
    if not key:
        raise ValueError("No matching JWKS key found")
    alg = key.get("alg", header.get("alg", "RS256"))
    if alg.startswith("ES"):
        public_key = ECAlgorithm.from_jwk(key)
    else:
        public_key = RSAAlgorithm.from_jwk(key)
    return jwt.decode(
        token,
        public_key,
        algorithms=[alg],
        audience="authenticated",
    )


class _AuthError(Exception):
    def __init__(self, status_code: int, content: dict):
        self.status_code = status_code
        self.content = content


async def _auth_error_handler(request: Request, exc: _AuthError):
    return JSONResponse(status_code=exc.status_code, content=exc.content)


_ANON_DAILY_LIMIT = 3
_TIER_RPM = {"free": 30, "builder": 120, "starter": 300, "pro": 600, "growth": 0, "enterprise": 500}


async def check_auth(request: Request) -> dict:
    """Unified auth dependency for /search and /query.

    Authenticated (X-Wayforth-API-Key present):
      - Validates key, checks monthly quota, increments usage.
      - Returns authenticated=True with tier/key_id.
      - The route handler is expected to additionally call
        `core.tier_gates.check_rate_limit(key_id, tier)` for the
        per-tier sliding-window per-minute / per-hour limits.

    Anonymous (no key):
      - Enforces a strict 3 searches/IP/day cap here.
      - Returns authenticated=False with anonymous_count.
      - Route handlers may additionally call
        `core.tier_gates.check_anon_rate_limit(ip)` for a per-minute
        cap (`_ANON_RPM`, currently 15). In practice the 3/day wall
        fires long before 15/min on /search, so the per-minute limit
        only matters as a defense-in-depth layer for code paths that
        bypass the daily wall.
      - `/query` (WayforthQL) is starter-tier-only via `require_tier`,
        so anonymous callers get 403 before any rate-limit check on
        that route — no anon rate limit is needed there.
    """
    from core.rate_limit import get_real_ip
    from core.credits import _downgrade_expired_usdc
    ip = get_real_ip(request)
    raw_key = request.headers.get("X-Wayforth-API-Key", "")

    if raw_key:
        pool = request.app.state.pool
        if not pool:
            raise HTTPException(status_code=503, detail="Database unavailable")
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        async with pool.acquire() as db:
            # Atomic check-and-increment: previously this was SELECT, Python-side
            # quota check, then a separate UPDATE in a second connection. Under
            # concurrent burst, N callers all read usage_this_month=quota-1, all
            # passed the check, then all incremented — letting a caller exceed
            # their monthly quota by N. The conditional UPDATE below returns
            # zero rows when the limit would be crossed, which we map to 429.
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
            # Either key is invalid/inactive or quota would be exceeded. Probe to
            # distinguish so we return the correct status code without ever
            # producing a stale "ok" result for a quota-exhausted key.
            async with pool.acquire() as db:
                existing = await db.fetchrow(
                    "SELECT active, monthly_quota, usage_this_month "
                    "FROM api_keys WHERE key_hash = $1", key_hash,
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

        # Graceful USDC subscription expiry — downgrade at start of next request, never mid-call
        if (key.get("payment_rail") == "usdc" and key.get("subscription_expires_at")
                and key["subscription_expires_at"] < datetime.now(timezone.utc)):
            asyncio.create_task(_downgrade_expired_usdc(str(key["id"])))

        rpm = _TIER_RPM.get(key["tier"], 10)
        tier = key["tier"] or "free"
        # RETURNING clause above gave us the POST-increment value already.
        usage = key["usage_this_month"]
        from core.credits import PLANS
        calls_included = PLANS.get(tier, {}).get("calls_included", 100)
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
    request.state.rate_limit_rpm = 30
    request.state.ratelimit_remaining = max(0, _ANON_DAILY_LIMIT - (count + 1))
    from datetime import timedelta as _td
    _anon_now = datetime.now(timezone.utc)
    request.state.ratelimit_reset = int(
        _anon_now.replace(hour=23, minute=59, second=59, microsecond=0).timestamp()
    )
    return {
        "authenticated": False,
        "tier": None,
        "key_id": None,
        "anonymous_count": count + 1,
        "ip": ip,
    }


async def _resolve_user(db, api_key: str):
    """Return (user_id, api_key_id, tier) for a valid active API key, or raise 401."""
    # S14 (v0.7.8): tighten to the two exact mint lengths (51 or 56). Keep the
    # set in sync with check_auth in main.py.
    if not api_key.startswith("wf_live_") or len(api_key) not in (51, 56):
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})
    key_record = await db.fetchrow(
        "SELECT id, user_id, tier FROM api_keys WHERE key_hash=$1 AND active=true",
        hashlib.sha256(api_key.encode()).hexdigest(),
    )
    if not key_record:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})
    return key_record["user_id"], key_record["id"], key_record["tier"] or "free"


async def resolve_dashboard_caller(request: Request, db) -> dict:
    """Authenticate a dashboard /account/* caller.

    Tries, in priority order:
      1. wf_session cookie — preferred for browsers (set by /auth/session,
         validated by SessionCookieMiddleware, available on request.scope).
      2. Authorization: Bearer <supabase_jwt> — for non-browser callers that
         still hold a Supabase JWT (legacy CLIs, the dashboard during the
         cookie rollout, server-to-server).
      3. X-Wayforth-API-Key header — programmatic clients.

    Returns a uniform dict regardless of which path authenticated:
        user_id                str (uuid)
        api_key_id             uuid | None   — None only if the account has
                                               no active API key (rare; new
                                               accounts always get one)
        tier                   str           — from the active api_key row,
                                               or "free" if no active key
        monthly_calls_count    int
        monthly_calls_reset_at datetime | None
        email                  str

    Why a single helper instead of per-endpoint duplication:
      Before the v0.7.0 session-cookie work, every /account/* endpoint did
      its own `X-Wayforth-API-Key` check. After we moved the dashboard onto
      the cookie session, /auth/me stopped returning the API key, which
      meant the dashboard had nothing to send to /account/* — and every one
      of those endpoints 401'd, silently falling back to "Free" tier in the
      UI. This helper closes that gap once for every endpoint that consumes
      it, without altering existing API-key behaviour.
    """
    from core.session import get_request_session

    # ── 1. wf_session cookie (browser dashboard) ────────────────────────────
    sess = get_request_session(request)
    if sess:
        return await _load_dashboard_user(db, sess["user_id"])

    # ── 2. Authorization: Bearer <supabase_jwt> ─────────────────────────────
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ").strip()
        try:
            claims = await verify_supabase_jwt(token)
            sub = (claims.get("sub") or "").strip()
            if not sub:
                raise ValueError("no sub")
        except Exception:
            raise HTTPException(status_code=401, detail="Invalid token")
        user_id = await db.fetchval("SELECT id FROM users WHERE supabase_id = $1", sub)
        if not user_id:
            raise HTTPException(status_code=401, detail={"error": "account_not_found"})
        return await _load_dashboard_user(db, str(user_id))

    # ── 3. X-Wayforth-API-Key header (programmatic) ─────────────────────────
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if raw_key:
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        row = await db.fetchrow("""
            SELECT k.id AS api_key_id, k.user_id, k.tier,
                   k.monthly_calls_count, k.monthly_calls_reset_at, u.email
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            WHERE k.key_hash = $1 AND k.active = TRUE
        """, key_hash)
        if not row:
            raise HTTPException(status_code=401, detail="Invalid API key")
        return {
            "user_id": str(row["user_id"]),
            "api_key_id": row["api_key_id"],
            "tier": row["tier"] or "free",
            "monthly_calls_count": row["monthly_calls_count"] or 0,
            "monthly_calls_reset_at": row["monthly_calls_reset_at"],
            "email": row["email"],
        }

    raise HTTPException(
        status_code=401,
        detail="Provide X-Wayforth-API-Key, Authorization: Bearer <supabase_jwt>, or wf_session cookie.",
    )


async def _load_dashboard_user(db, user_id: str) -> dict:
    """Look up the standard dashboard caller shape for a user_id resolved via
    cookie or JWT. Picks the user's most-recent active api_key for the api_key
    fields; if no active key exists, returns a safe "free" shape so the
    dashboard can still render without 500'ing."""
    row = await db.fetchrow("""
        SELECT k.id AS api_key_id, k.tier,
               k.monthly_calls_count, k.monthly_calls_reset_at, u.email
        FROM users u
        LEFT JOIN api_keys k ON k.user_id = u.id AND k.active = TRUE
        WHERE u.id = $1::uuid
        ORDER BY (k.encrypted_key IS NOT NULL) DESC NULLS LAST, k.created_at DESC NULLS LAST
        LIMIT 1
    """, user_id)
    if not row:
        raise HTTPException(status_code=401, detail={"error": "account_not_found"})
    return {
        "user_id": str(user_id),
        "api_key_id": row["api_key_id"],
        "tier": row["tier"] or "free",
        "monthly_calls_count": row["monthly_calls_count"] or 0,
        "monthly_calls_reset_at": row["monthly_calls_reset_at"],
        "email": row["email"],
    }


_AGENT_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')


def _validate_agent_id(agent_id) -> str | None:
    """Validate and normalise agent_id. Returns cleaned string or raises 422."""
    if not agent_id:
        return None
    agent_id = str(agent_id).strip()
    if not _AGENT_ID_RE.match(agent_id):
        raise HTTPException(status_code=422, detail={
            "error": "invalid_agent_id",
            "message": "agent_id must be 1-64 chars, alphanumeric, hyphens and underscores only.",
        })
    return agent_id
