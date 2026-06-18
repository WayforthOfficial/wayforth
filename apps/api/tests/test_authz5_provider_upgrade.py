"""AUTHZ-5 regression — a provider must not self-upgrade to a paid tier for free.

The billing/upgrade handler has a "mock mode" branch that sets tier directly with
no payment. Before the fix it fired in production whenever a single
STRIPE_PRICE_PROVIDER_* env was unset (or Stripe was test/empty), handing out free
Premium. The decision is now gated to non-production via
_block_free_provider_upgrade().

Run: uv run pytest tests/test_authz5_provider_upgrade.py -v
"""
from routers.provider import _block_free_provider_upgrade


def test_production_missing_price_is_blocked():
    # The exact exploit: prod, live Stripe key (mock=False), but price env unset.
    assert _block_free_provider_upgrade(is_prod=True, price_id="", stripe_mock=False) is True


def test_production_stripe_mock_is_blocked():
    assert _block_free_provider_upgrade(is_prod=True, price_id="price_123", stripe_mock=True) is True


def test_production_fully_configured_is_allowed():
    # Real price + real Stripe in prod → proceed to real checkout (not blocked).
    assert _block_free_provider_upgrade(is_prod=True, price_id="price_123", stripe_mock=False) is False


def test_non_production_mock_upgrade_still_allowed():
    # Dev/staging keep the convenient mock upgrade for testing.
    assert _block_free_provider_upgrade(is_prod=False, price_id="", stripe_mock=True) is False
    assert _block_free_provider_upgrade(is_prod=False, price_id="price_123", stripe_mock=False) is False
