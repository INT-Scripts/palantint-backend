FROM ghcr.io/astral-sh/uv:alpine

WORKDIR /app

# Copy the backend directory
# Context is the root PalantINT directory
COPY backend /app

# Sync dependencies (frozen ensures uv.lock is used)
RUN uv sync --frozen

EXPOSE 3000

CMD ["/app/.venv/bin/fastapi", "dev", "src/main.py", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers"]
