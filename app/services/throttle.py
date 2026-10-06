"""Read and write the durable throttle counters.

Kept deliberately small and failure-tolerant: throttling is a control, not a
feature, and a database hiccup must never be the reason a legitimate writer
cannot sign in. Every function here fails OPEN — if the counter cannot be read
or written, the request proceeds and the problem is logged.

That is a real trade-off and worth stating plainly: a database outage removes
the brute-force protection. The alternative, failing closed, turns a database
blip into a total login outage for every client, which is the worse failure for
a royalty portal. Edge protection (Cloudflare) is the layer that should survive
a database problem, not this one.
"""

from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.logger.logger import get_logger
from app.models.throttle import AuthThrottle  # re-exported for tests

logger = get_logger("throttle")


def _row(db: Session, scope: str, identifier: str) -> Optional[AuthThrottle]:
    return (
        db.query(AuthThrottle)
        .filter(AuthThrottle.scope == scope, AuthThrottle.identifier == identifier)
        .first()
    )


def locked_until(db: Session, scope: str, identifier: str) -> Optional[datetime]:
    """When this identifier is locked until, or None if it is not locked."""
    try:
        row = _row(db, scope, identifier)
        if row and row.locked_until and row.locked_until > datetime.now():
            return row.locked_until
        return None
    except Exception as exc:
        logger.error(f"throttle read failed ({scope}/{identifier}): {exc}")
        return None  # fail open


def record_failure(
    db: Session,
    scope: str,
    identifier: str,
    *,
    max_attempts: int,
    window: timedelta,
    lockout: timedelta,
) -> int:
    """Count one failure. Returns attempts used in the current window.

    Starts a fresh window when the previous one has expired, so a slow trickle
    of wrong passwords over hours never accumulates into a lock.
    """
    try:
        now = datetime.now()
        row = _row(db, scope, identifier)
        if row is None:
            row = AuthThrottle(scope=scope, identifier=identifier, count=0, window_start=now)
            db.add(row)

        if row.window_start is None or now - row.window_start > window:
            row.window_start = now
            row.count = 0

        row.count = (row.count or 0) + 1
        if row.count >= max_attempts:
            row.locked_until = now + lockout
        db.commit()
        return row.count
    except Exception as exc:
        db.rollback()
        logger.error(f"throttle write failed ({scope}/{identifier}): {exc}")
        return 0  # fail open


def clear(db: Session, scope: str, identifier: str) -> None:
    """Forget this identifier — called on a successful login."""
    try:
        row = _row(db, scope, identifier)
        if row is not None:
            db.delete(row)
            db.commit()
    except Exception as exc:
        db.rollback()
        logger.error(f"throttle clear failed ({scope}/{identifier}): {exc}")


def purge_expired(db: Session, older_than: timedelta = timedelta(days=7)) -> int:
    """Drop rows nobody is counting any more, so the table stays small."""
    try:
        cutoff = datetime.now() - older_than
        n = (
            db.query(AuthThrottle)
            .filter(AuthThrottle.updated_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        return n or 0
    except Exception as exc:
        db.rollback()
        logger.error(f"throttle purge failed: {exc}")
        return 0
