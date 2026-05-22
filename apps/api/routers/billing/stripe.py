"""routers/billing/stripe.py — Stripe checkout, webhook, cancel, mock-topup, packages."""

import asyncio
import hashlib
import logging
import os
import secrets
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.auth import _resolve_user
from core.credits import _dispatch_webhooks, _maybe_grant_founding_bonus
from core.db import get_db
from core.rate_limit import limiter, get_real_ip

logger = logging.getLogger("wayforth")

router = APIRouter()

# ── Local STRIPE_MOCK flag ────────────────────────────────────────────────────

# STRIPE_MOCK auto-enables /billing/mock-topup (free credit grant up to 100k
# per call). Auto-enabling on `sk_test_*` keys was a footgun: any test key
# deployed alongside ENVIRONMENT=production would expose unrestricted credit
# minting to every authenticated user. We now require an explicit STRIPE_MOCK=true
# flag, AND refuse to enable in production regardless of the Stripe key prefix.
_ENV = os.environ.get("ENVIRONMENT", "development").lower()
_STRIPE_MOCK_EXPLICIT = os.environ.get("STRIPE_MOCK", "false").lower() == "true"
_STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "")

if _ENV == "production":
    STRIPE_MOCK = False
    if _STRIPE_MOCK_EXPLICIT or _STRIPE_KEY.startswith("sk_test_"):
        logger.error(
            "STRIPE_MOCK=true or sk_test_* key with ENVIRONMENT=production — "
            "mock mode is FORCED OFF. Use a live Stripe key in production."
        )
else:
    STRIPE_MOCK = (not _STRIPE_KEY) or _STRIPE_MOCK_EXPLICIT

# ── Constants ─────────────────────────────────────────────────────────────────

PACKAGES = {
    "builder":    {"credits": 6_000,   "price_usd": 12,  "fee_bps": 150, "label": "Builder"},
    "starter":    {"credits": 21_000,  "price_usd": 29,  "fee_bps": 150, "label": "Starter"},
    "pro":        {"credits": 72_000,  "price_usd": 99,  "fee_bps": 150, "label": "Pro"},
    "growth":     {"credits": 240_000, "price_usd": 299, "fee_bps": 150, "label": "Growth"},
    "enterprise": {"credits": -1,      "price_usd": None,"fee_bps": 150, "label": "Enterprise"},
}

STRIPE_PACKAGES = {
    "builder": {"price_cents": 1200,  "credits": 6_000,   "label": "Builder",
                "price_id": os.environ.get("STRIPE_PRICE_BUILDER", "")},
    "starter": {"price_cents": 2900,  "credits": 21_000,  "label": "Starter",
                "price_id": os.environ.get("STRIPE_PRICE_STARTER", "")},
    "pro":     {"price_cents": 9900,  "credits": 72_000,  "label": "Pro",
                "price_id": os.environ.get("STRIPE_PRICE_PRO", "")},
    "growth":  {"price_cents": 29900, "credits": 240_000, "label": "Growth",
                "price_id": os.environ.get("STRIPE_PRICE_GROWTH", "")},
}

# Annual plans: 10 months price (2 months free). Credits replenished monthly, not upfront.
STRIPE_ANNUAL_PACKAGES = {
    "builder_annual": {"price_cents": 9900,   "credits": 6_000,   "label": "Builder Annual",
                       "price_id": os.environ.get("STRIPE_PRICE_BUILDER_ANNUAL", ""),
                       "savings_usd": 45.0,  "base_plan": "builder"},
    "starter_annual": {"price_cents": 29000,  "credits": 21_000,  "label": "Starter Annual",
                       "price_id": os.environ.get("STRIPE_PRICE_STARTER_ANNUAL", ""),
                       "savings_usd": 58.0,  "base_plan": "starter"},
    "pro_annual":     {"price_cents": 99000,  "credits": 72_000,  "label": "Pro Annual",
                       "price_id": os.environ.get("STRIPE_PRICE_PRO_ANNUAL", ""),
                       "savings_usd": 198.0, "base_plan": "pro"},
    "growth_annual":  {"price_cents": 299000, "credits": 240_000, "label": "Growth Annual",
                       "price_id": os.environ.get("STRIPE_PRICE_GROWTH_ANNUAL", ""),
                       "savings_usd": 598.0, "base_plan": "growth"},
}

# ── Models ────────────────────────────────────────────────────────────────────

class SubmitRequest(BaseModel):
    name: str
    description: str
    endpoint_url: str
    category: str
    price_per_call: float = 0.0
    contact_email: str | None = None


class PayRequest(BaseModel):
    service_id: str
    service_owner: str = ""
    amount_usd: float = 0.0
    query_id: str = ""
    agent_id: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _probe_new_service(service_id: str, endpoint_url: str):
    from main import app
    import httpx as _httpx
    # Re-validate at probe time in case the row was created via another path
    # that skipped url validation, and pin follow_redirects=False so a 30x
    # cannot rebind onto an internal IP.
    try:
        from core.url_validation import validate_external_url
        validate_external_url(endpoint_url, field_name="endpoint_url")
    except Exception as _vexc:
        logger.warning("probe refused for service %s url=%s: %s",
                       service_id, endpoint_url, _vexc)
        return
    try:
        async with _httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            r = await client.get(endpoint_url)
            new_tier = 1 if r.status_code < 500 else 0
            async with app.state.pool.acquire() as db:
                await db.execute("""
                    UPDATE services
                    SET coverage_tier=$1, last_tested_at=NOW(), consecutive_failures=0
                    WHERE id=$2::uuid
                """, new_tier, service_id)
                logger.info(f"New service {service_id} probed: tier {new_tier} (status {r.status_code})")
    except Exception as e:
        logger.warning(f"New service probe failed for {service_id}: {e}")


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/submit")
@limiter.limit("5/minute")
async def submit_service(request: Request, req: SubmitRequest, db=Depends(get_db)):
    from main import app
    import asyncpg
    from notifications import send_submission_confirmation
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")
    await _resolve_user(db, api_key)
    # SSRF defense: _probe_new_service immediately fetches the submitted URL.
    # Reject internal hostnames, private/loopback IPs, and non-https schemes.
    from core.url_validation import validate_external_url
    validate_external_url(req.endpoint_url, field_name="endpoint_url")
    if req.category not in ("inference", "data", "translation"):
        raise HTTPException(status_code=400, detail="category must be one of: inference, data, translation")
    if len(req.name) > 100:
        raise HTTPException(status_code=400, detail="name must be 100 characters or fewer")
    if len(req.description) > 500:
        raise HTTPException(status_code=400, detail="description must be 500 characters or fewer")
    if app.state.pool is None:
        raise HTTPException(status_code=503, detail="Database unavailable")
    try:
        async with app.state.pool.acquire() as conn:
            service_id = await conn.fetchval(
                """INSERT INTO services (name, description, endpoint_url, category, pricing_usdc, source, coverage_tier)
                   VALUES ($1, $2, $3, $4, $5, 'submitted', 0) RETURNING id""",
                req.name, req.description, req.endpoint_url, req.category, req.price_per_call,
            )
            await conn.execute(
                """INSERT INTO service_submissions (service_id, contact_email, ip_address)
                   VALUES ($1, $2, $3)""",
                service_id, req.contact_email, get_real_ip(request),
            )
        logger.info(f"submit name={req.name!r} category={req.category}")
        asyncio.create_task(_probe_new_service(str(service_id), req.endpoint_url))
        if req.contact_email:
            asyncio.create_task(asyncio.to_thread(
                send_submission_confirmation,
                req.contact_email, req.name, str(service_id), req.endpoint_url,
            ))
        import asyncio as _asyncio
        await _asyncio.sleep(3)
        async with app.state.pool.acquire() as conn2:
            service = await conn2.fetchrow("""
                SELECT coverage_tier, last_tested_at, consecutive_failures
                FROM services WHERE id = $1::uuid
            """, str(service_id))
        tier = service["coverage_tier"] if service else 0
        return {
            "status": "submitted",
            "service_id": str(service_id),
            "name": req.name,
            "initial_tier": tier,
            "message": f"Service submitted and probed. Current tier: {tier}. Tier 2 requires 90%+ uptime over 7 days.",
            "leaderboard_url": "https://wayforth.io/leaderboard",
            "tier3_url": "https://wayforth.io/tier3",
        }
    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="A service with this endpoint URL already exists")
    except Exception as e:
        logger.error(f"Submit error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")


@router.get("/services/{service_id}")
@limiter.limit("30/minute")
async def get_service(request: Request, service_id: str):
    from main import app
    try:
        async with app.state.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, name, description, endpoint_url, category,
                       coverage_tier, pricing_usdc, source, payment_protocol, created_at
                FROM services WHERE id = $1
                """,
                service_id,
            )
    except Exception as e:
        logger.error(f"DB error: {e}")
        raise HTTPException(status_code=503, detail="Database unavailable")
    if row is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return dict(row)


@router.get("/billing/packages")
async def get_packages(request: Request):
    from core.credits import PLANS
    result = []
    for key, pkg in PACKAGES.items():
        plan = PLANS.get(key, {})
        if pkg['price_usd'] is None:
            result.append({
                "plan": key,
                "label": pkg['label'],
                "price": "custom",
                "calls_included": plan.get("calls_included", 0),
                "description": "Custom pricing, priority support, SLA",
            })
        else:
            result.append({
                "plan": key,
                "label": pkg['label'],
                "price_usd": pkg['price_usd'],
                "calls_included": plan.get("calls_included", pkg['credits']),
                "price_per_credit": round(pkg['price_usd'] / pkg['credits'], 8),
            })

    annual_options = [
        {
            "plan": key,
            "label": pkg["label"],
            "price_usd_annual": pkg["price_cents"] / 100,
            "credits_per_month": pkg["credits"],
            "savings_usd": pkg["savings_usd"],
            "billing_cadence": "annual",
        }
        for key, pkg in STRIPE_ANNUAL_PACKAGES.items()
    ]

    return {"packages": result, "annual_options": annual_options}


@router.post("/billing/checkout")
@limiter.limit("10/minute")
async def create_checkout(request: Request, db=Depends(get_db)):
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)

    key_record = await db.fetchrow("""
        SELECT k.user_id, u.email
        FROM api_keys k JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401)

    body = await request.json()
    package = body.get("package", "starter")

    # Support annual packages (e.g. "pro_annual") alongside monthly ones
    all_packages = {**STRIPE_PACKAGES, **STRIPE_ANNUAL_PACKAGES}
    if package not in all_packages:
        raise HTTPException(status_code=400, detail="Invalid package")

    pkg = all_packages[package]
    billing_cadence = "annual" if package.endswith("_annual") else "monthly"

    # Mock mode: no real Stripe key configured or STRIPE_MOCK=true
    if STRIPE_MOCK:
        mock_session_id = "mock_sess_" + secrets.token_hex(12)
        async with db.transaction():
            existing = await db.fetchrow(
                "SELECT credits_balance FROM user_credits WHERE user_id=$1::uuid FOR UPDATE",
                key_record['user_id']
            )
            if existing:
                new_balance = existing['credits_balance'] + pkg["credits"]
                await db.execute("""
                    UPDATE user_credits
                    SET credits_balance=$1, lifetime_credits=lifetime_credits+$2,
                        payment_method='mock_card', updated_at=NOW()
                    WHERE user_id=$3::uuid
                """, new_balance, pkg["credits"], key_record['user_id'])
            else:
                new_balance = pkg["credits"]
                await db.execute("""
                    INSERT INTO user_credits
                    (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                    VALUES ($1::uuid, $2, $2, 'free', 'mock_card')
                """, key_record['user_id'], new_balance)

            await db.execute("""
                INSERT INTO credit_transactions
                (user_id, amount, balance_after, type, description)
                VALUES ($1::uuid, $2, $3, 'mock_purchase', $4)
            """, key_record['user_id'], pkg["credits"], new_balance,
                f"Mock purchase: {package} pack - {pkg['credits']:,} credits (Stripe not configured)")

        return {
            "checkout_url": f"https://wayforth.io/dashboard?purchase=success&package={package}&mock=true",
            "session_id": mock_session_id,
            "package": package,
            "credits": pkg["credits"],
            "price_usd": pkg["price_cents"] / 100,
            "mock": True,
            "credits_added": pkg["credits"],
            "new_balance": new_balance,
            "note": "Stripe not configured. Credits added automatically in mock mode.",
        }

    use_subscription = bool(pkg.get("price_id")) and not STRIPE_MOCK

    try:
        if use_subscription:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{"price": pkg["price_id"], "quantity": 1}],
                mode="subscription",
                success_url="https://wayforth.io/dashboard?purchase=success&package=" + package,
                cancel_url="https://wayforth.io/dashboard?purchase=cancelled",
                customer_email=key_record['email'],
                subscription_data={
                    "metadata": {
                        "user_id": str(key_record['user_id']),
                        "package": package,
                        "credits": str(pkg["credits"]),
                        "billing_cadence": billing_cadence,
                    }
                },
                metadata={
                    "user_id": str(key_record['user_id']),
                    "package": package,
                    "credits": str(pkg["credits"]),
                    "billing_cadence": billing_cadence,
                },
            )
        else:
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": pkg["price_cents"],
                        "product_data": {
                            "name": f"Wayforth {pkg['label']}",
                            "description": f"{pkg['credits']:,} credits · 1 credit = $0.001",
                        },
                    },
                    "quantity": 1,
                }],
                mode="payment",
                success_url="https://wayforth.io/dashboard?purchase=success&package=" + package,
                cancel_url="https://wayforth.io/dashboard?purchase=cancelled",
                customer_email=key_record['email'],
                metadata={
                    "user_id": str(key_record['user_id']),
                    "package": package,
                    "credits": str(pkg["credits"]),
                },
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

    await db.execute("""
        INSERT INTO package_purchases
        (user_id, package_name, credits_purchased, credits_total,
         amount_usd, payment_method, payment_status, stripe_payment_id)
        VALUES ($1, $2, $3, $3, $4, 'card', 'pending', $5)
    """, key_record['user_id'], package, pkg['credits'],
        pkg['price_cents'] / 100, session.id)

    return {
        "checkout_url": session.url,
        "session_id": session.id,
        "package": package,
        "credits": pkg["credits"],
        "price_usd": pkg["price_cents"] / 100,
        "billing_mode": "subscription" if use_subscription else "one_time",
    }


@router.post("/billing/cancel")
@limiter.limit("5/minute")
async def billing_cancel(request: Request, db=Depends(get_db)):
    """Cancel the caller's Stripe subscription at period end."""
    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    key_record = await db.fetchrow("""
        SELECT k.user_id, k.stripe_subscription_id, u.email
        FROM api_keys k JOIN users u ON u.id = k.user_id
        WHERE k.key_hash = $1 AND k.active = true
    """, hashlib.sha256(api_key.encode()).hexdigest())

    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid API key")

    sub_id = key_record["stripe_subscription_id"]

    if not sub_id or STRIPE_MOCK:
        return JSONResponse(content={
            "message": "Contact support@wayforth.io to cancel your subscription"
        })

    try:
        sub = stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")

    period_end = datetime.fromtimestamp(sub["current_period_end"], tz=timezone.utc)
    effective_date = period_end.strftime("%Y-%m-%d")
    human_date = period_end.strftime("%B %-d, %Y")

    return {
        "cancelled": True,
        "effective_date": effective_date,
        "message": f"Your plan will downgrade to Free on {human_date}",
    }


@router.post("/billing/mock-topup")
@limiter.limit("5/minute")
async def mock_topup(request: Request, db=Depends(get_db)):
    """Test endpoint: add credits without Stripe. Only works when STRIPE_MOCK=true
    AND ENVIRONMENT != production. Re-checks both at call time as defense in depth
    even though STRIPE_MOCK is already pinned to False in production above."""
    if _ENV == "production" or not STRIPE_MOCK:
        raise HTTPException(status_code=403, detail="Mock top-up not available in production")

    api_key = request.headers.get("X-Wayforth-API-Key", "")
    if not api_key:
        raise HTTPException(status_code=401)

    key_record = await db.fetchrow(
        "SELECT user_id FROM api_keys WHERE key_hash=$1 AND active=true",
        hashlib.sha256(api_key.encode()).hexdigest()
    )
    if not key_record:
        raise HTTPException(status_code=401)

    body = await request.json()
    credits = min(int(body.get("credits", 10000)), 100000)

    async with db.transaction():
        existing = await db.fetchrow(
            "SELECT credits_balance FROM user_credits WHERE user_id=$1::uuid FOR UPDATE",
            key_record['user_id']
        )
        if existing:
            new_balance = existing['credits_balance'] + credits
            await db.execute(
                "UPDATE user_credits SET credits_balance=$1, lifetime_credits=lifetime_credits+$2, updated_at=NOW() WHERE user_id=$3::uuid",
                new_balance, credits, key_record['user_id']
            )
        else:
            new_balance = credits
            await db.execute(
                "INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method) VALUES ($1::uuid, $2, $2, 'free', 'mock')",
                key_record['user_id'], new_balance
            )

        await db.execute("""
            INSERT INTO credit_transactions (user_id, amount, balance_after, type, description)
            VALUES ($1::uuid, $2, $3, 'mock_topup', 'Mock top-up for testing')
        """, key_record['user_id'], credits, new_balance)

    return {"status": "ok", "credits_added": credits, "new_balance": new_balance, "mock": True}


@router.post("/stripe/webhook")
@limiter.limit("100/minute")
async def stripe_webhook(request: Request, db=Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception:
        raise HTTPException(status_code=400)

    # Idempotency gate: Stripe explicitly says callers must tolerate duplicate
    # event delivery. We INSERT the event id and short-circuit if it's already
    # been processed. Previously the renewal handler (`invoice.payment_succeeded`)
    # had no dedup at all, so a single Stripe retry would double-credit the user.
    event_id = event.get("id") or ""
    event_type = event.get("type") or ""
    if event_id:
        try:
            inserted = await db.fetchval(
                "INSERT INTO stripe_events (event_id, event_type) VALUES ($1, $2) "
                "ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                event_id, event_type,
            )
            if inserted is None:
                logger.info("stripe webhook duplicate event=%s type=%s — skipping", event_id, event_type)
                return {"status": "duplicate_event"}
        except Exception as _e:
            # Fail closed: if we can't dedup, we'd rather refuse than risk
            # double-crediting. Stripe will retry, and the next attempt either
            # succeeds (DB healed) or keeps returning 503.
            logger.error("stripe_events dedup insert failed: %s", _e)
            raise HTTPException(status_code=503, detail="event_dedup_unavailable")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        meta = session.get("metadata", {})
        user_id = meta.get("user_id")
        package = meta.get("package")
        credits = int(meta.get("credits", 0))
        session_id = session.get("id")

        if not all([user_id, package, credits]):
            return {"status": "missing_metadata"}

        already = await db.fetchval(
            "SELECT id FROM package_purchases WHERE stripe_payment_id = $1 AND payment_status = 'completed'",
            session_id
        )
        if already:
            return {"status": "already_processed"}

        async with db.transaction():
            await db.execute(
                "UPDATE package_purchases SET payment_status = 'completed' WHERE stripe_payment_id = $1",
                session_id
            )

            existing = await db.fetchrow(
                "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
                user_id
            )

            if existing:
                new_balance = existing['credits_balance'] + credits
                await db.execute("""
                    UPDATE user_credits
                    SET credits_balance = $1, lifetime_credits = lifetime_credits + $2,
                        package_tier = $3, payment_method = 'card', updated_at = NOW()
                    WHERE user_id = $4::uuid
                """, new_balance, credits, package, user_id)
            else:
                new_balance = credits
                await db.execute("""
                    INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                    VALUES ($1::uuid, $2, $2, $3, 'card')
                """, user_id, credits, package)

            await db.execute("""
                INSERT INTO credit_transactions
                (user_id, amount, balance_after, type, description)
                VALUES ($1::uuid, $2, $3, 'purchase', $4)
            """, user_id, credits, new_balance,
                f"Stripe purchase: {package} pack — {credits:,} credits added")

            # Keep api_keys.tier and billing_cadence in sync.
            _new_tier = package.split("_")[0] if package else None
            _cadence = meta.get("billing_cadence", "monthly")
            if _new_tier in ("builder", "starter", "pro", "growth"):
                await db.execute(
                    "UPDATE api_keys SET tier = $1, billing_cadence = $2 WHERE user_id = $3::uuid",
                    _new_tier, _cadence, user_id,
                )

        # Store subscription_id on the api_key if this was a subscription checkout
        sub_id = session.get("subscription")
        api_key_id_row = None
        if sub_id:
            await db.execute("""
                UPDATE api_keys
                SET stripe_subscription_id = $1, subscription_status = 'active'
                WHERE user_id = $2::uuid
            """, sub_id, user_id)

        asyncio.create_task(_maybe_grant_founding_bonus(db, user_id))
        return {"status": "credited", "credits_added": credits, "new_balance": new_balance}

    elif event["type"] == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription")
        if not sub_id:
            return {"status": "no_subscription"}

        # Check if this is a provider subscription first
        provider_row = await db.fetchrow(
            "SELECT id FROM providers WHERE stripe_subscription_id = $1", sub_id
        )
        if not provider_row:
            try:
                sub_obj = stripe.Subscription.retrieve(sub_id)
                provider_id_meta = sub_obj.get("metadata", {}).get("provider_id")
                provider_tier_meta = sub_obj.get("metadata", {}).get("provider_tier")
                if provider_id_meta and provider_tier_meta:
                    await db.execute(
                        "UPDATE providers SET tier = $1, stripe_subscription_id = $2 WHERE id = $3::uuid",
                        provider_tier_meta, sub_id, provider_id_meta,
                    )
                    return {"status": "provider_upgraded", "tier": provider_tier_meta}
            except Exception:
                pass

        key_row = await db.fetchrow(
            "SELECT user_id FROM api_keys WHERE stripe_subscription_id = $1",
            sub_id
        )
        if not key_row:
            return {"status": "unknown_subscription"}
        user_id = str(key_row["user_id"])

        # Determine package from subscription metadata
        try:
            sub = stripe.Subscription.retrieve(sub_id)
            meta = sub.get("metadata", {})
            package = meta.get("package", "")
            credits = int(meta.get("credits", 0))
        except Exception:
            package, credits = "", 0

        if not credits:
            # Infer from amount
            amount_paid = invoice.get("amount_paid", 0)
            for pkg_name, pkg_data in STRIPE_PACKAGES.items():
                if pkg_data["price_cents"] == amount_paid:
                    package = pkg_name
                    credits = pkg_data["credits"]
                    break

        if credits:
            async with db.transaction():
                existing = await db.fetchrow(
                    "SELECT credits_balance FROM user_credits WHERE user_id = $1::uuid FOR UPDATE",
                    user_id
                )
                if existing:
                    new_balance = existing["credits_balance"] + credits
                    await db.execute("""
                        UPDATE user_credits
                        SET credits_balance = $1, lifetime_credits = lifetime_credits + $2,
                            package_tier = $3, payment_method = 'card', updated_at = NOW()
                        WHERE user_id = $4::uuid
                    """, new_balance, credits, package, user_id)
                else:
                    new_balance = credits
                    await db.execute("""
                        INSERT INTO user_credits (user_id, credits_balance, lifetime_credits, package_tier, payment_method)
                        VALUES ($1::uuid, $2, $2, $3, 'card')
                    """, user_id, credits, package)
                await db.execute("""
                    INSERT INTO credit_transactions (user_id, amount, balance_after, type, description)
                    VALUES ($1::uuid, $2, $3, 'subscription_renewal', $4)
                """, user_id, credits, new_balance,
                    f"Monthly renewal: {package} — {credits:,} credits")

            _renewed_tier = package.split("_")[0] if package else None
            if _renewed_tier in ("builder", "starter", "pro", "growth"):
                await db.execute(
                    "UPDATE api_keys SET tier = $1, subscription_status = 'active' "
                    "WHERE stripe_subscription_id = $2",
                    _renewed_tier, sub_id,
                )
            else:
                await db.execute(
                    "UPDATE api_keys SET subscription_status = 'active' "
                    "WHERE stripe_subscription_id = $1",
                    sub_id,
                )
        asyncio.create_task(_maybe_grant_founding_bonus(db, user_id))

        # Subscription confirmed email
        if credits and package:
            try:
                user_email_row = await db.fetchrow(
                    "SELECT email FROM users WHERE id = $1::uuid", user_id
                )
                if user_email_row and user_email_row["email"]:
                    from core.email import send_email
                    plan_label = PACKAGES.get(package.split("_")[0], {}).get("label", package.title())
                    price_usd = PACKAGES.get(package.split("_")[0], {}).get("price_usd", 0)
                    from datetime import datetime, timezone
                    renewal_dt = datetime.now(timezone.utc).replace(day=1)
                    import calendar
                    last_day = calendar.monthrange(renewal_dt.year, renewal_dt.month)[1]
                    renewal_date = f"{calendar.month_name[renewal_dt.month]} {last_day}, {renewal_dt.year}"
                    asyncio.create_task(send_email(user_email_row["email"], "subscription_confirmed", {
                        "plan_name": plan_label,
                        "credits_added": f"{credits:,}",
                        "amount": f"${price_usd}/mo",
                        "renewal_date": renewal_date,
                    }))
            except Exception as _email_err:
                logger.warning("subscription_confirmed email error: %s", _email_err)

        return {"status": "renewed", "credits_added": credits}

    elif event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        sub_id = sub.get("id")
        if not sub_id:
            return {"status": "no_id"}
        await db.execute("""
            UPDATE api_keys
            SET stripe_subscription_id = NULL, subscription_status = 'cancelled'
            WHERE stripe_subscription_id = $1
        """, sub_id)
        return {"status": "subscription_cancelled"}

    elif event["type"] == "invoice.payment_failed":
        invoice = event["data"]["object"]
        sub_id = invoice.get("subscription")
        if not sub_id:
            return {"status": "no_subscription"}
        # Mark grace period and run dunning logic
        key_row = await db.fetchrow("""
            SELECT k.user_id, k.tier, k.dunning_failure_count, u.email
            FROM api_keys k
            JOIN users u ON u.id = k.user_id
            WHERE k.stripe_subscription_id = $1
        """, sub_id)
        if key_row:
            failure_count = (key_row["dunning_failure_count"] or 0) + 1
            plan_name = key_row["tier"] or "your plan"
            if failure_count >= 3:
                # Downgrade to free
                await db.execute("""
                    UPDATE api_keys
                    SET subscription_status = 'cancelled', tier = 'free',
                        dunning_failure_count = 0
                    WHERE stripe_subscription_id = $1
                """, sub_id)
                import asyncio as _aio
                if key_row["email"]:
                    from notifications import send_account_downgraded_email
                    _aio.create_task(_aio.to_thread(
                        send_account_downgraded_email, key_row["email"], plan_name
                    ))
            else:
                await db.execute("""
                    UPDATE api_keys
                    SET subscription_status = 'past_due',
                        dunning_failure_count = $1
                    WHERE stripe_subscription_id = $2
                """, failure_count, sub_id)
                import asyncio as _aio
                if key_row["email"]:
                    from notifications import send_payment_failed_email
                    _aio.create_task(_aio.to_thread(
                        send_payment_failed_email, key_row["email"], plan_name, failure_count
                    ))
        return {"status": "payment_failed_grace_period"}

    return {"status": "ignored"}
