import httpx
from typing import TypedDict


class SearchResult(TypedDict, total=False):
    service_id: str
    name: str
    description: str
    endpoint_url: str
    category: str
    coverage_tier: int
    pricing_usdc: float
    payment_protocol: str
    score: float
    wri: float
    wayforth_id: str
    reason: str
    source: str


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

    def get_identity(self, agent_id: str) -> dict:
        """Get agent identity and trust score."""
        return self._get(f"/identity/{agent_id}")

    def register_identity(self, agent_id: str, display_name: str = "") -> dict:
        """Register or retrieve an agent identity."""
        return self._post("/identity/register", {
            "agent_id": agent_id,
            "display_name": display_name
        })

    def query(self, query: str, tier_min: int = 2, protocol: str = None,
              sort_by: str = "wri", limit: int = 5, **kwargs) -> dict:
        """WayforthQL structured query."""
        payload = {"query": query, "tier_min": tier_min,
                   "sort_by": sort_by, "limit": limit}
        if protocol:
            payload["protocol"] = protocol
        payload.update(kwargs)
        return self._post("/query", payload)

    def get_similar(self, service_id: str, limit: int = 5) -> dict:
        """Get services co-used with a given service."""
        return self._get(f"/services/similar/{service_id}", params={"limit": limit})

    def get_tiers(self) -> dict:
        """Get available API key tiers and pricing."""
        return self._get("/keys/tiers")

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

    def _get(self, path: str, params: dict = None) -> dict:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{self._base_url}{path}", params=params)
            r.raise_for_status()
            return r.json()

    def _post(self, path: str, payload: dict = None) -> dict:
        with httpx.Client(timeout=10.0) as client:
            r = client.post(f"{self._base_url}{path}", json=payload)
            r.raise_for_status()
            return r.json()
