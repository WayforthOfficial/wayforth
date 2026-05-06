FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY packages/mcp-server/ .
RUN uv sync --frozen
EXPOSE 8080
CMD ["/bin/sh", "-c", "uv run python server.py --transport streamable-http"]
