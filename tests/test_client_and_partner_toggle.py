"""The toggle is the CLIENT / COMMISSION-PARTNER split. Nothing else.

Two axes exist and they are not the same thing:

  * ROLE — client vs commission partner. Their own catalog on one side,
    commission earned on other people's works on the other. Both are theirs,
    both are readable, and switching between them is the point of this file.

  * OWNERSHIP — "Likybo NEW" (theirs) vs the counterpart the publisher acquired.
    That is NOT a toggle. The acquired catalog is not their money and they
    cannot see it at all, so it never reaches the switcher.

Conflating the two is easy because all three entries carry the same person's
name. `test_the_acquired_catalog_is_not_one_of_the_options` holds the line.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    BeneficiaryAccount,
    Cadence,
    Catalog,
    Contact,
    Distribution,
    ParseStatus,
    Publisher,
    Statement,
    StatementBatch,
    Writer,
    WriterKind,
)
from app.routers.auth import get_user
from app.routers.portal import me_router, portal_router, writer_invites_admin_router
from app.services.portal import invites as invite_svc

ADMIN_EMAIL = "admin@verax.app"
SHARED = "likybo@example.com"


@pytest.fixture()
def admin_user(session, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    u = User(email=ADMIN_EMAIL, username="admin", royalty_per_stream=0)
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def app_client(session, admin_user):
    holder = {"user": admin_user}
    app = FastAPI()
    app.include_router(writer_invites_admin_router)
    app.include_router(me_router)
    app.include_router(portal_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: holder["user"]
    c = TestClient(app)
    c.act_as = lambda u: holder.update(user=u)
    return c


@pytest.fixture()
def entries(session):
    """Likybo's two entries: his own catalog, and his commission partnership."""
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()

    def mk(name, kind, code, catalog):
        w = Writer(publisher_id=pub.id, canonical_name=name, kind=kind,
                   is_client=kind is WriterKind.CLIENT,
                   is_commission_partner=kind is WriterKind.COMMISSION_PARTNER,
                   expected_catalogs=[catalog.value], cadence=Cadence.SEMIANNUAL)
        session.add(w)
        session.flush()
        acct = BeneficiaryAccount(writer_id=w.id, account_code=code, catalog=catalog)
        session.add(acct)
        session.flush()
        batch = StatementBatch(publisher_id=pub.id, label=code, period_code="PUB26H1",
                               catalog=catalog)
        session.add(batch)
        session.flush()
        stmt = Statement(batch_id=batch.id, account_id=acct.id, period_code="PUB26H1",
                         version=1, parse_status=ParseStatus.PARSED)
        session.add(stmt)
        session.flush()
        session.add(Distribution(statement_id=stmt.id, writer_id=w.id, batch_id=batch.id,
                                 period_code="PUB26H1", catalog=catalog, portal_visible=True))
        return w

    own = mk("Likybo NEW", WriterKind.CLIENT, "JN0440", Catalog.MECH)          # theirs
    # the commission partnership — same person, different money, also theirs
    commission = mk("Likybo", WriterKind.COMMISSION_PARTNER, "CSJ047", Catalog.MECH)
    session.commit()
    return {"own": own, "commission": commission}


def test_one_login_reaches_both_and_each_says_which_it_is(app_client, session, entries):
    """The switcher needs the role, not just the name: both entries are called
    some version of "Likybo" and they hold different money."""
    tok_own = app_client.post(f"/admin/writers/{entries['own'].id}/invites",
                              json={"email": SHARED, "role": "primary"}).json()["token"]
    tok_com = app_client.post(f"/admin/writers/{entries['commission'].id}/invites",
                              json={"email": SHARED, "role": "primary"}).json()["token"]

    first = app_client.post("/portal/accept-invite", json={
        "token": tok_own, "password": "one-password", "username": "likybo"})
    assert first.status_code == 200, first.text
    second = app_client.post("/portal/accept-invite", json={
        "token": tok_com, "password": "one-password"})
    assert second.status_code == 200, second.text

    # one address, one login
    users = session.query(User).filter(User.email == SHARED).all()
    assert len(users) == 1
    me = users[0]

    app_client.act_as(me)
    cards = app_client.get("/me/writers").json()
    by_name = {c["name"]: c for c in cards}
    assert set(by_name) == {"Likybo NEW", "Likybo"}
    assert by_name["Likybo NEW"]["kind"] == "client"
    assert by_name["Likybo"]["kind"] == "commission_partner"


def test_each_side_shows_only_its_own_money(app_client, session, entries):
    """Commission and royalties are never added together, and picking one never
    leaks the other."""
    for w in (entries["own"], entries["commission"]):
        tok = app_client.post(f"/admin/writers/{w.id}/invites",
                              json={"email": SHARED, "role": "primary"}).json()["token"]
        app_client.post("/portal/accept-invite",
                        json={"token": tok, "password": "one-password", "username": "likybo"})

    me = session.query(User).filter(User.email == SHARED).one()
    app_client.act_as(me)

    own_only = app_client.get("/me/statements", params={"writer_id": entries["own"].id}).json()
    com_only = app_client.get("/me/statements", params={"writer_id": entries["commission"].id}).json()

    assert {s["writer_name"] for s in own_only} == {"Likybo NEW"}
    assert {s["writer_name"] for s in com_only} == {"Likybo"}


def test_holding_only_one_entry_means_nothing_to_toggle(app_client, session, entries):
    """Almost everyone. The switcher must not appear for them, which is decided
    by this list having one item."""
    tok = app_client.post(f"/admin/writers/{entries['own'].id}/invites",
                          json={"email": "solo@example.com", "role": "primary"}).json()["token"]
    app_client.post("/portal/accept-invite",
                    json={"token": tok, "password": "one-password", "username": "solo"})
    me = session.query(User).filter(User.email == "solo@example.com").one()
    assert invite_svc.writer_ids_for_user(session, me) == [entries["own"].id]


def test_the_acquired_catalog_is_not_one_of_the_options(app_client, session, entries):
    """All three of Likybo's entries at once, which is where the two axes get
    confused:

        Likybo NEW                 client, theirs          -> in the toggle
        Likybo (commission)        partner, theirs         -> in the toggle
        Likybo (100% to Regalias)  acquired, publisher's   -> NOT in the toggle

    The third carries his name and is a client-type row, so nothing about its
    shape says "keep this away from him". Only the flag does.
    """
    pub = session.query(Publisher).first()
    acquired = Writer(publisher_id=pub.id, canonical_name="Likybo (100% to Regalias)",
                      kind=WriterKind.CLIENT, is_client=True, publisher_owned=True,
                      expected_catalogs=["MECH"], cadence=Cadence.SEMIANNUAL)
    session.add(acquired)
    session.commit()

    # the two that are his
    for w in (entries["own"], entries["commission"]):
        tok = app_client.post(f"/admin/writers/{w.id}/invites",
                              json={"email": SHARED, "role": "primary"}).json()["token"]
        app_client.post("/portal/accept-invite",
                        json={"token": tok, "password": "one-password", "username": "likybo"})

    # the acquired one cannot even be offered
    refused = app_client.post(f"/admin/writers/{acquired.id}/invites",
                              json={"email": SHARED, "role": "primary"})
    assert refused.status_code == 409
    assert "belongs to the publisher" in refused.json()["detail"]

    me = session.query(User).filter(User.email == SHARED).one()
    app_client.act_as(me)
    names = {c["name"] for c in app_client.get("/me/writers").json()}

    assert names == {"Likybo NEW", "Likybo"}          # the toggle: role split
    assert "Likybo (100% to Regalias)" not in names   # ownership split: invisible
