"""routers/billing/usdc.py — USDC subscription flow, Base watcher, subscribe/topup-usdc."""

import asyncio
import hashlib
import logging
import math
import os
import secrets
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from core.credits import PLANS, CREDITS_PER_CALL, _dispatch_webhooks, _maybe_grant_founding_bonus
from core.db import get_db
from core.rate_limit import limiter
from core.tier_gates import require_tier

logger = logging.getLogger("wayforth")

router = APIRouter()

# ── Rail kill-switch (v0.8.5 security hardening) ──────────────────────────────
# The USDC watcher matched transfers by amount only, re-scanned from genesis,
# and had no tx dedup (FINDING-002); top-up did not bind the payer (FINDING-003).
# Both are exploitable against Base MAINNET. The rail stays HARD-DISABLED until
# the Phase 2 payer-binding + block-advancement + dedup work is deployed and
# verified. Default off; requires explicit opt-in env to enable.
USDC_RAIL_ENABLED = os.environ.get("WAYFORTH_USDC_ENABLED", "false").lower() == "true"


def _usdc_disabled_response():
    raise HTTPException(status_code=503, detail={
        "error": "usdc_rail_disabled",
        "message": (
            "USDC billing is temporarily disabled pending a payment-verification "
            "upgrade. Use card billing for now."
        ),
    })


async def _verify_tx_on_base(tx_hash: str, expected_recipient: str, expected_amount_usdc: str,
                             expected_sender: str | None = None) -> dict:
    """Verify a USDC transfer tx_hash on Base using eth_getTransactionReceipt.

    Returns {valid, reason, from_address}. **Fails closed** on RPC error/timeout/
    misconfig — credits are real money, so an unverifiable transaction is treated
    as not-paid. Set BASE_RPC_URL to a reliable endpoint in production.

    FINDING-003: when `expected_sender` is supplied, the transfer's on-chain
    sender (ERC-20 Transfer topics[1]) must match it, so a public tx hash can't
    be claimed by an account other than the one that actually paid. The admin
    reconciliation path passes expected_sender=None after manual review.
    """
    BASE_RPC_URL = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
    USDC_ADDRESS = os.environ.get("USDC_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    if not BASE_RPC_URL:
        logger.error("_verify_tx_on_base called with no BASE_RPC_URL — refusing to confirm")
        return {"valid": False, "reason": "rpc_not_configured"}

    try:
        payload = {
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_getTransactionReceipt",
            "params": [tx_hash],
        }
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(BASE_RPC_URL, json=payload)
        if r.status_code != 200:
            logger.warning("_verify_tx_on_base RPC HTTP %d for %s", r.status_code, tx_hash[:12])
            return {"valid": False, "reason": "rpc_unavailable"}

        receipt = r.json().get("result")
        if not receipt:
            return {"valid": False, "reason": "transaction_not_found"}
        if receipt.get("status") != "0x1":
            return {"valid": False, "reason": "transaction_failed_on_chain"}

        # Find Transfer log from USDC contract to expected_recipient
        for log in receipt.get("logs", []):
            if log.get("address", "").lower() != USDC_ADDRESS.lower():
                continue
            topics = log.get("topics", [])
            if not topics or topics[0].lower() != TRANSFER_TOPIC.lower():
                continue
            # ERC-20 Transfer: topics[1]=from, topics[2]=to (each padded 32 bytes)
            if len(topics) < 3:
                continue
            from_addr = "0x" + topics[1][-40:]
            to_addr = "0x" + topics[2][-40:]
            if to_addr.lower() != expected_recipient.lower():
                continue
            # FINDING-003: bind to the declared payer when provided.
            if expected_sender and from_addr.lower() != expected_sender.lower():
                return {
                    "valid": False,
                    "reason": "payer_mismatch",
                    "from_address": from_addr,
                }
            # Amount is in log data (USDC has 6 decimals). Tightened from 2% to 0.5%
            # tolerance — gas variance does not apply to USDC.transferWithAuthorization
            # value; 2% was a footgun that allowed silent underpayment at scale.
            try:
                amount_micro = int(log.get("data", "0x0"), 16)
                amount_usdc = amount_micro / 1_000_000
                expected = float(expected_amount_usdc)
                if abs(amount_usdc - expected) / expected <= 0.005:
                    return {"valid": True, "reason": "confirmed",
                            "amount_usdc": str(amount_usdc), "from_address": from_addr}
                return {
                    "valid": False,
                    "reason": f"amount_mismatch: expected ${expected:.6f}, received ${amount_usdc:.6f}",
                    "from_address": from_addr,
                }
            except (ValueError, ZeroDivisionError):
                continue

        return {"valid": False, "reason": "no_matching_usdc_transfer_found"}

    except httpx.TimeoutException:
        logger.warning("_verify_tx_on_base RPC timeout for %s", tx_hash[:12])
        return {"valid": False, "reason": "rpc_timeout"}
    except Exception as _e:
        logger.error("_verify_tx_on_base error: %s", _e)
        return {"valid": False, "reason": "rpc_error"}


async def _activate_usdc_subscription(pool, reference_id: str, tx_hash: str,
                                       plan: str, credits_total: int,
                                       payer_address: str, api_key_id: str,
                                       bonus_credits: int = 0):
    """Activate a USDC subscription after payment confirmation."""
    async with pool.acquire() as db:
        async with db.transaction():
            await db.execute("""
                UPDATE usdc_payments
                SET status = 'confirmed', tx_hash = $1, confirmed_at = NOW(),
                    bonus_credits = $3, updated_at = NOW()
                WHERE reference_id = $2
            """, tx_hash, reference_id, bonus_credits)
            await db.execute("""
                UPDATE api_keys
                SET payment_rail = 'usdc',
                    tier = $1,
                    subscription_expires_at = NOW() + INTERVAL '30 days',
                    usdc_wallet_address = $2,
                    subscription_status = 'active'
                WHERE id = $3::uuid
            """, plan, payer_address, api_key_id)
            user_row = await db.fetchrow(
                "SELECT user_id FROM api_keys WHERE id = $1::uuid", api_key_id
            )
            if user_row:
                plan_def = PLANS.get(plan, PLANS["free"])
                await db.execute("""
                    INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                    VALUES ($1::uuid, $2, $2, $3, 'usdc')
                    ON CONFLICT (user_id) DO UPDATE
                    SET credits_balance = user_credits.credits_balance + $2,
                        lifetime_credits = user_credits.lifetime_credits + $2,
                        package_tier = $3,
                        payment_method = 'usdc',
                        updated_at = NOW()
                """, str(user_row["user_id"]), credits_total, plan)
                asyncio.create_task(_dispatch_webhooks(
                    str(user_row["user_id"]), "subscription.activated", {
                        "plan": plan,
                        "payment_rail": "usdc",
                        "credits_added": credits_total,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ))


async def _usdc_payment_watcher():
    """Background task: poll Base chain for USDC transfers to WAYFORTH_BASE_WALLET."""
    from main import app
    if not USDC_RAIL_ENABLED:
        logger.info("_usdc_payment_watcher: USDC rail disabled — watcher not started")
        return
    BASE_RPC_URL = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
    USDC_ADDRESS = os.environ.get("USDC_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")
    TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    logger.info("USDC watcher started (rpc=%s usdc=%s)", BASE_RPC_URL, USDC_ADDRESS)

    while True:
        try:
            await asyncio.sleep(30)
            wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
            if not wallet or not app.state.pool:
                continue

            async with app.state.pool.acquire() as db:
                # FINDING-002: only consider unconsumed pending rows that carry a
                # declared payer address; amount-only matching is no longer enough.
                pending = await db.fetch(
                    "SELECT id, reference_id, plan, amount_usdc, api_key_id, payer_address "
                    "FROM usdc_payments "
                    "WHERE status = 'pending' AND consumed = FALSE "
                    "  AND expires_at > NOW() AND payer_address IS NOT NULL"
                )
                await db.execute(
                    "UPDATE usdc_payments SET status = 'expired', updated_at = NOW() "
                    "WHERE status = 'pending' AND expires_at <= NOW()"
                )
                # FINDING-002: persisted scan cursor — never re-scan from genesis.
                cursor = await db.fetchval("SELECT last_block FROM usdc_scan_state WHERE id = 1") or 0

            # Seed the cursor from the current head on first run. Scanning
            # eth_getLogs from genesis (0x0) to latest is rejected by public RPCs
            # for being too wide, so an uninitialised cursor would never progress.
            if not cursor:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        br = await client.post(BASE_RPC_URL, json={
                            "jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
                    head = int(br.json()["result"], 16)
                    async with app.state.pool.acquire() as db:
                        await db.execute(
                            "UPDATE usdc_scan_state SET last_block = $1, updated_at = NOW() WHERE id = 1",
                            head,
                        )
                    logger.info("USDC watcher: seeded scan cursor at block %d", head)
                except Exception as _seed_err:
                    logger.warning("USDC watcher: cursor seed failed: %s", _seed_err)
                continue

            if not pending:
                continue

            padded_wallet = "0x" + "0" * 24 + wallet[2:].lower()
            from_block = hex(int(cursor))
            payload = {
                "jsonrpc": "2.0", "id": 1, "method": "eth_getLogs",
                "params": [{
                    "fromBlock": from_block,
                    "toBlock": "latest",
                    "address": USDC_ADDRESS,
                    "topics": [TRANSFER_TOPIC, None, padded_wallet],
                }],
            }
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(BASE_RPC_URL, json=payload)
            if r.status_code != 200:
                continue

            logs = r.json().get("result", [])
            max_seen = int(cursor)

            for log in logs:
                tx_hash = log.get("transactionHash", "")
                try:
                    blk = int(log.get("blockNumber", "0x0"), 16)
                    max_seen = max(max_seen, blk)
                except ValueError:
                    pass
                topics = log.get("topics") or []
                sender = ("0x" + topics[1][-40:]).lower() if len(topics) >= 2 and topics[1] else ""
                try:
                    amount_usdc = int(log.get("data", "0x"), 16) / 1_000_000
                except ValueError:
                    continue

                for row in pending:
                    expected = float(row["amount_usdc"])
                    # FINDING-002/003: require amount AND payer match, then
                    # atomically claim an unconsumed row, binding THIS tx hash to
                    # it so the same transfer can never credit a second row.
                    if (abs(amount_usdc - expected) / expected <= 0.01
                            and sender
                            and sender == (row["payer_address"] or "").lower()):
                        async with app.state.pool.acquire() as cdb:
                            claimed = await cdb.fetchval(
                                "UPDATE usdc_payments SET consumed = TRUE, tx_hash = $1, updated_at = NOW() "
                                "WHERE id = $2 AND consumed = FALSE AND tx_hash IS NULL "
                                "RETURNING id",
                                tx_hash, row["id"],
                            )
                        if not claimed:
                            continue
                        plan_def = PLANS.get(row["plan"], PLANS["free"])
                        base_credits = plan_def["monthly_credits"]
                        bonus = math.floor(base_credits * 0.05)
                        await _activate_usdc_subscription(
                            app.state.pool,
                            row["reference_id"],
                            tx_hash,
                            row["plan"],
                            base_credits + bonus,
                            row["payer_address"],
                            str(row["api_key_id"]),
                            bonus,
                        )
                        break

            # Advance the persisted cursor so the next poll starts after the last
            # block we've already processed.
            async with app.state.pool.acquire() as db:
                await db.execute(
                    "UPDATE usdc_scan_state SET last_block = $1, updated_at = NOW() WHERE id = 1",
                    max_seen,
                )

        except Exception as _e:
            logger.error("_usdc_payment_watcher error: %s", _e)


async def _usdc_renewal_reminder():
    """Background task: fire renewal_due webhooks 7 days before USDC subscription expiry."""
    from main import app
    if not USDC_RAIL_ENABLED:
        logger.info("_usdc_renewal_reminder: USDC rail disabled — reminder not started")
        return
    while True:
        try:
            await asyncio.sleep(3600)  # check hourly
            if not app.state.pool:
                continue
            async with app.state.pool.acquire() as db:
                expiring = await db.fetch("""
                    SELECT k.id, k.user_id, k.tier, k.subscription_expires_at
                    FROM api_keys k
                    WHERE k.payment_rail = 'usdc'
                    AND k.subscription_expires_at BETWEEN NOW() AND NOW() + INTERVAL '7 days'
                """)
            for key in expiring:
                plan = key["tier"]
                plan_def = PLANS.get(plan, PLANS["free"])
                bonus_calls = plan_def["usdc_bonus_credits"] // CREDITS_PER_CALL
                new_ref = f"sub_usdc_{secrets.token_hex(8)}"
                asyncio.create_task(_dispatch_webhooks(
                    str(key["user_id"]), "subscription.renewal_due", {
                        "event": "subscription.renewal_due",
                        "plan": plan,
                        "expires_at": key["subscription_expires_at"].isoformat(),
                        "renewal": {
                            "payment_address": os.environ.get("WAYFORTH_BASE_WALLET", ""),
                            "amount_usdc": f"{plan_def['price_usdc']:.6f}",
                            "new_reference_id": new_ref,
                            "bonus_calls": bonus_calls,
                        },
                    }
                ))
        except Exception as _e:
            logger.error("_usdc_renewal_reminder error: %s", _e)


@router.post("/billing/subscribe-usdc")
@limiter.limit("10/minute")
async def subscribe_usdc(request: Request, db=Depends(get_db)):
    """Initiate a USDC subscription payment on Base.

    Returns payment instructions. A background watcher confirms the Transfer event
    and activates the subscription automatically.
    """
    if not USDC_RAIL_ENABLED:
        _usdc_disabled_response()
    wayforth_wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
    if not wayforth_wallet:
        raise HTTPException(status_code=503, detail={
            "error": "USDC subscriptions coming soon. Use card billing for now.",
            "alternatives": ["GET /pricing/json"],
        })

    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail={"error": "X-Wayforth-API-Key header required"})

    key_record = await db.fetchrow(
        "SELECT id, user_id FROM api_keys WHERE key_hash = $1 AND active = true",
        hashlib.sha256(api_key.encode()).hexdigest(),
    )
    if not key_record:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})

    body = await request.json()
    plan = body.get("plan", "").strip().lower()
    wallet_address = body.get("wallet_address", "").strip()

    if plan not in PLANS or plan == "free":
        paid_plans = [k for k in PLANS if k != "free"]
        raise HTTPException(status_code=400, detail={
            "error": f"Invalid plan '{plan}'. Choose from: {', '.join(paid_plans)}"
        })

    # FINDING-003: the paying wallet must be declared up front so the watcher can
    # bind the inbound transfer to this subscription (no amount-only matching).
    if not (wallet_address.startswith("0x") and len(wallet_address) == 42):
        raise HTTPException(status_code=400, detail={
            "error": "wallet_address_required",
            "message": "wallet_address (the wallet you will pay FROM, 0x… 42 chars) is required.",
        })

    plan_def = PLANS[plan]
    reference_id = f"sub_usdc_{secrets.token_hex(8)}"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    amount_usdc = f"{plan_def['price_usdc']:.6f}"
    calls_included = plan_def["calls_included"]
    bonus_calls = plan_def["usdc_bonus_credits"] // CREDITS_PER_CALL

    await db.execute("""
        INSERT INTO usdc_payments
            (reference_id, api_key_id, plan, amount_usdc, wallet_address, payer_address, expires_at)
        VALUES ($1, $2::uuid, $3, $4, $5, $5, $6)
    """, reference_id, str(key_record["id"]), plan, float(amount_usdc),
        wallet_address, expires_at)

    return {
        "payment_address": wayforth_wallet,
        "amount_usdc": amount_usdc,
        "plan": plan,
        "credits_included": calls_included,
        "calls_included": calls_included,  # backward compat
        "bonus_calls": bonus_calls,
        "reference_id": reference_id,
        "memo": "Include this in USDC transfer memo",
        "expires_at": expires_at.isoformat(),
        "instructions": (
            f"Send exactly {plan_def['price_usdc']} USDC to the address above on Base "
            f"within 24 hours. Include the reference_id as memo."
        ),
    }


@router.post("/billing/topup-usdc")
@limiter.limit("10/minute")
async def topup_usdc(request: Request, db=Depends(get_db)):
    """Agent-initiated USDC top-up. api_key + tx_hash in body — no Authorization header needed.

    Requires billing_permission = 'auto_topup' or 'full' on the API key.
    Enforces monthly_topup_limit_usd. Replay-protected via usdc_payments tx_hash index.
    """
    if not USDC_RAIL_ENABLED:
        _usdc_disabled_response()
    body = await request.json()
    api_key = body.get("api_key", "").strip()
    tx_hash = body.get("tx_hash", "").strip()
    amount_usdc_str = body.get("amount_usdc", "").strip()
    payer_address = body.get("payer_address", "").strip()

    if not api_key or not tx_hash or not amount_usdc_str or not payer_address:
        raise HTTPException(status_code=400, detail={
            "error": "api_key, tx_hash, amount_usdc, and payer_address are required"
        })
    # FINDING-003: the declared payer must match the on-chain sender so a public
    # tx hash cannot be claimed by an account other than the one that paid.
    if not (payer_address.startswith("0x") and len(payer_address) == 42):
        raise HTTPException(status_code=400, detail={"error": "invalid_payer_address"})

    try:
        amount_usdc_float = float(amount_usdc_str)
        if amount_usdc_float <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail={"error": "amount_usdc must be a positive number"})

    # 1. Look up API key with billing settings
    key_record = await db.fetchrow("""
        SELECT id, user_id, tier, billing_permission,
               monthly_topup_limit_usd, topup_amount_usd,
               monthly_topup_spent_usd, monthly_topup_reset_at
        FROM api_keys
        WHERE key_hash = $1 AND active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401, detail={"error": "invalid_api_key"})

    require_tier(key_record["tier"] or "free", "topup_usdc")

    # 2. Permission check
    billing_perm = key_record["billing_permission"] or "none"
    if billing_perm == "none":
        raise HTTPException(status_code=403, detail={
            "error": "billing_permission_denied",
            "message": (
                "This API key does not have billing permissions. "
                "Enable auto top-up in your dashboard at wayforth.io/dashboard"
            ),
        })

    # 3. Monthly reset if period has rolled over
    now_utc = datetime.now(timezone.utc)
    reset_at = key_record["monthly_topup_reset_at"]
    if reset_at and now_utc >= reset_at:
        # FINDING-014: advance to the first of NEXT month relative to now.
        # The old "reset_at + 32 days then day=1" skipped a month from a
        # late-month anchor (e.g. Jan-31 + 32d = Mar, skipping February).
        if now_utc.month == 12:
            next_reset = now_utc.replace(year=now_utc.year + 1, month=1, day=1,
                                         hour=0, minute=0, second=0, microsecond=0)
        else:
            next_reset = now_utc.replace(month=now_utc.month + 1, day=1,
                                         hour=0, minute=0, second=0, microsecond=0)
        await db.execute("""
            UPDATE api_keys
            SET monthly_topup_spent_usd = 0,
                monthly_topup_reset_at = $1
            WHERE id = $2::uuid
        """, next_reset, str(key_record["id"]))
        spent_usd = 0.0
        reset_at = next_reset
    else:
        spent_usd = float(key_record["monthly_topup_spent_usd"] or 0)

    # 4. Budget check
    limit_usd = float(key_record["monthly_topup_limit_usd"] or 20)
    remaining_usd = round(limit_usd - spent_usd, 2)
    if amount_usdc_float > remaining_usd:
        raise HTTPException(status_code=403, detail={
            "error": "monthly_limit_reached",
            "message": (
                f"Monthly auto top-up limit of ${limit_usd:.2f} reached. "
                f"Resets on {reset_at.strftime('%Y-%m-%d') if reset_at else 'next month'}."
            ),
            "limit_usd": limit_usd,
            "spent_usd": round(spent_usd, 2),
            "resets_at": reset_at.isoformat() if reset_at else None,
        })

    # 5. WAYFORTH_BASE_WALLET guard
    wayforth_wallet = os.environ.get("WAYFORTH_BASE_WALLET", "")
    if not wayforth_wallet:
        raise HTTPException(status_code=503, detail={
            "error": "Crypto payments not yet configured. Top up via dashboard instead.",
            "dashboard_url": "https://wayforth.io/billing",
        })

    # 6. Replay protection
    existing = await db.fetchval(
        "SELECT id FROM usdc_payments WHERE tx_hash = $1", tx_hash
    )
    if existing:
        raise HTTPException(status_code=409, detail={
            "error": "transaction_already_used",
            "message": "This transaction hash has already been applied to a top-up.",
        })

    # 7. Verify tx_hash on Base — payer must match the declared wallet.
    verify = await _verify_tx_on_base(tx_hash, wayforth_wallet, amount_usdc_str,
                                      expected_sender=payer_address)
    if not verify["valid"]:
        raise HTTPException(status_code=402, detail={
            "error": "payment_verification_failed",
            "reason": verify.get("reason", "invalid_transaction"),
            "message": (
                f"Could not verify USDC transfer of ${amount_usdc_float:.6f} to "
                f"Wayforth wallet. Ensure the transaction is confirmed on Base mainnet."
            ),
        })

    # 8. Atomic credit + budget update — 5% USDC bonus
    base_credits = math.floor(amount_usdc_float * 1000)
    topup_bonus = math.floor(base_credits * 0.05)
    credits_to_add = base_credits + topup_bonus
    reference_id = f"topup_{secrets.token_hex(8)}"

    async with db.transaction():
        # Add credits to user
        new_credits = await db.fetchval("""
            UPDATE user_credits
            SET credits_balance = credits_balance + $1,
                lifetime_credits = lifetime_credits + $1,
                updated_at = NOW()
            WHERE user_id = $2::uuid
            RETURNING credits_balance
        """, credits_to_add, str(key_record["user_id"]))

        if new_credits is None:
            # First-time credit row
            await db.execute("""
                INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                VALUES ($1::uuid, $2, $2, 'free', 'usdc')
            """, str(key_record["user_id"]), credits_to_add)
            new_credits = credits_to_add

        # Update monthly spend tracker
        await db.execute("""
            UPDATE api_keys
            SET monthly_topup_spent_usd = monthly_topup_spent_usd + $1
            WHERE id = $2::uuid
        """, amount_usdc_float, str(key_record["id"]))

        # Record payment for replay protection and audit
        await db.execute("""
            INSERT INTO usdc_payments
                (reference_id, api_key_id, plan, amount_usdc, tx_hash, payer_address,
                 status, consumed, bonus_credits, expires_at)
            VALUES ($1, $2::uuid, 'topup', $3, $4, $5, 'confirmed', TRUE, $6, NOW() + INTERVAL '10 years')
        """, reference_id, str(key_record["id"]), amount_usdc_float, tx_hash, payer_address, topup_bonus)

    asyncio.create_task(_maybe_grant_founding_bonus(db, str(key_record["user_id"])))

    new_spent = round(spent_usd + amount_usdc_float, 2)
    new_remaining = round(limit_usd - new_spent, 2)

    return {
        "success": True,
        "amount_usdc": f"{amount_usdc_float:.6f}",
        "credits_added": credits_to_add,
        "bonus_credits": topup_bonus,
        "calls_added": credits_to_add // CREDITS_PER_CALL,
        "new_balance_calls": new_credits // CREDITS_PER_CALL,
        "monthly_topup_spent_usd": new_spent,
        "monthly_topup_limit_usd": limit_usd,
        "monthly_topup_remaining_usd": new_remaining,
        "resets_at": reset_at.isoformat() if reset_at else None,
        "tx_confirmed": True,
        "payment_rail": "usdc",
    }


@router.get("/billing/settings")
@limiter.limit("30/minute")
async def get_billing_settings(request: Request, db=Depends(get_db)):
    """Return current billing permission settings for the authenticated API key."""
    from core.credits import compute_calls_remaining
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow("""
        SELECT id, user_id, tier, payment_rail,
               billing_permission, topup_trigger_calls,
               topup_amount_usd, monthly_topup_limit_usd,
               monthly_topup_spent_usd, monthly_topup_reset_at
        FROM api_keys
        WHERE key_hash = $1 AND active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    credits = await db.fetchval(
        "SELECT credits_balance FROM user_credits WHERE user_id = $1",
        key_record["user_id"],
    )
    balance = credits or 0
    tier = key_record["tier"] or "free"
    payment_rail = key_record["payment_rail"] or "card"
    limit = float(key_record["monthly_topup_limit_usd"] or 20)
    spent = float(key_record["monthly_topup_spent_usd"] or 0)
    reset_at = key_record["monthly_topup_reset_at"]
    usdc_active = payment_rail == "usdc"

    result = {
        "billing_permission": key_record["billing_permission"] or "none",
        "topup_trigger_calls": key_record["topup_trigger_calls"] or 100,
        "topup_amount_usd": float(key_record["topup_amount_usd"] or 5),
        "monthly_topup_limit_usd": limit,
        "monthly_topup_spent_usd": round(spent, 2),
        "monthly_topup_remaining_usd": round(limit - spent, 2),
        "monthly_topup_reset_at": reset_at.date().isoformat() if reset_at else None,
        "credits_remaining": await compute_calls_remaining(db, str(key_record["id"])),
        "calls_remaining": await compute_calls_remaining(db, str(key_record["id"])),  # backward compat
        "plan": tier,
        "payment_rail": payment_rail,
        "usdc_bonus_rate": 0.05,
        "usdc_bonus_active": usdc_active,
    }
    if not usdc_active:
        result["usdc_bonus_message"] = "Switch to USDC and get 5% more calls every month."
    return result


@router.patch("/billing/settings")
@limiter.limit("10/minute")
async def update_billing_settings(request: Request, db=Depends(get_db)):
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
        if not (1.0 <= val <= 100.0):
            raise HTTPException(status_code=400, detail={
                "error": "topup_amount_usd must be between 1.00 and 100.00"
            })
        updates["topup_amount_usd"] = val

    if "monthly_topup_limit_usd" in body:
        val = float(body["monthly_topup_limit_usd"])
        effective_topup = updates.get("topup_amount_usd", float(key_record["topup_amount_usd"] or 5))
        if val < effective_topup:
            raise HTTPException(status_code=400, detail={
                "error": f"monthly_topup_limit_usd ({val:.2f}) must be >= topup_amount_usd ({effective_topup:.2f})"
            })
        updates["monthly_topup_limit_usd"] = val

    if not updates:
        raise HTTPException(status_code=400, detail={"error": "No valid fields provided"})

    set_parts = [f"{col} = ${i + 2}" for i, col in enumerate(updates)]
    set_clause = ", ".join(set_parts)
    values = list(updates.values())

    await db.execute(
        f"UPDATE api_keys SET {set_clause} WHERE id = $1::uuid",
        str(key_record["id"]), *values,
    )

    return await get_billing_settings(request, db)
