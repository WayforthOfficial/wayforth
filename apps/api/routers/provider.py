"""routers/provider.py — Provider dashboard API."""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.requests import Request

from core.db import get_db
from core.rate_limit import limiter
from services.managed import SERVICE_DISPLAY_NAMES, SERVICE_CONFIGS

logger = logging.getLogger("wayforth")

router = APIRouter()

_PROVIDER_TIERS = {"observer", "intelligence", "premium"}

_PROVIDER_TIER_PRICES = {
    "intelligence": "STRIPE_PRICE_PROVIDER_INTELLIGENCE",
    "premium":      "STRIPE_PRICE_PROVIDER_PREMIUM",
}


async def _get_provider(request: Request, db):
    """Resolve X-Provider-Token → provider row. Raises 401 if invalid/expired."""
    token = request.headers.get("X-Provider-Token", "")
    if not token:
        raise HTTPException(status_code=401, detail={"error": "X-Provider-Token required"})
    row = await db.fetchrow("""
        SELECT ps.provider_id, p.company_name, p.email, p.tier, p.verified,
               p.stripe_customer_id, p.stripe_subscription_id
        FROM provider_sessions ps
        JOIN providers p ON p.id = ps.provider_id
        WHERE ps.token = $1 AND ps.expires_at > NOW()
    """, token)
    if not row:
        raise HTTPException(status_code=401, detail={"error": "invalid_or_expired_token"})
    return row


async def _get_provider_service(db, provider_id):
    """Return the provider's primary service row."""
    return await db.fetchrow(
        "SELECT service_slug, service_name, verified, verification_code "
        "FROM provider_services WHERE provider_id = $1 LIMIT 1",
        provider_id,
    )


async def _verify_dns_txt(domain: str, code: str) -> bool:
    """Check DNS TXT records for `domain` to find `code`."""
    try:
        import dns.resolver  # type: ignore
        answers = dns.resolver.resolve(domain, "TXT")
        for rdata in answers:
            for txt_string in rdata.strings:
                if txt_string.decode("utf-8", errors="ignore") == code:
                    return True
        return False
    except Exception:
        pass
    # Fallback: subprocess dig
    try:
        import subprocess
        result = subprocess.run(
            ["dig", "+short", "TXT", domain],
            capture_output=True, text=True, timeout=5,
        )
        return code in result.stdout
    except Exception:
        return False


async def _verify_header_check(endpoint_url: str, code: str) -> bool:
    """Call endpoint_url and check for X-Wayforth-Verify: {code} response header."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(endpoint_url, follow_redirects=True)
            return resp.headers.get("X-Wayforth-Verify", "") == code
    except Exception:
        return False


# ── Provider auth ─────────────────────────────────────────────────────────────

@router.post("/provider/register", tags=["Provider"])
@limiter.limit("10/minute")
async def provider_register(request: Request, db=Depends(get_db)):
    """Register a new provider account."""
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

    # Resolve service display name
    service_name = SERVICE_DISPLAY_NAMES.get(service_slug, service_slug.title())

    provider_id = await db.fetchval("""
        INSERT INTO providers (company_name, email, password_hash)
        VALUES ($1, $2, $3)
        RETURNING id
    """, company_name, email, password_hash)

    await db.execute("""
        INSERT INTO provider_services (provider_id, service_slug, service_name, verification_code)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (provider_id, service_slug) DO NOTHING
    """, provider_id, service_slug, service_name, verification_code)

    # Extract domain from email for DNS instructions
    domain = email.split("@")[-1] if "@" in email else service_slug + ".com"

    return {
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
    }


@router.post("/provider/login", tags=["Provider"])
@limiter.limit("20/minute")
async def provider_login(request: Request, db=Depends(get_db)):
    """Login as a provider. Returns a session token."""
    body = await request.json()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""

    provider = await db.fetchrow(
        "SELECT id, company_name, email, password_hash, tier, verified FROM providers WHERE email = $1",
        email,
    )
    if not provider or not bcrypt.checkpw(password.encode(), provider["password_hash"].encode()):
        raise HTTPException(status_code=401, detail={"error": "invalid_credentials"})

    token = "pvdr_" + secrets.token_hex(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)

    await db.execute("""
        INSERT INTO provider_sessions (provider_id, token, expires_at)
        VALUES ($1, $2, $3)
    """, provider["id"], token, expires_at)

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
    """Verify provider ownership of a service via DNS TXT record or response header."""
    provider = await _get_provider(request, db)
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
        pass
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

    # Per-agent call counts from agent_identities joined with search_analytics
    agent_rows = await db.fetch("""
        SELECT ai.agent_id,
               ai.total_searches AS call_count,
               ai.last_active_at
        FROM agent_identities ai
        WHERE EXISTS (
            SELECT 1 FROM search_analytics sa
            WHERE sa.session_id = ai.agent_id
        )
        ORDER BY ai.last_active_at DESC NULLS LAST
        LIMIT 50
    """)

    agents = [
        {
            "agent_id": r["agent_id"],
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


# ── Provider billing ──────────────────────────────────────────────────────────

@router.post("/provider/billing/upgrade", tags=["Provider"])
@limiter.limit("10/minute")
async def provider_billing_upgrade(request: Request, db=Depends(get_db)):
    """Create a Stripe checkout session to upgrade the provider's tier."""
    import stripe
    provider = await _get_provider(request, db)
    body = await request.json()
    target_tier = (body.get("tier") or "").strip().lower()

    if target_tier not in ("intelligence", "premium"):
        raise HTTPException(status_code=422, detail={
            "error": "invalid_tier",
            "valid": ["intelligence", "premium"],
        })

    STRIPE_MOCK = (
        os.environ.get("STRIPE_SECRET_KEY", "").startswith("sk_test_")
        or os.environ.get("STRIPE_MOCK", "false").lower() == "true"
        or not os.environ.get("STRIPE_SECRET_KEY", "")
    )

    price_env = _PROVIDER_TIER_PRICES[target_tier]
    price_id = os.environ.get(price_env, "")
    if not price_id or STRIPE_MOCK:
        # Mock mode: just upgrade directly
        await db.execute(
            "UPDATE providers SET tier = $1 WHERE id = $2",
            target_tier, provider["provider_id"],
        )
        return {
            "checkout_url": None,
            "mock": True,
            "tier": target_tier,
            "message": f"Stripe not configured. Tier set to {target_tier} in mock mode.",
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
                    "provider_id": str(provider["provider_id"]),
                    "provider_tier": target_tier,
                }
            },
            metadata={
                "provider_id": str(provider["provider_id"]),
                "provider_tier": target_tier,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Stripe error: {exc}")

    await db.execute(
        "UPDATE providers SET stripe_customer_id = $1 WHERE id = $2",
        session.get("customer"), provider["provider_id"],
    )

    return {"checkout_url": session["url"]}
