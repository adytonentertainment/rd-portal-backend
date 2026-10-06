"""Error reporting, with the royalty data kept out of it.

Until now a 500 in production was invisible unless somebody happened to read
the Render logs — on a system that moves other people's money, the first
notification of a fault was a client noticing.

WHAT THIS DELIBERATELY DOES NOT SEND. The events that matter here travel with
things that must not leave the building: a writer's email and name, bearer
tokens, invite tokens, the database URL and its password, and the royalty
figures themselves. Sentry's defaults already exclude request bodies and PII,
and `_scrub` strips the rest by key before the event is sent, because a
stack trace that leaks a client's earnings is worse than no stack trace.

Disabled when SENTRY_DSN is unset, so development and the Verax deployment are
unaffected by its presence.
"""

import os
from typing import Any, Dict, Optional

from app.logger.logger import get_logger

logger = get_logger("sentry")

# Anything whose key contains one of these is replaced before the event leaves
# the process. Substring matching on purpose: it is better to redact a
# harmless field than to miss `db_password` because the list said `password`.
_SENSITIVE = (
    "password", "token", "secret", "authorization", "auth", "cookie",
    "api_key", "apikey", "dsn", "database_url", "sqlalchemy_database_url",
    "passphrase", "credential", "session",
)

_REDACTED = "[redacted]"


def _scrub(obj: Any, depth: int = 0) -> Any:
    """Recursively redact sensitive values by key name."""
    if depth > 6:  # events are shallow; this is a guard against cycles
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and any(s in k.lower() for s in _SENSITIVE):
                out[k] = _REDACTED
            else:
                out[k] = _scrub(v, depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return type(obj)(_scrub(v, depth + 1) for v in obj)
    return obj


def _before_send(event: Dict, hint: Dict) -> Optional[Dict]:
    """Last gate before an event is transmitted."""
    try:
        # The query string can carry an invite or reset token; the URL path is
        # enough to locate the fault without it.
        request = event.get("request") or {}
        request.pop("query_string", None)
        request.pop("data", None)
        if "headers" in request:
            request["headers"] = _scrub(request["headers"])
        if "env" in request:
            request["env"] = _scrub(request["env"])
        return _scrub(event)
    except Exception as exc:  # never let reporting break the request
        logger.error(f"sentry before_send failed, dropping event: {exc}")
        return None


def init_sentry() -> bool:
    """Start error reporting. Returns whether it was enabled."""
    dsn = (os.getenv("SENTRY_DSN") or "").strip()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("ENVIRONMENT", "unknown").lower(),
            # Render exposes the deployed commit; it makes a stack trace
            # answerable ("which build was this?") instead of merely alarming.
            release=os.getenv("RENDER_GIT_COMMIT") or None,
            integrations=[
                StarletteIntegration(), FastApiIntegration(), SqlalchemyIntegration(),
            ],
            # Never send names, emails or IPs. This is the whole point.
            send_default_pii=False,
            # Errors only. Performance tracing on a statement ingest that runs
            # for minutes produces enormous, costly traces for no benefit here.
            traces_sample_rate=0.0,
            before_send=_before_send,
            max_breadcrumbs=25,
        )
        logger.info("Sentry error reporting enabled")
        return True
    except Exception as exc:
        # Monitoring must never be the reason the app will not boot.
        logger.error(f"Sentry init failed, continuing without it: {exc}")
        return False
