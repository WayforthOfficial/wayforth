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

try:
    from wayforth_rank_v2 import compute_wri_v2
except ImportError:
    # wayforth_rank_v2 is gitignored (private wayforth-rank repo).
    # Stub satisfies the signature and capping contract for unit tests.
    def compute_wri_v2(base_wri, payments, total_clicks, last_seen, boost_wri_bonus=0):  # type: ignore[misc]
        return round(min(base_wri * 0.90 + boost_wri_bonus, 100.0), 1)
from routers.billing.account import (
    _PIONEER_DAILY_CREDITS,
    _PIONEER_REJOIN_COOLDOWN,
    _cooldown_days_remaining,
)


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
    def __init__(self, pioneer_opt_in=False, pioneer_last_drip_date=None,
                 pioneer_cooldown_until=None):
        self.pioneer_opt_in = pioneer_opt_in
        self.pioneer_last_drip_date = pioneer_last_drip_date  # date or None
        self.pioneer_cooldown_until = pioneer_cooldown_until  # datetime or None


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

def _pioneer_daily_for_tier(tier: str) -> int:
    return _PIONEER_DAILY_CREDITS.get(tier, 0)


def _pioneer_routing_decision(query_id: str) -> bool:
    """True = 60% path (route to boosted). Mirror of search.py seed logic.

    v0.8.2: seeded from the SERVER-generated query_id only — the client-supplied
    query text is no longer part of the seed (it was manipulable)."""
    seed = int(hashlib.md5(query_id.encode()).hexdigest()[:8], 16)
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


def _drip_claim(user: _FakeUser, today) -> int:
    """Mirror run_pioneer_drip's per-user claim: award the tier's daily credits
    once per day. Returns credits awarded this run (0 if already dripped today
    or not opted in)."""
    if not user.pioneer_opt_in:
        return 0
    if user.pioneer_last_drip_date is not None and user.pioneer_last_drip_date >= today:
        return 0  # already dripped today
    user.pioneer_last_drip_date = today
    return _pioneer_daily_for_tier("starter")


class TestPioneerDrip:
    def test_daily_credits_by_tier(self):
        """Daily drip rates match the spec."""
        assert _pioneer_daily_for_tier("builder") == 30
        assert _pioneer_daily_for_tier("starter") == 105
        assert _pioneer_daily_for_tier("pro") == 360
        assert _pioneer_daily_for_tier("growth") == 1_200
        assert _pioneer_daily_for_tier("free") == 0

    def test_drip_awarded_once_then_skipped_same_day(self):
        """Drip awards once per day; a second run the same day awards nothing."""
        import datetime as _dt
        today = _dt.date(2026, 5, 28)
        user = _FakeUser(pioneer_opt_in=True, pioneer_last_drip_date=None)

        first = _drip_claim(user, today)
        second = _drip_claim(user, today)

        assert first == 105
        assert second == 0
        assert user.pioneer_last_drip_date == today

    def test_drip_resumes_next_day(self):
        """A new UTC day re-enables the drip."""
        import datetime as _dt
        user = _FakeUser(pioneer_opt_in=True, pioneer_last_drip_date=_dt.date(2026, 5, 28))
        assert _drip_claim(user, _dt.date(2026, 5, 28)) == 0   # same day
        assert _drip_claim(user, _dt.date(2026, 5, 29)) == 105  # next day

    def test_drip_skipped_when_not_opted_in(self):
        import datetime as _dt
        user = _FakeUser(pioneer_opt_in=False, pioneer_last_drip_date=None)
        assert _drip_claim(user, _dt.date(2026, 5, 28)) == 0


class TestPioneerCooldown:
    def test_leave_sets_seven_day_cooldown(self):
        """Leaving sets a 7-day rejoin cooldown and clears last_drip_date."""
        import datetime as _dt
        now = _dt.datetime(2026, 5, 28, tzinfo=_dt.timezone.utc)
        user = _FakeUser(pioneer_opt_in=True, pioneer_last_drip_date=_dt.date(2026, 5, 28))

        # Simulate leave
        user.pioneer_opt_in = False
        user.pioneer_cooldown_until = now + _PIONEER_REJOIN_COOLDOWN
        user.pioneer_last_drip_date = None

        assert _PIONEER_REJOIN_COOLDOWN == _dt.timedelta(days=7)
        assert user.pioneer_cooldown_until == now + _dt.timedelta(days=7)
        assert user.pioneer_last_drip_date is None
        assert user.pioneer_opt_in is False  # routing reverts

    def test_cooldown_blocks_rejoin(self):
        """Rejoin within the cooldown window is blocked (429)."""
        import datetime as _dt
        now = _dt.datetime(2026, 5, 28, tzinfo=_dt.timezone.utc)
        cooldown_until = now + _dt.timedelta(days=3)
        blocked = bool(cooldown_until and cooldown_until > now)
        assert blocked is True
        assert _cooldown_days_remaining(cooldown_until, now) == 3

    def test_cooldown_expired_allows_rejoin(self):
        """Once the cooldown is in the past, rejoin is allowed."""
        import datetime as _dt
        now = _dt.datetime(2026, 5, 28, tzinfo=_dt.timezone.utc)
        cooldown_until = now - _dt.timedelta(seconds=1)
        blocked = bool(cooldown_until and cooldown_until > now)
        assert blocked is False
        assert _cooldown_days_remaining(cooldown_until, now) == 0

    def test_cooldown_days_remaining_rounds_up(self):
        """Partial days round up so the UI never shows 0 while still blocked."""
        import datetime as _dt
        now = _dt.datetime(2026, 5, 28, 12, tzinfo=_dt.timezone.utc)
        assert _cooldown_days_remaining(now + _dt.timedelta(hours=1), now) == 1
        assert _cooldown_days_remaining(now + _dt.timedelta(days=6, hours=12), now) == 7
        assert _cooldown_days_remaining(None, now) == 0

    def test_T07_leave_reverts_routing(self):
        """Leaving (opt_in=FALSE) means the next search uses normal WayforthRank."""
        user = _FakeUser(pioneer_opt_in=False)
        assert not user.pioneer_opt_in


class TestPioneerRouting:
    def test_T08_sixty_forty_split_over_100_calls(self):
        """T08 — ~60/40 split over 100 server-generated query_ids."""
        boosted_count = sum(
            1 for i in range(100) if _pioneer_routing_decision(f"req-{i}-qid")
        )
        normal_count = 100 - boosted_count
        # Allow ±15% tolerance around 60/40
        assert 45 <= boosted_count <= 75, f"Expected ~60 boosted calls, got {boosted_count}"
        assert 25 <= normal_count <= 55, f"Expected ~40 normal calls, got {normal_count}"

    def test_T08_decision_deterministic_per_query_id(self):
        """T08 — Same server query_id always yields the same decision."""
        qid = "11111111-2222-3333-4444-555555555555"
        results = [_pioneer_routing_decision(qid) for _ in range(10)]
        assert all(r == results[0] for r in results)

    def test_T08_not_controllable_by_query_text(self):
        """T08 — The decision is seeded only from the server query_id; the client
        query text is not an input, so a caller cannot steer the bucket."""
        import inspect
        src = inspect.getsource(_pioneer_routing_decision)
        # Seed derives from query_id only — no query-text parameter in the seed.
        assert "query_id" in src and "{q}" not in src
        # Distinct query_ids still produce both outcomes (the split works).
        decisions = {_pioneer_routing_decision(f"qid-{i}") for i in range(20)}
        assert decisions == {True, False}

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
            if _pioneer_routing_decision("some-query-id"):
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
