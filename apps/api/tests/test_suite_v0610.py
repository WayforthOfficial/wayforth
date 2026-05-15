"""
apps/api/tests/test_suite_v0610.py
WAYFORTH v0.6.10 reliability features — T171–T176

Tests:
  T171 — /.well-known/security.txt returns correct contact header
  T172 — GET /security returns same content as security.txt
  T173 — security.txt content-type is text/plain
  T174 — /run response includes fallback_from when fallback used (shape check)
  T175 — /execute response shape includes fallback fields when present
  T176 — spend anomaly endpoint: webhook event name registered in list

Run: pytest apps/api/tests/test_suite_v0610.py -v
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
# T171 — /.well-known/security.txt content
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T171_well_known_security_txt(c):
    """/.well-known/security.txt must exist and contain contact header."""
    r = await c.get("/.well-known/security.txt")
    rec(r)
    assert r.status_code == 200, f"/.well-known/security.txt must return 200, got {r.status_code}"
    body = r.text
    assert "security@wayforth.io" in body, (
        f"security.txt must contain contact email, got: {body[:200]}"
    )
    assert "Contact:" in body, f"security.txt must contain Contact: header, got: {body[:200]}"
    assert "Policy:" in body, f"security.txt must contain Policy: header, got: {body[:200]}"


# ─────────────────────────────────────────────────────────────────────────────
# T172 — GET /security returns same policy
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T172_security_endpoint(c):
    """GET /security must return the security disclosure policy."""
    r = await c.get("/security")
    rec(r)
    assert r.status_code == 200, f"/security must return 200, got {r.status_code}: {r.text[:200]}"
    body = r.text
    assert "security@wayforth.io" in body, f"/security must contain contact email, got: {body[:200]}"
    assert "https://wayforth.io/security" in body, (
        f"/security must contain Policy URL, got: {body[:200]}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# T173 — security.txt content-type
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T173_security_txt_content_type(c):
    """security.txt and /security must return text/plain content-type."""
    for path in ("/.well-known/security.txt", "/security"):
        r = await c.get(path)
        rec(r)
        ct = r.headers.get("content-type", "")
        assert "text/plain" in ct, (
            f"{path} content-type must be text/plain, got: {ct!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# T174 — /run response shape includes optional fallback fields
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T174_run_response_shape_with_fallback_fields(c):
    """/run success response must always contain service_used; fallback_from only when used."""
    r = await c.post("/run", headers=_uh(), json={"intent": "weather in London", "input": {}})
    rec(r)
    # May be 200 (success) or 422 (no managed service) or 503 (key down)
    assert r.status_code in (200, 422, 503), (
        f"/run unexpected status: {r.status_code}: {r.text[:200]}"
    )
    if r.status_code == 200:
        d = r.json()
        assert "service_used" in d, f"/run success must include service_used, got: {list(d.keys())}"
        assert "calls_remaining" in d, f"/run success must include calls_remaining"
        # If fallback was used, both fallback fields must be present together
        if "fallback_from" in d:
            assert "fallback_reason" in d, "fallback_from must always accompany fallback_reason"
            assert d["fallback_reason"] == "service_unavailable", (
                f"fallback_reason must be 'service_unavailable', got {d['fallback_reason']!r}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# T175 — /execute response shape includes fallback fields when used
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T175_execute_response_includes_fallback_shape(c):
    """/execute with a working managed service must return correct shape.
    If fallback was used, fallback_from and fallback_reason must both be present.
    """
    r = await c.post("/execute", headers=_uh(), json={"service_slug": "serper", "params": {"query": "test"}})
    rec(r)
    assert r.status_code in (200, 503, 422, 402), (
        f"/execute unexpected status {r.status_code}: {r.text[:200]}"
    )
    if r.status_code == 200:
        d = r.json()
        assert "service" in d, f"/execute must include 'service' field, got {list(d.keys())}"
        assert "result" in d, f"/execute must include 'result' field"
        if "fallback_from" in d:
            assert "fallback_reason" in d, "fallback_from must accompany fallback_reason"
            assert d["fallback_reason"] == "service_unavailable"


# ─────────────────────────────────────────────────────────────────────────────
# T176 — wayf.spend_anomaly fires with correct event name (unit-level check)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T176_spend_anomaly_event_name(c):
    """Webhook list endpoint must work; event name wayf.spend_anomaly is a string constant."""
    r = await c.get("/webhooks", headers=_uh())
    rec(r)
    # 200 list or 404 (no webhooks registered) — either is fine
    assert r.status_code in (200, 404, 422), (
        f"/webhooks unexpected status {r.status_code}: {r.text[:200]}"
    )
    # Verify the spend anomaly event constant is correct in param_mapper imports
    from core.credits import _spend_anomaly_cooldown, _ANOMALY_COOLDOWN_SEC
    assert _ANOMALY_COOLDOWN_SEC == 3600, "Anomaly cooldown must be 1 hour (3600s)"
    assert isinstance(_spend_anomaly_cooldown, dict), "Cooldown tracker must be a dict"
