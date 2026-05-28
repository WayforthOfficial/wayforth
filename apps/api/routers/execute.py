"""routers/execute.py — BYOK key management, /execute, /run, /pay."""

import asyncio
import hashlib
import json as _json
import logging
import math
import os
import re
import secrets
import time as _time_mod
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.auth import _resolve_user, _validate_agent_id, decrypt_api_key, encrypt_api_key
from core.credits import (
    CREDITS_PER_CALL,
    _check_spend_anomaly,
    _increment_calls,
    _maybe_dispatch_credits_low,
    check_and_deduct_credits,
    compute_calls_remaining,
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

# ── /run result cache — 10s TTL, max 1000 entries ────────────────────────────

_RUN_CACHE: dict[tuple, tuple[float, dict]] = {}
_RUN_CACHE_TTL = 10.0
_RUN_CACHE_MAX = 1000


def _run_cache_get(key: tuple) -> dict | None:
    entry = _RUN_CACHE.get(key)
    if entry and _time_mod.monotonic() < entry[0]:
        return entry[1]
    _RUN_CACHE.pop(key, None)
    return None


def _run_cache_set(key: tuple, value: dict) -> None:
    if len(_RUN_CACHE) >= _RUN_CACHE_MAX:
        now = _time_mod.monotonic()
        expired = [k for k, (exp, _) in list(_RUN_CACHE.items()) if exp <= now]
        for k in expired:
            del _RUN_CACHE[k]
        while len(_RUN_CACHE) >= _RUN_CACHE_MAX:
            try:
                _RUN_CACHE.pop(next(iter(_RUN_CACHE)))
            except StopIteration:
                break
    _RUN_CACHE[key] = (_time_mod.monotonic() + _RUN_CACHE_TTL, value)


# ── Refund helpers ────────────────────────────────────────────────────────────

def _classify_error(error_msg: str) -> str:
    """Classify a service error as 'service_failure' (refund) or 'client_error' (no refund).

    Timeouts and 5xx responses are service failures — refund the caller.
    4xx responses are client/agent errors — caller sent bad params, no refund.
    """
    msg_lower = error_msg.lower()
    if "timeout" in msg_lower or "timed out" in msg_lower:
        return "service_failure"
    m = re.search(r'\b([45]\d{2})\b', error_msg)
    if m:
        code = int(m.group(1))
        return "client_error" if 400 <= code < 500 else "service_failure"
    return "service_failure"  # unknown exception → treat as service failure


async def _do_refund(
    db,
    user_id,
    credit_cost: int,
    service_slug: str,
    error_msg: str,
    endpoint: str,
    balance_after: int,
    refund_idempotency_key: str | None = None,
) -> int:
    """Restore credits, log the refund transaction, and fire wayf.call_refunded webhook.

    E8 (v0.7.8): if refund_idempotency_key is supplied, the partial unique
    index on credit_transactions(refund_uuid) prevents a duplicate refund
    even under concurrent execution. Callers without a stable key still get
    the historical at-most-once-per-attempt behavior.

    Returns the new credits balance.
    """
    async with db.transaction():
        if refund_idempotency_key:
            existing = await db.fetchval(
                "SELECT balance_after FROM credit_transactions "
                "WHERE refund_uuid = $1::uuid",
                refund_idempotency_key,
            )
            if existing is not None:
                # Already refunded — return the previous balance_after.
                return int(existing)
        row = await db.fetchrow(
            "UPDATE user_credits SET credits_balance = credits_balance + $1, updated_at = NOW() "
            "WHERE user_id = $2::uuid RETURNING credits_balance",
            credit_cost, user_id,
        )
        new_balance = row["credits_balance"] if row else balance_after + credit_cost
        await db.execute(
            """
            INSERT INTO credit_transactions
            (user_id, amount, balance_after, type, description, api_endpoint, service_id, refund_uuid)
            VALUES ($1::uuid, $2, $3, 'refund', $4, $5, $6, $7::uuid)
            """,
            user_id, credit_cost, new_balance,
            f"service_failure: {service_slug} — {error_msg[:100]}",
            endpoint, service_slug, refund_idempotency_key,
        )
    from core.credits import _dispatch_webhooks
    from main import app as _app
    asyncio.create_task(_dispatch_webhooks(
        str(user_id), "wayf.call_refunded", {
            "service_slug": service_slug,
            "credits_restored": credit_cost,
            "reason": "service_failure",
            "error": error_msg[:200],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    ))
    return new_balance


_STREAMING_SLUGS = {"groq", "together"}


async def _try_execute_managed(
    slug: str, params: dict, key: str
) -> tuple[object, str | None, int]:
    """Execute a managed adapter. Returns (result, error_msg, execution_ms).
    Non-assemblyai services retry once on timeout.
    """
    adapter = ADAPTERS[slug]
    result = None
    error_msg = None
    t0 = _time_mod.time()
    # E9 (v0.7.8): Python 3.11+ aliases asyncio.TimeoutError → TimeoutError but
    # on 3.10 they're distinct. Adapters that surface a httpx/requests
    # TimeoutError directly would slip past `except asyncio.TimeoutError` and
    # propagate as an unhandled exception. Catch both.
    if slug == "assemblyai":
        try:
            result = await asyncio.wait_for(adapter(params, key), timeout=35.0)
        except (asyncio.TimeoutError, TimeoutError):
            error_msg = "Service timeout"
        except Exception as _e:
            error_msg = str(_e)[:300]
    else:
        for _attempt in range(2):
            try:
                result = await asyncio.wait_for(adapter(params, key), timeout=10.0)
                break
            except (asyncio.TimeoutError, TimeoutError):
                if _attempt == 0:
                    continue
                error_msg = "Service timeout"
            except Exception as _e:
                error_msg = str(_e)[:300]
                break
    return result, error_msg, round((_time_mod.time() - t0) * 1000)


# Per-key concurrent SSE stream cap. SSE connections hold a worker open for the
# full upstream response duration; an unbounded number per key would let one
# caller exhaust connection capacity for everyone else.
_MAX_CONCURRENT_STREAMS_PER_KEY = 5
_active_streams: dict[str, int] = {}


async def _run_sse_stream(slug, params, svc_key, user_id, credit_cost, pool, service_used, calls_remaining, stream_owner_key):
    """Async generator producing SSE events for a streaming LLM /run call."""
    from services.managed import stream_groq, stream_together
    stream_fn = stream_groq if slug == "groq" else stream_together
    try:
        try:
            async for token in stream_fn(params, svc_key):
                yield f"data: {_json.dumps({'token': token, 'done': False})}\n\n"
        except Exception as exc:
            yield f"data: {_json.dumps({'error': str(exc)[:200], 'done': True})}\n\n"
            try:
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE user_credits SET credits_balance = credits_balance + $1, updated_at = NOW() "
                        "WHERE user_id = $2::uuid",
                        credit_cost, user_id,
                    )
            except Exception:
                pass
            return
        yield f"data: {_json.dumps({'token': '', 'done': True, 'service_used': service_used, 'calls_remaining': calls_remaining})}\n\n"
    finally:
        _active_streams[stream_owner_key] = max(0, _active_streams.get(stream_owner_key, 1) - 1)


# ── Pydantic models ───────────────────────────────────────────────────────────

class PayRequest(BaseModel):
    service_id: str
    service_owner: str = ""
    amount_usd: float = Field(default=0.001, gt=0)
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


# ── /run/intents — intent category catalogue ──────────────────────────────────

_INTENT_CATALOGUE = [
    {
        "category": "translation",
        "keywords": ["translate", "spanish", "french", "german", "language"],
        "routes_to": "deepl",
        "description": "Text translation between languages via DeepL",
    },
    {
        "category": "inference",
        "keywords": ["summarize", "explain", "write", "generate text", "chat"],
        "routes_to": "groq",
        "description": "LLM inference and text generation via Groq",
    },
    {
        "category": "research",
        "keywords": ["research", "deep dive", "fact check", "explain in detail"],
        "routes_to": "perplexity",
        "description": "In-depth research and comprehensive answers via Perplexity",
    },
    {
        "category": "image",
        "keywords": ["generate image", "draw", "picture", "illustration", "stable diffusion"],
        "routes_to": "stability",
        "description": "AI image generation via Stability AI",
    },
    {
        "category": "tts",
        "keywords": ["text to speech", "say this", "speak", "voice over", "narrate"],
        "routes_to": "elevenlabs",
        "description": "Text-to-speech synthesis via ElevenLabs",
    },
    {
        "category": "weather",
        "keywords": ["weather", "temperature", "forecast", "rain today", "humidity"],
        "routes_to": "openweather",
        "description": "Real-time weather data via OpenWeatherMap",
    },
    {
        "category": "financial",
        "keywords": ["stock price", "stock quote", "share price", "ticker symbol", "market data"],
        "routes_to": "alphavantage",
        "description": "Financial market data and stock quotes via Alpha Vantage",
    },
    {
        "category": "search",
        "keywords": ["search the web", "web search", "find articles", "look up", "latest news"],
        "routes_to": "brave",
        "description": "Web search and news retrieval via Brave Search",
    },
    {
        "category": "audio",
        "keywords": ["transcribe", "speech to text", "audio", "recording", "podcast"],
        "routes_to": "assemblyai",
        "description": "Audio transcription and speech recognition via AssemblyAI",
    },
]


@router.get("/run/intents")
async def list_run_intents():
    """Return all supported /run intent categories and the managed service each routes to."""
    return {
        "intents": _INTENT_CATALOGUE,
        "total": len(_INTENT_CATALOGUE),
        "protocol": "WayforthQL/2.0",
    }


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

    # For an 8-char key, "first 4 + last 4" reconstructs the whole key. Require
    # 16+ chars before showing both ends; shorter keys get a fully-masked preview.
    if len(raw_key) >= 16:
        preview = raw_key[:4] + "****" + raw_key[-4:]
    elif len(raw_key) >= 8:
        preview = raw_key[:2] + "****"
    else:
        preview = "****"

    try:
        encrypted, key_version = encrypt_api_key(raw_key, version=1)
    except Exception as _enc_err:
        logger.error("BYOK: failed to encrypt key for %s: %s", service_slug, _enc_err)
        raise HTTPException(status_code=500, detail={
            "error": "encryption_unavailable",
            "message": "Service key could not be stored securely. Check ENCRYPTION_KEY configuration.",
        })

    await db.execute("""
        INSERT INTO user_service_keys
            (user_id, service_slug, service_name, encrypted_key, key_preview, endpoint_url, default_method, active, key_version)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, true, $8)
        ON CONFLICT (user_id, service_slug)
        DO UPDATE SET
            service_name=EXCLUDED.service_name,
            encrypted_key=EXCLUDED.encrypted_key,
            key_preview=EXCLUDED.key_preview,
            endpoint_url=EXCLUDED.endpoint_url,
            default_method=EXCLUDED.default_method,
            active=true,
            key_version=EXCLUDED.key_version,
            updated_at=NOW()
    """, user_id, service_slug, service_name or service_slug, encrypted, preview, endpoint_url, default_method, key_version)

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

    if amount_usd <= 0:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_amount",
                "message": "amount_usd must be greater than 0",
            },
        )

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

    # Rail abstraction layer — developer's payment method is decoupled from the
    # provider's accepted settlement rail. Credits are always the developer's
    # unit of account; the underlying rail (card, USDC, CCTP) is Wayforth's concern.
    #
    # x402 fee model: providers receive 100% of their stated price.
    # Developer charge = provider_price / (1 − ROUTING_FEE) ≈ provider_price × 1.015233.
    # Wayforth keeps the difference as the routing fee.
    routing_fee_pct = ROUTING_FEE
    # amount_usd is the provider's stated price; fee is a markup on top, not a deduction.
    service_receives_usd = amount_usd
    routing_fee_usd = round(amount_usd / (1 - routing_fee_pct) - amount_usd, 8)
    wayforth_revenue = routing_fee_usd

    service_name = service["name"] if service else service_id
    x402_supported = service["x402_supported"] if service else False

    # TRACK C: x402 native — attempt real CDP settlement, fall back to Track A if unconfigured
    # v0.8.0 Item 1: replay protection landed in routers/x402.py (x402_execute
    # and x402_search) where the inbound X-PAYMENT header actually arrives.
    # This Track C path is the developer-facing /pay orchestrator and does not
    # receive X-PAYMENT directly; the EIP-3009 nonce + on-chain settlement
    # uniqueness handle replay at this layer.
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
    await check_rate_limit(str(_api_key_id), _tier)

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
                "docs": "https://gateway.wayforth.io/guide/",
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
            "SELECT encrypted_key, key_version, endpoint_url, default_method FROM user_service_keys "
            "WHERE user_id=$1::uuid AND service_slug=$2 AND active=true",
            user_id, service_slug,
        )
        if not byok_row:
            raise HTTPException(status_code=404, detail={
                "error": f"No BYOK key found for '{service_slug}'. Add one at /call/keys/add"
            })
        try:
            byok_key = decrypt_api_key(byok_row["encrypted_key"], byok_row["key_version"] or 1)
        except Exception as _dec_err:
            logger.error("BYOK: failed to decrypt key for %s: %s", service_slug, _dec_err)
            raise HTTPException(status_code=500, detail={"error": "decryption_failed"})

        req_endpoint = body.get("endpoint_url", "").strip() or byok_row["endpoint_url"]
        req_method = (body.get("method", "") or byok_row["default_method"] or "POST").upper()
        raw_extra_headers = body.get("headers", {}) or {}

        if not req_endpoint:
            raise HTTPException(status_code=400, detail={
                "error": "endpoint_url required (pass in body or store a default via /call/keys/add)"
            })
        # SSRF defense: same private-IP/internal-hostname checks we use for
        # webhook URLs. Without this, a caller could point endpoint_url at an
        # internal HTTPS service (or a public hostname that resolves to one)
        # and have Wayforth's egress fetch the response and return it verbatim.
        from core.url_validation import validate_external_url
        validate_external_url(req_endpoint, field_name="endpoint_url")

        # Header sanitation: strip caller-supplied headers that could subvert
        # the request (Authorization, Host, X-Forwarded-*, Content-Length,
        # Cookie). The user-provided BYOK key is the only Authorization we set.
        _FORBIDDEN_HEADER_PREFIXES = ("x-forwarded-", "x-real-", "x-amz-", "x-google-")
        _FORBIDDEN_HEADER_EXACT = {
            "authorization", "host", "content-length", "cookie", "proxy-authorization",
            "x-wayforth-api-key", "stripe-signature", "x-admin-key", "x-admin-token",
            "x-provider-token",
        }
        extra_headers = {}
        for k, v in raw_extra_headers.items():
            kl = str(k).lower().strip()
            if kl in _FORBIDDEN_HEADER_EXACT or any(kl.startswith(p) for p in _FORBIDDEN_HEADER_PREFIXES):
                continue
            if not isinstance(v, (str, int, float)):
                continue
            extra_headers[str(k)] = str(v)

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
        # The user-supplied extras come AFTER our Authorization so they can never
        # overwrite the BYOK auth header even if the sanitation pass missed a
        # variant (case-insensitive). We rebuild explicitly with our header last.
        call_headers = {**extra_headers, "Authorization": f"Bearer {byok_key}"}
        upstream_status: int | None = None
        error_msg = None
        raw_result = None
        _BYOK_MAX_BYTES = 1_048_576  # 1 MB hard cap on returned upstream body
        try:
            # follow_redirects=False (httpx default) — keep the egress pinned to
            # the validated endpoint and prevent post-validation rebinding via
            # 30x to a private host.
            async with _httpx.AsyncClient(timeout=15.0, follow_redirects=False) as _client:
                if req_method in ("GET", "DELETE"):
                    resp = await _client.request(req_method, req_endpoint, headers=call_headers, params=params)
                else:
                    resp = await _client.request(req_method, req_endpoint, headers=call_headers, json=params)
            upstream_status = resp.status_code
            _body_bytes = resp.content[:_BYOK_MAX_BYTES]
            _truncated = len(resp.content) > _BYOK_MAX_BYTES
            if resp.headers.get("content-type", "").startswith("application/json") and not _truncated:
                try:
                    raw_result = resp.json()
                except Exception:
                    raw_result = _body_bytes.decode("utf-8", errors="replace")
            else:
                raw_result = _body_bytes.decode("utf-8", errors="replace")
                if _truncated:
                    raw_result = {"_truncated": True, "_bytes": _BYOK_MAX_BYTES, "preview": raw_result}
            if resp.status_code >= 400:
                error_msg = f"Upstream {resp.status_code}: {str(raw_result)[:200]}"
        except _httpx.TimeoutException:
            error_msg = "Service timeout"
        except Exception as _e:
            error_msg = str(_e)[:300]

        execution_ms = round((_time.time() - start) * 1000)

        if error_msg:
            is_service_failure = upstream_status is None or upstream_status >= 500
            if is_service_failure:
                new_bal = await _do_refund(db, user_id, 1, service_slug, error_msg, "/execute", balance_after)
                raise HTTPException(status_code=503, detail={
                    "error": "Service unavailable",
                    "refunded": True,
                    "credits_restored": 1,
                    "calls_remaining": new_bal,
                })
            else:
                raise HTTPException(status_code=400, detail={
                    "error": error_msg,
                    "refunded": False,
                    "credits_restored": 0,
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

    # Normalise params: resolve aliases, inject defaults, wrap prompt→messages
    params, _missing = map_params(service_slug, params)
    if _missing:
        raise HTTPException(status_code=422, detail={
            "error": "missing_param",
            "missing": _missing,
            "hint": missing_param_hint(_missing),
        })

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
            "SELECT encrypted_key, key_version FROM user_service_keys WHERE user_id=$1::uuid AND service_slug=$2 AND active=true",
            user_id, service_slug,
        )
        if not row:
            raise HTTPException(status_code=404, detail={
                "error": "No API key found for service. Add one at /call/keys/add"
            })
        try:
            svc_key = decrypt_api_key(row["encrypted_key"], row["key_version"] or 1)
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
                logger.warning("managed adapter error: %s attempt=%d error=%s", service_slug, attempt, error_msg)
                break

    execution_ms = round((_time.time() - start) * 1000)

    _execute_fallback_from: str | None = None
    if error_msg and _classify_error(error_msg) == "service_failure":
        new_bal = await _do_refund(db, user_id, credit_cost, service_slug, error_msg, "/execute", balance_after)
        # Try one automatic fallback for managed-key calls
        _fb_slug = SERVICE_ALTERNATIVES.get(service_slug)
        if key_source == "managed" and _fb_slug and _fb_slug in SERVICE_CONFIGS:
            _fb_cfg = SERVICE_CONFIGS[_fb_slug]
            _fb_api_key = os.environ.get(_fb_cfg["key_var"], "")
            if _fb_api_key:
                _fb_mapped, _fb_miss = map_params(_fb_slug, params)
                if not _fb_miss:
                    _fb_cost = _fb_cfg["credits"]
                    _fb_ok, _fb_bal = await check_and_deduct_credits(
                        db, str(user_id), _fb_cost, "/execute",
                        service_id=_fb_slug, tx_type="execution",
                        agent_id=agent_id, api_key_id=str(_api_key_id),
                    )
                    if _fb_ok:
                        result, _fb_err, execution_ms = await _try_execute_managed(_fb_slug, _fb_mapped, _fb_api_key)
                        if _fb_err and _classify_error(_fb_err) == "service_failure":
                            await _do_refund(db, user_id, _fb_cost, _fb_slug, _fb_err, "/execute", _fb_bal)
                            result = None
                        elif _fb_err:
                            raise HTTPException(status_code=400, detail={"error": _fb_err, "refunded": False, "credits_restored": 0})
                        else:
                            _execute_fallback_from = service_slug
                            service_slug = _fb_slug
                            credit_cost = _fb_cost
                            balance_after = _fb_bal
        if result is None:
            raise HTTPException(status_code=503, detail={
                "error": "Service unavailable",
                "refunded": True,
                "credits_restored": credit_cost,
                "calls_remaining": new_bal,
            })
    elif error_msg:
        raise HTTPException(status_code=400, detail={
            "error": error_msg,
            "refunded": False,
            "credits_restored": 0,
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
    asyncio.create_task(_check_spend_anomaly(app.state.pool, str(user_id)))
    if _api_key_id:
        await _increment_calls(app.state.pool, str(_api_key_id))

    resp = {
        "status": "ok",
        "service": service_slug,
        "result": result,
        "credits_deducted": credit_cost,
        "execution_ms": execution_ms,
        "managed_services_available": len(SERVICE_CONFIGS),
        "priority": _tier in ("pro", "growth"),
    }
    if _execute_fallback_from:
        resp["fallback_from"] = _execute_fallback_from
        resp["fallback_reason"] = "service_unavailable"
    return resp


# ── /execute/batch — parallel multi-service execution ────────────────────────

async def _execute_one(call: dict, pool, user_id: str, api_key_id: str) -> dict:
    """Execute a single managed service call; used by /execute/batch via asyncio.gather."""
    slug = (call.get("slug") or "").strip().lower()
    params = call.get("params") or {}
    config = SERVICE_CONFIGS[slug]
    svc_key = os.environ.get(config["key_var"], "")

    if slug == "stability":
        credit_cost = 100 if params.get("quality") == "ultra" else 45
    else:
        credit_cost = config["credits"]

    t0 = _time_mod.time()

    if not svc_key:
        return {"slug": slug, "status": "error",
                "error": f"'{slug}' is not configured on this server.",
                "result": None, "execution_ms": 0}

    async with pool.acquire() as call_db:
        success, balance_after = await check_and_deduct_credits(
            call_db, user_id, credit_cost, "/execute/batch",
            service_id=slug, tx_type="execution", api_key_id=api_key_id,
        )

    if not success:
        return {"slug": slug, "status": "error",
                "error": "insufficient_credits",
                "result": None, "execution_ms": 0}

    result = None
    error_msg = None
    adapter = ADAPTERS[slug]
    timeout = 35.0 if slug == "assemblyai" else 10.0
    try:
        result = await asyncio.wait_for(adapter(params, svc_key), timeout=timeout)
    except asyncio.TimeoutError:
        error_msg = "Service timeout"
    except Exception as e:
        error_msg = str(e)[:300]

    execution_ms = round((_time_mod.time() - t0) * 1000)

    if error_msg:
        if _classify_error(error_msg) == "service_failure":
            async with pool.acquire() as refund_db:
                new_bal = await _do_refund(refund_db, user_id, credit_cost, slug, error_msg, "/execute/batch", balance_after)
            return {"slug": slug, "status": "error", "error": error_msg,
                    "refunded": True, "credits_restored": credit_cost,
                    "result": None, "execution_ms": execution_ms}
        else:
            return {"slug": slug, "status": "error", "error": error_msg,
                    "refunded": False, "credits_restored": 0,
                    "result": None, "execution_ms": execution_ms}

    return {"slug": slug, "status": "ok", "result": result, "execution_ms": execution_ms}


def _batch_call_cost(call: dict) -> int:
    """Credit cost for one batch call — must match _execute_one's pricing exactly."""
    slug = (call.get("slug") or "").strip().lower()
    config = SERVICE_CONFIGS[slug]
    if slug == "stability":
        params = call.get("params") or {}
        return 100 if params.get("quality") == "ultra" else 45
    return config["credits"]


@router.post("/execute/batch")
@limiter.limit("10/minute")
async def execute_batch(request: Request, db=Depends(get_db)):
    """Execute up to 5 managed service calls in parallel. Total credit cost is
    checked atomically up-front — if any call would push the balance negative,
    the whole batch is rejected before any external call is made."""
    from main import app

    api_key_header = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key_header:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key header required"})

    user_id, _api_key_id, _tier = await _resolve_user(db, api_key_header)
    await check_rate_limit(str(_api_key_id), _tier)

    body = await request.json()
    calls = body.get("calls", [])

    if not calls:
        raise HTTPException(status_code=400, detail={"error": "calls array is required and must not be empty"})

    if len(calls) > 5:
        raise HTTPException(status_code=422, detail={
            "error": "too_many_calls",
            "message": "Maximum 5 calls per batch request.",
            "received": len(calls),
            "limit": 5,
        })

    for call in calls:
        slug = (call.get("slug") or "").strip().lower()
        if not slug or slug not in SERVICE_CONFIGS:
            raise HTTPException(status_code=400, detail={
                "error": "unknown_service",
                "slug": slug or "(empty)",
                "message": f"Unknown managed service slug '{slug}'. "
                           "Use GET /services to browse available services.",
            })

    # Atomic batch credit gate — sum the cost of every call and verify the
    # caller can afford the whole batch before any _execute_one starts.
    # Without this, parallel per-call deductions in _execute_one would let
    # the first few calls succeed and the rest fail, producing partial
    # execution with no caller signal that the batch was incomplete.
    total_cost = sum(_batch_call_cost(c) for c in calls)
    balance_row = await db.fetchrow(
        "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid", str(user_id)
    )
    current_balance = balance_row["credits_balance"] if balance_row else 0
    if current_balance < total_cost:
        raise HTTPException(status_code=402, detail={
            "error": "insufficient_credits",
            "message": "Batch requires more credits than your current balance. "
                       "No calls were executed.",
            "credits_required": total_cost,
            "credits_balance": current_balance,
            "calls_in_batch": len(calls),
            "top_up_url": "https://wayforth.io/billing",
        })

    pool = app.state.pool
    batch_start = _time_mod.time()

    results = await asyncio.gather(
        *[_execute_one(c, pool, str(user_id), str(_api_key_id)) for c in calls]
    )

    total_ms = round((_time_mod.time() - batch_start) * 1000)
    calls_remaining = await compute_calls_remaining(db, str(_api_key_id))

    return {
        "results": list(results),
        "total_execution_ms": total_ms,
        "calls_remaining": calls_remaining,
    }


# ── /run — one-call runtime ───────────────────────────────────────────────────

@router.post("/run")
async def run_endpoint(request: Request, response: Response, db=Depends(get_db)):
    """Intent → search → rank → execute → result in one call."""
    import time as _time

    api_key_header = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key_header:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key header required"})

    user_id, _api_key_id, _tier = await _resolve_user(db, api_key_header)
    await check_rate_limit(str(_api_key_id), _tier)
    body = await request.json()

    intent = (body.get("intent") or "").strip()
    if not intent:
        raise HTTPException(status_code=400, detail={"error": "intent is required"})

    stream = bool(body.get("stream", False))

    _cache_key = (hashlib.sha256(api_key_header.encode()).hexdigest()[:16], intent)
    if not stream:
        _cached = _run_cache_get(_cache_key)
        if _cached is not None:
            response.headers["X-Wayforth-Cache"] = "hit"
            return _cached

    agent_id = _validate_agent_id(body.get("agent_id"))
    input_dict = body.get("input") or {}
    prefs = body.get("preferences") or {}
    category_filter = prefs.get("category") or detect_category_hint(intent)
    max_price = prefs.get("max_price_per_call")
    tier_min = int(prefs.get("tier_min", 2))

    # Map intent category → actual DB service categories before building the query.
    # Intent categories ("weather", "financial", "search") differ from DB categories
    # ("data", "finance", "image") — using raw intent category in WHERE returns 0 rows.
    _compatible_cats = INTENT_CATEGORY_MAP.get(category_filter) if category_filter else None

    run_start = _time.time()

    # Step 1 — Search: same DB query as GET /search
    conditions = [f"coverage_tier >= {tier_min}", "consecutive_failures < 3"]
    params_q: list = []
    idx = 1
    if _compatible_cats:
        conditions.append(f"category = ANY(${idx}::text[])")
        params_q.append(_compatible_cats)
        idx += 1
    elif category_filter:
        # User passed prefs.category directly (already a DB category value)
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
        # Build suggested_intents from categories that have configured managed keys
        _CATEGORY_EXAMPLES = {
            "inference":   "try: summarize this article",
            "translation": "try: translate to French",
            "weather":     "try: weather in London",
            "financial":   "try: stock price of AAPL",
            "search":      "try: search the web for latest AI news",
            "image":       "try: generate image of a sunset",
            "tts":         "try: text to speech hello world",
            "research":    "try: research quantum computing",
            "audio":       "try: transcribe this audio file",
        }
        _suggested: list[str] = []
        for _entry in _INTENT_CATALOGUE:
            if len(_suggested) >= 3:
                break
            _routes_to = _entry.get("routes_to", "")
            _cfg = SERVICE_CONFIGS.get(_routes_to)
            if _cfg and os.environ.get(_cfg["key_var"], ""):
                _example = _CATEGORY_EXAMPLES.get(_entry["category"])
                if _example:
                    _suggested.append(_example)

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
            "suggested_intents": _suggested,
            "supported_categories": len(_INTENT_CATALOGUE),
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

    # Build up-to-2 fallback candidates for automatic retry on 5xx (non-streaming only)
    _run_fallback_candidates: list[tuple[str, dict, dict, int, str]] = []
    if not stream:
        for _fc_svc in ranked:
            if len(_run_fallback_candidates) >= 2:
                break
            _fc_cat = _fc_svc.get("slug") or ""
            _fc_ms = CATALOG_TO_MANAGED.get(_fc_cat)
            if not _fc_ms or _fc_ms == selected_slug or _fc_ms not in SERVICE_CONFIGS:
                continue
            _fc_key = os.environ.get(SERVICE_CONFIGS[_fc_ms]["key_var"], "")
            if not _fc_key:
                continue
            if _compatible_cats and _fc_svc.get("category") not in _compatible_cats:
                continue
            _fc_mapped, _fc_miss = map_params(_fc_ms, input_dict)
            if _fc_miss:
                continue
            _fc_cfg = SERVICE_CONFIGS[_fc_ms]
            _fc_cost = 100 if (_fc_ms == "stability" and _fc_mapped.get("quality") == "ultra") else _fc_cfg["credits"]
            _run_fallback_candidates.append((_fc_ms, _fc_svc, _fc_mapped, _fc_cost, _fc_key))

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

    # ── Streaming path (inference LLM intents only) ───────────────────────────
    if stream:
        if selected_slug not in _STREAMING_SLUGS:
            async with db.transaction():
                await db.fetchrow(
                    "UPDATE user_credits SET credits_balance = credits_balance + $1, updated_at = NOW() "
                    "WHERE user_id = $2::uuid RETURNING credits_balance",
                    credit_cost, user_id,
                )
            return JSONResponse(status_code=400, content={
                "error": "streaming_not_supported",
                "intent_category": selected_svc.get("category", "unknown"),
            })

        # Per-key concurrent-stream cap — refund the just-deducted credits and
        # return 429 before opening another long-lived SSE connection.
        stream_owner_key = hashlib.sha256(api_key_header.encode()).hexdigest()[:16]
        active = _active_streams.get(stream_owner_key, 0)
        if active >= _MAX_CONCURRENT_STREAMS_PER_KEY:
            async with db.transaction():
                await db.fetchrow(
                    "UPDATE user_credits SET credits_balance = credits_balance + $1, updated_at = NOW() "
                    "WHERE user_id = $2::uuid RETURNING credits_balance",
                    credit_cost, user_id,
                )
            return JSONResponse(status_code=429, content={
                "error": "too_many_concurrent_streams",
                "message": f"Maximum {_MAX_CONCURRENT_STREAMS_PER_KEY} concurrent SSE streams per API key.",
                "active_streams": active,
                "limit": _MAX_CONCURRENT_STREAMS_PER_KEY,
            })
        _active_streams[stream_owner_key] = active + 1

        wri = selected_svc.get("wri_score")
        _service_used_sse = {
            "slug": selected_slug,
            "name": SERVICE_DISPLAY_NAMES.get(selected_slug, selected_slug),
            "wri_score": round(float(wri), 1) if wri else None,
            "category": selected_svc.get("category"),
            "credits_used": credit_cost,
        }
        gen = _run_sse_stream(
            selected_slug, mapped_params, svc_key, str(user_id),
            credit_cost, app.state.pool, _service_used_sse, _calls_remaining,
            stream_owner_key,
        )
        return StreamingResponse(gen, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    # ── Non-streaming execution with automatic fallback (max 2 retries) ─────────
    _fallback_from: str | None = None
    result, error_msg, execution_ms = await _try_execute_managed(selected_slug, mapped_params, svc_key)

    if error_msg and _classify_error(error_msg) == "service_failure":
        _fallback_from = selected_slug
        _all_failed_bal = await _do_refund(db, user_id, credit_cost, selected_slug, error_msg, "/run", balance_after)
        for _fc_slug, _fc_svc, _fc_params, _fc_cost, _fc_key in _run_fallback_candidates:
            _fc_ok, _fc_bal = await check_and_deduct_credits(
                db, str(user_id), _fc_cost, "/run",
                service_id=_fc_slug, tx_type="execution",
                agent_id=agent_id, api_key_id=str(_api_key_id),
            )
            if not _fc_ok:
                continue
            result, error_msg, execution_ms = await _try_execute_managed(_fc_slug, _fc_params, _fc_key)
            if error_msg and _classify_error(error_msg) == "service_failure":
                _all_failed_bal = await _do_refund(db, user_id, _fc_cost, _fc_slug, error_msg, "/run", _fc_bal)
                continue
            if error_msg:
                raise HTTPException(status_code=400, detail={"error": error_msg, "refunded": False, "credits_restored": 0})
            # Fallback succeeded — adopt its service context
            selected_slug = _fc_slug
            selected_svc = _fc_svc
            mapped_params = _fc_params
            credit_cost = _fc_cost
            balance_after = _fc_bal
            break
        else:
            raise HTTPException(status_code=503, detail={
                "error": "Service unavailable",
                "refunded": True,
                "credits_restored": credit_cost,
                "calls_remaining": _all_failed_bal,
                "service": _fallback_from,
            })
    elif error_msg:
        raise HTTPException(status_code=400, detail={
            "error": error_msg,
            "refunded": False,
            "credits_restored": 0,
        })

    asyncio.create_task(_update_search_signal(app.state.pool, str(user_id), selected_slug))
    asyncio.create_task(
        _maybe_dispatch_credits_low(app.state.pool, str(user_id), api_key_header, balance_after)
    )
    asyncio.create_task(_check_spend_anomaly(app.state.pool, str(user_id)))

    wri = selected_svc.get("wri_score")
    try:
        _health = await db.fetchrow(
            "SELECT avg_response_ms, error_rate FROM service_health WHERE slug = $1",
            selected_slug,
        )
        if _health and wri is not None:
            _adj = 0
            if (_health["error_rate"] or 0) > 0.3:
                _adj -= 10
            if (_health["avg_response_ms"] or 0) > 5000:
                _adj -= 5
            if _adj:
                wri = max(0.0, float(wri) + _adj)
    except Exception:
        pass

    response.headers["X-Wayforth-Cache"] = "miss"
    _run_result = {
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
    if _fallback_from:
        _run_result["fallback_from"] = _fallback_from
        _run_result["fallback_reason"] = "service_unavailable"
    _run_cache_set(_cache_key, _run_result)
    return _run_result
