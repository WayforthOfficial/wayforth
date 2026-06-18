"""conftest.py — session-finish summary hook + availability guard."""
import os

import httpx
import pytest
from tests.test_suite_v060 import API_KEY, BASE_URL, _500_errors, _forbidden_hits, _warnings


# Recurrence guard (2026-06): the cloud E2E suite CREATES hosted_agents/agent_runs.
# Run against the production gateway it pollutes prod and burns real E2B sandboxes
# (this is what seeded the demo agents + 10k runs on a real account). These modules
# must target a non-prod WAYFORTH_TEST_BASE_URL; running them at prod is refused
# unless WAYFORTH_ALLOW_PROD_WRITES=true is set deliberately.
_PROD_HOSTS = ("gateway.wayforth.io",)
_CLOUD_WRITE_MODULES = ("test_cloud_idor.py",)


_LIVE_TEST_MODULES = (
    "test_suite_v060.py", "test_suite_v062.py", "test_suite_v0610.py",
    "test_security_v063.py", "test_v0614.py", "test_email.py", "test_mfa.py",
    "test_refund.py",
)


@pytest.fixture(scope="session", autouse=True)
def service_up(request):
    """Skip the entire suite if the live deployment is unreachable.

    Only applies when running live-integration test modules. Pure-unit tests
    (test_body_size_limit.py, test_tier1_caps.py, etc.) do not need a live
    deployment and are not gated here.
    """
    # Check whether any collected item belongs to a live-test module.
    items = request.session.items
    if not any(
        any(m in str(item.fspath) for m in _LIVE_TEST_MODULES)
        for item in items
    ):
        return  # No live tests requested — skip the gateway probe.
    try:
        r = httpx.get(f"{BASE_URL}/status", timeout=15.0, follow_redirects=True)
        if r.status_code >= 500:
            pytest.skip(f"Service returned HTTP {r.status_code} — skipping suite")
    except Exception as exc:
        pytest.skip(f"Service unreachable ({type(exc).__name__}: {exc}) — skipping suite")


@pytest.fixture(autouse=True)
def _global_requires_api_key(request):
    """Skip auth-required tests across all test modules when WAYFORTH_TEST_API_KEY is unset.

    Tests that explicitly probe behavior without a key (e.g. "no key → 401") should
    opt out with @pytest.mark.no_api_key. The v063 security suite uses this marker
    on tests that intentionally verify unauthenticated 401 responses.
    """
    if API_KEY:
        return
    markers = {m.name for m in request.node.iter_markers()}
    if "no_api_key" in markers:
        return
    # Skip if the test module is one of the auth-required suites.
    module_path = str(request.node.fspath)
    auth_required_modules = (
        "test_suite_v060.py", "test_suite_v062.py", "test_suite_v0610.py",
        "test_suite_v052.py", "test_refund.py",
    )
    if any(m in module_path for m in auth_required_modules):
        pytest.skip("WAYFORTH_TEST_API_KEY not set — skipping auth-required test")


@pytest.fixture(autouse=True)
def _no_cloud_writes_against_prod(request):
    """Refuse to run cloud-write E2E modules against the production gateway.

    The cloud E2E suite creates hosted agents + dispatches real runs. Pointed at
    prod it writes demo data to real accounts and burns E2B sandboxes. Require a
    non-prod WAYFORTH_TEST_BASE_URL (override only with WAYFORTH_ALLOW_PROD_WRITES=true)."""
    module_path = str(request.node.fspath)
    if not any(m in module_path for m in _CLOUD_WRITE_MODULES):
        return
    hits_prod = any(h in BASE_URL for h in _PROD_HOSTS)
    allowed = os.environ.get("WAYFORTH_ALLOW_PROD_WRITES", "").lower() == "true"
    if hits_prod and not allowed:
        pytest.skip(
            f"Refusing cloud-write E2E against production ({BASE_URL}). Set "
            "WAYFORTH_TEST_BASE_URL to a non-prod gateway "
            "(or WAYFORTH_ALLOW_PROD_WRITES=true to override deliberately)."
        )


def pytest_sessionfinish(session, exitstatus):
    print("\n")
    print("═" * 62)
    print("  WAYFORTH v0.6.14  317 TESTS  SUMMARY")
    print("═" * 62)

    passed = session.testscollected - session.testsfailed - getattr(session, "testsskipped", 0)
    print(f"\n  ✅  PASSED  : {passed}")
    print(f"  ❌  FAILED  : {session.testsfailed}")
    print(f"  ⚠️   WARNINGS: {len(_warnings)}")
    print(f"  💥  500s    : {len(_500_errors)}")
    print(f"  🚫  FORBIDDEN FIELDS: {len(_forbidden_hits)}")

    if _500_errors:
        print("\n  💥 500 ERRORS:")
        for e in _500_errors:
            print(f"     {e['method']:6s} {e['url']}")
            print(f"            {e['body_preview'][:100]}")

    if _forbidden_hits:
        print("\n  🚫 FORBIDDEN FIELDS FOUND:")
        for url, field, val in _forbidden_hits:
            print(f"     [{field}] at {url}")
            print(f"       value = {val}")

    if _warnings:
        print("\n  ⚠️  WARNINGS:")
        for w in _warnings:
            print(f"     {w}")

    print("\n" + "═" * 62 + "\n")
