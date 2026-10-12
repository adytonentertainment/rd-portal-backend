"""A publisher portal is invite-only; the registration endpoint must refuse.

Registration was open on the live RD portal: one unauthenticated POST created a
real account with no passphrase, no invite and no verification. The guards that
were supposed to prevent that are all conditional on `settings.mode ==
"development"`, and mode defaults to "production" — so in production, the place
they matter, none of them ran.

That is the shape of the attack this client has already suffered once: roughly
80 junk accounts created in half an hour.

That legacy SaaS signup (`POST /auth/user`, which also created a Stripe
customer) has since been removed outright. These tests pin that it stays gone
AND that the invite path still works — a lockdown that also blocks invited
writers is not a fix, it is an outage.
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


def test_legacy_signup_endpoint_is_gone(client):
    r = client.post(
        "/auth/user",
        json={"email": "stranger@example.com", "username": "stranger",
              "password": "Testing123!x", "captchaToken": "test-token"},
    )
    # GET/PATCH/DELETE /auth/user (profile) remain; POST must not create anyone.
    assert r.status_code == 405


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
