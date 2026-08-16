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


async def check_rate_limit(request: Request, limiter: RateLimiter):
    """Check rate limit for a request"""
    # Use IP address as identifier
    client_ip = request.client.host if request.client else "unknown"

    # For authenticated requests, could also use user_id
    # identifier = f"{client_ip}:{user_id}"

    if await limiter.is_rate_limited(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(limiter.window_seconds)},
        )
