"""Inviting a whole roster in one action (portal onboarding).

Doing this client-by-client is hundreds of dialogs, and the clicking is where
the mistakes are. The batch has to be safe to re-run: everything it declines to
do comes back as a reason, and nobody gets mailed twice for the same catalog.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    Contact,
    ContactRole,
    Publisher,
    PortalInvite,
    Writer,
    WriterContact,
    WriterStatus,
)
from app.routers.auth import get_user
from app.routers.portal import portal_router, writer_invites_admin_router
from app.services.portal import invite_delivery

ADMIN_EMAIL = "admin@verax.app"


@pytest.fixture(autouse=True)
def no_pacing(monkeypatch):
    """The real batch sleeps between messages to stay inside provider rate
    limits. Tests should not."""
    monkeypatch.setattr(invite_delivery, "SEND_PACING_SECONDS", 0)


@pytest.fixture()
def admin_user(session, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    u = User(email=ADMIN_EMAIL, username="admin", royalty_per_stream=0)
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def client(session, admin_user):
    app = FastAPI()
    app.include_router(writer_invites_admin_router)
    app.include_router(portal_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


@pytest.fixture()
def publisher(session):
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.commit()
    return pub


def make_writer(session, publisher, name, email=None, role=ContactRole.PRIMARY,
                has_login=False, **kwargs):
    w = Writer(publisher_id=publisher.id, canonical_name=name, **kwargs)
    session.add(w)
    session.flush()
    if email:
        user = None
        if has_login:
            user = User(email=email, username=name.lower(), royalty_per_stream=0)
            session.add(user)
            session.flush()
        c = Contact(email=email, user_id=user.id if user else None)
        session.add(c)
        session.flush()
        session.add(WriterContact(writer_id=w.id, contact_id=c.id, role=role))
    session.commit()
    return w


def bulk(client, writer_ids, **kw):
    return client.post("/admin/writers/bulk-invite",
                       json={"writer_ids": writer_ids, **kw})


def test_invites_every_selected_client_with_an_email(client, session, publisher):
    a = make_writer(session, publisher, "Amenazzy", "a@x.com")
    b = make_writer(session, publisher, "Canserbero", "b@x.com")

    res = bulk(client, [a.id, b.id])
    assert res.status_code == 200
    body = res.json()

    assert body["requested"] == 2
    assert {q["email"] for q in body["queued"]} == {"a@x.com", "b@x.com"}
    assert body["skipped"] == []
    assert session.query(PortalInvite).count() == 2
    # the background send ran with the null mailer from conftest
    assert all(i.delivery_status == "sent" for i in session.query(PortalInvite))


def test_one_email_per_client_even_with_several_contacts(client, session, publisher):
    """A manager and an attorney on one catalog is normal. Bulk must not mail
    three people about one client — the primary is the one who gets it."""
    w = make_writer(session, publisher, "Lupita Vega", "manager@x.com",
                    role=ContactRole.MANAGER)
    primary = Contact(email="lupita@x.com")
    legal = Contact(email="legal@x.com")
    session.add_all([primary, legal])
    session.flush()
    session.add_all([
        WriterContact(writer_id=w.id, contact_id=primary.id, role=ContactRole.PRIMARY),
        WriterContact(writer_id=w.id, contact_id=legal.id, role=ContactRole.LEGAL),
    ])
    session.commit()

    body = bulk(client, [w.id]).json()
    assert [q["email"] for q in body["queued"]] == ["lupita@x.com"]
    assert session.query(PortalInvite).count() == 1


def test_falls_back_to_the_only_contact_when_none_is_primary(client, session, publisher):
    w = make_writer(session, publisher, "Gerencia", "mgr@x.com", role=ContactRole.MANAGER)
    body = bulk(client, [w.id]).json()
    assert [q["email"] for q in body["queued"]] == ["mgr@x.com"]


def test_skips_are_the_work_list(client, session, publisher):
    """"412 invited" is a useless answer when 90 had no address on file. Each
    skip has to say which problem to go fix."""
    ok = make_writer(session, publisher, "Amenazzy", "a@x.com")
    no_email = make_writer(session, publisher, "No Contact")
    house = make_writer(session, publisher, "Regalias Digitales", "house@x.com",
                        is_house_account=True)
    gone = make_writer(session, publisher, "Old Client", "old@x.com",
                       status=WriterStatus.OFFBOARDED)
    live = make_writer(session, publisher, "Already In", "in@x.com", has_login=True)

    body = bulk(client, [ok.id, no_email.id, house.id, gone.id, live.id, 9999]).json()

    assert [q["writer_id"] for q in body["queued"]] == [ok.id]
    reasons = {s["writer_id"]: s["reason"] for s in body["skipped"]}
    assert reasons[no_email.id] == "no email on file"
    assert reasons[house.id] == "house account"
    assert reasons[gone.id] == "offboarded"
    assert reasons[live.id] == "portal already active"
    assert reasons[9999] == "not found"
    # only the one invitable client was actually mailed
    assert session.query(PortalInvite).count() == 1


def test_rerunning_does_not_mail_a_pending_invite_twice(client, session, publisher):
    """The batch is meant to be re-run after filling in missing addresses."""
    w = make_writer(session, publisher, "Amenazzy", "a@x.com")
    first = bulk(client, [w.id]).json()
    assert len(first["queued"]) == 1

    again = bulk(client, [w.id]).json()
    assert again["queued"] == []
    assert again["skipped"][0]["reason"] == "invite already pending"
    assert session.query(PortalInvite).count() == 1


def test_resend_pending_reissues_the_link(client, session, publisher):
    """For the spam-folder case: a fresh invite replaces the old one, and the
    superseded link stops working."""
    w = make_writer(session, publisher, "Amenazzy", "a@x.com")
    bulk(client, [w.id])
    body = bulk(client, [w.id], resend_pending=True).json()

    assert len(body["queued"]) == 1
    invites = session.query(PortalInvite).order_by(PortalInvite.id).all()
    assert len(invites) == 2
    assert invites[0].revoked_at is not None
    assert invites[1].revoked_at is None


def test_response_carries_no_tokens(client, session, publisher):
    """Hundreds of live bearer credentials in one payload is not a thing to
    hand a browser. Per-client links stay in the per-client dialog."""
    w = make_writer(session, publisher, "Amenazzy", "a@x.com")
    body = bulk(client, [w.id]).json()
    assert "token" not in str(body)
    assert "invite_url" not in body["queued"][0]


def test_empty_and_oversized_selections_are_refused(client, session, publisher):
    assert bulk(client, []).status_code == 422
    assert bulk(client, list(range(1, 502))).status_code == 422


def test_batch_gives_up_when_every_message_is_failing(session, publisher, monkeypatch):
    """Bad credentials fail all 800 identically. Grinding through them wastes
    ten minutes and buries the real cause under 800 identical rows."""
    writers = [make_writer(session, publisher, f"W{i}", f"w{i}@x.com") for i in range(12)]
    pairs = []
    from app.services.portal import invites as invite_svc
    for w in writers:
        inv, raw = invite_svc.create_invite(session, w.id, f"w{w.id}@x.com")
        pairs.append((inv.id, raw))

    class _Broken:
        def send_portal_invite_email(self, **kwargs):
            raise RuntimeError("535 Authentication credentials invalid")

    monkeypatch.setattr(invite_delivery, "EMail", _Broken)

    summary = invite_delivery.send_invite_emails(pairs)

    assert summary["failed"] == invite_delivery.MAX_CONSECUTIVE_FAILURES
    assert summary["not_attempted"] == len(pairs) - invite_delivery.MAX_CONSECUTIVE_FAILURES

    statuses = [session.get(PortalInvite, i).delivery_status for i, _ in pairs]
    assert statuses[:5] == ["failed"] * 5
    # the untried rows say so instead of reading "Sending…" forever
    assert set(statuses[5:]) == {"not_sent"}
    assert "still valid" in session.get(PortalInvite, pairs[-1][0]).delivery_error


def test_one_bad_address_does_not_stop_the_batch(session, publisher, monkeypatch):
    """A single dead mailbox is not a reason to strand the other 400."""
    writers = [make_writer(session, publisher, f"W{i}", f"w{i}@x.com") for i in range(4)]
    from app.services.portal import invites as invite_svc
    pairs = [invite_svc.create_invite(session, w.id, f"w{w.id}@x.com") for w in writers]
    pairs = [(inv.id, raw) for inv, raw in pairs]

    class _OneBad:
        calls = 0

        def send_portal_invite_email(self, **kwargs):
            _OneBad.calls += 1
            if _OneBad.calls == 2:
                raise RuntimeError("550 mailbox unavailable")

    monkeypatch.setattr(invite_delivery, "EMail", _OneBad)

    summary = invite_delivery.send_invite_emails(pairs)
    assert summary == {"total": 4, "sent": 3, "failed": 1, "not_attempted": 0}
