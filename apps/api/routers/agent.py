"""routers/agent.py — x402 agent identity and legacy identity routes."""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth import _resolve_user
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier

logger = logging.getLogger("wayforth")

router = APIRouter()

# ── x402 agent identity helpers ───────────────────────────────────────────────

_X402_TIER_BADGES = {
    "unknown":     "⚪ New Agent",
    "emerging":    "🟢 Emerging Agent",
    "established": "🟡 Established Agent",
    "trusted":     "🔵 Trusted Agent",
    "elite":       "🏆 Elite Agent",
}


def _x402_tier(total_calls: int) -> str:
    if total_calls >= 500:
        return "elite"
    if total_calls >= 100:
        return "trusted"
    if total_calls >= 25:
        return "established"
    if total_calls >= 5:
        return "emerging"
    return "unknown"


def _x402_trust_score(total_calls: int, total_spent: float, first_seen) -> float:
    call_score = min(40.0, total_calls * 0.08)
    spend_score = min(30.0, total_spent * 3.0)
    reliability_score = 20.0
    if first_seen:
        age_days = (datetime.now(timezone.utc) - first_seen.replace(tzinfo=timezone.utc)).days
    else:
        age_days = 0
    age_score = min(10.0, age_days * 0.1)
    return round(call_score + spend_score + reliability_score + age_score, 2)


async def _upsert_x402_identity(pool, wallet_address: str, spend_usdc: float) -> dict:
    """Upsert x402 agent identity. Returns the agent_identity dict for the response."""
    if not pool or not wallet_address:
        return {}
    try:
        wallet = wallet_address.lower()
        async with pool.acquire() as db:
            row = await db.fetchrow("""
                INSERT INTO x402_agent_identities (wallet_address, total_calls, total_spent_usdc)
                VALUES ($1, 1, $2)
                ON CONFLICT (wallet_address) DO UPDATE
                SET total_calls = x402_agent_identities.total_calls + 1,
                    total_spent_usdc = x402_agent_identities.total_spent_usdc + $2,
                    last_seen = NOW()
                RETURNING total_calls, total_spent_usdc, first_seen
            """, wallet, spend_usdc)
            if not row:
                return {}
            total_calls = row["total_calls"]
            total_spent = float(row["total_spent_usdc"])
            tier = _x402_tier(total_calls)
            trust_score = _x402_trust_score(total_calls, total_spent, row["first_seen"])
            await db.execute(
                "UPDATE x402_agent_identities SET tier=$1, trust_score=$2 WHERE wallet_address=$3",
                tier, trust_score, wallet,
            )
        return {"wallet": wallet_address, "tier": tier, "trust_score": trust_score, "total_calls": total_calls}
    except Exception as exc:
        logger.warning("x402 identity upsert failed: %s", exc)
        return {}


# ── x402 wallet identity routes ───────────────────────────────────────────────

@router.get("/agent/identity/{wallet_address}", tags=["Agent Identity"])
async def agent_identity_lookup(wallet_address: str, db=Depends(get_db)):
    """Look up an x402 agent's identity and reputation by Base wallet address. No auth required."""
    wallet = wallet_address.lower()
    row = await db.fetchrow(
        "SELECT wallet_address, network, tier, trust_score, total_calls, "
        "total_spent_usdc, first_seen, last_seen FROM x402_agent_identities "
        "WHERE wallet_address = $1",
        wallet,
    )
    if not row:
        return {
            "wallet": wallet_address,
            "tier": "unknown",
            "trust_score": 0,
            "total_calls": 0,
            "message": "No activity recorded for this wallet on Wayforth.",
        }
    tier = row["tier"]
    return {
        "wallet": wallet_address,
        "network": row["network"],
        "tier": tier,
        "trust_score": float(row["trust_score"] or 0),
        "total_calls": row["total_calls"],
        "total_spent_usdc": f"{float(row['total_spent_usdc'] or 0):.6f}",
        "member_since": row["first_seen"].isoformat() if row["first_seen"] else None,
        "last_active": row["last_seen"].isoformat() if row["last_seen"] else None,
        "badge": _X402_TIER_BADGES.get(tier, "⚪ New Agent"),
    }


@router.get("/agent/leaderboard", tags=["Agent Identity"])
async def agent_leaderboard(limit: int = 20, db=Depends(get_db)):
    """Public leaderboard of top x402 agents by spend and call volume. No auth required."""
    limit = max(1, min(limit, 100))
    rows = await db.fetch("""
        SELECT wallet_address, tier, trust_score, total_calls, total_spent_usdc, first_seen
        FROM x402_agent_identities
        WHERE flagged = false
        ORDER BY total_spent_usdc DESC, total_calls DESC
        LIMIT $1
    """, limit)

    def _truncate_wallet(w: str) -> str:
        return f"{w[:6]}...{w[-4:]}" if len(w) >= 10 else w

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "agents": [
            {
                "rank": i + 1,
                "wallet": _truncate_wallet(r["wallet_address"]),
                "tier": r["tier"],
                "trust_score": float(r["trust_score"] or 0),
                "total_calls": r["total_calls"],
                "total_spent_usdc": f"{float(r['total_spent_usdc'] or 0):.6f}",
                "member_since": r["first_seen"].isoformat() if r["first_seen"] else None,
            }
            for i, r in enumerate(rows)
        ],
    }


# ── Legacy identity routes ────────────────────────────────────────────────────

class AgentIdentityRequest(BaseModel):
    agent_id: str
    display_name: str = ""


@router.post("/identity/register")
@limiter.limit("10/minute")
async def register_identity(request: Request, body: AgentIdentityRequest, db=Depends(get_db)):
    """Register an agent identity. Idempotent — safe to call multiple times."""
    _ident_key = request.headers.get("X-Wayforth-API-Key", "")
    if not _ident_key:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key required"})
    _, _, _ident_tier = await _resolve_user(db, _ident_key)
    require_tier(_ident_tier, "agent_identity")

    existing = await db.fetchrow("""
        SELECT id, trust_score, total_searches, total_payments
        FROM agent_identities WHERE agent_id = $1
    """, body.agent_id)

    if existing:
        return {
            "agent_id": body.agent_id,
            "status": "existing",
            "trust_score": existing["trust_score"],
            "total_searches": existing["total_searches"],
            "total_payments": existing["total_payments"],
            "message": "Identity already registered.",
        }

    await db.execute("""
        INSERT INTO agent_identities (agent_id, display_name, created_at, last_active_at)
        VALUES ($1, $2, NOW(), NOW())
    """, body.agent_id, body.display_name or body.agent_id[:12])

    return {
        "agent_id": body.agent_id,
        "status": "registered",
        "trust_score": 50.0,
        "message": "Identity registered. Trust score starts at 50 and improves with activity.",
    }


@router.get("/identity/{agent_id}")
@limiter.limit("30/minute")
async def get_identity(request: Request, agent_id: str, db=Depends(get_db)):
    """Get agent identity and reputation."""
    identity = await db.fetchrow("""
        SELECT agent_id, display_name, total_searches, total_payments,
               trust_score, created_at
        FROM agent_identities WHERE agent_id = $1
    """, agent_id)

    if not identity:
        raise HTTPException(status_code=404, detail="Agent identity not found. Register at POST /identity/register")

    trust = identity["trust_score"]
    if trust >= 90:
        tier = "elite"
    elif trust >= 75:
        tier = "trusted"
    elif trust >= 60:
        tier = "established"
    elif trust >= 40:
        tier = "new"
    else:
        tier = "unknown"

    return {
        "agent_id": identity["agent_id"],
        "display_name": identity["display_name"],
        "trust_score": identity["trust_score"],
        "reputation_tier": tier,
        "total_searches": identity["total_searches"],
        "total_payments": identity["total_payments"],
        "member_since": identity["created_at"].isoformat(),
    }


@router.get("/identity/{agent_id}/history")
@limiter.limit("20/minute")
async def identity_history(request: Request, agent_id: str, db=Depends(get_db)):
    """Agent's search and payment history."""
    searches = await db.fetch("""
        SELECT query, top_result_id, created_at
        FROM search_analytics
        WHERE session_id = $1
        ORDER BY created_at DESC LIMIT 20
    """, agent_id)

    payments = await db.fetch("""
        SELECT service_id, outcome_type, created_at
        FROM search_outcomes
        WHERE session_id = $1
        ORDER BY created_at DESC LIMIT 20
    """, agent_id)

    return {
        "agent_id": agent_id,
        "recent_searches": [dict(r) for r in searches],
        "recent_payments": [dict(r) for r in payments],
    }
