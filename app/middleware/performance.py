"""
Performance monitoring middleware for tracking request processing time
"""

import time
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


class PerformanceMonitoringMiddleware(BaseHTTPMiddleware):
    """
    Middleware that tracks request processing time and adds it to response headers.

    Useful for development and debugging to identify slow endpoints.
    """

    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        response.headers["X-Process-Time"] = str(process_time)
        return response
