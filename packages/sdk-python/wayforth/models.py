from dataclasses import dataclass


@dataclass
class Service:
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
