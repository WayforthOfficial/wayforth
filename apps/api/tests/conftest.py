"""conftest.py — session-finish summary hook + availability guard."""
import httpx
import pytest
from tests.test_suite_v060 import BASE_URL, _500_errors, _forbidden_hits, _warnings


@pytest.fixture(scope="session", autouse=True)
def service_up():
    """Skip the entire suite if the live deployment is unreachable."""
    try:
        r = httpx.get(f"{BASE_URL}/status", timeout=15.0, follow_redirects=True)
        if r.status_code >= 500:
            pytest.skip(f"Service returned HTTP {r.status_code} — skipping suite")
    except Exception as exc:
        pytest.skip(f"Service unreachable ({type(exc).__name__}: {exc}) — skipping suite")


def pytest_sessionfinish(session, exitstatus):
    print("\n")
    print("═" * 62)
    print("  WAYFORTH v0.6.2  END-TO-END TEST SUITE  SUMMARY")
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
