"""What a GUEST on someone's portal may and may not do.

Every contact linked to a writer can read everything — a manager or an attorney
who cannot see the money is useless, and the client explicitly wanted the
statements visible. The line is drawn at CHANGING things: access to someone's
royalties must not spread sideways without the person whose royalties they are.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from datetime import datetime

from app.models.statements import (
    Contact,
    ContactRole,
    Publisher,
    PortalInvite,
    Writer,
    WriterContact,
)
from app.routers.auth import get_user
from app.routers.portal import me_router, portal_router
from app.services.portal import invites as invite_svc


@pytest.fixture()
def world(session):
    """One writer; a primary contact and a manager guest, both with logins."""
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()
    w = Writer(publisher_id=pub.id, canonical_name="Amenazzy")
    session.add(w)
    session.flush()

    people = {}
    for role, email in ((ContactRole.PRIMARY, "artist@x.com"), (ContactRole.MANAGER, "manager@x.com")):
        u = User(email=email, username=email.split("@")[0], royalty_per_stream=0)
        session.add(u)
        session.flush()
        c = Contact(email=email, user_id=u.id)
        session.add(c)
        session.flush()
        session.add(WriterContact(writer_id=w.id, contact_id=c.id, role=role, user_id=u.id))
        # These two are live portal users, so they got in by accepting.
        session.add(PortalInvite(writer_id=w.id, email=email,
                                 token_hash=f"granted-{email}",
                                 expires_at=datetime.now(),
                                 accepted_at=datetime.now()))
        people[role.value] = u
    session.commit()
    return {"writer": w, "primary": people["primary"], "guest": people["manager"]}


@pytest.fixture()
def client(session):
    holder = {}
    app = FastAPI()
    app.include_router(me_router)
    app.include_router(portal_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: holder["user"]
    c = TestClient(app)
    c.login_as = lambda u: holder.update(user=u)
    return c


def test_guest_sees_the_writer_and_the_money(client, world):
    """Nothing is hidden from a guest — that was the explicit decision."""
    client.login_as(world["guest"])
    res = client.get("/me/writers")
    assert res.status_code == 200
    assert [w["name"] for w in res.json()] == ["Amenazzy"]

    assert client.get("/me/statements").status_code == 200
    assert client.get("/me/earnings").status_code == 200


def test_guest_cannot_hand_out_access(client, world):
    """THE boundary: a guest inviting further guests spreads someone's royalty
    data sideways with nobody's consent."""
    client.login_as(world["guest"])
    res = client.post(
        f"/me/writers/{world['writer'].id}/invites",
        json={"email": "stranger@x.com", "role": "other"},
    )
    assert res.status_code == 403
    assert "primary contact" in res.json()["detail"]


def test_guest_cannot_revoke_the_primarys_invite(client, session, world):
    invite, _ = invite_svc.create_invite(
        session, world["writer"].id, "lawyer@x.com", ContactRole.LEGAL
    )
    client.login_as(world["guest"])
    res = client.post(f"/me/invites/{invite.id}/revoke")
    assert res.status_code == 403
    assert session.get(PortalInvite, invite.id).revoked_at is None


def test_primary_can_still_share_and_revoke(client, session, world):
    client.login_as(world["primary"])
    res = client.post(
        f"/me/writers/{world['writer'].id}/invites",
        json={"email": "lawyer@x.com", "role": "legal"},
    )
    assert res.status_code == 201
    invite_id = res.json()["invite_id"]

    assert client.post(f"/me/invites/{invite_id}/revoke").status_code == 200
    assert session.get(PortalInvite, invite_id).revoked_at is not None


def test_guest_manages_their_own_language(client, session, world):
    """Their own login and preferences stay theirs — that is not the writer's
    account, and re-picking Spanish on every device is the friction the whole
    language feature exists to remove."""
    client.login_as(world["guest"])
    assert client.put("/me/language", json={"language": "es"}).status_code == 200
    assert session.query(Contact).filter(Contact.email == "manager@x.com").first().preferred_language == "es"


def test_the_portal_is_told_which_controls_to_show(client, world):
    """The server enforces it; the UI still needs to know, so it can omit the
    invite panel instead of offering a button that 403s."""
    client.login_as(world["primary"])
    card = client.get("/me/writers").json()[0]
    assert card["my_role"] == "primary"
    assert card["can_manage_access"] is True

    client.login_as(world["guest"])
    card = client.get("/me/writers").json()[0]
    assert card["my_role"] == "manager"
    assert card["can_manage_access"] is False


def test_a_stranger_still_sees_nothing(client, session, world):
    """The guest rules must not have widened anything: someone with no link at
    all gets 403 from /me, not a viewer's read access."""
    outsider = User(email="nobody@x.com", username="nobody", royalty_per_stream=0)
    session.add(outsider)
    session.commit()
    client.login_as(outsider)
    assert client.get("/me/writers").status_code == 403


def test_recording_a_contact_email_does_not_admit_them(session, client, world):
    """THE bug: paste an address that already runs one client's portal into a
    SECOND client's contact field, and that second client read "Portal active"
    while the person could open statements nobody had invited them to.

    Recording a contact says how to reach a client. It does not say who may
    read their money. 148 contacts on the real roster are linked to more than
    one writer, so this was the normal shape of the data, not a corner case.
    """
    from app.models.statements import Publisher, WriterKind
    from app.routers.writers_admin import _portal_status_for
    from app.services.portal import invites as invite_svc

    pub = session.query(Publisher).first()
    second = Writer(publisher_id=pub.id, canonical_name="A Different Client",
                    kind=WriterKind.CLIENT)
    session.add(second)
    session.flush()

    # the manager already runs the first client's portal; an admin now records
    # their address on the second client — no invite sent
    manager_contact = (
        session.query(Contact).filter(Contact.email == "manager@x.com").one()
    )
    session.add(WriterContact(writer_id=second.id, contact_id=manager_contact.id,
                              role=ContactRole.MANAGER))
    session.commit()

    # the roster badge answers for THIS client: nobody was invited here
    invites = session.query(PortalInvite).filter(PortalInvite.writer_id == second.id).all()
    links = [(None, manager_contact)]
    assert _portal_status_for(links, invites) == "none"

    # and the link alone does not let them read it
    assert second.id not in invite_svc.writer_ids_for_contact(session, manager_contact)
    client.login_as(world["guest"])
    assert client.get(f"/me/writers/{second.id}/members").status_code == 404
    assert [w["name"] for w in client.get("/me/writers").json()] == ["Amenazzy"]
