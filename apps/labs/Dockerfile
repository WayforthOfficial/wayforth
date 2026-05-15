# Pinned to python:3.12-slim-bookworm manifest list digest.
# To update: docker manifest inspect python:3.12-slim-bookworm | jq -r '.manifests[0].digest'
FROM python:3.12-slim-bookworm@sha256:d193c6f51a7dbd10395d6328de3a7edb0516fb0608ca138036576f574c3e07d2
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY . .
RUN uv sync --frozen
RUN groupadd --system app \
 && useradd --system --gid app --create-home --home-dir /home/app app \
 && chown -R app:app /app /home/app
USER app
EXPOSE 8001
CMD ["/bin/sh", "-c", "uv run uvicorn main:app --host 0.0.0.0 --port ${PORT:-8001}"]
