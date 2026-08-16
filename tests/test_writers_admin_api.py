"""HTTP surface for the Writer/Client Manager admin API (Writer-Scale UX PRD,
Feature C): list/paginate/search, create, update, archive, contact-unlink,
admin invite-revoke, and the require_admin gate."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    BeneficiaryAccount,
    Catalog,
    Contact,
    ContactRole,
    Publisher,
    Writer,
    WriterContact,
    WriterKind,
    WriterStatus,
)
from app.routers.auth import get_user
from app.routers.writers_admin import writers_admin_router

ADMIN_EMAIL = "admin@verax.app"


@pytest.fixture()
def admin_user(session):
    user = User(email=ADMIN_EMAIL, username="wa-admin", royalty_per_stream=0)
    session.add(user)
    session.commit()
    return user


@pytest.fixture()
def non_admin_user(session):
    user = User(email="nobody@example.com", username="wa-nobody", royalty_per_stream=0)
    session.add(user)
    session.commit()
    return user


def _app(session, user, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    app = FastAPI()
    app.include_router(writers_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: user
    return TestClient(app)


@pytest.fixture()
def client(session, admin_user, monkeypatch):
    return _app(session, admin_user, monkeypatch)


@pytest.fixture()
def publisher(session):
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.commit()
    return pub


def _seed_writer(session, publisher, name, **kw):
    w = Writer(publisher_id=publisher.id, canonical_name=name, **kw)
    session.add(w)
    session.commit()
    session.refresh(w)
    return w


# --- list / pagination / search ---------------------------------------------

def test_list_paginates(client, session, publisher):
    for i in range(30):
        _seed_writer(session, publisher, f"Writer {i:02d}")
    r = client.get("/admin/writers", params={"page": 1, "page_size": 25})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 30
    assert body["page"] == 1
    assert body["page_size"] == 25
    assert len(body["items"]) == 25

    r2 = client.get("/admin/writers", params={"page": 2, "page_size": 25})
    assert len(r2.json()["items"]) == 5


def test_list_search_by_name(client, session, publisher):
    _seed_writer(session, publisher, "RedZed")
    _seed_writer(session, publisher, "Luna Negra")
    r = client.get("/admin/writers", params={"search": "redz"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["canonical_name"] == "RedZed"


def test_list_search_by_contact_email(client, session, publisher):
    w = _seed_writer(session, publisher, "Contactable")
    _seed_writer(session, publisher, "Other")
    contact = Contact(email="find.me@example.com")
    session.add(contact)
    session.flush()
    session.add(WriterContact(writer_id=w.id, contact_id=contact.id, role=ContactRole.PRIMARY))
    session.commit()
    r = client.get("/admin/writers", params={"search": "find.me"})
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == w.id
    assert body["items"][0]["primary_email"] == "find.me@example.com"


def test_list_filters_by_kind_and_status(client, session, publisher):
    _seed_writer(session, publisher, "A client", kind=WriterKind.CLIENT)
    _seed_writer(session, publisher, "A partner", kind=WriterKind.COMMISSION_PARTNER)
    _seed_writer(session, publisher, "Gone", status=WriterStatus.OFFBOARDED)
    assert client.get("/admin/writers", params={"kind": "commission_partner"}).json()["total"] == 1
    assert client.get("/admin/writers", params={"status": "offboarded"}).json()["total"] == 1


# --- detail ------------------------------------------------------------------

def test_get_writer_detail_includes_accounts_and_contacts(client, session, publisher):
    w = _seed_writer(session, publisher, "Detailed", kind=WriterKind.CLIENT)
    session.add(BeneficiaryAccount(writer_id=w.id, account_code="C00616", catalog=Catalog.YT))
    contact = Contact(email="primary@example.com")
    session.add(contact)
    session.flush()
    session.add(WriterContact(writer_id=w.id, contact_id=contact.id, role=ContactRole.PRIMARY))
    session.commit()

    r = client.get(f"/admin/writers/{w.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["canonical_name"] == "Detailed"
    assert body["kind"] == "client"
    assert len(body["accounts"]) == 1
    assert body["accounts"][0]["account_code"] == "C00616"
    assert body["accounts"][0]["catalog"] == "YT"
    assert len(body["contacts"]) == 1
    assert body["contacts"][0]["email"] == "primary@example.com"
    assert body["portal_status"] == "none"


def test_get_writer_404(client):
    assert client.get("/admin/writers/999").status_code == 404


# --- create ------------------------------------------------------------------

def test_create_writer(client, session):
    r = client.post("/admin/writers", json={
        "canonical_name": "New Signing",
        "payee_name": "New Signing LLC",
        "kind": "client",
        "expected_catalogs": ["MECH", "YT"],
        "preferred_language": "es",
        "cadence": "semiannual",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["canonical_name"] == "New Signing"
    assert body["kind"] == "client"
    assert body["expected_catalogs"] == ["MECH", "YT"]
    assert body["status"] == "active"
    # persisted
    assert session.query(Writer).filter(Writer.canonical_name == "New Signing").count() == 1


def test_create_writer_requires_name(client):
    r = client.post("/admin/writers", json={"canonical_name": "   "})
    assert r.status_code == 422


def test_create_writer_rejects_bad_enum(client):
    r = client.post("/admin/writers", json={"canonical_name": "X", "kind": "bogus"})
    assert r.status_code == 422


# --- update ------------------------------------------------------------------

def test_update_writer_fields(client, session, publisher):
    w = _seed_writer(session, publisher, "Editable")
    r = client.patch(f"/admin/writers/{w.id}", json={
        "payee_name": "Edited Payee",
        "kind": "commission_partner",
        "preferred_language": "en",
        "cadence": "quarterly",
        "expected_catalogs": ["PERF"],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["payee_name"] == "Edited Payee"
    assert body["kind"] == "commission_partner"
    assert body["cadence"] == "quarterly"
    assert body["expected_catalogs"] == ["PERF"]


def test_update_only_touches_sent_fields(client, session, publisher):
    w = _seed_writer(session, publisher, "Keep", payee_name="Original Payee")
    client.patch(f"/admin/writers/{w.id}", json={"preferred_language": "es"})
    session.refresh(w)
    assert w.payee_name == "Original Payee"  # untouched
    assert w.preferred_language == "es"


def test_update_rejects_empty_name(client, session, publisher):
    w = _seed_writer(session, publisher, "Named")
    assert client.patch(f"/admin/writers/{w.id}", json={"canonical_name": ""}).status_code == 422


# --- archive -----------------------------------------------------------------

def test_archive_writer_soft_removes(client, session, publisher):
    w = _seed_writer(session, publisher, "Leaving")
    r = client.post(f"/admin/writers/{w.id}/archive")
    assert r.status_code == 200
    assert r.json()["status"] == "offboarded"
    session.refresh(w)
    assert w.status == WriterStatus.OFFBOARDED


# --- contact unlink ----------------------------------------------------------

def test_unlink_contact(client, session, publisher):
    w = _seed_writer(session, publisher, "HasContact")
    contact = Contact(email="drop@example.com")
    session.add(contact)
    session.flush()
    link = WriterContact(writer_id=w.id, contact_id=contact.id, role=ContactRole.MANAGER)
    session.add(link)
    session.commit()

    r = client.delete(f"/admin/writers/{w.id}/contacts/{contact.id}")
    assert r.status_code == 200
    assert session.query(WriterContact).filter_by(writer_id=w.id, contact_id=contact.id).count() == 0
    # Contact row itself survives
    assert session.get(Contact, contact.id) is not None


def test_unlink_contact_404_when_not_linked(client, session, publisher):
    w = _seed_writer(session, publisher, "NoLink")
    assert client.delete(f"/admin/writers/{w.id}/contacts/12345").status_code == 404


# --- admin invite revoke -----------------------------------------------------

def test_admin_revoke_invite(client, session, publisher):
    from app.services.portal import invites as invite_svc

    w = _seed_writer(session, publisher, "Invited")
    invite, _raw = invite_svc.create_invite(session, w.id, "invitee@example.com")
    session.commit()

    r = client.post(f"/admin/writers/{w.id}/invites/{invite.id}/revoke")
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"
    session.refresh(invite)
    assert invite.revoked_at is not None


def test_admin_revoke_invite_wrong_writer_404(client, session, publisher):
    from app.services.portal import invites as invite_svc

    w1 = _seed_writer(session, publisher, "W1")
    w2 = _seed_writer(session, publisher, "W2")
    invite, _raw = invite_svc.create_invite(session, w1.id, "x@example.com")
    session.commit()
    assert client.post(f"/admin/writers/{w2.id}/invites/{invite.id}/revoke").status_code == 404


# --- require_admin gate ------------------------------------------------------

def test_non_admin_forbidden(session, non_admin_user, monkeypatch):
    c = _app(session, non_admin_user, monkeypatch)
    assert c.get("/admin/writers").status_code == 403
    assert c.post("/admin/writers", json={"canonical_name": "Nope"}).status_code == 403
