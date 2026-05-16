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
from core.credits import _dispatch_webhooks
from core.db import get_db
from core.rate_limit import limiter, get_real_ip

logger = logging.getLogger("wayforth")

router = APIRouter()

# ── Local STRIPE_MOCK flag ────────────────────────────────────────────────────

STRIPE_MOCK = (
    not os.environ.get("STRIPE_SECRET_KEY", "")
    or os.environ.get("STRIPE_MOCK", "false").lower() == "true"
    or os.environ.get("STRIPE_SECRET_KEY", "").startswith("sk_test_")
)

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
    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
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
    if not req.endpoint_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="endpoint_url must start with https://")
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
            # Custom-priced plans: include with descriptive fields instead of numeric price
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
    return {"packages": result}


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

    if package not in STRIPE_PACKAGES:
        raise HTTPException(status_code=400, detail="Invalid package")

    pkg = STRIPE_PACKAGES[package]

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
                    }
                },
                metadata={
                    "user_id": str(key_record['user_id']),
                    "package": package,
                    "credits": str(pkg["credits"]),
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
    """Test endpoint: add credits without Stripe. Only works when STRIPE_MOCK=true or no Stripe key set."""
    if not STRIPE_MOCK:
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

            # Keep api_keys.tier in sync — this is the authoritative tier field
            # used by rate limiting and feature gating.
            _new_tier = package.split("_")[0] if package else None
            if _new_tier in ("builder", "starter", "pro", "growth"):
                await db.execute(
                    "UPDATE api_keys SET tier = $1 WHERE user_id = $2::uuid",
                    _new_tier, user_id,
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
