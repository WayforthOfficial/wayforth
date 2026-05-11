"""routers/admin/dashboard.py — /admin-api/* routes and get_admin_session helper."""

import asyncio
import bcrypt
import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from core.credits import PLANS, _dispatch_webhooks
from core.db import get_db
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
    from main import ADMIN_KEY
    # X-Admin-Key grants full ceo-level access without a JWT session
    admin_key = request.headers.get("X-Admin-Key", "")
    if admin_key and ADMIN_KEY and secrets.compare_digest(admin_key, ADMIN_KEY):
        return {"role": "ceo", "email": "admin", "full_name": "Admin", "is_active": True,
                "admin_user_id": None}

    token = request.headers.get("X-Admin-Token", "")
    if not token:
        raise HTTPException(status_code=401, detail="Admin token required")

    token_hash = hashlib.sha256(token.encode()).hexdigest()

    session = await db.fetchrow("""
        SELECT s.*, u.email, u.role, u.full_name, u.is_active
        FROM admin_sessions s
        JOIN admin_users u ON u.id = s.admin_user_id
        WHERE s.token_hash = $1 AND s.expires_at > NOW()
    """, token_hash)

    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    if not session['is_active']:
        raise HTTPException(status_code=403, detail="Account deactivated")

    return dict(session)


@router.post("/admin-api/auth/login")
@limiter.limit("10/minute")
async def admin_login(request: Request, db=Depends(get_db)):
    body = await request.json()
    email = body.get("email", "").lower().strip()
    password = body.get("password", "")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    user = await db.fetchrow(
        "SELECT * FROM admin_users WHERE email = $1 AND is_active = true", email
    )

    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not bcrypt.checkpw(password.encode(), user['password_hash'].encode()):
        raise HTTPException(status_code=401, detail="Invalid credentials")

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

    try:
        member = await db.fetchrow("""
            INSERT INTO admin_users (email, password_hash, full_name, role, created_by)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, email, full_name, role, created_at
        """, email, password_hash, full_name, role,
            session.get('admin_user_id'))
        return {"member": dict(member), "temp_password": temp_password}
    except Exception:
        raise HTTPException(status_code=400, detail="Email already exists")


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


@router.get("/admin-api/overview")
async def admin_overview(request: Request, db=Depends(get_db)):
    session = await get_admin_session(request, db)

    try:
        total_services = await db.fetchval("SELECT COUNT(*) FROM services") or 0
    except: total_services = 0

    try:
        tier2 = await db.fetchval("SELECT COUNT(*) FROM services WHERE coverage_tier >= 2") or 0
    except: tier2 = 0

    try:
        total_users = await db.fetchval("SELECT COUNT(*) FROM users") or 0
    except: total_users = 0

    try:
        total_keys = await db.fetchval("SELECT COUNT(*) FROM api_keys") or 0
    except: total_keys = 0

    try:
        searches_24h = await db.fetchval(
            "SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '24h'"
        ) or 0
    except: searches_24h = 0

    try:
        searches_7d = await db.fetchval(
            "SELECT COUNT(*) FROM search_analytics WHERE created_at > NOW() - INTERVAL '7 days'"
        ) or 0
    except: searches_7d = 0

    try:
        pending_tier3 = await db.fetchval(
            "SELECT COUNT(*) FROM tier3_applications WHERE kyb_status = 'pending'"
        ) or 0
    except: pending_tier3 = 0

    try:
        total_agents = await db.fetchval("SELECT COUNT(*) FROM agent_identities") or 0
    except: total_agents = 0

    try:
        daily = await db.fetch("""
            SELECT DATE(created_at) as date, COUNT(*) as count
            FROM search_analytics
            WHERE created_at > NOW() - INTERVAL '30 days'
            GROUP BY DATE(created_at)
            ORDER BY date ASC
        """)
    except: daily = []

    try:
        signups = await db.fetch("""
            SELECT DATE(created_at) as date, COUNT(*) as count
            FROM users
            WHERE created_at > NOW() - INTERVAL '30 days'
            GROUP BY DATE(created_at)
            ORDER BY date ASC
        """)
    except: signups = []

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

    searches = await db.fetch("""
        SELECT query, created_at, top_result_id
        FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '30 days'
        ORDER BY created_at DESC LIMIT 10
    """)

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
    body = await request.json()
    new_tier = body.get("tier")
    reason = body.get("reason", "Admin manual change")

    VALID_TIERS = list(PLANS.keys())  # ['free', 'builder', 'starter', 'pro', 'growth']
    if new_tier not in VALID_TIERS:
        raise HTTPException(status_code=400, detail=f"Invalid tier. Valid: {VALID_TIERS}")

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
    body = await request.json()
    reason = body.get("reason", "Admin reset")

    await db.execute("""
        UPDATE api_keys SET usage_this_month = 0, quota_reset_at = NOW()
        WHERE user_id = $1::uuid
    """, user_id)

    return {"status": "reset", "changed_by": session['email'], "reason": reason}


@router.post("/admin-api/users/{user_id}/add-credits")
async def admin_add_credits(request: Request, user_id: str, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    body = await request.json()
    credits = int(body.get("credits", 0))
    reason = body.get("reason", "Admin grant")
    payment_method = body.get("payment_method", "admin")

    if credits <= 0 or credits > 1000000:
        raise HTTPException(status_code=400, detail="Credits must be 1-1,000,000")

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
    body = await request.json()
    reason = body.get("reason", "Admin revoked")

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
    body = await request.json()
    suspended = body.get("suspended", True)
    reason = body.get("reason", "")

    await db.execute("""
        UPDATE api_keys SET active = $1 WHERE user_id = $2::uuid
    """, not suspended, user_id)

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

    await db.execute("""
        UPDATE api_keys SET monthly_quota = $1 WHERE user_id = $2::uuid
    """, quota, user_id)

    return {"status": "quota_set", "quota": quota, "changed_by": session['email'], "reason": reason}


@router.get("/admin-api/users/{user_id}/searches")
async def admin_user_searches(request: Request, user_id: str, limit: int = 50, db=Depends(get_db)):
    session = await get_admin_session(request, db)
    if session['role'] not in ['ceo', 'support']:
        raise HTTPException(status_code=403)

    key = await db.fetchrow("SELECT key_prefix FROM api_keys WHERE user_id = $1::uuid", user_id)
    if not key:
        return {"searches": [], "total": 0}

    searches = await db.fetch("""
        SELECT query, created_at, top_result_id, led_to_payment
        FROM search_analytics
        ORDER BY created_at DESC LIMIT $1
    """, limit)

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
