# Stage 1: Development
FROM ghcr.io/astral-sh/uv:alpine AS development

WORKDIR /app

# Copy dependency definition files
COPY pyproject.toml uv.lock ./

# Sync dependencies (frozen ensures uv.lock is used) without installing the project itself
RUN uv sync --frozen --no-install-project

# Copy the rest of the backend files (including README.md, src, etc.)
COPY . .

# Complete the sync to install the backend package
RUN uv sync --frozen

ENV ENVIRONMENT=development
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 3000

CMD ["fastapi", "dev", "src/main.py", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers"]


# Stage 2: Builder (Production dependency builder)
FROM ghcr.io/astral-sh/uv:alpine AS builder

WORKDIR /app

# Copy dependency definition files
COPY pyproject.toml uv.lock ./

# Sync dependencies in production mode (no-dev, no-cache) without installing the project itself
RUN uv sync --frozen --no-cache --no-dev --no-install-project

# Copy the rest of the backend files
COPY . .

# Complete the sync to install the backend package in production mode
RUN uv sync --frozen --no-cache --no-dev


# Stage 3: Production (Minimal runner)
FROM ghcr.io/astral-sh/uv:alpine AS production

WORKDIR /app

# Copy only built/synced artifacts from builder stage
COPY --from=builder /app /app

ENV ENVIRONMENT=production
ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 3000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers"]
