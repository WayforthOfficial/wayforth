"""server.py — Wayforth MCP server entry point.

Imports the shared mcp instance and registers all tools by importing the tools package.
pyproject.toml entry point: wayforth-mcp = "server:main"
"""

import logging
import os

from dotenv import load_dotenv

from mcp_instance import (
    mcp,
    API_BASE,
    WAYFORTH_API_KEY,
    ApiKeyMiddleware,
    _HOST,
    _PORT,
)

# Register all tools by importing the tools package
import tools  # noqa: F401

logger = logging.getLogger("wayforth-mcp")

load_dotenv()


def _fetch_credits_sync() -> int | None:
    api_key = os.getenv("WAYFORTH_API_KEY", "")
    if not api_key:
        return None
    try:
        import httpx
        with httpx.Client(timeout=5.0) as client:
            r = client.get(
                f"{API_BASE}/keys/usage",
                headers={"X-Wayforth-API-Key": api_key},
            )
            if r.status_code == 200:
                d = r.json()
                return d.get("credits_balance") or d.get("credits_remaining")
    except Exception:
        pass
    return None


def main():
    import sys
    import argparse

    parser = argparse.ArgumentParser(description="Wayforth MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default=os.getenv("MCP_TRANSPORT", "stdio"),
        help="Transport to use: stdio (default), sse, or streamable-http",
    )
    args, _ = parser.parse_known_args()

    banner = (
        "╔════════════════════════════════════════╗\n"
        "║         WAYFORTH MCP SERVER            ║\n"
        "║   Execute · Search · Pay · Repeat      ║\n"
        "╚════════════════════════════════════════╝"
    )
    print(banner, file=sys.stderr)

    if args.transport != "stdio":
        print(f"\nTransport: {args.transport}  host={_HOST}  port={_PORT}", file=sys.stderr)

    api_key = os.getenv("WAYFORTH_API_KEY", "")
    if api_key:
        if args.transport == "stdio":
            credits = _fetch_credits_sync()
            if credits is not None:
                print(f"\nYour credits: {credits} · wayforth.io/dashboard", file=sys.stderr)
            else:
                print("\nCredits unavailable · wayforth.io/dashboard", file=sys.stderr)
            print(
                '\nStart here:\n'
                '  wayforth_execute(service_slug="deepl",\n'
                '                   params={"text": "Hello", "target_lang": "ES"},\n'
                '                   key_source="managed")\n\n'
                'Or discover new services:\n'
                '  wayforth_search("translate text to Spanish")\n\n'
                'Need more credits? → wayforth.io/pricing\n',
                file=sys.stderr,
            )
    else:
        print(
            '\n  Set your API key: export WAYFORTH_API_KEY=wf_live_...\n'
            '  Get one free at: wayforth.io/signup\n',
            file=sys.stderr,
        )

    if args.transport in ("sse", "streamable-http"):
        # Wrap the Starlette app with ApiKeyMiddleware so query-param keys work
        import anyio
        import uvicorn
        if args.transport == "streamable-http":
            starlette_app = mcp.streamable_http_app()
        else:
            starlette_app = mcp.sse_app()
        config = uvicorn.Config(
            ApiKeyMiddleware(starlette_app),
            host=_HOST,
            port=_PORT,
            log_level="info",
        )
        anyio.run(uvicorn.Server(config).serve)
    else:
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
