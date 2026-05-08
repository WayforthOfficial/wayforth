"""routers/auth.py — Auth, API key management, and legacy identity routes."""

import asyncio
import hashlib
import logging
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import _resolve_user, verify_supabase_jwt, get_fernet
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier

logger = logging.getLogger("wayforth")

router = APIRouter()

# ── Constants ─────────────────────────────────────────────────────────────────

TIER_LIMITS = {
    "free":       {"rpm": 10,  "monthly": 1_000,    "fee_bps": 150},
    "builder":    {"rpm": 30,  "monthly": 5_000,    "fee_bps": 150},
    "starter":    {"rpm": 60,  "monthly": 20_000,   "fee_bps": 150},
    "pro":        {"rpm": 120, "monthly": 100_000,  "fee_bps": 150},
    "growth":     {"rpm": 300, "monthly": 500_000,  "fee_bps": 150},
    "enterprise": {"rpm": 500, "monthly": -1,       "fee_bps": 150},
}

PACKAGES = {
    "builder":    {"credits": 6_000,   "price_usd": 12,  "wayf_bonus_pct": 0.05, "fee_bps": 150, "label": "Builder"},
    "starter":    {"credits": 21_000,  "price_usd": 29,  "wayf_bonus_pct": 0.05, "fee_bps": 150, "label": "Starter"},
    "pro":        {"credits": 72_000,  "price_usd": 99,  "wayf_bonus_pct": 0.05, "fee_bps": 150, "label": "Pro"},
    "growth":     {"credits": 240_000, "price_usd": 299, "wayf_bonus_pct": 0.05, "fee_bps": 150, "label": "Growth"},
    "enterprise": {"credits": -1,      "price_usd": None,"wayf_bonus_pct": 0.05, "fee_bps": 150, "label": "Enterprise"},
}

CREDIT_COSTS = {
    "search": 1,
    "query": 2,
    "intelligence": 5,
    "graph": 2,
    "wri_history": 1,
    "payment_routing": 100,  # per $1 routed
}

# ── Models ────────────────────────────────────────────────────────────────────

class ApiKeyRequest(BaseModel):
    email: str
    tier: str = "free"
    admin_key: str = ""  # Required to create non-free keys


class AgentIdentityRequest(BaseModel):
    agent_id: str
    display_name: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _credits_to_tier(lifetime_credits: int, package_tier: str | None) -> str:
    _TIER_FEATURES = {"free", "builder", "starter", "pro", "growth"}
    if package_tier and package_tier in _TIER_FEATURES:
        return package_tier
    if lifetime_credits >= 240_000:
        return "growth"
    if lifetime_credits >= 72_000:
        return "pro"
    if lifetime_credits >= 21_000:
        return "starter"
    if lifetime_credits >= 6_000:
        return "builder"
    return "free"


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


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/keys/tiers", tags=["Keys"])
async def key_tiers():
    return {
        "tiers": [
            {"tier": "free",       "price_monthly_usd": 0,    "rpm": 10,  "monthly_quota": 1000,   "features": ["search", "query", "services"]},
            {"tier": "starter",    "price_monthly_usd": 19,   "rpm": 30,  "monthly_quota": 10000,  "features": ["search", "query", "services", "intelligence", "webhooks"]},
            {"tier": "pro",        "price_monthly_usd": 99,   "rpm": 100, "monthly_quota": 100000, "features": ["search", "query", "services", "intelligence", "webhooks", "history", "graph"]},
            {"tier": "enterprise", "price_monthly_usd": None, "rpm": 500, "monthly_quota": -1,     "features": ["everything", "sla", "private_catalog", "dedicated_infra", "custom_probing"]},
        ],
    }


@router.post("/keys/create")
@limiter.limit("5/minute")
async def create_api_key(request: Request, body: ApiKeyRequest, db=Depends(get_db)):
    from main import ADMIN_KEY
    from notifications import send_welcome_email
    if body.tier != "free" and (not ADMIN_KEY or not secrets.compare_digest(body.admin_key, ADMIN_KEY)):
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


@router.get("/keys/usage")
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


@router.post("/auth/register")
@limiter.limit("5/minute")
async def register_user(request: Request, db=Depends(get_db)):
    from notifications import send_welcome_email
    body = await request.json()
    email = body.get("email")
    supabase_id = body.get("supabase_id")

    if not email or not supabase_id:
        raise HTTPException(status_code=400, detail="email and supabase_id required")

    existing = await db.fetchrow("SELECT id FROM users WHERE email = $1", email)
    if existing:
        raise HTTPException(status_code=409, detail={"error": "account already exists", "code": 409})

    sub_conflict = await db.fetchrow("SELECT email FROM users WHERE supabase_id = $1", supabase_id)
    if sub_conflict:
        raise HTTPException(status_code=409, detail={
            "error": "supabase_id already linked to another account",
            "code": "supabase_id_conflict",
        })

    user = await db.fetchrow("""
        INSERT INTO users (email, supabase_id)
        VALUES ($1, $2)
        RETURNING id, email, created_at
    """, email, supabase_id)

    raw_key = "wf_live_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]
    try:
        _f = get_fernet()
        encrypted_key = _f.encrypt(raw_key.encode()).decode()
    except Exception:
        encrypted_key = None

    await db.execute("""
        INSERT INTO api_keys (key_hash, key_prefix, tier, user_id, owner_email, encrypted_key)
        VALUES ($1, $2, 'free', $3, $4, $5)
        ON CONFLICT DO NOTHING
    """, key_hash, key_prefix, str(user['id']), email, encrypted_key)

    await db.execute("""
        INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier)
        VALUES ($1, 100, 100, 'free')
        ON CONFLICT (user_id) DO NOTHING
    """, user['id'])

    await db.execute("""
        INSERT INTO credit_transactions
        (user_id, amount, balance_after, type, description)
        VALUES ($1, 100, 100, 'bonus', 'Free signup credits')
    """, user['id'])

    asyncio.create_task(asyncio.to_thread(
        send_welcome_email, email, key_prefix, 'free'
    ))

    return {
        "user_id": str(user['id']),
        "email": email,
        "api_key": raw_key,
        "tier": "free",
        "message": "Account created. Save your API key — it won't be shown again.",
    }


@router.get("/auth/me")
@limiter.limit("30/minute")
async def auth_me(request: Request, db=Depends(get_db)):
    """Return the caller's Wayforth API key prefix, email, and tier from a Supabase JWT.

    Authorization: Bearer <supabase_jwt>
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization: Bearer <token> required")

    token = auth_header.removeprefix("Bearer ").strip()

    # RS256 cryptographic verification via Supabase JWKS. Signature, expiry, and audience checked.
    try:
        claims = verify_supabase_jwt(token)
        supabase_sub = claims.get("sub", "")
        if not supabase_sub:
            raise ValueError("no sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    row = await db.fetchrow("""
        SELECT u.email, k.key_prefix, k.encrypted_key, k.tier,
               uc.package_tier, uc.credits_balance, uc.lifetime_credits
        FROM users u
        JOIN api_keys k ON k.user_id = u.id
        LEFT JOIN user_credits uc ON uc.user_id = u.id
        WHERE u.supabase_id = $1
          AND k.active = true
        ORDER BY (k.encrypted_key IS NOT NULL) DESC, k.created_at DESC
        LIMIT 1
    """, supabase_sub)

    if not row:
        raise HTTPException(status_code=401, detail={
            "detail": "No account found. Please register first.",
            "code": "account_not_found",
        })

    if row["encrypted_key"]:
        try:
            _f = get_fernet()
            api_key = _f.decrypt(row["encrypted_key"].encode()).decode()
        except Exception:
            raise HTTPException(
                status_code=500,
                detail="Key decryption failed — please contact support@wayforth.io",
            )
    else:
        api_key = row["key_prefix"] + "..."

    tier = _credits_to_tier(row["lifetime_credits"] or 0, row["package_tier"])

    # api_key is returned here intentionally so the frontend can display it once on login.
    # It is transmitted over HTTPS only. Cache-Control: no-store prevents proxy/browser caching.
    response = JSONResponse(content={
        "email": row["email"],
        "api_key": api_key,
        "tier": tier,
        "credits_remaining": row["credits_balance"] or 0,
    })
    response.headers["Cache-Control"] = "no-store, no-cache"
    return response
