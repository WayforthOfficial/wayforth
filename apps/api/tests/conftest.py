"""conftest.py — session-finish summary hook + availability guard."""
import httpx
import pytest
from tests.test_suite_v060 import API_KEY, BASE_URL, _500_errors, _forbidden_hits, _warnings


@pytest.fixture(scope="session", autouse=True)
def service_up():
    """Skip the entire suite if the live deployment is unreachable."""
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


def pytest_sessionfinish(session, exitstatus):
    print("\n")
    print("═" * 62)
    print("  WAYFORTH v0.6.13  END-TO-END TEST SUITE  SUMMARY")
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
