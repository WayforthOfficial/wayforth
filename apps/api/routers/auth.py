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

from core.auth import _resolve_user, verify_supabase_jwt, get_fernet, resolve_dashboard_caller
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


# ── Browser session proxy (wf_session cookie) ────────────────────────────────
#
# The dashboard exchanges its Supabase JWT for an opaque server-issued session
# token, delivered ONLY as an HttpOnly/Secure/SameSite=Strict cookie. JS cannot
# read HttpOnly cookies, so XSS on any wayforth.io subdomain can no longer
# exfiltrate the dashboard session. API key flows (X-Wayforth-API-Key) and
# non-browser Bearer JWT flows are unaffected.

def _redis_for_session():
    """Resolve the same Redis client the rate limiter / lockout use.
    Returns None if Redis is unreachable / unconfigured — caller must 503."""
    from core.tier_gates import _get_redis
    return _get_redis()


# Accepted body keys for the JWT, in priority order. Different Supabase
# client SDKs name the field differently — the JS client returns
# `access_token`, our docs reference `supabase_jwt`, hand-rolled callers
# tend to pick `token` or `jwt`. Accepting all four eliminates a class of
# integration bugs where the frontend uses one name and the backend
# rejects it.
_SESSION_JWT_FIELDS = ("supabase_jwt", "token", "access_token", "jwt")


@router.post("/auth/session")
@limiter.limit("10/minute")
async def auth_session_create(request: Request, db=Depends(get_db)):
    """Exchange a Supabase JWT for an HttpOnly session cookie.

    Body: JSON containing the JWT under any of these keys (priority order):
      - supabase_jwt   (canonical)
      - token          (generic)
      - access_token   (Supabase JS client default)
      - jwt            (generic)
    Response: {"ok": true} — the cookie is the only out-of-band credential.
    The raw token is never echoed in the response body, so a page-rendered
    response leak (logging frameworks, error reporters) cannot expose it.

    Validates the JWT with the same JWKS-backed verifier the rest of the
    codebase uses, then looks up the local user row by `supabase_id`. The
    session record stores user_id / email / tier / supabase_id so subsequent
    middleware-resolved auth has everything it needs without a DB hit.
    """
    redis = _redis_for_session()
    if redis is None:
        raise HTTPException(status_code=503, detail={
            "error": "session_unavailable",
            "message": "Session store is not reachable. Try again shortly.",
        })

    try:
        body = await request.json()
    except Exception:
        body = {}
    supabase_jwt = ""
    if isinstance(body, dict):
        for _field in _SESSION_JWT_FIELDS:
            _val = body.get(_field)
            if isinstance(_val, str) and _val.strip():
                supabase_jwt = _val.strip()
                break
    if not supabase_jwt:
        # Diagnostic log: keys only, never values. Helps debug frontend
        # integrations that send the JWT under an unexpected key name.
        received_keys = sorted(body.keys()) if isinstance(body, dict) else []
        logger.warning(
            "POST /auth/session 400 no_jwt_field received_keys=%s expected_any_of=%s",
            received_keys, list(_SESSION_JWT_FIELDS),
        )
        raise HTTPException(status_code=400, detail={
            "error": "supabase_jwt_required",
            "message": (
                "POST a JSON body with the JWT under one of: "
                f"{', '.join(_SESSION_JWT_FIELDS)}."
            ),
            "accepted_fields": list(_SESSION_JWT_FIELDS),
            "received_keys": received_keys,
        })

    try:
        claims = verify_supabase_jwt(supabase_jwt)
    except Exception:
        raise HTTPException(status_code=401, detail={"error": "invalid_supabase_jwt"})

    supabase_sub = (claims.get("sub") or "").strip()
    claim_email = (claims.get("email") or "").strip().lower()
    if not supabase_sub:
        raise HTTPException(status_code=401, detail={"error": "invalid_supabase_jwt"})

    row = await db.fetchrow("""
        SELECT u.id, u.email, u.supabase_id, k.tier,
               uc.package_tier, uc.lifetime_credits
        FROM users u
        LEFT JOIN api_keys k ON k.user_id = u.id AND k.active = true
        LEFT JOIN user_credits uc ON uc.user_id = u.id
        WHERE u.supabase_id = $1
        ORDER BY (k.encrypted_key IS NOT NULL) DESC NULLS LAST, k.created_at DESC NULLS LAST
        LIMIT 1
    """, supabase_sub)

    # Fallback: OAuth providers (e.g. Google) issue a different Supabase UUID
    # than the one stored from an earlier email/password signup. If the
    # supabase_id lookup missed, try matching by the verified email claim.
    # On success, update supabase_id to the new sub so future logins hit the
    # fast path and don't rely on this fallback again.
    email_fallback_used = False
    if not row and claim_email:
        row = await db.fetchrow("""
            SELECT u.id, u.email, u.supabase_id, k.tier,
                   uc.package_tier, uc.lifetime_credits
            FROM users u
            LEFT JOIN api_keys k ON k.user_id = u.id AND k.active = true
            LEFT JOIN user_credits uc ON uc.user_id = u.id
            WHERE lower(u.email) = $1
            ORDER BY (k.encrypted_key IS NOT NULL) DESC NULLS LAST, k.created_at DESC NULLS LAST
            LIMIT 1
        """, claim_email)
        if row:
            email_fallback_used = True

    if not row:
        raise HTTPException(status_code=401, detail={
            "error": "account_not_found",
            "message": "No Wayforth account is linked to this Supabase identity. Register first.",
        })
    # Belt-and-braces: confirm the JWT email matches the row email so a JWT
    # whose `email` claim was tampered with (and somehow passed signature)
    # cannot ride someone else's account.
    if claim_email and row["email"].lower() != claim_email:
        raise HTTPException(status_code=401, detail={"error": "email_mismatch"})

    if email_fallback_used:
        logger.info(
            "POST /auth/session identity-link sub=%s email=%s old_supabase_id=%s",
            supabase_sub, claim_email, row["supabase_id"],
        )
        await db.execute(
            "UPDATE users SET supabase_id = $1 WHERE id = $2",
            supabase_sub, row["id"],
        )

    tier = _credits_to_tier(row["lifetime_credits"] or 0, row["package_tier"])

    from core.session import create_session, set_session_cookie
    raw_token = await create_session(
        redis=redis,
        user_id=str(row["id"]),
        email=row["email"],
        tier=tier,
        supabase_id=supabase_sub,
    )

    response = JSONResponse(content={"ok": True})
    response.headers["Cache-Control"] = "no-store, no-cache"
    set_session_cookie(response, raw_token)
    return response


@router.post("/auth/session/refresh")
@limiter.limit("60/minute")
async def auth_session_refresh(request: Request):
    """Extend the session's Redis TTL and re-issue the cookie's Max-Age.

    Called by the dashboard periodically while the user is active. The cookie
    value does NOT rotate — only the TTL extends. See core/session.py for
    the rationale (token rotation on refresh does not meaningfully shrink the
    exposure window of a stolen cookie while it does multiply token-handling
    surface).
    """
    from core.session import (
        get_request_session, get_request_session_token,
        refresh_session, set_session_cookie,
    )
    record = get_request_session(request)
    raw_token = get_request_session_token(request)
    if not record or not raw_token:
        raise HTTPException(status_code=401, detail={"error": "session_not_found"})

    redis = _redis_for_session()
    if redis is None:
        raise HTTPException(status_code=503, detail={"error": "session_unavailable"})

    refreshed = await refresh_session(redis, raw_token)
    if not refreshed:
        # Expired between middleware lookup and now — extremely rare but treat
        # as a fresh-login required.
        raise HTTPException(status_code=401, detail={"error": "session_expired"})

    response = JSONResponse(content={"ok": True})
    response.headers["Cache-Control"] = "no-store, no-cache"
    set_session_cookie(response, raw_token)
    return response


@router.post("/auth/session/logout")
@limiter.limit("30/minute")
async def auth_session_logout(request: Request):
    """Server-side revocation + cookie expiry.

    Idempotent: callable without a valid cookie (browser clears its cookie
    regardless). Always returns 200 so logout flows don't surface noisy 4xxs
    on already-cleared sessions.
    """
    from core.session import (
        get_request_session_token, revoke_session, clear_session_cookie,
    )
    raw_token = get_request_session_token(request)
    if raw_token:
        redis = _redis_for_session()
        if redis is not None:
            await revoke_session(redis, raw_token)

    response = JSONResponse(content={"ok": True})
    response.headers["Cache-Control"] = "no-store, no-cache"
    clear_session_cookie(response)
    return response


@router.get("/auth/me")
@limiter.limit("10/minute")
async def auth_me(request: Request, db=Depends(get_db)):
    """Return email, tier, and credits. API key is NOT returned here — use GET /account/api-key.

    Accepts (in priority order):
      - wf_session HttpOnly cookie (browser dashboard, preferred)
      - Authorization: Bearer <supabase_jwt> (legacy / non-browser)
      - X-Wayforth-API-Key: <api_key> (programmatic clients)
    """
    # Fastest path: validated session cookie (middleware already looked it up).
    from core.session import get_request_session
    session = get_request_session(request)
    if session:
        row = await db.fetchrow("""
            SELECT u.email, k.tier,
                   uc.package_tier, uc.credits_balance, uc.lifetime_credits
            FROM users u
            LEFT JOIN api_keys k ON k.user_id = u.id AND k.active = true
            LEFT JOIN user_credits uc ON uc.user_id = u.id
            WHERE u.id = $1::uuid
            ORDER BY (k.encrypted_key IS NOT NULL) DESC NULLS LAST, k.created_at DESC NULLS LAST
            LIMIT 1
        """, session["user_id"])
        if row:
            tier = _credits_to_tier(row["lifetime_credits"] or 0, row["package_tier"])
            response = JSONResponse(content={
                "email": row["email"],
                "tier": tier,
                "credits_remaining": row["credits_balance"] or 0,
            })
            response.headers["Cache-Control"] = "no-store, no-cache"
            return response
        # Session is valid but the underlying account is gone — treat as logout.
        raise HTTPException(status_code=401, detail={"error": "account_not_found"})

    # API key header path
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
    """Return the caller's API key — called by the dashboard on session setup
    (and on explicit "Reveal key" actions).

    Accepts:
      - wf_session HttpOnly cookie (browser, preferred)
      - Authorization: Bearer <supabase_jwt> (legacy / non-browser)
      - X-Wayforth-API-Key (programmatic — degenerate but harmless)

    Returns 200 in three cases the dashboard needs to distinguish:
      - Account has an active key, decryptable           → {"api_key": "<key>", ...}
      - Account has an active key but it predates the    → {"api_key": "<prefix>...",
        encrypted-storage migration (key_prefix only)       "encrypted": false, ...}
      - Account has NO active key (new user, or all      → {"api_key": null, ...}
        keys revoked)
    Returns 401 only when authentication itself fails. Previously this
    endpoint conflated "no active key" with "account not found" and 401'd,
    which the dashboard treated as a fatal login failure for new users.
    """
    caller = await resolve_dashboard_caller(request, db)

    if caller["api_key_id"] is None:
        # Authenticated, but the account has no active API key. The dashboard
        # should surface a "generate key" action; this is not a login failure.
        response = JSONResponse(content={
            "api_key": None,
            "created_at": None,
            "last_used_at": None,
            "message": "No active API key for this account. Generate one to get started.",
        })
        response.headers["Cache-Control"] = "no-store, no-cache"
        return response

    row = await db.fetchrow("""
        SELECT key_prefix, encrypted_key, created_at, last_used_at
        FROM api_keys
        WHERE id = $1::uuid AND active = true
    """, str(caller["api_key_id"]))

    if not row:
        # Race: key was deactivated between resolve_dashboard_caller and now.
        # Treat the same as "no active key" so the dashboard recovers
        # gracefully rather than blowing up the login flow.
        response = JSONResponse(content={
            "api_key": None,
            "created_at": None,
            "last_used_at": None,
            "message": "Active API key was revoked. Generate a new one to continue.",
        })
        response.headers["Cache-Control"] = "no-store, no-cache"
        return response

    encrypted: bool
    if row["encrypted_key"]:
        try:
            _f = get_fernet()
            api_key = _f.decrypt(row["encrypted_key"].encode()).decode()
            encrypted = True
        except Exception:
            raise HTTPException(status_code=500, detail="Key decryption failed")
    else:
        # Legacy row: only the key_prefix was retained; the raw key was issued
        # once at signup and we never stored a decryptable copy. We surface a
        # masked preview rather than a full key.
        api_key = row["key_prefix"] + "..."
        encrypted = False

    response = JSONResponse(content={
        "api_key": api_key,
        "encrypted": encrypted,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_used_at": row["last_used_at"].isoformat() if row["last_used_at"] else None,
    })
    response.headers["Cache-Control"] = "no-store, no-cache"
    return response
