FROM python:3.12-slim
ARG BUILD_DATE=unknown
RUN echo "Cache bust: $BUILD_DATE" && date
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY apps/api/ .
RUN uv sync --frozen
EXPOSE 8000
CMD ["/bin/sh", "-c", "uv run uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
