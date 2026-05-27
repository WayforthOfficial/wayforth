# wayforth-sdk

[![PyPI](https://img.shields.io/pypi/v/wayforth-sdk)](https://pypi.org/project/wayforth-sdk/)

Python SDK for [Wayforth](https://wayforth.io) — search, execute, and pay for any of 4,974 indexed APIs.

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
from wayforth import Wayforth

client = Wayforth(api_key="wf_live_...")

# Search by natural language intent
results = client.search("translate text to Spanish")
for svc in results[:3]:
    print(f"{svc['name']}  WRI: {svc['wri']}  tier {svc['coverage_tier']}")

# Execute a managed service directly — no external API key needed
result = client.execute("deepl", text="Hello", target_lang="ES")
print(result)

# Intent-based routing — Wayforth picks the best service and runs it
result = client.run("summarize this text", content="...")
print(result)
```

Async client:

```python
from wayforth import AsyncWayforth

async with AsyncWayforth(api_key="wf_live_...") as client:
    results = await client.search("real-time stock data")
    result = await client.execute("alpha-vantage", symbol="AAPL", function="TIME_SERIES_INTRADAY")
```

## API

### `Wayforth(api_key, base_url=...)`

Synchronous client. `base_url` defaults to `https://gateway.wayforth.io`.

### `AsyncWayforth(api_key, base_url=...)`

Async client. Same methods as `Wayforth`, all async.

### Methods

| Method | Description |
|--------|-------------|
| `search(query, **filters)` | Search by natural language intent |
| `query(ql, **kwargs)` | Structured WayforthQL query |
| `services(category=None, tier=None, limit=100)` | List services with filters |
| `get_service(id)` | Get a single service by slug or ID |
| `get_similar(service_id, limit=5)` | Services co-used alongside this one |
| `execute(slug, **params)` | Execute a managed service by slug |
| `run(query, **params)` | Intent-based routing and execution |
| `stats()` | Catalog statistics by tier and category |
| `status()` | API health check |
| `balance()` | Account credit balance |
| `get_identity(agent_id)` | Look up a registered agent identity |
| `register_identity(agent_id, display_name="")` | Register an agent identity |

Full API reference: [gateway.wayforth.io/docs](https://gateway.wayforth.io/docs)
