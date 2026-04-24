FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY . .
RUN uv sync --frozen
EXPOSE 8001
CMD ["uv", "run", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "$PORT"]
