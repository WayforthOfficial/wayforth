import httpx


class WayforthClient:
    def __init__(self, base_url: str = "https://api-production-fd71.up.railway.app"):
        self._base_url = base_url.rstrip("/")

    def search(self, intent: str, category: str = None, limit: int = 5) -> list[dict]:
        services = self._get_services(category=category)
        tokens = intent.lower().split()

        def _score(svc: dict) -> int:
            haystack = f"{svc.get('name', '')} {svc.get('description', '')}".lower()
            return sum(1 for t in tokens if t in haystack)

        return sorted(services, key=_score, reverse=True)[:limit]

    def list_services(self, category: str = None, tier: int = None, limit: int = 100) -> list[dict]:
        services = self._get_services(category=category)
        if tier is not None:
            services = [s for s in services if s.get("coverage_tier") == tier]
        return services[:limit]

    def status(self) -> dict:
        try:
            with httpx.Client(timeout=5.0) as client:
                r = client.get(f"{self._base_url}/health")
                r.raise_for_status()
                return r.json()
        except Exception:
            return {"status": "error"}

    def _get_services(self, category: str = None) -> list[dict]:
        try:
            params = {}
            if category is not None:
                params["category"] = category
            with httpx.Client(timeout=10.0) as client:
                r = client.get(f"{self._base_url}/services", params=params)
                r.raise_for_status()
                return r.json()
        except Exception:
            return []
