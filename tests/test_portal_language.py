"""The writer's portal language is remembered on their contact record.

Storing it server-side (not only in localStorage) is what makes the choice
follow a writer from a laptop to a phone. Most of this roster reads Spanish,
so re-picking the language on every device is real friction, not a nicety.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import Contact
from app.routers.auth import get_user
from app.routers.portal import current_contact, me_router

WRITER_EMAIL = "escritor@example.com"


@pytest.fixture()
def contact(session):
    c = Contact(email=WRITER_EMAIL)
    session.add(c)
    session.commit()
    return c


@pytest.fixture()
def client(session, contact):
    user = User(email=WRITER_EMAIL, username="escritor", royalty_per_stream=0)
    session.add(user)
    session.commit()
    app = FastAPI()
    app.include_router(me_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: user
    app.dependency_overrides[current_contact] = lambda: contact
    return TestClient(app)


def test_writer_can_switch_to_spanish(client, session, contact):
    r = client.put("/me/language", json={"language": "es"})
    assert r.status_code == 200, r.text
    assert r.json()["language"] == "es"
    session.refresh(contact)
    assert contact.preferred_language == "es"


def test_writer_can_switch_back_to_english(client, session, contact):
    client.put("/me/language", json={"language": "es"})
    client.put("/me/language", json={"language": "en"})
    session.refresh(contact)
    assert contact.preferred_language == "en"


def test_the_choice_is_returned_by_me(client, session, contact):
    """The portal reads this on load, so a new device opens in the right one."""
    client.put("/me/language", json={"language": "es"})
    assert client.get("/me").json()["preferred_language"] == "es"


@pytest.mark.parametrize("bad", ["fr", "", "spanish", "es-MX-x-hacky", None])
def test_unsupported_languages_are_rejected(client, session, contact, bad):
    """The column is 2 chars; a longer value would be silently truncated into
    something meaningless rather than refused."""
    r = client.put("/me/language", json={"language": bad})
    assert r.status_code == 400
    session.refresh(contact)
    assert contact.preferred_language is None


def test_case_and_padding_are_tolerated(client, session, contact):
    """'ES ' from a picker shouldn't be an error."""
    r = client.put("/me/language", json={"language": "  ES  "})
    assert r.status_code == 200
    session.refresh(contact)
    assert contact.preferred_language == "es"
