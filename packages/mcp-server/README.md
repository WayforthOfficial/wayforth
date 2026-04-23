# wayforth-mcp

MCP server for [Wayforth](https://github.com/your-org/wayforth) — discover AI services from Claude Code, Cursor, or any MCP-compatible client.

## Install (one command)

```bash
claude mcp add wayforth -- uv run --directory ~/Code/wayforth/packages/mcp-server python server.py
```

That's it. Claude Code will restart and the three Wayforth tools will be available immediately.

## Tools

| Tool | Description |
|---|---|
| `wayforth_search` | Search services by natural language intent, with optional category and tier filters |
| `wayforth_list` | List all services, optionally filtered by category |
| `wayforth_status` | Catalog stats (counts by tier and category) and API health |

## Prerequisites

The Wayforth API must be running:

```bash
cd ~/Code/wayforth/apps/api
uv run uvicorn main:app --port 8000
```

By default the MCP server connects to `http://localhost:8000`. Override with:

```bash
export WAYFORTH_API_URL=https://api.wayforth.io
```

## Run manually (for testing)

```bash
cd ~/Code/wayforth/packages/mcp-server
uv run python server.py
```

The server starts on stdio and waits for MCP protocol messages. Press `Ctrl+C` to exit.

## Development

```bash
cd packages/mcp-server
uv sync          # install deps
uv run python server.py
```
