"""routers/billing/account.py — /account/* endpoints, /dashboard, /billing/balance, /billing/settings, /billing/permissions, /system/health, /billing/invoice."""

import hashlib
import logging
import math
import os
import random
import string
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from core.auth import resolve_dashboard_caller
from core.credits import PLANS, CREDITS_PER_CALL, ROUTING_FEE, PAYMENT_MULTIPLIERS, compute_calls_remaining
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier, _get_redis
from services.managed import SERVICE_DISPLAY_NAMES

logger = logging.getLogger("wayforth")

router = APIRouter()

# ── Constants ─────────────────────────────────────────────────────────────────

TIER_LIMITS = {
    "free":       {"rpm": 10,  "monthly": 1_000,    "fee_bps": 150},
    "starter":    {"rpm": 30,  "monthly": 5_000,    "fee_bps": 150},
    "builder":    {"rpm": 60,  "monthly": 20_000,   "fee_bps": 150},
    "pro":        {"rpm": 120, "monthly": 100_000,  "fee_bps": 150},
    "growth":     {"rpm": 300, "monthly": 500_000,  "fee_bps": 150},
    "enterprise": {"rpm": 500, "monthly": -1,       "fee_bps": 150},
}

_TIER_FEATURES = {
    "free":     {"execute_managed": True,  "byok": False, "analytics": False, "priority_support": False},
    "starter":  {"execute_managed": True,  "byok": True,  "analytics": False, "priority_support": False},
    "builder":  {"execute_managed": True,  "byok": True,  "analytics": True,  "priority_support": False},
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
        return "builder"
    if lifetime_credits >= 6_000:
        return "starter"
    return "free"


def _account_auth_key(request: Request):
    """Legacy API-key-only auth helper. Kept for endpoints that genuinely
    require the API key (not a cookie/JWT session). New /account/* endpoints
    should use `core.auth.resolve_dashboard_caller` instead, which accepts
    cookie, Bearer JWT, AND API key."""
    raw = request.headers.get("X-Wayforth-API-Key", "")
    if not raw:
        raise HTTPException(status_code=401, detail="API key required")
    return raw, hashlib.sha256(raw.encode()).hexdigest()


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/account/billing-permissions")
@limiter.limit("30/minute")
async def get_billing_permissions(request: Request, db=Depends(get_db)):
    """Return billing permission settings for the authenticated caller."""
    caller = await resolve_dashboard_caller(request, db)
    if not caller["api_key_id"]:
        # No active API key on this account → no billing permissions yet.
        return {
            "billing_permission": "none",
            "topup_trigger_calls": 100,
            "topup_amount_usd": 5.0,
            "monthly_topup_limit_usd": 20.0,
            "monthly_topup_spent_usd": 0.0,
            "monthly_topup_reset_at": None,
        }
    row = await db.fetchrow("""
        SELECT billing_permission, topup_trigger_calls, topup_amount_usd,
               monthly_topup_limit_usd, monthly_topup_spent_usd, monthly_topup_reset_at
        FROM api_keys
        WHERE id = $1::uuid
    """, str(caller["api_key_id"]))

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
    """Update billing permission settings for the authenticated caller."""
    caller = await resolve_dashboard_caller(request, db)
    if not caller["api_key_id"]:
        raise HTTPException(status_code=400, detail={
            "error": "no_active_api_key",
            "message": "Create or activate an API key before configuring billing permissions.",
        })
    key_record = await db.fetchrow(
        "SELECT id, topup_amount_usd, monthly_topup_limit_usd FROM api_keys WHERE id = $1::uuid",
        str(caller["api_key_id"]),
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
    # Session-OR-key (PR #25 pattern): the dashboard authenticates by wf_session
    # cookie. Resolve the caller, then read their primary active key + user row.
    caller = await resolve_dashboard_caller(request, db)
    if not caller.get("api_key_id"):
        raise HTTPException(status_code=404, detail="no_active_api_key")
    key = await db.fetchrow("""
        SELECT k.*, u.email, u.created_at as account_created,
               u.stripe_customer_id
        FROM api_keys k
        LEFT JOIN users u ON u.id = k.user_id
        WHERE k.id = $1
    """, caller["api_key_id"])

    if not key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # P13 (v0.7.8): replace `session_id ILIKE '%prefix%'` (full scan) with a
    # user_id filter. The composite idx_search_analytics_user_created (P3)
    # makes this O(log n). Trade-off: over-counts for users with multiple
    # api keys, but the dashboard is informational, not a billing source.
    # A true per-key count would require a schema change (api_key_id column
    # on search_analytics + backfill) — deferred to v0.8.x.
    searches_this_month = await db.fetchval("""
        SELECT COUNT(*) FROM search_analytics
        WHERE user_id = $1 AND created_at >= $2
    """, key['user_id'], month_start) or 0

    recent = await db.fetch("""
        SELECT query, created_at, top_result_id
        FROM search_analytics
        WHERE user_id = $1
          AND created_at > NOW() - INTERVAL '7 days'
        ORDER BY created_at DESC LIMIT 10
    """, key['user_id'])

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
    # PR #25 pattern: accept session-OR-key auth (resolve_dashboard_caller), not
    # key-only. The dashboard's balance widget authenticates by wf_session cookie
    # and has no raw API key to send; the old "API key required" check 401'd it,
    # and the UI fell back to a hardcoded balance (showed 100 while the account
    # was actually Growth/240k). Auth source for tier/credits is user_credits.
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

    # Billing-display fields (payment rail, reset dates, monthly usage) live on
    # the user's active api_key row. resolve_dashboard_caller already resolved the
    # primary key id (None only for the rare keyless account).
    key_record = None
    if caller.get("api_key_id"):
        key_record = await db.fetchrow("""
            SELECT k.payment_rail, k.quota_reset_at, k.subscription_expires_at,
                   k.monthly_calls_count, k.monthly_calls_reset_at
            FROM api_keys k
            WHERE k.id = $1 AND k.active = true
        """, caller["api_key_id"])

    def _kr(field, default=None):
        v = key_record[field] if key_record is not None else None
        return v if v is not None else default

    credits = await db.fetchrow(
        "SELECT credits_balance, pioneer_credits_balance, package_tier, payment_method FROM user_credits WHERE user_id = $1",
        user_id,
    )
    balance = credits["credits_balance"] if credits else 0
    pioneer_balance = credits["pioneer_credits_balance"] if credits else 0
    pkg_tier = credits["package_tier"] if credits else "free"
    payment_method = (credits["payment_method"] if credits else None) or "card"
    tier = _credits_to_tier(balance, pkg_tier)

    plan_def = PLANS.get(tier, PLANS["free"])
    resets_at = _kr("subscription_expires_at") or _kr("quota_reset_at")
    monthly_reset_at = _kr("monthly_calls_reset_at")
    payment_rail = _kr("payment_rail", "card")

    base_credits = plan_def["monthly_credits"]
    multiplier = PAYMENT_MULTIPLIERS.get(payment_method, 1.00)
    bonus_credits = math.floor(base_credits * (multiplier - 1.0))
    # credits_remaining reads from the authoritative credit ledger (user_credits.credits_balance),
    # not from monthly_calls_count which used to track raw call counts. monthly_calls_count now
    # tracks credits consumed this month (incremented by credit_cost per call).
    credits_remaining = balance  # balance already read from user_credits above

    # Forecast — daily average in credits (not calls). Returns null if < 3 days history.
    forecast = None
    monthly_credits_consumed = _kr("monthly_calls_count", 0) or 0
    if monthly_reset_at:
        now_utc = datetime.now(timezone.utc)
        period_start = monthly_reset_at.replace(tzinfo=timezone.utc) - timedelta(days=30)
        days_elapsed = max(1, (now_utc - period_start).days)
        days_until_reset = max(0, (monthly_reset_at.replace(tzinfo=timezone.utc) - now_utc).days)
        if days_elapsed >= 3 and monthly_credits_consumed > 0:
            daily_avg_credits = round(monthly_credits_consumed / days_elapsed, 2)
            if daily_avg_credits > 0:
                days_remaining = int(credits_remaining / daily_avg_credits)
                will_exhaust = days_remaining < days_until_reset
            else:
                days_remaining = "unlimited"
                will_exhaust = False
            projected = max(0, credits_remaining - int((daily_avg_credits or 0) * days_until_reset))
            forecast = {
                "daily_avg_credits": daily_avg_credits,
                "daily_avg_calls": daily_avg_credits,   # backward compat alias
                "days_remaining_at_current_rate": days_remaining,
                "projected_reset_balance": projected,
                "will_exhaust_before_reset": will_exhaust,
            }

    return {
        "plan": tier,
        "credits_remaining": credits_remaining,
        "pioneer_credits_remaining": pioneer_balance,
        "total_credits": credits_remaining + pioneer_balance,
        "credits_included": base_credits + bonus_credits,
        "calls_remaining": credits_remaining,         # backward compat
        "calls_included": base_credits + bonus_credits,  # backward compat
        "base_calls": base_credits,
        "bonus_calls": bonus_credits,
        "payment_method": payment_method,
        "payment_multiplier": multiplier,
        "resets_at": resets_at.isoformat() if resets_at else None,
        "payment_rail": payment_rail,
        "forecast": forecast,
    }


@router.get("/account/credits")
@limiter.limit("30/minute")
async def account_credits(request: Request, db=Depends(get_db)):
    """Current credit balance — canonical endpoint for dashboard and agents."""
    caller = await resolve_dashboard_caller(request, db)

    credits = await db.fetchrow(
        "SELECT credits_balance, lifetime_credits, package_tier FROM user_credits WHERE user_id = $1",
        caller["user_id"],
    )
    balance = credits['credits_balance'] if credits else 0
    lifetime = credits['lifetime_credits'] if credits else 0
    pkg_tier = credits['package_tier'] if credits else 'free'
    tier = _credits_to_tier(lifetime, pkg_tier)

    calls_remaining = (
        await compute_calls_remaining(db, str(caller["api_key_id"]))
        if caller["api_key_id"] else 0
    )

    return {
        "plan": tier,
        "credits_remaining": calls_remaining,
        "credits_included": PLANS.get(tier, PLANS["free"])["calls_included"],
        "calls_remaining": calls_remaining,   # backward compat
        "calls_included": PLANS.get(tier, PLANS["free"])["calls_included"],  # backward compat
        # Dashboard-only credit detail (not shown in public docs)
        "credits_remaining": balance,
        "credits_total": lifetime,
        "tier": tier,
        "email": caller["email"],
    }


@router.get("/account/tier")
@limiter.limit("30/minute")
async def account_tier(request: Request, db=Depends(get_db)):
    """Tier and feature flags — used by the dashboard to gate UI sections.

    Accepts wf_session cookie, Authorization: Bearer <supabase_jwt>, or
    X-Wayforth-API-Key. The dashboard relies on this to display the user's
    plan; before the cookie/Bearer paths were added, the post-/auth/me
    dashboard had no API key to send here and silently fell back to "Free".
    """
    caller = await resolve_dashboard_caller(request, db)
    credits = await db.fetchrow(
        "SELECT credits_balance, lifetime_credits, package_tier FROM user_credits WHERE user_id = $1",
        caller["user_id"],
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
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

    credits = await db.fetchrow(
        "SELECT credits_balance, lifetime_credits, package_tier FROM user_credits WHERE user_id = $1", user_id
    )
    tier = _credits_to_tier(credits["lifetime_credits"] or 0 if credits else 0, credits["package_tier"] if credits else None)
    require_tier(tier, "analytics")

    # ── Searches — source of truth: search_analytics table ──────────────────
    # P8 (v0.7.8): collapse three COUNTs into one filtered query — one round
    # trip instead of three. The idx_search_analytics_user_created composite
    # index (P3) keeps each FILTER cheap.
    search_counts = await db.fetchrow("""
        SELECT
            COUNT(*) FILTER (WHERE created_at >= date_trunc('month', NOW())) AS month,
            COUNT(*) FILTER (WHERE created_at >= date_trunc('day',   NOW())) AS today,
            COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days')  AS week
        FROM search_analytics
        WHERE user_id = $1
    """, user_id)
    searches_month = (search_counts["month"]  if search_counts else 0) or 0
    searches_today = (search_counts["today"]  if search_counts else 0) or 0
    searches_7d    = (search_counts["week"]   if search_counts else 0) or 0
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
    credits_included = plan_def["calls_included"]   # calls_included == monthly_credits after fix
    credits_used = caller["monthly_calls_count"] or 0
    credits_remaining = max(0, credits_included - credits_used)
    reset_at_str = (
        caller["monthly_calls_reset_at"].date().isoformat()
        if caller["monthly_calls_reset_at"] else reset.isoformat()
    )

    return {
        "searches": {
            "this_month": searches_month,
            "today": searches_today,
            "last_7_days": searches_7d,
        },
        # "api_calls" = actual HTTP executions (request count, legitimately "calls")
        "api_calls": {
            "this_month": exec_month,
            "by_endpoint": by_endpoint,
            "by_service": [{"service": r["service_id"], "request_count": r["count"]} for r in svc_rows],
        },
        # "executions" kept as a backward-compat alias
        "executions": {
            "this_month": exec_month,
            "by_endpoint": by_endpoint,
            "by_service": [{"service": r["service_id"], "count": r["count"]} for r in svc_rows],
        },
        # "credits" = monthly credit pool status (NOT call count)
        "credits": {
            "used":       credits_used,
            "included":   credits_included,
            "remaining":  credits_remaining,
            "resets_at":  reset_at_str,
        },
        # "calls" kept as a backward-compat alias (value identical to credits)
        "calls": {
            "used":       credits_used,
            "included":   credits_included,
            "remaining":  credits_remaining,
            "resets_at":  reset_at_str,
        },
        "top_queries": [{"query": r["query"], "count": r["count"]} for r in top_query_rows],
        "wri_scores": [
            {
                "service":          r["service"],
                "wri_score":        r["wri_score"],
                "ranking_version":  r["ranking_version"],
                "request_count":    r["calls"],      # renamed: was "calls"
                "calls":            r["calls"],      # backward compat
                "last_called":      r["last_called"],
            }
            for r in wri_score_entries
        ],
    }


@router.get("/account/usage/history")
@limiter.limit("30/minute")
async def account_usage_history(request: Request, db=Depends(get_db)):
    """30-day call history grouped by day and service. All tiers."""
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

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
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

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
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

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
    caller = await resolve_dashboard_caller(request, db)
    require_tier(caller["tier"], "account_agents")
    user_id = caller["user_id"]

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
    caller = await resolve_dashboard_caller(request, db)
    require_tier(caller["tier"], "account_agents")
    user_id = caller["user_id"]

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

    # Unauthenticated callers get only status + version — full internals are
    # gated behind a valid API key to prevent information disclosure.
    raw_key = request.headers.get("X-Wayforth-API-Key", "")
    _is_authed = False
    if raw_key:
        _key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        _key_row = await db.fetchrow(
            "SELECT id FROM api_keys WHERE key_hash = $1 AND active = TRUE",
            _key_hash,
        )
        _is_authed = bool(_key_row)
    if not _is_authed:
        return {"status": "ok", "version": VERSION}

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
        x402_count = 0  # non-critical: x402 service count for health dashboard display
    health["subsystems"]["x402"] = {
        "status": "configured" if cdp_configured else "not_configured",
        "facilitator": "Coinbase CDP",
        "cdp_credentials": "set" if cdp_configured else "missing",
        "services_supported": x402_count,
    }

    # Redis
    redis_url = os.environ.get("REDIS_URL", "")
    if not redis_url:
        health["subsystems"]["redis"] = {"status": "not_configured", "limiter": "memory"}
    else:
        try:
            _rc = _get_redis()
            if _rc is None:
                health["subsystems"]["redis"] = {"status": "degraded", "limiter": "fallback_memory"}
            else:
                await _rc.ping()
                health["subsystems"]["redis"] = {"status": "ok", "limiter": "redis"}
        except Exception:
            health["subsystems"]["redis"] = {"status": "degraded", "limiter": "fallback_memory"}

    health["latency_ms"] = round((_time.time() - start) * 1000)
    return health


# ── Feature 6: Invoice generation ────────────────────────────────────────────

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


@router.get("/billing/invoice/{year_month}")
@limiter.limit("10/minute")
async def get_invoice_alias(year_month: str, request: Request, db=Depends(get_db)):
    """Alias for /billing/invoice/{year}/{month} accepting YYYY-MM format."""
    try:
        year_s, month_s = year_month.split("-")
        year, month = int(year_s), int(month_s)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Use YYYY-MM format")
    return await get_invoice(year, month, request, db)


@router.get("/billing/invoice/{year}/{month}")
@limiter.limit("10/minute")
async def get_invoice(year: int, month: int, request: Request, db=Depends(get_db)):
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="invalid_month")
    # Session-OR-key (PR #25 pattern): the dashboard billing/invoice view uses
    # the wf_session cookie. Resolve the caller, then read their primary key row.
    caller = await resolve_dashboard_caller(request, db)
    if not caller.get("api_key_id"):
        raise HTTPException(status_code=404, detail="no_active_api_key")
    key_record = await db.fetchrow("""
        SELECT k.id, k.user_id, k.tier, u.email
        FROM api_keys k JOIN users u ON u.id = k.user_id
        WHERE k.id = $1 AND k.active = true
    """, caller["api_key_id"])
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    period_start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        period_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        period_end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    now_utc = datetime.now(timezone.utc)
    is_current_month = (year == now_utc.year and month == now_utc.month)

    # Credits used = the AUTHORITATIVE ledger (user_credits ⇄ credit_transactions),
    # summed over the period. Previously this read api_keys.monthly_calls_count for
    # the current month and COUNT(*) of transactions for past months — two non-
    # authoritative figures: monthly_calls_count is a denormalized per-key counter
    # maintained by a SEPARATE non-atomic path (_increment_calls) that drifts low
    # (it never counts /search debits — those carry NULL api_key_id — and the LLM
    # path under-increments), and COUNT(*) is a call count, not credits. Both
    # under-reported vs the real spend (e.g. June: 416/72 shown vs 861 actually
    # deducted). Summing -amount from the immutable ledger ties the invoice out to
    # the user_credits balance delta exactly — the only number real money may use.
    row = await db.fetchrow("""
        SELECT COALESCE(SUM(-amount), 0) AS credits_used
        FROM credit_transactions
        WHERE user_id = $1 AND amount < 0
          AND type IN ('execution', 'cross_rail', 'cloud_compute')
          AND created_at >= $2 AND created_at < $3
    """, key_record["user_id"], period_start, period_end)
    calls_used = int((row["credits_used"] or 0) if row else 0)
    if calls_used == 0:
        raise HTTPException(status_code=404, detail="no_activity_in_period")

    tier = key_record["tier"] or "free"
    plan_def = PLANS.get(tier, PLANS["free"])
    credits_row = await db.fetchrow(
        "SELECT payment_method FROM user_credits WHERE user_id = $1", key_record["user_id"]
    )
    payment_method = (credits_row["payment_method"] if credits_row else None) or "card"
    month_name = _MONTH_NAMES[month - 1]
    user_prefix = str(key_record["user_id"])[:6].upper()

    return {
        "invoice_id": f"WF-{year}-{month:02d}-{user_prefix}",
        "period": f"{month_name} {year}",
        "issued_to": key_record["email"],
        "plan": tier.capitalize(),
        "credits_included": plan_def["calls_included"],
        "credits_used": calls_used,
        "calls_included": plan_def["calls_included"],  # backward compat
        "calls_used": calls_used,  # backward compat
        "amount_usd": plan_def.get("price_usd", 0),
        "payment_method": payment_method,
        "status": "paid" if plan_def.get("price_usd", 0) > 0 else "unpaid",
        "line_items": [
            {
                "description": f"{tier.capitalize()} plan — {month_name} {year}",
                "amount": plan_def.get("price_usd", 0),
            }
        ],
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/account/alerts")
@limiter.limit("30/minute")
async def account_alerts(request: Request, db=Depends(get_db)):
    """Credit alert flags derived from the billing forecast."""
    caller = await resolve_dashboard_caller(request, db)
    credits = await db.fetchrow(
        "SELECT credits_balance, package_tier FROM user_credits WHERE user_id = $1",
        caller["user_id"],
    )
    balance = credits["credits_balance"] if credits else 0
    pkg_tier = credits["package_tier"] if credits else "free"
    tier = _credits_to_tier(balance, pkg_tier)

    calls_remaining = (
        await compute_calls_remaining(db, str(caller["api_key_id"]))
        if caller["api_key_id"] else 0
    )
    plan_def = PLANS.get(tier, PLANS["free"])
    calls_included = plan_def["calls_included"]

    threshold_80 = calls_included > 0 and calls_remaining <= math.floor(calls_included * 0.20)
    threshold_95 = calls_included > 0 and calls_remaining <= math.floor(calls_included * 0.05)

    will_exhaust = False
    days_remaining = None
    monthly_count = caller.get("monthly_calls_count") or 0
    monthly_reset_at = caller.get("monthly_calls_reset_at")
    if monthly_reset_at and monthly_count > 0:
        now_utc = datetime.now(timezone.utc)
        period_start = monthly_reset_at.replace(tzinfo=timezone.utc) - timedelta(days=30)
        days_elapsed = max(0, (now_utc - period_start).days)
        days_until_reset = max(0, (monthly_reset_at.replace(tzinfo=timezone.utc) - now_utc).days)
        if days_elapsed >= 3:
            daily_avg = monthly_count / days_elapsed
            if daily_avg > 0:
                days_remaining = int(calls_remaining / daily_avg)
                will_exhaust = days_remaining < days_until_reset

    return {
        "will_exhaust_before_reset": will_exhaust,
        "days_remaining": days_remaining,
        "threshold_80_pct": threshold_80,
        "threshold_95_pct": threshold_95,
        "credits_remaining": calls_remaining,
        "credits_included": calls_included,
        "calls_remaining": calls_remaining,   # backward compat
        "calls_included": calls_included,     # backward compat
    }


@router.get("/account/org")
@limiter.limit("30/minute")
async def account_org(request: Request, db=Depends(get_db)):
    """Alias for /org/members — returns the caller's org and member list."""
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]
    org = await db.fetchrow("""
        SELECT o.* FROM organizations o
        JOIN org_members m ON m.org_id = o.id
        WHERE m.user_id = $1::uuid
        ORDER BY m.joined_at
        LIMIT 1
    """, user_id)
    if not org:
        return {"org_id": None, "org_name": None, "members": []}

    rows = await db.fetch("""
        SELECT u.id, u.email, m.role, m.joined_at,
               ak.tier AS plan,
               ak.monthly_calls_count,
               COALESCE(uc.credits_balance, 0) AS credits_balance
        FROM org_members m
        JOIN users u ON u.id = m.user_id
        LEFT JOIN api_keys ak ON ak.user_id = u.id AND ak.active = true
        LEFT JOIN user_credits uc ON uc.user_id = u.id
        WHERE m.org_id = $1
        ORDER BY m.joined_at
    """, org["id"])
    return {
        "org_id": str(org["id"]),
        "org_name": org["name"],
        "members": [dict(r) for r in rows],
    }


FOUNDING_MEMBER_CUTOFF = "2026-08-31"


# ── Pioneer Developer Program ─────────────────────────────────────────────────

# Pioneer drip: tier-based credits awarded once per UTC day while enrolled
# (≈ each tier's monthly Pioneer allowance spread across ~30 days). Free tier is
# not eligible (0). Enterprise = 150k/mo ÷ 30.
_PIONEER_DAILY_CREDITS: dict[str, int] = {
    "starter":    30,
    "builder":    105,
    "pro":        360,
    "growth":     1_200,
    "enterprise": 5_000,
}

# Cooldown a developer must wait after leaving before they can rejoin.
_PIONEER_REJOIN_COOLDOWN = timedelta(days=7)


def _cooldown_days_remaining(cooldown_until, now) -> int:
    """Whole days (rounded up) until the cooldown expires; 0 if already past."""
    if not cooldown_until or cooldown_until <= now:
        return 0
    return max(1, math.ceil((cooldown_until - now).total_seconds() / 86400))


async def _resolve_pioneer_tier(db, user_id) -> str:
    key_row = await db.fetchrow(
        "SELECT tier FROM api_keys WHERE user_id = $1::uuid AND active = TRUE LIMIT 1",
        user_id,
    )
    return (key_row["tier"] if key_row else None) or "free"


@router.post("/account/pioneer/join", tags=["Account"])
@limiter.limit("10/minute")
async def pioneer_join(request: Request, db=Depends(get_db)):
    """Opt the authenticated developer into the Pioneer Program.

    Enrollment grants a tier-based daily credit drip (awarded by the drip job,
    not on this call). Re-joining is blocked until any rejoin cooldown — set when
    the developer last left — has elapsed.
    """
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    user = await db.fetchrow(
        "SELECT pioneer_opt_in, pioneer_cooldown_until FROM users WHERE id = $1::uuid",
        user_id,
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    now = datetime.now(timezone.utc)
    cooldown_until = user["pioneer_cooldown_until"]
    if cooldown_until and cooldown_until > now:
        raise HTTPException(status_code=429, detail={
            "error": "cooldown_active",
            "cooldown_until": cooldown_until.isoformat(),
            "days_remaining": _cooldown_days_remaining(cooldown_until, now),
            "message": "You recently left the Pioneer Program. You can rejoin after the cooldown ends.",
        })

    # Enroll. last_drip_date is left NULL so the next drip run awards the first
    # day's credits; no lump sum is granted on join.
    await db.execute("""
        UPDATE users
           SET pioneer_opt_in      = TRUE,
               pioneer_opted_in_at = COALESCE(pioneer_opted_in_at, $2),
               pioneer_opt_out_at  = NULL
         WHERE id = $1::uuid
    """, user_id, now)

    tier = await _resolve_pioneer_tier(db, user_id)
    daily = _PIONEER_DAILY_CREDITS.get(tier, 0)
    return {
        "opted_in":      True,
        "opted_in_at":   now.isoformat(),
        "daily_credits": daily,
        "tier":          tier,
        "cooldown_until": None,
        "message": (
            f"Welcome to the Pioneer Program! You'll receive {daily} bonus credits per day while enrolled."
            if daily > 0
            else "Welcome to the Pioneer Program! Your current tier is not eligible for daily credits — upgrade to start earning."
        ),
    }


@router.post("/account/pioneer/leave", tags=["Account"])
@limiter.limit("10/minute")
async def pioneer_leave(request: Request, db=Depends(get_db)):
    """Opt the authenticated developer out of the Pioneer Program.

    Credits already dripped are kept — no clawback. Routing reverts to normal
    WayforthRank immediately, the daily drip stops, and a 7-day rejoin cooldown
    is set (pioneer_last_drip_date is cleared so a future rejoin starts fresh).
    """
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    user = await db.fetchrow(
        "SELECT pioneer_opt_in FROM users WHERE id = $1::uuid", user_id
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user["pioneer_opt_in"]:
        raise HTTPException(status_code=409, detail={
            "error": "not_opted_in",
            "message": "You are not currently enrolled in the Pioneer Program.",
        })

    now = datetime.now(timezone.utc)
    cooldown_until = now + _PIONEER_REJOIN_COOLDOWN
    await db.execute("""
        UPDATE users
           SET pioneer_opt_in        = FALSE,
               pioneer_opt_out_at    = $2,
               pioneer_cooldown_until = $3,
               pioneer_last_drip_date = NULL
         WHERE id = $1::uuid
    """, user_id, now, cooldown_until)

    return {
        "opted_in":      False,
        "opted_out_at":  now.isoformat(),
        "cooldown_until": cooldown_until.isoformat(),
        "days_remaining": _cooldown_days_remaining(cooldown_until, now),
        "credits_kept":  True,
        "message": "You've left the Pioneer Program. Daily credit drips stop now; rejoin after the 7-day cooldown. Credits already received are kept.",
    }


@router.get("/account/pioneer/status", tags=["Account"])
@limiter.limit("30/minute")
async def pioneer_status(request: Request, db=Depends(get_db)):
    """Return Pioneer Program enrollment status and call statistics."""
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    user = await db.fetchrow(
        "SELECT pioneer_opt_in, pioneer_opted_in_at, pioneer_opt_out_at, "
        "pioneer_cooldown_until, pioneer_last_drip_date, "
        "pioneer_drip_credits_this_cycle, pioneer_drip_days_this_cycle "
        "FROM users WHERE id = $1::uuid",
        user_id,
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Count pioneer-routed searches and sum monthly drip credits.
    pioneer_calls_made = 0
    pioneer_calls_this_month = 0
    credits_earned_this_month = 0
    drip_days_this_cycle = 0
    try:
        pioneer_calls_made = await db.fetchval("""
            SELECT COUNT(DISTINCT sa.id)
              FROM search_analytics sa
              JOIN search_outcomes so ON so.query_id = sa.id
             WHERE sa.user_id = $1::uuid
               AND so.pioneer_routed = TRUE
               AND so.signal_weight < 1.0
        """, user_id) or 0

        pioneer_calls_this_month = await db.fetchval("""
            SELECT COUNT(DISTINCT sa.id)
              FROM search_analytics sa
              JOIN search_outcomes so ON so.query_id = sa.id
             WHERE sa.user_id = $1::uuid
               AND so.pioneer_routed = TRUE
               AND so.signal_weight < 1.0
               AND sa.created_at >= date_trunc('month', NOW())
        """, user_id) or 0

        # Sum of actual credits dripped this calendar month — this is what the
        # dashboard should display as "Credits earned this month", not the call
        # count above which was incorrectly used for that label.
        credits_earned_this_month = await db.fetchval("""
            SELECT COALESCE(SUM(amount), 0)
              FROM credit_transactions
             WHERE user_id = $1::uuid
               AND type = 'pioneer_drip'
               AND created_at >= date_trunc('month', NOW())
        """, user_id) or 0

        # Distinct Pacific calendar days dripped this cycle — DERIVED from the
        # immutable pioneer_drip ledger, not the denormalized
        # users.pioneer_drip_days_this_cycle column. The denormalized column can
        # orphan (it survived a full-account reset that wiped the pool + ledger,
        # leaving "15,600 / 13 days" with zero backing rows). Deriving from the
        # ledger means the figure can never disagree with the credits actually
        # dripped again.
        drip_days_this_cycle = await db.fetchval("""
            SELECT COUNT(DISTINCT (created_at AT TIME ZONE 'America/Los_Angeles')::date)
              FROM credit_transactions
             WHERE user_id = $1::uuid
               AND type = 'pioneer_drip'
               AND created_at >= date_trunc('month', NOW())
        """, user_id) or 0
    except Exception:
        pass  # non-critical: pioneer drip displays fall back to 0

    now = datetime.now(timezone.utc)
    tier = await _resolve_pioneer_tier(db, user_id)
    cooldown_until = user["pioneer_cooldown_until"]
    cooldown_active = bool(cooldown_until and cooldown_until > now)

    # Count providers whose boost is currently active (opted-in, not paused, not expired).
    active_boosted_providers = 0
    try:
        active_boosted_providers = await db.fetchval("""
            SELECT COUNT(DISTINCT p.id)
              FROM providers p
             WHERE p.boost_used    = TRUE
               AND p.boost_paused  = FALSE
               AND p.boost_expires_at > NOW()
        """) or 0
    except Exception:
        pass  # non-critical: active_boosted_providers display falls back to 0

    return {
        "opted_in":                              bool(user["pioneer_opt_in"]),
        "opted_in_at":                           user["pioneer_opted_in_at"].isoformat() if user["pioneer_opted_in_at"] else None,
        "opted_out_at":                          user["pioneer_opt_out_at"].isoformat() if user["pioneer_opt_out_at"] else None,
        "daily_credits":                         _PIONEER_DAILY_CREDITS.get(tier, 0),
        "tier":                                  tier,
        "last_drip_date":                        user["pioneer_last_drip_date"].isoformat() if user["pioneer_last_drip_date"] else None,
        "cooldown_until":                        cooldown_until.isoformat() if cooldown_active else None,
        # days until the rejoin cooldown expires (only non-null when cooldown_active).
        # Pioneer enrollment is INDEFINITE — there is no 30-day cap. This field
        # does NOT count down from 30; it only counts down after opting out.
        "cooldown_days_remaining":               _cooldown_days_remaining(cooldown_until, now) if cooldown_active else None,
        "days_remaining":                        _cooldown_days_remaining(cooldown_until, now) if cooldown_active else None,  # backward compat
        "credits_earned_this_month":             int(credits_earned_this_month),  # backward compat alias
        # Task 10 display fields
        "days_enrolled":                         int((now - user["pioneer_opted_in_at"]).days) if user["pioneer_opted_in_at"] else 0,
        # Derived from the pioneer_drip ledger (same source as credits_earned_this_month),
        # NOT the denormalized users.pioneer_drip_* columns which can orphan.
        "drip_credits_this_cycle":               int(credits_earned_this_month),
        "drip_days_this_cycle":                  int(drip_days_this_cycle),
        "daily_drip_rate":                       _PIONEER_DAILY_CREDITS.get(tier, 0),
        "rollover_note":                         "Credits reset with your monthly subscription. Unused credits do not carry over.",
        # Searches that were in the 60% boosted bucket AND had a boosted provider
        # promoted to the front. Not "API calls made" — pioneer_calls_* names kept
        # as backward-compat aliases.
        "pioneer_boosted_searches":              int(pioneer_calls_made),
        "pioneer_boosted_searches_this_month":   int(pioneer_calls_this_month),
        "pioneer_calls_made":                    int(pioneer_calls_made),         # backward compat
        "pioneer_calls_this_month":              int(pioneer_calls_this_month),   # backward compat
        # How many providers currently have an active boost window. When 0, the
        # 60% routing bucket is empty and pioneer_routing degrades gracefully to
        # normal WayforthRank order (signal_weight stays 1.0).
        "active_boosted_providers":              int(active_boosted_providers),
    }


async def run_pioneer_drip(db) -> int:
    """Award one calendar-day Pioneer drip to every opted-in user not yet dripped
    today. Uses Pacific time (UTC-7) as the calendar-day boundary so users in the
    western US don't get skipped when the job runs just after UTC midnight.

    TODO: swap CURRENT_DATE_PACIFIC to per-user tz once users.timezone is stored.

    Idempotent per day via a conditional claim UPDATE — a concurrent run sees
    last_drip_date == today and gets no row.
    """
    rows = await db.fetch("""
        SELECT u.id,
               COALESCE((SELECT tier FROM api_keys WHERE user_id = u.id AND active = TRUE LIMIT 1), 'free') AS tier
          FROM users u
         WHERE u.pioneer_opt_in = TRUE
           AND (
               u.pioneer_last_drip_date IS NULL
               OR u.pioneer_last_drip_date < (NOW() AT TIME ZONE 'America/Los_Angeles')::date
           )
    """)

    dripped = 0
    for r in rows:
        uid = r["id"]
        tier = r["tier"] or "free"
        daily = _PIONEER_DAILY_CREDITS.get(tier, 0)
        async with db.transaction():
            # Claim today's drip atomically using Pacific calendar date as the
            # idempotency key. last_drip_date is DATE (no tz), so we store the
            # Pacific date to match the boundary used in the eligibility check.
            pacific_today = await db.fetchval(
                "SELECT (NOW() AT TIME ZONE 'America/Los_Angeles')::date"
            )
            claimed = await db.fetchval("""
                UPDATE users SET pioneer_last_drip_date = $2
                 WHERE id = $1
                   AND pioneer_opt_in = TRUE
                   AND (pioneer_last_drip_date IS NULL OR pioneer_last_drip_date < $2)
                 RETURNING id
            """, uid, pacific_today)
            if not claimed:
                continue
            if daily > 0:
                # Drip lands in the separate pioneer overflow pool, not the main
                # credits_balance.
                cred = await db.fetchrow(
                    "SELECT pioneer_credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE", uid
                )
                current = cred["pioneer_credits_balance"] if cred else 0
                new_pioneer = current + daily
                await db.execute(
                    "UPDATE user_credits SET pioneer_credits_balance = $1, lifetime_credits = lifetime_credits + $2, updated_at = NOW() WHERE user_id = $3::uuid",
                    new_pioneer, daily, uid,
                )
                await db.execute("""
                    UPDATE users
                       SET pioneer_drip_credits_this_cycle = pioneer_drip_credits_this_cycle + $2
                     WHERE id = $1::uuid
                """, uid, daily)
                # balance_after records the pioneer pool after the drip.
                await db.execute("""
                    INSERT INTO credit_transactions
                      (user_id, amount, balance_after, type, description, api_endpoint)
                    VALUES ($1::uuid, $2, $3, 'pioneer_drip', $4, '/account/pioneer/drip')
                """, uid, daily, new_pioneer, f"pioneer_drip: {daily} credits ({tier})")
                # FIX 3: derive days from DISTINCT Pacific calendar dates dripped
                # this cycle (since last_credited_at) — robust against makeup /
                # out-of-band drips that would otherwise inflate a raw event count.
                # Runs after the INSERT so the current drip is included.
                await db.execute("""
                    UPDATE users
                       SET pioneer_drip_days_this_cycle = (
                           SELECT COUNT(DISTINCT (ct.created_at AT TIME ZONE 'America/Los_Angeles')::date)
                             FROM credit_transactions ct
                             JOIN user_credits uc ON uc.user_id = ct.user_id
                            WHERE ct.user_id = $1::uuid
                              AND ct.type = 'pioneer_drip'
                              AND ct.created_at >= COALESCE(uc.last_credited_at, '2000-01-01')
                       )
                     WHERE id = $1::uuid
                """, uid)
        dripped += 1
        logger.info("pioneer_drip_awarded user=%s credits=%s tier=%s", uid, daily, tier)

    return dripped


async def pioneer_drip_loop():
    """Background loop: run the Pioneer drip at startup (catch-up) and then once
    per day shortly after UTC midnight. Drip is idempotent per day, so the
    catch-up run on every boot is safe."""
    import asyncio
    from main import app
    while True:
        try:
            pool = getattr(app.state, "pool", None)
            if pool is not None:
                async with pool.acquire() as conn:
                    n = await run_pioneer_drip(conn)
                    if n:
                        logger.info("pioneer drip: %d user(s) dripped", n)
        except Exception as exc:
            logger.warning("pioneer drip loop error: %s", exc)
        now = datetime.now(timezone.utc)
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        await asyncio.sleep(max(60.0, (nxt - now).total_seconds()))


@router.get("/account/signal-summary", tags=["Account"])
@limiter.limit("30/minute")
async def account_signal_summary(request: Request, db=Depends(get_db)):
    """Aggregate signal intelligence for the authenticated account.

    Returns execution outcomes, failure breakdown, top services, substitution
    events, and average output length — all scoped to the current calendar month.
    """
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]

    # ── Core execution stats ──────────────────────────────────────────────────
    stats = await db.fetchrow("""
        SELECT
            COUNT(*)                                                          AS total,
            COUNT(*) FILTER (WHERE failure_code IS NULL)                     AS successes,
            COALESCE(SUM(ABS(amount)), 0)                                    AS credits_consumed,
            COUNT(*) FILTER (WHERE failure_code = 'timeout')                 AS fc_timeout,
            COUNT(*) FILTER (WHERE failure_code = 'rate_limit')              AS fc_rate_limit,
            COUNT(*) FILTER (WHERE failure_code = 'unavailable')             AS fc_unavailable,
            COUNT(*) FILTER (WHERE failure_code = 'auth')                    AS fc_auth,
            COUNT(*) FILTER (WHERE failure_code = 'parse_error')             AS fc_parse_error,
            COUNT(*) FILTER (WHERE substitution_from IS NOT NULL)            AS substitution_events,
            COALESCE(AVG(output_length_chars) FILTER (
                WHERE output_length_chars IS NOT NULL), 0)                   AS avg_output_len
        FROM credit_transactions
        WHERE user_id      = $1::uuid
          AND type         = 'execution'
          AND created_at  >= date_trunc('month', NOW())
    """, user_id)

    total = int(stats["total"]) if stats else 0
    successes = int(stats["successes"]) if stats else 0
    success_rate = round(successes / total, 4) if total > 0 else 1.0

    # ── Top services this month ───────────────────────────────────────────────
    svc_rows = await db.fetch("""
        SELECT
            service_id                                  AS slug,
            COUNT(*)                                    AS calls,
            COUNT(*) FILTER (WHERE failure_code IS NULL) AS ok
        FROM credit_transactions
        WHERE user_id     = $1::uuid
          AND type        = 'execution'
          AND service_id  IS NOT NULL
          AND created_at >= date_trunc('month', NOW())
        GROUP BY service_id
        ORDER BY calls DESC
        LIMIT 10
    """, user_id)

    top_services = [
        {
            "slug": r["slug"],
            "calls": int(r["calls"]),
            "success_rate": round(int(r["ok"]) / int(r["calls"]), 4) if r["calls"] else 1.0,
        }
        for r in svc_rows
    ]

    # ── Top substitution pairs ────────────────────────────────────────────────
    sub_rows = await db.fetch("""
        SELECT substitution_from AS "from", substitution_to AS "to", COUNT(*) AS count
        FROM credit_transactions
        WHERE user_id           = $1::uuid
          AND substitution_from IS NOT NULL
          AND substitution_to   IS NOT NULL
          AND created_at       >= date_trunc('month', NOW())
        GROUP BY substitution_from, substitution_to
        ORDER BY count DESC
        LIMIT 5
    """, user_id)

    return {
        "executions_this_month":    total,
        "success_rate":             success_rate,
        "credits_consumed":         int(stats["credits_consumed"]) if stats else 0,
        "failure_breakdown": {
            "timeout":     int(stats["fc_timeout"])    if stats else 0,
            "rate_limit":  int(stats["fc_rate_limit"]) if stats else 0,
            "unavailable": int(stats["fc_unavailable"])if stats else 0,
            "auth":        int(stats["fc_auth"])        if stats else 0,
            "parse_error": int(stats["fc_parse_error"])if stats else 0,
        },
        "top_services": top_services,
        "substitution_events": int(stats["substitution_events"]) if stats else 0,
        "top_substitution_pairs": [
            {"from": r["from"], "to": r["to"], "count": int(r["count"])}
            for r in sub_rows
        ],
        "avg_output_length_chars": int(stats["avg_output_len"]) if stats else 0,
    }


@router.get("/account/founding-status", tags=["Account"])
@limiter.limit("30/minute")
async def account_founding_status(request: Request, db=Depends(get_db)):
    """Return founding-member status and bonus grant state for the authenticated user."""
    caller = await resolve_dashboard_caller(request, db)

    user = await db.fetchrow(
        "SELECT founding_member, founding_bonus_granted_at FROM users WHERE id = $1::uuid",
        caller["user_id"],
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    return {
        "is_founding_member": bool(user["founding_member"]),
        "bonus_granted": user["founding_bonus_granted_at"] is not None,
        "bonus_amount": 500,
        "cutoff_date": FOUNDING_MEMBER_CUTOFF,
    }


# ── Self-serve account deletion (FINDING-016) ─────────────────────────────────

_DELETION_GRACE = timedelta(hours=24)


@router.delete("/account", tags=["Account"])
@limiter.limit("3/minute")
async def delete_account(request: Request, db=Depends(get_db)):
    """Schedule the caller's account for deletion after a 24h grace window.

    Requires an authenticated session/key AND an explicit confirmation in the
    body: {"confirm": "DELETE"}. Access is revoked immediately (API keys made
    inactive, sessions invalidated, Stripe subscription cancelled); the rows are
    hard-deleted by the reaper once the grace window elapses. The user can undo
    via POST /account/undelete during the window.
    """
    caller = await resolve_dashboard_caller(request, db)
    user_id = caller["user_id"]
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict) or body.get("confirm") != "DELETE":
        raise HTTPException(status_code=400, detail={
            "error": "confirmation_required",
            "message": 'Send {"confirm": "DELETE"} to schedule account deletion.',
        })

    scheduled_at = datetime.now(timezone.utc) + _DELETION_GRACE

    # Mark for deletion and immediately revoke access.
    async with db.transaction():
        await db.execute(
            "UPDATE users SET deletion_scheduled_at = $2 WHERE id = $1::uuid",
            user_id, scheduled_at,
        )
        await db.execute(
            "UPDATE api_keys SET active = FALSE WHERE user_id = $1::uuid",
            user_id,
        )

    # Best-effort: cancel any Stripe subscription so billing stops during grace.
    try:
        sub_rows = await db.fetch(
            "SELECT DISTINCT stripe_subscription_id FROM api_keys "
            "WHERE user_id = $1::uuid AND stripe_subscription_id IS NOT NULL",
            user_id,
        )
        if sub_rows:
            import stripe
            for r in sub_rows:
                try:
                    stripe.Subscription.delete(r["stripe_subscription_id"])
                except Exception as _se:
                    logger.warning("account delete: stripe cancel failed: %s", _se)
    except Exception as _e:
        logger.warning("account delete: stripe cancel sweep failed: %s", _e)

    # Best-effort: invalidate any active browser session for this user.
    try:
        from core.session import get_request_session_token, revoke_session
        redis = _get_redis()
        tok = get_request_session_token(request)
        if redis is not None and tok:
            await revoke_session(redis, tok)
    except Exception as _e:
        logger.warning("account delete: session revoke failed: %s", _e)

    # Confirmation email (best-effort).
    try:
        from core.email import send_email
        import asyncio as _aio
        if caller.get("email"):
            _aio.create_task(send_email(caller["email"], "account_deletion_scheduled", {
                "grace_hours": "24",
                "scheduled_at": scheduled_at.isoformat(),
            }))
    except Exception as _e:
        logger.warning("account delete: email failed: %s", _e)

    logger.info("account_deletion_scheduled user=%s at=%s", user_id, scheduled_at.isoformat())
    return {
        "status": "deletion_scheduled",
        "scheduled_at": scheduled_at.isoformat(),
        "grace_period_hours": 24,
        "message": "Your account is scheduled for deletion in 24 hours. "
                   "POST /account/undelete to cancel.",
    }


@router.post("/account/undelete", tags=["Account"])
@limiter.limit("5/minute")
async def undelete_account(request: Request, db=Depends(get_db)):
    """Cancel a pending account deletion within the grace window and reactivate keys."""
    # FINDING-106: this endpoint legitimately operates on a pending-deletion
    # account, so it must bypass the "account_scheduled_for_deletion" 403 guard.
    caller = await resolve_dashboard_caller(request, db, allow_pending_deletion=True)
    user_id = caller["user_id"]
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")

    row = await db.fetchrow(
        "SELECT deletion_scheduled_at FROM users WHERE id = $1::uuid", user_id,
    )
    if not row or row["deletion_scheduled_at"] is None:
        raise HTTPException(status_code=400, detail={"error": "no_pending_deletion"})

    async with db.transaction():
        await db.execute(
            "UPDATE users SET deletion_scheduled_at = NULL WHERE id = $1::uuid", user_id,
        )
        await db.execute(
            "UPDATE api_keys SET active = TRUE WHERE user_id = $1::uuid", user_id,
        )
    logger.info("account_deletion_cancelled user=%s", user_id)
    return {"status": "deletion_cancelled", "message": "Your account deletion has been cancelled."}


async def _purge_user(db, user_id) -> None:
    """Hard-delete one user and all dependents in FK-safe order, anonymizing
    financial records (kept for aggregate accounting, PII stripped)."""
    # FINDING-106: revoke any session minted during the grace window so it can't
    # outlive the account. Best-effort and outside the DB transaction.
    try:
        from core.session import revoke_all_user_sessions
        redis = _get_redis()
        if redis is not None:
            n = await revoke_all_user_sessions(redis, str(user_id))
            if n:
                logger.info("account_purge revoked %d session(s) user=%s", n, user_id)
    except Exception as _se:
        logger.warning("account_purge session revoke failed user=%s: %s", user_id, _se)
    async with db.transaction():
        # Anonymize financial history rather than delete (keep aggregates).
        await db.execute(
            "UPDATE credit_transactions SET description = 'redacted', api_endpoint = NULL, "
            "agent_id = NULL WHERE user_id = $1::uuid",
            user_id,
        )
        # Provider rows keyed by this user's emails (best-effort).
        emails = await db.fetch(
            "SELECT DISTINCT owner_email FROM api_keys WHERE user_id = $1::uuid AND owner_email IS NOT NULL",
            user_id,
        )
        for er in emails:
            prov = await db.fetchrow("SELECT id FROM providers WHERE email = $1", er["owner_email"])
            if prov:
                await db.execute("DELETE FROM provider_services WHERE provider_id = $1", prov["id"])
                await db.execute("DELETE FROM provider_webhooks WHERE contact_email = $1", er["owner_email"])
                await db.execute("DELETE FROM providers WHERE id = $1", prov["id"])
        # Dependent rows that don't already cascade.
        await db.execute("DELETE FROM api_keys WHERE user_id = $1::uuid", user_id)
        # user_credits has ON DELETE CASCADE on users; deleting the user removes it.
        await db.execute("DELETE FROM users WHERE id = $1::uuid", user_id)
    logger.info("account_purged user=%s", user_id)


async def _account_deletion_reaper():
    """Background task: hard-delete accounts whose 24h grace window has elapsed."""
    import asyncio as _aio
    from main import app
    await _aio.sleep(120)  # startup delay
    while True:
        try:
            pool = getattr(app.state, "pool", None)
            if pool:
                async with pool.acquire() as db:
                    due = await db.fetch(
                        "SELECT id FROM users WHERE deletion_scheduled_at IS NOT NULL "
                        "AND deletion_scheduled_at <= NOW() LIMIT 50",
                    )
                    for r in due:
                        try:
                            await _purge_user(db, r["id"])
                        except Exception as _pe:
                            logger.error("account reaper purge failed user=%s: %s", r["id"], _pe)
        except Exception as _e:
            logger.error("account deletion reaper error: %s", _e)
        await _aio.sleep(3600)  # hourly
