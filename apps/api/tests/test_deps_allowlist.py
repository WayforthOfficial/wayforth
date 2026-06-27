"""test_deps_allowlist.py — GET /cloud/deps/allowlist payload (requirements picker source).

The load-bearing assertion: HASHES NEVER REACH THE CLIENT — the picker gets only package
names + installable versions. Tests the exact transform the endpoint serves.
"""
import json

from routers.cloud import _allowlist_payload
from services.agent_deps import MAX_DIRECT_DEPS, load_lockfile


def test_payload_shape():
    p = _allowlist_payload()
    assert set(p) == {"packages", "max_direct_deps"}
    assert p["max_direct_deps"] == MAX_DIRECT_DEPS
    assert isinstance(p["packages"], dict) and p["packages"]          # non-empty


def test_no_hashes_reach_the_client():
    blob = json.dumps(_allowlist_payload())
    assert "sha256" not in blob                                       # not a single hash
    # every value is a list of version strings — never the {version: [hashes]} sub-dict
    for name, versions in _allowlist_payload()["packages"].items():
        assert isinstance(versions, list)
        assert all(isinstance(v, str) and "sha256" not in v for v in versions)


def test_names_and_versions_match_the_lockfile():
    lock = load_lockfile()
    pkgs = _allowlist_payload()["packages"]
    assert set(pkgs) == set(lock)                                     # same package names
    for name, versions in pkgs.items():
        assert versions == sorted(lock[name].keys())                 # same versions, sorted
