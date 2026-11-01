"""Lightweight in-memory rate limiter for auth endpoints.

Uses a simple sliding-window counter per IP. No external dependencies.
For production at scale, replace with Redis-backed rate limiting.
"""

import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request, status


class _RateLimiter:
    """Thread-safe sliding window rate limiter."""

    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        window_start = now - self.window_seconds
        self._hits[key] = [t for t in self._hits[key] if t > window_start]
        return self._hits[key]

    def check(self, key: str) -> None:
        """Raise 429 if the key has exceeded the rate limit."""
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            if len(hits) >= self.max_requests:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests. Please try again later.",
                )
            hits.append(now)

    def is_limited(self, key: str) -> bool:
        """Whether the key is over the limit, without counting this call as a hit."""
        with self._lock:
            return len(self._prune(key, time.monotonic())) >= self.max_requests

    def record(self, key: str) -> None:
        """Count one hit against the key, e.g. a failed credential check."""
        now = time.monotonic()
        with self._lock:
            self._prune(key, now).append(now)


# Singleton: 5 login attempts per minute per IP
login_limiter = _RateLimiter(max_requests=5, window_seconds=60)


# Singleton: 5 refresh-token exchanges per minute per IP
refresh_limiter = _RateLimiter(max_requests=5, window_seconds=60)


# Singleton: 60 requests per minute per IP for general API
api_limiter = _RateLimiter(max_requests=60, window_seconds=60)


# Singleton: 20 failed MCP bearer-token verifications per minute per IP.
# Same purpose as login_limiter (bound credential-guessing attempts). Only failures
# count: the SSE transport re-verifies the token on every JSON-RPC message, so
# counting successes would lock out a legitimate client after a handful of tool calls.
mcp_auth_limiter = _RateLimiter(max_requests=20, window_seconds=60)


def get_client_ip(request: Request) -> str:
    """Extract the real client IP, respecting X-Forwarded-For from trusted proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit_dep(request: Request) -> None:
    """FastAPI dependency to rate limit public routes."""
    api_limiter.check(get_client_ip(request))

