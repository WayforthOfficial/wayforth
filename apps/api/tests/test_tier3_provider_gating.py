"""Tier 3 provider-gating — eligibility, package ordering, status mapping.

Tier 3 application is provider-only and requires (a) the domain-verified flag and
(b) a provider package of Intelligence or above. These are pure-function tests of
the gating helpers used by POST/GET /provider/tier3/*.

Run: uv run pytest tests/test_tier3_provider_gating.py -v
"""
from routers.provider import (
    _TIER3_MIN_PACKAGE,
    _provider_package_rank,
    _tier3_application_status,
    _tier3_eligibility,
)


def test_min_package_is_intelligence():
    assert _TIER3_MIN_PACKAGE == "intelligence"


def test_package_ordering():
    assert (_provider_package_rank("observer")
            < _provider_package_rank("intelligence")
            < _provider_package_rank("premium"))


def test_package_rank_defaults_to_observer():
    assert _provider_package_rank(None) == _provider_package_rank("observer") == 0
    assert _provider_package_rank("not-a-tier") == 0
    assert _provider_package_rank("PREMIUM") == _provider_package_rank("premium")  # case-insensitive


def test_eligibility_not_verified_blocks_even_premium():
    assert _tier3_eligibility({"verified": False, "tier": "premium"}) == (False, "not_verified")


def test_eligibility_observer_package_too_low():
    assert _tier3_eligibility({"verified": True, "tier": "observer"}) == (False, "package_too_low")


def test_eligibility_intelligence_ok():
    assert _tier3_eligibility({"verified": True, "tier": "intelligence"}) == (True, "ok")


def test_eligibility_premium_ok():
    assert _tier3_eligibility({"verified": True, "tier": "premium"}) == (True, "ok")


def test_application_status_mapping():
    assert _tier3_application_status(None) == "none"
    assert _tier3_application_status("pending") == "pending"
    assert _tier3_application_status("in_review") == "pending"
    assert _tier3_application_status("approved") == "approved"
    assert _tier3_application_status("rejected") == "rejected"
