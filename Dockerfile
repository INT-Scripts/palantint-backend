# Stage 1: Base image with uv
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS base

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"


# Stage 2: Development stage
FROM base AS development

# Copy dependency specifications and install dependencies with cache mount
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Copy backend source files
COPY . .

# Install project package into virtualenv
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

ENV ENVIRONMENT=development

EXPOSE 3000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers", "--reload"]


# Stage 3: Builder (Production dependency builder)
FROM base AS builder

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# Stage 4: Production (Minimal non-root runner)
FROM python:3.13-slim-bookworm AS production

WORKDIR /app

ENV ENVIRONMENT=production \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Create non-root user for enhanced security
RUN groupadd -g 10001 appgroup && \
    useradd -u 10001 -g appgroup -s /bin/sh appuser

COPY --chown=appuser:appuser --from=builder /app /app

USER appuser

EXPOSE 3000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "3000", "--proxy-headers"]
