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
                credits_remaining=body.get("credits_remaining") or body.get("balance"),
                credits_required=body.get("credits_required") or body.get("required"),
                upgrade_url=body.get("upgrade_url") or body.get("top_up_url"),
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

    Authenticated via the ``X-Wayforth-API-Key`` header. All requests retry up
    to 3 times on 5xx responses with exponential backoff (1s, 2s, 4s). Talks to
    the production gateway at https://gateway.wayforth.io by default.

    >>> from wayforth import Wayforth
    >>> wf = Wayforth(api_key="wf_live_...")
    >>> hits = wf.search("translate text to Spanish")["results"]
    >>> wf.execute("deepl", text="Hello world", target_lang="ES")
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-Wayforth-API-Key": api_key},
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

    def search(self, query: str, limit: int = 5, category: str | None = None,
               tier_min: int | None = None) -> dict:
        """Search the catalog by natural-language intent (1 credit).

        Returns the full response: ``{"query", "results": [...], ...}``.
        """
        p: dict = {"q": query, "limit": limit}
        if category is not None:
            p["category"] = category
        if tier_min is not None:
            p["tier_min"] = tier_min
        return self._request("GET", "/search", params=p)

    def query(self, ql: str, **kwargs: Any) -> dict:
        """Run a structured WayforthQL query (starter tier+)."""
        return self._request("POST", "/query", json={"query": ql, **kwargs})

    def services(
        self,
        category: str | None = None,
        tier: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Any:
        """List services from the catalog."""
        p: dict = {"limit": limit, "offset": offset}
        if category is not None:
            p["category"] = category
        if tier is not None:
            p["tier"] = tier
        return self._request("GET", "/services", params=p)

    def get_service(self, service_id: str) -> dict | None:
        """Get a single service by id. Returns None if not found."""
        try:
            return self._request("GET", f"/services/{service_id}")
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
        """Get catalog statistics (totals, by category, by tier)."""
        return self._request("GET", "/stats")

    def status(self) -> dict:
        """Check gateway health."""
        return self._request("GET", "/health")

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(self, slug: str, **params: Any) -> dict:
        """Execute a managed service by slug with managed credentials."""
        return self._request("POST", "/execute", json={"service_slug": slug, "params": params})

    def execute_batch(self, calls: list[dict]) -> dict:
        """Execute up to 5 managed calls in parallel.

        Each call is ``{"slug": "<service>", "params": {...}}``.
        """
        return self._request("POST", "/execute/batch", json={"calls": calls})

    def run(self, intent: str, **params: Any) -> dict:
        """Intent → search → rank → execute the best-matching service in one call."""
        return self._request("POST", "/run", json={"intent": intent, **params})

    # ── Payments ──────────────────────────────────────────────────────────────

    def pay(self, service_id: str, amount_usd: float = 0.001,
            track: str = "auto", query_id: str | None = None) -> dict:
        """Pay for a service call via card credits or USDC. ``track`` is
        one of ``auto`` | ``card`` | ``crypto``."""
        body: dict = {"service_id": service_id, "amount_usd": amount_usd, "track": track}
        if query_id is not None:
            body["query_id"] = query_id
        return self._request("POST", "/pay", json=body)

    # ── Account ───────────────────────────────────────────────────────────────

    def balance(self) -> dict:
        """Get current credit balance and plan."""
        return self._request("GET", "/billing/balance")

    def credits(self) -> dict:
        """Get the account's credit summary."""
        return self._request("GET", "/account/credits")

    def tier(self) -> dict:
        """Get the account's current tier."""
        return self._request("GET", "/account/tier")

    def usage_history(self) -> dict:
        """Get usage history for the account."""
        return self._request("GET", "/account/usage/history")

    # ── Auth ──────────────────────────────────────────────────────────────────

    def me(self) -> dict:
        """Return the authenticated account (whoami)."""
        return self._request("GET", "/auth/me")

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
    """Asynchronous Wayforth client. Mirrors :class:`Wayforth`.

    Authenticated via the ``X-Wayforth-API-Key`` header. Requests retry up to
    3 times on 5xx with exponential backoff. Defaults to gateway.wayforth.io.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = _DEFAULT_BASE_URL,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"X-Wayforth-API-Key": api_key},
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

    async def search(self, query: str, limit: int = 5, category: str | None = None,
                     tier_min: int | None = None) -> dict:
        p: dict = {"q": query, "limit": limit}
        if category is not None:
            p["category"] = category
        if tier_min is not None:
            p["tier_min"] = tier_min
        return await self._request("GET", "/search", params=p)

    async def query(self, ql: str, **kwargs: Any) -> dict:
        return await self._request("POST", "/query", json={"query": ql, **kwargs})

    async def services(
        self,
        category: str | None = None,
        tier: int | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> Any:
        p: dict = {"limit": limit, "offset": offset}
        if category is not None:
            p["category"] = category
        if tier is not None:
            p["tier"] = tier
        return await self._request("GET", "/services", params=p)

    async def get_service(self, service_id: str) -> dict | None:
        try:
            return await self._request("GET", f"/services/{service_id}")
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
        return await self._request("POST", "/execute", json={"service_slug": slug, "params": params})

    async def execute_batch(self, calls: list[dict]) -> dict:
        return await self._request("POST", "/execute/batch", json={"calls": calls})

    async def run(self, intent: str, **params: Any) -> dict:
        return await self._request("POST", "/run", json={"intent": intent, **params})

    # ── Payments ──────────────────────────────────────────────────────────────

    async def pay(self, service_id: str, amount_usd: float = 0.001,
                  track: str = "auto", query_id: str | None = None) -> dict:
        body: dict = {"service_id": service_id, "amount_usd": amount_usd, "track": track}
        if query_id is not None:
            body["query_id"] = query_id
        return await self._request("POST", "/pay", json=body)

    # ── Account ───────────────────────────────────────────────────────────────

    async def balance(self) -> dict:
        return await self._request("GET", "/billing/balance")

    async def credits(self) -> dict:
        return await self._request("GET", "/account/credits")

    async def tier(self) -> dict:
        return await self._request("GET", "/account/tier")

    async def usage_history(self) -> dict:
        return await self._request("GET", "/account/usage/history")

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def me(self) -> dict:
        return await self._request("GET", "/auth/me")

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
