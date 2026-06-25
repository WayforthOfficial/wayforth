# Pinned to python:3.12-slim-bookworm manifest list digest.
# To update: docker manifest inspect python:3.12-slim-bookworm | jq -r '.manifests[0].digest'
FROM python:3.12-slim-bookworm@sha256:d193c6f51a7dbd10395d6328de3a7edb0516fb0608ca138036576f574c3e07d2
ARG BUILD_DATE=1777145911
RUN echo "Cache bust: $BUILD_DATE" && date
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY apps/api/ .
# --no-dev: the `dev` dependency-group (pytest, a2a-sdk — the interop-gate verifier)
# is test-only and must not enter the runtime image. Without it `uv sync` installs
# dev by default, dragging a2a-sdk's grpc/protobuf chain into the slim build.
RUN uv sync --frozen --no-dev
RUN groupadd --system app \
 && useradd --system --gid app --create-home --home-dir /home/app app \
 && chown -R app:app /app /home/app
USER app
EXPOSE 8000
CMD ["/bin/sh", "-c", "uv run uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
