from __future__ import annotations

from typing import Any

import httpx

from .errors import (
    AuthenticationError,
    InsufficientCreditsError,
    ServiceUnavailableError,
    WayforthError,
)
from .retry import with_retry, with_retry_async

_DEFAULT_BASE_URL = "https://gateway.wayforth.io"


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        detail = response.json().get("detail", response.text)
    except Exception:
        detail = response.text
    status = response.status_code
    if status == 401:
        raise AuthenticationError(detail, status)
    if status == 402:
        try:
            body = response.json()
            raise InsufficientCreditsError(
                body.get("error", "insufficient_credits"),
                status,
                credits_remaining=body.get("credits_remaining"),
                credits_required=body.get("credits_required"),
                upgrade_url=body.get("upgrade_url"),
            )
        except InsufficientCreditsError:
            raise
        except Exception:
            raise InsufficientCreditsError(detail, status)
    if status >= 500:
        raise ServiceUnavailableError(detail, status)
    raise WayforthError(detail, status)


class Wayforth:
    """Synchronous Wayforth client.

    Authenticated via x-wayforth-api-key header. All requests retry up to
    3 times on 5xx responses with exponential backoff (1s, 2s, 4s).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"x-wayforth-api-key": api_key},
            timeout=30.0,
        )

    def __enter__(self) -> "Wayforth":
        return self

    def __exit__(self, *args: Any) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        response = with_retry(
            lambda: self._client.request(method, path, json=json, params=params)
        )
        _raise_for_status(response)
        return response.json()

    # ── Search & discovery ────────────────────────────────────────────────────

    def search(self, query: str, **filters: Any) -> list[dict]:
        """Search the service catalog by intent."""
        return self._request("POST", "/v1/search", json={"query": query, **filters})

    def query(self, ql: str, **kwargs: Any) -> dict:
        """Execute a WayforthQL structured query."""
        return self._request("POST", "/v1/query", json={"ql": ql, **kwargs})

    def services(
        self,
        category: str | None = None,
        tier: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        """List services from the catalog."""
        p: dict = {"limit": limit, "offset": offset}
        if category is not None:
            p["category"] = category
        if tier is not None:
            p["tier"] = tier
        return self._request("GET", "/services", params=p)

    def get_service(self, id: str) -> dict | None:
        """Get a single service by ID. Returns None if not found."""
        try:
            return self._request("GET", f"/services/{id}")
        except WayforthError as exc:
            if exc.status_code == 404:
                return None
            raise

    def get_similar(self, service_id: str, limit: int = 5) -> dict:
        """Get services frequently used alongside a given service."""
        return self._request(
            "GET", f"/services/similar/{service_id}", params={"limit": limit}
        )

    def get_tiers(self) -> dict:
        """Get available API key tiers and their pricing."""
        return self._request("GET", "/keys/tiers")

    def stats(self) -> dict:
        """Get platform statistics (total services, by category, by tier)."""
        return self._request("GET", "/stats")

    def status(self) -> dict:
        """Check API health."""
        return self._request("GET", "/health")

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(self, slug: str, **params: Any) -> dict:
        """Execute a managed service by slug."""
        return self._request("POST", f"/v1/execute/{slug}", json=params)

    def run(self, query: str, **params: Any) -> dict:
        """Run a natural-language query against the best-matching service."""
        return self._request("POST", "/v1/run", json={"query": query, **params})

    # ── Account ───────────────────────────────────────────────────────────────

    def balance(self) -> dict:
        """Get current credit balance and tier."""
        return self._request("GET", "/v1/balance")

    # ── Agent identity ────────────────────────────────────────────────────────

    def get_identity(self, agent_id: str) -> dict:
        """Get agent identity and trust score."""
        return self._request("GET", f"/identity/{agent_id}")

    def register_identity(self, agent_id: str, display_name: str = "") -> dict:
        """Register or retrieve an agent identity."""
        return self._request(
            "POST",
            "/identity/register",
            json={"agent_id": agent_id, "display_name": display_name},
        )


class AsyncWayforth:
    """Asynchronous Wayforth client.

    Authenticated via x-wayforth-api-key header. All requests retry up to
    3 times on 5xx responses with exponential backoff (1s, 2s, 4s).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"x-wayforth-api-key": api_key},
            timeout=30.0,
        )

    async def __aenter__(self) -> "AsyncWayforth":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        response = await with_retry_async(
            lambda: self._client.request(method, path, json=json, params=params)
        )
        _raise_for_status(response)
        return response.json()

    # ── Search & discovery ────────────────────────────────────────────────────

    async def search(self, query: str, **filters: Any) -> list[dict]:
        return await self._request("POST", "/v1/search", json={"query": query, **filters})

    async def query(self, ql: str, **kwargs: Any) -> dict:
        return await self._request("POST", "/v1/query", json={"ql": ql, **kwargs})

    async def services(
        self,
        category: str | None = None,
        tier: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        p: dict = {"limit": limit, "offset": offset}
        if category is not None:
            p["category"] = category
        if tier is not None:
            p["tier"] = tier
        return await self._request("GET", "/services", params=p)

    async def get_service(self, id: str) -> dict | None:
        try:
            return await self._request("GET", f"/services/{id}")
        except WayforthError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def get_similar(self, service_id: str, limit: int = 5) -> dict:
        return await self._request(
            "GET", f"/services/similar/{service_id}", params={"limit": limit}
        )

    async def get_tiers(self) -> dict:
        return await self._request("GET", "/keys/tiers")

    async def stats(self) -> dict:
        return await self._request("GET", "/stats")

    async def status(self) -> dict:
        return await self._request("GET", "/health")

    # ── Execution ─────────────────────────────────────────────────────────────

    async def execute(self, slug: str, **params: Any) -> dict:
        return await self._request("POST", f"/v1/execute/{slug}", json=params)

    async def run(self, query: str, **params: Any) -> dict:
        return await self._request("POST", "/v1/run", json={"query": query, **params})

    # ── Account ───────────────────────────────────────────────────────────────

    async def balance(self) -> dict:
        return await self._request("GET", "/v1/balance")

    # ── Agent identity ────────────────────────────────────────────────────────

    async def get_identity(self, agent_id: str) -> dict:
        return await self._request("GET", f"/identity/{agent_id}")

    async def register_identity(self, agent_id: str, display_name: str = "") -> dict:
        return await self._request(
            "POST",
            "/identity/register",
            json={"agent_id": agent_id, "display_name": display_name},
        )


# Backwards-compatible alias
WayforthClient = Wayforth
