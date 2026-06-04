# wayforth-sdk

[![PyPI](https://img.shields.io/pypi/v/wayforth-sdk)](https://pypi.org/project/wayforth-sdk/)

Python SDK for [Wayforth](https://wayforth.io) — search, execute, and pay for ~5,000 indexed APIs from one client. Talks to the production gateway (`https://gateway.wayforth.io`) and authenticates with your `wf_live_` API key.

Get a free key at [wayforth.io/signup](https://wayforth.io/signup) (100 credits, no card required).

## Install

```bash
pip install wayforth-sdk
# or
uv add wayforth-sdk
```

## Usage

```python
from wayforth import Wayforth

client = Wayforth(api_key="wf_live_...")

# Search by natural-language intent (returns {"query", "results": [...], ...})
hits = client.search("translate text to Spanish")
for svc in hits["results"][:3]:
    print(f"{svc['name']}  WRI {svc['wri']}  tier {svc['coverage_tier']}")

# Execute a managed service directly — Wayforth holds the upstream key
result = client.execute("deepl", text="Hello world", target_lang="ES")
print(result)

# Intent-based routing — Wayforth picks the best service and runs it in one call
result = client.run("summarize this article", content="...")
print(result)

# Account
print(client.balance())   # credits + plan
print(client.me())        # whoami
```

Async client:

```python
from wayforth import AsyncWayforth

async with AsyncWayforth(api_key="wf_live_...") as client:
    hits = await client.search("real-time stock data")
    result = await client.execute("alphavantage", symbol="AAPL")
```

Batch up to 5 calls in parallel:

```python
client.execute_batch([
    {"slug": "groq",  "params": {"messages": [{"role": "user", "content": "2+2?"}], "model": "llama-3.3-70b-versatile"}},
    {"slug": "deepl", "params": {"text": "Good morning", "target_lang": "FR"}},
])
```

## API

### `Wayforth(api_key, base_url="https://gateway.wayforth.io")`
Synchronous client.

### `AsyncWayforth(api_key, base_url="https://gateway.wayforth.io")`
Async client — same methods, all `await`-able.

### Methods

| Method | Endpoint | Description |
|--------|----------|-------------|
| `search(query, limit=5, category=None, tier_min=None)` | `GET /search` | Search by natural-language intent (1 credit) |
| `query(ql, **kwargs)` | `POST /query` | Structured WayforthQL query (starter tier+) |
| `services(category=None, tier=None, limit=20, offset=0)` | `GET /services` | List catalog services |
| `get_service(service_id)` | `GET /services/{id}` | One service by id (None if 404) |
| `get_similar(service_id, limit=5)` | `GET /services/similar/{id}` | Co-used services |
| `execute(slug, **params)` | `POST /execute` | Run a managed service by slug |
| `execute_batch(calls)` | `POST /execute/batch` | Up to 5 managed calls in parallel |
| `run(intent, **params)` | `POST /run` | Intent → search → rank → execute |
| `pay(service_id, amount_usd=0.001, track="auto", query_id=None)` | `POST /pay` | Pay via card credits or USDC |
| `balance()` | `GET /billing/balance` | Credit balance and plan |
| `credits()` | `GET /account/credits` | Credit summary |
| `tier()` | `GET /account/tier` | Current tier |
| `usage_history()` | `GET /account/usage/history` | Usage history |
| `me()` | `GET /auth/me` | Authenticated account (whoami) |
| `get_tiers()` | `GET /keys/tiers` | Tiers and pricing |
| `stats()` | `GET /stats` | Catalog statistics |
| `status()` | `GET /health` | Gateway health |
| `get_identity(agent_id)` | `GET /identity/{id}` | Agent identity / trust score |
| `register_identity(agent_id, display_name="")` | `POST /identity/register` | Register an agent identity |

### Errors

All raise subclasses of `WayforthError` (`status_code` attribute): `AuthenticationError` (401), `InsufficientCreditsError` (402, with `credits_remaining` / `credits_required` / `upgrade_url`), `ServiceUnavailableError` (5xx). Requests retry up to 3× on 5xx with exponential backoff.

Full API reference: [gateway.wayforth.io/docs](https://gateway.wayforth.io/docs)
