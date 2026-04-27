# wayforth-sdk

Python SDK for [Wayforth](https://gateway.wayforth.io) — discover and call AI services from the catalog.

## Install

```bash
pip install wayforth-sdk
```

Or with uv:

```bash
uv add wayforth-sdk
```

## Usage

```python
from wayforth import WayforthClient

client = WayforthClient()

# Check API status
print(client.status())

# Search for services by natural language intent
results = client.search("fast cheap inference for coding")
for svc in results[:3]:
    print(f"{svc['name']} (tier {svc['coverage_tier']}) — {svc['endpoint_url']}")

# List all translation services
translators = client.list_services(category="translation")
print(f"Found {len(translators)} translation services")
```

## API

### `WayforthClient(base_url=...)`

Defaults to the production API. Pass a custom `base_url` to point at a local instance.

### `search(intent, category=None, limit=5) -> list[dict]`

Returns services ranked by keyword overlap with `intent`. Optional `category` filter (`"inference"`, `"translation"`, `"data"`).

### `list_services(category=None, tier=None, limit=100) -> list[dict]`

Lists services, optionally filtered by `category` and/or `coverage_tier` (0–2).

### `status() -> dict`

Returns API health: `{"status": "ok", "service": "wayforth-api", "version": "...", "db_status": "ok"}`.
