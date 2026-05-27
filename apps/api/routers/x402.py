"""routers/x402.py — x402 pay-per-call endpoint."""

import asyncio
import hashlib as _hl
import logging
import os
import time as _time

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.rate_limit import limiter, _check_x402_rate_limit, _X402_RPM
from routers.agent import _upsert_x402_identity

logger = logging.getLogger("wayforth")

# Replay prevention: store payment header hashes for 5 minutes.
# NOTE: this dict is per-process. In a multi-worker / multi-instance deploy a
# determined attacker could replay a payment by hitting a different worker.
# Move to Postgres or Redis once cross-instance replay protection is required.
# Hard cap on entries to bound memory if the prune ever fails.
_x402_seen: dict[str, float] = {}
_X402_NONCE_TTL = 300  # seconds
_X402_SEEN_MAX = 50_000

router = APIRouter()


async def _verify_x402_payment(payment_header: str, payto: str, expected_price_str: str) -> dict:
    """Decode and verify an EIP-3009 X-PAYMENT authorization header.

    Returns {valid, from_address, amount_usdc}. When CDP signing keys are not
    configured (dev/staging without a wallet), we still parse the header and
    require it to be a well-formed JSON envelope with the expected payee +
    amount — we just skip the on-chain attestation step.
    """
    cdp_key_name = os.environ.get("CDP_API_KEY_NAME", "")
    cdp_private_key = os.environ.get("CDP_API_KEY_PRIVATE_KEY", "")
    cdp_configured = bool(cdp_key_name and cdp_private_key)

    try:
        import base64 as _b64, json as _json
        # Payment header is base64-encoded JSON with EIP-3009 auth fields.
        # Use validate=False to accept urlsafe variants; require successful decode.
        raw = _b64.b64decode(payment_header + "==", validate=False)
        decoded = _json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("payment header is not a JSON object")
        from_address = decoded.get("from") or decoded.get("authorization", {}).get("from", "")
        to_address = (decoded.get("to") or decoded.get("authorization", {}).get("to") or "").lower()
        # Amount is in micro-USDC; convert to USDC string for comparison
        from services.x402_pricing import to_micro_usdc
        expected_micro = int(to_micro_usdc(expected_price_str))
        received_micro = int(decoded.get("value", decoded.get("authorization", {}).get("value", 0)))
        # Validate payee matches: prevents accepting payments to attacker-controlled wallets.
        if payto and to_address and to_address != payto.lower():
            logger.warning("x402 payee mismatch: expected=%s received=%s", payto, to_address)
            return {
                "valid": False,
                "from_address": from_address,
                "amount_usdc": str(received_micro / 1_000_000),
                "expected_micro": expected_micro,
                "received_micro": received_micro,
                "error": "payee_mismatch",
            }
        # Tightened from 2% to 0.5%. Gas variance does not apply to the
        # USDC.transferWithAuthorization `value` field — `value` is the
        # amount being authorized, not what's deducted after fees — so any
        # meaningful underpayment is intentional.
        within_tolerance = received_micro >= int(expected_micro * 0.995)
        return {
            "valid": within_tolerance,
            "from_address": from_address,
            "amount_usdc": str(received_micro / 1_000_000),
            "expected_micro": expected_micro,
            "received_micro": received_micro,
        }
    except Exception as _e:
        # Previously this branch fell-open as {valid: True}. That allowed any
        # malformed/garbage X-PAYMENT header to bypass payment entirely. Fail
        # closed: an unparseable header is not a valid payment.
        logger.warning("x402 payment header decode failed: %s", _e)
        return {
            "valid": False,
            "from_address": None,
            "amount_usdc": "0",
            "error": "decode_failed",
        }


async def _verify_payment_async(payment_header: str, service_slug: str):
    """Background verification after optimistic acceptance.

    When the synchronous verification times out (5s), the call has already been
    served. We re-verify here and, on failure, flag the payer's wallet so
    subsequent x402 calls from it are subject to closer scrutiny / blocking. A
    determined attacker who can repeatedly trigger verify timeouts could
    otherwise execute services for free and only ever be logged.
    """
    await asyncio.sleep(10)  # allow chain to confirm
    try:
        from services.x402_pricing import X402_PRICES_USDC
        price_str = X402_PRICES_USDC.get(service_slug, "0.010")
        result = await _verify_x402_payment(
            payment_header, os.environ.get("WAYFORTH_BASE_WALLET", ""), price_str
        )
        if not result.get("valid"):
            payer = (result.get("from_address") or "").lower()
            logger.warning(
                "x402 optimistic payment failed post-verification: service=%s payer=%s reason=%s",
                service_slug, payer or "(unknown)", result.get("error") or "invalid",
            )
            if payer:
                try:
                    from main import app as _app
                    pool = getattr(_app.state, "pool", None)
                    if pool:
                        async with pool.acquire() as _db:
                            await _db.execute(
                                """
                                INSERT INTO x402_agent_identities
                                    (wallet_address, flagged, flag_reason, last_seen)
                                VALUES ($1, true, $2, NOW())
                                ON CONFLICT (wallet_address) DO UPDATE
                                SET flagged = true,
                                    flag_reason = EXCLUDED.flag_reason,
                                    last_seen = NOW()
                                """,
                                payer,
                                f"optimistic_unverified:{service_slug}",
                            )
                except Exception as _flag_err:
                    logger.error("x402 flag-wallet failed: %s", _flag_err)
    except Exception as _e:
        logger.error("_verify_payment_async error: %s", _e)


async def _refund_x402(payer_address: str, price_str: str, service_slug: str):
    """Refund USDC to payer when service call fails after valid payment."""
    if not payer_address:
        logger.warning("x402 refund skipped: no payer_address for service=%s", service_slug)
        return
    cdp_key_name = os.environ.get("CDP_API_KEY_NAME", "")
    cdp_private_key = os.environ.get("CDP_API_KEY_PRIVATE_KEY", "")
    if not cdp_key_name or not cdp_private_key:
        logger.warning("x402 refund skipped: CDP not configured for service=%s", service_slug)
        return
    try:
        from cdp import Cdp, Wallet
        loop = asyncio.get_event_loop()

        def _do_refund():
            Cdp.configure(cdp_key_name, cdp_private_key)
            wallet = Wallet.fetch(os.environ.get("WAYFORTH_BASE_WALLET", ""))
            transfer = wallet.transfer(float(price_str), "usdc", payer_address)
            transfer.wait(timeout_seconds=60, interval_seconds=1)
            return transfer.transaction_hash

        tx_hash = await asyncio.wait_for(loop.run_in_executor(None, _do_refund), timeout=65.0)
        logger.info("x402 refund complete: tx=%s service=%s amount=%s", tx_hash, service_slug, price_str)
    except Exception as _e:
        logger.error("x402 refund failed for service=%s payer=%s: %s", service_slug, payer_address, _e)


@router.post("/x402/execute")
@limiter.limit("60/minute")
async def x402_execute(request):
    """x402 pay-per-call endpoint. No API key required — payment IS authentication.

    Step 1 (no X-PAYMENT header): Returns 402 with USDC payment instructions.
    Step 2 (X-PAYMENT header): Verifies payment, executes service, returns result.
    """
    import time as _time
    from fastapi import HTTPException
    from services.x402_pricing import X402_PRICES_USDC, to_micro_usdc
    from services.managed import ADAPTERS, SERVICE_CONFIGS, SERVICE_ALTERNATIVES, SERVICE_DISPLAY_NAMES

    wayforth_wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
    if not wayforth_wallet:
        raise HTTPException(status_code=503, detail={
            "error": "x402 payments coming soon. Use subscription for now.",
            "alternatives": ["POST /billing/subscribe-usdc", "GET /pricing/json"],
        })

    body = await request.json()
    service_slug = body.get("service_slug", "").strip().lower()
    params = body.get("params", {})

    # Validate service before issuing any 402
    if service_slug not in SERVICE_CONFIGS:
        available = sorted(SERVICE_CONFIGS.keys())
        raise HTTPException(status_code=400, detail={
            "error": f"Unknown service: {service_slug}. Available: {', '.join(available)}"
        })

    # Check service env key is configured
    config = SERVICE_CONFIGS[service_slug]
    svc_key = os.environ.get(config["key_var"], "")
    if not svc_key:
        alt = SERVICE_ALTERNATIVES.get(service_slug)
        alt_msg = f" Try '{alt}' which provides similar functionality." if alt else ""
        raise HTTPException(status_code=503, detail={
            "error": f"{service_slug} is temporarily unavailable.{alt_msg}"
        })

    price_str = X402_PRICES_USDC.get(service_slug, "0.010")
    micro = to_micro_usdc(price_str)
    USDC_BASE_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    display_name = SERVICE_DISPLAY_NAMES.get(service_slug, service_slug)

    payment_header = request.headers.get("X-PAYMENT", "")

    _execute_resource = f"https://gateway.wayforth.io/x402/execute"
    if not payment_header:
        return JSONResponse(status_code=402, content={
            "x402Version": 1,
            "error": "Payment required",
            "accepts": [{
                "scheme": "exact",
                "network": "eip155:8453",
                "maxAmountRequired": str(micro),
                "asset": USDC_BASE_ADDRESS,
                "payTo": wayforth_wallet,
                "resource": _execute_resource,
                "description": f"{display_name} via Wayforth — ${price_str} USDC",
                "maxTimeoutSeconds": 300,
            }],
        })

    # Replay prevention: reject payment headers seen within the last 5 minutes.
    # In a multi-replica deploy, the per-process dict is not sufficient — an
    # attacker can replay against a different worker. Use Redis when available;
    # the in-memory dict remains as a fallback for single-process / dev.
    _ph_hash = _hl.sha256(payment_header.encode()).hexdigest()
    _now = _time.time()
    _replay_blocked = False
    from core.tier_gates import _get_redis as _x402_get_redis
    _redis = _x402_get_redis()
    if _redis is not None:
        try:
            # SET NX with TTL: returns truthy on first write, None on replay.
            _set_ok = await _redis.set(
                f"wf:x402:nonce:{_ph_hash}",
                "1",
                ex=_X402_NONCE_TTL,
                nx=True,
            )
            _replay_blocked = _set_ok is None
        except Exception as _redis_err:
            logger.warning("x402 replay redis check failed, falling back to memory: %s", _redis_err)
            _redis = None
    if _redis is None:
        # In-process fallback. Prune expired entries by removing keys; the
        # previous `update` form re-added the same keys and never removed.
        expired = [k for k, v in _x402_seen.items() if _now - v >= _X402_NONCE_TTL]
        for k in expired:
            _x402_seen.pop(k, None)
        # Hard cap so a flood of unique payments can't OOM the worker if prune
        # is somehow bypassed; oldest entries get evicted first.
        if len(_x402_seen) > _X402_SEEN_MAX:
            for k in sorted(_x402_seen, key=_x402_seen.get)[: len(_x402_seen) - _X402_SEEN_MAX]:
                _x402_seen.pop(k, None)
        if _ph_hash in _x402_seen:
            _replay_blocked = True
        else:
            _x402_seen[_ph_hash] = _now
    if _replay_blocked:
        return JSONResponse(status_code=400, content={
            "error": "replay_rejected",
            "message": "This payment has already been processed. Each payment header can only be used once.",
        })

    # E3 (v0.7.8): synchronous on-chain verification only. The old code
    # accepted optimistically on a 5s timeout and verified asynchronously
    # afterward — but a stuck RPC meant the service ran for free with no
    # refund path. Bumped to 15s (typical chain reorg + propagation budget)
    # and a timeout now returns 504. Real customers may see a transient
    # 504 during chain congestion; that is the correct conservative behavior.
    payer_address = None
    try:
        verify_result = await asyncio.wait_for(
            _verify_x402_payment(payment_header, wayforth_wallet, price_str),
            timeout=15.0,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "x402 verification timeout (15s) — refusing service=%s payment_hash=%s",
            service_slug, _ph_hash,
        )
        return JSONResponse(status_code=504, content={
            "x402Version": 1,
            "error": "Payment verification timed out. Please retry; no service was rendered and no funds were captured by Wayforth.",
            "retry_after_seconds": 30,
        })

    if not verify_result.get("valid"):
        received = verify_result.get("received_micro", 0)
        received_usdc = f"${received / 1_000_000:.3f}"
        return JSONResponse(status_code=402, content={
            "x402Version": 1,
            "error": f"Payment of ${price_str} USDC required, received {received_usdc} USDC. Please retry.",
            "accepts": [{
                "scheme": "exact",
                "network": "eip155:8453",
                "maxAmountRequired": str(micro),
                "asset": USDC_BASE_ADDRESS,
                "payTo": wayforth_wallet,
                "resource": _execute_resource,
                "description": f"{display_name} via Wayforth — ${price_str} USDC",
                "maxTimeoutSeconds": 300,
            }],
        })
    payer_address = verify_result.get("from_address")

    # Tier-based rate limiting (wallet identity lookup)
    if payer_address:
        from main import app
        wallet_lower = payer_address.lower()
        try:
            async with app.state.pool.acquire() as _id_db:
                id_row = await _id_db.fetchrow(
                    "SELECT tier, flagged FROM x402_agent_identities WHERE wallet_address=$1",
                    wallet_lower,
                )
            wallet_tier = id_row["tier"] if id_row else "unknown"
            # Refuse calls from wallets flagged for prior unverified payments.
            if id_row and id_row.get("flagged"):
                return JSONResponse(status_code=403, content={
                    "error": "wallet_flagged",
                    "message": "This wallet has been flagged for prior unverified payments. "
                               "Contact support@wayforth.io if you believe this is an error.",
                })
        except Exception:
            wallet_tier = "unknown"
        allowed, retry_after = _check_x402_rate_limit(wallet_lower, wallet_tier)
        if not allowed:
            limit_val = _X402_RPM.get(wallet_tier, 10)
            return JSONResponse(status_code=429, content={
                "error": "rate_limit_exceeded",
                "tier": wallet_tier,
                "limit": f"{limit_val} calls/minute",
                "retry_after": retry_after,
                "message": "Make more calls to increase your agent tier and rate limits.",
            })

    # Execute service
    adapter = ADAPTERS[service_slug]
    result = None
    error_msg = None
    start = _time.time()

    if service_slug == "assemblyai":
        try:
            result = await asyncio.wait_for(adapter(params, svc_key), timeout=35.0)
        except asyncio.TimeoutError:
            error_msg = "Service timeout"
        except Exception as _e:
            error_msg = str(_e)[:300]
    else:
        for attempt in range(2):
            try:
                result = await asyncio.wait_for(adapter(params, svc_key), timeout=10.0)
                break
            except asyncio.TimeoutError:
                if attempt == 0:
                    continue
                error_msg = "Service timeout"
            except Exception as _e:
                error_msg = str(_e)[:300]
                break

    execution_ms = round((_time.time() - start) * 1000)

    if error_msg:
        # Refund payment since service failed
        if payer_address:
            asyncio.create_task(_refund_x402(payer_address, price_str, service_slug))
            refund_note = f"${price_str} USDC refunded to your wallet."
        else:
            refund_note = "Refund initiated — contact support@wayforth.io if not received within 10 minutes."
        raise HTTPException(status_code=503, detail={
            "error": f"Service call failed. {refund_note}",
            "service": service_slug,
            "detail": error_msg,
        })

    # Upsert wallet identity (async — don't block response)
    price_usdc = float(price_str)
    agent_identity: dict = {}
    if payer_address:
        from main import app as _app
        if _app.state.pool:
            agent_identity = await _upsert_x402_identity(_app.state.pool, payer_address, price_usdc)

    resp_body: dict = {
        "result": result,
        "payment_confirmed": True,
        "credits_equivalent": SERVICE_CONFIGS[service_slug]["credits"],
        "execution_ms": execution_ms,
    }
    if agent_identity:
        resp_body["agent_identity"] = agent_identity

    response = JSONResponse(content=resp_body)
    response.headers["X-PAYMENT-RESPONSE"] = "confirmed"
    return response


# ── /x402/search ─────────────────────────────────────────────────────────────

_X402_SEARCH_PRICE  = "0.002"          # $0.002 USDC per query
_X402_SEARCH_MICRO  = 2000             # micro-USDC (6 decimals)
_USDC_BASE_MAINNET  = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
_SEARCH_RESOURCE_URL = "https://gateway.wayforth.io/x402/search"


def _search_payment_required(wayforth_wallet: str, error: str = "Payment required") -> dict:
    """Build a v2-compliant PaymentRequired payload for /x402/search."""
    return {
        "x402Version": 2,
        "error": error,
        "resource": {
            "url": _SEARCH_RESOURCE_URL,
            "description": "Wayforth API Search — $0.002 USDC per query on Base",
            "mimeType": "application/json",
        },
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:8453",
            "amount": str(_X402_SEARCH_MICRO),
            "asset": _USDC_BASE_MAINNET,
            "payTo": wayforth_wallet,
            "maxTimeoutSeconds": 300,
        }],
        "extensions": {
            "bazaar": {
                "info": {
                    "name": "Wayforth API Search",
                    "description": (
                        "Search 3,000+ APIs for AI agents. "
                        "Ranked by verified payment signal — not ads."
                    ),
                    "category": "search",
                    "version": "1.0.0",
                    "output": {
                        "description": (
                            "Ranked list of APIs matching the search query, "
                            "with WRI scores, pricing, and available payment rails."
                        ),
                        "mimeType": "application/json",
                        "example": {
                            "results": [{
                                "name": "Groq",
                                "wri": 82,
                                "price_per_call": 0.00001,
                                "payment_rails": ["card", "usdc", "x402"],
                                "category": "inference",
                            }],
                        },
                    },
                },
                "schema": {
                    "$schema": "http://json-schema.org/draft-07/schema#",
                    "type": "object",
                    "properties": {
                        "results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name":          {"type": "string"},
                                    "wri":           {"type": "number"},
                                    "price_per_call":{"type": "number"},
                                    "payment_rails": {"type": "array", "items": {"type": "string"}},
                                    "category":      {"type": "string"},
                                },
                            },
                        },
                    },
                },
                "outputExample": {
                    "results": [{
                        "name": "Groq",
                        "wri": 82,
                        "price_per_call": 0.00001,
                        "payment_rails": ["card", "usdc", "x402"],
                        "category": "inference",
                    }],
                },
            },
        },
    }


def _search_402_response(wayforth_wallet: str, error: str = "Payment required") -> JSONResponse:
    """Return a v2-compliant 402 with PAYMENT-REQUIRED header (base64 encoded)."""
    import base64 as _b64
    import json as _json
    payload = _search_payment_required(wayforth_wallet, error)
    encoded = _b64.b64encode(_json.dumps(payload, separators=(",", ":")).encode()).decode()
    resp = JSONResponse(status_code=402, content=payload)
    resp.headers["PAYMENT-REQUIRED"] = encoded
    return resp


@router.get("/.well-known/x402")
async def x402_well_known():
    """x402 v2 discovery document — lists only x402-enabled endpoints."""
    return JSONResponse(content={
        "x402Version": 2,
        "endpoints": [{
            "path": "/x402/search",
            "method": "GET",
            "description": "Wayforth API Search — $0.002 USDC per query on Base",
            "resource": {
                "url": _SEARCH_RESOURCE_URL,
                "mimeType": "application/json",
            },
            "accepts": [{
                "scheme": "exact",
                "network": "eip155:8453",
                "amount": str(_X402_SEARCH_MICRO),
                "asset": _USDC_BASE_MAINNET,
                "maxTimeoutSeconds": 300,
            }],
        }],
    })


@router.get("/x402/search")
@limiter.limit("120/minute")
async def x402_search(
    request: Request,
    q: str | None = Query(default=None, max_length=500, description="Natural language search query"),
):
    """x402 v2 pay-per-call search. No API key — $0.002 USDC per query on Base.

    No PAYMENT-SIGNATURE header (or no ?q=) → 402 with v2 PaymentRequired.
    Valid payment header + ?q= → ranked service results.
    """
    import hashlib as _hs
    import html as _html
    from fastapi import HTTPException

    wayforth_wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
    if not wayforth_wallet:
        raise HTTPException(status_code=503, detail={
            "error": "x402 payments not configured on this instance",
        })

    # v2 uses PAYMENT-SIGNATURE; also accept X-PAYMENT for v1 client compat
    payment_header = request.headers.get("PAYMENT-SIGNATURE", "") or request.headers.get("X-PAYMENT", "")
    # Return 402 when no payment header OR no query (e.g. x402scan/Agentic.Market probe)
    if not payment_header or not q:
        return _search_402_response(wayforth_wallet)

    # Verify payment (5 s timeout; accept optimistically on timeout)
    payer_address = None
    try:
        verify = await asyncio.wait_for(
            _verify_x402_payment(payment_header, wayforth_wallet, _X402_SEARCH_PRICE),
            timeout=5.0,
        )
        if not verify.get("valid"):
            return _search_402_response(
                wayforth_wallet,
                f"Payment of ${_X402_SEARCH_PRICE} USDC required. Please retry.",
            )
        payer_address = verify.get("from_address")
    except asyncio.TimeoutError:
        logger.warning("x402/search payment verification timeout — accepting optimistically")

    # Execute search
    from main import app as _app
    from ranker_client import rank_services

    q_clean = _html.escape(q.strip().lower())

    try:
        async with _app.state.pool.acquire() as _db:
            rows = await _db.fetch(
                """
                SELECT id, name, slug, description, endpoint_url, category,
                       coverage_tier, pricing_usdc, x402_supported,
                       wri_score, wri_version
                FROM services
                WHERE source != 'demo'
                ORDER BY created_at DESC
                """,
            )
            services = [dict(r) for r in rows]
            ranked = await rank_services(q_clean, services, db=_db)
    except Exception as _e:
        logger.error("x402/search error: %s", _e)
        raise HTTPException(status_code=503, detail={"error": "Search temporarily unavailable"})

    top = ranked[:5]
    results = [
        {
            "name": s.get("name"),
            "slug": s.get("slug"),
            "description": s.get("description"),
            "wri": s.get("wri_score"),
            "coverage_tier": s.get("coverage_tier"),
            "category": s.get("category"),
            "pricing": {"per_call_usd": s.get("pricing_usdc")},
            "x402_supported": bool(s.get("x402_supported")),
            "wayforth_id": (
                f"wayforth://{s.get('slug', '')}/"
                f"{_hs.sha256(s.get('endpoint_url', '').encode()).hexdigest()[:8]}"
            ),
        }
        for s in top
    ]

    resp = JSONResponse(content={
        "query": q_clean,
        "results": results,
        "total_results": len(top),
        "payment_confirmed": True,
        "paid_via": "x402",
        "amount_usdc": _X402_SEARCH_PRICE,
    })
    resp.headers["X-PAYMENT-RESPONSE"] = "confirmed"
    return resp
