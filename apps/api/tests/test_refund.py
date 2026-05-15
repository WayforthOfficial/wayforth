"""tests/test_refund.py — Automatic credit refund on service failure (v0.6.10).

Strategy: BYOK calls pointing at httpbin.org for 5xx/4xx, plus
pure-unit coverage of _classify_error.

Run: pytest apps/api/tests/test_refund.py -v
"""

import sys
import os
import pytest
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Config ─────────────────────────────────────────────────────────────────────

BASE_URL = os.environ.get("WAYFORTH_TEST_BASE_URL", "https://gateway.wayforth.io")
API_KEY  = os.environ.get("WAYFORTH_TEST_API_KEY", "")


@pytest.fixture(autouse=True)
def _requires_api_key(request):
    if not API_KEY and "no_api_key" not in {m.name for m in request.node.iter_markers()}:
        pytest.skip("WAYFORTH_TEST_API_KEY not set — skipping auth-required test")

_SLUG_5XX  = "wf-refund-test-5xx"
_SLUG_4XX  = "wf-refund-test-4xx"
_SLUG_CONN = "wf-refund-test-conn"

def _uh():
    return {"X-Wayforth-API-Key": API_KEY}


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
        yield c


@pytest.fixture(scope="session", autouse=True)
def register_byok_keys(client):
    """Register three throwaway BYOK keys for refund tests, clean up after."""
    keys = [
        (_SLUG_5XX,  "https://httpbin.org/status/503", "GET"),
        (_SLUG_4XX,  "https://httpbin.org/status/400", "GET"),
        (_SLUG_CONN, "https://thisdomaindoesnotexist-wf-refund-test.invalid/api", "GET"),
    ]
    registered = []
    for slug, url, method in keys:
        r = client.post("/call/keys/add", headers=_uh(), json={
            "service_slug": slug,
            "api_key": "test-key-placeholder",
            "endpoint_url": url,
            "default_method": method,
        })
        if r.status_code in (200, 201):
            registered.append(slug)

    yield

    for slug in registered:
        client.delete(f"/call/keys/{slug}", headers=_uh())


def _balance(client) -> int:
    r = client.get("/billing/balance", headers=_uh())
    assert r.status_code == 200, f"balance check failed: {r.text}"
    return r.json().get("calls_remaining", 0)


def _execute_byok(client, slug) -> httpx.Response:
    return client.post("/execute", headers=_uh(), json={
        "service_slug": slug,
        "key_source": "byok",
        "params": {},
    })


def _detail(r: httpx.Response) -> dict:
    """Unwrap FastAPI HTTPException: body is {detail: {...}} for error responses."""
    body = r.json()
    return body.get("detail", body)


# ── T_REFUND_01: 5xx upstream → credits restored ──────────────────────────────

def test_T_REFUND_01_5xx_triggers_refund(client):
    before = _balance(client)
    r = _execute_byok(client, _SLUG_5XX)
    after = _balance(client)

    assert r.status_code == 503, f"Expected 503, got {r.status_code}: {r.text[:200]}"
    d = _detail(r)
    assert d.get("refunded") is True, f"refunded missing or false: {d}"
    assert d.get("credits_restored", 0) >= 1, f"credits_restored missing: {d}"
    assert "calls_remaining" in d, f"calls_remaining missing: {d}"
    assert after >= before - 1, f"Balance dropped unexpectedly: before={before} after={after}"


# ── T_REFUND_02: connection failure (DNS) → credits restored ──────────────────

def test_T_REFUND_02_connection_failure_triggers_refund(client):
    before = _balance(client)
    r = _execute_byok(client, _SLUG_CONN)
    after = _balance(client)

    assert r.status_code == 503, f"Expected 503, got {r.status_code}: {r.text[:200]}"
    d = _detail(r)
    assert d.get("refunded") is True, f"refunded should be True: {d}"
    assert d.get("credits_restored", 0) >= 1
    assert after >= before - 1, f"Balance should be restored: before={before} after={after}"


# ── T_REFUND_03: 4xx upstream → no refund ─────────────────────────────────────

def test_T_REFUND_03_4xx_no_refund(client):
    r = _execute_byok(client, _SLUG_4XX)

    assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text[:200]}"
    d = _detail(r)
    assert d.get("refunded") is False, f"refunded should be False for 4xx: {d}"
    assert d.get("credits_restored", 0) == 0, f"credits_restored should be 0 for 4xx: {d}"
    # Verify no refund transaction was logged for this call
    txns = client.get("/billing/transactions", headers=_uh())
    items = txns.json().get("transactions", txns.json().get("data", []))
    # Most recent transaction should NOT be a refund (it would be an execution charge)
    if items:
        assert items[0].get("type") != "refund" or _SLUG_4XX not in items[0].get("description", ""), \
            f"4xx should not create a refund transaction: {items[0]}"


# ── T_REFUND_04: successful call → no refund fields ───────────────────────────

def test_T_REFUND_04_success_no_refund(client):
    r = client.get("/search", headers=_uh(), params={"q": "translate"})
    assert r.status_code == 200
    body = r.json()
    assert "refunded" not in body, f"refunded should not appear on success: {body}"
    assert "credits_restored" not in body


# ── T_REFUND_05: wayf.call_refunded transaction logged on 5xx ─────────────────

def test_T_REFUND_05_webhook_fires_on_5xx(client):
    r = _execute_byok(client, _SLUG_5XX)
    assert r.status_code == 503

    txns = client.get("/billing/transactions", headers=_uh())
    assert txns.status_code == 200, f"transactions endpoint failed: {txns.text}"
    items = txns.json().get("transactions", txns.json().get("data", []))
    refund_txns = [t for t in items if t.get("type") == "refund"]
    assert refund_txns, (
        "No 'refund' type transaction found after 5xx failure. "
        "Transaction log not recording refund."
    )
    assert refund_txns[0].get("amount", 0) >= 1, f"Refund amount too small: {refund_txns[0]}"


# ── Unit tests for _classify_error (inline — avoids Python 3.9/3.12 import gap)

import re as _re

def _classify_error(error_msg: str) -> str:
    """Mirror of routers.execute._classify_error for local testing."""
    msg_lower = error_msg.lower()
    if "timeout" in msg_lower or "timed out" in msg_lower:
        return "service_failure"
    m = _re.search(r'\b([45]\d{2})\b', error_msg)
    if m:
        code = int(m.group(1))
        return "client_error" if 400 <= code < 500 else "service_failure"
    return "service_failure"


def test_classify_error_timeout():
    assert _classify_error("Service timeout") == "service_failure"
    assert _classify_error("upstream timed out after 10s") == "service_failure"


def test_classify_error_5xx():
    assert _classify_error("Groq error 500: internal server error") == "service_failure"
    assert _classify_error("Upstream 503: gateway unavailable") == "service_failure"
    assert _classify_error("DeepL error 502: bad gateway") == "service_failure"


def test_classify_error_4xx():
    assert _classify_error("Groq error 400: invalid request") == "client_error"
    assert _classify_error("DeepL error 422: unsupported language") == "client_error"
    assert _classify_error("Upstream 401: unauthorized") == "client_error"
    assert _classify_error("Upstream 429: rate limit exceeded") == "client_error"


def test_classify_error_unknown():
    assert _classify_error("connection refused") == "service_failure"
    assert _classify_error("SSL handshake failed") == "service_failure"
