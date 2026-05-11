"""routers/billing/account.py — /account/* endpoints, /dashboard, /billing/balance, /billing/settings, /billing/permissions, /system/health."""

import hashlib
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from core.credits import PLANS, CREDITS_PER_CALL, ROUTING_FEE, compute_calls_remaining
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier, _get_redis
from services.managed import SERVICE_DISPLAY_NAMES

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

_TIER_FEATURES = {
    "free":     {"execute_managed": True,  "byok": False, "analytics": False, "priority_support": False},
    "builder":  {"execute_managed": True,  "byok": True,  "analytics": False, "priority_support": False},
    "starter":  {"execute_managed": True,  "byok": True,  "analytics": True,  "priority_support": False},
    "pro":      {"execute_managed": True,  "byok": True,  "analytics": True,  "priority_support": True},
    "growth":   {"execute_managed": True,  "byok": True,  "analytics": True,  "priority_support": True},
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _credits_to_tier(lifetime_credits: int, package_tier: str | None) -> str:
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


def _account_auth_key(request: Request):
    """Return (raw_key, key_hash) from X-Wayforth-API-Key header, or raise 401."""
    raw = request.headers.get("X-Wayforth-API-Key", "")
    if not raw:
        raise HTTPException(status_code=401, detail="API key required")
    return raw, hashlib.sha256(raw.encode()).hexdigest()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/account/billing-permissions")
@limiter.limit("30/minute")
async def get_billing_permissions(request: Request, db=Depends(get_db)):
    """Return billing permission settings for the authenticated API key."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    row = await db.fetchrow("""
        SELECT billing_permission, topup_trigger_calls, topup_amount_usd,
               monthly_topup_limit_usd, monthly_topup_spent_usd, monthly_topup_reset_at
        FROM api_keys
        WHERE key_hash = $1 AND active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not row:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return {
        "billing_permission": row["billing_permission"] or "none",
        "topup_trigger_calls": row["topup_trigger_calls"] or 100,
        "topup_amount_usd": float(row["topup_amount_usd"] or 5),
        "monthly_topup_limit_usd": float(row["monthly_topup_limit_usd"] or 20),
        "monthly_topup_spent_usd": float(row["monthly_topup_spent_usd"] or 0),
        "monthly_topup_reset_at": row["monthly_topup_reset_at"].date().isoformat() if row["monthly_topup_reset_at"] else None,
    }


@router.put("/account/billing-permissions")
@limiter.limit("10/minute")
async def put_billing_permissions(request: Request, db=Depends(get_db)):
    """Update billing permission settings for the authenticated API key."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow(
        "SELECT id, topup_amount_usd, monthly_topup_limit_usd FROM api_keys "
        "WHERE key_hash = $1 AND active = true",
        hashlib.sha256(api_key.encode()).hexdigest(),
    )
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    body = await request.json()
    updates: dict = {}

    if "billing_permission" in body:
        val = body["billing_permission"]
        if val not in ("none", "auto_topup", "full"):
            raise HTTPException(status_code=400, detail={
                "error": "billing_permission must be one of: none, auto_topup, full"
            })
        updates["billing_permission"] = val

    if "topup_trigger_calls" in body:
        val = int(body["topup_trigger_calls"])
        if not (50 <= val <= 1000):
            raise HTTPException(status_code=400, detail={
                "error": "topup_trigger_calls must be between 50 and 1000"
            })
        updates["topup_trigger_calls"] = val

    if "topup_amount_usd" in body:
        val = float(body["topup_amount_usd"])
        if not (5.0 <= val <= 100.0):
            raise HTTPException(status_code=400, detail={
                "error": "topup_amount_usd must be between 5.00 and 100.00"
            })
        updates["topup_amount_usd"] = val

    if "monthly_topup_limit_usd" in body:
        val = float(body["monthly_topup_limit_usd"])
        if not (5.0 <= val <= 500.0):
            raise HTTPException(status_code=400, detail={
                "error": "monthly_topup_limit_usd must be between 5.00 and 500.00"
            })
        effective_topup = updates.get("topup_amount_usd", float(key_record["topup_amount_usd"] or 5))
        if val < effective_topup:
            raise HTTPException(status_code=400, detail={
                "error": f"monthly_topup_limit_usd ({val:.2f}) must be >= topup_amount_usd ({effective_topup:.2f})"
            })
        updates["monthly_topup_limit_usd"] = val

    if not updates:
        raise HTTPException(status_code=400, detail={"error": "No valid fields provided"})

    set_parts = [f"{col} = ${i + 2}" for i, col in enumerate(updates)]
    await db.execute(
        f"UPDATE api_keys SET {', '.join(set_parts)} WHERE id = $1::uuid",
        str(key_record["id"]), *list(updates.values()),
    )

    return await get_billing_permissions(request, db)


@router.get("/dashboard")
@limiter.limit("30/minute")
async def dashboard(request: Request, db=Depends(get_db)):
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    key = await db.fetchrow("""
        SELECT k.*, u.email, u.created_at as account_created,
               u.stripe_customer_id
        FROM api_keys k
        LEFT JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1
    """, key_hash)

    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    searches_this_month = await db.fetchval("""
        SELECT COUNT(*) FROM search_analytics
        WHERE created_at >= $1
        AND session_id ILIKE $2
    """, month_start, f"%{key['key_prefix']}%") or 0

    recent = await db.fetch("""
        SELECT query, created_at, top_result_id
        FROM search_analytics
        WHERE created_at > NOW() - INTERVAL '7 days'
        ORDER BY created_at DESC LIMIT 10
    """)

    _fee = round(ROUTING_FEE * 100, 4)
    LIMITS = {
        'free':       {'rpm': 10,  'monthly': 1000,   'fee_pct': _fee},
        'starter':    {'rpm': 30,  'monthly': 10000,  'fee_pct': _fee},
        'pro':        {'rpm': 100, 'monthly': 100000, 'fee_pct': _fee},
        'enterprise': {'rpm': 500, 'monthly': -1,     'fee_pct': _fee},
    }
    tier = key['tier'] or 'free'
    limits = LIMITS.get(tier, LIMITS['free'])

    return {
        "account": {
            "email": key['email'],
            "tier": tier,
            "created_at": key['account_created'].isoformat() if key['account_created'] else None,
            "stripe_customer_id": key['stripe_customer_id'],
        },
        "api_key": {
            "prefix": key['key_prefix'],
            "created_at": key['created_at'].isoformat(),
            "subscription_status": key.get('subscription_status', 'active'),
            "current_period_end": key['current_period_end'].isoformat() if key.get('current_period_end') else None,
        },
        "usage": {
            "searches_this_month": searches_this_month,
            "monthly_limit": limits['monthly'],
            "pct_used": round((searches_this_month / limits['monthly'] * 100), 1) if limits['monthly'] > 0 else 0,
            "rate_limit_rpm": limits['rpm'],
        },
        "recent_searches": [
            {"query": r['query'], "at": r['created_at'].isoformat()}
            for r in recent
        ],
        "upgrade_url": "https://wayforth.io/pricing",
    }


@router.get("/billing/balance")
@limiter.limit("30/minute")
async def get_balance(request: Request, db=Depends(get_db)):
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow("""
        SELECT k.id, k.user_id, k.tier, k.payment_rail,
               k.quota_reset_at, k.subscription_expires_at
        FROM api_keys k
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    credits = await db.fetchrow(
        "SELECT credits_balance, package_tier FROM user_credits WHERE user_id = $1",
        key_record["user_id"],
    )
    balance = credits["credits_balance"] if credits else 0
    pkg_tier = credits["package_tier"] if credits else "free"
    tier = _credits_to_tier(balance, pkg_tier)

    plan_def = PLANS.get(tier, PLANS["free"])
    resets_at = key_record.get("subscription_expires_at") or key_record.get("quota_reset_at")
    payment_rail = key_record.get("payment_rail") or "card"

    return {
        "plan": tier,
        "calls_remaining": await compute_calls_remaining(db, str(key_record["id"])),
        "calls_included": plan_def["calls_included"],
        "resets_at": resets_at.isoformat() if resets_at else None,
        "payment_rail": payment_rail,
    }


@router.get("/account/credits")
@limiter.limit("30/minute")
async def account_credits(request: Request, db=Depends(get_db)):
    """Current credit balance — canonical endpoint for dashboard and agents."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow("""
        SELECT k.id, k.user_id, k.tier, u.email
        FROM api_keys k JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    credits = await db.fetchrow(
        "SELECT credits_balance, lifetime_credits, package_tier FROM user_credits WHERE user_id = $1",
        key_record['user_id']
    )
    balance = credits['credits_balance'] if credits else 0
    lifetime = credits['lifetime_credits'] if credits else 0
    pkg_tier = credits['package_tier'] if credits else 'free'
    tier = _credits_to_tier(lifetime, pkg_tier)

    return {
        "plan": tier,
        "calls_remaining": await compute_calls_remaining(db, str(key_record["id"])),
        "calls_included": PLANS.get(tier, PLANS["free"])["calls_included"],
        # Dashboard-only credit detail (not shown in public docs)
        "credits_remaining": balance,
        "credits_total": lifetime,
        "tier": tier,
        "email": key_record["email"],
    }


@router.get("/account/tier")
@limiter.limit("30/minute")
async def account_tier(request: Request, db=Depends(get_db)):
    """Tier and feature flags — used by the dashboard to gate UI sections."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow("""
        SELECT k.user_id
        FROM api_keys k
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    credits = await db.fetchrow(
        "SELECT credits_balance, lifetime_credits, package_tier FROM user_credits WHERE user_id = $1",
        key_record['user_id']
    )
    balance = credits['credits_balance'] if credits else 0
    lifetime = credits['lifetime_credits'] if credits else 0
    pkg_tier = credits['package_tier'] if credits else 'free'
    tier = _credits_to_tier(lifetime, pkg_tier)

    return {
        "tier": tier,
        "credits_remaining": balance,
        "credits_total": lifetime,
        "features": _TIER_FEATURES[tier],
    }


@router.get("/account/analytics")
@limiter.limit("30/minute")
async def account_analytics(request: Request, db=Depends(get_db)):
    """Per-user analytics — Pro and Growth tiers only."""
    import re as _re
    import datetime as _datetime
    raw_key, key_hash = _account_auth_key(request)
    key_record = await db.fetchrow(
        "SELECT k.user_id, k.id, k.monthly_calls_count, k.monthly_calls_reset_at, k.tier "
        "FROM api_keys k WHERE k.key_hash = $1 AND k.active = true", key_hash
    )
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")
    user_id = key_record["user_id"]

    credits = await db.fetchrow(
        "SELECT credits_balance, lifetime_credits, package_tier FROM user_credits WHERE user_id = $1", user_id
    )
    tier = _credits_to_tier(credits["lifetime_credits"] or 0 if credits else 0, credits["package_tier"] if credits else None)
    require_tier(tier, "analytics")

    # ── Searches — source of truth: search_analytics table ──────────────────
    searches_month = await db.fetchval(
        "SELECT COUNT(*) FROM search_analytics WHERE user_id=$1 AND created_at >= date_trunc('month', NOW())", user_id) or 0
    searches_today = await db.fetchval(
        "SELECT COUNT(*) FROM search_analytics WHERE user_id=$1 AND created_at >= date_trunc('day', NOW())", user_id) or 0
    searches_7d = await db.fetchval(
        "SELECT COUNT(*) FROM search_analytics WHERE user_id=$1 AND created_at >= NOW() - INTERVAL '7 days'", user_id) or 0
    top_query_rows = await db.fetch(
        "SELECT query, COUNT(*) as count FROM search_analytics "
        "WHERE user_id=$1 AND created_at >= date_trunc('month', NOW()) AND query IS NOT NULL "
        "GROUP BY query ORDER BY count DESC LIMIT 5", user_id)

    # ── Executions — source of truth: credit_transactions type='execution' ──
    exec_month = await db.fetchval(
        "SELECT COUNT(*) FROM credit_transactions WHERE user_id=$1 AND type='execution' "
        "AND created_at >= date_trunc('month', NOW())", user_id) or 0
    endpoint_rows = await db.fetch(
        "SELECT api_endpoint, COUNT(*) as count FROM credit_transactions "
        "WHERE user_id=$1 AND type='execution' AND created_at >= date_trunc('month', NOW()) "
        "AND api_endpoint IS NOT NULL GROUP BY api_endpoint ORDER BY count DESC", user_id)
    by_endpoint = {r["api_endpoint"].lstrip("/"): r["count"] for r in endpoint_rows}
    svc_rows = await db.fetch(
        "SELECT service_id, COUNT(*) as count FROM credit_transactions "
        "WHERE user_id=$1 AND type='execution' AND created_at >= date_trunc('month', NOW()) "
        "AND service_id IS NOT NULL GROUP BY service_id ORDER BY count DESC LIMIT 10", user_id)

    # ── Calls — source of truth: monthly_calls_count on api_keys ─────────────
    today = _datetime.date.today()
    if today.month == 12:
        reset = _datetime.date(today.year + 1, 1, 1)
    else:
        reset = _datetime.date(today.year, today.month + 1, 1)

    # WRI scores per service (from search→execute signal chain)
    wri_rows = await db.fetch("""
        SELECT
            clicked_slug AS service,
            ROUND(AVG(top_result_wri)) AS base_wri,
            COUNT(*) AS calls,
            MAX(created_at) AS last_called
        FROM search_analytics
        WHERE user_id = $1
          AND clicked_slug IS NOT NULL
          AND payment_followed = true
        GROUP BY clicked_slug
        ORDER BY calls DESC
    """, user_id)

    # Build v2 score lookup using same 3-tier matching as /admin/rank/recalculate
    def _slug_fn(name: str) -> str:
        return name.lower().replace(" ", "_").replace("-", "_").replace("/", "_")
    def _norm_fn(name: str) -> str:
        return _re.sub(r'[^a-z0-9]', '', name.lower())

    v2_all = await db.fetch("SELECT name, wri_score FROM services WHERE wri_version = 'v2' AND wri_score IS NOT NULL")
    svc_slug_map = {_slug_fn(s["name"]): s for s in v2_all}
    svc_norm_map = {_norm_fn(s["name"]): s for s in v2_all}

    def _find_v2(slug: str):
        key = slug.lower().replace("-", "_")
        svc = svc_slug_map.get(key)
        if not svc:
            for k, s in svc_slug_map.items():
                if k.startswith(key + "_"):
                    svc = s
                    break
        if not svc:
            nk = _norm_fn(slug)
            svc = svc_norm_map.get(nk)
        if not svc:
            nk = _norm_fn(slug)
            for k, s in svc_norm_map.items():
                if k.startswith(nk):
                    svc = s
                    break
        return svc

    wri_score_entries = []
    for r in wri_rows:
        v2 = _find_v2(r["service"])
        wri_score_entries.append({
            "service": r["service"],
            "wri_score": round(float(v2["wri_score"]), 1) if v2 else (int(r["base_wri"]) if r["base_wri"] is not None else None),
            "ranking_version": "v2" if v2 else "v1",
            "calls": r["calls"],
            "last_called": r["last_called"].isoformat() if r["last_called"] else None,
        })

    plan_tier = credits["package_tier"] if credits else "free"
    plan_def = PLANS.get(plan_tier, PLANS["free"])
    calls_included = plan_def["calls_included"]
    calls_used = key_record["monthly_calls_count"] or 0
    calls_remaining = max(0, calls_included - calls_used)

    return {
        "searches": {
            "this_month": searches_month,
            "today": searches_today,
            "last_7_days": searches_7d,
        },
        "executions": {
            "this_month": exec_month,
            "by_endpoint": by_endpoint,
            "by_service": [{"service": r["service_id"], "count": r["count"]} for r in svc_rows],
        },
        "calls": {
            "used": calls_used,
            "included": calls_included,
            "remaining": calls_remaining,
            "resets_at": (
                key_record["monthly_calls_reset_at"].date().isoformat()
                if key_record["monthly_calls_reset_at"] else reset.isoformat()
            ),
        },
        "top_queries": [{"query": r["query"], "count": r["count"]} for r in top_query_rows],
        "wri_scores": wri_score_entries,
    }


@router.get("/account/wayf-points/history")
@limiter.limit("30/minute")
async def account_wayf_points_history(request: Request, db=Depends(get_db)):
    """WAYF points earning history — last 50 daily buckets. All tiers."""
    from wayf_points import get_current_rate
    raw_key, key_hash = _account_auth_key(request)
    key_record = await db.fetchrow(
        "SELECT k.user_id, k.tier FROM api_keys k WHERE k.key_hash = $1 AND k.active = true",
        key_hash,
    )
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")
    user_id = key_record["user_id"]
    tier = key_record["tier"] or "free"

    rows = await db.fetch("""
        SELECT
            DATE(created_at AT TIME ZONE 'UTC') AS day,
            SUM(points)                         AS points_earned,
            MAX(rate_at_award)                  AS rate
        FROM wayf_points_log
        WHERE user_id = $1::uuid
        GROUP BY DATE(created_at AT TIME ZONE 'UTC')
        ORDER BY day DESC
        LIMIT 50
    """, user_id)

    if rows:
        oldest_day = min(r["day"] for r in rows)
        call_rows = await db.fetch("""
            SELECT
                DATE(created_at AT TIME ZONE 'UTC') AS day,
                COUNT(*) AS call_count
            FROM credit_transactions
            WHERE user_id = $1::uuid
              AND type IN ('execution', 'cross_rail')
              AND DATE(created_at AT TIME ZONE 'UTC') >= $2
            GROUP BY DATE(created_at AT TIME ZONE 'UTC')
        """, user_id, oldest_day)
        calls_by_day = {r["day"]: r["call_count"] for r in call_rows}
    else:
        calls_by_day = {}

    wp_row = await db.fetchrow(
        "SELECT points_earned_total FROM wayf_points WHERE user_id = $1::uuid", user_id
    )
    total_points = int(wp_row["points_earned_total"]) if wp_row else 0

    rate_info = await get_current_rate(db)
    current_rate_val = rate_info["points_per_wayf"]
    current_rate_str = f"{current_rate_val} pts = 1 WAYF"

    return {
        "history": [
            {
                "date": str(r["day"]),
                "calls_made": calls_by_day.get(r["day"], 0),
                "points_earned": int(r["points_earned"]),
                "rate": f"{r['rate']} pts = 1 WAYF" if r["rate"] else current_rate_str,
            }
            for r in rows
        ],
        "total_points": total_points,
        "current_rate": current_rate_str,
        "tier": tier,
    }


@router.get("/account/usage/history")
@limiter.limit("30/minute")
async def account_usage_history(request: Request, db=Depends(get_db)):
    """30-day call history grouped by day and service. All tiers."""
    raw_key, key_hash = _account_auth_key(request)
    key_record = await db.fetchrow(
        "SELECT k.user_id FROM api_keys k WHERE k.key_hash = $1 AND k.active = true", key_hash
    )
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")
    user_id = key_record["user_id"]

    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)

    rows = await db.fetch("""
        SELECT
            DATE(created_at AT TIME ZONE 'UTC') AS day,
            service_id                          AS service_slug,
            COUNT(*)                            AS call_count,
            ABS(SUM(amount))                    AS credits_used
        FROM credit_transactions
        WHERE user_id = $1::uuid
          AND type IN ('execution', 'cross_rail')
          AND created_at >= $2
        GROUP BY DATE(created_at AT TIME ZONE 'UTC'), service_id
        ORDER BY day DESC, call_count DESC
    """, user_id, thirty_days_ago)

    total_calls = sum(r["call_count"] for r in rows)
    total_credits = sum(int(r["credits_used"] or 0) for r in rows)

    return {
        "history": [
            {
                "date": str(r["day"]),
                "service_slug": r["service_slug"],
                "call_count": r["call_count"],
                "credits_used": int(r["credits_used"] or 0),
            }
            for r in rows
        ],
        "total_calls": total_calls,
        "total_credits": total_credits,
        "period": "30d",
    }


@router.get("/account/searches")
@limiter.limit("30/minute")
async def account_searches(request: Request, db=Depends(get_db)):
    """Authenticated user's own search history — all tiers."""
    raw_key, key_hash = _account_auth_key(request)
    key_record = await db.fetchrow(
        "SELECT k.user_id FROM api_keys k WHERE k.key_hash = $1 AND k.active = true", key_hash
    )
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")
    user_id = key_record["user_id"]

    rows = await db.fetch("""
        SELECT sa.query, sa.created_at, sa.result_count,
               s.name as top_result
        FROM search_analytics sa
        LEFT JOIN services s ON s.id = sa.top_result_id
        WHERE sa.user_id = $1
        ORDER BY sa.created_at DESC
        LIMIT 100
    """, user_id)
    total = await db.fetchval(
        "SELECT COUNT(*) FROM search_analytics WHERE user_id = $1", user_id) or 0

    return {
        "searches": [
            {
                "query": r["query"],
                "timestamp": r["created_at"].isoformat(),
                "results_count": r["result_count"] or 0,
                "top_result": r["top_result"],
            }
            for r in rows
        ],
        "total": total,
    }


@router.get("/account/executions")
@limiter.limit("30/minute")
async def account_executions(request: Request, db=Depends(get_db)):
    """Authenticated user's own execution history — all tiers."""
    raw_key, key_hash = _account_auth_key(request)
    key_record = await db.fetchrow(
        "SELECT k.user_id FROM api_keys k WHERE k.key_hash = $1 AND k.active = true", key_hash
    )
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")
    user_id = key_record["user_id"]

    rows = await db.fetch("""
        SELECT service_id, created_at, ABS(amount) as credits_used, type
        FROM credit_transactions
        WHERE user_id = $1 AND type IN ('execution', 'execution_refund')
        ORDER BY created_at DESC
        LIMIT 100
    """, user_id)
    total = await db.fetchval(
        "SELECT COUNT(*) FROM credit_transactions WHERE user_id=$1 AND type IN ('execution','execution_refund')", user_id) or 0

    return {
        "executions": [
            {
                "service": r["service_id"],
                "timestamp": r["created_at"].isoformat(),
                "credits_used": r["credits_used"],
                "status": "refunded" if r["type"] == "execution_refund" else "success",
            }
            for r in rows
        ],
        "total": total,
    }


@router.get("/account/agents", tags=["Account"])
@limiter.limit("30/minute")
async def account_agents(request: Request, db=Depends(get_db)):
    """Per-agent usage breakdown for the authenticated user."""
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key required"})
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_record = await db.fetchrow(
        "SELECT user_id, tier FROM api_keys WHERE key_hash=$1 AND active=true", key_hash
    )
    if not key_record:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})
    require_tier(key_record["tier"] or "free", "account_agents")
    user_id = key_record["user_id"]

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    rows = await db.fetch("""
        SELECT
            agent_id,
            COUNT(*) AS calls_total,
            COUNT(*) FILTER (WHERE created_at >= $2) AS calls_this_month,
            ABS(SUM(amount)) AS credits_used,
            MIN(created_at) AS first_seen,
            MAX(created_at) AS last_seen,
            ARRAY_AGG(DISTINCT service_id ORDER BY service_id) FILTER (WHERE service_id IS NOT NULL) AS services
        FROM credit_transactions
        WHERE user_id = $1::uuid
          AND agent_id IS NOT NULL
          AND type IN ('execution', 'cross_rail')
        GROUP BY agent_id
        ORDER BY calls_total DESC
    """, user_id, month_start)

    untagged = await db.fetchval("""
        SELECT COUNT(*) FROM credit_transactions
        WHERE user_id = $1::uuid
          AND agent_id IS NULL
          AND type IN ('execution', 'cross_rail')
          AND created_at >= $2
    """, user_id, month_start) or 0

    agents_out = []
    for row in rows:
        total_days = max(1, (row["last_seen"] - row["first_seen"]).days + 1) if row["first_seen"] and row["last_seen"] else 1
        top_svcs = (row["services"] or [])[:3]
        agents_out.append({
            "agent_id": row["agent_id"],
            "calls_total": row["calls_total"],
            "calls_this_month": row["calls_this_month"],
            "credits_used": int(row["credits_used"] or 0),
            "top_services": top_svcs,
            "first_seen": row["first_seen"].isoformat() if row["first_seen"] else None,
            "last_seen": row["last_seen"].isoformat() if row["last_seen"] else None,
            "avg_calls_per_day": round(row["calls_total"] / total_days, 1),
        })

    return {
        "period": "month",
        "from": month_start.isoformat(),
        "to": now.replace(hour=23, minute=59, second=59).isoformat(),
        "agents": agents_out,
        "untagged_calls": int(untagged),
        "total_agents": len(agents_out),
    }


@router.get("/account/agents/{agent_id}", tags=["Account"])
@limiter.limit("30/minute")
async def account_agent_detail(request: Request, agent_id: str, db=Depends(get_db)):
    """Detailed usage breakdown for a single agent_id."""
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    if not raw_key:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key required"})
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_record = await db.fetchrow(
        "SELECT user_id, tier FROM api_keys WHERE key_hash=$1 AND active=true", key_hash
    )
    if not key_record:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})
    require_tier(key_record["tier"] or "free", "account_agents")
    user_id = key_record["user_id"]

    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    thirty_days_ago = now - timedelta(days=30)

    summary_row = await db.fetchrow("""
        SELECT
            COUNT(*) AS calls_total,
            COUNT(*) FILTER (WHERE created_at >= $3) AS calls_this_month,
            ABS(SUM(amount)) AS credits_used_total,
            ABS(SUM(amount) FILTER (WHERE created_at >= $3)) AS credits_used_this_month,
            MIN(created_at) AS first_seen,
            MAX(created_at) AS last_seen
        FROM credit_transactions
        WHERE user_id = $1::uuid AND agent_id = $2
          AND type IN ('execution', 'cross_rail')
    """, user_id, agent_id, month_start)

    if not summary_row or not summary_row["calls_total"]:
        raise HTTPException(status_code=404, detail={
            "error": "agent_not_found",
            "agent_id": agent_id,
            "message": "No calls recorded for this agent_id under your account.",
        })

    # Per-service breakdown
    svc_rows = await db.fetch("""
        SELECT
            service_id AS service_slug,
            COUNT(*) AS calls,
            ABS(SUM(amount)) AS credits_used,
            MAX(created_at) AS last_used
        FROM credit_transactions
        WHERE user_id = $1::uuid AND agent_id = $2
          AND type IN ('execution', 'cross_rail')
          AND service_id IS NOT NULL
        GROUP BY service_id
        ORDER BY calls DESC
        LIMIT 10
    """, user_id, agent_id)

    # Daily usage — last 30 days
    daily_rows = await db.fetch("""
        SELECT
            DATE(created_at) AS day,
            COUNT(*) AS calls,
            ABS(SUM(amount)) AS credits_used
        FROM credit_transactions
        WHERE user_id = $1::uuid AND agent_id = $2
          AND type IN ('execution', 'cross_rail')
          AND created_at >= $3
        GROUP BY DATE(created_at)
        ORDER BY day DESC
    """, user_id, agent_id, thirty_days_ago)

    return {
        "agent_id": agent_id,
        "summary": {
            "calls_total": summary_row["calls_total"],
            "calls_this_month": summary_row["calls_this_month"],
            "credits_used_total": int(summary_row["credits_used_total"] or 0),
            "credits_used_this_month": int(summary_row["credits_used_this_month"] or 0),
            "first_seen": summary_row["first_seen"].isoformat() if summary_row["first_seen"] else None,
            "last_seen": summary_row["last_seen"].isoformat() if summary_row["last_seen"] else None,
        },
        "services_breakdown": [
            {
                "service_slug": r["service_slug"],
                "service_name": SERVICE_DISPLAY_NAMES.get(r["service_slug"], r["service_slug"]),
                "calls": r["calls"],
                "credits_used": int(r["credits_used"] or 0),
                "last_used": r["last_used"].isoformat() if r["last_used"] else None,
            }
            for r in svc_rows
        ],
        "daily_usage": [
            {
                "date": str(r["day"]),
                "calls": r["calls"],
                "credits_used": int(r["credits_used"] or 0),
            }
            for r in daily_rows
        ],
    }


@router.get("/system/health")
async def system_health(request: Request, db=Depends(get_db)):
    """Comprehensive health check for all payment tracks and subsystems."""
    import time as _time
    from main import app, VERSION
    from core.auth import get_fernet
    from routers.billing.stripe import STRIPE_MOCK as _STRIPE_MOCK
    start = _time.time()

    health = {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "version": VERSION,
        "subsystems": {},
    }

    # Database
    try:
        await db.fetchval("SELECT 1")
        health["subsystems"]["database"] = {"status": "ok"}
    except Exception as e:
        health["subsystems"]["database"] = {"status": "error", "detail": str(e)[:100]}
        health["status"] = "degraded"

    # Credits system
    try:
        count = await db.fetchval("SELECT COUNT(*) FROM user_credits")
        health["subsystems"]["credits"] = {"status": "ok", "accounts": count}
    except Exception as e:
        health["subsystems"]["credits"] = {"status": "error", "detail": str(e)[:100]}

    # BYOK
    try:
        key_count = await db.fetchval("SELECT COUNT(*) FROM user_service_keys WHERE active=true")
        health["subsystems"]["byok"] = {"status": "ok", "active_keys": key_count}
    except Exception as e:
        health["subsystems"]["byok"] = {"status": "error", "detail": str(e)[:100]}

    # Stripe
    stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
    health["subsystems"]["stripe"] = {
        "status": "mock" if _STRIPE_MOCK else "configured",
        "stripe_mode": "test" if stripe_key.startswith("sk_test_") else ("live" if stripe_key.startswith("sk_live_") else "not_set"),
    }

    # Payment tracks
    health["subsystems"]["payment_tracks"] = {
        "track_a_card": {
            "status": "mock" if _STRIPE_MOCK else "active",
            "processor": "Stripe Treasury",
            "credits_deduction": "active",
        },
        "track_b_crypto": {
            "status": "active",
            "network": "base-sepolia",
            "calldata_generation": "active",
            "escrow": "0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809",
        },
        "track_c_x402": {
            "status": "active" if (os.environ.get("CDP_API_KEY_NAME") and os.environ.get("CDP_API_KEY_PRIVATE_KEY")) else "fallback_to_card",
            "auto_detection": "active",
            "facilitator": "Coinbase CDP",
            "settlement": "live" if (os.environ.get("CDP_API_KEY_NAME") and os.environ.get("CDP_API_KEY_PRIVATE_KEY")) else "not_configured",
        },
    }

    # Services catalog
    try:
        total = await db.fetchval("SELECT COUNT(*) FROM services")
        tier2 = await db.fetchval("SELECT COUNT(*) FROM services WHERE coverage_tier >= 2")
        x402 = await db.fetchval("SELECT COUNT(*) FROM services WHERE x402_supported=true")
        health["subsystems"]["catalog"] = {
            "status": "ok",
            "total_services": total,
            "tier2_verified": tier2,
            "x402_native": x402,
        }
    except Exception as e:
        health["subsystems"]["catalog"] = {"status": "error", "detail": str(e)[:100]}

    # Encryption
    try:
        enc_key = os.environ.get("ENCRYPTION_KEY", "")
        if enc_key:
            f = get_fernet()
            token = f.encrypt(b"test").decode()
            f.decrypt(token.encode())
            health["subsystems"]["encryption"] = {"status": "ok", "algorithm": "Fernet-AES128"}
        else:
            health["subsystems"]["encryption"] = {
                "status": "not_configured",
                "note": "ENCRYPTION_KEY not set — BYOK key encryption unavailable",
            }
    except Exception as e:
        health["subsystems"]["encryption"] = {"status": "error", "detail": str(e)[:100]}

    # Managed services — derived from SERVICE_CONFIGS so this stays in sync automatically
    from services.managed import SERVICE_CONFIGS as _SERVICE_CONFIGS
    managed_key_vars = {slug: cfg["key_var"] for slug, cfg in _SERVICE_CONFIGS.items()}
    configured = [s for s, v in managed_key_vars.items() if os.environ.get(v)]
    missing = [s for s, v in managed_key_vars.items() if not os.environ.get(v)]
    health["subsystems"]["managed_services"] = {
        "status": "ok" if configured else "degraded",
        "configured": configured,
        "missing": missing,
    }

    # x402 / Coinbase CDP
    cdp_key_name = os.environ.get("CDP_API_KEY_NAME", "")
    cdp_private_key = os.environ.get("CDP_API_KEY_PRIVATE_KEY", "")
    cdp_configured = bool(cdp_key_name and cdp_private_key)
    try:
        x402_count = await db.fetchval("SELECT COUNT(*) FROM services WHERE x402_supported=true")
    except Exception:
        x402_count = 0
    health["subsystems"]["x402"] = {
        "status": "configured" if cdp_configured else "not_configured",
        "facilitator": "Coinbase CDP",
        "cdp_credentials": "set" if cdp_configured else "missing",
        "services_supported": x402_count,
    }

    # Redis
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        health["subsystems"]["redis"] = {"status": "not_configured", "mode": "memory"}
    else:
        try:
            _rc = _get_redis()
            if _rc is None:
                health["subsystems"]["redis"] = {"status": "degraded", "mode": "fallback_memory"}
            else:
                await _rc.ping()
                health["subsystems"]["redis"] = {"status": "ok", "mode": "redis"}
        except Exception:
            health["subsystems"]["redis"] = {"status": "degraded", "mode": "fallback_memory"}

    health["latency_ms"] = round((_time.time() - start) * 1000)
    return health
