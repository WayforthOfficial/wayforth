"""
v0.6.14 Economics regression tests + v0.7.0 version bump.

Covers:
  - x402 fee model: developer charge = provider_price * 1.015
  - SERVICE_CONFIGS: all services have real_cost_per_call defined
  - Margin check: stability AI is positive at Growth tier
  - VERSION constant
"""
import pytest

from main import VERSION
from core.credits import (
    ROUTING_FEE,
    _GROWTH_CREDIT_VALUE_USD,
    check_service_margins,
    x402_developer_charge,
)
from services.managed import SERVICE_CONFIGS


# ── TestVersion ───────────────────────────────────────────────────────────────

class TestVersion:

    def test_version_is_070(self):
        # v0.8.3 — Calibration release.
        assert VERSION == "0.8.3"

    def test_version_is_string(self):
        assert isinstance(VERSION, str)


# ── TestX402FeeModel ──────────────────────────────────────────────────────────

class TestX402FeeModel:

    def test_fee_multiplier_is_1015(self):
        # New model: developer pays provider_price * 1.015 exactly.
        charge = x402_developer_charge(1.0)
        assert charge == pytest.approx(1.015, rel=1e-6)

    def test_fee_multiplier_derived_from_routing_fee(self):
        charge = x402_developer_charge(1.0)
        assert charge == pytest.approx(1.0 * (1 + ROUTING_FEE), rel=1e-6)

    def test_developer_charge_exceeds_provider_price(self):
        charge = x402_developer_charge(0.002)
        assert charge > 0.002

    def test_developer_charge_formula(self):
        charge = x402_developer_charge(0.002)
        assert charge == pytest.approx(0.002 * 1.015, rel=1e-6)

    def test_fee_rate_is_exactly_1_5_percent(self):
        provider_price = 1.0
        charge = x402_developer_charge(provider_price)
        fee_rate = (charge - provider_price) / provider_price
        assert fee_rate == pytest.approx(0.015, rel=1e-6)

    def test_fee_is_positive(self):
        charge = x402_developer_charge(0.002)
        assert charge - 0.002 > 0

    def test_provider_receives_full_stated_price(self):
        provider_price = 0.002
        charge = x402_developer_charge(provider_price)
        # developer pays `charge`; Wayforth forwards `provider_price` to provider
        wayforth_fee = charge - provider_price
        assert wayforth_fee > 0
        assert provider_price + wayforth_fee == pytest.approx(charge, rel=1e-6)

    @pytest.mark.parametrize("provider_price,expected", [
        (0.002,  0.002 * 1.015),
        (0.010,  0.010 * 1.015),
        (1.000,  1.000 * 1.015),
    ])
    def test_developer_charge_exact(self, provider_price, expected):
        charge = x402_developer_charge(provider_price)
        assert charge == pytest.approx(expected, rel=1e-6)

    def test_routing_fee_constant_is_1_5_percent(self):
        assert ROUTING_FEE == pytest.approx(0.015)


# ── TestServiceApiCost ────────────────────────────────────────────────────────

class TestServiceApiCost:

    def test_all_services_have_real_cost_field(self):
        for slug, cfg in SERVICE_CONFIGS.items():
            assert "real_cost_per_call" in cfg, f"{slug} missing real_cost_per_call"

    def test_all_real_costs_are_positive(self):
        for slug, cfg in SERVICE_CONFIGS.items():
            # Subscription-based services (e.g. deepl) legitimately have 0 per-call cost.
            assert cfg["real_cost_per_call"] >= 0, f"{slug} real_cost_per_call must be >= 0"

    def test_stability_api_cost(self):
        assert SERVICE_CONFIGS["stability"]["real_cost_per_call"] == pytest.approx(0.08)

    def test_elevenlabs_is_most_expensive(self):
        costs = {s: c["real_cost_per_call"] for s, c in SERVICE_CONFIGS.items()}
        assert costs["elevenlabs"] == max(costs.values())

    @pytest.mark.parametrize("slug,expected", [
        ("groq",       0.001),
        ("deepl",      0.0),
        ("stability",  0.08),
        ("elevenlabs", 0.150),
    ])
    def test_spot_check_api_costs(self, slug, expected):
        assert SERVICE_CONFIGS[slug]["real_cost_per_call"] == pytest.approx(expected)


# ── TestServiceMargins ────────────────────────────────────────────────────────

class TestServiceMargins:

    def test_growth_credit_value_constant(self):
        assert _GROWTH_CREDIT_VALUE_USD == pytest.approx(0.001246, rel=1e-3)

    def test_stability_margin_is_positive_at_growth(self):
        cfg = SERVICE_CONFIGS["stability"]
        margin = cfg["credits"] * _GROWTH_CREDIT_VALUE_USD - cfg["real_cost_per_call"]
        # 65 × 0.001246 − 0.065 = 0.08099 − 0.065 = 0.01599
        assert margin > 0, f"stability margin {margin:.6f} must be positive"

    def test_stability_margin_at_growth_value(self):
        cfg = SERVICE_CONFIGS["stability"]
        margin = cfg["credits"] * _GROWTH_CREDIT_VALUE_USD - cfg["real_cost_per_call"]
        # 86 credits * 0.001246 - 0.08 = 0.107156 - 0.08 = 0.027156
        assert margin == pytest.approx(0.027156, rel=1e-2)

    def test_stability_margin_exceeds_alert_threshold(self):
        cfg = SERVICE_CONFIGS["stability"]
        margin = cfg["credits"] * _GROWTH_CREDIT_VALUE_USD - cfg["real_cost_per_call"]
        assert margin >= 0.005, f"stability margin {margin:.6f} below $0.005 alert threshold"

    def test_check_service_margins_runs_without_error(self):
        check_service_margins()

    def test_check_margins_warns_on_unprofitable_service(self):
        from unittest.mock import patch
        unprofitable = {
            "bad-svc": {"key_var": "X", "credits": 1, "real_cost_per_call": 1.0}
        }
        with patch("services.managed.SERVICE_CONFIGS", unprofitable):
            with patch("core.credits.logger") as mock_log:
                check_service_margins()
                mock_log.warning.assert_called_once()
                assert "MARGIN ALERT" in mock_log.warning.call_args[0][0]

    def test_check_margins_no_warn_for_profitable_service(self):
        from unittest.mock import patch
        profitable = {
            "good-svc": {"key_var": "X", "credits": 100, "real_cost_per_call": 0.001}
        }
        with patch("services.managed.SERVICE_CONFIGS", profitable):
            with patch("core.credits.logger") as mock_log:
                check_service_margins()
                mock_log.warning.assert_not_called()
