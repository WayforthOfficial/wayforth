"""
apps/api/tests/test_suite_v060.py
WAYFORTH v0.6.0 end-to-end test suite — T001–T150

Run: pytest apps/api/tests/test_suite_v060.py -v
All tests hit the live Railway deployment.
"""

import asyncio
import os
from typing import Optional
import pytest
import pytest_asyncio
import httpx

# ── Connection config ─────────────────────────────────────────────────────────

BASE_URL  = "https://gateway.wayforth.io"
API_KEY   = "REDACTED_KEY_2"
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

# ── Fields that must NEVER appear in any API response ─────────────────────────

FORBIDDEN_FIELDS = [
    "mode", "fee_type", "markup_pct", "managed_30pct", "byok_10pct",
    "service_receives_usd", "wayf_burn_allocation_usd", "wayforth_revenue",
    "fee_bps", "fee_pct", "wayf_bonus_pct", "credits_per_call",
]

# ── Global collectors (read by conftest.py summary hook) ──────────────────────

_500_errors:    list[dict] = []
_forbidden_hits: list[tuple] = []
_warnings:      list[str] = []

# ── Helpers ───────────────────────────────────────────────────────────────────

def _uh() -> dict:
    return {"X-Wayforth-API-Key": API_KEY}

def _ah() -> dict:
    return {"X-Admin-Key": ADMIN_KEY}

def _scan(data, url: str) -> None:
    """Recursively scan a parsed JSON value for forbidden fields."""
    if isinstance(data, dict):
        for f in FORBIDDEN_FIELDS:
            if f in data:
                _forbidden_hits.append((url, f, repr(data[f])[:80]))
        for v in data.values():
            _scan(v, url)
    elif isinstance(data, list):
        for item in data:
            _scan(item, url)

def rec(resp: httpx.Response) -> httpx.Response:
    """Record 500 errors and scan for forbidden fields."""
    url = str(resp.url)
    if resp.status_code == 500:
        _500_errors.append({
            "url": url,
            "method": resp.request.method,
            "body_preview": resp.text[:300],
        })
    try:
        _scan(resp.json(), url)
    except Exception:
        pass
    return resp

def no_leak(resp: httpx.Response) -> None:
    text = resp.text
    assert "Traceback" not in text,       f"Python traceback in response: {resp.url}"
    assert 'File "/'   not in text,       f"Internal path leaked: {resp.url}"
    assert "sqlalchemy" not in text.lower(), f"SQLAlchemy leaked: {resp.url}"

# ── Shared async client (function-scoped for maximum reliability) ─────────────

@pytest_asyncio.fixture
async def c():
    async with httpx.AsyncClient(
        base_url=BASE_URL, timeout=60.0, follow_redirects=True
    ) as client:
        yield client

# ── Account tier (fetched once, cached at module level) ───────────────────────

_account_tier = None  # type: Optional[str]

async def _get_tier(c: httpx.AsyncClient) -> str:
    global _account_tier
    if _account_tier is None:
        r = await c.get("/billing/balance", headers=_uh())
        _account_tier = r.json().get("plan", "free") if r.status_code == 200 else "free"
    return _account_tier

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — HEALTH & STATUS
# ─────────────────────────────────────────────────────────────────────────────

async def test_T001_status(c):
    r = rec(await c.get("/status"))
    assert r.status_code == 200
    d = r.json()
    assert "payment_rails" in d or "payment_rail" in d, \
        f"payment_rail(s) missing from /status — keys: {list(d.keys())}"

async def test_T002_system_health(c):
    r = rec(await c.get("/system/health"))
    assert r.status_code == 200
    d = r.json()
    assert "subsystems" in d, f"'subsystems' missing from /system/health — keys: {list(d.keys())}"
    ok_statuses = {"ok", "mock", "configured", "active", "fallback_to_card", "test", "not_set"}
    for name, sub in d["subsystems"].items():
        if isinstance(sub, dict) and "status" in sub:
            if sub["status"] not in ok_statuses:
                _warnings.append(f"T002: subsystem '{name}' has status={sub['status']!r}")

async def test_T003_chain(c):
    r = rec(await c.get("/chain"))
    assert r.status_code == 200
    d = r.json()
    assert "network" in d, f"'network' missing from /chain — keys: {list(d.keys())}"
    assert "0x" in r.text, "/chain has no contract addresses (expected 0x...)"

async def test_T004_services_count(c):
    r = rec(await c.get("/services/count"))
    assert r.status_code == 200
    d = r.json()
    total = d.get("total", 0)
    tier2 = d.get("tier2", 0)
    assert total > 200, f"Expected >200 total services, got {total}"
    assert tier2 > 150, f"Expected >150 tier2 services, got {tier2}"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — SEARCH & DISCOVERY
# ─────────────────────────────────────────────────────────────────────────────

async def test_T010_search_basic(c):
    r = rec(await c.get("/search", params={"q": "translate"}, headers=_uh()))
    assert r.status_code == 200, f"/search basic: {r.status_code} — {r.text[:200]}"
    results = r.json().get("results", [])
    assert len(results) > 0, "No results for 'translate'"
    for item in results:
        assert "wri_score" in item or "wri" in item, f"Missing wri field in: {item}"
        assert "slug" in item or "name" in item,    f"Missing slug/name in: {item}"

async def test_T011_search_limit(c):
    r = rec(await c.get("/search", params={"q": "translate", "limit": 3}, headers=_uh()))
    assert r.status_code == 200
    results = r.json().get("results", [])
    assert len(results) <= 3, f"limit=3 gave {len(results)} results"

async def test_T012_search_category_filter(c):
    r = rec(await c.get("/search", params={"q": "translate", "category": "translation"}, headers=_uh()))
    assert r.status_code == 200
    for item in r.json().get("results", []):
        cat = (item.get("category") or "").lower()
        assert "translation" in cat or cat == "", f"Category filter broken: got '{cat}'"

async def test_T013_search_empty_query(c):
    r = rec(await c.get("/search", params={"q": ""}, headers=_uh()))
    # Product should reject; 400/422 is correct. If 200, flag it.
    assert r.status_code != 500, f"Empty query must not 500 — got {r.status_code}: {r.text[:200]}"
    if r.status_code == 200:
        _warnings.append("T013: GET /search?q= returned 200 — spec expects 400/422 (missing validation)")
    no_leak(r)

async def test_T014_search_xss(c):
    xss = "<script>alert(1)</script>"
    r = rec(await c.get("/search", params={"q": xss}, headers=_uh()))
    assert r.status_code != 500
    assert "<script>" not in r.text, f"XSS input reflected back in response at {r.url}"

async def test_T015_search_sqli(c):
    r = rec(await c.get("/search", params={"q": "' OR 1=1--"}, headers=_uh()))
    assert r.status_code != 500, f"SQLi probe returned 500 — possible DB error: {r.text[:200]}"
    no_leak(r)

async def test_T016_wayforthql(c):
    tier = await _get_tier(c)
    if tier not in ("starter", "pro", "growth"):
        pytest.skip(f"WayforthQL requires starter tier; account is '{tier}'")
    r = rec(await c.post("/query", headers=_uh(), json={
        "query": "inference",
        "tier_min": 2,
        "price_max": 0.01,
        "sort_by": "wri",
    }))
    assert r.status_code == 200, f"WayforthQL: {r.status_code} — {r.text[:300]}"
    d = r.json()
    assert "protocol" in d and "WayforthQL" in d["protocol"], \
        f"protocol field wrong or missing: {d.get('protocol')}"
    for svc in d.get("results", []):
        assert svc.get("coverage_tier", 0) >= 2, \
            f"tier_min=2 not enforced: slug={svc.get('slug')} tier={svc.get('coverage_tier')}"

async def test_T017_wayforthql_invalid_sort(c):
    tier = await _get_tier(c)
    if tier not in ("starter", "pro", "growth"):
        pytest.skip("Needs starter tier")
    r = rec(await c.post("/query", headers=_uh(), json={
        "query": "inference",
        "sort_by": "__invalid_sort_field__",
    }))
    assert r.status_code in (400, 422), \
        f"Invalid sort_by should fail cleanly, got {r.status_code}: {r.text[:200]}"
    assert r.status_code != 500

async def test_T018_leaderboard(c):
    r = rec(await c.get("/leaderboard"))
    assert r.status_code == 200, f"Leaderboard: {r.status_code}"
    d = r.json()
    services = d.get("services", d.get("results", d.get("leaderboard", [])))
    assert len(services) > 0, "Leaderboard returned no services"
    scores = [s.get("wri_score") or s.get("wri") for s in services]
    numeric = [float(s) for s in scores if s is not None]
    if len(numeric) >= 2:
        assert numeric == sorted(numeric, reverse=True), \
            f"Leaderboard not sorted by WRI desc: {numeric[:5]}"

async def test_T019_compare(c):
    tier = await _get_tier(c)
    if tier not in ("starter", "pro", "growth"):
        pytest.skip(f"Compare requires starter tier; account is '{tier}'")
    r = rec(await c.get("/compare", params={"slugs": "deepl,groq"}, headers=_uh()))
    assert r.status_code == 200, f"Compare: {r.status_code} — {r.text[:200]}"
    d = r.json()
    services = d.get("services", d.get("comparison", d.get("results", [])))
    names = " ".join(str(s.get("slug", s.get("name", ""))).lower() for s in services)
    assert "deepl" in names, f"deepl not in compare response: {list(d.keys())}"
    assert "groq"  in names, f"groq not in compare response: {list(d.keys())}"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — AUTH & TIER GATING
# ─────────────────────────────────────────────────────────────────────────────

# Endpoints that require X-Wayforth-API-Key
_AUTH_CASES = [
    ("GET",    "/billing/balance",    {}),
    ("GET",    "/billing/transactions", {}),
    ("POST",   "/billing/deduct",     {"json": {"amount_usd": 0.001, "service_id": "test"}}),
    ("POST",   "/pay",                {"json": {"service_id": "DeepL", "amount_usd": 0.001}}),
    ("GET",    "/call/keys",          {}),
    ("GET",    "/account/wayf-points", {}),
    ("POST",   "/run",                {"json": {"intent": "translate hi"}}),
    ("GET",    "/account/agents",     {}),
]

@pytest.mark.parametrize("method,path,kw", _AUTH_CASES)
async def test_T020_no_key_returns_401(c, method, path, kw):
    r = rec(await getattr(c, method.lower())(path, **kw))
    assert r.status_code == 401, \
        f"T020: {method} {path} with no key → expected 401, got {r.status_code}"

@pytest.mark.parametrize("method,path,kw", _AUTH_CASES)
async def test_T021_invalid_key_returns_401(c, method, path, kw):
    r = rec(await getattr(c, method.lower())(
        path, **{**kw, "headers": {"X-Wayforth-API-Key": "wf_live_invalid123abc"}}
    ))
    assert r.status_code == 401, \
        f"T021: {method} {path} with invalid key → expected 401, got {r.status_code}"

@pytest.mark.parametrize("method,path,kw", _AUTH_CASES)
async def test_T022_malformed_key_returns_401(c, method, path, kw):
    r = rec(await getattr(c, method.lower())(
        path, **{**kw, "headers": {"X-Wayforth-API-Key": "not_a_wayforth_key_at_all"}}
    ))
    assert r.status_code == 401, \
        f"T022: {method} {path} with malformed key → expected 401, got {r.status_code}"

_ADMIN_PATHS = [
    ("GET", "/admin/wayf-points/totals"),
    ("GET", "/admin/health"),
    ("GET", "/admin/stats"),
]

@pytest.mark.parametrize("method,path", _ADMIN_PATHS)
async def test_T023_admin_endpoint_with_user_key(c, method, path):
    r = rec(await getattr(c, method.lower())(path, headers=_uh()))
    assert r.status_code in (401, 403), \
        f"T023: {path} with user key → expected 401/403, got {r.status_code}"

@pytest.mark.parametrize("method,path", _ADMIN_PATHS)
async def test_T024_admin_endpoint_no_key(c, method, path):
    r = rec(await getattr(c, method.lower())(path))
    assert r.status_code in (401, 403), \
        f"T024: {path} no key → expected 401/403, got {r.status_code}"

async def test_T025_free_tier_gating_response_shape(c):
    # Test with invalid key to confirm 401 shape; real free-tier gate tested via known endpoint
    r = rec(await c.get("/account/wayf-points", headers={"X-Wayforth-API-Key": "wf_live_badinvalid"}))
    assert r.status_code == 401
    d = r.json()
    assert "error" in str(d).lower() or "detail" in d, \
        "401 response should contain error detail"

async def test_T026_valid_key_authenticated(c):
    r = rec(await c.get("/billing/balance", headers=_uh()))
    assert r.status_code == 200, f"Valid key should return 200, got {r.status_code}: {r.text}"
    d = r.json()
    assert "plan" in d,            f"'plan' missing from /billing/balance"
    assert "calls_remaining" in d, f"'calls_remaining' missing from /billing/balance"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — BILLING & CREDITS
# ─────────────────────────────────────────────────────────────────────────────

async def test_T030_billing_balance(c):
    r = rec(await c.get("/billing/balance", headers=_uh()))
    assert r.status_code == 200
    d = r.json()
    assert d.get("calls_remaining", -1) >= 0, \
        f"calls_remaining should be ≥0, got {d.get('calls_remaining')}"
    for bad in ("fee_bps", "fee_pct", "wayf_bonus_pct", "markup_pct"):
        assert bad not in d, f"Internal field '{bad}' exposed in /billing/balance"

async def test_T031_billing_packages(c):
    r = rec(await c.get("/billing/packages"))
    assert r.status_code == 200
    pkgs = r.json().get("packages", [])
    assert len(pkgs) >= 4, f"Expected ≥4 packages, got {len(pkgs)}"
    for pkg in pkgs:
        for bad in ("fee_bps", "wayf_bonus_pct", "fee_pct", "markup_pct"):
            assert bad not in pkg, \
                f"Internal field '{bad}' in package '{pkg.get('id')}'"

async def test_T032_mock_topup(c):
    r = rec(await c.post("/billing/mock-topup", headers=_uh(), json={"credits": 10}))
    assert r.status_code == 200, f"mock-topup: {r.status_code} — {r.text[:200]}"
    d = r.json()
    assert d.get("credits_added") == 10, f"Expected credits_added=10, got {d.get('credits_added')}"
    assert d.get("new_balance", 0) >= 10, f"new_balance={d.get('new_balance')} seems wrong"

async def test_T033_billing_transactions_no_raw_types(c):
    r = rec(await c.get("/billing/transactions", headers=_uh()))
    assert r.status_code == 200
    forbidden_raw = {"byok", "managed", "byok_10pct", "managed_30pct"}
    for tx in r.json().get("transactions", []):
        t = tx.get("type", "")
        assert t not in forbidden_raw, \
            f"Raw internal type '{t}' exposed in /billing/transactions"

async def test_T034_billing_purchases(c):
    r = rec(await c.get("/billing/purchases", headers=_uh()))
    assert r.status_code == 200, f"purchases: {r.status_code}"
    assert isinstance(r.json().get("purchases"), list), \
        f"'purchases' should be a list, got {type(r.json().get('purchases'))}"

async def test_T035_billing_checkout_mock(c):
    r = rec(await c.post("/billing/checkout", headers=_uh(), json={"package": "starter"}))
    assert r.status_code in (200, 302, 303), \
        f"checkout: {r.status_code} — {r.text[:200]}"

async def test_T036_billing_deduct(c):
    r = rec(await c.post("/billing/deduct", headers=_uh(), json={
        "amount_usd": 0.001,
        "service_id": "test-deduct",
    }))
    assert r.status_code == 200, f"deduct 0.001: {r.status_code} — {r.text}"
    d = r.json()
    assert d.get("credits_deducted", 0) >= 1
    assert "credits_remaining" in d

async def test_T037_billing_deduct_insufficient(c):
    r = rec(await c.post("/billing/deduct", headers=_uh(), json={
        "amount_usd": 999999.0,
        "service_id": "test",
    }))
    assert r.status_code in (400, 402), \
        f"Huge deduct should be 402, got {r.status_code}: {r.text[:200]}"
    assert r.status_code != 500

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — PAYMENT RAIL
# ─────────────────────────────────────────────────────────────────────────────

async def test_T040_pay_card_no_forbidden_fields(c):
    r = rec(await c.post("/pay", headers=_uh(), json={
        "service_id": "DeepL",
        "amount_usd": 0.001,
        "track": "card",
    }))
    assert r.status_code in (200, 402), \
        f"pay card: {r.status_code} — {r.text[:200]}"
    if r.status_code == 200:
        d = r.json()
        assert d.get("payment_track") == "card"
        for bad in ("fee_type", "mode", "markup_pct"):
            assert bad not in d, f"Forbidden field '{bad}' in /pay response"

async def test_T041_pay_crypto_calldata(c):
    r = rec(await c.post("/pay", headers=_uh(), json={
        "service_id": "DeepL",
        "amount_usd": 0.001,
        "track": "crypto",
    }))
    assert r.status_code == 200, f"pay crypto: {r.status_code} — {r.text[:200]}"
    d = r.json()
    assert "approve_calldata"  in d, "approve_calldata missing from crypto track"
    assert "payment_calldata"  in d, "payment_calldata missing from crypto track"
    assert d.get("payment_track") == "crypto"
    for bad in ("fee_type", "mode", "markup_pct"):
        assert bad not in d, f"Forbidden field '{bad}' in crypto /pay response"

async def test_T042_pay_x402_graceful(c):
    r = rec(await c.post("/pay", headers=_uh(), json={
        "service_id": "DeepL",
        "amount_usd": 0.001,
        "track": "auto",
    }))
    assert r.status_code in (200, 402), f"pay x402/auto: {r.status_code}"
    assert r.status_code != 500

async def test_T043_pay_missing_service_id(c):
    r = rec(await c.post("/pay", headers=_uh(), json={
        "amount_usd": 0.001,
        "track": "card",
    }))
    assert r.status_code in (400, 422), \
        f"Missing service_id should be 400/422, got {r.status_code}: {r.text[:200]}"

async def test_T044_pay_unknown_service_no_500(c):
    r = rec(await c.post("/pay", headers=_uh(), json={
        "service_id": "xXxNonExistentServiceXxX",
        "amount_usd": 0.001,
        "track": "crypto",  # crypto doesn't need credits
    }))
    assert r.status_code != 500, \
        f"Unknown service must not 500: {r.text[:200]}"
    # Product note: currently falls through with service_name = service_id (not 404)

async def test_T045_pay_zero_amount_no_500(c):
    r = rec(await c.post("/pay", headers=_uh(), json={
        "service_id": "DeepL",
        "amount_usd": 0,
        "track": "crypto",
    }))
    assert r.status_code != 500, f"Zero amount must not 500: {r.text[:200]}"
    if r.status_code == 200:
        _warnings.append("T045: amount_usd=0 returns 200 — spec expects 400/422 (missing validation)")

async def test_T046_pay_negative_amount_no_500(c):
    r = rec(await c.post("/pay", headers=_uh(), json={
        "service_id": "DeepL",
        "amount_usd": -1.0,
        "track": "crypto",
    }))
    assert r.status_code != 500, f"Negative amount must not 500: {r.text[:200]}"
    if r.status_code == 200:
        _warnings.append("T046: amount_usd=-1 returns 200 — spec expects 400/422 (missing validation)")

async def test_T047_pay_huge_amount_credits_gate(c):
    r = rec(await c.post("/pay", headers=_uh(), json={
        "service_id": "DeepL",
        "amount_usd": 999999.0,
        "track": "card",
    }))
    assert r.status_code in (400, 402), \
        f"Huge amount on card should be 402, got {r.status_code}: {r.text[:200]}"
    assert r.status_code != 500

async def test_T048_pay_routing_fee_correct(c):
    amount = 0.100
    r = rec(await c.post("/pay", headers=_uh(), json={
        "service_id": "DeepL",
        "amount_usd": amount,
        "track": "card",
    }))
    if r.status_code == 402:
        pytest.skip("Insufficient credits to verify routing fee (T048)")
    assert r.status_code == 200
    actual_fee = r.json().get("routing_fee_usd", None)
    if actual_fee is None:
        pytest.skip("routing_fee_usd not in response (may be crypto fallback)")
    expected = round(amount * 0.015, 8)
    assert abs(actual_fee - expected) < 0.001, \
        f"routing_fee_usd: expected ≈{expected}, got {actual_fee}"

async def test_T049_pay_no_forbidden_fields_full(c):
    r = rec(await c.post("/pay", headers=_uh(), json={
        "service_id": "DeepL",
        "amount_usd": 0.001,
        "track": "card",
    }))
    if r.status_code not in (200, 402):
        pytest.skip(f"Unexpected /pay status {r.status_code}")
    d = r.json()
    forbidden = [
        "mode", "fee_type", "markup_pct", "service_receives_usd",
        "wayf_burn_allocation_usd", "wayforth_revenue",
        "wayf_bonus_pct", "fee_bps", "fee_pct",
    ]
    hits = [f for f in forbidden if f in d]
    assert not hits, f"Forbidden fields in /pay response: {hits}"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — EXECUTION
# ─────────────────────────────────────────────────────────────────────────────

async def test_T050_run_basic(c):
    r = rec(await c.post("/run", headers=_uh(), json={"intent": "translate Hello to Spanish"}))
    assert r.status_code in (200, 402, 503), \
        f"/run basic: {r.status_code} — {r.text[:300]}"
    if r.status_code == 200:
        d = r.json()
        assert "result" in d or "service_used" in d, \
            f"/run 200 but no result/service_used: {list(d.keys())}"

async def test_T051_run_empty_intent(c):
    r = rec(await c.post("/run", headers=_uh(), json={"intent": ""}))
    assert r.status_code in (400, 422), \
        f"Empty intent should be 400/422, got {r.status_code}: {r.text[:200]}"

async def test_T052_run_odd_intent_no_500(c):
    r = rec(await c.post("/run", headers=_uh(), json={
        "intent": "xyzabc123_completely_undefined_action_qwerty",
    }))
    assert r.status_code != 500, \
        f"/run with odd intent returned 500: {r.text[:200]}"

async def test_T053_execute_groq(c):
    r = rec(await c.post("/execute", headers=_uh(), json={
        "service_slug": "groq",
        "params": {"messages": [{"role": "user", "content": "Say hi in one word"}]},
    }))
    assert r.status_code in (200, 402, 503), \
        f"/execute groq: {r.status_code} — {r.text[:300]}"
    if r.status_code == 200:
        d = r.json()
        assert "result" in d, f"/execute 200 but no result: {list(d.keys())}"
        assert d.get("credits_deducted", 0) > 0, "credits_deducted should be > 0 on success"

async def test_T054_execute_unknown_slug(c):
    r = rec(await c.post("/execute", headers=_uh(), json={
        "service_slug": "xXxNonExistentXxX",
        "params": {},
    }))
    assert r.status_code in (400, 404, 422), \
        f"Unknown slug should fail cleanly, got {r.status_code}: {r.text[:200]}"
    assert r.status_code != 500

async def test_T055_execute_credit_gate(c):
    r = rec(await c.post("/execute", headers=_uh(), json={
        "service_slug": "groq",
        "params": {"messages": [{"role": "user", "content": "ping"}]},
    }))
    assert r.status_code in (200, 402, 503), f"/execute: {r.status_code}"
    if r.status_code == 402:
        d = r.json()
        assert "credits" in str(d).lower(), \
            f"402 response should mention credits: {d}"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — BYOK KEY MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

_SLUG_MAIN = "wayforth-test-byok-v040"
_SLUG_DUP  = "wayforth-test-byok-v040-dup"

async def test_T060_byok_add(c):
    # Cleanup stale from previous run
    await c.delete(f"/call/keys/{_SLUG_MAIN}", headers=_uh())
    r = rec(await c.post("/call/keys/add", headers=_uh(), json={
        "service_slug":  _SLUG_MAIN,
        "service_name":  "Test BYOK v040",
        "api_key":       "sk_test_wayforth_byok_v040_sentinel",
    }))
    assert r.status_code in (200, 201), \
        f"BYOK add failed: {r.status_code} — {r.text}"

async def test_T061_byok_in_list(c):
    r = rec(await c.get("/call/keys", headers=_uh()))
    assert r.status_code == 200
    keys = r.json().get("service_keys", r.json().get("keys", r.json().get("services", [])))
    slugs = {k.get("service_slug", k.get("slug", "")) for k in keys}
    assert _SLUG_MAIN in slugs, \
        f"Added key '{_SLUG_MAIN}' not found in /call/keys: {slugs}"

async def test_T062_byok_key_not_plaintext(c):
    r = rec(await c.get("/call/keys", headers=_uh()))
    assert r.status_code == 200
    assert "sk_test_wayforth_byok_v040_sentinel" not in r.text, \
        "BYOK API key value leaked in plaintext in /call/keys response"

async def test_T063_byok_delete(c):
    r = rec(await c.delete(f"/call/keys/{_SLUG_MAIN}", headers=_uh()))
    assert r.status_code == 200, f"BYOK delete failed: {r.status_code} — {r.text}"

async def test_T064_byok_gone_after_delete(c):
    r = rec(await c.get("/call/keys", headers=_uh()))
    assert r.status_code == 200
    keys = r.json().get("service_keys", r.json().get("keys", r.json().get("services", [])))
    active_slugs = {
        k.get("service_slug", k.get("slug", ""))
        for k in keys
        if k.get("active", True) is not False
    }
    assert _SLUG_MAIN not in active_slugs, \
        f"Deleted BYOK key '{_SLUG_MAIN}' still active in /call/keys"

async def test_T065_byok_duplicate_handled(c):
    await c.delete(f"/call/keys/{_SLUG_DUP}", headers=_uh())
    await c.post("/call/keys/add", headers=_uh(), json={
        "service_slug": _SLUG_DUP, "service_name": "Dup", "api_key": "sk_first",
    })
    r = rec(await c.post("/call/keys/add", headers=_uh(), json={
        "service_slug": _SLUG_DUP, "service_name": "Dup", "api_key": "sk_second",
    }))
    assert r.status_code in (200, 201, 409), \
        f"Duplicate BYOK slug: expected 200/409, got {r.status_code}: {r.text}"
    assert r.status_code != 500
    await c.delete(f"/call/keys/{_SLUG_DUP}", headers=_uh())

async def test_T066_byok_empty_api_key(c):
    r = rec(await c.post("/call/keys/add", headers=_uh(), json={
        "service_slug": "test-empty-byok",
        "service_name": "Test",
        "api_key": "",
    }))
    assert r.status_code in (400, 422), \
        f"Empty api_key should fail, got {r.status_code}: {r.text}"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — AGENT IDENTITY
# ─────────────────────────────────────────────────────────────────────────────

_AGENT_ID = "wayforth-test-suite-agent-v040"

async def test_T070_agent_register_or_exists(c):
    r = rec(await c.post("/identity/register", headers=_uh(), json={
        "agent_id":    _AGENT_ID,
        "display_name": "Wayforth Test Suite v0.6.0",
    }))
    assert r.status_code in (200, 201, 409), \
        f"identity register: {r.status_code} — {r.text}"

async def test_T071_agent_identity_fields(c):
    await c.post("/identity/register", headers=_uh(), json={
        "agent_id": _AGENT_ID, "display_name": "v040",
    })
    r = rec(await c.get(f"/identity/{_AGENT_ID}"))
    if r.status_code == 404:
        pytest.skip("Agent not found — registration may not have persisted")
    assert r.status_code == 200, f"identity get: {r.status_code} — {r.text}"
    d = r.json()
    assert "agent_id" in d,                  "agent_id missing"
    assert "trust_score" in d or "reputation_score" in d, "trust/reputation missing"

async def test_T072_agent_identity_persistent(c):
    r1 = rec(await c.get(f"/identity/{_AGENT_ID}"))
    r2 = rec(await c.get(f"/identity/{_AGENT_ID}"))
    if r1.status_code == 404:
        pytest.skip("Agent not registered")
    assert r1.status_code == r2.status_code == 200
    assert r1.json().get("agent_id") == r2.json().get("agent_id"), \
        "agent_id differs between identical calls"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9 — ANALYTICS
# ─────────────────────────────────────────────────────────────────────────────

async def test_T080_account_analytics_reachable(c):
    r = rec(await c.get("/account/analytics", headers=_uh()))
    assert r.status_code == 200, \
        f"/account/analytics: {r.status_code} — {r.text[:200]}"

async def test_T081_analytics_key_fields(c):
    r = rec(await c.get("/account/analytics", headers=_uh()))
    assert r.status_code == 200
    d = r.json()
    assert "searches"   in d or "search_count"   in d, \
        f"searches/search_count missing from analytics: {list(d.keys())}"
    assert "executions" in d or "execution_count" in d, \
        f"executions/execution_count missing from analytics: {list(d.keys())}"

async def test_T082_analytics_searches_non_negative(c):
    r = rec(await c.get("/account/analytics", headers=_uh()))
    assert r.status_code == 200
    d = r.json()
    searches = d.get("searches", {})
    if isinstance(searches, dict):
        count = searches.get("this_month", searches.get("today", 0))
    else:
        count = searches
    assert count >= 0, f"searches should be >= 0, got {count}"
    if count == 0:
        _warnings.append("T082: searches this_month = 0 after test run — expected > 0")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 10 — $WAYF POINTS
# ─────────────────────────────────────────────────────────────────────────────

async def test_T090_wayf_points_reachable(c):
    r = rec(await c.get("/account/wayf-points", headers=_uh()))
    if r.status_code == 403:
        pytest.skip("Account tier too low for wayf-points (needs builder+)")
    assert r.status_code == 200, f"/account/wayf-points: {r.status_code} — {r.text[:200]}"
    assert r.json().get("points_balance", -1) >= 0

async def test_T091_wayf_rate_formula_consistent(c):
    r = rec(await c.get("/account/wayf-points", headers=_uh()))
    if r.status_code == 403:
        pytest.skip("Tier too low")
    assert r.status_code == 200
    d = r.json()
    pts  = d.get("points_balance", 0)
    wayf = d.get("wayf_balance", 0.0)
    rate = d.get("current_rate", {}).get("points_per_wayf", 10)
    if pts > 0 and rate > 0:
        # Allow 5% or 1 WAYF tolerance (awards may span multiple rate tiers)
        tolerance = max(1.0, pts / rate * 0.05)
        expected  = pts / rate
        assert abs(wayf - expected) <= tolerance, \
            f"wayf_balance={wayf} inconsistent with points={pts} / rate={rate} (expected ≈{expected:.4f})"

async def test_T092_wayf_disclaimer_present(c):
    r = rec(await c.get("/account/wayf-points", headers=_uh()))
    if r.status_code == 403:
        pytest.skip("Tier too low")
    assert r.status_code == 200
    d = r.json()
    assert "disclaimer" in d,            "disclaimer missing from wayf-points"
    assert len(d["disclaimer"]) > 50,    f"disclaimer suspiciously short: {d['disclaimer']!r}"

async def test_T093_wayf_no_proportional_model(c):
    r = rec(await c.get("/account/wayf-points", headers=_uh()))
    if r.status_code == 403:
        pytest.skip("Tier too low")
    assert r.status_code == 200
    d = r.json()
    # These fields belong to the old proportional model — must not appear
    for bad in ("tge_estimate", "your_share_pct", "estimated_wayf", "total_all_users_points"):
        assert bad not in d, \
            f"Old proportional field '{bad}' still present in /account/wayf-points"

async def test_T094_wayf_rate_valid_tier(c):
    r = rec(await c.get("/account/wayf-points", headers=_uh()))
    if r.status_code == 403:
        pytest.skip("Tier too low")
    assert r.status_code == 200
    rate = r.json().get("current_rate", {}).get("points_per_wayf")
    valid = {10, 20, 30, 40, 50, 60, 70, 80, 90, 100}
    assert rate in valid, \
        f"points_per_wayf={rate!r} is not a valid tier value (must be one of {sorted(valid)})"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 11 — WEBHOOKS
# ─────────────────────────────────────────────────────────────────────────────

async def test_T100_stripe_webhook_no_signature(c):
    r = rec(await c.post(
        "/stripe/webhook",
        content=b'{"type":"test"}',
        headers={"Content-Type": "application/json"},
    ))
    assert r.status_code in (400, 422), \
        f"No stripe-signature → expected 400, got {r.status_code}"

async def test_T101_stripe_webhook_bad_signature(c):
    r = rec(await c.post(
        "/stripe/webhook",
        content=b'{"type":"test"}',
        headers={
            "Content-Type": "application/json",
            "stripe-signature": "t=0,v1=baaaaadsignaturevalue",
        },
    ))
    assert r.status_code in (400, 422), \
        f"Bad stripe-signature → expected 400, got {r.status_code}"
    assert r.status_code != 500

async def test_T102_webhooks_list(c):
    r = rec(await c.get("/webhooks", headers=_uh()))
    assert r.status_code in (200, 403, 404), f"/webhooks: {r.status_code}"
    if r.status_code == 200:
        assert isinstance(r.json().get("webhooks", []), list)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 12 — PROVIDER DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

async def test_T110_provider_endpoints_no_500(c):
    r = rec(await c.get("/provider/overview"))
    assert r.status_code in (200, 401, 403, 404, 422), \
        f"/provider/overview: unexpected {r.status_code}"
    assert r.status_code != 500

async def test_T111_provider_rejects_user_key(c):
    r = rec(await c.get("/provider/me", headers=_uh()))
    # Regular API key is not a provider session token
    assert r.status_code in (401, 403, 422), \
        f"/provider/me with user key → expected 401/403, got {r.status_code}"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 13 — ADMIN ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

async def test_T120_admin_get_user_with_admin_key(c):
    list_r = rec(await c.get("/admin-api/users", headers=_ah()))
    assert list_r.status_code == 200, \
        f"/admin-api/users list failed: {list_r.status_code} — {list_r.text[:200]}"
    users = list_r.json().get("users", [])
    if not users:
        pytest.skip("No users in admin list")
    user_id = users[0]["id"]
    r = rec(await c.get(f"/admin-api/users/{user_id}", headers=_ah()))
    assert r.status_code == 200, f"admin get user {user_id}: {r.status_code}"
    assert "user" in r.json(), f"'user' key missing from admin user detail"

async def test_T121_admin_user_detail_rejects_user_key(c):
    r = rec(await c.get("/admin-api/users/some-fake-uuid-0000", headers=_uh()))
    assert r.status_code in (401, 403), \
        f"Admin endpoint with user key → expected 401/403, got {r.status_code}"

async def test_T122_admin_wayf_totals(c):
    r = rec(await c.get("/admin/wayf-points/totals", headers=_ah()))
    assert r.status_code == 200
    d = r.json()
    assert "total_users_earning" in d, f"'total_users_earning' missing: {list(d.keys())}"
    assert "pool_remaining"      in d, f"'pool_remaining' missing: {list(d.keys())}"
    assert "current_rate"        in d, f"'current_rate' missing: {list(d.keys())}"

async def test_T123_admin_no_internal_markup_fields(c):
    list_r = await c.get("/admin-api/users", headers=_ah())
    if list_r.status_code != 200:
        pytest.skip("Admin users unavailable")
    users = list_r.json().get("users", [])
    if not users:
        pytest.skip("No users to inspect")
    r = rec(await c.get(f"/admin-api/users/{users[0]['id']}", headers=_ah()))
    if r.status_code != 200:
        pytest.skip("Admin user detail unavailable")
    d = r.json()
    for bad in ("markup_pct", "fee_bps", "wayf_bonus_pct", "managed_30pct", "byok_10pct"):
        _scan(d, f"admin user detail (T123 manual check for '{bad}')")
    # _scan already recorded any hits; assert none appeared
    _bad_admin_fields = {"markup_pct", "fee_bps", "wayf_bonus_pct", "managed_30pct", "byok_10pct"}
    hits = [(u, f, v) for u, f, v in _forbidden_hits if f in _bad_admin_fields]
    assert not hits, f"Internal fields found in admin user detail: {[f for _, f, _ in hits]}"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 14 — RATE LIMITING
# ─────────────────────────────────────────────────────────────────────────────

async def test_T130_search_rate_limit(c):
    """Fire 22 concurrent searches — expect at least one 429."""
    resps = await asyncio.gather(
        *[c.get("/search", params={"q": "translate"}, headers=_uh()) for _ in range(22)],
        return_exceptions=True,
    )
    statuses = []
    for r in resps:
        if isinstance(r, httpx.Response):
            rec(r)
            statuses.append(r.status_code)
    got_429 = 429 in statuses
    if not got_429:
        _warnings.append(
            f"T130: 22 rapid searches — no 429 seen (statuses: {set(statuses)}). "
            "Rate limiting may be too permissive — flag for security audit."
        )
    # Per spec: warning, not hard fail

async def test_T131_run_rate_limit(c):
    """Fire 15 concurrent /run calls — expect 429 or credits gate (402)."""
    resps = await asyncio.gather(
        *[c.post("/run", headers=_uh(), json={"intent": "translate hi"}) for _ in range(15)],
        return_exceptions=True,
    )
    statuses = []
    for r in resps:
        if isinstance(r, httpx.Response):
            rec(r)
            statuses.append(r.status_code)
    got_limited = any(s in (429, 402) for s in statuses)
    if not got_limited:
        _warnings.append(
            f"T131: 15 rapid /run calls — no 429/402 seen (statuses: {set(statuses)}). "
            "Review rate limit thresholds."
        )

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 15 — RESPONSE CLEANLINESS
# ─────────────────────────────────────────────────────────────────────────────

async def test_T140_no_forbidden_fields_in_any_response(c):
    """Assert no forbidden fields appeared in any response during the test run."""
    if _forbidden_hits:
        details = "\n".join(
            f"  [{field}] at {url} = {val}"
            for url, field, val in _forbidden_hits
        )
        pytest.fail(
            f"{len(_forbidden_hits)} forbidden field(s) found across responses:\n{details}"
        )

async def test_T141_error_responses_no_stack_traces(c):
    """Probe error paths — assert no tracebacks, SQL, or internal paths leak."""
    probes = await asyncio.gather(
        c.get("/search", params={"q": "x" * 3000}, headers=_uh()),
        c.post("/execute", headers=_uh(), json={"service_slug": "", "params": {}}),
        c.post("/run",     headers=_uh(), json={}),
        c.get("/services/00000000-0000-0000-0000-000000000000"),
        return_exceptions=True,
    )
    for r in probes:
        if isinstance(r, httpx.Response):
            rec(r)
            if r.status_code >= 400:
                no_leak(r)

async def test_T142_zero_500s_in_test_run(c):
    """Hard fail if any endpoint returned HTTP 500 during the run."""
    if _500_errors:
        lines = "\n".join(
            f"  {e['method']} {e['url']}\n  {e['body_preview'][:120]}"
            for e in _500_errors
        )
        pytest.fail(
            f"{len(_500_errors)} unexpected 500 error(s) during test run:\n{lines}"
        )

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 16 — WAYFORTHQL v1.1 FILTERS (T143–T147)
# ─────────────────────────────────────────────────────────────────────────────

async def _skip_if_no_query_access(c):
    tier = await _get_tier(c)
    if tier not in ("starter", "pro", "growth", "enterprise"):
        pytest.skip(f"WayforthQL requires starter tier or above; account is '{tier}'")


async def test_T143_query_latency_max_valid(c):
    """POST /query with latency_max=500 returns 200 and filters_applied reflects the value."""
    await _skip_if_no_query_access(c)
    r = rec(await c.post("/query", headers=_uh(), json={
        "query": "api",
        "latency_max": 500,
        "limit": 5,
    }))
    assert r.status_code == 200, \
        f"latency_max=500 should return 200, got {r.status_code}: {r.text[:200]}"
    d = r.json()
    applied = d.get("filters_applied", {})
    assert applied.get("latency_max") == 500, \
        f"filters_applied.latency_max should be 500, got {applied.get('latency_max')}"


async def test_T144_query_latency_max_invalid(c):
    """POST /query with latency_max=99999 returns 422 with error invalid_latency_max."""
    await _skip_if_no_query_access(c)
    r = rec(await c.post("/query", headers=_uh(), json={
        "query": "api",
        "latency_max": 99999,
    }))
    assert r.status_code == 422, \
        f"latency_max=99999 should return 422, got {r.status_code}: {r.text[:200]}"
    d = r.json()
    # FastAPI wraps custom error bodies inside {"detail": {...}}
    error_body = d.get("detail") if isinstance(d.get("detail"), dict) else d
    assert error_body.get("error") == "invalid_latency_max", \
        f"Expected error='invalid_latency_max', got {error_body}"


async def test_T145_query_region_us_returns_results(c):
    """POST /query with region='us' returns 200 and non-empty results."""
    await _skip_if_no_query_access(c)
    r = rec(await c.post("/query", headers=_uh(), json={
        "query": "api",
        "region": "us",
        "limit": 10,
    }))
    assert r.status_code == 200, \
        f"region=us should return 200, got {r.status_code}: {r.text[:200]}"
    d = r.json()
    results = d.get("results", [])
    assert len(results) > 0, \
        "region='us' returned no results — region backfill may not have run"
    applied = d.get("filters_applied", {})
    assert applied.get("region") == "us", \
        f"filters_applied.region should be 'us', got {applied.get('region')}"


async def test_T146_query_region_invalid(c):
    """POST /query with region='invalid_region' returns 422."""
    await _skip_if_no_query_access(c)
    r = rec(await c.post("/query", headers=_uh(), json={
        "query": "api",
        "region": "invalid_region",
    }))
    assert r.status_code == 422, \
        f"region='invalid_region' should return 422, got {r.status_code}: {r.text[:200]}"


async def test_T147_query_payment_rail_x402(c):
    """POST /query with payment_rail='x402' returns 200; all results have x402_supported=True."""
    await _skip_if_no_query_access(c)
    r = rec(await c.post("/query", headers=_uh(), json={
        "query": "api",
        "payment_rail": "x402",
        "limit": 10,
    }))
    assert r.status_code == 200, \
        f"payment_rail=x402 should return 200, got {r.status_code}: {r.text[:200]}"
    d = r.json()
    results = d.get("results", [])
    for svc in results:
        opts = svc.get("payment_options", {})
        x402 = opts.get("x402_supported")
        assert x402 is True, \
            f"payment_rail=x402 filter leaked non-x402 service: slug={svc.get('slug')}, x402_supported={x402}"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 17 — /run INTENT ROUTING (T148–T150)
# ─────────────────────────────────────────────────────────────────────────────

async def test_T148_run_weather_intent(c):
    """POST /run with weather intent routes to openweather and returns temp/condition."""
    r = rec(await c.post("/run", headers=_uh(), json={
        "intent": "weather in London",
    }))
    assert r.status_code == 200, \
        f"weather intent should return 200, got {r.status_code}: {r.text[:300]}"
    d = r.json()
    svc = d.get("service_used", {})
    assert svc.get("slug") == "openweather", \
        f"weather intent should route to openweather, got slug={svc.get('slug')}"
    result = d.get("result", {})
    assert "temp_c" in result or "condition" in result, \
        f"weather result missing temp_c/condition: keys={list(result.keys())}"


async def test_T149_run_financial_intent(c):
    """POST /run with stock price intent routes to alphavantage and returns price data."""
    r = rec(await c.post("/run", headers=_uh(), json={
        "intent": "stock price for GOOGL",
    }))
    assert r.status_code == 200, \
        f"financial intent should return 200, got {r.status_code}: {r.text[:300]}"
    d = r.json()
    svc = d.get("service_used", {})
    assert svc.get("slug") == "alphavantage", \
        f"financial intent should route to alphavantage, got slug={svc.get('slug')}"
    result = d.get("result", {})
    assert "symbol" in result or "close" in result or "open" in result, \
        f"financial result missing symbol/close/open: keys={list(result.keys())}"


async def test_T150_run_search_intent(c):
    """POST /run with web search intent routes to brave/serper/tavily and returns results."""
    r = rec(await c.post("/run", headers=_uh(), json={
        "intent": "search the web for python",
    }))
    assert r.status_code == 200, \
        f"search intent should return 200, got {r.status_code}: {r.text[:300]}"
    d = r.json()
    svc = d.get("service_used", {})
    assert svc.get("slug") in ("brave", "serper", "tavily"), \
        f"search intent should route to brave/serper/tavily, got slug={svc.get('slug')}"
    result = d.get("result", {})
    assert "results" in result or "organic" in result or "web" in result, \
        f"search result missing results/organic/web: keys={list(result.keys())}"


# SECTION 18 — v0.6.0 NEW FEATURES (T151–T160)

@pytest.mark.asyncio
async def test_T151_run_stream_llm_intent(c):
    """POST /run with stream=true and LLM intent returns 200 text/event-stream."""
    r = await c.post("/run", headers=_uh(), json={
        "intent": "summarize in one sentence",
        "input": {"messages": [{"role": "user", "content": "Summarize: AI is transforming software."}]},
        "stream": True,
    })
    assert r.status_code == 200, f"stream LLM should be 200, got {r.status_code}: {r.text[:200]}"
    ct = r.headers.get("content-type", "")
    assert "text/event-stream" in ct, f"expected text/event-stream, got {ct!r}"
    assert "data:" in r.text, f"no SSE data in response: {r.text[:200]}"


@pytest.mark.asyncio
async def test_T152_run_stream_non_llm_returns_400(c):
    """POST /run with stream=true and non-LLM intent returns 400 streaming_not_supported."""
    r = await c.post("/run", headers=_uh(), json={
        "intent": "weather in Paris",
        "input": {"city": "Paris"},
        "stream": True,
    })
    assert r.status_code == 400, f"non-LLM stream should be 400, got {r.status_code}: {r.text[:200]}"
    d = r.json()
    assert d.get("error") == "streaming_not_supported", f"unexpected error: {d}"


@pytest.mark.asyncio
async def test_T153_execute_batch_two_slugs(c):
    """POST /execute/batch with 2 valid slugs returns 200 with results array."""
    r = rec(await c.post("/execute/batch", headers=_uh(), json={
        "calls": [
            {"slug": "openweather", "params": {"city": "Berlin"}},
            {"slug": "openweather", "params": {"city": "Tokyo"}},
        ]
    }))
    assert r.status_code == 200, f"batch should be 200, got {r.status_code}: {r.text[:300]}"
    d = r.json()
    assert "results" in d, f"missing results: {d.keys()}"
    assert len(d["results"]) == 2, f"expected 2 results, got {len(d['results'])}"


@pytest.mark.asyncio
async def test_T154_execute_batch_six_slugs_returns_422(c):
    """POST /execute/batch with 6 slugs returns 422 too_many_calls."""
    r = rec(await c.post("/execute/batch", headers=_uh(), json={
        "calls": [{"slug": "openweather", "params": {"city": "x"}}] * 6
    }))
    assert r.status_code == 422, f"6-slug batch should be 422, got {r.status_code}: {r.text[:200]}"
    d = r.json()
    detail = d.get("detail", d)
    assert detail.get("error") == "too_many_calls", f"unexpected detail: {detail}"


@pytest.mark.asyncio
async def test_T155_account_usage_history(c):
    """GET /account/usage/history returns 200 with history array."""
    r = rec(await c.get("/account/usage/history", headers=_uh()))
    assert r.status_code == 200, f"usage history should be 200, got {r.status_code}: {r.text[:200]}"
    d = r.json()
    assert "history" in d, f"missing history: {d.keys()}"
    assert isinstance(d["history"], list), "history should be a list"


@pytest.mark.asyncio
async def test_T156_account_wayf_points_history(c):
    """GET /account/wayf-points/history returns 200 with total_points field."""
    r = rec(await c.get("/account/wayf-points/history", headers=_uh()))
    assert r.status_code == 200, f"wayf-points history should be 200, got {r.status_code}: {r.text[:200]}"
    d = r.json()
    assert "total_points" in d, f"missing total_points: {d.keys()}"


@pytest.mark.asyncio
async def test_T157_run_intents_returns_nine(c):
    """GET /run/intents returns 200 with intents array of 9 entries."""
    r = rec(await c.get("/run/intents"))
    assert r.status_code == 200, f"run/intents should be 200, got {r.status_code}: {r.text[:200]}"
    d = r.json()
    assert "intents" in d, f"missing intents: {d.keys()}"
    assert len(d["intents"]) == 9, f"expected 9 intents, got {len(d['intents'])}"


@pytest.mark.asyncio
async def test_T158_openapi_json(c):
    """GET /openapi.json returns 200 with openapi version field."""
    r = await c.get("/openapi.json")
    assert r.status_code == 200, f"openapi.json should be 200, got {r.status_code}"
    d = r.json()
    assert "openapi" in d, f"missing openapi field: {list(d.keys())[:10]}"


@pytest.mark.asyncio
async def test_T159_services_slug_health(c):
    """GET /services/groq/health returns 200 with slug field."""
    r = rec(await c.get("/services/groq/health"))
    assert r.status_code == 200, f"services/groq/health should be 200, got {r.status_code}: {r.text[:200]}"
    d = r.json()
    assert "slug" in d, f"missing slug: {d.keys()}"
    assert d["slug"] == "groq", f"unexpected slug: {d['slug']}"


@pytest.mark.asyncio
async def test_T160_sitemap_xml(c):
    """GET /sitemap.xml returns 200 with application/xml content-type."""
    r = await c.get("/sitemap.xml")
    assert r.status_code == 200, f"sitemap.xml should be 200, got {r.status_code}: {r.text[:200]}"
    ct = r.headers.get("content-type", "")
    assert "xml" in ct, f"expected xml content-type, got {ct!r}"
    assert "wayforth.io" in r.text, f"sitemap missing wayforth.io URLs"
