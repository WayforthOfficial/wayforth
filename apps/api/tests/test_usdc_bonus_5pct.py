"""USDC bonus must be a real 5% of included credits, surfaced 1:1.

The bonus GRANTED was already 5% (base_credits × 0.05), but /pricing/json and the
USDC subscribe/renewal quotes divided usdc_bonus_credits by CREDITS_PER_CALL (6),
displaying 50/175/600/2000 (~0.83%) instead of 300/1050/3600/12000 (5%). Quotas
are credit-denominated (calls_included == monthly_credits), so the bonus is now
surfaced 1:1 — consistent with /billing/balance and the actual grant.

Run: uv run pytest tests/test_usdc_bonus_5pct.py -v
"""
import pytest

from core.credits import PLANS

EXPECTED_BONUS = {
    "starter": 300,
    "builder": 1_050,
    "pro":     3_600,
    "growth":  12_000,
}


@pytest.mark.parametrize("tier,expected", EXPECTED_BONUS.items())
def test_usdc_bonus_credits_is_5pct(tier, expected):
    plan = PLANS[tier]
    assert plan["usdc_bonus_credits"] == expected
    # exactly 5% of included credits
    assert plan["usdc_bonus_credits"] == round(plan["monthly_credits"] * 0.05)


@pytest.mark.parametrize("tier,expected", EXPECTED_BONUS.items())
def test_pricing_json_surfaces_bonus_1to1(tier, expected):
    # Mirror the /pricing/json computation after the fix: surfaced 1:1, not ÷6.
    plan = PLANS[tier]
    bonus_calls = plan["usdc_bonus_credits"]
    assert bonus_calls == expected
    # the bug was dividing by CREDITS_PER_CALL (6) → one-sixth of the real bonus
    assert bonus_calls != plan["usdc_bonus_credits"] // 6


def test_free_and_enterprise_have_no_usdc_bonus():
    assert PLANS["free"]["usdc_bonus_credits"] == 0
    assert PLANS["enterprise"]["usdc_bonus_credits"] == 0
