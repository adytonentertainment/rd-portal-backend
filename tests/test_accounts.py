"""Role-based registration + admin approval + effective-admin gating."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.routers.auth import get_user
from app.routers import accounts as accounts_mod
from app.routers.accounts import accounts_router, admin_accounts_router
from app.utils.roles import is_effective_admin

STRONG_PW = "VeraxLocal123!"


@pytest.fixture()
def client(session, monkeypatch):
    # Registration is rate-limited in prod; neutralize it for the test.
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(accounts_mod, "check_rate_limit", _noop)
    monkeypatch.setenv("ADMIN_SIGNUP_CODE", "letmein")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@verax.app")

    app = FastAPI()
    app.include_router(accounts_router)
    app.include_router(admin_accounts_router)
    app.dependency_overrides[get_session] = lambda: session
    # Admin-workflow routes need an authenticated caller; tests that use them
    # set holder['user']. Registration routes ignore it.
    holder = {"user": None}
    app.dependency_overrides[get_user] = lambda: holder["user"]
    app._holder = holder
    return app, TestClient(app)


def _register(tc, email, role="writer", code=None, username=None):
    body = {"email": email, "username": username or email.split("@")[0], "password": STRONG_PW, "role": role}
    if code is not None:
        body["admin_code"] = code
    return tc.post("/auth/register", json=body)


# --- registration ------------------------------------------------------------

def test_register_writer(client, session):
    _app, tc = client
    r = _register(tc, "writer1@x.com", role="writer")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "writer"
    assert body["is_admin"] is False
    assert body["pending_admin_approval"] is False
    assert body["access_token"]
    u = session.query(User).filter(User.email == "writer1@x.com").one()
    assert u.role == "writer" and u.admin_approved is False


def test_admin_register_requires_code(client, session):
    _app, tc = client
    # no code
    assert _register(tc, "admin1@x.com", role="admin").status_code == 403
    # wrong code
    assert _register(tc, "admin1@x.com", role="admin", code="nope").status_code == 403
    assert session.query(User).filter(User.email == "admin1@x.com").first() is None


def test_admin_register_with_code_is_pending(client, session):
    _app, tc = client
    r = _register(tc, "newadmin@x.com", role="admin", code="letmein")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["role"] == "admin"
    assert body["admin_approved"] is False
    assert body["is_admin"] is False  # pending → not effective
    assert body["pending_admin_approval"] is True
    u = session.query(User).filter(User.email == "newadmin@x.com").one()
    assert is_effective_admin(u) is False


def test_bootstrap_admin_email_is_auto_approved(client, session):
    _app, tc = client
    r = _register(tc, "boss@verax.app", role="admin", code="letmein")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["admin_approved"] is True
    assert body["is_admin"] is True
    assert body["pending_admin_approval"] is False


def test_duplicate_email_and_username(client, session):
    _app, tc = client
    assert _register(tc, "dup@x.com").status_code == 201
    assert _register(tc, "dup@x.com").status_code == 409  # email
    assert _register(tc, "dup2@x.com", username="dup").status_code == 409  # username


def test_weak_password_rejected(client):
    _app, tc = client
    r = tc.post("/auth/register", json={"email": "w@x.com", "username": "weakpw", "password": "123", "role": "writer"})
    assert r.status_code == 422


# --- admin approval workflow -------------------------------------------------

def _make(session, email, role="writer", approved=False):
    u = User(email=email, username=email.split("@")[0], role=role,
             admin_approved=approved, activated=True, royalty_per_stream=0)
    session.add(u)
    session.commit()
    return u


def test_approve_pending_admin(client, session):
    app, tc = client
    approver = _make(session, "approver@x.com", role="admin", approved=True)
    pending = _make(session, "pending@x.com", role="admin", approved=False)
    app._holder["user"] = approver

    assert is_effective_admin(pending) is False
    r = tc.post(f"/admin/accounts/admins/{pending.id}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["is_admin"] is True
    session.refresh(pending)
    assert pending.admin_approved is True and is_effective_admin(pending) is True


def test_pending_admin_cannot_use_admin_routes(client, session):
    app, tc = client
    pending = _make(session, "pending2@x.com", role="admin", approved=False)
    app._holder["user"] = pending  # caller is the pending admin
    # require_admin must reject them
    assert tc.get("/admin/accounts/admins").status_code == 403


def test_list_and_revoke(client, session):
    app, tc = client
    approver = _make(session, "approver2@x.com", role="admin", approved=True)
    other = _make(session, "other@x.com", role="admin", approved=True)
    app._holder["user"] = approver

    listed = tc.get("/admin/accounts/admins").json()["admins"]
    assert {a["email"] for a in listed} >= {"approver2@x.com", "other@x.com"}

    pending_only = tc.get("/admin/accounts/admins?pending=true").json()["admins"]
    assert all(not a["effective"] for a in pending_only)

    r = tc.post(f"/admin/accounts/admins/{other.id}/revoke")
    assert r.status_code == 200
    session.refresh(other)
    assert other.admin_approved is False

    # can't revoke self
    assert tc.post(f"/admin/accounts/admins/{approver.id}/revoke").status_code == 409
