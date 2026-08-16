"""Invite emails actually go out — and a mail failure never costs the invite.

Before this, creating an invite returned a raw token that a human had to copy
into WhatsApp by hand. Now it is emailed, but the link is still returned and
the invite still stands if SMTP is down, because the token is valid either way.
"""

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import ContactRole, PortalInvite, Publisher, Writer
from app.routers.auth import get_user
from app.routers.portal import writer_invites_admin_router
from app.services.portal import invite_delivery
from app.services.portal.invites import create_invite

ADMIN_EMAIL = "invite-admin@verax.app"
WRITER_EMAIL = "songwriter@example.com"


@pytest.fixture()
def admin_user(session):
    u = User(email=ADMIN_EMAIL, username="invite-admin", royalty_per_stream=0)
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def writer(session):
    pub = session.query(Publisher).first() or Publisher(name="Regalias Digitales")
    session.add(pub)
    session.commit()
    w = Writer(canonical_name="Los Tucanes De Tijuana", publisher_id=pub.id)
    session.add(w)
    session.commit()
    return w


@pytest.fixture()
def client(session, admin_user, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    app = FastAPI()
    app.include_router(writer_invites_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


class _Recorder:
    """Stands in for the SMTP client."""

    def __init__(self, explode=None):
        self.sent = []
        self.explode = explode

    def send_portal_invite_email(self, **kwargs):
        if self.explode:
            raise self.explode
        self.sent.append(kwargs)


@pytest.fixture()
def mail(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(invite_delivery, "EMail", lambda: rec)
    return rec


@pytest.fixture(autouse=True)
def _same_session(session, monkeypatch):
    """The delivery task opens its own session in production; in tests point it
    at the test session so assertions see the committed status."""
    monkeypatch.setattr(invite_delivery, "SessionLocal", lambda: session)
    monkeypatch.setattr(session, "close", lambda: None)


def test_creating_an_invite_emails_the_recipient(client, session, writer, mail):
    r = client.post(f"/admin/writers/{writer.id}/invites", json={"email": WRITER_EMAIL})
    assert r.status_code == 201, r.text

    assert len(mail.sent) == 1, "no invite email was sent"
    sent = mail.sent[0]
    assert sent["recipient_email"] == WRITER_EMAIL
    assert sent["writer_name"] == "Los Tucanes De Tijuana"

    body = r.json()
    # The emailed link and the returned link must be the same one, or the admin
    # would hand out a link that differs from what the writer received.
    assert sent["accept_url"] == body["invite_url"]
    assert body["token"] in sent["accept_url"]

    invite = session.get(PortalInvite, body["invite_id"])
    session.refresh(invite)
    assert invite.delivery_status == "sent"
    assert invite.sent_at is not None


def test_the_link_points_at_the_frontend_accept_page(client, writer, mail):
    """/invite/:token is a React route. Pointing at the API would show JSON."""
    r = client.post(f"/admin/writers/{writer.id}/invites", json={"email": WRITER_EMAIL})
    url = r.json()["invite_url"]
    assert "/invite/" in url
    assert "/admin/" not in url and "/portal/invites/" not in url


def test_smtp_failure_does_not_cost_the_invite(client, session, writer, monkeypatch):
    """SMTP being down must not take the invite with it — the token is already
    valid and the admin can still copy the link."""
    monkeypatch.setattr(
        invite_delivery, "EMail",
        lambda: _Recorder(explode=OSError("Connection refused")),
    )
    r = client.post(f"/admin/writers/{writer.id}/invites", json={"email": WRITER_EMAIL})
    assert r.status_code == 201, "a mail failure must not fail invite creation"

    body = r.json()
    assert body["token"] and body["invite_url"]

    invite = session.get(PortalInvite, body["invite_id"])
    session.refresh(invite)
    assert invite.delivery_status == "failed"
    assert "Connection refused" in invite.delivery_error
    assert invite.is_active(datetime.now()), "the invite must still be usable"


def test_delivery_error_never_records_the_token(client, session, writer, monkeypatch):
    """The token is a bearer credential: whoever holds it claims the portal.
    An SMTP error that echoes the message body must not persist it."""
    holder = {}

    class _Leaky:
        def send_portal_invite_email(self, **kwargs):
            holder["url"] = kwargs["accept_url"]
            raise ValueError(f"552 message rejected: {kwargs['accept_url']}")

    monkeypatch.setattr(invite_delivery, "EMail", lambda: _Leaky())
    r = client.post(f"/admin/writers/{writer.id}/invites", json={"email": WRITER_EMAIL})
    token = r.json()["token"]

    invite = session.get(PortalInvite, r.json()["invite_id"])
    session.refresh(invite)
    assert token not in (invite.delivery_error or ""), "token leaked into the DB"


def test_resend_issues_a_fresh_link_and_kills_the_old_one(client, session, writer, mail):
    """Tokens are hashed, so the original cannot be re-sent — resend replaces
    it, which also revokes a link that may have leaked."""
    first = client.post(f"/admin/writers/{writer.id}/invites", json={"email": WRITER_EMAIL}).json()

    r = client.post(f"/admin/writers/{writer.id}/invites/{first['invite_id']}/resend")
    assert r.status_code == 202, r.text
    second = r.json()

    assert second["token"] != first["token"]
    assert second["replaces_invite_id"] == first["invite_id"]
    assert len(mail.sent) == 2

    old = session.get(PortalInvite, first["invite_id"])
    session.refresh(old)
    assert not old.is_active(datetime.now()), "the superseded link must stop working"


def test_resend_refuses_an_already_accepted_invite(client, session, writer, mail):
    body = client.post(f"/admin/writers/{writer.id}/invites", json={"email": WRITER_EMAIL}).json()
    invite = session.get(PortalInvite, body["invite_id"])
    invite.accepted_at = datetime.now()
    session.commit()

    r = client.post(f"/admin/writers/{writer.id}/invites/{body['invite_id']}/resend")
    assert r.status_code == 409


def test_listing_invites_shows_whether_the_email_landed(client, session, writer, mail):
    client.post(f"/admin/writers/{writer.id}/invites", json={"email": WRITER_EMAIL})
    rows = client.get(f"/admin/writers/{writer.id}/invites").json()
    assert rows[0]["delivery_status"] == "sent"
    assert rows[0]["sent_at"]


def test_existing_invites_are_not_reported_as_pending(session, writer):
    """Invites created before email delivery existed were shared as copied
    links. Calling those 'pending' would imply a send is still coming."""
    invite, _raw = create_invite(
        session, writer.id, "old@example.com", ContactRole.MANAGER,
        invited_by_user_id=None, is_admin_invite=True,
    )
    # simulates the server_default the migration backfills with
    session.execute(
        __import__("sqlalchemy").text(
            "UPDATE portal_invite SET delivery_status='not_sent' WHERE id=:i"
        ),
        {"i": invite.id},
    )
    session.commit()
    session.refresh(invite)
    assert invite.delivery_status == "not_sent"
