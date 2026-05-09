import asyncio
import hashlib
import logging
import os
import re
from datetime import datetime, timezone

import jwt
import requests
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


def get_jwks() -> list:
    import time
    if time.time() - _jwks_cache["fetched_at"] > 3600:
        resp = requests.get(_JWKS_URL, timeout=5)
        resp.raise_for_status()
        _jwks_cache["keys"] = resp.json()["keys"]
        _jwks_cache["fetched_at"] = time.time()
    return _jwks_cache["keys"]


def verify_supabase_jwt(token: str) -> dict:
    """Asymmetric verification via Supabase JWKS. Supports RS256 and ES256.
    Checks signature, expiry, and audience."""
    from jwt.algorithms import RSAAlgorithm, ECAlgorithm
    header = jwt.get_unverified_header(token)
    kid = header.get("kid")
    keys = get_jwks()
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

    Anonymous (no key):
      - Enforces 3 searches/IP/day via in-memory dict.
      - Returns authenticated=False with anonymous_count.
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

        # Graceful USDC subscription expiry — downgrade at start of next request, never mid-call
        if (key.get("payment_rail") == "usdc" and key.get("subscription_expires_at")
                and key["subscription_expires_at"] < datetime.now(timezone.utc)):
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
    request.state.rate_limit_rpm = 30
    return {
        "authenticated": False,
        "tier": None,
        "key_id": None,
        "anonymous_count": count + 1,
        "ip": ip,
    }


async def _resolve_user(db, api_key: str):
    """Return (user_id, api_key_id, tier) for a valid active API key, or raise 401."""
    key_record = await db.fetchrow(
        "SELECT id, user_id, tier FROM api_keys WHERE key_hash=$1 AND active=true",
        hashlib.sha256(api_key.encode()).hexdigest(),
    )
    if not key_record:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})
    return key_record["user_id"], key_record["id"], key_record["tier"] or "free"


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
