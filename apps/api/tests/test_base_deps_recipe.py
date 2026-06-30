"""test_base_deps_recipe.py — the base-image recipe + drift detector (Step: flip-readiness).

Pure functions shared by scripts/build_base_image.py (bakes FROM BASE_DEPS) and
scripts/deps_live_proof.py (asserts the WIRED image == BASE_DEPS), so "proof passed" and
"wired image correct" can't drift.
"""
from services.agent_deps import (
    BASE_DEPS, base_deps_lock, base_deps_match, base_deps_pinned, diff_against_base_deps,
)

# The ACTUAL pip freeze captured from the live wayforth-agent-base-v1 template (read-only
# inspection). The drift assertion must PASS on this — the wired image is currently correct.
CURRENT_TEMPLATE_FREEZE = """\
anyio==4.14.1
certifi==2026.6.17
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
idna==3.18
pip==23.2.1
setuptools==65.5.1
typing_extensions==4.15.0
wayforth-sdk==0.9.0
wheel==0.42.0
"""


def test_pinned_has_the_critical_closure():
    p = base_deps_pinned()
    # the lazy-import deps #61 caught + the SDK/http client
    for name in ("httpcore", "typing-extensions", "wayforth-sdk", "httpx"):
        assert name in p, f"{name} missing from BASE_DEPS"
    assert len(p) == len(BASE_DEPS)


def test_current_template_matches_base_deps():
    # underscore typing_extensions normalizes to typing-extensions; pip/setuptools/wheel ignored
    assert diff_against_base_deps(CURRENT_TEMPLATE_FREEZE) == {"missing": [], "mismatch": [], "extra": []}
    assert base_deps_match(CURRENT_TEMPLATE_FREEZE) is True


def test_detects_missing_dep():
    freeze = "\n".join(l for l in CURRENT_TEMPLATE_FREEZE.splitlines() if "httpcore" not in l)
    d = diff_against_base_deps(freeze)
    assert d["missing"] == ["httpcore"]
    assert base_deps_match(freeze) is False


def test_detects_version_mismatch():
    freeze = CURRENT_TEMPLATE_FREEZE.replace("wayforth-sdk==0.9.0", "wayforth-sdk==0.8.0")
    d = diff_against_base_deps(freeze)
    assert d["mismatch"] == [("wayforth-sdk", "0.9.0", "0.8.0")]
    assert base_deps_match(freeze) is False


def test_detects_unexpected_extra():
    freeze = CURRENT_TEMPLATE_FREEZE + "requests==2.32.0\n"   # not in the closure
    d = diff_against_base_deps(freeze)
    assert d["extra"] == ["requests"]
    assert base_deps_match(freeze) is False


def test_base_deps_lock_is_hashed_and_covers_closure():
    lock = base_deps_lock()
    assert "httpcore==1.0.9" in lock and "--hash=sha256:" in lock
    assert "typing-extensions==4.15.0" in lock
    # one line per BASE_DEPS entry, every line hashed
    lines = [l for l in lock.splitlines() if l.strip()]
    assert len(lines) == len(BASE_DEPS)
    assert all("--hash=sha256:" in l for l in lines)
