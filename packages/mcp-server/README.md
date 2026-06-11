# wayforth-mcp

[![PyPI](https://img.shields.io/pypi/v/wayforth-mcp)](https://pypi.org/project/wayforth-mcp/)

MCP server for [Wayforth](https://wayforth.io) — one tool call to search, execute, and pay for any of ~5,000 indexed APIs.

mcp-name: io.github.WayforthOfficial/wayforth

## Install

```bash
uvx wayforth-mcp
```

Add to Claude Code permanently:

```bash
claude mcp add wayforth -- uvx wayforth-mcp
```

## Setup

Get an API key at [wayforth.io/signup](https://wayforth.io/signup), then set it in your environment:

```bash
export WAYFORTH_API_KEY=wf_live_...
```

## Tools

| Tool | Description |
|------|-------------|
| `wayforth_search` | Search ~5,000 APIs by natural language — returns WRI scores, tier, price/req |
| `wayforth_query` | Structured discovery — filter by tier, latency, region, price, payment rail |
| `wayforth_run` | Intent-based routing: describe what you need, Wayforth picks and executes |
| `wayforth_execute` | Direct execution of a managed service by slug — no API key required |
| `wayforth_pay` | Pay for a service via card credits or USDC on Base |
| `wayforth_list` | Browse catalog with category, tier, and limit filters |
| `wayforth_status` | Live API health and service counts |
| `wayforth_stats` | Catalog statistics — total services, tier breakdown |
| `wayforth_keys` | Store your own API keys encrypted at rest (BYOK) |
| `wayforth_remember` | Save a service to persistent agent memory |
| `wayforth_recall` | Retrieve services previously saved to memory |
| `wayforth_compare` | Side-by-side comparison of two services by slug |
| `wayforth_similar` | Find services co-used alongside a given service |
| `wayforth_identity` | Get or create your agent identity — returns trust score, reputation tier, usage history |
| `wayforth_check_agent_identity` | Look up another agent's identity by wallet address |
| `wayforth_set_wri_alert` | Set a webhook to fire when a service crosses a WRI score threshold |
| `wayforth_quickstart` | Get started guide — returned as a formatted string |

## Development

```bash
cd packages/mcp-server
uv sync
uv run python server.py
```
