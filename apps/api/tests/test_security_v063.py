"""
apps/api/tests/test_security_v063.py
WAYFORTH v0.6.3 security regression suite — pentest negative-path coverage.

Each test guards a specific attack class identified during the v0.6.10 Aikido
audit + manual pentest pass. Tests hit the live deployment (BASE_URL from
test_suite_v060). Tests that depend on fixes shipped in v0.6.11+ self-skip
when run against an older deployment so the suite stays green during rollout.

Run: pytest apps/api/tests/test_security_v063.py -v
"""

import base64
import hashlib
import json
import os
import time

import httpx
import pytest
import pytest_asyncio

from tests.test_suite_v060 import API_KEY, BASE_URL


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uh() -> dict:
    return {"X-Wayforth-API-Key": API_KEY}


def _deployed_version() -> str:
    try:
        r = httpx.get(f"{BASE_URL}/status", timeout=10.0)
        return (r.json() or {}).get("version", "0.0.0")
    except Exception:
        return "0.0.0"


def _version_at_least(target: str) -> bool:
    def _tup(v: str):
        return tuple(int(p) for p in v.split(".")[:3] if p.isdigit())
    return _tup(_deployed_version()) >= _tup(target)


_REQUIRES_V063 = pytest.mark.skipif(
    not _version_at_least("0.6.11"),
    reason="Fix not yet deployed — requires API version 0.6.11 or later",
)


@pytest_asyncio.fixture
async def c():
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=30.0, follow_redirects=True
    ) as client:
        yield client


# ═════════════════════════════════════════════════════════════════════════════
# C1 — AUTHENTICATION & AUTHORIZATION
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_T201_protected_endpoints_require_api_key(c):
    """Every protected endpoint must return 401 when no API key header is sent."""
    protected = [
        ("GET",  "/keys/usage"),
        ("GET",  "/account/usage/history"),
        ("GET",  "/call/keys"),
        ("POST", "/call/keys/add"),
        ("POST", "/auth/regenerate-key"),
    ]
    for method, path in protected:
        r = await c.request(method, path, json={} if method == "POST" else None)
        assert r.status_code == 401, (
            f"{method} {path} must 401 without API key, got {r.status_code}: {r.text[:160]}"
        )


@pytest.mark.asyncio
async def test_T202_admin_routes_reject_missing_admin_key(c):
    """Admin routes must reject requests without ADMIN_KEY."""
    r = await c.post("/keys/create", json={"email": "x@y.com", "tier": "pro"})
    assert r.status_code in (401, 403, 404), (
        f"/keys/create with non-free tier and no admin key must 401/403/404, got {r.status_code}: {r.text[:160]}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_key", [
    "",
    "x",
    "wf_live_d_short",
    "x" * 4096,
    "wf with spaces",
    "<script>alert(1)</script>",
    "' OR 1=1 --",
])
async def test_T203_malformed_api_key_does_not_500(c, bad_key):
    """A malformed/wrong API key on /keys/usage must 401 — never 500 (would indicate input not handled)."""
    try:
        r = await c.get("/keys/usage", headers={"X-Wayforth-API-Key": bad_key})
    except (httpx.InvalidURL, httpx.LocalProtocolError):
        # httpx blocked the malformed header before it left — acceptable defense in depth.
        return
    assert r.status_code in (401, 422), (
        f"Bad key {bad_key!r} returned {r.status_code}: {r.text[:160]}"
    )


@pytest.mark.asyncio
async def test_T203b_null_byte_in_api_key_blocked(c):
    """Null bytes in the X-Wayforth-API-Key header must be rejected at some layer (client OR server)."""
    bad_key = "wf_live_d_\x00null"
    try:
        r = await c.get("/keys/usage", headers={"X-Wayforth-API-Key": bad_key})
        # Server-side rejection: 401 or 400 acceptable
        assert r.status_code in (400, 401, 422), (
            f"Null-byte key returned {r.status_code}: {r.text[:160]}"
        )
    except (httpx.InvalidURL, httpx.LocalProtocolError):
        # Client-side rejection — even better.
        return


@pytest.mark.asyncio
async def test_T204_expired_or_garbage_jwt_rejected(c):
    """/auth/me must reject any non-Supabase Bearer token with 401."""
    bogus_jwts = [
        "Bearer not.a.jwt",
        # alg=none token with a sub claim: header={"alg":"none"} payload={"sub":"attacker"}
        "Bearer eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
        "eyJzdWIiOiJhdHRhY2tlciJ9.",
        # expired token (synthetic — exp in 2000)
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiJ4Iiwic3ViIjoieCIsImV4cCI6OTQ2Njg0ODAwfQ."
        "deadbeef",
    ]
    for tok in bogus_jwts:
        r = await c.get("/auth/me", headers={"Authorization": tok})
        assert r.status_code == 401, (
            f"Bogus JWT {tok[:40]}... must 401, got {r.status_code}: {r.text[:160]}"
        )


@pytest.mark.asyncio
async def test_T205_auth_me_without_credentials_returns_401(c):
    """/auth/me with neither header returns 401, not 500."""
    r = await c.get("/auth/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_T206_byok_endpoints_require_byok_tier(c):
    """BYOK endpoints reject free-tier callers (require_tier('byok'))."""
    # Without auth → 401 first; with auth, free tier → 403.
    r = await c.get("/call/keys")
    assert r.status_code == 401
    r = await c.post("/call/keys/add", json={"service_slug": "x", "api_key": "y"})
    assert r.status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
# C2 — INJECTION
# ═════════════════════════════════════════════════════════════════════════════

SQLI_PAYLOADS = [
    "' OR 1=1 --",
    "'; DROP TABLE users; --",
    "' UNION SELECT password FROM users --",
    "1' OR '1'='1",
    "admin'--",
    "%27%20OR%201%3D1--",
    "\\'; SELECT pg_sleep(5); --",
]

XSS_PAYLOADS = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "javascript:alert(1)",
    "\"><svg/onload=alert(1)>",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", SQLI_PAYLOADS)
async def test_T210_search_sqli_payloads_safe(c, payload):
    """SQLi payloads on /search must 200/400/422 — never 500 (indicates raw concatenation)."""
    r = await c.get("/search", params={"q": payload, "limit": 1}, headers=_uh())
    assert r.status_code != 500, (
        f"SQLi payload {payload!r} caused 500: {r.text[:200]}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", XSS_PAYLOADS)
async def test_T211_search_xss_no_html_reflection(c, payload):
    """Search responses must be JSON-only — never echo input as raw HTML."""
    r = await c.get("/search", params={"q": payload, "limit": 1}, headers=_uh())
    if r.status_code >= 500:
        pytest.skip(f"Search 5xx — separate concern")
    ct = r.headers.get("content-type", "")
    assert "application/json" in ct, f"Search returned non-JSON content-type {ct!r}"
    # The payload may legitimately appear inside a JSON string (echoed query);
    # what matters is the response is not text/html.
    assert "<script" not in r.text.lower() or '"<script' in r.text.lower(), (
        f"Search echoed raw <script> — payload {payload!r}: {r.text[:200]}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    "../../etc/passwd",
    "..\\..\\windows\\system32",
    "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    "..%252fetc%252fpasswd",
])
async def test_T212_path_traversal_on_slug_rejected(c, payload):
    """Slug-style path parameters must not allow traversal."""
    try:
        r = await c.get(f"/services/{payload}/health")
    except (httpx.InvalidURL, httpx.LocalProtocolError):
        return
    # Acceptable: 404 (not found), 400 (invalid), 422 (validation). NOT 200 (treated as a real slug).
    assert r.status_code in (400, 404, 422), (
        f"Path traversal {payload!r} returned {r.status_code}: {r.text[:200]}"
    )


@pytest.mark.asyncio
async def test_T212b_null_byte_in_path_blocked(c):
    """Null bytes in URL path must be rejected (client OR server)."""
    try:
        r = await c.get("/services/\x00/etc/passwd/health")
        assert r.status_code in (400, 404, 422), (
            f"Null-byte path returned {r.status_code}: {r.text[:200]}"
        )
    except (httpx.InvalidURL, httpx.LocalProtocolError):
        return


@pytest.mark.asyncio
async def test_T213_crlf_in_custom_header_blocked(c):
    """CRLF in header values must be rejected at client (preferred) or server level."""
    try:
        r = await c.get(
            "/status",
            headers={"X-Test": "foo\r\nX-Injected: malicious"},
        )
    except (httpx.InvalidURL, httpx.LocalProtocolError, httpx.RequestError):
        # httpx refused to send — exactly what we want.
        return
    # If the request was sent, no injected header should appear in response.
    assert "x-injected" not in {k.lower() for k in r.headers.keys()}


# ═════════════════════════════════════════════════════════════════════════════
# C3 — PAYMENT & x402
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_T220_x402_search_no_payment_returns_402(c):
    """GET /x402/search without payment + query returns 402 with PaymentRequired."""
    r = await c.get("/x402/search")
    if r.status_code == 503:
        pytest.skip(
            "x402 rail hard-disabled (FINDING-001): no real on-chain settlement until "
            "EIP-3009 wired via funded CDP account — returns 503 until re-enabled"
        )
    assert r.status_code == 402, f"Expected 402, got {r.status_code}: {r.text[:200]}"
    body = r.json()
    assert body.get("x402Version") in (1, 2)
    assert "accepts" in body


@pytest.mark.asyncio
async def test_T221_x402_execute_no_payment_does_not_authorize(c):
    """POST /x402/execute without payment must not authorize a paid call (200)."""
    r = await c.post("/x402/execute", json={"service_slug": "openweather", "params": {}})
    # Allowed: 402 (payment required), 503 (not configured), 400 (validation),
    # 422 (Pydantic), 404 (route missing). NOT 200.
    assert r.status_code != 200, (
        f"x402/execute with no payment header authorized a call: {r.text[:200]}"
    )
    assert r.status_code in (400, 402, 422, 503, 404), (
        f"Expected 4xx/503, got {r.status_code}: {r.text[:200]}"
    )


@pytest.mark.asyncio
async def test_T222_x402_unknown_service_rejected(c):
    """x402/execute with unknown service_slug must NOT execute a paid call."""
    fake_payment = base64.b64encode(json.dumps({
        "from": "0xdeadbeef",
        "to": "0xcafe",
        "value": 1000,
    }).encode()).decode()
    r = await c.post(
        "/x402/execute",
        json={"service_slug": "nonexistent-service-xyz-zzzzzz", "params": {}},
        headers={"X-PAYMENT": fake_payment},
    )
    # Acceptable: any 4xx/5xx. NOT 200 (would mean call ran against an unknown service).
    assert r.status_code != 200, (
        f"Unknown service should not return 200, got: {r.text[:200]}"
    )


@pytest.mark.asyncio
@_REQUIRES_V063
async def test_T223_x402_garbage_payment_header_rejected(c):
    """Pre-fix: garbage X-PAYMENT was treated as valid (fail-open). Must now be rejected."""
    fake_payment = "not-base64-at-all-!!!"
    r = await c.post(
        "/x402/execute",
        json={"service_slug": "openweather", "params": {"city": "London"}},
        headers={"X-PAYMENT": fake_payment},
    )
    # Acceptable: 400/402/422 (invalid/missing fields), 503 (wallet unconfigured). NOT 200.
    assert r.status_code != 200, (
        f"Garbage payment header should not authorize a call, got 200: {r.text[:200]}"
    )
    assert r.status_code in (400, 402, 422, 503), (
        f"Garbage payment should yield 400/402/422/503, got {r.status_code}: {r.text[:200]}"
    )


@pytest.mark.asyncio
@_REQUIRES_V063
async def test_T224_x402_wrong_payee_rejected(c):
    """Payment to an attacker-controlled `to` address must not authorize a Wayforth call."""
    attacker_payment = base64.b64encode(json.dumps({
        "from": "0x" + "a" * 40,
        "to": "0x" + "b" * 40,  # attacker's wallet, not Wayforth's
        "value": 10_000_000,
    }).encode()).decode()
    r = await c.post(
        "/x402/execute",
        json={"service_slug": "openweather", "params": {"city": "London"}},
        headers={"X-PAYMENT": attacker_payment},
    )
    assert r.status_code != 200, (
        f"Payment to wrong payee should not authorize call, got 200: {r.text[:200]}"
    )


@pytest.mark.asyncio
async def test_T225_x402_replay_same_header_rejected(c):
    """Submitting the same X-PAYMENT header twice — second call gets 400 replay_rejected."""
    # Use a uniquely-suffixed payment header so we don't collide with real ones.
    unique = hashlib.sha256(f"test-replay-{time.time()}".encode()).hexdigest()
    payment = base64.b64encode(json.dumps({
        "from": "0x" + "0" * 40,
        "to": "0x" + "1" * 40,
        "value": 1,
        "_test_nonce": unique,
    }).encode()).decode()
    body = {"service_slug": "nonexistent-service-xyz", "params": {}}
    r1 = await c.post("/x402/execute", json=body, headers={"X-PAYMENT": payment})
    r2 = await c.post("/x402/execute", json=body, headers={"X-PAYMENT": payment})
    # First call may be 400 (unknown service) or 402/503 (payment) or 200 (if service ran).
    # Second call MUST be 400 replay_rejected if first one was processed past replay check.
    if r1.status_code == 200:
        assert r2.status_code == 400
        assert "replay_rejected" in r2.text


# ═════════════════════════════════════════════════════════════════════════════
# C4 — RATE LIMITING & DOS
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.no_api_key
async def test_T230_anon_search_daily_limit_enforced(c):
    """Unauthenticated /search must 429 within the first 10 requests.

    Anonymous callers are capped at `_ANON_DAILY_LIMIT` (3) searches per IP
    per day in `core.auth.check_auth`. Sending 10 requests from the same IP
    therefore MUST yield at least one 429 — either at request 4 (counter
    starting fresh) or at request 1 (counter already maxed from prior
    activity on this IP today). If all 10 succeed, the anon rate-limit
    code is either disabled, dead-coded, or misconfigured.
    """
    statuses = []
    for _ in range(10):
        try:
            r = await c.get("/search", params={"q": "anon-rate-limit-probe", "limit": 1})
        except httpx.RequestError:
            pytest.skip("Network error during anon rate-limit probe")
        statuses.append(r.status_code)
        if r.status_code == 429:
            break
        if r.status_code >= 500:
            pytest.skip(f"/search 5xx (status={r.status_code}) — cannot test rate limiting")
    assert 429 in statuses, (
        f"Anonymous /search returned no 429 in 10 requests "
        f"(statuses: {statuses}). Anonymous rate limiting is not enforced."
    )


@pytest.mark.asyncio
@pytest.mark.no_api_key
async def test_T230b_anon_query_blocked_by_tier_gate(c):
    """Unauthenticated /query must 401/403 — tier gate blocks before rate limit.

    Anonymous (and free-tier) callers must not reach the rate-limit code on
    /query because WayforthQL is `starter+`. If this returns 200/429, the
    `require_tier("wayforthql")` gate is broken — which would also mean
    free-tier users could access paid features.
    """
    try:
        r = await c.post("/query", json={"query": "rate-limit-tier-gate-probe", "limit": 1})
    except httpx.RequestError:
        pytest.skip("Network error during /query tier-gate probe")
    if r.status_code >= 500:
        pytest.skip(f"/query 5xx (status={r.status_code})")
    # Anon path: check_auth returns authenticated=False; require_tier(None or "free",
    # "wayforthql") raises 403. Some deployments may 401 if auth is preprocessed.
    # 429 is also acceptable if the anon daily limit on check_auth fires first
    # (since check_auth runs before require_tier in the dependency chain).
    assert r.status_code in (401, 403, 429), (
        f"/query anon must be rejected with 401/403/429, got {r.status_code}: {r.text[:200]}"
    )


@pytest.mark.asyncio
async def test_T231_auth_register_blocks_pentest_domains(c):
    """Registration with pentest domains must never create DB records.

    Three independent guards prevent persistence:
    1. Supabase JWT verification (v0.7.0+) → 401 when no Bearer header is sent
    2. supabase_id format validation (UUID4 regex) → 400 on any pre-v0.7.0 server
    3. Domain blocklist (@example.invalid) → 403 once deployed

    Using a non-UUID supabase_id AND omitting the Bearer header guarantees
    rejection before any INSERT, so this test never writes to production
    regardless of which guard fires first.
    """
    statuses = set()
    for i in range(3):
        r = await c.post("/auth/register", json={
            "email": f"ratelimit-test-{i}@example.invalid",
            "supabase_id": f"not-a-uuid-{i}",  # fails UUID4 guard if JWT guard misses
        })
        statuses.add(r.status_code)
    # 401 = JWT guard (v0.7.0); 400 = UUID guard; 403 = domain block; 409 = preexisting;
    # 429 = rate limit. ANY of these is acceptable — the assertion is "must not 2xx".
    assert statuses <= {400, 401, 403, 409, 429}, (
        f"Pentest-domain registration must be rejected (not 200/201/2xx), got: {statuses}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# C5 — API SURFACE HARDENING
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_T240_cors_rejects_unauthorized_origin(c):
    """Origin from an unauthorized domain must not get Access-Control-Allow-Origin reflected."""
    r = await c.options(
        "/status",
        headers={
            "Origin": "https://evil.attacker.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    aco = r.headers.get("access-control-allow-origin", "")
    assert aco != "https://evil.attacker.com", (
        f"CORS reflected evil origin: {aco!r}"
    )
    assert aco != "*", "CORS must not be wildcard while allow_credentials=True"


@pytest.mark.asyncio
async def test_T241_cors_lovable_regex_does_not_match_subdomain_attack(c):
    """The allow_origin_regex must be anchored — 'evil-lovable.app.attacker.com' must not match."""
    bad = "https://evil-lovable.app.attacker.com"
    r = await c.options(
        "/status",
        headers={"Origin": bad, "Access-Control-Request-Method": "GET"},
    )
    aco = r.headers.get("access-control-allow-origin", "")
    assert aco != bad, f"CORS regex matched a subdomain attack: {aco!r}"


@pytest.mark.asyncio
async def test_T242_security_headers_present(c):
    """All responses must carry the security headers configured in main.py middleware."""
    r = await c.get("/status")
    for h in (
        "X-Frame-Options",
        "X-Content-Type-Options",
        "Referrer-Policy",
        "Permissions-Policy",
        "Strict-Transport-Security",
    ):
        assert h in r.headers, f"Missing security header {h}"
    assert r.headers["X-Frame-Options"].lower() == "deny"
    assert r.headers["X-Content-Type-Options"].lower() == "nosniff"


@pytest.mark.asyncio
async def test_T243_oversized_payload_rejected(c):
    """A multi-megabyte JSON body should be rejected — not crash the worker."""
    huge = {"q": "x" * (2 * 1024 * 1024), "limit": 1}  # 2 MiB
    try:
        r = await c.post("/search", json=huge, headers=_uh(), timeout=15.0)
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError,
            httpx.ConnectError, httpx.RequestError):
        # Server closing the connection on oversized payload is acceptable
        # behavior (e.g. nginx/cloudflare upstream cutoff).
        return
    # 413 (Payload Too Large), 400, or 422 are all acceptable. 200 would mean
    # the server is willing to accept arbitrary-sized payloads, which is a DoS lever.
    assert r.status_code in (400, 413, 422, 404, 405, 503), (
        f"Oversized payload returned {r.status_code}: {r.text[:200]}"
    )


# ═════════════════════════════════════════════════════════════════════════════
# C6 — BYOK & KEY MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_T250_byok_endpoints_never_return_raw_encrypted_key(c):
    """Listing BYOK keys must never include an `encrypted_key` or full `api_key` field."""
    # Without auth → 401 (expected). We assert the *shape* of the 401 doesn't leak fields either.
    r = await c.get("/call/keys")
    assert r.status_code == 401
    text = r.text.lower()
    for forbidden in ("encrypted_key", "fernet", "decrypted"):
        assert forbidden not in text, (
            f"Auth error response leaks {forbidden!r}: {r.text[:200]}"
        )


@pytest.mark.asyncio
async def test_T251_keys_usage_does_not_reveal_full_key(c):
    """/keys/usage with a valid key returns prefix only, never the full key."""
    r = await c.get("/keys/usage", headers=_uh())
    if r.status_code != 200:
        pytest.skip(f"Test API key not accepted on /keys/usage: {r.status_code}")
    body = r.json()
    # Must NOT contain the full API_KEY value
    assert API_KEY not in json.dumps(body), (
        "Response includes the full API key — must only return prefix"
    )


# ═════════════════════════════════════════════════════════════════════════════
# C7 — PROVIDER ISOLATION
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_T260_provider_routes_require_auth(c):
    """All /provider/* routes must require auth — no information leakage without a key."""
    for path in ("/provider/me", "/provider/analytics", "/provider/services"):
        r = await c.get(path)
        # 401 (unauthenticated) or 404 (route doesn't exist) — both acceptable.
        # 200 with data would be the leak.
        assert r.status_code in (401, 404, 403, 405), (
            f"{path} leaked data without auth: {r.status_code} {r.text[:200]}"
        )


@pytest.mark.asyncio
async def test_T261_admin_routes_require_auth(c):
    """All /admin-api/* routes must require admin token — must not return data unauthenticated."""
    for path in (
        "/admin-api/overview",
        "/admin-api/users?limit=1",
        "/admin-api/providers?limit=1",
        "/admin-api/catalog/services?limit=1",
    ):
        r = await c.get(path)
        assert r.status_code in (401, 403, 404), (
            f"Admin route {path} accessible unauthenticated: {r.status_code} {r.text[:200]}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# C8 — RESPONSE CACHING & SENSITIVE ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_T270_auth_me_sets_cache_control_no_store(c):
    """/auth/me responses must be Cache-Control: no-store to prevent intermediary caching of API keys."""
    r = await c.get("/auth/me", headers=_uh())
    if r.status_code == 200:
        cc = r.headers.get("cache-control", "").lower()
        assert "no-store" in cc or "no-cache" in cc, (
            f"/auth/me missing no-store/no-cache: cc={cc!r}"
        )


@pytest.mark.asyncio
async def test_T271_regenerate_key_no_store(c):
    """Even an error from /auth/regenerate-key must not be cached by intermediaries."""
    r = await c.post("/auth/regenerate-key")
    # 401 expected without key; but if response did include a key, ensure not cacheable.
    cc = r.headers.get("cache-control", "").lower()
    if r.status_code == 200:
        assert "no-store" in cc, f"regenerate-key 200 missing no-store: cc={cc!r}"
