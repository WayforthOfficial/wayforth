"""routers/auth.py — Auth, API key management, and legacy identity routes."""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import (
    _resolve_user,
    decrypt_api_key,
    encrypt_api_key,
    get_fernet,
    resolve_dashboard_caller,
    verify_supabase_jwt,
)
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier

logger = logging.getLogger("wayforth")

router = APIRouter()

# S21 (v0.7.8): cutoff is env-configurable so we can extend or shorten the
# founding-member window without a redeploy. Default keeps existing behavior.
# Format: ISO 8601 with timezone, e.g. "2026-08-31T00:00:00+00:00".
import os as _os_for_cutoff


def _parse_cutoff() -> datetime:
    raw = _os_for_cutoff.environ.get("FOUNDING_MEMBER_CUTOFF", "")
    if raw:
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            logger.warning(
                "Invalid FOUNDING_MEMBER_CUTOFF=%r (expected ISO 8601); using default 2026-08-31",
                raw,
            )
    return datetime(2026, 8, 31, tzinfo=timezone.utc)


FOUNDING_MEMBER_CUTOFF = _parse_cutoff()

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
    "starter":    {"rpm": 30,  "monthly": 5_000,    "fee_bps": 150},
    "builder":    {"rpm": 60,  "monthly": 20_000,   "fee_bps": 150},
    "pro":        {"rpm": 120, "monthly": 100_000,  "fee_bps": 150},
    "growth":     {"rpm": 300, "monthly": 500_000,  "fee_bps": 150},
    "enterprise": {"rpm": 500, "monthly": -1,       "fee_bps": 150},
}

PACKAGES = {
    "starter":    {"credits": 6_000,   "price_usd": 12,  "fee_bps": 150, "label": "Starter"},
    "builder":    {"credits": 21_000,  "price_usd": 29,  "fee_bps": 150, "label": "Builder"},
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
        return "builder"
    if lifetime_credits >= 6_000:
        return "starter"
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
            logger.info(
                "auth_failure reason=invalid_api_key path=/auth/quota key_hash_prefix=%s",
                key_hash[:12],
            )
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
    # Session-OR-key (PR #25 pattern): the dashboard usage widget authenticates by
    # wf_session cookie. Resolve the caller, then read their primary active key row.
    caller = await resolve_dashboard_caller(request, db)
    if not caller.get("api_key_id"):
        raise HTTPException(status_code=404, detail="no_active_api_key")
    key = await db.fetchrow("""
        SELECT key_prefix, tier, rate_limit_per_minute, monthly_quota,
               usage_this_month, quota_reset_at, created_at, last_used_at
        FROM api_keys WHERE id = $1 AND active = TRUE
    """, caller["api_key_id"])
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
        claims = await verify_supabase_jwt(token)
    except Exception as e:
        logger.info("auth_failure reason=invalid_supabase_jwt path=/auth/register err=%s", type(e).__name__)
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

    # FINDING-011: dedup on the canonical email so alias variants
    # (user+1@gmail.com, u.s.e.r@gmail.com) cannot farm multiple free accounts.
    from core.auth import canonicalize_email
    email_canon = canonicalize_email(email)
    existing = await db.fetchrow(
        "SELECT id FROM users WHERE email = $1 OR email_canonical = $2",
        email, email_canon,
    )
    if existing:
        raise HTTPException(status_code=409, detail={"error": "account already exists", "code": 409})

    sub_conflict = await db.fetchrow("SELECT email FROM users WHERE supabase_id = $1", supabase_id)
    if sub_conflict:
        raise HTTPException(status_code=409, detail={
            "error": "supabase_id already linked to another account",
            "code": "supabase_id_conflict",
        })

    is_founding = datetime.now(timezone.utc) < FOUNDING_MEMBER_CUTOFF
    try:
        user = await db.fetchrow("""
            INSERT INTO users (email, supabase_id, founding_member, email_canonical)
            VALUES ($1, $2, $3, $4)
            RETURNING id, email, created_at
        """, email, supabase_id, is_founding, email_canon)
    except Exception as _ie:
        # FINDING-107: a concurrent registration can win the race against the
        # check-then-insert above. The UNIQUE constraints on email /
        # email_canonical / supabase_id (migration 057) now make that fail
        # closed — surface a clean 409 instead of a 500 rather than ever
        # creating a duplicate canonical identity.
        if _ie.__class__.__name__ == "UniqueViolationError":
            raise HTTPException(status_code=409, detail={"error": "account already exists", "code": 409})
        raise

    raw_key = "wf_live_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]
    # v0.8.0 Item 3: store key_version alongside ciphertext for rotation.
    encrypted_key = None
    key_version = 1
    try:
        encrypted_key, key_version = encrypt_api_key(raw_key, version=1)
    except Exception as e:
        logger.warning("api_key encrypt-at-rest failed (key stored unencrypted): %s", type(e).__name__)

    await db.execute("""
        INSERT INTO api_keys (key_hash, key_prefix, tier, user_id, owner_email, encrypted_key, key_version)
        VALUES ($1, $2, 'free', $3, $4, $5, $6)
        ON CONFLICT DO NOTHING
    """, key_hash, key_prefix, str(user['id']), email, encrypted_key, key_version)

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
    # Session-OR-key (PR #25 pattern): the dashboard "regenerate key" button
    # authenticates by wf_session cookie and rotates the user's primary active key.
    caller = await resolve_dashboard_caller(request, db)
    if not caller.get("api_key_id"):
        raise HTTPException(status_code=404, detail={"error": "no_active_api_key"})
    row = await db.fetchrow("""
        SELECT id, tier, user_id, owner_email, rate_limit_per_minute, monthly_quota
        FROM api_keys WHERE id = $1 AND active = TRUE
    """, caller["api_key_id"])

    if not row:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})

    new_raw = "wf_live_" + secrets.token_urlsafe(32)
    new_hash = hashlib.sha256(new_raw.encode()).hexdigest()
    new_prefix = new_raw[:12]
    # v0.8.0 Item 3: re-encrypt with current default version (1) on rotate.
    encrypted = None
    new_key_version = 1
    try:
        encrypted, new_key_version = encrypt_api_key(new_raw, version=1)
    except Exception as e:
        logger.warning("api_key regenerate encrypt failed (key stored unencrypted): %s", type(e).__name__)

    await db.execute("""
        UPDATE api_keys
        SET key_hash = $1, key_prefix = $2, encrypted_key = $3,
            key_version = $4, last_used_at = NULL
        WHERE id = $5
    """, new_hash, new_prefix, encrypted, new_key_version, row["id"])

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


_ME_CACHE_TTL = 30  # seconds — short enough that tier/credit changes propagate quickly


def _me_cache_key(raw_token: str) -> str:
    return "me:" + hashlib.sha256(raw_token.encode()).hexdigest()


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
        claims = await verify_supabase_jwt(supabase_jwt)
    except Exception as e:
        logger.info("auth_failure reason=invalid_supabase_jwt path=/auth/session err=%s", type(e).__name__)
        raise HTTPException(status_code=401, detail={"error": "invalid_supabase_jwt"})

    supabase_sub = (claims.get("sub") or "").strip()
    claim_email = (claims.get("email") or "").strip().lower()
    if not supabase_sub:
        raise HTTPException(status_code=401, detail={"error": "invalid_supabase_jwt"})

    row = await db.fetchrow("""
        SELECT u.id, u.email, u.supabase_id, u.deletion_scheduled_at, k.tier,
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
    # FINDING-107: match the fallback on the CANONICAL email so an OAuth identity
    # for user@gmail.com correctly links to an existing u.s.e.r+tag@gmail.com
    # account (same human) instead of missing and minting a duplicate.
    from core.auth import canonicalize_email
    claim_email_canon = canonicalize_email(claim_email) if claim_email else ""
    email_fallback_used = False
    if not row and claim_email_canon:
        row = await db.fetchrow("""
            SELECT u.id, u.email, u.supabase_id, u.deletion_scheduled_at, k.tier,
                   uc.package_tier, uc.lifetime_credits
            FROM users u
            LEFT JOIN api_keys k ON k.user_id = u.id AND k.active = true
            LEFT JOIN user_credits uc ON uc.user_id = u.id
            WHERE u.email_canonical = $1
            ORDER BY (k.encrypted_key IS NOT NULL) DESC NULLS LAST, k.created_at DESC NULLS LAST
            LIMIT 1
        """, claim_email_canon)
        if row:
            email_fallback_used = True

    if not row:
        raise HTTPException(status_code=401, detail={
            "error": "account_not_found",
            "message": "No Wayforth account is linked to this Supabase identity. Register first.",
        })
    # Belt-and-braces: confirm the JWT email matches the row email so a JWT
    # whose `email` claim was tampered with (and somehow passed signature)
    # cannot ride someone else's account. Compared on the canonical form so a
    # dotted/aliased-but-equivalent Gmail address is not falsely rejected.
    if claim_email_canon and canonicalize_email(row["email"]) != claim_email_canon:
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

    # FINDING-106: reaching this endpoint requires a freshly-verified Supabase
    # JWT for this account — i.e. a deliberate, authenticated re-login by the
    # owner. If the account is inside the deletion grace window, treat that as
    # an explicit intent to recover: cancel the pending deletion and reactivate
    # the API keys that delete_account deactivated. This both unblocks the
    # account and closes the silent-purge footgun (a re-login that didn't clear
    # deletion_scheduled_at would otherwise still be reaped at T+24h).
    account_recovered = False
    if row["deletion_scheduled_at"] is not None:
        async with db.transaction():
            await db.execute(
                "UPDATE users SET deletion_scheduled_at = NULL WHERE id = $1::uuid",
                row["id"],
            )
            await db.execute(
                "UPDATE api_keys SET active = TRUE WHERE user_id = $1::uuid",
                row["id"],
            )
        account_recovered = True
        logger.info("account_deletion_cancelled_via_relogin user=%s", row["id"])

    tier = _credits_to_tier(row["lifetime_credits"] or 0, row["package_tier"])

    from core.session import create_session, set_session_cookie
    raw_token = await create_session(
        redis=redis,
        user_id=str(row["id"]),
        email=row["email"],
        tier=tier,
        supabase_id=supabase_sub,
    )

    _content = {"ok": True}
    if account_recovered:
        _content["account_recovered"] = True
        _content["message"] = (
            "Your account was scheduled for deletion. Logging in has cancelled "
            "the deletion — your account and API keys are active again."
        )
    response = JSONResponse(content=_content)
    response.headers["Cache-Control"] = "no-store, no-cache"
    set_session_cookie(response, raw_token)
    return response


@router.post("/auth/session/refresh")
@limiter.limit("60/minute")
async def auth_session_refresh(request: Request):
    """Extend the session's Redis TTL and rotate the cookie token.

    S17 (v0.7.8): on every refresh the token rotates. A stolen cookie loses
    validity at the next refresh tick rather than persisting for the full
    TTL. The dashboard polls /auth/session/refresh while the user is active
    so a stolen cookie has a small exposure window in practice.
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

    _record, new_token = refreshed
    response = JSONResponse(content={"ok": True})
    response.headers["Cache-Control"] = "no-store, no-cache"
    set_session_cookie(response, new_token)
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
            try:
                await redis.delete(_me_cache_key(raw_token))
            except Exception:
                pass  # non-critical: stale /me cache key; expires on its own TTL

    response = JSONResponse(content={"ok": True})
    response.headers["Cache-Control"] = "no-store, no-cache"
    clear_session_cookie(response)
    return response


@router.get("/auth/me")
@limiter.limit("120/minute")
async def auth_me(request: Request, db=Depends(get_db)):
    """Return email, tier, and credits. API key is NOT returned here — use GET /account/api-key.

    Accepts (in priority order):
      - wf_session HttpOnly cookie (browser dashboard, preferred)
      - Authorization: Bearer <supabase_jwt> (legacy / non-browser)
      - X-Wayforth-API-Key: <api_key> (programmatic clients)
    """
    # Fastest path: validated session cookie (middleware already looked it up).
    from core.session import get_request_session, get_request_session_token
    session = get_request_session(request)
    if session:
        raw_token = get_request_session_token(request)
        redis = _redis_for_session()

        me_payload = None
        me_key = None
        if redis is not None and raw_token:
            me_key = _me_cache_key(raw_token)
            try:
                cached = await redis.get(me_key)
                if cached:
                    me_payload = json.loads(cached)
            except Exception:
                pass  # cache miss — fall through to DB

        if me_payload is None:
            row = await db.fetchrow("""
                SELECT u.email, k.tier,
                       uc.package_tier, uc.credits_balance, uc.pioneer_credits_balance, uc.lifetime_credits
                FROM users u
                LEFT JOIN api_keys k ON k.user_id = u.id AND k.active = true
                LEFT JOIN user_credits uc ON uc.user_id = u.id
                WHERE u.id = $1::uuid
                ORDER BY (k.encrypted_key IS NOT NULL) DESC NULLS LAST, k.created_at DESC NULLS LAST
                LIMIT 1
            """, session["user_id"])
            if not row:
                # Session is valid but the underlying account is gone — treat as logout.
                raise HTTPException(status_code=401, detail={"error": "account_not_found"})
            tier = _credits_to_tier(row["lifetime_credits"] or 0, row["package_tier"])
            _main = row["credits_balance"] or 0
            _pioneer = row["pioneer_credits_balance"] or 0
            me_payload = {
                "email": row["email"],
                "tier": tier,
                "credits_remaining": _main,
                "pioneer_credits_remaining": _pioneer,
                "total_credits": _main + _pioneer,
            }
            if redis is not None and me_key:
                try:
                    await redis.set(me_key, json.dumps(me_payload), ex=_ME_CACHE_TTL)
                except Exception:
                    pass  # non-fatal — next call will repopulate

        response = JSONResponse(content=me_payload)
        response.headers["Cache-Control"] = "no-store, no-cache"
        return response

    # API key header path
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if raw_key:
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        row = await db.fetchrow("""
            SELECT u.email, k.key_prefix, k.tier,
                   uc.package_tier, uc.credits_balance, uc.pioneer_credits_balance, uc.lifetime_credits
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
        _main = row["credits_balance"] or 0
        _pioneer = row["pioneer_credits_balance"] or 0
        response = JSONResponse(content={
            "email": row["email"],
            "tier": tier,
            "credits_remaining": _main,
            "pioneer_credits_remaining": _pioneer,
            "total_credits": _main + _pioneer,
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
        claims = await verify_supabase_jwt(token)
        supabase_sub = claims.get("sub", "")
        if not supabase_sub:
            raise ValueError("no sub")
    except Exception as e:
        logger.info("auth_failure reason=invalid_supabase_jwt path=/auth/me-bearer err=%s", type(e).__name__)
        raise HTTPException(status_code=401, detail="Invalid token")

    row = await db.fetchrow("""
        SELECT u.email, k.key_prefix, k.tier,
               uc.package_tier, uc.credits_balance, uc.pioneer_credits_balance, uc.lifetime_credits
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
    _main = row["credits_balance"] or 0
    _pioneer = row["pioneer_credits_balance"] or 0

    response = JSONResponse(content={
        "email": row["email"],
        "tier": tier,
        "credits_remaining": _main,
        "pioneer_credits_remaining": _pioneer,
        "total_credits": _main + _pioneer,
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
        SELECT key_prefix, encrypted_key, key_version, created_at, last_used_at
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
            # v0.8.0 Item 3: decrypt with the version stored on the row.
            # Pre-v0.8.0 rows have key_version=1 (column default).
            stored_version = row["key_version"] if row["key_version"] is not None else 1
            api_key = decrypt_api_key(row["encrypted_key"], stored_version)
            encrypted = True
        except Exception as e:
            logger.error("api_key decrypt failed (encryption key rotated or corrupt?): %s", type(e).__name__)
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
