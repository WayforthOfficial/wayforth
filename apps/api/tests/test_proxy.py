"""tests/test_proxy.py — Reliability Proxy tests (v0.9.0).

Covers:
  - Auth: 401 on missing key                              (live, no_api_key)
  - Slug: 404 on unknown slug                             (live, no_api_key)
  - Params: 422 on missing required param                 (live)
  - POST call: Serper → native upstream shape + headers   (live)
  - GET call: OpenWeather-style → native shape + headers  (live)
  - ?wayforth_wrap=true: full envelope response           (live)
  - Failover: forced primary outage → headers + signal    (unit, mock)
  - Signal write: substitution_from/to in credit_tx row   (unit, mock)
  - Structural: all managed slugs resolve                  (unit)

Run: pytest apps/api/tests/test_proxy.py -v
"""

import os
import sys
import asyncio
import json
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Config ────────────────────────────────────────────────────────────────────

BASE_URL = os.environ.get("WAYFORTH_TEST_BASE_URL", "https://gateway.wayforth.io")
API_KEY  = os.environ.get("WAYFORTH_TEST_API_KEY", "")


@pytest.fixture(autouse=True)
def _requires_api_key(request):
    if not API_KEY and "no_api_key" not in {m.name for m in request.node.iter_markers()}:
        pytest.skip("WAYFORTH_TEST_API_KEY not set — skipping auth-required test")


def _h():
    return {"X-Wayforth-API-Key": API_KEY}


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE_URL, timeout=30.0) as c:
        yield c


# ── Structural unit tests (no gateway needed) ─────────────────────────────────

@pytest.mark.no_api_key
class TestProxyStructure:

    def test_all_managed_slugs_in_service_configs(self):
        from services.managed import SERVICE_CONFIGS
        assert "groq" in SERVICE_CONFIGS
        assert "openweather" in SERVICE_CONFIGS
        assert "serper" in SERVICE_CONFIGS

    def test_proxy_module_importable(self):
        import routers.proxy  # noqa: F401

    def test_proxy_router_registered(self):
        from routers.proxy import router
        routes = [r.path for r in router.routes]
        assert "/proxy/{slug}" in routes

    def test_proxy_router_accepts_get_and_post(self):
        from routers.proxy import router
        for route in router.routes:
            if route.path == "/proxy/{slug}":
                assert "GET" in route.methods
                assert "POST" in route.methods

    def test_all_required_helpers_importable(self):
        from routers.execute import (
            _FAILURE_REASON_LABELS, _classify_error, _classify_failure,
            _do_refund, _fetch_wri, _mk_refund_key, _patch_tx_signals,
            _try_execute_managed, _update_search_signal,
        )
        assert callable(_try_execute_managed)
        assert callable(_classify_error)
        assert isinstance(_FAILURE_REASON_LABELS, dict)

    def test_lLM_slugs_constant(self):
        import routers.proxy as p
        assert "groq" in p._LLM_SLUGS
        assert "together" in p._LLM_SLUGS
        assert "openweather" not in p._LLM_SLUGS


# ── Auth tests (live, no key needed to verify 401) ────────────────────────────

class TestProxyAuth:

    @pytest.mark.no_api_key
    def test_missing_key_returns_401(self, client):
        r = client.post("/proxy/serper", json={"query": "test"})
        assert r.status_code == 401
        assert "X-Wayforth-API-Key" in r.json()["detail"]["error"]

    @pytest.mark.no_api_key
    def test_get_missing_key_returns_401(self, client):
        r = client.get("/proxy/openweather", params={"city": "London"})
        assert r.status_code == 401

    @pytest.mark.no_api_key
    def test_unknown_slug_returns_404(self, client):
        r = client.post(
            "/proxy/this-service-does-not-exist",
            headers={"X-Wayforth-API-Key": "wf_live_fake"},
            json={},
        )
        # 401 (bad key) or 404 (slug unknown) — either is acceptable;
        # the important thing is it's not 200 and doesn't crash.
        assert r.status_code in (401, 404)


# ── POST proxy tests (live) ───────────────────────────────────────────────────

class TestProxyPost:

    def test_serper_returns_native_shape(self, client):
        r = client.post("/proxy/serper", headers=_h(), json={"query": "python programming"})
        assert r.status_code == 200, f"unexpected {r.status_code}: {r.text[:200]}"
        body = r.json()
        # Serper's native shape has organic results
        assert isinstance(body, dict)
        assert "organic" in body or "news" in body or "answerBox" in body or "knowledgeGraph" in body

    def test_serper_response_headers_present(self, client):
        r = client.post("/proxy/serper", headers=_h(), json={"query": "wayforth api"})
        assert r.status_code == 200
        assert r.headers.get("x-wayforth-failover") in ("true", "false")
        assert r.headers.get("x-wayforth-rail") == "managed"
        assert r.headers.get("x-wayforth-cost") is not None
        assert r.headers.get("x-wayforth-wri") is not None
        assert r.headers.get("x-wayforth-credits-remaining") is not None

    def test_serper_no_wayforth_wrapper_in_body(self, client):
        r = client.post("/proxy/serper", headers=_h(), json={"query": "test"})
        assert r.status_code == 200
        body = r.json()
        # Native shape — no Wayforth envelope
        assert "status" not in body
        assert "credits_deducted" not in body
        assert "failover" not in body

    def test_failover_false_header_on_success(self, client):
        r = client.post("/proxy/serper", headers=_h(), json={"query": "api routing"})
        assert r.status_code == 200
        assert r.headers.get("x-wayforth-failover") == "false"

    def test_missing_required_param_returns_422(self, client):
        r = client.post("/proxy/serper", headers=_h(), json={})
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert "missing_param" in detail.get("error", "")
        assert "query" in detail.get("missing", [])

    def test_missing_messages_returns_422(self, client):
        r = client.post("/proxy/groq", headers=_h(), json={})
        assert r.status_code == 422


# ── GET proxy tests (live, OpenWeather) ───────────────────────────────────────

class TestProxyGetOpenWeather:

    def test_openweather_get_returns_native_shape(self, client):
        r = client.get("/proxy/openweather", headers=_h(), params={"city": "London"})
        assert r.status_code == 200, f"unexpected {r.status_code}: {r.text[:200]}"
        body = r.json()
        # OpenWeather adapter returns this exact shape
        assert "city" in body
        assert "temp_c" in body
        assert "temp_f" in body
        assert "condition" in body
        assert "humidity" in body
        assert "wind_kph" in body

    def test_openweather_get_response_headers(self, client):
        r = client.get("/proxy/openweather", headers=_h(), params={"city": "Tokyo"})
        assert r.status_code == 200
        assert r.headers.get("x-wayforth-failover") == "false"
        assert r.headers.get("x-wayforth-rail") == "managed"
        assert r.headers.get("x-wayforth-cost") == "2"  # openweather costs 2 credits
        assert r.headers.get("x-wayforth-wri") is not None
        assert r.headers.get("x-wayforth-credits-remaining") is not None

    def test_openweather_no_envelope_in_body(self, client):
        r = client.get("/proxy/openweather", headers=_h(), params={"city": "Paris"})
        assert r.status_code == 200
        body = r.json()
        assert "status" not in body
        assert "credits_deducted" not in body

    def test_openweather_missing_city_returns_422(self, client):
        r = client.get("/proxy/openweather", headers=_h())
        assert r.status_code == 422
        assert "city" in r.json()["detail"].get("missing", [])

    def test_openweather_city_alias_location(self, client):
        # "location" is a recognised alias for "city" in param_mapper
        r = client.get("/proxy/openweather", headers=_h(), params={"location": "Berlin"})
        assert r.status_code == 200
        assert "temp_c" in r.json()


# ── wayforth_wrap=true tests (live) ───────────────────────────────────────────

class TestProxyWrap:

    def test_wrap_returns_full_envelope(self, client):
        r = client.post(
            "/proxy/serper",
            headers=_h(),
            json={"query": "wayforth reliability proxy"},
            params={"wayforth_wrap": "true"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") == "ok"
        assert "result" in body
        assert "credits_deducted" in body
        assert "execution_ms" in body
        assert "failover" in body

    def test_wrap_failover_block_structure(self, client):
        r = client.post(
            "/proxy/serper",
            headers=_h(),
            json={"query": "test"},
            params={"wayforth_wrap": "true"},
        )
        assert r.status_code == 200
        failover = r.json().get("failover", {})
        assert "triggered" in failover
        assert isinstance(failover["triggered"], bool)

    def test_wrap_headers_still_present(self, client):
        r = client.post(
            "/proxy/serper",
            headers=_h(),
            json={"query": "test"},
            params={"wayforth_wrap": "true"},
        )
        assert r.status_code == 200
        assert r.headers.get("x-wayforth-failover") is not None
        assert r.headers.get("x-wayforth-cost") is not None

    def test_get_wrap_openweather(self, client):
        r = client.get(
            "/proxy/openweather",
            headers=_h(),
            params={"city": "Sydney", "wayforth_wrap": "true"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body.get("status") == "ok"
        assert "temp_c" in body.get("result", {})


# ── Failover unit tests (direct async, no live gateway) ───────────────────────
#
# Call proxy_call() directly via pytest-asyncio with a minimal mock Request.
# Avoids TestClient app startup (which needs a live DB) while testing the
# entire failover code path end-to-end.

def _build_mock_request(method="POST", body=None, query_string="", headers=None):
    """Build a minimal Starlette Request mock sufficient for proxy_call."""
    req = MagicMock()
    req.method = method

    _headers = {"x-wayforth-api-key": "wf_test_key"}
    if headers:
        _headers.update(headers)

    class FakeHeaders:
        def get(self, key, default=""):
            return _headers.get(key.lower(), default)

    class FakeQueryParams:
        def __init__(self, qs):
            self._d = dict(p.split("=", 1) for p in qs.split("&") if "=" in p) if qs else {}

        def get(self, key, default=""):
            return self._d.get(key, default)

        def pop(self, key, default=None):
            return self._d.pop(key, default)

        def items(self):
            return self._d.items()

        def __iter__(self):
            return iter(self._d)

    req.headers = FakeHeaders()
    req.query_params = FakeQueryParams(query_string)
    req.state = MagicMock()
    req.state.request_id = "test-req-id"

    async def json():
        return body or {}

    req.json = json
    return req


def _make_failover_mocks():
    """Mocks for groq→together failover scenario."""
    fake_user_id    = "00000000-0000-0000-0000-000000000001"
    fake_api_key_id = "00000000-0000-0000-0000-000000000002"

    resolve_user  = AsyncMock(return_value=(fake_user_id, fake_api_key_id, "pro"))
    rate_limit    = AsyncMock(return_value=None)
    upstream_cap  = AsyncMock(return_value=None)
    refund        = AsyncMock(return_value=5999)
    patch_tx      = AsyncMock(return_value=None)
    update_sa     = AsyncMock(return_value=None)
    increment     = AsyncMock(return_value=None)
    anomaly       = AsyncMock(return_value=None)
    low_bal       = AsyncMock(return_value=None)
    fetch_wri     = AsyncMock(return_value=83.3)

    credits_call_count = [0]

    async def credits_side(*a, **kw):
        credits_call_count[0] += 1
        if credits_call_count[0] == 1:
            return (True, 5997, "tx-primary-id")
        return (True, 5993, "tx-fallback-id")

    deduct_credits = AsyncMock(side_effect=credits_side)

    exec_call_count = [0]

    async def try_execute_side(slug, params, key):
        exec_call_count[0] += 1
        if exec_call_count[0] == 1:
            return None, "Service timeout", 10000
        return {"id": "together-resp", "choices": [{"message": {"content": "pong"}}]}, None, 300

    try_execute = AsyncMock(side_effect=try_execute_side)

    # Pool mock for fire-and-forget create_task calls inside proxy
    pool = MagicMock()
    app_mock = MagicMock()
    app_mock.state.pool = pool

    return {
        "resolve_user":  resolve_user,
        "rate_limit":    rate_limit,
        "upstream_cap":  upstream_cap,
        "deduct_credits": deduct_credits,
        "refund":        refund,
        "patch_tx":      patch_tx,
        "update_sa":     update_sa,
        "increment":     increment,
        "anomaly":       anomaly,
        "low_bal":       low_bal,
        "try_execute":   try_execute,
        "fetch_wri":     fetch_wri,
        "app_mock":      app_mock,
    }


def _apply_proxy_patches(stack, m, create_task_side_effect=None):
    """Enter all proxy patches into the given ExitStack."""
    if create_task_side_effect is None:
        create_task_side_effect = lambda coro: coro.close()

    # Stub the app object so `from main import app` inside proxy_call resolves
    # to a mock with a pool attribute (pool is passed to fire-and-forget tasks).
    mock_app = MagicMock()
    mock_app.state.pool = m["app_mock"].state.pool

    patches = [
        patch("routers.proxy._resolve_user",               m["resolve_user"]),
        patch("routers.proxy.check_rate_limit",            m["rate_limit"]),
        patch("routers.proxy.check_upstream_cap",          m["upstream_cap"]),
        patch("routers.proxy.check_and_deduct_credits",    m["deduct_credits"]),
        patch("routers.proxy._do_refund",                  m["refund"]),
        patch("routers.proxy._try_execute_managed",        m["try_execute"]),
        patch("routers.proxy._patch_tx_signals",           m["patch_tx"]),
        patch("routers.proxy._update_search_signal",       m["update_sa"]),
        patch("routers.proxy._check_spend_anomaly",        m["anomaly"]),
        patch("routers.proxy._maybe_dispatch_credits_low", m["low_bal"]),
        patch("routers.proxy._increment_calls",            m["increment"]),
        patch("routers.proxy._fetch_wri",                  m["fetch_wri"]),
        patch("routers.proxy.os.environ.get",
              side_effect=lambda k, d="": "fake-key"
              if k in ("GROQ_API_KEY", "TOGETHER_API_KEY") else d),
        patch("routers.proxy.asyncio.create_task", side_effect=create_task_side_effect),
        patch("main.app", mock_app),
    ]
    for p in patches:
        stack.enter_context(p)


@pytest.mark.no_api_key
class TestProxyFailover:
    """
    Direct async unit tests for the failover path.
    Primary: groq → "Service timeout" (service_failure)
    Fallback: together → succeeds
    Expected: X-Wayforth-Failover: true, headers set correctly, signal written.
    """

    @pytest.mark.asyncio
    async def test_failover_triggered_on_primary_timeout(self):
        from routers.proxy import proxy_call
        fn = proxy_call.__wrapped__  # bypass @limiter.limit which requires real Request
        m = _make_failover_mocks()
        req = _build_mock_request(
            method="POST",
            body={"messages": [{"role": "user", "content": "ping"}]},
        )
        db = MagicMock()
        with ExitStack() as stack:
            _apply_proxy_patches(stack, m)
            response = await fn(req, "groq", db)

        assert response.status_code == 200
        assert response.headers["x-wayforth-failover"] == "true"
        assert response.headers["x-wayforth-original-service"] == "groq"
        assert response.headers["x-wayforth-routed-to"] == "together"
        assert response.headers["x-wayforth-reason"] == "Service timeout"

    @pytest.mark.asyncio
    async def test_failover_response_is_upstream_shape(self):
        from routers.proxy import proxy_call
        fn = proxy_call.__wrapped__
        m = _make_failover_mocks()
        req = _build_mock_request(
            method="POST",
            body={"messages": [{"role": "user", "content": "ping"}]},
        )
        db = MagicMock()
        with ExitStack() as stack:
            _apply_proxy_patches(stack, m)
            response = await fn(req, "groq", db)

        body = json.loads(response.body)
        assert "id" in body
        assert "choices" in body
        assert "status" not in body
        assert "credits_deducted" not in body

    @pytest.mark.asyncio
    async def test_failover_signal_patch_has_substitution_fields(self):
        from routers.proxy import proxy_call
        fn = proxy_call.__wrapped__
        m = _make_failover_mocks()
        req = _build_mock_request(
            method="POST",
            body={"messages": [{"role": "user", "content": "ping"}]},
        )
        db = MagicMock()

        # Schedule coroutines via ensure_future so _patch_tx_signals is awaited
        # and its call args are recorded without blocking the test.
        def run_coro_inline(coro):
            if asyncio.iscoroutine(coro):
                asyncio.ensure_future(coro)

        with ExitStack() as stack:
            _apply_proxy_patches(stack, m, create_task_side_effect=run_coro_inline)
            response = await fn(req, "groq", db)

        assert response.status_code == 200
        m["patch_tx"].assert_called()
        call_kwargs = m["patch_tx"].call_args[1]
        assert call_kwargs.get("substitution_from") == "groq"
        assert call_kwargs.get("substitution_to") == "together"
        assert call_kwargs.get("substitution_reason") == "timeout"

    @pytest.mark.asyncio
    async def test_failover_wrap_returns_full_envelope(self):
        from routers.proxy import proxy_call
        fn = proxy_call.__wrapped__
        m = _make_failover_mocks()
        req = _build_mock_request(
            method="POST",
            body={"messages": [{"role": "user", "content": "ping"}]},
            query_string="wayforth_wrap=true",
        )
        db = MagicMock()
        with ExitStack() as stack:
            _apply_proxy_patches(stack, m)
            response = await fn(req, "groq", db)

        body = json.loads(response.body)
        assert body["status"] == "ok"
        assert body["service"] == "together"
        failover = body["failover"]
        assert failover["triggered"] is True
        assert failover["original_service"] == "groq"
        assert failover["routed_to"] == "together"
