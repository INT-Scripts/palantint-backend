FROM ghcr.io/astral-sh/uv:alpine

WORKDIR /app

# Copy dependency files first
COPY pyproject.toml uv.lock ./

# Sync dependencies (this caches the layer)
RUN uv sync --frozen

# Then copy the rest of the app
COPY . .

EXPOSE 3000

CMD ["/app/.venv/bin/fastapi", "dev", "src/main.py", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers"]