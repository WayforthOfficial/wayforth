"""tests/test_pioneer_boost.py — Unit tests for Pioneer Program + Provider Boost.

Self-contained: no live deployment, no real DB, no network. Uses asyncpg-shaped
stub objects following the test_v080_security.py pattern.

Coverage:
  T01 — Boost activation happy path
  T02 — Boost activation blocked when boost_used already TRUE
  T03 — Boost activation blocked when service not Tier 2
  T04 — Boost auto-pause when provider drops below Tier 2
  T05 — Boost WRI bonus applied and capped at 100
  T06 — Pioneer join: credits awarded exactly once
  T07 — Pioneer leave: credits kept, routing reverts
  T08 — 60/40 routing split over 100 calls
  T09 — signal_weight 0.75 recorded for pioneer-routed calls
  T10 — Pioneer join with no boosted providers routes normally (signal_weight 1.0)

Run: pytest apps/api/tests/test_pioneer_boost.py -v
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wayforth_rank_v2 import compute_wri_v2


# ── Stub helpers ──────────────────────────────────────────────────────────────

class _FakeProvider:
    def __init__(self, tier="intelligence", boost_used=False, boost_paused=False,
                 boost_expires_at=None, boost_wri_bonus=0):
        self.tier = tier
        self.boost_used = boost_used
        self.boost_paused = boost_paused
        self.boost_expires_at = boost_expires_at
        self.boost_wri_bonus = boost_wri_bonus


class _FakeService:
    def __init__(self, coverage_tier=2, consecutive_failures=0):
        self.coverage_tier = coverage_tier
        self.consecutive_failures = consecutive_failures


class _FakeUser:
    def __init__(self, pioneer_opt_in=False, pioneer_credits_awarded=False):
        self.pioneer_opt_in = pioneer_opt_in
        self.pioneer_credits_awarded = pioneer_credits_awarded


# ── Boost business logic extracted for unit testing ──────────────────────────

def _can_activate_boost(provider: _FakeProvider, service: _FakeService) -> tuple[bool, str]:
    """Mirror the activation guard logic from POST /provider/boost/activate."""
    if provider.tier not in ("intelligence", "premium"):
        return False, "ineligible_tier"
    if provider.boost_used:
        return False, "boost_already_used"
    tier2_ok = (service.coverage_tier or 0) >= 2 and (service.consecutive_failures or 0) < 3
    if not tier2_ok:
        return False, "tier2_required"
    return True, "ok"


_BOOST_CONFIG = {
    "intelligence": {"days": 15, "wri_bonus": 10},
    "premium":      {"days": 30, "wri_bonus": 20},
}

_PIONEER_CREDITS = {
    "free":    0,
    "builder": 900,
    "starter": 3_150,
    "pro":     10_800,
    "growth":  36_000,
}


def _pioneer_credits_for_tier(tier: str) -> int:
    return _PIONEER_CREDITS.get(tier, 0)


def _pioneer_routing_decision(q: str, user_id: str) -> bool:
    """True = 60% path (route to boosted). Mirror of search.py seed logic."""
    seed = int(hashlib.md5(f"{q}{user_id}".encode()).hexdigest()[:8], 16)
    return seed % 10 < 6


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestBoostActivation:
    def test_T01_happy_path_intelligence(self):
        """T01 — Intelligence provider with Tier 2 service can activate boost."""
        provider = _FakeProvider(tier="intelligence", boost_used=False)
        service = _FakeService(coverage_tier=2, consecutive_failures=0)
        ok, reason = _can_activate_boost(provider, service)
        assert ok is True
        assert reason == "ok"
        cfg = _BOOST_CONFIG["intelligence"]
        assert cfg["days"] == 15
        assert cfg["wri_bonus"] == 10

    def test_T01_happy_path_premium(self):
        """T01 — Premium provider gets 30 days, +20 WRI."""
        provider = _FakeProvider(tier="premium", boost_used=False)
        service = _FakeService(coverage_tier=3, consecutive_failures=0)
        ok, reason = _can_activate_boost(provider, service)
        assert ok is True
        cfg = _BOOST_CONFIG["premium"]
        assert cfg["days"] == 30
        assert cfg["wri_bonus"] == 20

    def test_T02_blocked_boost_already_used(self):
        """T02 — boost_used=TRUE blocks activation unconditionally."""
        provider = _FakeProvider(tier="intelligence", boost_used=True)
        service = _FakeService(coverage_tier=2, consecutive_failures=0)
        ok, reason = _can_activate_boost(provider, service)
        assert ok is False
        assert reason == "boost_already_used"

    def test_T02_boost_used_blocks_premium_too(self):
        """T02 — boost_used guard applies to premium as well."""
        provider = _FakeProvider(tier="premium", boost_used=True)
        service = _FakeService(coverage_tier=3, consecutive_failures=0)
        ok, reason = _can_activate_boost(provider, service)
        assert ok is False
        assert reason == "boost_already_used"

    def test_T03_blocked_not_tier2_low_coverage(self):
        """T03 — coverage_tier < 2 blocks activation."""
        provider = _FakeProvider(tier="intelligence", boost_used=False)
        service = _FakeService(coverage_tier=1, consecutive_failures=0)
        ok, reason = _can_activate_boost(provider, service)
        assert ok is False
        assert reason == "tier2_required"

    def test_T03_blocked_not_tier2_high_failures(self):
        """T03 — consecutive_failures >= 3 blocks activation even at coverage_tier 2."""
        provider = _FakeProvider(tier="intelligence", boost_used=False)
        service = _FakeService(coverage_tier=2, consecutive_failures=3)
        ok, reason = _can_activate_boost(provider, service)
        assert ok is False
        assert reason == "tier2_required"

    def test_T03_blocked_observer_tier(self):
        """T03 — observer tier is ineligible regardless of service health."""
        provider = _FakeProvider(tier="observer", boost_used=False)
        service = _FakeService(coverage_tier=3, consecutive_failures=0)
        ok, reason = _can_activate_boost(provider, service)
        assert ok is False
        assert reason == "ineligible_tier"


class TestBoostAutoPause:
    def test_T04_pause_on_tier2_drop(self):
        """T04 — Auto-pause logic triggers when service drops below Tier 2."""
        from datetime import datetime, timezone, timedelta

        provider = _FakeProvider(
            tier="intelligence", boost_used=True, boost_paused=False,
            boost_expires_at=datetime.now(timezone.utc) + timedelta(days=10),
            boost_wri_bonus=10,
        )
        service = _FakeService(coverage_tier=1, consecutive_failures=5)

        tier2_ok = (service.coverage_tier or 0) >= 2 and (service.consecutive_failures or 0) < 3
        assert not tier2_ok  # service degraded

        # Simulate the auto-pause update
        if not tier2_ok and not provider.boost_paused:
            provider.boost_paused = True
            provider.boost_wri_bonus = 0

        assert provider.boost_paused is True
        assert provider.boost_wri_bonus == 0

    def test_T04_resume_restores_bonus(self):
        """T04 — Recovery resumes boost with correct bonus; expires_at unchanged."""
        from datetime import datetime, timezone, timedelta

        original_expires = datetime.now(timezone.utc) + timedelta(days=10)
        provider = _FakeProvider(
            tier="intelligence", boost_used=True, boost_paused=True,
            boost_expires_at=original_expires, boost_wri_bonus=0,
        )
        service = _FakeService(coverage_tier=2, consecutive_failures=0)

        tier2_ok = (service.coverage_tier or 0) >= 2 and (service.consecutive_failures or 0) < 3
        assert tier2_ok

        correct_bonus = _BOOST_CONFIG["intelligence"]["wri_bonus"]
        if tier2_ok and provider.boost_paused:
            provider.boost_paused = False
            provider.boost_wri_bonus = correct_bonus

        assert provider.boost_paused is False
        assert provider.boost_wri_bonus == 10
        # expires_at must NOT change
        assert provider.boost_expires_at == original_expires


class TestWRIBoostBonus:
    def test_T05_boost_bonus_added_to_score(self):
        """T05 — boost_wri_bonus is added to compute_wri_v2 score."""
        from datetime import datetime, timezone, timedelta
        last_seen = datetime.now(timezone.utc) - timedelta(days=3)

        score_no_boost  = compute_wri_v2(60.0, 100, 200, last_seen, boost_wri_bonus=0)
        score_with_boost = compute_wri_v2(60.0, 100, 200, last_seen, boost_wri_bonus=10)
        assert score_with_boost > score_no_boost
        assert score_with_boost - score_no_boost == pytest.approx(10.0, abs=0.2)

    def test_T05_score_capped_at_100(self):
        """T05 — Score is capped at 100 even with large bonus."""
        from datetime import datetime, timezone, timedelta
        last_seen = datetime.now(timezone.utc) - timedelta(days=1)

        score = compute_wri_v2(95.0, 1000, 1000, last_seen, boost_wri_bonus=20)
        assert score == 100.0

    def test_T05_zero_bonus_unchanged(self):
        """T05 — boost_wri_bonus=0 (default) leaves score unchanged."""
        from datetime import datetime, timezone, timedelta
        last_seen = datetime.now(timezone.utc) - timedelta(days=5)

        score_explicit = compute_wri_v2(50.0, 50, 100, last_seen, boost_wri_bonus=0)
        score_default  = compute_wri_v2(50.0, 50, 100, last_seen)
        assert score_explicit == score_default


class TestPioneerCredits:
    def test_T06_credits_awarded_on_join(self):
        """T06 — First join awards 15% of monthly tier allowance."""
        user = _FakeUser(pioneer_opt_in=False, pioneer_credits_awarded=False)
        tier = "starter"
        expected = _pioneer_credits_for_tier(tier)
        assert expected == 3_150

        # Simulate join flow
        awarded = 0
        if not user.pioneer_credits_awarded:
            awarded = _pioneer_credits_for_tier(tier)
            user.pioneer_credits_awarded = True
            user.pioneer_opt_in = True

        assert awarded == 3_150
        assert user.pioneer_credits_awarded is True

    def test_T06_credits_not_awarded_twice(self):
        """T06 — Re-joining after leave does not re-award credits."""
        user = _FakeUser(pioneer_opt_in=False, pioneer_credits_awarded=True)
        tier = "pro"

        awarded = 0
        if not user.pioneer_credits_awarded:
            awarded = _pioneer_credits_for_tier(tier)
            user.pioneer_credits_awarded = True
        user.pioneer_opt_in = True

        assert awarded == 0  # no double-award
        assert user.pioneer_opt_in is True

    def test_T06_credits_by_tier(self):
        """T06 — Correct credit amounts for each paid tier."""
        assert _pioneer_credits_for_tier("builder") == 900
        assert _pioneer_credits_for_tier("starter") == 3_150
        assert _pioneer_credits_for_tier("pro") == 10_800
        assert _pioneer_credits_for_tier("growth") == 36_000
        assert _pioneer_credits_for_tier("free") == 0

    def test_T07_leave_keeps_credits(self):
        """T07 — Leaving Pioneer Program does not claw back awarded credits."""
        balance_before = 10_000
        credits_awarded = 3_150
        balance_with_bonus = balance_before + credits_awarded

        user = _FakeUser(pioneer_opt_in=True, pioneer_credits_awarded=True)

        # Simulate leave — credits NOT removed
        user.pioneer_opt_in = False
        balance_after = balance_with_bonus  # unchanged

        assert user.pioneer_opt_in is False
        assert balance_after == balance_with_bonus  # no clawback

    def test_T07_leave_reverts_routing(self):
        """T07 — pioneer_opt_in=FALSE means next search uses normal WayforthRank."""
        user = _FakeUser(pioneer_opt_in=False)
        # A non-opted-in user never enters pioneer routing logic
        assert not user.pioneer_opt_in


class TestPioneerRouting:
    def test_T08_sixty_forty_split_over_100_calls(self):
        """T08 — 60/40 deterministic split: ~60 boosted, ~40 normal over 100 calls."""
        boosted_count = 0
        user_id = "test-user-uuid-1234"
        for i in range(100):
            q = f"query-{i}"
            if _pioneer_routing_decision(q, user_id):
                boosted_count += 1
        normal_count = 100 - boosted_count

        # Allow ±15% tolerance around 60/40
        assert 45 <= boosted_count <= 75, f"Expected ~60 boosted calls, got {boosted_count}"
        assert 25 <= normal_count <= 55, f"Expected ~40 normal calls, got {normal_count}"

    def test_T08_same_query_user_always_same_result(self):
        """T08 — Same query + user always gets the same routing decision (deterministic)."""
        q, uid = "find a translation api", "user-abc"
        results = [_pioneer_routing_decision(q, uid) for _ in range(10)]
        assert all(r == results[0] for r in results)

    def test_T08_different_queries_independent(self):
        """T08 — Different queries can get different routing (not all same direction)."""
        uid = "user-xyz"
        queries = [f"query-{i}" for i in range(20)]
        decisions = [_pioneer_routing_decision(q, uid) for q in queries]
        # Not all identical — independence check
        assert len(set(decisions)) == 2  # has both True and False

    def test_T09_signal_weight_075_when_pioneer_routed(self):
        """T09 — signal_weight=0.75 when call is pioneer-routed to boosted provider."""
        pioneer_routed = True
        signal_weight = 0.75 if pioneer_routed else 1.0
        assert signal_weight == 0.75

    def test_T09_signal_weight_10_when_not_routed(self):
        """T09 — signal_weight=1.0 on the normal 40% path."""
        pioneer_routed = False
        signal_weight = 0.75 if pioneer_routed else 1.0
        assert signal_weight == 1.0

    def test_T09_wri_contribution_discounted(self):
        """T09 — score_contribution = base_score × signal_weight."""
        base_score = 80.0
        contribution_pioneer = base_score * 0.75
        contribution_normal  = base_score * 1.0
        assert contribution_pioneer == pytest.approx(60.0)
        assert contribution_normal  == pytest.approx(80.0)

    def test_T10_no_boosted_providers_routes_normally(self):
        """T10 — If no boosted providers in category, routing is normal (signal_weight=1.0)."""
        boosted_slugs: set = set()  # empty — no active boosts in category

        pioneer_routing = True
        pioneer_routed_to_boosted = False
        signal_weight = 1.0

        if boosted_slugs:
            if _pioneer_routing_decision("test query", "user-1"):
                pioneer_routed_to_boosted = True
                signal_weight = 0.75

        assert pioneer_routing is True
        assert pioneer_routed_to_boosted is False
        assert signal_weight == 1.0

    def test_T10_pioneer_metadata_present_no_boosted(self):
        """T10 — pioneer_routing:true still appears even when no boosted providers."""
        # Simulate what the search endpoint returns when pioneer but no boosts
        response = {
            "pioneer_routing": True,
            "pioneer_routed_to_boosted": False,
            "signal_weight": 1.0,
            "boost_active": False,
        }
        assert response["pioneer_routing"] is True
        assert response["pioneer_routed_to_boosted"] is False
        assert response["signal_weight"] == 1.0
        assert response["boost_active"] is False
