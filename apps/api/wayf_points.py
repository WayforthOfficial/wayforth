"""wayf_points.py — $WAYF pre-TGE loyalty points system."""
import json
import os
from datetime import datetime, timezone

SUBSCRIPTION_POINTS: dict[str, int] = {
    "builder": 50,
    "starter": 120,
    "pro":     400,
    "growth":  1200,
    "free":    0,
}

EXECUTIONS_PER_POINT = 10
DAILY_BONUS_POINTS = 2

MILESTONES: dict[str, int] = {
    "first_execution":         10,
    "first_run_call":          5,
    "first_byok_added":        5,
    "first_webhook":           5,
    "reached_100_executions":  20,
    "reached_1000_executions": 50,
}

MONTHLY_POINTS_CAP = 2000
AIRDROP_POOL_WAYF = 50_000_000

DISCLAIMER = (
    "$WAYF points are a pre-launch loyalty "
    "program. They are not tokens, securities, "
    "or financial instruments. Points may "
    "convert to $WAYF tokens at a future TGE "
    "subject to applicable law, regulatory "
    "approval, and Wayforth terms of service. "
    "Wayforth reserves the right to modify "
    "or terminate this program at any time. "
    "Free tier accounts earn no points."
)


def calculate_tge_allocation(user_points: int, total_all_users_points: int) -> float:
    """Proportional — NOT fixed rate. Total pool always = 50M $WAYF."""
    if total_all_users_points == 0:
        return 0.0
    return (user_points / total_all_users_points) * AIRDROP_POOL_WAYF


async def award_points(
    conn, user_id: str, api_key_id: str,
    tier: str, points: int, reason: str, source: str,
    metadata: dict | None = None,
) -> int:
    """Return actual points awarded (0 if capped, free tier, or past cutoff)."""
    if metadata is None:
        metadata = {}

    cutoff = os.getenv("WAYF_POINTS_CUTOFF_DATE")
    if cutoff:
        cutoff_dt = datetime.fromisoformat(cutoff).replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= cutoff_dt:
            return 0

    if tier == "free":
        return 0

    row = await conn.fetchrow(
        "SELECT points_earned_this_month, monthly_points_reset_at FROM wayf_points WHERE user_id = $1::uuid",
        user_id,
    )

    now = datetime.now(timezone.utc)
    current_month = row["points_earned_this_month"] if row else 0
    reset_at = row["monthly_points_reset_at"] if row else None

    if reset_at and now >= reset_at:
        await conn.execute(
            """UPDATE wayf_points
               SET points_earned_this_month = 0,
                   monthly_points_reset_at = date_trunc('month', NOW()) + INTERVAL '1 month'
               WHERE user_id = $1::uuid""",
            user_id,
        )
        current_month = 0

    remaining = max(0, MONTHLY_POINTS_CAP - current_month)
    actual = min(points, remaining)
    if actual <= 0:
        return 0

    await conn.execute(
        """
        INSERT INTO wayf_points (
            user_id, api_key_id,
            points_balance, points_earned_total, points_earned_this_month,
            monthly_points_reset_at
        ) VALUES (
            $1::uuid, $2::uuid, $3, $3, $3,
            date_trunc('month', NOW()) + INTERVAL '1 month'
        )
        ON CONFLICT (user_id) DO UPDATE SET
            points_balance             = wayf_points.points_balance + $3,
            points_earned_total        = wayf_points.points_earned_total + $3,
            points_earned_this_month   = wayf_points.points_earned_this_month + $3,
            monthly_points_reset_at    = COALESCE(
                wayf_points.monthly_points_reset_at,
                date_trunc('month', NOW()) + INTERVAL '1 month'
            ),
            updated_at = NOW()
        """,
        user_id, api_key_id, actual,
    )

    await conn.execute(
        """INSERT INTO wayf_points_log (user_id, api_key_id, points, reason, source, metadata)
           VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6)""",
        user_id, api_key_id, actual, reason, source, json.dumps(metadata),
    )

    return actual


async def check_milestones(
    conn, user_id: str, api_key_id: str, tier: str, calls_count: int
) -> None:
    """Award one-time milestone points based on cumulative call count."""
    earned_raw = await conn.fetchval(
        "SELECT array_agg(reason) FROM wayf_points_log WHERE user_id = $1::uuid AND source = 'milestone'",
        user_id,
    )
    earned = set(earned_raw or [])

    checks = [
        ("first_execution", 1),
        ("reached_100_executions", 100),
        ("reached_1000_executions", 1000),
    ]
    for key, threshold in checks:
        if key not in earned and calls_count >= threshold:
            await award_points(
                conn, user_id, api_key_id, tier,
                MILESTONES[key], key, "milestone",
                {"calls_at_milestone": calls_count},
            )
