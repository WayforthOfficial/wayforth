"""routers/billing/__init__.py — assembles combined billing router, re-exports background task funcs."""

from fastapi import APIRouter
from .usdc import router as usdc_router
from .stripe import router as stripe_router
from .account import router as account_router
from .analytics_billing import router as analytics_router
from .favorites import router as favorites_router
from .referrals import router as referrals_router

# Re-exports for main.py compatibility
from .usdc import _usdc_payment_watcher, _usdc_renewal_reminder, _activate_usdc_subscription
from .account import _credits_to_tier

router = APIRouter()
router.include_router(usdc_router)
router.include_router(stripe_router)
router.include_router(account_router)
router.include_router(analytics_router)
router.include_router(favorites_router)
router.include_router(referrals_router)
