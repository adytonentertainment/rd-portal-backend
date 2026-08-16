"""Dropbox-style portal sharing: admin bootstrap, self-serve share, accept,
and tenancy isolation (infra PRD §7.2, §7.3)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    BeneficiaryAccount,
    Catalog,
    Contact,
    Publisher,
    Writer,
    WriterContact,
)
from app.routers.auth import get_user
from app.routers.portal import me_router, portal_router, writer_invites_admin_router

ADMIN_EMAIL = "admin@verax.app"


@pytest.fixture()
def admin_user(session):
    u = User(email=ADMIN_EMAIL, username="admin", royalty_per_stream=0)
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def seed(session):
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()
    w = Writer(publisher_id=pub.id, canonical_name="RedZed", kind=None)
    other = Writer(publisher_id=pub.id, canonical_name="Someone Else")
    session.add_all([w, other])
    session.flush()
    session.add(BeneficiaryAccount(writer_id=w.id, account_code="C00616", catalog=Catalog.YT))
    session.commit()
    return {"writer_id": w.id, "other_id": other.id}


def _app(session, current_user_holder):
    """App whose get_user returns whatever holder['user'] currently is, so a
    test can 'log in' as different people by swapping the holder."""
    app = FastAPI()
    app.include_router(writer_invites_admin_router)
    app.include_router(me_router)
    app.include_router(portal_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: current_user_holder["user"]
    return TestClient(app)


def test_full_share_lifecycle(session, admin_user, seed, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    holder = {"user": admin_user}
    client = _app(session, holder)
    wid = seed["writer_id"]

    # 1) Admin bootstraps: invites the writer's own email.
    r = client.post(f"/admin/writers/{wid}/invites",
                    json={"email": "redzed@x.com", "role": "primary"})
    assert r.status_code == 201, r.text
    token = r.json()["token"]

    # 2) Public preview shows it needs a password (no login yet).
    pv = client.get(f"/portal/invites/{token}").json()
    assert pv["writer_name"] == "RedZed" and pv["needs_password"] is True

    # 3) Accept -> creates login + link, returns a session + the writer.
    a = client.post("/portal/accept-invite",
                    json={"token": token, "password": "hunter2hunter2"})
    assert a.status_code == 200, a.text
    assert [w["name"] for w in a.json()["writers"]] == ["RedZed"]
    redzed_user = session.query(User).filter(User.email == "redzed@x.com").one()

    # token is single-use: second accept fails
    assert client.post("/portal/accept-invite",
                       json={"token": token, "password": "x"}).status_code == 400

    # 4) Now logged in AS redzed, share with a manager (Dropbox action).
    holder["user"] = redzed_user
    s = client.post(f"/me/writers/{wid}/invites", json={"email": "mgr@x.com"})
    assert s.status_code == 201, s.text
    mgr_token = s.json()["token"]

    # redzed sees the writer; the pending invite shows in the members panel
    assert [w["name"] for w in client.get("/me/writers").json()] == ["RedZed"]
    panel = client.get(f"/me/writers/{wid}/members").json()
    assert any(p["email"] == "mgr@x.com" for p in panel["pending_invites"])

    # 5) Manager accepts -> also sees RedZed.
    holder["user"] = admin_user  # accept is public; user irrelevant
    am = client.post("/portal/accept-invite",
                     json={"token": mgr_token, "password": "managerpass1"})
    assert am.status_code == 200
    assert [w["name"] for w in am.json()["writers"]] == ["RedZed"]

    # both contacts now linked to the writer
    assert session.query(WriterContact).filter(WriterContact.writer_id == wid).count() == 2


def test_cannot_share_or_see_writer_without_access(session, admin_user, seed):
    # A contact linked only to 'other' writer cannot touch RedZed.
    other_user = User(email="stranger@x.com", username="stranger", royalty_per_stream=0)
    session.add(other_user)
    session.flush()
    c = Contact(email="stranger@x.com", user_id=other_user.id)
    session.add(c)
    session.flush()
    session.add(WriterContact(writer_id=seed["other_id"], contact_id=c.id))
    session.commit()

    holder = {"user": other_user}
    client = _app(session, holder)
    wid = seed["writer_id"]

    # not in their /me/writers
    assert all(w["id"] != wid for w in client.get("/me/writers").json())
    # can't view members (404, not 403 — existence hidden)
    assert client.get(f"/me/writers/{wid}/members").status_code == 404
    # can't share access they don't have
    assert client.post(f"/me/writers/{wid}/invites",
                       json={"email": "x@y.com"}).status_code == 404


def test_no_portal_access_is_403(session, seed):
    # A plain User with no Contact row gets 403 from /me.
    u = User(email="nobody@x.com", username="nobody", royalty_per_stream=0)
    session.add(u)
    session.commit()
    client = _app(session, {"user": u})
    assert client.get("/me").status_code == 403


def test_admin_invite_duplicate_access_conflicts(session, admin_user, seed, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    client = _app(session, {"user": admin_user})
    wid = seed["writer_id"]
    t = client.post(f"/admin/writers/{wid}/invites", json={"email": "a@x.com"}).json()["token"]
    client.post("/portal/accept-invite", json={"token": t, "password": "passpasspass"})
    # inviting an email that already has access is a 409
    r = client.post(f"/admin/writers/{wid}/invites", json={"email": "a@x.com"})
    assert r.status_code == 409


# --- invite links must never authenticate an existing account ----------------

def _accept_app(session):
    """Public app: no get_user override, so /portal/* is genuinely unauthenticated."""
    app = FastAPI()
    app.include_router(portal_router)
    app.include_router(writer_invites_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _issue_invite(session, writer_id, email):
    from app.routers.portal import bcrypt_context  # noqa: F401  (parity with route)
    from app.services.portal import invites as invite_svc

    _inv, raw = invite_svc.create_invite(session, writer_id, email, is_admin_invite=True)
    return raw


def test_invite_link_cannot_take_over_an_existing_account(session, seed):
    """THE takeover: holding a link for an email that already has a login must
    NOT mint a session. Anyone forwarded the link (or reading it out of a log)
    would otherwise become that user — including an admin."""
    from app.routers.auth import bcrypt_context

    victim = User(
        email="victim@example.com",
        username="victim",
        hashed_password=bcrypt_context.hash("the-real-password"),
        royalty_per_stream=0,
    )
    session.add(victim)
    session.commit()

    raw = _issue_invite(session, seed["writer_id"], "victim@example.com")
    client = TestClient(_accept_app(session))

    # no password at all -> refused
    r = client.post("/portal/accept-invite", json={"token": raw})
    assert r.status_code == 401, r.text
    assert "access_token" not in r.json()

    # wrong password -> refused
    r = client.post("/portal/accept-invite", json={"token": raw, "password": "guess"})
    assert r.status_code == 401, r.text
    assert "access_token" not in r.json()

    # the invite must still be unaccepted and grant no access
    from app.models.statements import PortalInvite

    inv = session.query(PortalInvite).filter(PortalInvite.email == "victim@example.com").one()
    assert inv.accepted_at is None
    assert session.query(WriterContact).count() == 0

    # correct password -> accepted
    r = client.post(
        "/portal/accept-invite", json={"token": raw, "password": "the-real-password"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


def test_invite_accept_creates_login_for_a_brand_new_email(session, seed):
    """The onboarding path is unchanged: an email with no account sets one up."""
    raw = _issue_invite(session, seed["writer_id"], "newcomer@example.com")
    client = TestClient(_accept_app(session))

    # still requires a password to create the login
    assert client.post("/portal/accept-invite", json={"token": raw}).status_code == 400

    r = client.post(
        "/portal/accept-invite", json={"token": raw, "password": "chosen-password"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]
    assert session.query(User).filter(User.email == "newcomer@example.com").one()


def test_signed_in_user_accepts_without_password(session, seed):
    """Proof of ownership can also be an existing session — the only path open
    to Google/OAuth accounts, which have no local password."""
    from app.routers.portal import _mint_token

    oauth_user = User(
        email="google@example.com", username="googler",
        hashed_password=None, royalty_per_stream=0,
    )
    session.add(oauth_user)
    session.commit()

    raw = _issue_invite(session, seed["writer_id"], "google@example.com")
    client = TestClient(_accept_app(session))

    # no password and no session -> refused (nothing to verify against)
    assert client.post("/portal/accept-invite", json={"token": raw}).status_code == 401

    # signed in as that same email -> accepted
    r = client.post(
        "/portal/accept-invite",
        json={"token": raw},
        headers={"Authorization": f"Bearer {_mint_token(oauth_user)}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


def test_someone_elses_session_does_not_accept_your_invite(session, seed):
    """Being signed in as a DIFFERENT account is not proof of owning this one."""
    from app.routers.auth import bcrypt_context
    from app.routers.portal import _mint_token

    victim = User(email="victim2@example.com", username="victim2",
                  hashed_password=bcrypt_context.hash("pw"), royalty_per_stream=0)
    attacker = User(email="attacker@example.com", username="attacker",
                    hashed_password=bcrypt_context.hash("pw"), royalty_per_stream=0)
    session.add_all([victim, attacker])
    session.commit()

    raw = _issue_invite(session, seed["writer_id"], "victim2@example.com")
    client = TestClient(_accept_app(session))

    r = client.post(
        "/portal/accept-invite",
        json={"token": raw},
        headers={"Authorization": f"Bearer {_mint_token(attacker)}"},
    )
    assert r.status_code == 401, r.text
