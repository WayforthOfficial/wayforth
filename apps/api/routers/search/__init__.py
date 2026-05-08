"""routers/search/__init__.py — assembles combined search router."""

from fastapi import APIRouter
from .search import router as search_router
from .query import router as query_router
from .catalog import router as catalog_router
from .compare import router as compare_router

router = APIRouter()
router.include_router(search_router)
router.include_router(query_router)
router.include_router(catalog_router)
router.include_router(compare_router)
