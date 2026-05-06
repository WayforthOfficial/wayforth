FROM ghcr.io/astral-sh/uv:latest AS uv
FROM python:3.12-slim

WORKDIR /app
COPY --from=uv /uv /bin/uv

COPY . .

RUN uv sync --frozen --no-dev

EXPOSE 8080

CMD ["uv", "run", "python", "server.py", "--transport", "streamable-http"]
