"""A publisher portal is invite-only; the registration endpoint must refuse.

Registration was open on the live RD portal: one unauthenticated POST created a
real account with no passphrase, no invite and no verification. The guards that
were supposed to prevent that are all conditional on `settings.mode ==
"development"`, and mode defaults to "production" — so in production, the place
they matter, none of them ran.

That is the shape of the attack this client has already suffered once: roughly
80 junk accounts created in half an hour.

The pair of tests that matter here are the refusal AND the invite path still
working — a lockdown that also blocks invited writers is not a fix, it is an
outage.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.routers.auth import auth_router


@pytest.fixture()
def client(session):
    app = FastAPI()
    app.include_router(auth_router)
    app.dependency_overrides[get_session] = lambda: session
    return TestClient(app)


def _payload(email="stranger@example.com"):
    # captchaToken is required by the schema, so FastAPI validates it before
    # the endpoint runs. Supplying it proves the 403 comes from the invite-only
    # gate and not from body validation.
    return {
        "email": email,
        "username": "stranger",
        "password": "Testing123!x",
        "captchaToken": "test-token",
    }


def test_registration_is_refused_when_invite_only(client, monkeypatch):
    monkeypatch.setattr("app.routers.auth.settings.allow_public_registration", False, raising=False)
    r = client.post("/auth/user", json=_payload())
    assert r.status_code == 403
    # The message has to tell a confused writer what to do instead.
    assert "invitation" in r.json()["detail"].lower()


def test_refusal_happens_before_anything_else(client, monkeypatch):
    """The gate must not sit behind the rate limiter or captcha, both of which
    are conditional and neither of which was running in production."""
    monkeypatch.setattr("app.routers.auth.settings.allow_public_registration", False, raising=False)
    # Ten rapid attempts: every one should be a flat 403, never a 429 or a 400
    # about captcha, which would mean the request got past the gate.
    codes = {client.post("/auth/user", json=_payload(f"a{i}@example.com")).status_code
             for i in range(10)}
    assert codes == {403}


def test_registration_still_works_when_enabled(client, monkeypatch):
    """Default stays open so the Verax SaaS product is unaffected; this asserts
    the gate is the only thing standing in the way."""
    monkeypatch.setattr("app.routers.auth.settings.allow_public_registration", True, raising=False)
    r = client.post("/auth/user", json=_payload())
    assert r.status_code != 403


def test_the_invite_path_does_not_go_through_registration():
    """Invited writers get their login from invite acceptance, which builds the
    User itself. If that ever changes to call registration, closing it here
    would silently lock out every writer — so this pins the assumption.
    """
    import inspect

    from app.services.portal import invites as invite_svc

    src = inspect.getsource(invite_svc.accept_invite)
    assert "User(" in src, "invite acceptance must create its own login"

    from app.routers import portal
    accept_src = inspect.getsource(portal.accept_invite)
    assert "create_user" not in accept_src
    assert "/auth/user" not in accept_src
