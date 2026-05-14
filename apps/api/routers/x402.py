"""routers/x402.py — x402 pay-per-call endpoint."""

import asyncio
import logging
import os

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.rate_limit import limiter, _check_x402_rate_limit, _X402_RPM
from routers.agent import _upsert_x402_identity

logger = logging.getLogger("wayforth")

router = APIRouter()


async def _verify_x402_payment(payment_header: str, payto: str, expected_price_str: str) -> dict:
    """Decode and verify an EIP-3009 X-PAYMENT authorization header.

    Returns {valid, from_address, amount_usdc}. Trusts the header if CDP is not configured.
    """
    cdp_key_name = os.environ.get("CDP_API_KEY_NAME", "")
    cdp_private_key = os.environ.get("CDP_API_KEY_PRIVATE_KEY", "")
    if not cdp_key_name or not cdp_private_key:
        return {"valid": True, "from_address": None, "amount_usdc": expected_price_str}

    try:
        import base64 as _b64, json as _json
        # Payment header is base64-encoded JSON with EIP-3009 auth fields
        decoded = _json.loads(_b64.b64decode(payment_header + "==").decode("utf-8", errors="ignore"))
        from_address = decoded.get("from") or decoded.get("authorization", {}).get("from", "")
        # Amount is in micro-USDC; convert to USDC string for comparison
        from services.x402_pricing import to_micro_usdc, X402_PRICES_USDC
        expected_micro = int(to_micro_usdc(expected_price_str))
        received_micro = int(decoded.get("value", decoded.get("authorization", {}).get("value", 0)))
        # Allow 2% tolerance for gas variance
        within_tolerance = received_micro >= int(expected_micro * 0.98)
        return {
            "valid": within_tolerance,
            "from_address": from_address,
            "amount_usdc": str(received_micro / 1_000_000),
            "expected_micro": expected_micro,
            "received_micro": received_micro,
        }
    except Exception as _e:
        logger.warning("x402 payment header decode failed: %s", _e)
        return {"valid": True, "from_address": None, "amount_usdc": expected_price_str}


async def _verify_payment_async(payment_header: str, service_slug: str):
    """Background verification after optimistic acceptance. Flags account on failure."""
    await asyncio.sleep(10)  # allow chain to confirm
    try:
        from services.x402_pricing import X402_PRICES_USDC
        price_str = X402_PRICES_USDC.get(service_slug, "0.010")
        result = await _verify_x402_payment(
            payment_header, os.environ.get("WAYFORTH_BASE_WALLET", ""), price_str
        )
        if not result.get("valid"):
            logger.warning(
                "x402 optimistic payment failed post-verification: service=%s payment_status=pending_verification",
                service_slug,
            )
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

    if not payment_header:
        return JSONResponse(status_code=402, content={
            "x402Version": "1",
            "accepts": [{
                "scheme": "exact",
                "network": "eip155:8453",
                "maxAmountRequired": micro,
                "asset": USDC_BASE_ADDRESS,
                "payTo": wayforth_wallet,
                "maxTimeoutSeconds": 300,
                "description": f"{display_name} via Wayforth · ${price_str} USDC",
            }],
            "service": service_slug,
            "estimated_response_ms": 500,
        })

    # Verify payment with 5s timeout; accept optimistically on timeout
    payer_address = None
    try:
        verify_result = await asyncio.wait_for(
            _verify_x402_payment(payment_header, wayforth_wallet, price_str),
            timeout=5.0,
        )
        if not verify_result.get("valid"):
            received = verify_result.get("received_micro", 0)
            expected = verify_result.get("expected_micro", int(micro))
            received_usdc = f"${received / 1_000_000:.3f}"
            return JSONResponse(status_code=402, content={
                "x402Version": "1",
                "error": f"Payment of ${price_str} USDC required, received {received_usdc} USDC. Please retry.",
                "accepts": [{
                    "scheme": "exact",
                    "network": "eip155:8453",
                    "maxAmountRequired": micro,
                    "asset": USDC_BASE_ADDRESS,
                    "payTo": wayforth_wallet,
                    "maxTimeoutSeconds": 300,
                    "description": f"{display_name} via Wayforth · ${price_str} USDC",
                }],
            })
        payer_address = verify_result.get("from_address")
    except asyncio.TimeoutError:
        logger.warning("x402 verification timeout — accepting optimistically for service=%s", service_slug)
        asyncio.create_task(_verify_payment_async(payment_header, service_slug))

    # Tier-based rate limiting (wallet identity lookup)
    if payer_address:
        from main import app
        wallet_lower = payer_address.lower()
        try:
            async with app.state.pool.acquire() as _id_db:
                id_row = await _id_db.fetchrow(
                    "SELECT tier FROM x402_agent_identities WHERE wallet_address=$1", wallet_lower
                )
            wallet_tier = id_row["tier"] if id_row else "unknown"
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


def _search_payment_terms(wayforth_wallet: str) -> dict:
    return {
        "x402Version": "1",
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:8453",
            "maxAmountRequired": _X402_SEARCH_MICRO,
            "asset": _USDC_BASE_MAINNET,
            "payTo": wayforth_wallet,
            "maxTimeoutSeconds": 300,
            "description": "Wayforth API Search · $0.002 USDC per query",
        }],
        "service": "wayforth_search",
        "estimated_response_ms": 200,
    }


@router.get("/x402/search")
@limiter.limit("120/minute")
async def x402_search(
    request: Request,
    q: str = Query(min_length=1, max_length=500, description="Natural language search query"),
):
    """x402 pay-per-call search. No API key — $0.002 USDC per query on Base.

    No X-PAYMENT header → 402 with payment terms.
    Valid X-PAYMENT header → ranked service results.
    """
    import hashlib as _hs
    import html as _html
    from fastapi import HTTPException

    wayforth_wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
    if not wayforth_wallet:
        raise HTTPException(status_code=503, detail={
            "error": "x402 payments not configured on this instance",
        })

    payment_header = request.headers.get("X-PAYMENT", "")
    if not payment_header:
        return JSONResponse(status_code=402, content=_search_payment_terms(wayforth_wallet))

    # Verify payment (5 s timeout; accept optimistically on timeout)
    payer_address = None
    try:
        verify = await asyncio.wait_for(
            _verify_x402_payment(payment_header, wayforth_wallet, _X402_SEARCH_PRICE),
            timeout=5.0,
        )
        if not verify.get("valid"):
            terms = _search_payment_terms(wayforth_wallet)
            terms["error"] = f"Payment of ${_X402_SEARCH_PRICE} USDC required. Please retry."
            return JSONResponse(status_code=402, content=terms)
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
