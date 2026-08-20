"""One email address, several clients — they must stay separate.

A manager or an attorney represents more than one writer, so the same address
legitimately appears on several clients. What must NOT happen is the second
client being absorbed into the first: one invite standing in for both, or
access to one quietly carrying access to the other.

Every (client, email) pair is its own grant, with its own invite and its own
email.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    Cadence,
    Contact,
    ContactRole,
    PortalInvite,
    Publisher,
    Writer,
    WriterContact,
    WriterKind,
)
from app.routers.auth import bcrypt_context, get_user
from app.routers.portal import portal_router, writer_invites_admin_router
from app.services.portal import invites as invite_svc

ADMIN_EMAIL = "admin@verax.app"
SHARED = "manager@label.com"


@pytest.fixture()
def admin_user(session, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    u = User(email=ADMIN_EMAIL, username="admin", royalty_per_stream=0)
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def two_writers(session):
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()
    a = Writer(publisher_id=pub.id, canonical_name="Amenazzy", kind=WriterKind.CLIENT,
               expected_catalogs=["YT"], cadence=Cadence.SEMIANNUAL)
    b = Writer(publisher_id=pub.id, canonical_name="Canserbero", kind=WriterKind.CLIENT,
               expected_catalogs=["YT"], cadence=Cadence.SEMIANNUAL)
    session.add_all([a, b])
    session.commit()
    return a, b


@pytest.fixture()
def client(session, admin_user):
    app = FastAPI()
    app.include_router(writer_invites_admin_router)
    app.include_router(portal_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


@pytest.fixture()
def sent_emails(monkeypatch, session):
    """Capture one entry per email actually put on the wire."""
    box = []
    from app.services.portal import invite_delivery

    class _Recorder:
        def send_portal_invite_email(self, **kw):
            box.append(kw)

    monkeypatch.setattr(invite_delivery, "EMail", _Recorder)
    monkeypatch.setattr(invite_delivery, "SessionLocal", lambda: session)
    monkeypatch.setattr(session, "close", lambda: None)
    return box


def test_the_same_address_gets_its_own_invite_per_client(client, session, two_writers, sent_emails):
    a, b = two_writers
    r1 = client.post(f"/admin/writers/{a.id}/invites", json={"email": SHARED, "role": "manager"})
    r2 = client.post(f"/admin/writers/{b.id}/invites", json={"email": SHARED, "role": "manager"})
    assert r1.status_code == 201 and r2.status_code == 201

    # two distinct invites, two distinct tokens — neither stands in for the other
    assert r1.json()["invite_id"] != r2.json()["invite_id"]
    assert r1.json()["token"] != r2.json()["token"]

    # and two separate emails went out, each naming its own client
    assert len(sent_emails) == 2
    assert {e["writer_name"] for e in sent_emails} == {"Amenazzy", "Canserbero"}
    assert [e["recipient_email"] for e in sent_emails] == [SHARED, SHARED]


def test_accepting_one_client_does_not_open_the_other(client, session, two_writers, sent_emails):
    """The merge that must not happen: accept Amenazzy's invite and Canserbero
    comes along with it."""
    a, b = two_writers
    tok_a = client.post(f"/admin/writers/{a.id}/invites",
                        json={"email": SHARED, "role": "manager"}).json()["token"]
    client.post(f"/admin/writers/{b.id}/invites", json={"email": SHARED, "role": "manager"})

    accepted = client.post("/portal/accept-invite",
                           json={"token": tok_a, "password": "hunter2hunter2"})
    assert accepted.status_code == 200
    assert [w["name"] for w in accepted.json()["writers"]] == ["Amenazzy"]

    contact = session.query(Contact).filter(Contact.email == SHARED).one()
    assert invite_svc.writer_ids_for_contact(session, contact) == [a.id]

    # Canserbero's invite is still pending — untouched by the other acceptance
    inv_b = session.query(PortalInvite).filter(
        PortalInvite.writer_id == b.id, PortalInvite.email == SHARED).one()
    assert inv_b.accepted_at is None


def test_the_second_client_is_a_separate_login(client, session, two_writers, sent_emails):
    """One inbox, two clients, two logins — the whole point of the change.

    The manager claims each portal with its own username and password. Signing
    into one shows that client alone; there is no single account holding both,
    so no screen ever puts one client's royalties next to another's.
    """
    a, b = two_writers
    tok_a = client.post(f"/admin/writers/{a.id}/invites",
                        json={"email": SHARED, "role": "manager"}).json()["token"]
    tok_b = client.post(f"/admin/writers/{b.id}/invites",
                        json={"email": SHARED, "role": "manager"}).json()["token"]

    first = client.post("/portal/accept-invite", json={
        "token": tok_a, "password": "amenazzy-pass-1", "username": "amenazzy_mgr"})
    second = client.post("/portal/accept-invite", json={
        "token": tok_b, "password": "canserbero-pass-2", "username": "canserbero_mgr"})
    assert first.status_code == 200 and second.status_code == 200, second.text

    # two distinct logins behind one address
    users = session.query(User).filter(User.email == SHARED).all()
    assert {u.username for u in users} == {"amenazzy_mgr", "canserbero_mgr"}
    assert len(users) == 2

    # each one reaches exactly its own client
    by_name = {u.username: u for u in users}
    assert invite_svc.writer_ids_for_user(session, by_name["amenazzy_mgr"]) == [a.id]
    assert invite_svc.writer_ids_for_user(session, by_name["canserbero_mgr"]) == [b.id]

    # and each session lists only that client
    assert [w["name"] for w in first.json()["writers"]] == ["Amenazzy"]
    assert [w["name"] for w in second.json()["writers"]] == ["Canserbero"]


def test_the_claim_form_offers_the_client_name_as_the_username(client, session, two_writers):
    """Defaulted to the client, because that is what tells two portals apart at
    the login screen — two variations on your own address do not."""
    a, _ = two_writers
    tok = client.post(f"/admin/writers/{a.id}/invites",
                      json={"email": SHARED, "role": "manager"}).json()["token"]
    preview = client.get(f"/portal/invites/{tok}").json()
    assert preview["writer_name"] == "Amenazzy"
    assert preview["suggested_username"] == "amenazzy"


def test_recording_the_address_on_a_second_client_grants_nothing(client, session, two_writers):
    """An admin typing the address into the other client's contact field is a
    note, not an admission — and must not ride in on the first acceptance."""
    a, b = two_writers
    tok_a = client.post(f"/admin/writers/{a.id}/invites",
                        json={"email": SHARED, "role": "manager"}).json()["token"]
    client.post("/portal/accept-invite", json={"token": tok_a, "password": "hunter2hunter2"})

    contact = session.query(Contact).filter(Contact.email == SHARED).one()
    session.add(WriterContact(writer_id=b.id, contact_id=contact.id, role=ContactRole.MANAGER))
    session.commit()

    assert invite_svc.writer_ids_for_contact(session, contact) == [a.id]


def test_claiming_one_client_leaves_the_next_one_invitable(client, session, two_writers, sent_emails):
    """The exact sequence that kept reading wrong.

    Invite an address to Amenazzy, claim it, then invite the SAME address to
    Canserbero. Canserbero must read as un-invited, be invitable in bulk, and
    show no login of its own — every one of those questions used to be answered
    from `Contact.user_id`, which names the first client claimed and therefore
    said "active" for every client that address was merely listed on.
    """
    from app.routers.writers_admin import _portal_status_for

    a, b = two_writers
    tok_a = client.post(f"/admin/writers/{a.id}/invites",
                        json={"email": SHARED, "role": "manager"}).json()["token"]
    claimed = client.post("/portal/accept-invite", json={
        "token": tok_a, "password": "amenazzy-pass", "username": "amenazzy_mgr"})
    assert claimed.status_code == 200

    # now record + invite the same address on the second client
    r = client.post(f"/admin/writers/{b.id}/invites", json={"email": SHARED, "role": "manager"})
    assert r.status_code == 201, r.text        # NOT "already has access"

    b_links = (
        session.query(WriterContact, Contact)
        .join(Contact, WriterContact.contact_id == Contact.id)
        .filter(WriterContact.writer_id == b.id)
        .all()
    )
    b_invites = session.query(PortalInvite).filter(PortalInvite.writer_id == b.id).all()

    # the badge answers for Canserbero: invited, not active
    assert _portal_status_for(b_links, b_invites) == "invited"
    # and no login has claimed it
    assert all(link.user_id is None for link, _ in b_links)

    # and with the address recorded on the second client the way an admin adds
    # it, bulk invite offers it rather than skipping it as "already active"
    contact = session.query(Contact).filter(Contact.email == SHARED).one()
    session.add(WriterContact(writer_id=b.id, contact_id=contact.id,
                              role=ContactRole.MANAGER))
    session.commit()

    res = client.post("/admin/writers/bulk-invite",
                      json={"writer_ids": [b.id], "resend_pending": True}).json()
    assert [q["writer_id"] for q in res["queued"]] == [b.id], res["skipped"]
