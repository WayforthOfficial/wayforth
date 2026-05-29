"""routers/search/query.py — /query (WayforthQL), /memory, /tier3/*."""

import asyncio
import hashlib
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Literal

from core.auth import _resolve_user, check_auth, _ANON_DAILY_LIMIT
from core.credits import CREDIT_COSTS, check_and_deduct_credits
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier, check_rate_limit
from services.wayforthrank import compute_wri

logger = logging.getLogger("wayforth")

router = APIRouter()


# ── Models ────────────────────────────────────────────────────────────────────

class WayforthQLQuery(BaseModel):
    query: str
    tier_min: int | None = 2
    price_max: float | None = None
    uptime_min: float | None = None  # reserved — no column yet
    category: str | None = None
    protocol: str | None = None       # 'wayforth' | 'any'
    exclude_ids: list[str] | None = []  # service_id SHA256 hashes to exclude
    sort_by: Literal["wri", "score", "price", "tier"] | None = "wri"
    limit: int | None = 5
    with_similar: bool | None = False  # include similar services for top result
    x402_only: bool = False            # only x402-native services
    provider: str | None = None        # filter by provider name substring
    verified_only: bool = False        # only tier-2+ verified services
    offset: int = 0                    # pagination offset
    # v1.1 filter fields
    latency_max: int | None = None                          # max avg response time in ms
    region: Literal["us", "eu", "global"] | None = None    # service region
    payment_rail: Literal["card", "usdc", "x402"] | None = None  # required payment rail


class MemoryItem(BaseModel):
    service_id: str
    service_name: str
    note: str = ""
    agent_id: str = ""


class Tier3Application(BaseModel):
    service_name: str
    company_name: str
    contact_email: str
    website: str = ""
    endpoint_url: str
    monthly_volume_usd: float = 0.0
    sla_uptime_target: float = 99.9


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/query")
async def wayforthql(request: Request, body: WayforthQLQuery, auth: dict = Depends(check_auth), db=Depends(get_db)):
    """WayforthQL — declarative query language for agent service discovery."""
    from ranker_client import rank_services
    from core.auth import _ANON_DAILY_LIMIT

    if len(body.query) > 500:
        raise HTTPException(status_code=400, detail={"error": "query_too_long", "max_length": 500})

    if body.latency_max is not None and not (1 <= body.latency_max <= 60000):
        raise HTTPException(status_code=422, detail={
            "error": "invalid_latency_max",
            "message": "latency_max must be between 1 and 60000 ms.",
        })

    effective_limit = body.limit if body.limit is not None else 5
    if not (1 <= effective_limit <= 50):
        raise HTTPException(status_code=422, detail={
            "error": "invalid_limit",
            "message": "limit must be between 1 and 50.",
        })

    effective_offset = body.offset if body.offset is not None else 0
    if effective_offset < 0:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_offset",
            "message": "offset must be >= 0.",
        })

    require_tier(auth.get("tier") or "free", "wayforthql")
    await check_rate_limit(auth["key_id"], auth["tier"])
    if auth.get("authenticated") and auth.get("user_id"):
        success, balance = await check_and_deduct_credits(
            db, auth["user_id"], CREDIT_COSTS["query"], "/query"
        )
        if not success:
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "insufficient_credits",
                    "message": "You've run out of credits. Top up to continue.",
                    "balance": balance,
                    "required": CREDIT_COSTS["query"],
                    "top_up_url": "https://wayforth.io/dashboard/billing",
                    "packages_url": "https://wayforth.io/pricing",
                }
            )

    conditions = ["coverage_tier >= $1", "source != 'demo'"]
    params: list = [body.tier_min if body.tier_min is not None else 0]
    idx = 2

    if body.price_max is not None:
        conditions.append(f"(pricing_usdc IS NULL OR pricing_usdc <= ${idx})")
        params.append(body.price_max)
        idx += 1

    if body.category:
        conditions.append(f"category = ${idx}")
        params.append(body.category)
        idx += 1

    if body.protocol and body.protocol != "any":
        conditions.append(f"payment_protocol = ${idx}")
        params.append(body.protocol)
        idx += 1

    if body.x402_only:
        conditions.append("x402_supported = true")

    if body.verified_only:
        conditions.append("coverage_tier >= 2")

    if body.provider:
        conditions.append(f"LOWER(name) LIKE ${idx}")
        params.append(f"%{body.provider.lower()}%")
        idx += 1

    if body.latency_max is not None:
        conditions.append(f"(avg_latency_ms IS NULL OR avg_latency_ms <= ${idx})")
        params.append(float(body.latency_max))
        idx += 1

    if body.region is not None:
        conditions.append(f"(region IS NULL OR region = ${idx})")
        params.append(body.region)
        idx += 1

    if body.payment_rail == "x402":
        conditions.append("x402_supported = true")
    # card and usdc are supported for all services via Wayforth routing layers

    where = " AND ".join(conditions)
    limit = effective_limit
    offset = effective_offset

    fetch_n = (offset + limit) * 4
    try:
        total_results = await db.fetchval(
            f"SELECT COUNT(*) FROM services WHERE {where}", *params
        ) or 0
        rows = await db.fetch(
            f"""
            SELECT id, name, slug, description, endpoint_url, category,
                   pricing_usdc, coverage_tier, source, payment_protocol,
                   last_tested_at, consecutive_failures, x402_supported
            FROM services
            WHERE {where}
            ORDER BY coverage_tier DESC
            LIMIT {fetch_n}
            """,
            *params,
        )
    except Exception as e:
        logger.error(f"DB error in /query: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")

    if not rows:
        return {
            "query": body.query, "results": [], "total": 0,
            "total_results": 0, "offset": offset, "protocol": "WayforthQL/2.0",
        }

    candidates = [dict(r) for r in rows]
    try:
        ranked = await rank_services(body.query, candidates)
    except Exception as _re:
        logger.error("query ranker error: %s", _re)
        raise HTTPException(status_code=503, detail={"error": "ranker_unavailable"})

    # Secondary sort before slicing
    if body.sort_by == "price":
        ranked.sort(key=lambda s: (s.get("pricing_usdc") is None, s.get("pricing_usdc") or 0))
    elif body.sort_by == "tier":
        ranked.sort(key=lambda s: s.get("coverage_tier", 0), reverse=True)

    # Exclude specific service IDs
    if body.exclude_ids:
        exclude_set = set(body.exclude_ids)
        ranked = [
            s for s in ranked
            if ("0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest()) not in exclude_set
        ]

    results_raw = ranked[offset:offset + limit]

    results = []
    for s in results_raw:
        service_id = "0x" + hashlib.sha256(s.get("endpoint_url", "").encode()).hexdigest()
        name_slug = s.get("slug") or s.get("name", "").lower().replace(" ", "_").replace("/", "_")[:30]
        entry = {
            "name": s.get("name"),
            "slug": s.get("slug"),
            "score": s.get("score", 0),
            "wri": compute_wri(s, s.get("score", 0)),
            "reason": s.get("reason", ""),
            "coverage_tier": s.get("coverage_tier"),
            "category": s.get("category"),
            "endpoint_url": s.get("endpoint_url"),
            "pricing": {
                "per_call_usd": s.get("pricing_usdc"),
            },
            "service_id": service_id,
            "wayforth_id": f"wayforth://{name_slug}/{service_id[2:10]}",
            "payment_options": {
                "track_a": {
                    "method": "card",
                    "processor": "Stripe Treasury",
                    "credits_needed": max(1, round((s.get("pricing_usdc") or 0.001) * 1000)),
                },
                "track_b": {
                    "method": "crypto",
                    "network": "base-sepolia",
                    "amount_usdc": s.get("pricing_usdc") or 0.001,
                    "calldata_via": "wayforth_pay(service_id, amount_usd, track='crypto')",
                },
                "x402_supported": bool(s.get("x402_supported", False)),
            },
        }
        results.append(entry)

    # Attach similar services for top result when requested
    if body.with_similar and results_raw:
        top_id = str(results_raw[0].get("id", ""))
        try:
            graph_rows = await db.fetch(
                """
                SELECT
                    CASE WHEN service_a_id = $1 THEN service_b_id ELSE service_a_id END AS related_id,
                    co_search_count
                FROM service_graph
                WHERE service_a_id = $1 OR service_b_id = $1
                ORDER BY co_search_count DESC LIMIT 5
                """,
                top_id,
            )
            similar = []
            for gr in graph_rows:
                svc = await db.fetchrow(
                    "SELECT name, category, coverage_tier FROM services WHERE id::text = $1",
                    gr["related_id"],
                )
                if svc:
                        similar.append({
                            "service_id": gr["related_id"],
                            "name": svc["name"],
                            "category": svc["category"],
                            "tier": svc["coverage_tier"],
                            "co_search_count": gr["co_search_count"],
                        })
            results[0]["similar_services"] = similar
        except Exception as e:
            logger.warning(f"with_similar failed: {e}")

    import html as _html
    response: dict = {
        "query": _html.escape(body.query),
        "results": results,
        "total": len(results),
        "total_results": total_results,
        "offset": offset,
        "protocol": "WayforthQL/2.0",
        "filters_applied": {
            "tier_min": body.tier_min,
            "price_max": body.price_max,
            "category": body.category,
            "protocol": body.protocol,
            "sort_by": body.sort_by,
            "exclude_ids": body.exclude_ids or [],
            "x402_only": body.x402_only,
            "verified_only": body.verified_only,
            "provider": body.provider,
            "latency_max": body.latency_max,
            "region": body.region,
            "payment_rail": body.payment_rail,
        },
    }
    if not auth["authenticated"]:
        remaining = _ANON_DAILY_LIMIT - auth["anonymous_count"]
        response["anonymous_searches_remaining"] = remaining
        if remaining > 0:
            response["signup_url"] = "https://wayforth.io/signup"
            response["message"] = f"{remaining} free {'search' if remaining == 1 else 'searches'} remaining. Sign up free for 100/month."
    return response


@router.post("/memory")
@limiter.limit("30/minute")
async def save_memory(request: Request, body: MemoryItem, db=Depends(get_db)):
    """Save a service to agent memory. Requires X-Wayforth-API-Key."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "api_key_required"})
    user_id, _, _ = await _resolve_user(db, api_key)
    # Memory is keyed on the authenticated user_id — client-supplied agent_id is ignored
    await db.execute(
        """
        INSERT INTO agent_memory (agent_id, service_id, service_name, note, created_at, updated_at)
        VALUES ($1, $2, $3, $4, NOW(), NOW())
        ON CONFLICT (agent_id, service_id)
        DO UPDATE SET note=$4, updated_at=NOW()
        """,
        str(user_id), body.service_id, body.service_name, body.note,
    )
    return {"status": "saved", "service_id": body.service_id, "service_name": body.service_name}


@router.get("/memory")
@limiter.limit("30/minute")
async def get_memory(request: Request, q: str = "", db=Depends(get_db)):
    """Retrieve agent's saved services. Requires X-Wayforth-API-Key."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "api_key_required"})
    user_id, _, _ = await _resolve_user(db, api_key)
    # Memory namespace is always the authenticated user_id — client cannot override it
    namespace = str(user_id)
    if q:
        rows = await db.fetch(
            """
            SELECT service_id, service_name, note, created_at
            FROM agent_memory
            WHERE agent_id = $1
            AND (LOWER(service_name) LIKE $2 OR LOWER(note) LIKE $2)
            ORDER BY created_at DESC LIMIT 20
            """,
            namespace, f"%{q.lower()}%",
        )
    else:
        rows = await db.fetch(
            """
            SELECT service_id, service_name, note, created_at
            FROM agent_memory WHERE agent_id = $1
            ORDER BY created_at DESC LIMIT 20
            """,
            namespace,
        )
    return {"services": [dict(r) for r in rows], "total": len(rows)}


@router.post("/tier3/apply")
@limiter.limit("5/minute")
async def tier3_apply(request: Request, body: Tier3Application):
    """Apply for Tier 3 verification — KYB + SLA. Institutional-grade. Manual review required."""
    from main import app
    from notifications import send_tier3_application_notification

    async with app.state.pool.acquire() as db:
        existing = await db.fetchrow("""
            SELECT id, kyb_status FROM tier3_applications
            WHERE contact_email = $1 AND endpoint_url = $2
        """, body.contact_email, body.endpoint_url)

        if existing:
            return {
                "status": "already_applied",
                "kyb_status": existing["kyb_status"],
                "message": "Application already on file. We'll contact you at the email provided.",
            }

        app_id = await db.fetchval("""
            INSERT INTO tier3_applications
            (service_name, company_name, contact_email, website, endpoint_url,
             monthly_volume_usdc, sla_uptime_target, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, NOW())
            RETURNING id
        """, body.service_name, body.company_name, body.contact_email,
            body.website, body.endpoint_url, body.monthly_volume_usd,
            body.sla_uptime_target)

    if os.getenv("RESEND_API_KEY"):
        asyncio.create_task(asyncio.to_thread(
            send_tier3_application_notification,
            body.contact_email, body.service_name, body.company_name, str(app_id),
        ))

    return {
        "status": "submitted",
        "application_id": str(app_id),
        "message": "Application received. Our team will review your KYB documentation and contact you within 2 business days.",
        "next_steps": [
            "We will email you a KYB documentation checklist",
            "SLA terms will be negotiated based on your uptime target",
            "Tier 3 badge appears on your service within 24h of approval",
        ],
    }


@router.get("/tier3/status")
@limiter.limit("10/minute")
async def tier3_status(request: Request, email: str):
    """Check Tier 3 application status by email."""
    from main import app

    async with app.state.pool.acquire() as db:
        apps = await db.fetch("""
            SELECT id, service_name, company_name, kyb_status, created_at
            FROM tier3_applications WHERE contact_email = $1
            ORDER BY created_at DESC
        """, email)
    if not apps:
        return {"status": "not_found", "message": "No application found for this email."}
    return {
        "applications": [dict(a) for a in apps],
        "total": len(apps),
    }


@router.get("/tier3/admin", include_in_schema=False)
@limiter.limit("10/minute")
async def tier3_admin(request: Request):
    """Admin view of Tier 3 applications filtered by KYB status."""
    import secrets as _secrets
    from main import app, ADMIN_KEY

    # Header only — ?key= is no longer accepted (leaks into access logs).
    provided_key = request.headers.get("X-Admin-Key", "")
    if not ADMIN_KEY or not _secrets.compare_digest(provided_key, ADMIN_KEY):
        raise HTTPException(status_code=401, detail="Unauthorized")
    async with app.state.pool.acquire() as db:
        apps = await db.fetch("""
            SELECT id, service_name, company_name, contact_email, endpoint_url,
                   monthly_volume_usdc, sla_uptime_target, kyb_status, created_at
            FROM tier3_applications WHERE kyb_status = $1
            ORDER BY created_at DESC
        """, request.query_params.get("status", "pending"))
    return {
        "status_filter": request.query_params.get("status", "pending"),
        "applications": [dict(a) for a in apps],
        "total": len(apps),
    }
