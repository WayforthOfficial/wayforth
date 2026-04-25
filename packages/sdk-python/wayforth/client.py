import httpx
from typing import TypedDict


class SearchResult(TypedDict, total=False):
    name: str
    score: int
    reason: str
    wayforth_id: str
    wri: float
    category: str
    endpoint_url: str
    pricing_usdc: float
    payment_protocol: str
    service_id: str
    coverage_tier: int


class WayforthClient:
    def __init__(self, base_url: str = "https://api-production-fd71.up.railway.app"):
        self._base_url = base_url.rstrip("/")

    def search(self, intent: str, category: str = None, limit: int = 5) -> list[SearchResult]:
        services = self._get_services(category=category)
        tokens = intent.lower().split()

        def _score(svc: dict) -> int:
            haystack = f"{svc.get('name', '')} {svc.get('description', '')}".lower()
            return sum(1 for t in tokens if t in haystack)

        return sorted(services, key=_score, reverse=True)[:limit]

    def list_services(
        self, category: str = None, tier: int = None, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        return self._get_services(category=category, tier=tier, limit=limit, offset=offset)

    def get_service(self, id: str) -> dict | None:
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.get(f"{self._base_url}/services/{id}")
                if r.status_code == 404:
                    return None
                r.raise_for_status()
                return r.json()
        except Exception:
            return None

    def stats(self) -> dict:
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.get(f"{self._base_url}/stats")
                r.raise_for_status()
                return r.json()
        except Exception:
            return {}

    def status(self) -> dict:
        try:
            with httpx.Client(timeout=5.0) as client:
                r = client.get(f"{self._base_url}/health")
                r.raise_for_status()
                return r.json()
        except Exception:
            return {"status": "error"}

    def _get_services(
        self, category: str = None, tier: int = None, limit: int = 20, offset: int = 0
    ) -> list[dict]:
        try:
            params: dict = {"limit": limit, "offset": offset}
            if category is not None:
                params["category"] = category
            if tier is not None:
                params["tier"] = tier
            with httpx.Client(timeout=10.0) as client:
                r = client.get(f"{self._base_url}/services", params=params)
                r.raise_for_status()
                return r.json()["results"]
        except Exception:
            return []
