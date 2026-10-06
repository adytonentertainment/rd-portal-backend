"""Login lockout survives a restart.

It used to live in a dict on the running process, so every deploy and every
container restart erased it — and Render restarts routinely. A brute-force
attempt never had to outwait the 30-minute lockout; it only had to outlast the
next deploy. The limit read as protection in review and was close to nothing in
practice.

The test that matters is test_lockout_survives_a_process_restart: everything
else here is detail.
"""

from datetime import datetime, timedelta

import pytest

from app.services import throttle as svc

WINDOW = timedelta(minutes=15)
LOCKOUT = timedelta(minutes=30)


def _fail(session, who, n=1):
    last = 0
    for _ in range(n):
        last = svc.record_failure(
            session, "lockout", who, max_attempts=5, window=WINDOW, lockout=LOCKOUT
        )
    return last


def test_counts_failures(session):
    assert _fail(session, "a@b.com") == 1
    assert _fail(session, "a@b.com") == 2
    assert svc.locked_until(session, "lockout", "a@b.com") is None


def test_locks_at_the_threshold(session):
    _fail(session, "a@b.com", 5)
    until = svc.locked_until(session, "lockout", "a@b.com")
    assert until is not None
    assert until > datetime.now()


def test_lockout_survives_a_process_restart(session):
    """The whole point. Nothing is cached in the process, so a new one sees it."""
    _fail(session, "victim@example.com", 5)

    # Simulate a restart as hard as this can be simulated in-process: drop every
    # object the ORM is holding, so the next read must come from the database.
    session.expunge_all()

    assert svc.locked_until(session, "lockout", "victim@example.com") is not None


def test_a_successful_login_clears_it(session):
    _fail(session, "a@b.com", 3)
    svc.clear(session, "lockout", "a@b.com")
    assert _fail(session, "a@b.com") == 1  # counting started again


def test_an_expired_window_starts_over(session):
    """A slow trickle of wrong passwords over hours must not accumulate."""
    _fail(session, "slow@example.com", 4)
    row = (
        session.query(svc.AuthThrottle)
        .filter_by(scope="lockout", identifier="slow@example.com")
        .first()
    )
    row.window_start = datetime.now() - timedelta(hours=2)
    session.commit()

    assert _fail(session, "slow@example.com") == 1
    assert svc.locked_until(session, "lockout", "slow@example.com") is None


def test_identifiers_are_independent(session):
    _fail(session, "one@example.com", 5)
    assert svc.locked_until(session, "lockout", "one@example.com") is not None
    assert svc.locked_until(session, "lockout", "two@example.com") is None


def test_expired_lock_stops_blocking(session):
    _fail(session, "a@b.com", 5)
    row = (
        session.query(svc.AuthThrottle).filter_by(scope="lockout", identifier="a@b.com").first()
    )
    row.locked_until = datetime.now() - timedelta(minutes=1)
    session.commit()
    assert svc.locked_until(session, "lockout", "a@b.com") is None


def test_it_fails_open_rather_than_locking_everyone_out(session, monkeypatch):
    """A database problem must not become a total login outage.

    This is a deliberate trade-off: an outage removes brute-force protection
    rather than removing the ability to sign in. The edge layer is what should
    survive a database fault, not this.
    """
    def _boom(*a, **k):
        raise RuntimeError("database is down")

    monkeypatch.setattr(svc, "_row", _boom)
    assert svc.locked_until(session, "lockout", "a@b.com") is None
    assert svc.record_failure(
        session, "lockout", "a@b.com", max_attempts=5, window=WINDOW, lockout=LOCKOUT
    ) == 0


def test_purge_drops_only_stale_rows(session):
    _fail(session, "old@example.com")
    _fail(session, "new@example.com")
    old = session.query(svc.AuthThrottle).filter_by(identifier="old@example.com").first()
    old.updated_at = datetime.now() - timedelta(days=30)
    session.commit()

    assert svc.purge_expired(session, older_than=timedelta(days=7)) == 1
    assert session.query(svc.AuthThrottle).filter_by(identifier="new@example.com").first()
