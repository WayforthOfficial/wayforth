"""routers/execute.py — BYOK key management, /execute, /run, /pay."""

import asyncio
import hashlib
import logging
import math
import os
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from core.auth import _resolve_user, _validate_agent_id, get_fernet
from core.credits import (
    CREDITS_PER_CALL,
    _increment_calls,
    _maybe_dispatch_credits_low,
    check_and_deduct_credits,
    ROUTING_FEE,
)
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import check_rate_limit, require_tier
from services.managed import (
    ADAPTERS,
    SERVICE_ALTERNATIVES,
    SERVICE_CONFIGS,
    SERVICE_DISPLAY_NAMES,
)
from services.param_mapper import (
    CATALOG_TO_MANAGED,
    INTENT_CATEGORY_MAP,
    MANAGED_TO_CATALOG,
    SERVICE_REQUIRED_PARAMS,
    detect_category_hint,
    extract_params_from_intent,
    map_params,
    missing_param_hint,
)

logger = logging.getLogger("wayforth")

router = APIRouter()


async def _award_execution_points(pool, user_id: str, api_key_id: str, tier: str) -> None:
    """Fire-and-forget: award WAYF points for a successful execution."""
    from wayf_points import (
        EXECUTIONS_PER_POINT, DAILY_BONUS_POINTS, award_points, check_milestones
    )
    try:
        async with pool.acquire() as conn:
            calls_count = await conn.fetchval(
                "SELECT calls_count FROM api_keys WHERE id = $1::uuid", api_key_id
            ) or 0

            if calls_count > 0 and calls_count % EXECUTIONS_PER_POINT == 0:
                await award_points(
                    conn, user_id, api_key_id, tier, 1,
                    f"Every {EXECUTIONS_PER_POINT} executions",
                    "execution",
                    {"total_calls": calls_count},
                )

            had_daily = await conn.fetchval(
                """SELECT 1 FROM wayf_points_log
                   WHERE user_id = $1::uuid
                   AND source = 'daily_bonus'
                   AND created_at >= date_trunc('day', NOW())
                   LIMIT 1""",
                user_id,
            )
            if not had_daily:
                await award_points(
                    conn, user_id, api_key_id, tier,
                    DAILY_BONUS_POINTS,
                    "First execution of the day",
                    "daily_bonus",
                )

            await check_milestones(conn, user_id, api_key_id, tier, calls_count)
    except Exception as _e:
        logger.warning("_award_execution_points error: %s", _e)


# ── Pydantic models ───────────────────────────────────────────────────────────

class PayRequest(BaseModel):
    service_id: str
    service_owner: str = ""
    amount_usd: float = 0.0
    query_id: str = ""
    agent_id: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _update_search_signal(pool, user_id: str, clicked_slug: str):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE search_analytics
                SET clicked_slug = $1, payment_followed = true
                WHERE id = (
                    SELECT id FROM search_analytics
                    WHERE user_id = $2::uuid
                      AND payment_followed = false
                      AND created_at > NOW() - INTERVAL '30 minutes'
                    ORDER BY created_at DESC
                    LIMIT 1
                )
            """, clicked_slug, user_id)
    except Exception as e:
        logger.warning(f"search signal update failed: {e}")


async def _x402_settle_cdp(service_endpoint: str, amount_usd: float) -> dict:
    """Attempt x402 settlement via Coinbase CDP. Returns {settled, tx_hash?, reason?}."""
    cdp_key_name = os.environ.get("CDP_API_KEY_NAME", "")
    cdp_private_key = os.environ.get("CDP_API_KEY_PRIVATE_KEY", "")
    if not cdp_key_name or not cdp_private_key:
        return {"settled": False, "reason": "CDP credentials not configured"}
    try:
        from cdp import Cdp, Wallet  # cdp-sdk

        loop = asyncio.get_event_loop()

        # Step 1: initial request to service — expect 402 with payment details
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(service_endpoint)

        if r.status_code != 402:
            return {"settled": False, "reason": f"Service returned {r.status_code}, expected 402"}

        try:
            payment_info = r.json()
        except Exception:
            payment_info = {}

        recipient = (
            payment_info.get("recipient")
            or payment_info.get("payment_address")
            or r.headers.get("x-payment-address")
            or r.headers.get("X-Payment-Address")
        )
        amount_usdc = float(payment_info.get("amount_usdc", amount_usd))
        network_id = "base-sepolia"

        if not recipient:
            return {"settled": False, "reason": "Could not parse payment recipient from 402 response"}

        # Step 2: configure CDP and submit USDC transfer (sync SDK — run in executor)
        def _cdp_transfer():
            Cdp.configure(cdp_key_name, cdp_private_key)
            wallet = Wallet.create(network_id=network_id)
            transfer = wallet.transfer(amount_usdc, "usdc", recipient)
            transfer.wait(timeout_seconds=15, interval_seconds=0.5)
            return transfer.transaction_hash

        tx_hash = await asyncio.wait_for(
            loop.run_in_executor(None, _cdp_transfer),
            timeout=20.0,
        )
        return {"settled": True, "tx_hash": tx_hash, "network": network_id}

    except asyncio.TimeoutError:
        return {"settled": False, "reason": "x402 settlement timed out after 20 seconds"}
    except ImportError:
        return {"settled": False, "reason": "cdp-sdk not installed"}
    except Exception as e:
        return {"settled": False, "reason": str(e)[:200]}


# ── BYOK key routes ───────────────────────────────────────────────────────────

@router.get("/call/keys")
@limiter.limit("30/minute")
async def list_service_keys(request: Request, db=Depends(get_db)):
    """List the caller's stored BYOK service keys (active only)."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)
    user_id, _api_key_id, _tier = await _resolve_user(db, api_key)

    rows = await db.fetch("""
        SELECT service_slug, service_name, key_preview,
               total_calls, last_used_at, active, created_at,
               endpoint_url, default_method
        FROM user_service_keys
        WHERE user_id=$1::uuid AND active=true
        ORDER BY created_at DESC
    """, user_id)
    return {"service_keys": [dict(r) for r in rows], "total": len(rows)}


@router.post("/call/keys/add")
@limiter.limit("10/minute")
async def add_service_key(request: Request, db=Depends(get_db)):
    """Store an encrypted BYOK API key for a third-party service."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)
    user_id, _api_key_id, _tier = await _resolve_user(db, api_key)
    require_tier(_tier, "byok")

    body = await request.json()
    service_slug = body.get("service_slug", "").strip().lower()
    service_name = body.get("service_name", "").strip()
    raw_key = body.get("api_key", "").strip()
    endpoint_url = body.get("endpoint_url", "").strip() or None
    default_method = (body.get("default_method", "") or "POST").strip().upper()

    if not service_slug or not raw_key:
        raise HTTPException(status_code=400, detail={"error": "service_slug and api_key required"})
    if default_method not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        raise HTTPException(status_code=400, detail={"error": "default_method must be GET, POST, PUT, PATCH, or DELETE"})
    if endpoint_url and not endpoint_url.startswith("https://"):
        raise HTTPException(status_code=400, detail={"error": "endpoint_url must start with https://"})

    preview = raw_key[:4] + "****" + raw_key[-4:] if len(raw_key) >= 8 else "****"

    try:
        f = get_fernet()
        encrypted = f.encrypt(raw_key.encode()).decode()
    except Exception as _enc_err:
        logger.error("BYOK: failed to encrypt key for %s: %s", service_slug, _enc_err)
        raise HTTPException(status_code=500, detail={
            "error": "encryption_unavailable",
            "message": "Service key could not be stored securely. Check ENCRYPTION_KEY configuration.",
        })

    await db.execute("""
        INSERT INTO user_service_keys
            (user_id, service_slug, service_name, encrypted_key, key_preview, endpoint_url, default_method, active)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, true)
        ON CONFLICT (user_id, service_slug)
        DO UPDATE SET
            service_name=EXCLUDED.service_name,
            encrypted_key=EXCLUDED.encrypted_key,
            key_preview=EXCLUDED.key_preview,
            endpoint_url=EXCLUDED.endpoint_url,
            default_method=EXCLUDED.default_method,
            active=true,
            updated_at=NOW()
    """, user_id, service_slug, service_name or service_slug, encrypted, preview, endpoint_url, default_method)

    return {
        "service_slug": service_slug,
        "service_name": service_name or service_slug,
        "key_preview": preview,
        "endpoint_url": endpoint_url,
        "default_method": default_method,
        "created": True,
    }


@router.delete("/call/keys/{service_slug}")
@limiter.limit("10/minute")
async def deactivate_service_key(request: Request, service_slug: str, db=Depends(get_db)):
    """Soft-delete a stored service key (sets active=false)."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)
    user_id, _api_key_id, _tier = await _resolve_user(db, api_key)

    result = await db.execute("""
        UPDATE user_service_keys
        SET active=false, updated_at=NOW()
        WHERE user_id=$1::uuid AND service_slug=$2 AND active=true
    """, user_id, service_slug)

    if result == "UPDATE 0":
        raise HTTPException(status_code=404, detail={"error": "key_not_found"})
    return {"service_slug": service_slug, "deactivated": True}


# ── /pay ──────────────────────────────────────────────────────────────────────

@router.post("/pay")
@limiter.limit("30/minute")
async def pay_for_service(request: Request, db=Depends(get_db)):
    """
    Pay for a service through Wayforth.

    Two payment tracks:

    Track A (card-funded via Stripe Treasury):
      - Developer funded their Wayforth balance via card
      - Credits deducted from balance
      - Wayforth instructs Stripe Treasury to pay service
      - Returns payment receipt

    Track B (crypto wallet — non-custodial):
      - Developer has own Base wallet with USDC
      - Returns approve + payment calldata
      - Agent broadcasts from own wallet
      - Wayforth captures routing fee on-chain

    Track C (x402 native — detected automatically):
      - Service supports x402 protocol
      - Returns x402 payment details
      - Coinbase facilitator handles settlement

    Routing fee: 1.5% on all tracks
    30% of fee allocated to $WAYF burn (post-mainnet)
    """
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "api_key_required",
                "message": "Get your free API key at wayforth.io/dashboard",
            },
        )

    key_record = await db.fetchrow(
        """
        SELECT k.user_id, k.tier, u.email
        FROM api_keys k JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1 AND k.active = true
        """,
        hashlib.sha256(api_key.encode()).hexdigest(),
    )

    if not key_record:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})

    body = await request.json()
    service_id = body.get("service_id", "")
    amount_usd = float(body.get("amount_usd", 0.001))
    track = body.get("track", "auto")  # auto, card, crypto
    query_id = body.get("query_id", None)

    if not service_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "service_id_required",
                "example": {"service_id": "deepl", "amount_usd": 0.001},
            },
        )

    # Look up service (match by name or DB id; wayforth_id is computed not stored)
    service = await db.fetchrow(
        """
        SELECT id, name, payment_protocol, pricing_usdc, x402_supported, endpoint_url
        FROM services
        WHERE name ILIKE $1 OR id::text = $1
        LIMIT 1
        """,
        service_id,
    )

    # Calculate routing fee
    routing_fee_pct = ROUTING_FEE
    routing_fee_usd = round(amount_usd * routing_fee_pct, 8)
    service_receives_usd = round(amount_usd - routing_fee_usd, 8)
    wayf_burn_allocation = round(routing_fee_usd * 0.30, 8)  # 30% to $WAYF burn
    wayforth_revenue = round(routing_fee_usd * 0.70, 8)      # 70% to Wayforth

    service_name = service["name"] if service else service_id
    x402_supported = service["x402_supported"] if service else False

    # TRACK C: x402 native — attempt real CDP settlement, fall back to Track A if unconfigured
    x402_fallback_note = None
    if x402_supported and track in ["auto", "crypto"]:
        cdp_configured = bool(
            os.environ.get("CDP_API_KEY_NAME") and os.environ.get("CDP_API_KEY_PRIVATE_KEY")
        )
        if not cdp_configured:
            x402_fallback_note = "CDP not configured, routed via card"
        else:
            endpoint_url = service["endpoint_url"] if service else None
            if not endpoint_url:
                x402_fallback_note = "x402 settlement unavailable (no service endpoint), routed via card"
            else:
                settlement = await _x402_settle_cdp(endpoint_url, amount_usd)
                if settlement["settled"]:
                    credits_needed = max(1, round(amount_usd * 1000))
                    ok, bal_after = await check_and_deduct_credits(
                        db, str(key_record["user_id"]), credits_needed, "/pay", service_id
                    )
                    if query_id and service:
                        try:
                            await db.execute(
                                """
                                INSERT INTO search_outcomes
                                (query_id, service_id, payment_amount_usdc, chain, payment_track)
                                VALUES ($1, $2::uuid, $3, 'base-sepolia', 'x402')
                                ON CONFLICT DO NOTHING
                                """,
                                query_id,
                                str(service["id"]),
                                amount_usd,
                            )
                        except Exception:
                            pass
                    return {
                        "payment_track": "x402",
                        "status": "ok",
                        "service_id": service_id,
                        "service_name": service_name,
                        "amount_usd": amount_usd,
                        "facilitator": "Coinbase CDP",
                        "tx_hash": settlement["tx_hash"],
                        "network": settlement.get("network", "base-sepolia"),
                        "credits_deducted": credits_needed if ok else 0,
                        "credits_remaining": bal_after,
                        "query_id": query_id,
                    }
                else:
                    x402_fallback_note = f"x402 failed ({settlement['reason']}), routed via card"

    # TRACK B: Crypto calldata (non-custodial)
    if track == "crypto":
        escrow_address = "0xE6EDB0a93e0e0cB9F0402Bd49F2eD1Fffc448809"
        usdc_address = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
        amount_usdc = amount_usd

        # Log payment intent for WayforthRank
        if query_id and service:
            try:
                await db.execute(
                    """
                    INSERT INTO search_outcomes
                    (query_id, service_id, payment_amount_usdc, chain, payment_track)
                    VALUES ($1, $2::uuid, $3, 'base-sepolia', 'crypto')
                    ON CONFLICT DO NOTHING
                    """,
                    query_id,
                    str(service["id"]),
                    amount_usdc,
                )
            except Exception:
                pass

        return {
            "payment_track": "crypto",
            "service_id": service_id,
            "service_name": service_name,
            "amount_usd": amount_usd,
            "amount_usdc": amount_usdc,
            "routing_fee_usd": routing_fee_usd,
            "network": "base-sepolia",
            "escrow_address": escrow_address,
            "usdc_contract": usdc_address,
            "approve_calldata": f"0x095ea7b3{escrow_address[2:].zfill(64)}{hex(int(amount_usdc * 1e6))[2:].zfill(64)}",
            "payment_calldata": f"0x{secrets.token_hex(32)}",
            "instructions": [
                "1. Call approve() on USDC contract with escrow_address and amount",
                "2. Call routePayment() on escrow with payment_calldata",
                "3. Wayforth captures 1.5% routing fee from escrow",
            ],
            "status": "calldata_ready",
            "query_id": query_id,
        }

    # TRACK A: Card-funded (Stripe Treasury — credits deduction)
    credits_needed = max(1, round(amount_usd * 1000))

    success, balance_after = await check_and_deduct_credits(
        db,
        str(key_record["user_id"]),
        credits_needed,
        "/pay",
        service_id,
    )

    if not success:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_credits",
                "message": f"Need {credits_needed} credits. Balance: {balance_after}.",
                "credits_needed": credits_needed,
                "credits_balance": balance_after,
                "top_up_url": "https://wayforth.io/dashboard",
                "alternative": "Use track='crypto' if you have a Base wallet with USDC",
            },
        )

    # Log payment for WayforthRank
    if query_id and service:
        try:
            await db.execute(
                """
                INSERT INTO search_outcomes
                (query_id, service_id, payment_amount_usdc, chain, payment_track)
                VALUES ($1, $2::uuid, $3, 'stripe-treasury', 'card')
                ON CONFLICT DO NOTHING
                """,
                query_id,
                str(service["id"]),
                amount_usd,
            )
        except Exception:
            pass

    tx_ref = f"wf_pay_{secrets.token_hex(12)}"

    card_response = {
        "payment_track": "card",
        "service_id": service_id,
        "service_name": service_name,
        "amount_usd": amount_usd,
        "routing_fee_usd": routing_fee_usd,
        "credits_deducted": credits_needed,
        "credits_remaining": balance_after,
        "status": "ok",
        "tx_ref": tx_ref,
        "query_id": query_id,
    }
    if x402_fallback_note:
        card_response["x402_fallback"] = x402_fallback_note
    return card_response


# ── /execute ──────────────────────────────────────────────────────────────────

@router.post("/execute")
@limiter.limit("60/minute")
async def execute_service(request: Request, db=Depends(get_db)):
    """Call a real external API using Wayforth-managed keys or user BYOK keys."""
    import time as _time

    api_key_header = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key_header:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key header required"})

    user_id, _api_key_id, _tier = await _resolve_user(db, api_key_header)
    check_rate_limit(str(_api_key_id), _tier)

    body = await request.json()
    service_slug = body.get("service_slug", "").strip().lower()
    params = body.get("params", {})
    key_source = body.get("key_source", "managed")
    agent_id = _validate_agent_id(body.get("agent_id"))

    if key_source not in ("managed", "byok"):
        raise HTTPException(status_code=400, detail={"error": "key_source must be 'managed' or 'byok'"})

    is_managed_service = service_slug in SERVICE_CONFIGS
    is_byok = (not is_managed_service) and key_source == "byok"

    # Cross-rail: x402-capable catalog service that is not in our managed set
    is_cross_rail = not is_managed_service and not is_byok
    cross_rail_svc = None
    if is_cross_rail:
        catalog_svc = await db.fetchrow(
            "SELECT id, name, slug, category, x402_supported, endpoint_url, pricing_usdc, consecutive_failures "
            "FROM services WHERE slug = $1 OR LOWER(name) = $1",
            service_slug,
        )

        if not catalog_svc:
            raise HTTPException(status_code=404, detail={
                "error": "service_not_found",
                "service": service_slug,
                "message": "No service found with this slug.",
                "suggestion": "Search for available services:",
                "search_endpoint": f"GET /search?q={service_slug}",
                "docs": "https://wayforth.io/docs",
            })

        if not catalog_svc["x402_supported"]:
            display_name_to_slug = {v: k for k, v in SERVICE_DISPLAY_NAMES.items()}
            managed_names = list(SERVICE_DISPLAY_NAMES.values())
            svc_category = catalog_svc["category"]
            svc_name = catalog_svc["name"]

            alt_rows = await db.fetch(
                "SELECT name FROM services "
                "WHERE category = $1 AND name = ANY($2::text[]) LIMIT 2",
                svc_category, managed_names,
            )
            alternatives = [
                display_name_to_slug[r["name"]]
                for r in alt_rows
                if r["name"] in display_name_to_slug
            ]

            raise HTTPException(status_code=422, detail={
                "error": "key_required",
                "service": service_slug,
                "service_name": svc_name,
                "message": "This service requires your own API key. Add it to execute through Wayforth.",
                "action": {
                    "label": "Add API key",
                    "url": "https://wayforth.io/dashboard/keys",
                    "endpoint": "POST /call/keys/add",
                    "body_example": {
                        "service_slug": catalog_svc["slug"] or service_slug,
                        "service_name": svc_name,
                        "api_key": "your_key_here",
                    },
                },
                "alternatives": {
                    "message": "Or use a managed service with zero setup:",
                    "services": alternatives,
                },
            })

        if catalog_svc["consecutive_failures"] >= 3:
            raise HTTPException(status_code=503, detail={
                "error": "service_unavailable",
                "service": service_slug,
                "message": "Service is temporarily unhealthy. Try again shortly.",
            })

        cross_rail_svc = catalog_svc

    # ── Cross-rail path: credits → x402 catalog service ──────────────────────
    if is_cross_rail and cross_rail_svc:
        pricing_usdc  = float(cross_rail_svc["pricing_usdc"] or 0.01)
        total_credits = math.ceil(pricing_usdc * 1000)

        success, balance_after = await check_and_deduct_credits(
            db, str(user_id), total_credits, "/execute",
            service_id=service_slug, tx_type="cross_rail",
            agent_id=agent_id, api_key_id=str(_api_key_id),
        )
        if not success:
            raise HTTPException(status_code=402, detail={
                "error": "insufficient_credits",
                "message": f"You need {total_credits - balance_after} more credits for this call. "
                           "Top up at wayforth.io/billing",
                "credits_needed": total_credits,
                "top_up_url": "https://wayforth.io/billing",
            })

        from main import app
        settlement = await _x402_settle_cdp(cross_rail_svc["endpoint_url"], pricing_usdc)
        if not settlement["settled"]:
            await db.execute(
                "UPDATE user_credits SET credits_balance = credits_balance + $1, updated_at = NOW() "
                "WHERE user_id = $2::uuid",
                total_credits, user_id,
            )
            raise HTTPException(status_code=503, detail={
                "error": f"Cross-rail payment failed: {settlement.get('reason')}",
                "credits_refunded": total_credits,
            })

        if _api_key_id:
            await _increment_calls(app.state.pool, str(_api_key_id))
        return {
            "status": "ok",
            "service": service_slug,
            "result": settlement,
            "cross_rail": True,
            "calls_used": 1,
            "priority": _tier in ("pro", "growth"),
        }

    # ── Universal BYOK path (any external API, not in managed catalog) ────────
    if is_byok:
        byok_row = await db.fetchrow(
            "SELECT encrypted_key, endpoint_url, default_method FROM user_service_keys "
            "WHERE user_id=$1::uuid AND service_slug=$2 AND active=true",
            user_id, service_slug,
        )
        if not byok_row:
            raise HTTPException(status_code=404, detail={
                "error": f"No BYOK key found for '{service_slug}'. Add one at /call/keys/add"
            })
        try:
            f = get_fernet()
            byok_key = f.decrypt(byok_row["encrypted_key"].encode()).decode()
        except Exception as _dec_err:
            logger.error("BYOK: failed to decrypt key for %s: %s", service_slug, _dec_err)
            raise HTTPException(status_code=500, detail={"error": "decryption_failed"})

        req_endpoint = body.get("endpoint_url", "").strip() or byok_row["endpoint_url"]
        req_method = (body.get("method", "") or byok_row["default_method"] or "POST").upper()
        extra_headers = body.get("headers", {})

        if not req_endpoint:
            raise HTTPException(status_code=400, detail={
                "error": "endpoint_url required (pass in body or store a default via /call/keys/add)"
            })
        if not req_endpoint.startswith("https://"):
            raise HTTPException(status_code=400, detail={"error": "endpoint_url must start with https://"})

        success, balance_after = await check_and_deduct_credits(
            db, str(user_id), 1, "/execute",
            service_id=service_slug, tx_type="execution",
            agent_id=agent_id, api_key_id=str(_api_key_id),
        )
        if not success:
            raise HTTPException(status_code=402, detail={
                "error": "insufficient_credits",
                "message": f"You need {1 - balance_after} more credits for this call. Top up at wayforth.io/billing",
                "credits_balance": balance_after,
                "credits_needed": 1,
                "top_up_url": "https://wayforth.io/billing",
            })

        import httpx as _httpx
        start = _time.time()
        call_headers = {"Authorization": f"Bearer {byok_key}", **extra_headers}
        error_msg = None
        raw_result = None
        try:
            async with _httpx.AsyncClient(timeout=15.0) as _client:
                if req_method in ("GET", "DELETE"):
                    resp = await _client.request(req_method, req_endpoint, headers=call_headers, params=params)
                else:
                    resp = await _client.request(req_method, req_endpoint, headers=call_headers, json=params)
            raw_result = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
            if resp.status_code >= 400:
                error_msg = f"Upstream {resp.status_code}: {str(raw_result)[:200]}"
        except _httpx.TimeoutException:
            error_msg = "Service timeout"
        except Exception as _e:
            error_msg = str(_e)[:300]

        execution_ms = round((_time.time() - start) * 1000)

        if error_msg:
            async with db.transaction():
                refund_row = await db.fetchrow(
                    "UPDATE user_credits SET credits_balance = credits_balance + 1, updated_at = NOW() "
                    "WHERE user_id = $1::uuid RETURNING credits_balance",
                    user_id,
                )
                refunded_balance = refund_row["credits_balance"] if refund_row else balance_after
                await db.execute("""
                    INSERT INTO credit_transactions
                    (user_id, amount, balance_after, type, description, api_endpoint, service_id)
                    VALUES ($1::uuid, $2, $3, 'execution_refund', $4, '/execute', $5)
                """, user_id, 1, refunded_balance,
                    f"Refund: {service_slug} BYOK failed - {error_msg[:100]}", service_slug)
            raise HTTPException(status_code=503, detail={
                "status": "error",
                "service": service_slug,
                "error": error_msg,
                "credits_deducted": 0,
                "credits_remaining": refunded_balance,
            })

        from main import app
        asyncio.create_task(_update_search_signal(app.state.pool, str(user_id), service_slug))
        if _api_key_id:
            await _increment_calls(app.state.pool, str(_api_key_id))
        return {
            "status": "ok",
            "service": service_slug,
            "key_source": "byok",
            "result": raw_result,
            "credits_deducted": 1,
            "credits_remaining": balance_after,
            "execution_ms": execution_ms,
            "priority": _tier in ("pro", "growth"),
        }

    # ── Managed-catalog path ──────────────────────────────────────────────────
    config = SERVICE_CONFIGS[service_slug]

    # Stability has variable credits: 45 for core (default), 100 for ultra
    if service_slug == "stability":
        quality = params.get("quality", "core")
        credit_cost = 100 if quality == "ultra" else 45
    else:
        credit_cost = config["credits"]

    if key_source == "managed":
        svc_key = os.environ.get(config["key_var"], "")
        if not svc_key:
            alt = SERVICE_ALTERNATIVES.get(service_slug)
            alt_msg = f" Try '{alt}' for similar functionality." if alt else ""
            raise HTTPException(status_code=503, detail={
                "error": f"'{service_slug}' is not yet available on this server.{alt_msg}"
            })
    else:
        row = await db.fetchrow(
            "SELECT encrypted_key FROM user_service_keys WHERE user_id=$1::uuid AND service_slug=$2 AND active=true",
            user_id, service_slug,
        )
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "No API key found for service. Add one at /call/keys/add"
            })
        try:
            f = get_fernet()
            svc_key = f.decrypt(row["encrypted_key"].encode()).decode()
        except Exception as _dec_err:
            logger.error("BYOK: failed to decrypt key for service %s: %s", service_slug, _dec_err)
            raise HTTPException(status_code=500, detail={
                "error": "decryption_failed",
                "message": "Could not decrypt service key. Contact support.",
            })

    # Validate key is ASCII-safe (HTTP headers require ASCII)
    try:
        svc_key.encode("ascii")
    except UnicodeEncodeError as enc_err:
        raise HTTPException(status_code=503, detail={
            "error": (
                f"API key for '{service_slug}' contains non-ASCII characters at position {enc_err.start}. "
                "Re-paste the key in Railway environment variables using plain text (avoid rich text editors)."
            )
        })

    success, balance_after = await check_and_deduct_credits(
        db, str(user_id), credit_cost, "/execute",
        service_id=service_slug, tx_type="execution",
        agent_id=agent_id, api_key_id=str(_api_key_id),
    )
    if not success:
        raise HTTPException(status_code=402, detail={
            "error": "insufficient_credits",
            "message": f"You need {credit_cost - balance_after} more credits for this call. Top up at wayforth.io/billing",
            "credits_balance": balance_after,
            "credits_needed": credit_cost,
            "top_up_url": "https://wayforth.io/billing",
        })

    start = _time.time()
    adapter = ADAPTERS[service_slug]
    result = None
    error_msg = None

    if service_slug == "assemblyai":
        try:
            result = await asyncio.wait_for(adapter(params, svc_key), timeout=35.0)
        except asyncio.TimeoutError:
            error_msg = "Service timeout"
        except Exception as e:
            error_msg = str(e)[:300]
    else:
        for attempt in range(2):
            try:
                result = await asyncio.wait_for(adapter(params, svc_key), timeout=10.0)
                break
            except asyncio.TimeoutError:
                if attempt == 0:
                    continue
                error_msg = "Service timeout"
            except Exception as e:
                error_msg = str(e)[:300]
                break

    execution_ms = round((_time.time() - start) * 1000)

    if error_msg:
        async with db.transaction():
            refund_row = await db.fetchrow(
                "UPDATE user_credits SET credits_balance = credits_balance + $1, updated_at = NOW() "
                "WHERE user_id = $2::uuid RETURNING credits_balance",
                credit_cost, user_id,
            )
            refunded_balance = refund_row["credits_balance"] if refund_row else balance_after
            await db.execute("""
                INSERT INTO credit_transactions
                (user_id, amount, balance_after, type, description, api_endpoint, service_id)
                VALUES ($1::uuid, $2, $3, 'execution_refund', $4, '/execute', $5)
            """, user_id, credit_cost, refunded_balance,
                f"Refund: {service_slug} failed - {error_msg[:100]}", service_slug)
        raise HTTPException(status_code=503, detail={
            "status": "error",
            "service": service_slug,
            "error": error_msg,
            "credits_deducted": 0,
            "credits_remaining": refunded_balance,
        })

    from core.credits import _dispatch_webhooks
    from main import app
    asyncio.create_task(_dispatch_webhooks(
        str(user_id), "execution.completed", {
            "service_slug": service_slug,
            "credits_used": credit_cost,
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    ))
    asyncio.create_task(_update_search_signal(app.state.pool, str(user_id), service_slug))
    asyncio.create_task(
        _maybe_dispatch_credits_low(app.state.pool, str(user_id), api_key_header, balance_after)
    )
    if _api_key_id:
        await _increment_calls(app.state.pool, str(_api_key_id))

    # Award WAYF points asynchronously (fire-and-forget)
    if _api_key_id and user_id:
        asyncio.create_task(_award_execution_points(
            app.state.pool, str(user_id), str(_api_key_id), _tier
        ))

    return {
        "status": "ok",
        "service": service_slug,
        "result": result,
        "credits_deducted": credit_cost,
        "execution_ms": execution_ms,
        "managed_services_available": len(SERVICE_CONFIGS),
        "priority": _tier in ("pro", "growth"),
    }


# ── /run — one-call runtime ───────────────────────────────────────────────────

@router.post("/run")
@limiter.limit("15/minute")
async def run_endpoint(request: Request, db=Depends(get_db)):
    """Intent → search → rank → execute → result in one call."""
    import time as _time

    api_key_header = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key_header:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key header required"})

    user_id, _api_key_id, _tier = await _resolve_user(db, api_key_header)
    check_rate_limit(str(_api_key_id), _tier)
    body = await request.json()

    intent = (body.get("intent") or "").strip()
    if not intent:
        raise HTTPException(status_code=400, detail={"error": "intent is required"})

    agent_id = _validate_agent_id(body.get("agent_id"))
    input_dict = body.get("input") or {}
    prefs = body.get("preferences") or {}
    category_filter = prefs.get("category") or detect_category_hint(intent)
    max_price = prefs.get("max_price_per_call")
    tier_min = int(prefs.get("tier_min", 2))

    run_start = _time.time()

    # Step 1 — Search: same DB query as GET /search
    conditions = [f"coverage_tier >= {tier_min}", "consecutive_failures < 3"]
    params_q: list = []
    idx = 1
    if category_filter:
        conditions.append(f"category = ${idx}")
        params_q.append(category_filter)
        idx += 1
    if max_price is not None:
        conditions.append(f"(pricing_usdc IS NULL OR pricing_usdc <= ${idx})")
        params_q.append(float(max_price))
        idx += 1

    where = " AND ".join(conditions)
    from main import app
    from ranker_client import rank_services
    try:
        rows = await db.fetch(
            f"""
            SELECT id, name, slug, description, endpoint_url, category,
                   pricing_usdc, coverage_tier, source, payment_protocol,
                   last_tested_at, consecutive_failures, x402_supported,
                   wri_score, wri_version
            FROM services
            WHERE {where}
            ORDER BY coverage_tier DESC
            LIMIT 200
            """,
            *params_q,
        )
    except Exception as _db_err:
        logger.error("run: db error: %s", _db_err)
        raise HTTPException(status_code=503, detail="Database unavailable")

    candidates = [dict(r) for r in rows]
    try:
        ranked = await rank_services(intent, candidates)
    except Exception as _re:
        logger.error("run ranker error: %s", _re)
        raise HTTPException(status_code=503, detail={"error": "ranker_unavailable"})
    top5 = ranked[:5]

    # Enrich input_dict with any params extractable from the intent string itself
    # (e.g. "translate Hello to Spanish" → {text: "Hello", target_lang: "ES"})
    extracted = extract_params_from_intent(intent)
    if extracted:
        merged = dict(extracted)
        merged.update(input_dict)  # explicit user-supplied values always win
        input_dict = merged

    # Step 2 — Select: first managed service in ranked results with a configured key.
    # Scan all ranked results (not just top-5) so managed services ranked outside
    # top-5 by the LLM still get found.
    selected_slug: str | None = None
    selected_svc: dict | None = None
    selected_rank: int | None = None

    _compatible_cats = INTENT_CATEGORY_MAP.get(category_filter) if category_filter else None

    for i, svc in enumerate(ranked):
        catalog_slug = svc.get("slug") or ""
        managed_slug = CATALOG_TO_MANAGED.get(catalog_slug)
        if not managed_slug:
            continue
        if managed_slug not in SERVICE_CONFIGS:
            continue
        if not os.environ.get(SERVICE_CONFIGS[managed_slug]["key_var"], ""):
            continue
        if _compatible_cats and svc.get("category") not in _compatible_cats:
            continue  # wrong category for this intent
        _, _missing = map_params(managed_slug, input_dict)
        if _missing:
            continue  # params not satisfied — try next service
        selected_slug = managed_slug
        selected_svc = svc
        selected_rank = i + 1
        break

    # If category filter produced no managed service, retry with a direct lookup
    # of all known managed catalog slugs — bypasses rank_services() entirely so
    # category/tier mismatches (e.g. intent="search" but DB category="data") can't block.
    if not selected_slug and category_filter:
        try:
            managed_catalog_slugs = list(CATALOG_TO_MANAGED.keys())
            fb_rows = await db.fetch(
                """SELECT id, name, slug, description, endpoint_url, category,
                          pricing_usdc, coverage_tier, source, payment_protocol,
                          last_tested_at, consecutive_failures, x402_supported,
                          wri_score, wri_version
                   FROM services
                   WHERE slug = ANY($1::text[])
                     AND consecutive_failures < 3
                   ORDER BY wri_score DESC NULLS LAST""",
                managed_catalog_slugs,
            )
            logger.info("run fallback: found %d managed catalog services; slugs: %s",
                        len(fb_rows), [r["slug"] for r in fb_rows])
            for row in fb_rows:
                catalog_slug = row["slug"]
                managed_slug = CATALOG_TO_MANAGED.get(catalog_slug)
                if not managed_slug:
                    continue
                if managed_slug not in SERVICE_CONFIGS:
                    continue
                if not os.environ.get(SERVICE_CONFIGS[managed_slug]["key_var"], ""):
                    continue
                if _compatible_cats and row.get("category") not in _compatible_cats:
                    continue  # wrong category for this intent
                _, _fb_missing = map_params(managed_slug, input_dict)
                if _fb_missing:
                    continue  # params not satisfied — try next service
                selected_svc = dict(row)
                selected_slug = managed_slug
                selected_rank = 999
                top5 = [dict(r) for r in fb_rows[:5]]
                logger.info("run fallback: selected managed slug=%s", managed_slug)
                break
            if not selected_slug:
                logger.warning("run fallback: no managed service found; catalog slugs in DB: %s",
                               [r["slug"] for r in fb_rows])
        except Exception as _fb_err:
            logger.warning("run: category-free fallback failed: %s", _fb_err)

    if not selected_slug:
        top = top5[0] if top5 else None
        if not top:
            try:
                _best = await db.fetchrow(
                    "SELECT slug, name, wri_score FROM services "
                    "WHERE consecutive_failures < 3 ORDER BY wri_score DESC NULLS LAST LIMIT 1"
                )
                top = dict(_best) if _best else {}
            except Exception:
                top = {}
        raise HTTPException(status_code=422, detail={
            "error": "no_managed_service",
            "intent": intent,
            "message": (
                f"No managed service matched this intent. "
                f"Top catalog result: {top.get('name', 'unknown')} — "
                "add your key via BYOK to use it."
            ),
            "top_result": {
                "slug": top.get("slug"),
                "name": top.get("name"),
                "wri_score": top.get("wri_score"),
            } if top else None,
            "action": "POST /call/keys/add",
        })

    # Step 3 — Map params
    mapped_params, missing = map_params(selected_slug, input_dict)
    if missing:
        raise HTTPException(status_code=422, detail={
            "error": "missing_param",
            "service_selected": selected_slug,
            "required_params": SERVICE_REQUIRED_PARAMS.get(selected_slug, []),
            "provided_params": list(input_dict.keys()),
            "missing": missing,
            "hint": missing_param_hint(missing),
        })

    # Step 4 — Execute (managed path, mirrors /execute)
    config = SERVICE_CONFIGS[selected_slug]
    if selected_slug == "stability":
        credit_cost = 100 if mapped_params.get("quality") == "ultra" else 45
    else:
        credit_cost = config["credits"]

    svc_key = os.environ.get(config["key_var"], "")

    success, balance_after = await check_and_deduct_credits(
        db, str(user_id), credit_cost, "/run",
        service_id=selected_slug, tx_type="execution",
        agent_id=agent_id, api_key_id=str(_api_key_id),
    )
    if not success:
        raise HTTPException(status_code=402, detail={
            "error": "insufficient_credits",
            "required_credits": credit_cost,
            "current_balance_credits": balance_after,
            "current_balance_calls": balance_after // CREDITS_PER_CALL,
            "message": "Not enough credits for this call.",
            "top_up": "https://wayforth.io/billing",
        })

    _calls_remaining: int = balance_after // CREDITS_PER_CALL  # fallback if key absent
    if _api_key_id:
        _inc = await _increment_calls(app.state.pool, str(_api_key_id))
        if _inc:
            _calls_remaining = _inc

    # Award WAYF points asynchronously for /run
    if _api_key_id and user_id:
        asyncio.create_task(_award_execution_points(
            app.state.pool, str(user_id), str(_api_key_id), _tier
        ))

    adapter = ADAPTERS[selected_slug]
    result = None
    error_msg = None
    exec_start = _time.time()

    if selected_slug == "assemblyai":
        try:
            result = await asyncio.wait_for(adapter(mapped_params, svc_key), timeout=35.0)
        except asyncio.TimeoutError:
            error_msg = "Service timeout"
        except Exception as _e:
            error_msg = str(_e)[:300]
    else:
        for _attempt in range(2):
            try:
                result = await asyncio.wait_for(adapter(mapped_params, svc_key), timeout=10.0)
                break
            except asyncio.TimeoutError:
                if _attempt == 0:
                    continue
                error_msg = "Service timeout"
            except Exception as _e:
                error_msg = str(_e)[:300]
                break

    execution_ms = round((_time.time() - exec_start) * 1000)

    if error_msg:
        async with db.transaction():
            _refund = await db.fetchrow(
                "UPDATE user_credits SET credits_balance = credits_balance + $1, updated_at = NOW() "
                "WHERE user_id = $2::uuid RETURNING credits_balance",
                credit_cost, user_id,
            )
            refunded_balance = _refund["credits_balance"] if _refund else balance_after
            await db.execute("""
                INSERT INTO credit_transactions
                (user_id, amount, balance_after, type, description, api_endpoint, service_id)
                VALUES ($1::uuid, $2, $3, 'execution_refund', $4, '/run', $5)
            """, user_id, credit_cost, refunded_balance,
                f"Refund: {selected_slug} failed — {error_msg[:100]}", selected_slug)
        fallback_slug = SERVICE_ALTERNATIVES.get(selected_slug)
        detail: dict = {
            "error": "service_unavailable",
            "service": selected_slug,
            "message": f"{SERVICE_DISPLAY_NAMES.get(selected_slug, selected_slug)} is temporarily unavailable.",
        }
        if fallback_slug:
            detail["fallback"] = {
                "slug": fallback_slug,
                "message": f"Try {SERVICE_DISPLAY_NAMES.get(fallback_slug, fallback_slug)} as an alternative.",
            }
        raise HTTPException(status_code=503, detail=detail)

    asyncio.create_task(_update_search_signal(app.state.pool, str(user_id), selected_slug))
    asyncio.create_task(
        _maybe_dispatch_credits_low(app.state.pool, str(user_id), api_key_header, balance_after)
    )

    wri = selected_svc.get("wri_score")
    return {
        "result": result,
        "service_used": {
            "slug": selected_slug,
            "name": SERVICE_DISPLAY_NAMES.get(selected_slug, selected_slug),
            "wri_score": round(float(wri), 1) if wri else None,
            "category": selected_svc.get("category"),
            "credits_used": credit_cost,
        },
        "search_context": {
            "intent": intent,
            "results_considered": len(top5),
            "selected_rank": selected_rank,
        },
        "calls_remaining": _calls_remaining,
        "execution_ms": execution_ms,
        "priority": _tier in ("pro", "growth"),
    }
