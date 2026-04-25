# Contributing to Wayforth

## What's Open for Contribution

- MCP server (`packages/mcp-server/`)
- Python SDK (`packages/sdk-python/`)
- TypeScript SDK (`packages/sdk-typescript/`)
- Smart contracts (`contracts/base/`)
- Crawler sources (`apps/crawler/`)

## What's Closed

The WayforthRank ranking engine is proprietary closed-source. The `ranker_client.py` interface is open — the implementation is not.

## How to Contribute

1. Fork the repo
2. Create a feature branch: `git checkout -b feat/your-feature`
3. Make changes with tests
4. For contracts: `cd contracts/base && forge test`
5. Open a PR with a clear description

## Code Style

- Python: follow existing patterns, type hints required
- TypeScript: strict mode, no `any`
- Solidity: NatSpec comments on all public functions

## License

By contributing, you agree your contributions are licensed under BSL 1.1.

Questions? hello@wayforth.io
