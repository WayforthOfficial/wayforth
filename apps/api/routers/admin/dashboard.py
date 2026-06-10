"""routers/admin/dashboard.py — /admin-api/* routes and get_admin_session helper."""

import asyncio
import bcrypt
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from core.audit import log_admin_action
from core.credits import PLANS, _dispatch_webhooks
from core.db import get_db
from core.login_security import (
    check_login_lockout, record_login_failure, clear_login_failures,
    check_admin_login_lockout, record_admin_login_failure, clear_admin_login_failures,
)
from core.rate_limit import limiter

logger = logging.getLogger("wayforth")

router = APIRouter()

ADMIN_ROLES = {
    'ceo':        ['all'],
    'operations': ['catalog', 'health', 'tier3', 'webhooks'],
    'support':    ['users', 'keys', 'tier3'],
    'analytics':  ['analytics', 'searches', 'leaderboard'],
}


async def get_admin_session(request: Request, db):
    import os as _os
    from main import ADMIN_KEY
    # X-Admin-Key grants full ceo-level access without a JWT session and without
    # MFA. This is a break-glass mechanism — gate it on an explicit env opt-in
    # so it cannot be used in a hardened production deploy. Always log when it
    # IS used, so abuse leaves a trail (sessioned admin paths produce one too).
    admin_key = request.headers.get("X-Admin-Key", "")
    if admin_key and ADMIN_KEY:
        # S2 (v0.7.8): default to disabled. Break-glass requires explicit
        # WAYFORTH_ADMIN_KEY_ENABLED=true in the deploy env. Set it on Railway
        # production only when you need break-glass access; unset it after.
        admin_key_enabled = _os.environ.get("WAYFORTH_ADMIN_KEY_ENABLED", "false").lower() == "true"
        env_name = _os.environ.get("ENVIRONMENT", "development").lower()
        if not admin_key_enabled:
            logger.warning("X-Admin-Key presented but disabled by WAYFORTH_ADMIN_KEY_ENABLED=false")
            raise HTTPException(status_code=404, detail="Not found")
        if secrets.compare_digest(admin_key, ADMIN_KEY):
            logger.warning(
                "ADMIN_KEY break-glass used env=%s ip=%s ua=%s path=%s",
                env_name,
                request.client.host if request.client else "?",
                request.headers.get("user-agent", "?")[:80],
                request.url.path,
            )
            return {"role": "ceo", "email": "admin", "full_name": "Admin", "is_active": True,
                    "admin_user_id": None}

    token = request.headers.get("X-Admin-Token", "")
    if not token:
        # Return 404 — prevents endpoint enumeration by unauthenticated callers
        raise HTTPException(status_code=404, detail="Not found")

    token_hash = hashlib.sha256(token.encode()).hexdigest()

    session = await db.fetchrow("""
        SELECT s.*, u.email, u.role, u.full_name, u.is_active
        FROM admin_sessions s
        JOIN admin_users u ON u.id = s.admin_user_id
        WHERE s.token_hash = $1 AND s.expires_at > NOW()
    """, token_hash)

    if not session or not session['is_active']:
        # Return 404 regardless of reason — prevents session probing
        raise HTTPException(status_code=404, detail="Not found")

    return dict(session)


@router.post("/admin-api/auth/login")
@limiter.limit("10/minute")
async def admin_login(request: Request, db=Depends(get_db)):
    body = await request.json()
    email = body.get("email", "").lower().strip()
    password = body.get("password", "")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    from core.tier_gates import _get_redis
    from core.rate_limit import get_real_ip
    redis = _get_redis()
    ip = get_real_ip(request)
    # B-001: strict admin-gate per-IP throttle (3 fails/hour → 3h lock) in
    # addition to the shared email/IP login lockout.
    await check_admin_login_lockout(redis, ip)
    await check_login_lockout(email, redis, ip=ip)

    user = await db.fetchrow(
        "SELECT * FROM admin_users WHERE email = $1 AND is_active = true", email
    )

    if not user or not bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
        await record_admin_login_failure(redis, ip)
        await record_login_failure(email, redis, ip=ip)
        # v0.8.0 Item 4: audit failed logins so a brute-force attempt is visible
        # to anyone reading the audit log, not just to whoever scrapes logs.
        if user:
            await log_admin_action(
                db, {"admin_user_id": user["id"], "email": email},
                "admin_login_failed", payload={"reason": "bad_password"}, request=request,
            )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    await clear_login_failures(email, redis)
    await clear_admin_login_failures(redis, ip)

    if user.get("mfa_enabled"):
        from routers.mfa import issue_mfa_challenge
        challenge = await issue_mfa_challenge(db, "admin", user["id"])
        return {"mfa_required": True, "mfa_challenge": challenge, "token": None}

    raw_token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=12)

    await db.execute("""
        INSERT INTO admin_sessions (admin_user_id, token_hash, expires_at, ip_address)
        VALUES ($1, $2, $3, $4)
    """, user['id'], token_hash, expires_at,
        request.client.host if request.client else None)

    await db.execute(
        "UPDATE admin_users SET last_login_at = NOW() WHERE id = $1", user['id']
    )

    await log_admin_action(
        db, {"admin_user_id": user["id"], "email": email},
        "admin_login", payload={"role": user["role"]}, request=request,
    )

    return {
        "token": raw_token,
        "expires_at": expires_at.isoformat(),
        "admin": {
            "id": str(user['id']),
            "email": user['email'],
            "full_name": user['full_name'],
            "role": user['role'],
        }
    }


@router.post("/admin-api/auth/logout")
async def admin_logout(request: Request, db=Depends(get_db)):
    token = request.headers.get("X-Admin-Token", "")
    if token:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        await db.execute(
            "DELETE FROM admin_sessions WHERE token_hash = $1", token_hash
        )
    return {"status": "logged out"}


@router.get("/admin-api/auth/me")
async def admin_me(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    return {
        "id": session.get('admin_user_id'),
        "email": session['email'],
        "full_name": session['full_name'],
        "role": session['role'],
    }


@router.get("/admin-api/team")
async def admin_team(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] != 'ceo':
        raise HTTPException(status_code=403, detail="CEO access required")

    members = await db.fetch("""
        SELECT id, email, full_name, role, is_active, last_login_at, created_at
        FROM admin_users ORDER BY created_at ASC
    """)
    return {"team": [dict(m) for m in members]}


@router.post("/admin-api/team/invite")
async def admin_invite(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] != 'ceo':
        raise HTTPException(status_code=403, detail="CEO access required")

    body = await request.json()
    email = body.get("email", "").lower().strip()
    full_name = body.get("full_name", "")
    role = body.get("role", "support")
    temp_password = body.get("password", "")

    if not all([email, full_name, role, temp_password]):
        raise HTTPException(status_code=400, detail="All fields required")
    if role not in ['support', 'operations', 'analytics', 'ceo']:
        raise HTTPException(status_code=400, detail="Invalid role")

    password_hash = bcrypt.hashpw(
        temp_password.encode(), bcrypt.gensalt()
    ).decode()

    import asyncpg as _asyncpg
    try:
        member = await db.fetchrow("""
            INSERT INTO admin_users (email, password_hash, full_name, role, created_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, email, full_name, role, created_at
        """, email, password_hash, full_name, role,
            session.get('admin_user_id'))
    except _asyncpg.UniqueViolationError:
        raise HTTPException(status_code=400, detail="Email already exists")
    # S12 (v0.7.8): never echo the temp password back in the response.
    logger.info(
        "ADMIN_ACTION action=team_invite invited_by=%s invited_email=%s role=%s",
        session.get('admin_user_id'), email, role,
    )
    await log_admin_action(
        db, session, "invite_team_member",
        target_resource=str(member["id"]),
        payload={"invited_email": email, "role": role, "full_name": full_name},
        request=request,
    )
    return {"status": "invited", "member": dict(member)}


@router.patch("/admin-api/team/{member_id}")
async def admin_update_member(
    request: Request, member_id: str, db=Depends(get_db)
):
    session = await get_admin_session(request, db)
    if session['role'] != 'ceo':
        raise HTTPException(status_code=403, detail="CEO access required")

    body = await request.json()

    if 'is_active' in body:
        await db.execute(
            "UPDATE admin_users SET is_active=$1 WHERE id=$2",
            body['is_active'], member_id
        )
    if 'role' in body:
        await db.execute(
            "UPDATE admin_users SET role=$1 WHERE id=$2",
            body['role'], member_id
        )
    return {"status": "updated"}


# E1 (v0.7.8): degrade-but-don't-lie helpers for admin_overview. The previous
# `except: var = 0` pattern hid DB failures behind clean zeros — the dashboard
# looked fine while the DB was on fire. These helpers log every failure with
# the metric name so operators can see exactly which counter is degraded.
async def _safe_count(db, name: str, query: str, *args) -> int:
    try:
        return int(await db.fetchval(query, *args) or 0)
    except Exception as e:
        logger.error("admin_overview metric=%s failed: %s", name, e, exc_info=True)
        return 0


async def _safe_decimal(db, name: str, query: str, *args) -> float:
    try:
        return float(await db.fetchval(query, *args) or 0.0)
    except Exception as e:
        logger.error("admin_overview metric=%s failed: %s", name, e, exc_info=True)
        return 0.0


async def _safe_fetch(db, name: str, query: str, *args) -> list:
    try:
        return list(await db.fetch(query, *args))
    except Exception as e:
        logger.error("admin_overview metric=%s failed: %s", name, e, exc_info=True)
        return []


@router.get("/admin-api/overview")
async def admin_overview(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)

    total_services = await _safe_count(db, "total_services", "SELECT COUNT(*) FROM services")
    tier2 = await _safe_count(db, "tier2", "SELECT COUNT(*) FROM services WHERE coverage_tier >= 2 AND consecutive_failures < 3")
    total_users = await _safe_count(db, "total_users", "SELECT COUNT(*) FROM users")
    total_keys = await _safe_count(db, "total_keys", "SELECT COUNT(*) FROM api_keys")
    searches_24h = await _safe_count(
        db, "searches_24h",
        "SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '24h'",
    )
    searches_7d = await _safe_count(
        db, "searches_7d",
        "SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '7 days'",
    )
    pending_tier3 = await _safe_count(
        db, "pending_tier3",
        "SELECT COUNT(*) FROM tier3_applications WHERE kyb_status = 'pending'",
    )
    total_agents = await _safe_count(db, "total_agents", "SELECT COUNT(*) FROM agent_identities")
    daily = await _safe_fetch(db, "daily_searches", """
        SELECT DATE(created_at) as date, COUNT(*) as count
        FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '30 days'
        GROUP BY DATE(created_at)
        ORDER BY date ASC
    """)
    signups = await _safe_fetch(db, "daily_signups", """
        SELECT DATE(created_at) as date, COUNT(*) as count
        FROM users
        WHERE created_at > NOW() - INTERVAL '30 days'
        GROUP BY DATE(created_at)
        ORDER BY date ASC
    """)
    calls_30d = await _safe_count(db, "calls_30d", """
        SELECT COUNT(*) FROM credit_transactions
        WHERE type = 'execution'
          AND created_at > NOW() - INTERVAL '30 days'
    """)
    revenue_30d_usd = await _safe_decimal(db, "revenue_30d_usd", """
        SELECT COALESCE(SUM(amount_usd), 0) FROM package_purchases
        WHERE payment_status = 'completed'
          AND purchased_at > NOW() - INTERVAL '30 days'
    """)

    return {
        "stats": {
            "total_services": total_services,
            "tier2": tier2,
            "total_users": total_users,
            "total_keys": total_keys,
            "searches_24h": searches_24h,
            "searches_7d": searches_7d,
            "pending_tier3": pending_tier3,
            "total_agents": total_agents,
            "calls_30d": int(calls_30d),
            "revenue_30d_usd": round(float(revenue_30d_usd), 2),
        },
        "daily_searches": [{"date": str(r['date']), "count": r['count']} for r in daily],
        "daily_signups": [{"date": str(r['date']), "count": r['count']} for r in signups],
        "admin": {
            "email": session['email'],
            "role": session['role'],
            "full_name": session['full_name'],
        }
    }


@router.get("/admin-api/users")
async def admin_users_list(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    db=Depends(get_db)
):
    session = await get_admin_session(request, db)
    if session['role'] not in ['ceo', 'support']:
        raise HTTPException(status_code=403)

    users = await db.fetch("""
        SELECT u.id, u.email, u.created_at,
               k.tier, k.owner_email, k.key_prefix,
               k.usage_this_month, k.monthly_quota, k.monthly_calls_count,
               k.subscription_status,
               uc.package_tier, uc.credits_balance, uc.lifetime_credits,
               GREATEST(MAX(s.created_at), MAX(ct.created_at)) as last_active
        FROM users u
        LEFT JOIN LATERAL (
            SELECT tier, owner_email, key_prefix, usage_this_month, monthly_quota,
                   monthly_calls_count, subscription_status
            FROM api_keys
            WHERE user_id = u.id AND active = true
            ORDER BY (encrypted_key IS NOT NULL) DESC, created_at DESC
            LIMIT 1
        ) k ON true
        LEFT JOIN user_credits uc ON uc.user_id = u.id
        LEFT JOIN search_analytics s ON s.user_id = u.id
        LEFT JOIN credit_transactions ct ON ct.user_id = u.id AND ct.type = 'execution'
        WHERE u.email NOT LIKE '%@wayforth.test'
          AND u.email NOT LIKE 'probe-%'
        GROUP BY u.id, u.email, u.created_at,
                 k.tier, k.owner_email, k.key_prefix,
                 k.usage_this_month, k.monthly_quota, k.monthly_calls_count,
                 k.subscription_status,
                 uc.package_tier, uc.credits_balance, uc.lifetime_credits
        ORDER BY last_active DESC NULLS LAST
        LIMIT $1 OFFSET $2
    """, limit, offset)

    total = await db.fetchval("""
        SELECT COUNT(*) FROM users
        WHERE email NOT LIKE '%@wayforth.test'
          AND email NOT LIKE 'probe-%'
    """)

    def _fix_usage(u: dict) -> dict:
        tier = u.get("tier") or "free"
        plan = PLANS.get(tier)
        if plan:
            u["monthly_quota"] = plan["calls_included"]
        u["monthly_calls_count"] = u.get("monthly_calls_count") or 0
        u["plan"] = u.get("package_tier") or tier
        u["calls_remaining"] = u.get("credits_balance") or 0
        return u

    return {
        "users": [_fix_usage(dict(u)) for u in users],
        "total": total,
        "limit": limit,
        "offset": offset
    }


@router.get("/admin-api/catalog")
async def admin_catalog(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] not in ['ceo', 'operations']:
        raise HTTPException(status_code=403)

    rows = await db.fetch("""
        SELECT category,
               COUNT(*) as total,
               COUNT(*) FILTER (WHERE coverage_tier >= 2) as tier2,
               COUNT(*) FILTER (WHERE endpoint_url NOT ILIKE '%github%') as real_apis
        FROM services
        GROUP BY category ORDER BY total DESC
    """)

    recent_promotions = await db.fetch("""
        SELECT name, coverage_tier, last_tested_at
        FROM services
        WHERE coverage_tier >= 2
        ORDER BY last_tested_at DESC LIMIT 10
    """)

    return {
        "by_category": [dict(r) for r in rows],
        "recent_promotions": [dict(r) for r in recent_promotions]
    }


@router.get("/admin-api/users/{user_id}")
async def admin_get_user(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    # Use the same LATERAL join as the list endpoint so both views read
    # the same canonical active api_key and agree on tier.
    user = await db.fetchrow("""
        SELECT u.id, u.email, u.created_at, u.stripe_customer_id,
               k.tier, k.key_prefix, k.usage_this_month, k.monthly_quota, k.monthly_calls_count,
               k.subscription_status, k.stripe_subscription_id,
               k.created_at as key_created_at, k.last_used_at,
               uc.package_tier, uc.credits_balance,
               COALESCE(sa.total_searches, 0) as total_searches,
               sa.last_search_at
        FROM users u
        LEFT JOIN LATERAL (
            SELECT tier, key_prefix, usage_this_month, monthly_quota, monthly_calls_count,
                   subscription_status, stripe_subscription_id,
                   created_at, last_used_at
            FROM api_keys
            WHERE user_id = u.id AND active = true
            ORDER BY (encrypted_key IS NOT NULL) DESC, created_at DESC
            LIMIT 1
        ) k ON true
        LEFT JOIN user_credits uc ON uc.user_id = u.id
        LEFT JOIN LATERAL (
            SELECT COUNT(*) as total_searches, MAX(created_at) as last_search_at
            FROM search_analytics
            WHERE user_id = u.id
        ) sa ON true
        WHERE u.id = $1::uuid
    """, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Scope to THIS user — the original query had no user_id filter and would
    # return arbitrary users' search history when an admin viewed any profile.
    searches = await db.fetch("""
        SELECT query, created_at, top_result_id
        FROM search_analytics
        WHERE user_id = $1::uuid
          AND created_at > NOW() - INTERVAL '30 days'
        ORDER BY created_at DESC LIMIT 10
    """, user_id)

    service_keys = await db.fetch("""
        SELECT service_slug, service_name, key_preview,
               total_calls, last_used_at, active, created_at
        FROM user_service_keys
        WHERE user_id=$1::uuid
        ORDER BY created_at DESC
    """, user_id)

    user_dict = dict(user)
    tier = user_dict.get("tier") or "free"
    plan = PLANS.get(tier)
    if plan:
        user_dict["monthly_quota"] = plan["calls_included"]
    user_dict["monthly_calls_count"] = user_dict.get("monthly_calls_count") or 0

    result = {
        "user": user_dict,
        "recent_searches": [dict(s) for s in searches],
        "service_keys": [dict(k) for k in service_keys],
    }
    return result


@router.patch("/admin-api/users/{user_id}/tier")
async def admin_change_tier(request: Request, user_id: str, db=Depends(get_db)):
    from core.credits import PLANS
    session = await get_admin_session(request, db)
    # Tier changes affect billing and feature gates — restrict to roles that
    # legitimately operate on customer plans. Previously any authenticated
    # admin (e.g. an `analytics` viewer) could rewrite any user's tier.
    if session["role"] not in ("ceo", "support", "operations"):
        raise HTTPException(status_code=403, detail="Insufficient admin role")
    body = await request.json()
    new_tier = body.get("tier")
    reason = body.get("reason", "Admin manual change")

    VALID_TIERS = list(PLANS.keys())  # ['free', 'builder', 'starter', 'pro', 'growth']
    if new_tier not in VALID_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Valid: {VALID_TIERS}")

    # L5 (v0.7.8): audit log every admin write before mutating state.
    logger.warning(
        "ADMIN_ACTION admin=%s action=tier_change target_user=%s new_tier=%s reason=%r",
        session.get('admin_user_id') or session.get('email'),
        user_id, new_tier, reason,
    )

    plan = PLANS[new_tier]
    new_quota = plan["calls_included"]
    new_credits = plan["monthly_credits"]

    old_key = await db.fetchrow(
        "SELECT tier FROM api_keys WHERE user_id=$1::uuid AND active=true "
        "ORDER BY (encrypted_key IS NOT NULL) DESC, created_at DESC LIMIT 1",
        user_id,
    )
    old_tier = old_key["tier"] if old_key else "free"

    async with db.transaction():
        await db.execute("""
            UPDATE api_keys SET tier = $1, monthly_quota = $2
            WHERE user_id = $3::uuid AND active = true
        """, new_tier, new_quota, user_id)

        existing = await db.fetchrow(
            "SELECT user_id FROM user_credits WHERE user_id = $1::uuid", user_id
        )
        if existing:
            await db.execute("""
                UPDATE user_credits
                SET credits_balance = $1, lifetime_credits = $1,
                    package_tier = $2, updated_at = NOW()
                WHERE user_id = $3::uuid
            """, new_credits, new_tier, user_id)
        else:
            await db.execute("""
                INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier)
                VALUES ($1::uuid, $2, $2, $3)
            """, user_id, new_credits, new_tier)

        await db.execute("""
            INSERT INTO credit_transactions (user_id, amount, balance_after, type, description)
            VALUES ($1::uuid, $2, $2, 'tier_change', $3)
        """, user_id, new_credits, f"Tier changed {old_tier} → {new_tier} by admin")

    asyncio.create_task(_dispatch_webhooks(
        user_id, "tier.changed", {
            "old_tier": old_tier,
            "new_tier": new_tier,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    ))

    await log_admin_action(
        db, session, "tier_change",
        target_user_id=user_id,
        payload={"old_tier": old_tier, "new_tier": new_tier, "reason": reason},
        request=request,
    )

    return {
        "status": "updated",
        "tier": new_tier,
        "credits_reset_to": new_credits,
        "changed_by": session['email'],
        "reason": reason,
    }


@router.post("/admin-api/users/{user_id}/reset-usage")
async def admin_reset_usage(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session["role"] not in ("ceo", "support", "operations"):
        raise HTTPException(status_code=403, detail="Insufficient admin role")
    body = await request.json()
    reason = body.get("reason", "Admin reset")

    logger.warning(
        "ADMIN_ACTION admin=%s action=reset_usage target_user=%s reason=%r",
        session.get('admin_user_id') or session.get('email'), user_id, reason,
    )
    await db.execute("""
        UPDATE api_keys SET usage_this_month = 0, quota_reset_at = NOW()
        WHERE user_id = $1::uuid
    """, user_id)

    await log_admin_action(
        db, session, "reset_usage",
        target_user_id=user_id,
        payload={"reason": reason},
        request=request,
    )

    return {"status": "reset", "changed_by": session['email'], "reason": reason}


@router.post("/admin-api/users/{user_id}/add-credits")
async def admin_add_credits(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    # Granting up to 1M credits per call is the same as minting money — restrict
    # to ceo or support roles. Previously any admin role could grant credits.
    if session["role"] not in ("ceo", "support"):
        raise HTTPException(status_code=403, detail="Insufficient admin role")
    body = await request.json()
    credits = int(body.get("credits", 0))
    reason = body.get("reason", "Admin grant")
    payment_method = body.get("payment_method", "admin")

    if credits <= 0 or credits > 1000000:
        raise HTTPException(status_code=400, detail="Credits must be 1-1,000,000")

    logger.warning(
        "ADMIN_ACTION admin=%s action=add_credits target_user=%s credits=%d reason=%r",
        session.get('admin_user_id') or session.get('email'), user_id, credits, reason,
    )

    async with db.transaction():
        row = await db.fetchrow(
            "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
            user_id
        )
        if not row:
            await db.execute("""
                INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier)
                VALUES ($1::uuid, $2, $2, 'free')
            """, user_id, credits)
            new_balance = credits
        else:
            new_balance = row['credits_balance'] + credits
            await db.execute("""
                UPDATE user_credits
                SET credits_balance = $1, lifetime_credits = lifetime_credits + $2, updated_at = NOW()
                WHERE user_id = $3::uuid
            """, new_balance, credits, user_id)

        await db.execute("""
            INSERT INTO credit_transactions
            (user_id, amount, balance_after, type, description)
            VALUES ($1::uuid, $2, $3, 'admin_grant', $4)
        """, user_id, credits, new_balance, reason)

    await log_admin_action(
        db, session, "credit_grant",
        target_user_id=user_id,
        payload={
            "credits": credits,
            "new_balance": new_balance,
            "reason": reason,
            "payment_method": payment_method,
        },
        request=request,
    )

    return {
        "status": "credits_added",
        "credits_added": credits,
        "new_balance": new_balance,
        "granted_by": session['email'],
        "reason": reason,
    }


@router.post("/admin-api/users/{user_id}/regenerate-key")
async def admin_regenerate_key(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    # The new key value is returned in the response — anyone calling this can
    # impersonate the target user until the user notices and re-rotates.
    # Restrict to CEO and support roles.
    if session["role"] not in ("ceo", "support"):
        raise HTTPException(status_code=403, detail="Insufficient admin role")
    body = await request.json()
    reason = body.get("reason", "Admin revoked")

    logger.warning(
        "ADMIN_ACTION admin=%s action=regenerate_user_key target_user=%s reason=%r",
        session.get('admin_user_id') or session.get('email'), user_id, reason,
    )

    raw_key = "wf_live_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]

    # Deactivate all existing keys, then reactivate only the most recent one with new credentials.
    # Without this, if all keys were already inactive (e.g. after suspend), the regenerated key
    # would remain inactive and the user could never authenticate.
    await db.execute(
        "UPDATE api_keys SET active = false WHERE user_id = $1::uuid",
        user_id,
    )
    await db.execute("""
        UPDATE api_keys
        SET key_hash = $1, key_prefix = $2, last_used_at = NULL, active = true
        WHERE user_id = $3::uuid
          AND created_at = (SELECT MAX(created_at) FROM api_keys WHERE user_id = $3::uuid)
    """, key_hash, key_prefix, user_id)

    await log_admin_action(
        db, session, "api_key_revoke",
        target_user_id=user_id,
        target_resource=key_prefix,
        payload={"reason": reason},
        request=request,
    )

    return {
        "status": "regenerated",
        "new_key": raw_key,
        "new_prefix": key_prefix,
        "changed_by": session['email'],
        "reason": reason,
        "warning": "Send this key to the user securely. It will not be shown again."
    }


@router.patch("/admin-api/users/{user_id}/suspend")
async def admin_suspend_user(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session["role"] not in ("ceo", "support", "operations"):
        raise HTTPException(status_code=403, detail="Insufficient admin role")
    body = await request.json()
    suspended = body.get("suspended", True)
    reason = body.get("reason", "")

    logger.warning(
        "ADMIN_ACTION admin=%s action=%s target_user=%s reason=%r",
        session.get('admin_user_id') or session.get('email'),
        "suspend" if suspended else "unsuspend", user_id, reason,
    )

    await db.execute("""
        UPDATE api_keys SET active = $1 WHERE user_id = $2::uuid
    """, not suspended, user_id)

    await log_admin_action(
        db, session, "user_suspend" if suspended else "user_unsuspend",
        target_user_id=user_id,
        payload={"reason": reason},
        request=request,
    )

    return {
        "status": "suspended" if suspended else "unsuspended",
        "changed_by": session['email'],
        "reason": reason
    }


@router.patch("/admin-api/users/{user_id}/custom-quota")
async def admin_custom_quota(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] not in ['ceo', 'operations']:
        raise HTTPException(status_code=403)
    body = await request.json()
    quota = int(body.get("quota", 0))
    reason = body.get("reason", "")

    logger.warning(
        "ADMIN_ACTION admin=%s action=custom_quota target_user=%s quota=%d reason=%r",
        session.get('admin_user_id') or session.get('email'), user_id, quota, reason,
    )

    await db.execute("""
        UPDATE api_keys SET monthly_quota = $1 WHERE user_id = $2::uuid
    """, quota, user_id)

    await log_admin_action(
        db, session, "quota_set",
        target_user_id=user_id,
        payload={"quota": quota, "reason": reason},
        request=request,
    )

    return {"status": "quota_set", "quota": quota, "changed_by": session['email'], "reason": reason}


@router.get("/admin-api/users/{user_id}/searches")
async def admin_user_searches(request: Request, user_id: str, limit: int = 50, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] not in ['ceo', 'support']:
        raise HTTPException(status_code=403)

    key = await db.fetchrow("SELECT key_prefix FROM api_keys WHERE user_id = $1::uuid", user_id)
    if not key:
        return {"searches": [], "total": 0}

    # Scope to the target user — the original query returned global search
    # history regardless of user_id.
    searches = await db.fetch("""
        SELECT query, created_at, top_result_id, led_to_payment
        FROM search_analytics
        WHERE user_id = $1::uuid
        ORDER BY created_at DESC LIMIT $2
    """, user_id, limit)

    return {
        "searches": [dict(s) for s in searches],
        "total": len(searches)
    }


@router.get("/admin-api/users/{user_id}/service-keys")
async def admin_get_user_service_keys(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] not in ['ceo', 'support']:
        raise HTTPException(status_code=403)
    keys = await db.fetch("""
        SELECT service_slug, service_name, key_preview,
               total_calls, last_used_at, active, created_at
        FROM user_service_keys
        WHERE user_id=$1::uuid
        ORDER BY created_at DESC
    """, user_id)
    return {"service_keys": [dict(k) for k in keys], "total": len(keys)}


# ── Provider Management ────────────────────────────────────────────────────────

# ── Pioneer Boost Management ──────────────────────────────────────────────────

@router.get("/admin-api/providers/boosts")
async def admin_boosts_list(request: Request, db=Depends(get_db)):
    """List all providers with active or historical Pioneer Boosts."""
    session = await get_admin_session(request, db)
    rows = await db.fetch("""
        SELECT p.id, p.company_name, p.email, p.boost_tier,
               p.boost_used, p.boost_paused,
               p.boost_activated_at, p.boost_expires_at,
               ps.service_slug
          FROM providers p
          LEFT JOIN provider_services ps ON ps.provider_id = p.id
         WHERE p.boost_used = TRUE
         ORDER BY p.boost_activated_at DESC NULLS LAST
    """)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    result = []
    for r in rows:
        expires = r["boost_expires_at"]
        active = (
            not bool(r["boost_paused"])
            and expires is not None
            and expires > now
        )
        days_remaining = max(0, (expires - now).days) if expires else 0
        result.append({
            "provider_id":    str(r["id"]),
            "company_name":   r["company_name"],
            "email":          r["email"],
            "service_slug":   r["service_slug"],
            "boost_tier":     r["boost_tier"],
            "boost_active":   active,
            "boost_paused":   bool(r["boost_paused"]),
            "days_remaining": days_remaining,
            "activated_at":   r["boost_activated_at"].isoformat() if r["boost_activated_at"] else None,
            "expires_at":     expires.isoformat() if expires else None,
        })
    return {"boosts": result, "total": len(result)}


@router.get("/admin-api/providers")
async def admin_providers_list(
    request: Request,
    q: str = "",
    sort: str = "newest",
    limit: int = 50,
    offset: int = 0,
    db=Depends(get_db),
):
    await get_admin_session(request, db)

    where = "WHERE 1=1"
    params: list = []
    idx = 1

    if q:
        where += f" AND p.email ILIKE ${idx}"
        params.append(f"%{q}%")
        idx += 1

    order = {
        "newest":        "p.created_at DESC",
        "most_services": "service_count DESC",
        "most_calls":    "total_calls DESC",
    }.get(sort, "p.created_at DESC")

    rows = await db.fetch(f"""
        SELECT
            p.id, p.email, p.company_name, p.created_at,
            p.tier, p.verified,
            COALESCE(p.suspended, false) AS suspended,
            COUNT(DISTINCT ps.id)        AS service_count,
            COALESCE(calls.total_calls, 0) AS total_calls
        FROM providers p
        LEFT JOIN provider_services ps ON ps.provider_id = p.id
        LEFT JOIN (
            SELECT ps2.provider_id, COUNT(ct.*) AS total_calls
            FROM provider_services ps2
            LEFT JOIN credit_transactions ct
                ON ct.service_id = ps2.service_slug AND ct.type = 'execution'
            GROUP BY ps2.provider_id
        ) calls ON calls.provider_id = p.id
        {where}
        GROUP BY p.id, p.email, p.company_name, p.created_at,
                 p.tier, p.verified, p.suspended, calls.total_calls
        ORDER BY {order}
        LIMIT ${idx} OFFSET ${idx + 1}
    """, *params, limit, offset)

    total = await db.fetchval(
        f"SELECT COUNT(*) FROM providers p {where}", *params
    )

    return {
        "providers": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/admin-api/providers/{provider_id}")
async def admin_provider_detail(
    request: Request, provider_id: str, db=Depends(get_db)
):
    await get_admin_session(request, db)

    provider = await db.fetchrow("""
        SELECT id, email, company_name, created_at, tier, verified,
               COALESCE(suspended, false) AS suspended, last_login_at
        FROM providers WHERE id = $1::uuid
    """, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")

    services = await db.fetch("""
        SELECT
            ps.service_slug AS slug,
            ps.service_name AS name,
            ps.verified,
            ps.created_at AS registered_at,
            COALESCE(s.category, '') AS category,
            COALESCE(s.coverage_tier, 1) AS tier,
            COALESCE(s.pricing_usdc, 0) AS price,
            COALESCE(calls.cnt, 0) AS calls
        FROM provider_services ps
        LEFT JOIN services s ON s.slug = ps.service_slug
        LEFT JOIN (
            SELECT service_id, COUNT(*) AS cnt
            FROM credit_transactions
            WHERE type = 'execution'
            GROUP BY service_id
        ) calls ON calls.service_id = ps.service_slug
        WHERE ps.provider_id = $1::uuid
        ORDER BY calls DESC
    """, provider_id)

    return {
        "provider": dict(provider),
        "services": [dict(s) for s in services],
    }


@router.post("/admin-api/providers/{provider_id}/suspend")
async def admin_suspend_provider(
    request: Request, provider_id: str, db=Depends(get_db)
):
    session = await get_admin_session(request, db)
    result = await db.execute(
        "UPDATE providers SET suspended = true WHERE id = $1::uuid",
        provider_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Provider not found")
    return {"status": "suspended", "changed_by": session["email"]}


@router.post("/admin-api/providers/{provider_id}/reinstate")
async def admin_reinstate_provider(
    request: Request, provider_id: str, db=Depends(get_db)
):
    session = await get_admin_session(request, db)
    result = await db.execute(
        "UPDATE providers SET suspended = false WHERE id = $1::uuid",
        provider_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Provider not found")
    return {"status": "reinstated", "changed_by": session["email"]}


@router.post("/admin-api/providers/{provider_id}/boost/pause")
async def admin_boost_pause(request: Request, provider_id: str, db=Depends(get_db)):
    """Manually pause a provider's Pioneer Boost."""
    session = await get_admin_session(request, db)
    result = await db.execute(
        "UPDATE providers SET boost_paused = TRUE WHERE id = $1::uuid AND boost_used = TRUE",
        provider_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Provider not found or boost not activated")
    from core.audit import log_admin_action
    await log_admin_action(db, session, "boost_manually_paused",
                           target_resource=provider_id, request=request)
    return {"status": "paused", "provider_id": provider_id, "changed_by": session["email"]}


@router.post("/admin-api/providers/{provider_id}/boost/unpause")
async def admin_boost_unpause(request: Request, provider_id: str, db=Depends(get_db)):
    """Manually unpause a provider's Pioneer Boost (resumes Pioneer routing traffic)."""
    session = await get_admin_session(request, db)
    row = await db.fetchrow(
        "SELECT boost_expires_at FROM providers WHERE id = $1::uuid AND boost_used = TRUE",
        provider_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Provider not found or boost not activated")
    from datetime import datetime, timezone
    if row["boost_expires_at"] and row["boost_expires_at"] < datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail={"error": "boost_expired", "message": "Boost window has elapsed; cannot unpause."})
    await db.execute(
        "UPDATE providers SET boost_paused = FALSE WHERE id = $1::uuid",
        provider_id,
    )
    from core.audit import log_admin_action
    await log_admin_action(db, session, "boost_manually_unpaused",
                           target_resource=provider_id,
                           request=request)
    return {"status": "unpaused", "provider_id": provider_id, "changed_by": session["email"]}


# ── Pioneer Developer Program Stats ───────────────────────────────────────────

@router.get("/admin-api/pioneer/stats")
async def admin_pioneer_stats(request: Request, db=Depends(get_db)):
    """Platform-wide Pioneer Program statistics."""
    await get_admin_session(request, db)

    total_opted_in = await db.fetchval(
        "SELECT COUNT(*) FROM users WHERE pioneer_opt_in = TRUE"
    ) or 0

    credits_awarded_total = await db.fetchval("""
        SELECT COALESCE(SUM(amount), 0)
          FROM credit_transactions
         WHERE type = 'pioneer_bonus'
           AND created_at >= date_trunc('month', NOW())
    """) or 0

    pioneer_calls_month = await db.fetchval("""
        SELECT COUNT(DISTINCT sa.id)
          FROM search_analytics sa
          JOIN search_outcomes so ON so.query_id = sa.id
         WHERE so.pioneer_routed = TRUE
           AND sa.created_at >= date_trunc('month', NOW())
    """) or 0

    # Breakdown of opted-in users by tier
    tier_rows = await db.fetch("""
        SELECT ak.tier, COUNT(*) AS cnt
          FROM users u
          JOIN api_keys ak ON ak.user_id = u.id AND ak.active = TRUE
         WHERE u.pioneer_opt_in = TRUE
         GROUP BY ak.tier
    """)
    by_tier = {r["tier"]: int(r["cnt"]) for r in tier_rows}

    return {
        "total_pioneer_developers": int(total_opted_in),
        "pioneer_calls_this_month": int(pioneer_calls_month),
        "credits_awarded_this_month": int(credits_awarded_total),
        "opted_in_by_tier": by_tier,
    }


# ── Catalog / Services Management ─────────────────────────────────────────────

@router.get("/admin-api/catalog/services")
async def admin_catalog_services_list(
    request: Request,
    q: str = "",
    category: str = "",
    tier: str = "",
    verified: str = "",
    x402: str = "",
    limit: int = 50,
    offset: int = 0,
    db=Depends(get_db),
):
    await get_admin_session(request, db)

    where_parts = ["1=1"]
    params: list = []
    idx = 1

    if q:
        where_parts.append(f"(s.slug ILIKE ${idx} OR s.name ILIKE ${idx})")
        params.append(f"%{q}%")
        idx += 1
    if category:
        where_parts.append(f"s.category = ${idx}")
        params.append(category)
        idx += 1
    if tier:
        try:
            where_parts.append(f"s.coverage_tier = ${idx}")
            params.append(int(tier))
            idx += 1
        except ValueError:
            pass
    if verified == "true":
        where_parts.append("s.coverage_tier >= 2")
    elif verified == "false":
        where_parts.append("s.coverage_tier < 2")
    if x402 == "true":
        where_parts.append("s.x402_supported = true")
    elif x402 == "false":
        where_parts.append("(s.x402_supported IS NULL OR s.x402_supported = false)")

    where = "WHERE " + " AND ".join(where_parts)

    rows = await db.fetch(f"""
        SELECT
            s.id, s.slug, s.name, s.description, s.category,
            s.coverage_tier AS tier, s.x402_supported,
            COALESCE(s.pricing_usdc, 0) AS price,
            s.wri_score, s.created_at,
            COALESCE(calls.cnt, 0) AS calls,
            COALESCE(prov.provider_email, '') AS provider
        FROM services s
        LEFT JOIN (
            SELECT service_id, COUNT(*) AS cnt
            FROM credit_transactions WHERE type = 'execution'
            GROUP BY service_id
        ) calls ON calls.service_id = s.slug
        LEFT JOIN (
            SELECT ps.service_slug, p.email AS provider_email
            FROM provider_services ps
            JOIN providers p ON p.id = ps.provider_id
        ) prov ON prov.service_slug = s.slug
        {where}
        ORDER BY calls DESC, s.created_at DESC
        LIMIT ${idx} OFFSET ${idx + 1}
    """, *params, limit, offset)

    total = await db.fetchval(
        f"SELECT COUNT(*) FROM services s {where}", *params
    )

    categories = await db.fetch(
        "SELECT DISTINCT category FROM services WHERE category IS NOT NULL ORDER BY category"
    )

    return {
        "services": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "categories": [r["category"] for r in categories],
    }


@router.post("/admin-api/catalog/services/{slug}/verify")
async def admin_catalog_verify_service(
    request: Request, slug: str, db=Depends(get_db)
):
    session = await get_admin_session(request, db)
    result = await db.execute(
        "UPDATE services SET coverage_tier = 2, consecutive_failures = 0 WHERE slug = $1",
        slug,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Service not found")
    await log_admin_action(
        db, session, "service_approve",
        target_resource=slug,
        request=request,
    )
    return {"status": "verified", "slug": slug, "changed_by": session["email"]}


@router.post("/admin-api/catalog/services/{slug}/demote")
async def admin_catalog_demote_service(
    request: Request, slug: str, db=Depends(get_db)
):
    session = await get_admin_session(request, db)
    result = await db.execute(
        "UPDATE services SET coverage_tier = 1 WHERE slug = $1", slug
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Service not found")
    await log_admin_action(
        db, session, "service_reject",
        target_resource=slug,
        request=request,
    )
    return {"status": "demoted", "slug": slug, "changed_by": session["email"]}


@router.patch("/admin-api/catalog/services/{slug}")
async def admin_catalog_edit_service(
    request: Request, slug: str, db=Depends(get_db)
):
    await get_admin_session(request, db)
    body = await request.json()

    allowed = {"name", "description", "category", "coverage_tier", "pricing_usdc", "x402_supported"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields to update")

    set_parts = []
    params: list = []
    for i, (col, val) in enumerate(updates.items(), start=1):
        set_parts.append(f"{col} = ${i}")
        params.append(val)
    params.append(slug)

    result = await db.execute(
        f"UPDATE services SET {', '.join(set_parts)}, updated_at = NOW() WHERE slug = ${len(params)}",
        *params,
    )
    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail="Service not found")
    return {"status": "updated", "slug": slug}


@router.delete("/admin-api/catalog/services/{slug}")
async def admin_catalog_delete_service(
    request: Request, slug: str, db=Depends(get_db)
):
    session = await get_admin_session(request, db)
    result = await db.execute("DELETE FROM services WHERE slug = $1", slug)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Service not found")
    return {"status": "deleted", "slug": slug, "changed_by": session["email"]}


@router.post("/admin-api/catalog/services/bulk")
async def admin_catalog_bulk_services(
    request: Request, db=Depends(get_db)
):
    session = await get_admin_session(request, db)
    body = await request.json()
    slugs = body.get("slugs", [])
    action = body.get("action", "")

    if not slugs or action not in ("verify", "delete"):
        raise HTTPException(status_code=400, detail="slugs and action ('verify'|'delete') required")

    if action == "verify":
        await db.execute(
            "UPDATE services SET coverage_tier = 2, consecutive_failures = 0 WHERE slug = ANY($1)",
            slugs,
        )
    elif action == "delete":
        await db.execute("DELETE FROM services WHERE slug = ANY($1)", slugs)

    return {"status": action + "d", "count": len(slugs), "changed_by": session["email"]}


# ── v0.8.0 Item 3: API-key encryption rotation ────────────────────────────────


@router.get("/admin-api/keys/rotation-status")
async def admin_keys_rotation_status(request: Request, db=Depends(get_db)):
    """Report how many api_keys rows exist per encryption key version.

    Used by the CEO before and after a rotation to track progress. Any non-CEO
    role can read; this is operational telemetry, not a state mutation.
    """
    session = await get_admin_session(request, db)
    if session["role"] not in ("ceo", "operations"):
        raise HTTPException(status_code=403, detail="Insufficient admin role")
    rows = await db.fetch("""
        SELECT COALESCE(key_version, 1) AS version, COUNT(*) AS count
          FROM api_keys
         WHERE encrypted_key IS NOT NULL
      GROUP BY 1
      ORDER BY 1
    """)
    from core.auth import KEY_VERSIONS, load_key_versions, ROTATION_IN_PROGRESS
    load_key_versions()
    return {
        "rotation_in_progress": ROTATION_IN_PROGRESS,
        "loaded_versions": sorted(KEY_VERSIONS.keys()),
        "rows_per_version": [
            {"version": int(r["version"]), "count": int(r["count"])} for r in rows
        ],
    }


@router.post("/admin-api/keys/rotate")
async def admin_keys_rotate(request: Request, db=Depends(get_db)):
    """Spawn a background re-encryption job from one key version to another.

    Body: {"from_version": int, "to_version": int}

    CEO-only. The job streams api_keys rows where key_version=from_version,
    decrypts with the old version, re-encrypts with the new, and updates
    encrypted_key + key_version atomically per row. Progress is logged; the
    response returns immediately with the job id.
    """
    session = await get_admin_session(request, db)
    if session["role"] != "ceo":
        raise HTTPException(status_code=403, detail="CEO role required")
    body = await request.json()
    from_v = int(body.get("from_version", 0))
    to_v = int(body.get("to_version", 0))
    if from_v <= 0 or to_v <= 0 or from_v == to_v:
        raise HTTPException(
            status_code=400,
            detail="from_version and to_version must be positive integers and differ",
        )

    from core.auth import KEY_VERSIONS, load_key_versions
    load_key_versions()
    if from_v not in KEY_VERSIONS:
        raise HTTPException(status_code=400, detail=f"from_version {from_v} not loaded")
    if to_v not in KEY_VERSIONS:
        raise HTTPException(status_code=400, detail=f"to_version {to_v} not loaded")

    import asyncio as _asyncio
    import uuid as _uuid
    job_id = str(_uuid.uuid4())

    logger.warning(
        "ADMIN_ACTION admin=%s action=keys_rotate from=%s to=%s job=%s",
        session.get("admin_user_id") or session.get("email"),
        from_v, to_v, job_id,
    )

    pool = request.app.state.pool
    _asyncio.create_task(_rotate_api_keys_worker(pool, from_v, to_v, job_id))
    return {"status": "started", "job_id": job_id, "from_version": from_v, "to_version": to_v}


async def _rotate_api_keys_worker(pool, from_v: int, to_v: int, job_id: str) -> None:
    """Background worker: stream rows in batches of 100 and re-encrypt each.

    Per-row transaction (decrypt with old, re-encrypt with new, UPDATE) means
    a partial failure leaves the table in a mixed-version state that the next
    rotation can resume — never in a corrupted state.
    """
    from core.auth import decrypt_api_key, encrypt_api_key
    import core.auth as _auth_mod

    _auth_mod.ROTATION_IN_PROGRESS = True
    total = 0
    failed = 0
    try:
        while True:
            async with pool.acquire() as conn:
                batch = await conn.fetch(
                    """SELECT id, encrypted_key
                         FROM api_keys
                        WHERE key_version = $1
                          AND encrypted_key IS NOT NULL
                        LIMIT 100""",
                    from_v,
                )
            if not batch:
                break
            for row in batch:
                try:
                    plaintext = decrypt_api_key(row["encrypted_key"], from_v)
                    new_ct, _ = encrypt_api_key(plaintext, version=to_v)
                    async with pool.acquire() as conn:
                        await conn.execute(
                            """UPDATE api_keys
                                  SET encrypted_key = $1, key_version = $2
                                WHERE id = $3 AND key_version = $4""",
                            new_ct, to_v, row["id"], from_v,
                        )
                    total += 1
                except Exception as e:
                    failed += 1
                    logger.error("rotation job=%s row=%s failed: %s", job_id, row["id"], e)
            logger.info("rotation job=%s progress total=%d failed=%d", job_id, total, failed)
    finally:
        _auth_mod.ROTATION_IN_PROGRESS = False
        logger.warning(
            "rotation job=%s complete total=%d failed=%d from_v=%d to_v=%d",
            job_id, total, failed, from_v, to_v,
        )


# ── v0.8.0 Item 4: admin audit log reader ────────────────────────────────────


@router.get("/admin-api/audit-log")
async def admin_audit_log_read(
    request: Request,
    db=Depends(get_db),
    admin_id: str | None = None,
    action: str | None = None,
    target_user_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
):
    """Read the append-only admin audit log. CEO-only.

    Filterable by admin_id, action, target_user_id, and a created_at range
    (ISO 8601). Cursor pagination uses the last row's created_at; pass it
    back in `cursor` to fetch the next page. There is no POST/PATCH/DELETE
    counterpart by design — this resource is read-only.
    """
    session = await get_admin_session(request, db)
    if session["role"] != "ceo":
        raise HTTPException(status_code=403, detail="CEO role required")

    limit = max(1, min(int(limit), 200))
    where_parts: list[str] = []
    params: list = []
    idx = 1

    if admin_id:
        where_parts.append(f"admin_id = ${idx}::uuid")
        params.append(admin_id)
        idx += 1
    if action:
        where_parts.append(f"action = ${idx}")
        params.append(action)
        idx += 1
    if target_user_id:
        where_parts.append(f"target_user_id = ${idx}::uuid")
        params.append(target_user_id)
        idx += 1
    if since:
        where_parts.append(f"created_at >= ${idx}::timestamptz")
        params.append(since)
        idx += 1
    if until:
        where_parts.append(f"created_at <= ${idx}::timestamptz")
        params.append(until)
        idx += 1
    if cursor:
        where_parts.append(f"created_at < ${idx}::timestamptz")
        params.append(cursor)
        idx += 1

    where_sql = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)

    rows = await db.fetch(
        f"""SELECT id, admin_id, admin_email, action,
                   target_user_id, target_resource, payload,
                   ip_address, user_agent, created_at
              FROM admin_audit_log
              {where_sql}
          ORDER BY created_at DESC
             LIMIT ${idx}""",
        *params,
    )
    entries = [
        {
            "id": str(r["id"]),
            "admin_id": str(r["admin_id"]),
            "admin_email": r["admin_email"],
            "action": r["action"],
            "target_user_id": str(r["target_user_id"]) if r["target_user_id"] else None,
            "target_resource": r["target_resource"],
            "payload": r["payload"],
            "ip_address": r["ip_address"],
            "user_agent": r["user_agent"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    next_cursor = entries[-1]["created_at"] if len(entries) == limit else None
    return {"entries": entries, "next_cursor": next_cursor}
