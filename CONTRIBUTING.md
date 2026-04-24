# Contributing to Wayforth

Thanks for your interest in contributing. Here's how to get a change in.

## Workflow

**1. Fork the repo**

Click **Fork** on GitHub to create your own copy, then clone it locally:

```bash
git clone https://github.com/<your-username>/wayforth.git
cd wayforth
```

**2. Create a feature branch**

Branch off `main` with a short, descriptive name:

```bash
git checkout -b feat/your-feature-name
```

**3. Make your changes**

Set up the dev environment (requires Python 3.12+ and [uv](https://github.com/astral-sh/uv)):

```bash
cp .env.example .env          # fill in your values
docker compose -f infra/docker/docker-compose.dev.yml up -d
cd apps/api && uv run uvicorn main:app --reload
```

Each app (`apps/api`, `apps/crawler`, `apps/labs`) and the MCP server (`packages/mcp-server`) are standalone `uv` projects — run `uv sync` inside each one you touch.

**4. Open a pull request**

Push your branch and open a PR against `main`:

```bash
git push -u origin feat/your-feature-name
```

In the PR description, explain:
- **What** you changed (a brief summary of the diff)
- **Why** you changed it (the problem you're solving or the improvement you're making)
- **How to test it** (any steps a reviewer should follow to verify the change works)

Keep PRs focused — one logical change per PR makes review faster and history cleaner.

## Guidelines

- Follow the existing code style (Python type hints, asyncpg for DB, httpx for HTTP).
- New crawler sources go in `apps/crawler/main.py`; follow the `crawl_*` function pattern.
- Significant architecture decisions belong in `DECISIONS.md` as a new ADR.
- Don't commit `.env` or any file containing secrets.
