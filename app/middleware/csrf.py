"""CSRF guard for cookie-authenticated requests.

The browser session is an HttpOnly cookie (see app/routers/auth.py), which the
browser attaches on its own. SameSite=Lax already withholds it from other
sites' requests; this closes the remaining gap (another subdomain of the same
site) by requiring a custom header on every data-changing request that carries
the cookie. A page can only add a custom header to a cross-origin request after
a CORS preflight, which succeeds only for the portal's own origins.

Requests authenticated with a Bearer header are not affected: browsers never
attach that header by themselves.

Pure ASGI rather than BaseHTTPMiddleware so request bodies (multi-gigabyte
statement uploads) stream through untouched.
"""

from http.cookies import CookieError, SimpleCookie

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = b"x-requested-with"


def _has_cookie(raw: bytes, name: str) -> bool:
    try:
        jar = SimpleCookie()
        jar.load(raw.decode("latin-1"))
    except CookieError:
        return False
    return name in jar


class CookieCSRFMiddleware:
    def __init__(self, app: ASGIApp, cookie_name: str) -> None:
        self.app = app
        self.cookie_name = cookie_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            headers = dict(scope.get("headers") or [])
            cookie = headers.get(b"cookie", b"")
            if cookie and _has_cookie(cookie, self.cookie_name) and not headers.get(CSRF_HEADER):
                response = JSONResponse(
                    status_code=403, content={"detail": "Missing X-Requested-With header"}
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
