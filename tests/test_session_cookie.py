"""Browser sessions live in an HttpOnly cookie, not in page-readable storage.

Audit finding RD-FE-01: the portal kept its session token in localStorage, where
any script on the page could read it. The API now sets the session as an
HttpOnly, SameSite=Lax cookie on login, registration and invite acceptance, reads
it on every request, clears it on logout, and requires X-Requested-With on
cookie-authenticated writes (CSRF).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.middleware.csrf import CookieCSRFMiddleware
from app.models.models import User
from app.routers import auth as auth_mod
from app.routers.accounts import accounts_router
from app.routers.auth import SESSION_COOKIE, auth_router, bcrypt_context

PASSWORD = "Cookie-Session-1!"
XHR = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture()
def client(session, monkeypatch):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(auth_mod, "check_rate_limit", _noop)
    session.add(
        User(
            username="cookie_user",
            email="cookie@example.com",
            hashed_password=bcrypt_context.hash(PASSWORD),
            activated=True,
            royalty_per_stream=0,
        )
    )
    session.commit()
    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(accounts_router)
    app.add_middleware(CookieCSRFMiddleware, cookie_name=SESSION_COOKIE)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _login(tc, password=PASSWORD):
    return tc.post("/auth/token", data={"username": "cookie@example.com", "password": password})


def test_login_sets_an_httponly_lax_cookie(client):
    r = _login(client)
    assert r.status_code == 200, r.text
    set_cookie = r.headers["set-cookie"].lower()
    assert set_cookie.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/" in set_cookie


def test_the_cookie_alone_authenticates(client):
    _login(client)
    r = client.get("/auth/me/account")
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "cookie@example.com"
    assert client.get("/auth/verify-token").status_code == 200


def test_no_cookie_and_no_header_is_unauthenticated(client):
    assert client.get("/auth/me/account").status_code == 401


def test_logout_clears_the_session(client):
    _login(client)
    r = client.post("/auth/logout", headers=XHR)
    assert r.status_code == 204
    assert f"{SESSION_COOKIE}=" in r.headers["set-cookie"]
    assert client.get("/auth/me/account").status_code == 401


def test_cookie_writes_need_the_csrf_header(client):
    _login(client)
    body = {"password_old": "wrong-old-password", "password_new": "Another-Pass-2!"}
    blocked = client.post("/auth/change-password", json=body)
    assert blocked.status_code == 403
    assert "X-Requested-With" in blocked.json()["detail"]
    # With the header the request reaches the route (which rejects the wrong
    # old password: a 401 from the handler, not the CSRF 403).
    allowed = client.post("/auth/change-password", json=body, headers=XHR)
    assert allowed.status_code == 401


def test_reads_do_not_need_the_csrf_header(client):
    _login(client)
    assert client.get("/auth/me/account").status_code == 200


def test_a_password_change_ends_the_cookie_session(client, session):
    _login(client)
    r = client.post(
        "/auth/change-password",
        json={"password_old": PASSWORD, "password_new": "Another-Pass-2!"},
        headers=XHR,
    )
    assert r.status_code == 200, r.text
    # The cookie still holds the old token, which no longer matches the password.
    assert client.get("/auth/me/account").status_code == 403


def test_bearer_header_still_works_for_scripts(client):
    token = _login(client).json()["access_token"]
    client.cookies.clear()
    r = client.get("/auth/me/account", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_bearer_writes_do_not_need_the_csrf_header(client):
    token = _login(client).json()["access_token"]
    client.cookies.clear()
    r = client.post(
        "/auth/change-password",
        json={"password_old": "wrong-old-password", "password_new": "Another-Pass-2!"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 401  # reached the handler; no CSRF 403


def test_registration_sets_the_cookie(client, monkeypatch):
    from app.routers import accounts as accounts_mod

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(accounts_mod, "check_rate_limit", _noop)
    monkeypatch.setenv("ADMIN_SIGNUP_CODE", "letmein")
    r = client.post(
        "/auth/register",
        json={"email": "new-admin@example.com", "username": "new_admin",
              "password": PASSWORD, "role": "admin", "admin_code": "letmein"},
    )
    assert r.status_code == 201, r.text
    set_cookie = r.headers["set-cookie"].lower()
    assert set_cookie.startswith(f"{SESSION_COOKIE}=") and "httponly" in set_cookie


def test_cookie_is_secure_outside_development(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "PRODUCTION")
    r = _login(client)
    assert "secure" in r.headers["set-cookie"].lower()


def test_cookie_is_not_secure_in_development(client, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "DEVELOPMENT")
    r = _login(client)
    assert "secure" not in r.headers["set-cookie"].lower()
