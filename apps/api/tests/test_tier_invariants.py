"""
tests/test_tier_invariants.py — Tier binding regression tests.

Guards the invariant: {key -> label, price_usd, price_cents, drip, agent_limit,
credits/mo, RPM, annual_price} for the two developer tiers so a future
mis-binding (e.g. swapping values instead of keys) fails CI immediately.

Also optionally verifies the Stripe test-mode price objects when
STRIPE_SECRET_KEY is set, confirming the key -> Stripe amount binding.
"""
import os
import pytest

from core.credits import PLANS, STRIPE_PACKAGES, PLAN_ANNUAL_DETAILS, _PLAN_ANNUAL_PRICE_ENV
from core.tier_gates import TIER_FEATURES, HOSTED_AGENT_LIMITS, CONCURRENT_RUNS_PER_USER, TIER_RATE_LIMITS
from routers.billing.account import _PIONEER_DAILY_CREDITS


# ── Expected end state ────────────────────────────────────────────────────────

_EXPECT = {
    "starter": {
        "label":            "Starter",
        "price_usd":        12,
        "price_cents":      1200,
        "monthly_credits":  6_000,
        "drip_per_day":     30,
        "agent_limit":      3,
        "concurrent_runs":  1,
        "rpm":              120,
        "annual_price_usd": 99.0,
        "stripe_price_env": "STRIPE_PRICE_STARTER",
        "annual_price_env": "STRIPE_PRICE_STARTER_ANNUAL",
        # Features: compare/analytics/wayforthql should NOT be included
        "has_compare":      False,
        "has_analytics":    False,
    },
    "builder": {
        "label":            "Builder",
        "price_usd":        29,
        "price_cents":      2900,
        "monthly_credits":  21_000,
        "drip_per_day":     105,
        "agent_limit":      5,
        "concurrent_runs":  2,
        "rpm":              300,
        "annual_price_usd": 290.0,
        "stripe_price_env": "STRIPE_PRICE_BUILDER",
        "annual_price_env": "STRIPE_PRICE_BUILDER_ANNUAL",
        # Features: compare/analytics/wayforthql SHOULD be included
        "has_compare":      True,
        "has_analytics":    True,
    },
}


@pytest.mark.parametrize("tier", ["starter", "builder"])
class TestTierInvariants:

    def test_plans_price_usd(self, tier):
        assert PLANS[tier]["price_usd"] == _EXPECT[tier]["price_usd"], \
            f"PLANS['{tier}']['price_usd'] wrong"

    def test_plans_monthly_credits(self, tier):
        assert PLANS[tier]["monthly_credits"] == _EXPECT[tier]["monthly_credits"], \
            f"PLANS['{tier}']['monthly_credits'] wrong"

    def test_plans_stripe_price_env(self, tier):
        assert PLANS[tier]["stripe_price_env"] == _EXPECT[tier]["stripe_price_env"], \
            f"PLANS['{tier}']['stripe_price_env'] wrong — env var binding mismatch"

    def test_stripe_packages_price_cents(self, tier):
        assert STRIPE_PACKAGES[tier]["price_cents"] == _EXPECT[tier]["price_cents"], \
            f"STRIPE_PACKAGES['{tier}']['price_cents'] wrong"

    def test_stripe_packages_label(self, tier):
        assert STRIPE_PACKAGES[tier]["label"] == _EXPECT[tier]["label"], \
            f"STRIPE_PACKAGES['{tier}']['label'] wrong — label/key mismatch"

    def test_stripe_packages_price_id_env(self, tier):
        expected_env = _EXPECT[tier]["stripe_price_env"]
        actual = STRIPE_PACKAGES[tier]["price_id"]
        env_val = os.environ.get(expected_env, "")
        assert actual == env_val, \
            f"STRIPE_PACKAGES['{tier}']['price_id'] != os.environ['{expected_env}']"

    def test_pioneer_drip(self, tier):
        assert _PIONEER_DAILY_CREDITS[tier] == _EXPECT[tier]["drip_per_day"], \
            f"_PIONEER_DAILY_CREDITS['{tier}'] wrong"

    def test_hosted_agent_limit(self, tier):
        assert HOSTED_AGENT_LIMITS[tier] == _EXPECT[tier]["agent_limit"], \
            f"HOSTED_AGENT_LIMITS['{tier}'] wrong"

    def test_concurrent_runs(self, tier):
        assert CONCURRENT_RUNS_PER_USER[tier] == _EXPECT[tier]["concurrent_runs"], \
            f"CONCURRENT_RUNS_PER_USER['{tier}'] wrong"

    def test_rate_limit_rpm(self, tier):
        assert TIER_RATE_LIMITS[tier]["calls_per_minute"] == _EXPECT[tier]["rpm"], \
            f"TIER_RATE_LIMITS['{tier}']['calls_per_minute'] wrong"

    def test_annual_price(self, tier):
        assert PLAN_ANNUAL_DETAILS[tier]["price_usd_annual"] == _EXPECT[tier]["annual_price_usd"], \
            f"PLAN_ANNUAL_DETAILS['{tier}']['price_usd_annual'] wrong"

    def test_annual_price_env(self, tier):
        assert _PLAN_ANNUAL_PRICE_ENV[tier] == _EXPECT[tier]["annual_price_env"], \
            f"_PLAN_ANNUAL_PRICE_ENV['{tier}'] wrong — annual env var binding mismatch"

    def test_compare_feature_gate(self, tier):
        in_compare = tier in TIER_FEATURES.get("compare", [])
        assert in_compare == _EXPECT[tier]["has_compare"], \
            f"'compare' gate for tier '{tier}' wrong (in_gate={in_compare})"

    def test_analytics_feature_gate(self, tier):
        in_analytics = tier in TIER_FEATURES.get("analytics", [])
        assert in_analytics == _EXPECT[tier]["has_analytics"], \
            f"'analytics' gate for tier '{tier}' wrong (in_gate={in_analytics})"

    def test_cloud_agents_gate_open(self, tier):
        assert tier in TIER_FEATURES.get("cloud_agents", []), \
            f"'{tier}' must be in cloud_agents gate"


# ── Stripe live price verification (skipped if no key) ────────────────────────

_STRIPE_KEY = os.environ.get("STRIPE_SECRET_KEY", "")


@pytest.mark.skipif(not _STRIPE_KEY, reason="STRIPE_SECRET_KEY not set — skipping live price check")
@pytest.mark.parametrize("tier,expected_amount_cents", [
    ("starter", 1200),
    ("builder", 2900),
])
def test_stripe_price_amount_matches_tier(tier, expected_amount_cents):
    """Resolve the actual Stripe price object and assert the unit_amount matches."""
    import stripe as _stripe
    _stripe.api_key = _STRIPE_KEY
    env_var = _EXPECT[tier]["stripe_price_env"]
    price_id = os.environ.get(env_var, "")
    if not price_id:
        pytest.skip(f"{env_var} not set — cannot verify live price amount")
    price = _stripe.Price.retrieve(price_id)
    assert price["unit_amount"] == expected_amount_cents, (
        f"Stripe price {price_id} (key '{tier}', env {env_var}) "
        f"has unit_amount={price['unit_amount']} but expected {expected_amount_cents}. "
        f"Env var binding is WRONG — check STRIPE_PRICE_STARTER vs STRIPE_PRICE_BUILDER."
    )
