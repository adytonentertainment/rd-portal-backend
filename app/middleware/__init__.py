"""
Middleware modules for the TuneScan backend API
"""

from app.middleware.account_lockout import AccountLockout, account_lockout
from app.middleware.rate_limit import (
    RateLimiter,
    login_rate_limiter,
    signup_rate_limiter,
    password_reset_limiter,
    check_rate_limit,
)
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.middleware.performance import PerformanceMonitoringMiddleware

__all__ = [
    "AccountLockout",
    "account_lockout",
    "RateLimiter",
    "login_rate_limiter",
    "signup_rate_limiter",
    "password_reset_limiter",
    "check_rate_limit",
    "SecurityHeadersMiddleware",
    "PerformanceMonitoringMiddleware",
]
