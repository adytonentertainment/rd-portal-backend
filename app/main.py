import os
import traceback
from contextlib import asynccontextmanager

import uvicorn
from app.database import create_tables
from app.logger import get_logger, setup_logging
from app.middleware import PerformanceMonitoringMiddleware, SecurityHeadersMiddleware
from app.routers import api_router
from app.routers.prelaunch import prelaunch_router
from app.services.scheduled_tasks import start_scheduler, stop_scheduler
from app.services.statement_ingest.runner import start_ingest_worker, stop_ingest_worker
from app.settings import get_settings
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

settings = get_settings()

# Setup logging first to capture all logs including uvicorn
setup_logging()
logger = get_logger()

# Create necessary directories on startup
uploads_dir = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "uploads", "avatars"
)
os.makedirs(uploads_dir, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events for startup and shutdown."""
    # Startup
    logger.info("Starting notification scheduler...")
    start_scheduler()
    # Statement ingest: without a running worker an upload is accepted, written
    # to disk, and then silently never processed. Starting it here means the
    # pipeline cannot be missing just because a deploy didn't launch a separate
    # process. Safe alongside a dedicated worker — uploads are leased.
    logger.info("Starting statement ingest worker...")
    start_ingest_worker()
    yield
    # Shutdown
    logger.info("Stopping statement ingest worker...")
    stop_ingest_worker()
    logger.info("Stopping notification scheduler...")
    stop_scheduler()


# initialize application
app = FastAPI(
    title="Verax Backend API",
    docs_url="/docs" if settings.enable_docs else None,
    redoc_url="/redoc" if settings.enable_docs else None,
    openapi_url="/openapi.json" if settings.enable_docs else None,
    lifespan=lifespan,
    redirect_slashes=False,
)
# Deploy-time origins. A Render service gets a generated hostname
# (verax-frontend-xxxx.onrender.com) that cannot be hardcoded here, and a
# missing origin fails as an opaque browser CORS error rather than a server
# log — so it is configurable. Comma-separated, e.g.
#   EXTRA_CORS_ORIGINS=https://verax-frontend.onrender.com,https://app.verax.app
_extra_origins = [
    o.strip().rstrip("/")
    for o in (os.getenv("EXTRA_CORS_ORIGINS") or "").split(",")
    if o.strip()
]

origins = [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    # RENDER TEST
    "https://r.verax.app",
    "https://www.r.verax.app",
    # LOCAL NETWORK ACCESS
    "http://192.168.1.133:3000",  # Backend IP
    "http://192.168.8.114:3000",  # Frontend IP
    "http://192.168.1.100:3000",  # Previous network IP
    # PRODUCTION
    "https://verax.app",
    "https://www.verax.app",
    "https://production.verax.app",
    "https://www.production.verax.app",
    # STAGING
    "https://staging.verax.app",
    "https://www.staging.verax.app",
    # DEVELOPMENT
    "https://development.verax.app",
    "https://www.development.verax.app",
]


if settings.pre_launch:
    app.include_router(prelaunch_router)
else:
    app.include_router(api_router)


# Validation error handler to return user-friendly error messages
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Convert Pydantic validation errors to user-friendly messages.
    """
    errors = exc.errors()

    # Extract the first error message for a cleaner response
    if errors:
        first_error = errors[0]
        field = first_error.get("loc", ["unknown"])[-1]
        message = first_error.get("msg", "Invalid value")

        # Clean up the message (remove "Value error, " prefix if present)
        if message.startswith("Value error, "):
            message = message[13:]

        detail = message
    else:
        detail = "Invalid request data"

    # Get origin from request
    origin = request.headers.get("origin", "")

    response = JSONResponse(status_code=400, content={"detail": detail})

    # Add CORS headers if origin is allowed
    if origin in origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Expose-Headers"] = "*"

    return response


# Global exception handler to ensure CORS headers are added to error responses
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """
    Catch all exceptions and return a proper JSON response with CORS headers.
    This prevents CORS errors when the backend throws an unexpected exception.
    """
    logger.error(f"Unhandled exception: {exc}")
    logger.error(traceback.format_exc())

    # Get origin from request
    origin = request.headers.get("origin", "")

    # Build response
    response = JSONResponse(
        status_code=500, content={"detail": "Internal server error"}
    )

    # Add CORS headers if origin is allowed
    if origin in origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Expose-Headers"] = "*"

    return response


# Middleware is executed in REVERSE order of addition
# So we add them in reverse priority order (last added = first executed)

# Add performance monitoring middleware (development only) - runs last
if settings.mode == "development":
    app.add_middleware(PerformanceMonitoringMiddleware)

# Add security headers middleware
app.add_middleware(SecurityHeadersMiddleware)

# Add GZip compression for responses (skip SSE endpoints to prevent buffering)
class SSEAwareGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http" and "/genius-stream/" in scope.get("path", ""):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)

app.add_middleware(SSEAwareGZipMiddleware, minimum_size=1000)

# Trust proxy headers from nginx
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[
        "r.verax.app",
        "www.r.verax.app",
        "r-api.verax.app",
        "www.r-api.verax.app",
        "staging.verax.app",
        "www.staging.verax.app",
        "api-staging.verax.app",
        "www.api-staging.verax.app",
        "verax.app",
        "www.verax.app",
        "api.verax.app",
        "www.api.verax.app",
        "development.verax.app",
        "www.development.verax.app",
        "api-development.verax.app",
        "www.api-development.verax.app",
        "localhost",
        "127.0.0.1",
        # The API's own hostname on the deploy target. TrustedHostMiddleware
        # rejects anything not listed with a bare 400, so the generated
        # *.onrender.com host has to be allowed or every request fails.
        *[h.strip() for h in (os.getenv("EXTRA_ALLOWED_HOSTS") or "").split(",") if h.strip()],
    ],
)

origins.extend(_extra_origins)

# Add CORS middleware LAST so it runs FIRST (handles preflight before other middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
    expose_headers=["Content-Type", "Content-Disposition"],
)

create_tables()

if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=8002,
        reload=True,  # Set to False in production
    )
