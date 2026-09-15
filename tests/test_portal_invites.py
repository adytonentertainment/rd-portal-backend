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


def test_invite_link_alone_cannot_act_as_an_existing_account(session, seed):
    """THE takeover.

    One address has one login, so accepting a second invite ADDS a client to an
    account that already exists. That is exactly why the link cannot be enough
    on its own: anyone forwarded it (or reading it out of a log) would otherwise
    attach themselves to somebody's account and see their royalties. Proof of
    the account is required first.
    """
    from app.routers.auth import bcrypt_context
    from app.services.portal import invites as invite_svc

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

    # no password -> refused, nothing granted
    r = client.post("/portal/accept-invite", json={"token": raw})
    assert r.status_code == 401, r.text
    assert "access_token" not in r.json()

    # wrong password -> refused
    r = client.post("/portal/accept-invite", json={"token": raw, "password": "guess"})
    assert r.status_code == 401, r.text
    assert invite_svc.writer_ids_for_user(session, victim) == []

    # the invite is still unaccepted and no link was made
    from app.models.statements import PortalInvite

    inv = session.query(PortalInvite).filter(PortalInvite.email == "victim@example.com").one()
    assert inv.accepted_at is None
    assert session.query(WriterContact).count() == 0

    # correct password -> the client is added to the SAME login
    r = client.post(
        "/portal/accept-invite", json={"token": raw, "password": "the-real-password"}
    )
    assert r.status_code == 200, r.text
    assert session.query(User).filter(User.email == "victim@example.com").count() == 1
    assert invite_svc.writer_ids_for_user(session, victim) == [seed["writer_id"]]


def test_a_claim_gets_the_username_it_asks_for(session, seed):
    """The username is what tells one portal from another at the login screen,
    so the person claiming picks it."""
    raw = _issue_invite(session, seed["writer_id"], "mgr@example.com")
    client = TestClient(_accept_app(session))
    r = client.post(
        "/portal/accept-invite",
        json={"token": raw, "password": "hunter2hunter2", "username": "redzed_mgr"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "redzed_mgr"


def test_a_taken_username_is_refused_so_the_form_can_ask_again(session, seed):
    session.add(User(email="someone@else.com", username="taken_name", royalty_per_stream=0))
    session.commit()
    raw = _issue_invite(session, seed["writer_id"], "mgr2@example.com")
    client = TestClient(_accept_app(session))
    r = client.post(
        "/portal/accept-invite",
        json={"token": raw, "password": "hunter2hunter2", "username": "taken_name"},
    )
    assert r.status_code == 409
    assert "taken" in r.json()["detail"]


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


def test_being_signed_in_does_not_replace_the_password(session, seed):
    """Proof-of-ownership used to let a signed-in user accept without typing a
    password. There is nothing to prove now — the acceptance builds a new login
    for this client — so a password is required either way."""
    raw = _issue_invite(session, seed["writer_id"], "already@example.com")
    client = TestClient(_accept_app(session))

    r = client.post("/portal/accept-invite", json={"token": raw})
    assert r.status_code == 400
    assert "password" in r.json()["detail"].lower()


def test_one_link_claims_one_client_and_cannot_be_reused(session, seed):
    """The link is good for the client it names, once. A second acceptance of
    the same token is refused, so a forwarded link cannot quietly add a second
    portal to anyone."""
    raw = _issue_invite(session, seed["writer_id"], "once@example.com")
    client = TestClient(_accept_app(session))

    first = client.post(
        "/portal/accept-invite",
        json={"token": raw, "password": "hunter2hunter2", "username": "once_claim"},
    )
    assert first.status_code == 200
    assert [w["name"] for w in first.json()["writers"]] == ["RedZed"]

    again = client.post(
        "/portal/accept-invite",
        json={"token": raw, "password": "hunter2hunter2", "username": "once_claim_2"},
    )
    assert again.status_code == 400


# --- an existing login must be recognised whatever case it was typed in ------
#
# Invites lowercase the address; signup stores whatever the person typed. A
# user registered as "Name@example.com" was therefore invisible to a lookup for
# "name@example.com", so the invite screen decided they had no login and asked
# them to choose a password for an account they already had. Reported twice.


def test_an_existing_login_is_found_regardless_of_email_case(session):
    from app.models.models import User
    from app.services.portal.invites import user_by_email

    session.add(User(email="Manuel.Eanz@Example.COM", username="manuel", royalty_per_stream=0))
    session.commit()

    for typed in ("manuel.eanz@example.com", "MANUEL.EANZ@EXAMPLE.COM", " Manuel.Eanz@Example.com "):
        found = user_by_email(session, typed)
        assert found is not None, f"existing login not found for {typed!r}"
        assert found.username == "manuel"


def test_unknown_address_still_has_no_login(session):
    from app.services.portal.invites import user_by_email

    assert user_by_email(session, "nobody@example.com") is None
    assert user_by_email(session, "") is None
    assert user_by_email(session, None) is None


def test_case_duplicates_resolve_to_the_same_account_every_time(session):
    """If two rows already differ only by case, the choice must be stable —
    alternating between them would hand the same person different portals."""
    from app.models.models import User
    from app.services.portal.invites import user_by_email

    session.add(User(email="dup@example.com", username="first", royalty_per_stream=0))
    session.commit()
    session.add(User(email="DUP@example.com", username="second", royalty_per_stream=0))
    session.commit()

    picks = {user_by_email(session, "dup@example.com").username for _ in range(5)}
    assert picks == {"first"}
