"""
Rate limiting middleware for authentication endpoints
"""

from fastapi import HTTPException, Request
from datetime import datetime, timedelta
from collections import defaultdict
import asyncio


class RateLimiter:
    """Simple in-memory rate limiter"""

    def __init__(self, max_requests: int = 5, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = defaultdict(list)
        self.lock = asyncio.Lock()

    async def is_rate_limited(self, identifier: str) -> bool:
        """Check if identifier (IP/user) is rate limited"""
        async with self.lock:
            now = datetime.now()
            cutoff = now - timedelta(seconds=self.window_seconds)

            # Clean old requests
            self.requests[identifier] = [
                req_time for req_time in self.requests[identifier] if req_time > cutoff
            ]

            # Check limit
            if len(self.requests[identifier]) >= self.max_requests:
                return True

            # Add new request
            self.requests[identifier].append(now)
            return False

    async def cleanup_old_entries(self):
        """Periodic cleanup of old entries to prevent memory leak"""
        async with self.lock:
            now = datetime.now()
            cutoff = now - timedelta(seconds=self.window_seconds * 2)

            # Remove identifiers with no recent requests
            self.requests = defaultdict(
                list,
                {
                    k: v
                    for k, v in self.requests.items()
                    if any(req_time > cutoff for req_time in v)
                },
            )


# Global rate limiters for different endpoints
login_rate_limiter = RateLimiter(max_requests=10, window_seconds=60)  # 10 per minute
signup_rate_limiter = RateLimiter(max_requests=10, window_seconds=300)  # 10 per 5 minutes (beta-friendly)
password_reset_limiter = RateLimiter(
    max_requests=5, window_seconds=600
)  # 5 per 10 minutes


def _client_ip(request: Request) -> str:
    """Address to count.

    Behind nginx the socket peer is the proxy, so every visitor shared one
    bucket. When that peer is a loopback or private address, use the last
    X-Forwarded-For entry, which is the one the proxy appended. A value the
    client put on the left is ignored.
    """
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded and _is_trusted_proxy(peer):
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if parts:
            return parts[-1]
    return peer or "unknown"


def _is_trusted_proxy(host: str) -> bool:
    if host in {"127.0.0.1", "::1", "localhost"}:
        return True
    octets = host.split(".")
    if len(octets) != 4 or not all(part.isdigit() for part in octets):
        return False
    a, b = int(octets[0]), int(octets[1])
    if a == 10 or a == 127:
        return True
    if a == 192 and b == 168:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    return False


async def check_rate_limit(request: Request, limiter: RateLimiter):
    """Check rate limit for a request"""
    client_ip = _client_ip(request)

    if await limiter.is_rate_limited(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(limiter.window_seconds)},
        )
