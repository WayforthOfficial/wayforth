"""routers/provider.py — Provider dashboard API."""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from core.db import get_db
from core.login_security import check_login_lockout, record_login_failure, clear_login_failures
from core.rate_limit import limiter
from services.managed import SERVICE_DISPLAY_NAMES, SERVICE_CONFIGS

logger = logging.getLogger("wayforth")

router = APIRouter()

_PROVIDER_TIERS = {"observer", "intelligence", "premium"}


def _mask_agent_id(agent_id: str | None) -> str:
    """Mask a caller agent/session ID so providers can distinguish agents
    without seeing the full identifier (which is PII-adjacent / cross-tenant)."""
    if not agent_id:
        return "agent_unknown"
    s = str(agent_id)
    if len(s) <= 10:
        return s[:4] + "…"
    return f"{s[:6]}…{s[-4:]}"

_PROVIDER_TIER_PRICES = {
    "intelligence": "STRIPE_PRICE_PROVIDER_INTELLIGENCE",
    "premium":      "STRIPE_PRICE_PROVIDER_PREMIUM",
}

# Annual prices: 17% discount (10 months pricing = 2 months free).
# Intelligence: $99/mo × 10 = $990/yr → $984 billed annually
# Premium:      $299/mo × 10 = $2,990/yr → $2,988 billed annually
_PROVIDER_TIER_PRICES_ANNUAL = {
    "intelligence": "STRIPE_PRICE_PROVIDER_INTELLIGENCE_ANNUAL",
    "premium":      "STRIPE_PRICE_PROVIDER_PREMIUM_ANNUAL",
}

_PROVIDER_TIER_MONTHLY_USD = {
    "intelligence": 99,
    "premium":      299,
}
_PROVIDER_TIER_ANNUAL_USD = {
    "intelligence": 984,    # $82/mo × 12
    "premium":      2_988,  # $249/mo × 12
}


async def _get_provider(request: Request, db):
    """Resolve X-Provider-Token → provider row. Raises 401 if invalid/expired.

    Tokens are stored hashed (sha256) in provider_sessions.token_hash; we hash
    the inbound token and look that up. Constant-time comparison is enforced by
    the unique-index lookup in Postgres.
    """
    import hashlib as _hashlib
    token = request.headers.get("X-Provider-Token", "")
    if not token:
        raise HTTPException(status_code=401, detail={"error": "X-Provider-Token required"})
    token_hash = _hashlib.sha256(token.encode()).hexdigest()
    row = await db.fetchrow("""
        SELECT ps.provider_id, p.company_name, p.email, p.tier, p.verified,
               p.email_verified, p.stripe_customer_id, p.stripe_subscription_id,
               COALESCE(p.billing_interval, 'month') AS billing_interval
        FROM provider_sessions ps
        JOIN providers p ON p.id = ps.provider_id
        WHERE ps.token_hash = $1 AND ps.expires_at > NOW()
    """, token_hash)
    if not row:
        raise HTTPException(status_code=401, detail={"error": "invalid_or_expired_token"})
    return row


async def _require_email_verified(request: Request, db):
    """Resolve provider AND require email_verified=true.

    v0.8.0 Item 2: gates write endpoints behind tokenised email verification.
    Read endpoints (dashboard, queries, earnings, etc.) bypass this — an
    unverified provider can still log in and browse, they just can't trigger
    domain verification or billing changes until they prove control of the
    email address they registered with.
    """
    provider = await _get_provider(request, db)
    if not provider.get("email_verified"):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "email_not_verified",
                "message": "Verify your email before performing this action. Check your inbox or POST /provider/resend-verification.",
            },
        )
    return provider


async def _get_provider_service(db, provider_id):
    """Return the provider's primary service row."""
    return await db.fetchrow(
        "SELECT service_slug, service_name, verified, verification_code "
        "FROM provider_services WHERE provider_id = $1 LIMIT 1",
        provider_id,
    )


async def _verify_dns_txt(domain: str, code: str) -> bool:
    """Check DNS TXT records for `domain` to find `code`.

    S13 (v0.7.8): removed the subprocess dig fallback. `dns.resolver` is a
    hard dep (`dnspython` in pyproject.toml) and is reliable; the fallback
    added attack surface (untrusted domain → argv to a shell tool) for no
    operational benefit. If dnspython itself ever fails, we'd rather log
    and return False than silently shell out.
    """
    try:
        import dns.resolver  # type: ignore
        answers = dns.resolver.resolve(domain, "TXT")
        for rdata in answers:
            for txt_string in rdata.strings:
                if txt_string.decode("utf-8", errors="ignore") == code:
                    return True
        return False
    except Exception as e:
        logger.info("DNS TXT lookup failed for %s: %s", domain, type(e).__name__)
        return False


async def _verify_header_check(endpoint_url: str, code: str) -> bool:
    """Call endpoint_url and check for X-Wayforth-Verify: {code} response header.

    Re-validates the URL (external https, no private/loopback/metadata IPs) and
    refuses to follow redirects — previously `follow_redirects=True` allowed an
    attacker-controlled catalog endpoint to bounce the request to an internal
    host whose response headers could then be probed by side-effect.
    """
    import httpx
    try:
        from core.url_validation import validate_external_url
        validate_external_url(endpoint_url, field_name="endpoint_url")
    except Exception:
        return False  # non-critical: invalid URL fails verification cleanly
    try:
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
            resp = await client.get(endpoint_url)
            return resp.headers.get("X-Wayforth-Verify", "") == code
    except Exception:
        return False  # non-critical: network/timeout failure → verification not confirmed


# ── Provider auth ─────────────────────────────────────────────────────────────

@router.post("/provider/register", tags=["Provider"])
@limiter.limit("10/minute")
async def provider_register(request: Request, db=Depends(get_db)):
    """Register a new provider account.

    v0.8.0 Item 2: account is created with email_verified=false. The caller
    must click the link in the verification email before they can invoke
    write endpoints (domain verification, billing upgrade). The 202 status
    code signals "accepted, pending email confirmation".
    """
    from fastapi.responses import JSONResponse
    body = await request.json()
    company_name = (body.get("company_name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    service_slug = (body.get("service_slug") or "").strip().lower()

    if not all([company_name, email, password, service_slug]):
        raise HTTPException(status_code=422, detail={"error": "company_name, email, password, service_slug required"})
    if len(password) < 8:
        raise HTTPException(status_code=422, detail={"error": "password must be at least 8 characters"})

    existing = await db.fetchval("SELECT id FROM providers WHERE email = $1", email)
    if existing:
        raise HTTPException(status_code=409, detail={"error": "email_already_registered"})

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    verification_code = "wayforth-verify-" + secrets.token_hex(8)
    email_verify_token = secrets.token_urlsafe(32)

    # Resolve service display name
    service_name = SERVICE_DISPLAY_NAMES.get(service_slug, service_slug.title())

    provider_id = await db.fetchval("""
        INSERT INTO providers (
            company_name, email, password_hash,
            email_verification_token, email_verification_sent_at
        )
        VALUES ($1, $2, $3, $4, NOW())
        RETURNING id
    """, company_name, email, password_hash, email_verify_token)

    await db.execute("""
        INSERT INTO provider_services (provider_id, service_slug, service_name, verification_code)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (provider_id, service_slug) DO NOTHING
    """, provider_id, service_slug, service_name, verification_code)

    # Best-effort send: a Resend outage must not prevent registration. If the
    # email fails, the provider can hit POST /provider/resend-verification.
    verify_url = (
        os.environ.get("WAYFORTH_GATEWAY_URL", "https://gateway.wayforth.io")
        + "/provider/verify-email?token=" + email_verify_token
    )
    try:
        from core.email import send_email
        await send_email(email, "provider_verify", {
            "company_name": company_name,
            "verify_url": verify_url,
        })
    except Exception as e:
        logger.error("provider_verify email send failed for %s: %s", email, e)

    # Extract domain from email for DNS instructions
    domain = email.split("@")[-1] if "@" in email else service_slug + ".com"

    return JSONResponse(status_code=202, content={
        "status": "pending_email_verification",
        "message": "Check your inbox to verify your email address. Write endpoints unlock once verified.",
        "provider_id": str(provider_id),
        "email": email,
        "tier": "observer",
        "service_slug": service_slug,
        "verification": {
            "code": verification_code,
            "dns_instructions": f"Add TXT record to {domain}: {verification_code}",
            "header_instructions": f"Return header X-Wayforth-Verify: {verification_code} from your API endpoint",
            "manual_note": "Email support@wayforth.io to verify manually",
        },
    })


@router.get("/provider/verify-email", tags=["Provider"])
async def provider_verify_email(token: str, db=Depends(get_db)):
    """Complete email verification by clicking the link from the registration email.

    v0.8.0 Item 2. Token TTL is 24 hours; expired tokens return 410 so the
    caller can request a fresh one via POST /provider/resend-verification.
    """
    if not token or len(token) < 16:
        raise HTTPException(status_code=404, detail={"error": "invalid_or_unknown_token"})

    row = await db.fetchrow("""
        SELECT id, email_verification_sent_at, email_verified
        FROM providers
        WHERE email_verification_token = $1
    """, token)
    if not row:
        raise HTTPException(status_code=404, detail={"error": "invalid_or_unknown_token"})

    if row["email_verified"]:
        # Token leftover from a previous successful verification — idempotent success.
        return {"email_verified": True, "message": "Email already verified."}

    sent_at = row["email_verification_sent_at"]
    if sent_at is None or (datetime.now(timezone.utc) - sent_at) > timedelta(hours=24):
        raise HTTPException(
            status_code=410,
            detail={
                "error": "token_expired",
                "message": "Token expired. POST /provider/resend-verification to request a new one.",
            },
        )

    await db.execute("""
        UPDATE providers
           SET email_verified = true,
               email_verification_token = NULL
         WHERE id = $1
    """, row["id"])

    return {"email_verified": True, "message": "Email verified. You can now use provider write endpoints."}


@router.post("/provider/resend-verification", tags=["Provider"])
@limiter.limit("3/hour")
async def provider_resend_verification(request: Request, db=Depends(get_db)):
    """Issue a fresh email-verification token. Rate-limited to 3/hour per IP.

    Always returns 200 with a generic message, even if the email is unknown,
    so an attacker can't enumerate registered providers via this endpoint.
    """
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail={"error": "email required"})

    row = await db.fetchrow(
        "SELECT id, company_name, email_verified FROM providers WHERE email = $1",
        email,
    )
    if row and not row["email_verified"]:
        new_token = secrets.token_urlsafe(32)
        await db.execute("""
            UPDATE providers
               SET email_verification_token = $1,
                   email_verification_sent_at = NOW()
             WHERE id = $2
        """, new_token, row["id"])
        verify_url = (
            os.environ.get("WAYFORTH_GATEWAY_URL", "https://gateway.wayforth.io")
            + "/provider/verify-email?token=" + new_token
        )
        try:
            from core.email import send_email
            await send_email(email, "provider_verify", {
                "company_name": row["company_name"],
                "verify_url": verify_url,
            })
        except Exception as e:
            logger.error("provider_verify resend failed for %s: %s", email, e)

    return {
        "status": "sent",
        "message": "If the email is registered and unverified, a fresh link is on its way.",
    }


@router.post("/provider/login", tags=["Provider"])
@limiter.limit("20/minute")
async def provider_login(request: Request, db=Depends(get_db)):
    """Login as a provider. Returns a session token."""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    from core.tier_gates import _get_redis
    from core.rate_limit import get_real_ip
    redis = _get_redis()
    ip = get_real_ip(request)
    await check_login_lockout(email, redis, ip=ip)

    provider = await db.fetchrow(
        "SELECT id, company_name, email, password_hash, tier, verified, mfa_enabled FROM providers WHERE email = $1",
        email,
    )
    if not provider or not bcrypt.checkpw(password.encode(), provider["password_hash"].encode()):
        await record_login_failure(email, redis, ip=ip)
        raise HTTPException(status_code=401, detail={"error": "invalid_credentials"})

    await clear_login_failures(email, redis)

    if provider.get("mfa_enabled"):
        from routers.mfa import issue_mfa_challenge
        challenge = await issue_mfa_challenge(db, "provider", provider["id"])
        return {"mfa_required": True, "mfa_challenge": challenge, "token": None}

    token = "pvdr_" + secrets.token_hex(32)
    import hashlib as _hashlib
    token_hash = _hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)

    # Store only the hash — never the raw token — so a DB/backup/replica leak
    # cannot yield usable session tokens. The raw token is returned to the
    # caller below but not persisted. Lookup (line ~55) matches on token_hash.
    await db.execute("""
        INSERT INTO provider_sessions (provider_id, token_hash, expires_at)
        VALUES ($1, $2, $3)
    """, provider["id"], token_hash, expires_at)

    await db.execute(
        "UPDATE providers SET last_login_at = NOW() WHERE id = $1", provider["id"]
    )

    return {
        "token": token,
        "provider_id": str(provider["id"]),
        "company_name": provider["company_name"],
        "tier": provider["tier"],
        "verified": provider["verified"],
        "expires_at": expires_at.isoformat(),
    }


@router.post("/provider/verify", tags=["Provider"])
@limiter.limit("10/minute")
async def provider_verify(request: Request, db=Depends(get_db)):
    """Verify provider ownership of a service via DNS TXT record or response header.

    v0.8.0 Item 2: gated behind email verification — must prove control of the
    registered email before claiming ownership of a service domain.
    """
    provider = await _require_email_verified(request, db)
    body = await request.json()
    method = (body.get("method") or "dns_txt").strip()

    if method not in ("dns_txt", "header"):
        raise HTTPException(status_code=422, detail={"error": "method must be 'dns_txt' or 'header'"})

    svc = await _get_provider_service(db, provider["provider_id"])
    if not svc:
        raise HTTPException(status_code=404, detail={"error": "no_service_registered"})

    if svc["verified"]:
        return {"verified": True, "message": "Already verified.", "method": method}

    code = svc["verification_code"]
    verified = False

    if method == "dns_txt":
        domain = provider["email"].split("@")[-1] if "@" in provider["email"] else ""
        if not domain:
            raise HTTPException(status_code=422, detail={"error": "cannot_extract_domain_from_email"})
        verified = await _verify_dns_txt(domain, code)
        if not verified:
            return {
                "verified": False,
                "method": "dns_txt",
                "instructions": f"Add TXT record to {domain}: {code}",
                "message": "TXT record not found. DNS propagation can take up to 48 hours.",
            }
    elif method == "header":
        svc_catalog = await db.fetchrow(
            "SELECT endpoint_url FROM services WHERE slug = $1 OR name ILIKE $2 LIMIT 1",
            svc["service_slug"], f"%{svc['service_slug']}%",
        )
        if not svc_catalog or not svc_catalog["endpoint_url"]:
            return {
                "verified": False,
                "method": "header",
                "message": "Could not find your service's endpoint URL in the catalog.",
            }
        verified = await _verify_header_check(svc_catalog["endpoint_url"], code)
        if not verified:
            return {
                "verified": False,
                "method": "header",
                "instructions": f"Return header X-Wayforth-Verify: {code} from {svc_catalog['endpoint_url']}",
                "message": "Verification header not found at your service endpoint.",
            }

    if verified:
        await db.execute("""
            UPDATE provider_services SET verified = true, verified_at = NOW()
            WHERE provider_id = $1
        """, provider["provider_id"])
        await db.execute("""
            UPDATE providers SET verified = true, verification_method = $1
            WHERE id = $2
        """, method, provider["provider_id"])
        return {"verified": True, "method": method, "message": "Service verified successfully."}

    return {"verified": False}


@router.get("/provider/me", tags=["Provider"])
@limiter.limit("60/minute")
async def provider_me(request: Request, db=Depends(get_db)):
    """Return current provider profile and service."""
    provider = await _get_provider(request, db)
    svc = await _get_provider_service(db, provider["provider_id"])
    services_count = await db.fetchval(
        "SELECT COUNT(*) FROM provider_services WHERE provider_id = $1",
        provider["provider_id"],
    ) or 0
    return {
        "provider_id": str(provider["provider_id"]),
        "company_name": provider["company_name"],
        "email": provider["email"],
        "tier": provider["tier"],
        "billing_interval": provider["billing_interval"],
        "monthly_price_usd": _PROVIDER_TIER_MONTHLY_USD.get(provider["tier"]),
        "annual_price_usd":  _PROVIDER_TIER_ANNUAL_USD.get(provider["tier"]),
        "verified": provider["verified"],
        "verification_status": "verified" if provider["verified"] else "pending",
        "services_count": int(services_count),
        "service": {
            "slug": svc["service_slug"] if svc else None,
            "name": svc["service_name"] if svc else None,
            "verified": svc["verified"] if svc else False,
        } if svc else None,
    }


# ── Provider analytics ────────────────────────────────────────────────────────

@router.get("/provider/overview", tags=["Provider"])
@limiter.limit("30/minute")
async def provider_overview(request: Request, db=Depends(get_db)):
    """Provider dashboard overview: WRI, calls, discovery, trends."""
    provider = await _get_provider(request, db)
    svc = await _get_provider_service(db, provider["provider_id"])
    if not svc:
        raise HTTPException(status_code=404, detail={"error": "no_service_registered"})

    slug = svc["service_slug"]

    # Service info from catalog
    svc_row = await db.fetchrow(
        "SELECT id, name, category, wri_score FROM services "
        "WHERE slug = $1 OR name ILIKE $2 LIMIT 1",
        slug, f"%{SERVICE_DISPLAY_NAMES.get(slug, slug)}%",
    )

    # Calls this month and last month from credit_transactions
    calls_this_month = await db.fetchval("""
        SELECT COUNT(*) FROM credit_transactions
        WHERE service_id = $1
          AND type IN ('execution','cross_rail','usage')
          AND created_at >= date_trunc('month', NOW())
    """, slug) or 0

    calls_last_month = await db.fetchval("""
        SELECT COUNT(*) FROM credit_transactions
        WHERE service_id = $1
          AND type IN ('execution','cross_rail','usage')
          AND created_at >= date_trunc('month', NOW()) - INTERVAL '1 month'
          AND created_at < date_trunc('month', NOW())
    """, slug) or 0

    calls_change_pct = 0.0
    if calls_last_month:
        calls_change_pct = round((calls_this_month - calls_last_month) * 100.0 / calls_last_month, 1)

    # Discovery + payment signals from search_analytics (30 days)
    signal_row = await db.fetchrow("""
        SELECT
            COUNT(*) AS total_signals,
            SUM(CASE WHEN payment_followed THEN 1 ELSE 0 END) AS payments,
            MAX(created_at) AS last_seen
        FROM search_analytics
        WHERE clicked_slug = $1
          AND created_at >= NOW() - INTERVAL '30 days'
    """, slug)

    total_signals = int(signal_row["total_signals"] or 0) if signal_row else 0
    payments = int(signal_row["payments"] or 0) if signal_row else 0
    discovery_count = await db.fetchval("""
        SELECT COUNT(*) FROM search_analytics
        WHERE clicked_slug = $1
          AND created_at >= NOW() - INTERVAL '30 days'
    """, slug) or 0

    conversion_rate = round(payments * 100.0 / max(discovery_count, 1), 1)
    payment_rate = round(payments * 100.0 / max(total_signals, 1), 1) if total_signals else 0.0

    # Response time and uptime from service_probes
    probe_stats = None
    avg_ms = None
    uptime_7d = 100.0
    if svc_row:
        probe_stats = await db.fetchrow("""
            SELECT AVG(response_time_ms)::float AS avg_ms,
                   COUNT(*) FILTER (WHERE reachable) AS ok,
                   COUNT(*) AS total
            FROM service_probes
            WHERE service_id = $1::uuid
              AND probed_at >= NOW() - INTERVAL '7 days'
        """, str(svc_row["id"]))
        if probe_stats and probe_stats["total"]:
            avg_ms = round(float(probe_stats["avg_ms"] or 0))
            uptime_7d = round(probe_stats["ok"] * 100.0 / probe_stats["total"], 1)

    # Services count and estimated earnings
    services_listed = await db.fetchval(
        "SELECT COUNT(*) FROM provider_services WHERE provider_id = $1",
        provider["provider_id"],
    ) or 0
    pricing_row = await db.fetchrow(
        "SELECT pricing_usdc FROM services WHERE slug = $1 LIMIT 1", slug
    )
    price_per_call = float(pricing_row["pricing_usdc"] or 0) if pricing_row else 0.0
    estimated_earnings_usdc = round(calls_this_month * price_per_call * 0.985, 4)

    # Category rank
    category = svc_row["category"] if svc_row else None
    wri_score = float(svc_row["wri_score"] or 0) if svc_row else 0.0
    category_rank = 1
    category_total = 1
    if category:
        ranked = await db.fetch("""
            SELECT slug, wri_score FROM services
            WHERE category = $1 AND wri_score IS NOT NULL
            ORDER BY wri_score DESC
        """, category)
        category_total = len(ranked)
        for i, r in enumerate(ranked):
            if r["slug"] == slug:
                category_rank = i + 1
                break

    # WRI trend (weekly buckets, last 5 weeks)
    wri_trend = []
    try:
        hist_rows = await db.fetch("""
            SELECT DATE_TRUNC('week', recorded_at)::date AS week, AVG(wri_score) AS avg_wri
            FROM service_score_history
            WHERE service_id = $1::uuid
              AND recorded_at >= NOW() - INTERVAL '5 weeks'
            GROUP BY 1 ORDER BY 1
        """, str(svc_row["id"]) if svc_row else "00000000-0000-0000-0000-000000000000")
        if hist_rows:
            wri_trend = [{"date": str(r["week"]), "score": round(float(r["avg_wri"]), 1)} for r in hist_rows]
    except Exception:
        pass  # non-critical: WRI trend history falls back to synthetic points below
    if not wri_trend and wri_score:
        today = datetime.now(timezone.utc).date()
        wri_trend = [
            {"date": str(today - timedelta(weeks=4-i)), "score": round(wri_score, 1)}
            for i in range(5)
        ]

    return {
        "service": {
            "slug": slug,
            "name": svc["service_name"],
            "category": category,
            "wri_score": round(wri_score, 1),
            "category_rank": category_rank,
            "category_total": category_total,
            "managed": slug in SERVICE_CONFIGS,
        },
        "period": "last_30_days",
        "stats": {
            "total_calls": calls_this_month + calls_last_month,
            "calls_this_month": calls_this_month,
            "calls_last_month": calls_last_month,
            "calls_change_pct": calls_change_pct,
            "discovery_count": discovery_count,
            "conversion_rate": conversion_rate,
            "avg_response_ms": avg_ms,
            "uptime_7d_pct": uptime_7d,
            "total_signals": total_signals,
            "payment_rate": payment_rate,
            "services_listed": int(services_listed),
            "estimated_earnings_usdc": estimated_earnings_usdc,
        },
        "wri_trend": wri_trend,
    }


@router.get("/provider/queries", tags=["Provider"])
@limiter.limit("30/minute")
async def provider_queries(request: Request, db=Depends(get_db)):
    """Search queries that discovered this service."""
    provider = await _get_provider(request, db)
    svc = await _get_provider_service(db, provider["provider_id"])
    if not svc:
        raise HTTPException(status_code=404, detail={"error": "no_service_registered"})

    slug = svc["service_slug"]
    rows = await db.fetch("""
        SELECT
            query AS query_text,
            COUNT(*) AS times_shown,
            SUM(CASE WHEN payment_followed THEN 1 ELSE 0 END) AS times_executed,
            MAX(created_at) AS last_seen
        FROM search_analytics
        WHERE clicked_slug = $1
          AND created_at > NOW() - INTERVAL '30 days'
          AND query IS NOT NULL
        GROUP BY query
        ORDER BY times_executed DESC, times_shown DESC
        LIMIT 50
    """, slug)

    queries = [
        {
            "query": r["query_text"],
            "times_shown": int(r["times_shown"]),
            "times_executed": int(r["times_executed"] or 0),
            "conversion_rate": round(int(r["times_executed"] or 0) * 100.0 / max(r["times_shown"], 1), 1),
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
        }
        for r in rows
    ]

    if not queries:
        queries = [
            {"query": "fast inference api", "times_shown": 12, "times_executed": 3, "conversion_rate": 25.0, "last_seen": None},
            {"query": "llm inference endpoint", "times_shown": 8, "times_executed": 1, "conversion_rate": 12.5, "last_seen": None},
            {"query": "cheap gpt alternative", "times_shown": 5, "times_executed": 0, "conversion_rate": 0.0, "last_seen": None},
        ]

    return {
        "queries": queries,
        "total_unique_queries": len(queries),
        "period": "last_30_days",
        "is_sample": len(rows) == 0,
    }


@router.get("/provider/competitors", tags=["Provider"])
@limiter.limit("30/minute")
async def provider_competitors(request: Request, db=Depends(get_db)):
    """Competitive positioning within the same category."""
    provider = await _get_provider(request, db)
    svc = await _get_provider_service(db, provider["provider_id"])
    if not svc:
        raise HTTPException(status_code=404, detail={"error": "no_service_registered"})

    slug = svc["service_slug"]
    svc_row = await db.fetchrow(
        "SELECT category, wri_score FROM services WHERE slug = $1 OR name ILIKE $2 LIMIT 1",
        slug, f"%{SERVICE_DISPLAY_NAMES.get(slug, slug)}%",
    )

    if not svc_row or not svc_row["category"]:
        # No catalog entry or category — return sample data
        return {
            "your_service": {"slug": slug, "wri_score": 0.0, "category_rank": 1},
            "competitors": [
                {"label": "Competitor A", "wri_score": 72.4, "category_rank": 2, "signals": 18, "managed": False},
                {"label": "Competitor B", "wri_score": 68.1, "category_rank": 3, "signals": 9, "managed": False},
                {"label": "Competitor C", "wri_score": 61.5, "category_rank": 4, "signals": 4, "managed": True},
            ],
            "category": None,
            "total_in_category": 4,
            "is_sample": True,
        }

    category = svc_row["category"]
    my_wri = float(svc_row["wri_score"] or 0)

    peers = await db.fetch("""
        SELECT s.slug, s.name, s.wri_score,
               COUNT(sa.id) FILTER (WHERE sa.clicked_slug = s.slug) AS signals
        FROM services s
        LEFT JOIN search_analytics sa ON sa.clicked_slug = s.slug
            AND sa.created_at >= NOW() - INTERVAL '30 days'
        WHERE s.category = $1 AND s.wri_score IS NOT NULL AND s.source != 'demo'
        GROUP BY s.slug, s.name, s.wri_score
        ORDER BY s.wri_score DESC
    """, category)

    is_premium = provider["tier"] == "premium"
    managed_slugs = set(SERVICE_CONFIGS.keys())
    competitors_out = []
    my_rank = 1
    for i, p in enumerate(peers):
        if p["slug"] == slug:
            my_rank = i + 1
            continue
        entry: dict = {
            "wri_score": round(float(p["wri_score"]), 1),
            "category_rank": i + 1,
            "signals": int(p["signals"] or 0),
            "managed": p["slug"] in managed_slugs,
        }
        if is_premium:
            entry["slug"] = p["slug"]
            entry["name"] = p["name"]
        else:
            entry["label"] = f"Competitor {chr(65 + len(competitors_out))}"
        competitors_out.append(entry)

    return {
        "your_service": {
            "slug": slug,
            "wri_score": round(my_wri, 1),
            "category_rank": my_rank,
        },
        "competitors": competitors_out[:9],
        "category": category,
        "total_in_category": len(peers),
        "is_sample": False,
    }


@router.get("/provider/performance", tags=["Provider"])
@limiter.limit("30/minute")
async def provider_performance(request: Request, db=Depends(get_db)):
    """Response time and uptime metrics from service probes."""
    provider = await _get_provider(request, db)
    svc = await _get_provider_service(db, provider["provider_id"])
    if not svc:
        raise HTTPException(status_code=404, detail={"error": "no_service_registered"})

    slug = svc["service_slug"]
    svc_row = await db.fetchrow(
        "SELECT id FROM services WHERE slug = $1 OR name ILIKE $2 LIMIT 1",
        slug, f"%{SERVICE_DISPLAY_NAMES.get(slug, slug)}%",
    )
    if not svc_row:
        return {"response_time": None, "uptime": {"last_7d_pct": None}, "probe_history": []}

    svc_id = str(svc_row["id"])

    my_7d = await db.fetchrow("""
        SELECT AVG(response_time_ms)::float AS avg_ms,
               COUNT(*) FILTER (WHERE reachable) AS ok,
               COUNT(*) AS total
        FROM service_probes WHERE service_id = $1::uuid
          AND probed_at >= NOW() - INTERVAL '7 days'
    """, svc_id)

    my_30d = await db.fetchrow("""
        SELECT COUNT(*) FILTER (WHERE reachable) AS ok, COUNT(*) AS total
        FROM service_probes WHERE service_id = $1::uuid
          AND probed_at >= NOW() - INTERVAL '30 days'
    """, svc_id)

    my_avg_ms = round(float(my_7d["avg_ms"] or 0)) if my_7d and my_7d["avg_ms"] else None
    uptime_7d = round(my_7d["ok"] * 100.0 / max(my_7d["total"], 1), 1) if my_7d and my_7d["total"] else 100.0
    uptime_30d = round(my_30d["ok"] * 100.0 / max(my_30d["total"], 1), 1) if my_30d and my_30d["total"] else 100.0
    incidents = int((my_30d["total"] or 0) - (my_30d["ok"] or 0)) if my_30d else 0

    # Category average response time
    svc_category_row = await db.fetchrow("SELECT category FROM services WHERE id = $1::uuid", svc_id)
    category_avg = None
    percentile = None
    if svc_category_row and svc_category_row["category"] and my_avg_ms:
        cat_row = await db.fetchrow("""
            SELECT AVG(sp.response_time_ms)::float AS cat_avg,
                   COUNT(*) FILTER (WHERE sp.response_time_ms < $2) AS faster_count,
                   COUNT(*) AS total_count
            FROM service_probes sp
            JOIN services s ON s.id = sp.service_id
            WHERE s.category = $1
              AND sp.probed_at >= NOW() - INTERVAL '7 days'
              AND sp.reachable = true
        """, svc_category_row["category"], my_avg_ms)
        if cat_row and cat_row["cat_avg"]:
            category_avg = round(float(cat_row["cat_avg"]))
            if cat_row["total_count"]:
                pct = round((cat_row["faster_count"] or 0) * 100 / cat_row["total_count"])
                percentile = f"p{pct}"

    trend = "stable"
    if my_avg_ms and category_avg:
        if my_avg_ms < category_avg * 0.8:
            trend = "faster_than_average"
        elif my_avg_ms > category_avg * 1.3:
            trend = "slower_than_average"

    probe_rows = await db.fetch("""
        SELECT probed_at, reachable, response_time_ms
        FROM service_probes WHERE service_id = $1::uuid
        ORDER BY probed_at DESC LIMIT 20
    """, svc_id)

    return {
        "response_time": {
            "current_avg_ms": my_avg_ms,
            "category_avg_ms": category_avg,
            "percentile": percentile,
            "trend": trend,
        },
        "uptime": {
            "last_7d_pct": uptime_7d,
            "last_30d_pct": uptime_30d,
            "incidents": incidents,
        },
        "probe_history": [
            {
                "probed_at": r["probed_at"].isoformat(),
                "success": r["reachable"],
                "response_ms": r["response_time_ms"],
            }
            for r in probe_rows
        ],
    }


@router.get("/provider/agents", tags=["Provider"])
@limiter.limit("30/minute")
async def provider_agents(request: Request, db=Depends(get_db)):
    """Agent IDs that called this service, with call count and last active."""
    provider = await _get_provider(request, db)
    svc = await _get_provider_service(db, provider["provider_id"])
    if not svc:
        raise HTTPException(status_code=404, detail={"error": "no_service_registered"})

    slug = svc["service_slug"]
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Per-agent call counts from agent_identities joined with search_analytics.
    # Scoped to agents whose searches actually surfaced THIS provider's service
    # (sa.clicked_slug = slug) — without this filter the EXISTS matched any agent
    # that ran any search platform-wide, leaking other providers' caller IDs.
    agent_rows = await db.fetch("""
        SELECT ai.agent_id,
               ai.total_searches AS call_count,
               ai.last_active_at
        FROM agent_identities ai
        WHERE EXISTS (
            SELECT 1 FROM search_analytics sa
            WHERE sa.session_id = ai.agent_id
              AND sa.clicked_slug = $1
        )
        ORDER BY ai.last_active_at DESC NULLS LAST
        LIMIT 50
    """, slug)

    agents = [
        {
            "agent_id": _mask_agent_id(r["agent_id"]),
            "call_count": int(r["call_count"] or 0),
            "last_active": r["last_active_at"].isoformat() if r["last_active_at"] else None,
        }
        for r in agent_rows
    ]

    if not agents:
        agents = [
            {"agent_id": "agent_sample_a1b2c3", "call_count": 47, "last_active": None},
            {"agent_id": "agent_sample_d4e5f6", "call_count": 23, "last_active": None},
            {"agent_id": "agent_sample_g7h8i9", "call_count": 8, "last_active": None},
        ]

    # Wallet-based tier breakdown
    wallet_rows = await db.fetch("""
        SELECT xi.tier,
               COUNT(DISTINCT xi.wallet_address) AS count
        FROM x402_agent_identities xi
        WHERE EXISTS (
            SELECT 1 FROM credit_transactions ct
            WHERE ct.service_id = $1
              AND ct.type IN ('execution','cross_rail','usage')
        )
        GROUP BY xi.tier
    """, slug)

    tier_counts = {t: 0 for t in ("unknown", "emerging", "established", "trusted", "elite")}
    for r in wallet_rows:
        if r["tier"] in tier_counts:
            tier_counts[r["tier"]] = int(r["count"])

    total_unique = sum(tier_counts.values()) or len(agents)

    returning = await db.fetchval("""
        SELECT COUNT(DISTINCT service_id)
        FROM credit_transactions
        WHERE service_id = $1
          AND type IN ('execution','cross_rail','usage')
          AND created_at < $2
    """, slug, month_start) or 0

    new_this_month = await db.fetchval("""
        SELECT COUNT(DISTINCT service_id)
        FROM credit_transactions
        WHERE service_id = $1
          AND type IN ('execution','cross_rail','usage')
          AND created_at >= $2
    """, slug, month_start) or 0

    return {
        "agents": agents,
        "agent_tiers": tier_counts,
        "total_unique_agents": total_unique,
        "returning_agents": int(returning),
        "new_agents_this_month": int(new_this_month),
        "is_sample": len(agent_rows) == 0,
    }


# ── Provider earnings ────────────────────────────────────────────────────────

@router.get("/provider/earnings", tags=["Provider"])
@limiter.limit("30/minute")
async def provider_earnings(request: Request, db=Depends(get_db)):
    """Current-month earnings with 1.5% platform fee / 98.5% provider split."""
    provider = await _get_provider(request, db)
    svc = await _get_provider_service(db, provider["provider_id"])
    if not svc:
        raise HTTPException(status_code=404, detail={"error": "no_service_registered"})

    slug = svc["service_slug"]
    pricing_row = await db.fetchrow(
        "SELECT pricing_usdc FROM services WHERE slug = $1 LIMIT 1", slug
    )
    price_per_call = float(pricing_row["pricing_usdc"] or 0) if pricing_row else 0.0

    calls = await db.fetchval("""
        SELECT COUNT(*) FROM credit_transactions
        WHERE service_id = $1
          AND type IN ('execution', 'cross_rail', 'usage')
          AND created_at >= date_trunc('month', NOW())
    """, slug) or 0

    gross = round(float(calls) * price_per_call, 6)
    fee   = round(gross * 0.015, 6)
    net   = round(gross * 0.985, 6)

    period_start = datetime.now(timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )

    return {
        "period_start":       period_start.isoformat(),
        "service_slug":       slug,
        "calls_count":        int(calls),
        "price_per_call_usdc": price_per_call,
        "gross_revenue_usdc": gross,
        "platform_fee_usdc":  fee,
        "net_payout_usdc":    net,
        "fee_rate":           0.015,
    }


@router.get("/provider/earnings/history", tags=["Provider"])
@limiter.limit("30/minute")
async def provider_earnings_history(request: Request, db=Depends(get_db)):
    """Monthly earnings history for the past 6 months."""
    provider = await _get_provider(request, db)
    svc = await _get_provider_service(db, provider["provider_id"])
    if not svc:
        raise HTTPException(status_code=404, detail={"error": "no_service_registered"})

    slug = svc["service_slug"]
    pricing_row = await db.fetchrow(
        "SELECT pricing_usdc FROM services WHERE slug = $1 LIMIT 1", slug
    )
    price_per_call = float(pricing_row["pricing_usdc"] or 0) if pricing_row else 0.0

    rows = await db.fetch("""
        SELECT
            date_trunc('month', created_at)::date AS month,
            COUNT(*) AS calls
        FROM credit_transactions
        WHERE service_id = $1
          AND type IN ('execution', 'cross_rail', 'usage')
          AND created_at >= date_trunc('month', NOW()) - INTERVAL '5 months'
        GROUP BY 1
        ORDER BY 1 DESC
    """, slug)

    history = []
    for row in rows:
        calls = int(row["calls"] or 0)
        gross = round(calls * price_per_call, 6)
        history.append({
            "month":            str(row["month"]),
            "calls_count":      calls,
            "gross_revenue_usdc": gross,
            "net_payout_usdc":  round(gross * 0.985, 6),
        })

    return {
        "service_slug":        slug,
        "price_per_call_usdc": price_per_call,
        "history":             history,
    }


# ── Provider Boost (Pioneer Program) ─────────────────────────────────────────

_BOOST_CONFIG = {
    "intelligence": {"days": 15, "wri_bonus": 10},
    "premium":      {"days": 30, "wri_bonus": 20},
}


@router.post("/provider/boost/activate", tags=["Provider"])
@limiter.limit("5/minute")
async def provider_boost_activate(request: Request, db=Depends(get_db)):
    """One-time Pioneer Boost activation for Intelligence or Premium providers.

    Requirements:
    - Provider tier must be 'intelligence' or 'premium'
    - Provider's service must be Tier 2 verified (coverage_tier >= 2, consecutive_failures < 3)
    - boost_used must be FALSE — this is a lifetime, one-time benefit

    On success: sets boost_used=TRUE (permanent), boost_activated_at, boost_expires_at,
    boost_tier, boost_wri_bonus; records to audit log.
    """
    from datetime import datetime, timezone, timedelta
    from core.audit import log_provider_action

    provider = await _require_email_verified(request, db)
    provider_id = provider["provider_id"]
    tier = provider["tier"]

    if tier not in _BOOST_CONFIG:
        raise HTTPException(status_code=403, detail={
            "error": "ineligible_tier",
            "message": "Pioneer Boost requires an Intelligence or Premium subscription.",
            "current_tier": tier,
        })

    existing = await db.fetchrow(
        "SELECT boost_used FROM providers WHERE id = $1", provider_id
    )
    if existing and existing["boost_used"]:
        raise HTTPException(status_code=409, detail={
            "error": "boost_already_used",
            "message": "Pioneer Boost is a one-time, lifetime benefit and has already been activated on this account.",
        })

    svc = await _get_provider_service(db, provider_id)
    if not svc:
        raise HTTPException(status_code=404, detail={"error": "no_service_registered"})

    service_row = await db.fetchrow(
        "SELECT coverage_tier, consecutive_failures FROM services WHERE slug = $1",
        svc["service_slug"],
    )
    if not service_row:
        raise HTTPException(status_code=404, detail={"error": "service_not_found"})

    tier2_ok = (
        (service_row["coverage_tier"] or 0) >= 2
        and (service_row["consecutive_failures"] or 0) < 3
    )
    if not tier2_ok:
        raise HTTPException(status_code=403, detail={
            "error": "tier2_required",
            "message": "Your service must be Tier 2 verified (90%+ uptime, 7 days) to activate Pioneer Boost.",
            "coverage_tier": service_row["coverage_tier"],
            "consecutive_failures": service_row["consecutive_failures"],
        })

    cfg = _BOOST_CONFIG[tier]
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=cfg["days"])

    # Atomically claim the one-time boost. The earlier SELECT-based check is a
    # fast-path; this conditional UPDATE ... WHERE boost_used = FALSE is the
    # authoritative guard — it locks the row and flips the flag in one statement,
    # so two concurrent activations (within the 5/min budget) cannot both pass.
    # Only the request whose UPDATE returns a row activated the boost.
    activated = await db.fetchval("""
        UPDATE providers
           SET boost_used         = TRUE,
               boost_activated_at = $2,
               boost_expires_at   = $3,
               boost_tier         = $4,
               boost_wri_bonus    = $5,
               boost_paused       = FALSE
         WHERE id = $1 AND boost_used = FALSE
         RETURNING id
    """, provider_id, now, expires_at, tier, cfg["wri_bonus"])
    if activated is None:
        raise HTTPException(status_code=409, detail={
            "error": "boost_already_used",
            "message": "Pioneer Boost is a one-time, lifetime benefit and has already been activated on this account.",
        })

    await log_provider_action(
        db,
        str(provider_id),
        provider["email"],
        "pioneer_boost_activated",
        target_resource=svc["service_slug"],
        payload={
            "tier": tier,
            "wri_bonus": cfg["wri_bonus"],
            "expires_at": expires_at.isoformat(),
        },
        request=request,
    )

    return {
        "boost_activated": True,
        "boost_tier":      tier,
        "boost_wri_bonus": cfg["wri_bonus"],
        "boost_activated_at": now.isoformat(),
        "boost_expires_at":   expires_at.isoformat(),
        "days":               cfg["days"],
        "service_slug":       svc["service_slug"],
    }


@router.get("/provider/boost/status", tags=["Provider"])
@limiter.limit("30/minute")
async def provider_boost_status(request: Request, db=Depends(get_db)):
    """Return current Pioneer Boost state for the authenticated provider."""
    from datetime import datetime, timezone

    provider = await _get_provider(request, db)
    row = await db.fetchrow("""
        SELECT boost_used, boost_activated_at, boost_expires_at,
               boost_tier, boost_wri_bonus, boost_paused
          FROM providers WHERE id = $1
    """, provider["provider_id"])
    if not row:
        raise HTTPException(status_code=404, detail={"error": "provider_not_found"})

    now = datetime.now(timezone.utc)
    expires_at = row["boost_expires_at"]
    boost_active = (
        bool(row["boost_used"])
        and not bool(row["boost_paused"])
        and expires_at is not None
        and expires_at > now
    )
    days_remaining: int | None = None
    if expires_at:
        days_remaining = max(0, (expires_at - now).days)

    return {
        "boost_used":         bool(row["boost_used"]),
        "boost_active":       boost_active,
        "boost_paused":       bool(row["boost_paused"]),
        "boost_tier":         row["boost_tier"],
        "boost_wri_bonus":    row["boost_wri_bonus"] if boost_active else 0,
        "boost_activated_at": row["boost_activated_at"].isoformat() if row["boost_activated_at"] else None,
        "boost_expires_at":   expires_at.isoformat() if expires_at else None,
        "days_remaining":     days_remaining,
    }


# ── Provider service management ───────────────────────────────────────────────

_VALID_CATEGORIES = {"inference", "data", "translation"}


def _valid_slug(slug: str) -> bool:
    return bool(slug) and len(slug) <= 64 and all(c.isalnum() or c in "-_" for c in slug)


async def _owned_service_or_404(db, provider_id, slug: str):
    """Return the provider_services row for (provider_id, slug) or raise 404.
    Enforces that the authenticated provider owns the slug."""
    row = await db.fetchrow(
        "SELECT id, service_slug FROM provider_services WHERE provider_id = $1 AND service_slug = $2",
        provider_id, slug,
    )
    if not row:
        raise HTTPException(status_code=404, detail={
            "error": "service_not_found",
            "message": "No service with that slug is registered under your account.",
        })
    return row


@router.post("/provider/services", tags=["Provider"])
@limiter.limit("10/minute")
async def provider_add_service(request: Request, db=Depends(get_db)):
    """Add a new service to the catalog under the authenticated provider."""
    from core.url_validation import validate_external_url

    provider = await _require_email_verified(request, db)
    provider_id = provider["provider_id"]
    body = await request.json()

    name = (body.get("name") or "").strip()
    slug = (body.get("slug") or "").strip().lower()
    description = (body.get("description") or "").strip()
    category = (body.get("category") or "").strip().lower()
    endpoint_url = (body.get("endpoint_url") or "").strip()
    try:
        price_per_call = float(body.get("price_per_call") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail={"error": "price_per_call must be a number"})

    if not all([name, slug, category, endpoint_url]):
        raise HTTPException(status_code=422, detail={"error": "name, slug, category, endpoint_url are required"})
    if not _valid_slug(slug):
        raise HTTPException(status_code=422, detail={"error": "slug must be <=64 chars, alphanumeric/hyphen/underscore only"})
    if category not in _VALID_CATEGORIES:
        raise HTTPException(status_code=422, detail={"error": f"category must be one of: {', '.join(sorted(_VALID_CATEGORIES))}"})
    if len(name) > 100:
        raise HTTPException(status_code=422, detail={"error": "name must be 100 characters or fewer"})
    if len(description) > 500:
        raise HTTPException(status_code=422, detail={"error": "description must be 500 characters or fewer"})
    if price_per_call < 0:
        raise HTTPException(status_code=422, detail={"error": "price_per_call must be >= 0"})
    # SSRF defense — reject internal/loopback/non-https endpoints.
    validate_external_url(endpoint_url, field_name="endpoint_url")

    # Slug uniqueness is global (provider_services_slug_unique); reject early.
    if await db.fetchval("SELECT 1 FROM provider_services WHERE service_slug = $1", slug) \
       or await db.fetchval("SELECT 1 FROM services WHERE slug = $1", slug):
        raise HTTPException(status_code=409, detail={"error": "slug_taken", "slug": slug})

    try:
        async with db.transaction():
            service_id = await db.fetchval(
                """INSERT INTO services (name, slug, description, endpoint_url, category,
                                         pricing_usdc, source, coverage_tier, active)
                   VALUES ($1, $2, $3, $4, $5, $6, 'provider', 0, TRUE) RETURNING id""",
                name, slug, description, endpoint_url, category, price_per_call,
            )
            await db.execute(
                """INSERT INTO provider_services (provider_id, service_slug, service_name)
                   VALUES ($1, $2, $3)""",
                provider_id, slug, name,
            )
    except Exception as exc:
        # Unique violations (slug raced, or endpoint_url already in catalog).
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(status_code=409, detail={"error": "slug_or_endpoint_taken"})
        raise

    return JSONResponse(status_code=201, content={
        "status": "created",
        "service_id": str(service_id),
        "slug": slug,
        "name": name,
        "category": category,
        "coverage_tier": 0,
        "message": "Service added. Coverage tier is computed asynchronously by the health monitor.",
    })


@router.patch("/provider/services/{slug}", tags=["Provider"])
@limiter.limit("20/minute")
async def provider_edit_service(slug: str, request: Request, db=Depends(get_db)):
    """Edit an existing service owned by the authenticated provider.
    Editable: name, description, price_per_call, endpoint_url. Slug and category
    are immutable (a slug change is a new service)."""
    from core.url_validation import validate_external_url

    provider = await _require_email_verified(request, db)
    provider_id = provider["provider_id"]
    slug = slug.strip().lower()
    await _owned_service_or_404(db, provider_id, slug)

    body = await request.json()
    sets, args = [], []
    if "name" in body:
        name = (body.get("name") or "").strip()
        if not name or len(name) > 100:
            raise HTTPException(status_code=422, detail={"error": "name must be 1-100 characters"})
        args.append(name); sets.append(f"name = ${len(args)}")
    if "description" in body:
        description = (body.get("description") or "").strip()
        if len(description) > 500:
            raise HTTPException(status_code=422, detail={"error": "description must be 500 characters or fewer"})
        args.append(description); sets.append(f"description = ${len(args)}")
    if "price_per_call" in body:
        try:
            price = float(body.get("price_per_call"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"error": "price_per_call must be a number"})
        if price < 0:
            raise HTTPException(status_code=422, detail={"error": "price_per_call must be >= 0"})
        args.append(price); sets.append(f"pricing_usdc = ${len(args)}")
    if "endpoint_url" in body:
        endpoint_url = (body.get("endpoint_url") or "").strip()
        validate_external_url(endpoint_url, field_name="endpoint_url")
        args.append(endpoint_url); sets.append(f"endpoint_url = ${len(args)}")

    if not sets:
        raise HTTPException(status_code=422, detail={"error": "no editable fields provided",
                            "editable": ["name", "description", "price_per_call", "endpoint_url"]})

    args.append(slug)
    try:
        await db.execute(
            f"UPDATE services SET {', '.join(sets)}, updated_at = NOW() WHERE slug = ${len(args)}",
            *args,
        )
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(status_code=409, detail={"error": "endpoint_url_taken"})
        raise
    # Keep provider_services.service_name in sync if the name changed.
    if "name" in body:
        await db.execute(
            "UPDATE provider_services SET service_name = $1 WHERE provider_id = $2 AND service_slug = $3",
            (body.get("name") or "").strip(), provider_id, slug,
        )
    return {"status": "updated", "slug": slug, "updated_fields": [s.split(" = ")[0] for s in sets]}


@router.delete("/provider/services/{slug}", tags=["Provider"])
@limiter.limit("20/minute")
async def provider_delete_service(slug: str, request: Request, db=Depends(get_db)):
    """Soft-delete a service owned by the authenticated provider. The catalog row
    and WayforthRank signal history are preserved; the service stops surfacing in
    search/fallback (active=FALSE)."""
    provider = await _require_email_verified(request, db)
    provider_id = provider["provider_id"]
    slug = slug.strip().lower()
    await _owned_service_or_404(db, provider_id, slug)

    await db.execute("UPDATE services SET active = FALSE, updated_at = NOW() WHERE slug = $1", slug)
    return {"status": "deleted", "slug": slug, "soft_delete": True,
            "message": "Service deactivated. It no longer appears in search; its history is retained."}


# ── Provider billing ──────────────────────────────────────────────────────────

@router.post("/provider/billing/upgrade", tags=["Provider"])
@limiter.limit("10/minute")
async def provider_billing_upgrade(request: Request, db=Depends(get_db)):
    """Create a Stripe checkout session to upgrade the provider's tier.

    Accepts:
      tier: "intelligence" | "premium"
      billing_interval: "month" | "year"  (default: "month")

    Annual billing: 17% discount (10 months pricing).
      Intelligence: $984/yr ($82/mo)
      Premium:      $2,988/yr ($249/mo)

    The Launch Boost (15-day / 30-day) is tied to tier, not billing interval —
    annual subscribers get the same boost as monthly subscribers of the same tier.

    Provider plans grant dashboard access (analytics, WRI, competitor data).
    There is no monthly credit pool to reset — billing_interval only affects
    Stripe charge cadence, not any in-app credit counter.
    """
    import stripe
    provider = await _require_email_verified(request, db)
    body = await request.json()
    target_tier = (body.get("tier") or "").strip().lower()
    billing_interval = (body.get("billing_interval") or "month").strip().lower()

    if target_tier not in ("intelligence", "premium"):
        raise HTTPException(status_code=422, detail={
            "error": "invalid_tier",
            "valid": ["intelligence", "premium"],
        })
    if billing_interval not in ("month", "year"):
        raise HTTPException(status_code=422, detail={
            "error": "invalid_billing_interval",
            "valid": ["month", "year"],
        })

    STRIPE_MOCK = (
        os.environ.get("STRIPE_SECRET_KEY", "").startswith("sk_test_")
        or os.environ.get("STRIPE_MOCK", "false").lower() == "true"
        or not os.environ.get("STRIPE_SECRET_KEY", "")
    )

    price_map = _PROVIDER_TIER_PRICES_ANNUAL if billing_interval == "year" else _PROVIDER_TIER_PRICES
    price_env = price_map[target_tier]
    price_id = os.environ.get(price_env, "")

    amount_display = (
        _PROVIDER_TIER_ANNUAL_USD[target_tier] if billing_interval == "year"
        else _PROVIDER_TIER_MONTHLY_USD[target_tier]
    )

    if not price_id or STRIPE_MOCK:
        # Mock mode: upgrade directly and record billing_interval
        await db.execute(
            "UPDATE providers SET tier = $1, billing_interval = $2 WHERE id = $3",
            target_tier, billing_interval, provider["provider_id"],
        )
        return {
            "checkout_url": None,
            "mock": True,
            "tier": target_tier,
            "billing_interval": billing_interval,
            "amount_usd": amount_display,
            "message": (
                f"Stripe not configured. Tier set to {target_tier} "
                f"({billing_interval}ly, ${amount_display}) in mock mode."
            ),
        }

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url="https://wayforth.io/providers/dashboard?upgrade=success",
            cancel_url="https://wayforth.io/providers/pricing",
            customer_email=provider["email"],
            subscription_data={
                "metadata": {
                    "provider_id":        str(provider["provider_id"]),
                    "provider_tier":      target_tier,
                    "billing_interval":   billing_interval,
                }
            },
            metadata={
                "provider_id":      str(provider["provider_id"]),
                "provider_tier":    target_tier,
                "billing_interval": billing_interval,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Stripe error: {exc}")

    await db.execute(
        "UPDATE providers SET stripe_customer_id = $1, billing_interval = $2 WHERE id = $3",
        session.get("customer"), billing_interval, provider["provider_id"],
    )

    return {
        "checkout_url": session["url"],
        "billing_interval": billing_interval,
        "amount_usd": amount_display,
    }
