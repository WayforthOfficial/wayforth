"""
apps/api/tests/test_suite_v062.py
WAYFORTH v0.6.2 security hardening regression tests — T162–T170

Run: pytest apps/api/tests/test_suite_v062.py -v

All tests hit the live Railway deployment (BASE_URL from test_suite_v060).
Tests assert the v0.6.2 fixed behavior; they will fail against v0.6.1.
"""

import pytest
import httpx
import pytest_asyncio

from tests.test_suite_v060 import BASE_URL, API_KEY, _uh, rec


@pytest_asyncio.fixture
async def c():
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=60.0, follow_redirects=True
    ) as client:
        yield client


# ─────────────────────────────────────────────────────────────────────────────
# T162 — V1 auth-bypass surface
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T162_auth_surface_split(c):
    """Public endpoints reachable without key; protected endpoints 401 without key."""
    # Public — no X-Wayforth-API-Key
    for path in ("/run/intents", "/services/groq/health", "/openapi.json", "/sitemap.xml"):
        r = await c.get(path)
        assert r.status_code == 200, (
            f"{path} must be public, got {r.status_code}: {r.text[:200]}"
        )

    # Protected — no key must return 401
    for path in ("/account/usage/history", "/account/wayf-points/history"):
        r = await c.get(path)
        assert r.status_code == 401, (
            f"{path} must require auth, got {r.status_code}: {r.text[:200]}"
        )

    r = await c.post("/execute/batch", json={"calls": [{"slug": "openweather", "params": {"city": "x"}}]})
    assert r.status_code == 401, f"/execute/batch must require auth, got {r.status_code}"

    r = await c.post("/run", json={"intent": "summarize hello", "stream": True})
    assert r.status_code == 401, f"/run stream:true must require auth, got {r.status_code}"


# ─────────────────────────────────────────────────────────────────────────────
# T163 — V2 SQL-injection payloads on WayforthQL filters
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T163_wayforthql_filter_injection(c):
    """Filter injection payloads must 422 (Pydantic validation), never 500."""
    payloads = [
        {"region": "us' OR '1'='1"},
        {"latency_max": "1; DROP TABLE services;--"},
        {"payment_rail": "x402'; DELETE FROM services;--"},
    ]
    for filt in payloads:
        body = {"query": "test", "limit": 5, **filt}
        r = rec(await c.post("/query", headers=_uh(), json=body))
        assert r.status_code == 422, (
            f"/query with {filt!r} must be 422, got {r.status_code}: {r.text[:300]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# T164 — V3 slug-injection on /services/{slug}/health
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T164_services_slug_injection(c):
    """Path-traversal and SQL-style slug payloads must 404 (or 422), never 500."""
    payloads = [
        "/services/..%2F..%2F..%2Fetc%2Fpasswd/health",
        "/services/groq%3BDROP%20TABLE%20services/health",
        "/services/' OR 1=1 --/health",
    ]
    for path in payloads:
        r = rec(await c.get(path))
        assert r.status_code in (404, 422), (
            f"{path} must 404/422, got {r.status_code}: {r.text[:200]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# T165 — V4 webhook SSRF blocklist
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T165_webhook_ssrf_blocked(c):
    """Webhook registration must reject internal/loopback/link-local URLs
    BEFORE the URL is stored or any HTTP request is made.

    /webhooks/register has a 5/minute rate limit — keep this list at 4 entries
    so the rate limit itself doesn't mask the SSRF defense we're verifying.
    Each is a distinct private/internal target class:
      - AWS / GCP-style cloud metadata IP (link-local)
      - Railway internal service DNS (.internal suffix)
      - localhost hostname
      - RFC1918 literal
    """
    internal_urls = [
        "https://169.254.169.254/latest/meta-data/",  # link-local cloud metadata
        "https://redis.railway.internal/",            # internal-DNS suffix
        "https://localhost/webhook",                  # localhost hostname
        "https://10.0.0.1/webhook",                   # RFC1918 literal
    ]
    for url in internal_urls:
        r = rec(await c.post("/webhooks/register", headers=_uh(), json={
            "url": url, "events": ["tier_change"],
        }))
        assert r.status_code == 422, (
            f"webhook register {url!r} must be 422, got {r.status_code}: {r.text[:200]}"
        )
        d = r.json()
        detail = d.get("detail", d)
        assert detail.get("error") in ("internal_target_forbidden", "invalid_url"), (
            f"expected SSRF block error for {url!r}, got {detail!r}"
        )

    # WRI alerts has its own 20/min rate limit — verify SSRF defense covers it too.
    r = rec(await c.post("/webhooks/wri-alerts", headers=_uh(), json={
        "threshold_score": 75.0,
        "min_signals": 5,
        "notify_url": "https://169.254.169.254/",
    }))
    assert r.status_code == 422, (
        f"wri-alerts with internal notify_url must be 422, got {r.status_code}: {r.text[:200]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# T166 — V6 batch credit gate atomicity
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T166_execute_batch_atomic_credit_gate(c):
    """If the batch's total credit cost exceeds the caller's balance, the
    server must reject the whole batch up-front with 402 and no partial results.

    The response shape on 402 must include credits_required, credits_balance,
    and calls_in_batch — fields v0.6.2 introduced specifically for the gate."""
    # 5 ultra-stability calls = 5 * 100 = 500 credits. Test account may or may
    # not have that balance — what we verify is the v0.6.2 *contract*:
    #   - 200 with len(results)==5 (balance >= 500), OR
    #   - 402 with the new atomic-gate fields and no `results` key.
    r = rec(await c.post("/execute/batch", headers=_uh(), json={
        "calls": [
            {"slug": "stability", "params": {"prompt": "test", "quality": "ultra"}},
        ] * 5
    }))
    if r.status_code == 402:
        d = r.json()
        detail = d.get("detail", d)
        assert detail.get("error") == "insufficient_credits", (
            f"unexpected 402 error: {detail!r}"
        )
        for field in ("credits_required", "credits_balance", "calls_in_batch"):
            assert field in detail, (
                f"v0.6.2 atomic gate response missing {field!r}: {detail!r}"
            )
        assert detail["calls_in_batch"] == 5, (
            f"expected calls_in_batch=5, got {detail['calls_in_batch']}"
        )
        assert "results" not in detail, "402 response must not include partial results"
    else:
        assert r.status_code == 200, (
            f"batch must be 200 or 402, got {r.status_code}: {r.text[:300]}"
        )
        d = r.json()
        assert "results" in d and len(d["results"]) == 5


# ─────────────────────────────────────────────────────────────────────────────
# T167 — V8 pagination abuse
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T167_query_pagination_bounds(c):
    """limit=999999 and offset=-1 must return 422, not run a massive query."""
    r = rec(await c.post("/query", headers=_uh(), json={
        "query": "test", "limit": 999999, "offset": 0,
    }))
    assert r.status_code == 422, f"limit=999999 must 422, got {r.status_code}: {r.text[:200]}"
    detail = r.json().get("detail", {})
    assert detail.get("error") == "invalid_limit", f"unexpected detail: {detail!r}"

    r = rec(await c.post("/query", headers=_uh(), json={
        "query": "test", "limit": 5, "offset": -1,
    }))
    assert r.status_code == 422, f"offset=-1 must 422, got {r.status_code}: {r.text[:200]}"
    detail = r.json().get("detail", {})
    assert detail.get("error") == "invalid_offset", f"unexpected detail: {detail!r}"


# ─────────────────────────────────────────────────────────────────────────────
# T168 — V10 X-Request-ID injection
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T168_request_id_server_generated(c):
    """Server must generate its own X-Request-ID UUID; client-provided value
    must never be reflected in the response or used for log correlation."""
    import re
    client_provided = "INJECTED-EVIL-VALUE-123"
    r = await c.get("/status", headers={"X-Request-ID": client_provided})
    assert r.status_code == 200
    server_id = r.headers.get("X-Request-ID", "")
    assert server_id, "X-Request-ID header missing on response"
    assert server_id != client_provided, (
        f"server reflected client-provided X-Request-ID: {server_id!r}"
    )
    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    assert uuid_re.match(server_id), f"X-Request-ID is not a UUID: {server_id!r}"


# ─────────────────────────────────────────────────────────────────────────────
# T169 — V11 OpenAPI exposure of admin routes
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T169_openapi_hides_admin_routes(c):
    """GET /openapi.json must not list /admin, /admin-api, or /tier3/admin paths."""
    r = await c.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    paths = list(schema.get("paths", {}).keys())
    leaked = [
        p for p in paths
        if p.startswith("/admin")
        or p.startswith("/admin-api")
        or p == "/tier3/admin"
    ]
    assert not leaked, f"admin routes leaked into public OpenAPI schema: {leaked}"


# ─────────────────────────────────────────────────────────────────────────────
# T170 — V12 streaming intent priority (regression-confirms v0.6.1 fix
#         survives v0.6.2 changes)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T170_stream_inference_intent_routes_to_llm(c):
    """POST /run {intent: 'inference say hello', stream: true} must route to
    an inference service (groq/together), not a TTS service (elevenlabs)."""
    r = await c.post("/run", headers=_uh(), json={
        "intent": "inference say hello",
        "stream": True,
    })
    # On free/missing keys the call may 503/422 instead of streaming, but it
    # must never be 400 streaming_not_supported (which would mean it routed to
    # a non-streaming service like elevenlabs).
    if r.status_code == 400:
        d = r.json()
        detail = d.get("detail", d)
        assert detail.get("error") != "streaming_not_supported", (
            f"intent 'inference say hello' routed to non-streaming service "
            f"(category={detail.get('intent_category')!r}) — strong LLM signal "
            f"must beat 'say' TTS keyword"
        )
    ct = r.headers.get("content-type", "")
    if "text/event-stream" in ct:
        # If it streamed, must be groq/together — the only _STREAMING_SLUGS.
        body = r.text
        assert "elevenlabs" not in body.lower(), (
            f"stream payload mentions elevenlabs, expected groq/together: {body[:300]}"
        )
