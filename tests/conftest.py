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
