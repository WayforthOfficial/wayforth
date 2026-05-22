"""routers/auth.py — Auth, API key management, and legacy identity routes."""

import asyncio
import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import _resolve_user, verify_supabase_jwt, get_fernet
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier

logger = logging.getLogger("wayforth")

router = APIRouter()

FOUNDING_MEMBER_CUTOFF = datetime(2026, 8, 31, tzinfo=timezone.utc)

# ── Registration guards ───────────────────────────────────────────────────────

_UUID4_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
)

_RESERVED_PREFIXES = {
    'admin', 'founder', 'founders', 'billing', 'legal', 'info', 'contact',
    'hello', 'noreply', 'no-reply', 'no_reply', 'team', 'dev', 'support',
    'security', 'abuse', 'postmaster', 'hostmaster', 'webmaster', 'root',
    'system',
}

_BLOCKED_DOMAINS = {
    'wayforth.io',
    'example.invalid',
    'audit-research.io',
    'example.com',
}


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
    "builder":    {"credits": 6_000,   "price_usd": 12,  "fee_bps": 150, "label": "Builder"},
    "starter":    {"credits": 21_000,  "price_usd": 29,  "fee_bps": 150, "label": "Starter"},
    "pro":        {"credits": 72_000,  "price_usd": 99,  "fee_bps": 150, "label": "Pro"},
    "growth":     {"credits": 240_000, "price_usd": 299, "fee_bps": 150, "label": "Growth"},
    "enterprise": {"credits": -1,      "price_usd": None,"fee_bps": 150, "label": "Enterprise"},
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
    # Atomic check-and-increment (see core/auth.py:check_auth for rationale).
    key = await db.fetchrow("""
        UPDATE api_keys
        SET usage_this_month = usage_this_month + 1, last_used_at = NOW()
        WHERE key_hash = $1
          AND active = TRUE
          AND (monthly_quota = 0 OR usage_this_month < monthly_quota)
        RETURNING id, tier, rate_limit_per_minute, monthly_quota, usage_this_month,
                  quota_reset_at, active
    """, key_hash)

    if not key:
        existing = await db.fetchrow(
            "SELECT active, monthly_quota, quota_reset_at "
            "FROM api_keys WHERE key_hash = $1", key_hash,
        )
        if not existing or not existing["active"]:
            raise HTTPException(status_code=401, detail="Invalid API key")
        reset_str = (
            existing["quota_reset_at"].strftime("%Y-%m-%d")
            if existing["quota_reset_at"] else "next billing cycle"
        )
        raise HTTPException(
            status_code=429,
            detail=f"Monthly quota of {existing['monthly_quota']} requests exceeded. Resets {reset_str}",
        )

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
        raise HTTPException(status_code=429, detail="Unable to create key. Please contact support.")

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
    """Register a new Wayforth account.

    Requires a valid Supabase Bearer JWT in the `Authorization` header — the
    request body's email/supabase_id are read FROM the verified JWT claims, not
    trusted from the body. Before this guard, anyone could POST {email,
    supabase_id} for any email/UUID4 string and squat the account (and receive
    a working `wf_live_*` API key with free credits in the response).
    """
    from notifications import send_welcome_email

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail={
            "error": "supabase_session_required",
            "message": "Provide Authorization: Bearer <supabase_jwt> to register.",
        })
    token = auth_header.removeprefix("Bearer ").strip()
    try:
        claims = verify_supabase_jwt(token)
    except Exception:
        raise HTTPException(status_code=401, detail={"error": "invalid_supabase_token"})

    supabase_id = (claims.get("sub") or "").strip()
    email = (claims.get("email") or "").strip().lower()
    if not supabase_id or not email:
        raise HTTPException(status_code=401, detail={
            "error": "invalid_supabase_token",
            "message": "Token is missing sub/email claims.",
        })

    # Optionally allow the body to specify a fallback email for callers that
    # use phone-based Supabase auth — but the JWT claim still wins when present.
    try:
        body = await request.json()
    except Exception:
        body = {}
    body_email = (body.get("email") or "").strip().lower() if isinstance(body, dict) else ""
    if body_email and body_email != email:
        raise HTTPException(status_code=400, detail={
            "error": "email_mismatch",
            "message": "Body email does not match the Supabase token's email claim.",
        })

    # Guard a: block @wayforth.io (and any other reserved domains)
    domain = email.split('@')[-1].lower() if '@' in email else ''
    if domain in _BLOCKED_DOMAINS:
        raise HTTPException(status_code=403, detail="invalid_email_domain")

    # Guard b: supabase_id must be a valid UUID v4 (sanity check — Supabase issues UUIDv4)
    if not _UUID4_RE.match(supabase_id.lower()):
        raise HTTPException(status_code=400, detail="invalid_supabase_id")

    # Guard c: block reserved local-part prefixes
    local = email.split('@')[0].lower()
    if local in _RESERVED_PREFIXES:
        raise HTTPException(status_code=403, detail="reserved_email")

    existing = await db.fetchrow("SELECT id FROM users WHERE email = $1", email)
    if existing:
        raise HTTPException(status_code=409, detail={"error": "account already exists", "code": 409})

    sub_conflict = await db.fetchrow("SELECT email FROM users WHERE supabase_id = $1", supabase_id)
    if sub_conflict:
        raise HTTPException(status_code=409, detail={
            "error": "supabase_id already linked to another account",
            "code": "supabase_id_conflict",
        })

    is_founding = datetime.now(timezone.utc) < FOUNDING_MEMBER_CUTOFF
    user = await db.fetchrow("""
        INSERT INTO users (email, supabase_id, founding_member)
        VALUES ($1, $2, $3)
        RETURNING id, email, created_at
    """, email, supabase_id, is_founding)

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

    from core.email import send_email, _build_founding_note
    asyncio.create_task(send_email(email, "welcome", {
        "credits": "100",
        "quick_start": "uvx wayforth-mcp",
        "founding_note": _build_founding_note(is_founding),
    }))

    return {
        "user_id": str(user['id']),
        "email": email,
        "api_key": raw_key,
        "tier": "free",
        "message": "Account created. Save your API key — it won't be shown again.",
    }


@router.post("/auth/regenerate-key")
@limiter.limit("3/minute")
async def regenerate_api_key(request: Request, db=Depends(get_db)):
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})

    old_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    row = await db.fetchrow("""
        SELECT id, tier, user_id, owner_email, rate_limit_per_minute, monthly_quota
        FROM api_keys WHERE key_hash = $1 AND active = TRUE
    """, old_hash)

    if not row:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})

    new_raw = "wf_live_" + secrets.token_urlsafe(32)
    new_hash = hashlib.sha256(new_raw.encode()).hexdigest()
    new_prefix = new_raw[:12]
    try:
        encrypted = get_fernet().encrypt(new_raw.encode()).decode()
    except Exception:
        encrypted = None

    await db.execute("""
        UPDATE api_keys
        SET key_hash = $1, key_prefix = $2, encrypted_key = $3, last_used_at = NULL
        WHERE id = $4
    """, new_hash, new_prefix, encrypted, row["id"])

    response = JSONResponse(content={"api_key": new_raw})
    response.headers["Cache-Control"] = "no-store, no-cache"
    return response


@router.get("/auth/me")
@limiter.limit("10/minute")
async def auth_me(request: Request, db=Depends(get_db)):
    """Return email, tier, and credits. API key is NOT returned here — use GET /account/api-key.

    Accepts either:
      - Authorization: Bearer <supabase_jwt>
      - X-Wayforth-API-Key: <api_key>
    """
    # Fast path: API key header
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if raw_key:
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        row = await db.fetchrow("""
            SELECT u.email, k.key_prefix, k.tier,
                   uc.package_tier, uc.credits_balance, uc.lifetime_credits
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            LEFT JOIN user_credits uc ON uc.user_id = k.user_id
            WHERE k.key_hash = $1 AND k.active = true
            ORDER BY (k.encrypted_key IS NOT NULL) DESC, k.created_at DESC
            LIMIT 1
        """, key_hash)
        if not row:
            raise HTTPException(status_code=401, detail="Invalid API key")
        tier = _credits_to_tier(row["lifetime_credits"] or 0, row["package_tier"])
        response = JSONResponse(content={
            "email": row["email"],
            "tier": tier,
            "credits_remaining": row["credits_balance"] or 0,
        })
        response.headers["Cache-Control"] = "no-store, no-cache"
        return response

    # JWT path
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization: Bearer <token> or X-Wayforth-API-Key required",
        )

    token = auth_header.removeprefix("Bearer ").strip()

    try:
        claims = verify_supabase_jwt(token)
        supabase_sub = claims.get("sub", "")
        if not supabase_sub:
            raise ValueError("no sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    row = await db.fetchrow("""
        SELECT u.email, k.key_prefix, k.tier,
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

    tier = _credits_to_tier(row["lifetime_credits"] or 0, row["package_tier"])

    response = JSONResponse(content={
        "email": row["email"],
        "tier": tier,
        "credits_remaining": row["credits_balance"] or 0,
    })
    response.headers["Cache-Control"] = "no-store, no-cache"
    return response


@router.get("/account/api-key")
@limiter.limit("5/minute")
async def get_api_key(request: Request, db=Depends(get_db)):
    """Return the caller's API key — called only on explicit user action (Reveal button).

    Requires Authorization: Bearer <supabase_jwt>.
    Never exposed in the default /auth/me response.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization: Bearer <token> required")

    token = auth_header.removeprefix("Bearer ").strip()
    try:
        claims = verify_supabase_jwt(token)
        supabase_sub = claims.get("sub", "")
        if not supabase_sub:
            raise ValueError("no sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    row = await db.fetchrow("""
        SELECT k.key_prefix, k.encrypted_key, k.created_at, k.last_used_at
        FROM users u
        JOIN api_keys k ON k.user_id = u.id
        WHERE u.supabase_id = $1 AND k.active = true
        ORDER BY (k.encrypted_key IS NOT NULL) DESC, k.created_at DESC
        LIMIT 1
    """, supabase_sub)

    if not row:
        raise HTTPException(status_code=401, detail={"code": "account_not_found"})

    if row["encrypted_key"]:
        try:
            _f = get_fernet()
            api_key = _f.decrypt(row["encrypted_key"].encode()).decode()
        except Exception:
            raise HTTPException(status_code=500, detail="Key decryption failed")
    else:
        api_key = row["key_prefix"] + "..."

    response = JSONResponse(content={
        "api_key": api_key,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
    })
    response.headers["Cache-Control"] = "no-store, no-cache"
    return response
