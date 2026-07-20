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

    def check(self, key: str) -> None:
        """Raise 429 if the key has exceeded the rate limit."""
        now = time.monotonic()
        with self._lock:
            window_start = now - self.window_seconds
            # Prune old entries
            self._hits[key] = [t for t in self._hits[key] if t > window_start]
            if len(self._hits[key]) >= self.max_requests:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Too many requests. Please try again later.",
                )
            self._hits[key].append(now)


# Singleton: 5 login attempts per minute per IP
login_limiter = _RateLimiter(max_requests=5, window_seconds=60)


# Singleton: 60 requests per minute per IP for general API
api_limiter = _RateLimiter(max_requests=60, window_seconds=60)


def get_client_ip(request: Request) -> str:
    """Extract the real client IP, respecting X-Forwarded-For from trusted proxies."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def rate_limit_dep(request: Request) -> None:
    """FastAPI dependency to rate limit public routes."""
    api_limiter.check(get_client_ip(request))

