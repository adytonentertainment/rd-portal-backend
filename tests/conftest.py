import os

# Must be set before any app import — settings resolve the env file from this.
os.environ.setdefault("ENVIRONMENT", "DEVELOPMENT")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
import app.models.models  # noqa: F401 — registers all tables (incl. statements) on Base


@pytest.fixture()
def engine(tmp_path):
    """Scratch SQLite engine with the full schema — never the dev Postgres."""
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def session(engine):
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = TestSession()
    yield db
    db.rollback()
    db.close()


@pytest.fixture(autouse=True)
def no_real_email(session, monkeypatch):
    """No test may send a real email or write to the real database.

    Invite delivery runs in a BackgroundTask that deliberately opens its own
    SessionLocal, because in production the request's session is already closed
    by then. Left alone under test that reaches the developer's actual database
    and the live SMTP server — creating an invite in a test would genuinely mail
    somebody. Point both at the scratch session and a no-op mailer.

    A test that wants to assert on delivery overrides these itself; its own
    monkeypatch runs after this fixture and wins.
    """
    from app.services.portal import invite_delivery

    class _NullMailer:
        def send_portal_invite_email(self, **kwargs):
            return None

    monkeypatch.setattr(invite_delivery, "SessionLocal", lambda: session)
    monkeypatch.setattr(invite_delivery, "EMail", _NullMailer)
    monkeypatch.setattr(session, "close", lambda: None)


@pytest.fixture()
def grant_access(session):
    """Give a contact real portal access to a writer, the way production does.

    Being LINKED to a writer only means the client is contactable — an admin
    can record an email without inviting anyone. Access is granted by accepting
    an invite, so a fixture standing in for a live portal user has to record
    that acceptance too, or it is describing a state the app never produces.
    """
    from datetime import datetime

    from app.models.statements import PortalInvite

    def _grant(writer_id, email, accepted_at=None):
        invite = PortalInvite(
            writer_id=writer_id,
            email=email,
            token_hash=f"test-{writer_id}-{email}",
            expires_at=datetime.now(),
            accepted_at=accepted_at or datetime.now(),
        )
        session.add(invite)
        session.flush()
        return invite

    return _grant
