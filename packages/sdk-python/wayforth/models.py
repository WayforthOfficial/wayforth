from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SearchResult:
    """A single result returned by Wayforth catalog search."""

    slug: str | None
    name: str
    description: str
    wri: float
    coverage_tier: int
    category: str | None
    score: float

    @classmethod
    def from_dict(cls, d: dict) -> "SearchResult":
        return cls(
            slug=d.get("slug"),
            name=d.get("name", ""),
            description=d.get("description") or "",
            wri=float(d.get("wri") or 0),
            coverage_tier=int(d.get("coverage_tier") or 0),
            category=d.get("category"),
            score=float(d.get("score") or 0),
        )


@dataclass
class Service:
    """A service from the Wayforth catalog."""

    id: str
    name: str
    description: str
    endpoint_url: str
    category: str
    coverage_tier: int
    pricing_usdc: float
    source: str

    @classmethod
    def from_dict(cls, d: dict) -> "Service":
        return cls(
            id=d.get("id", ""),
            name=d.get("name", ""),
            description=d.get("description") or "",
            endpoint_url=d.get("endpoint_url", ""),
            category=d.get("category") or "",
            coverage_tier=d.get("coverage_tier", 0),
            pricing_usdc=float(d.get("pricing_usdc") or 0),
            source=d.get("source", ""),
        )
