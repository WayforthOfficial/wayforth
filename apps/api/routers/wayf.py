"""routers/wayf.py — $WAYF pre-TGE points endpoints."""
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from core.auth import _resolve_user
from core.db import get_db
from core.rate_limit import limiter
from wayf_points import AIRDROP_POOL_WAYF, DISCLAIMER, MONTHLY_POINTS_CAP, calculate_tge_allocation

router = APIRouter()


@router.get("/account/wayf-points")
@limiter.limit("60/minute")
async def get_wayf_points(request: Request, db=Depends(get_db)):
    api_key_header = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key_header:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key header required"})

    user_id, _api_key_id, _tier = await _resolve_user(db, api_key_header)

    if _tier == "free":
        raise HTTPException(status_code=403, detail={
            "error": "tier_required",
            "feature": "wayf_points",
            "your_tier": "free",
            "required_tier": "builder",
            "message": "WAYF points require builder tier or above. Free tier accounts earn no points.",
            "upgrade_url": "https://wayforth.io/pricing",
        })

    row = await db.fetchrow(
        """SELECT points_balance, points_earned_total, points_earned_this_month,
                  monthly_points_reset_at
           FROM wayf_points WHERE user_id = $1::uuid""",
        str(user_id),
    )

    points_balance = row["points_balance"] if row else 0
    points_earned_total = row["points_earned_total"] if row else 0
    points_earned_this_month = row["points_earned_this_month"] if row else 0
    reset_at = row["monthly_points_reset_at"] if row else None

    total_all = await db.fetchval("SELECT SUM(points_earned_total) FROM wayf_points") or 0

    estimated_wayf = calculate_tge_allocation(points_earned_total, int(total_all))
    your_share_pct = round((points_earned_total / int(total_all) * 100), 4) if total_all else 0.0

    recent_rows = await db.fetch(
        """SELECT points, reason, source, created_at
           FROM wayf_points_log
           WHERE user_id = $1::uuid
           ORDER BY created_at DESC LIMIT 10""",
        str(user_id),
    )

    cutoff = os.getenv("WAYF_POINTS_CUTOFF_DATE")
    earning_active = True
    if cutoff:
        cutoff_dt = datetime.fromisoformat(cutoff).replace(tzinfo=timezone.utc)
        earning_active = datetime.now(timezone.utc) < cutoff_dt

    return {
        "points_balance": points_balance,
        "points_earned_total": points_earned_total,
        "points_earned_this_month": points_earned_this_month,
        "monthly_cap": MONTHLY_POINTS_CAP,
        "monthly_remaining": max(0, MONTHLY_POINTS_CAP - points_earned_this_month),
        "resets_at": reset_at.date().isoformat() if reset_at else None,
        "earning_active": earning_active,
        "tge_estimate": {
            "your_points": points_earned_total,
            "total_all_users": int(total_all),
            "your_share_pct": your_share_pct,
            "estimated_wayf": round(estimated_wayf),
            "airdrop_pool": AIRDROP_POOL_WAYF,
            "note": (
                "Estimate based on current totals. Grows more accurate over time. "
                "Final allocation at TGE snapshot."
            ),
        },
        "recent_earnings": [
            {
                "points": r["points"],
                "reason": r["reason"],
                "source": r["source"],
                "earned_at": r["created_at"].isoformat(),
            }
            for r in recent_rows
        ],
        "disclaimer": DISCLAIMER,
    }


@router.get("/admin/wayf-points/totals")
@limiter.limit("30/minute")
async def admin_wayf_totals(request: Request, db=Depends(get_db)):
    admin_key = request.headers.get("X-Admin-Key", "")
    if not admin_key or admin_key != os.environ.get("ADMIN_KEY", ""):
        raise HTTPException(status_code=403, detail={"error": "forbidden"})

    total_users = await db.fetchval(
        "SELECT COUNT(*) FROM wayf_points WHERE points_earned_total > 0"
    ) or 0
    total_points = await db.fetchval(
        "SELECT SUM(points_earned_total) FROM wayf_points"
    ) or 0
    points_this_month = await db.fetchval(
        "SELECT SUM(points_earned_this_month) FROM wayf_points"
    ) or 0

    avg_wayf = round(AIRDROP_POOL_WAYF / int(total_users)) if total_users else 0

    cutoff = os.getenv("WAYF_POINTS_CUTOFF_DATE")
    earning_active = True
    if cutoff:
        cutoff_dt = datetime.fromisoformat(cutoff).replace(tzinfo=timezone.utc)
        earning_active = datetime.now(timezone.utc) < cutoff_dt

    return {
        "total_users_earning": int(total_users),
        "total_points_all_users": int(total_points),
        "airdrop_pool": AIRDROP_POOL_WAYF,
        "estimated_avg_wayf_per_user": avg_wayf,
        "points_awarded_this_month": int(points_this_month),
        "earning_active": earning_active,
        "cutoff_date": cutoff,
    }
