"""Unmatched accounts get a guessed owner, and "missing data" is findable.

Both exist for the same moment: statements have landed, and the publisher has
to work out who is still unresolved before anything can be sent.
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
    ParseStatus,
    Publisher,
    Statement,
    StatementBatch,
    Writer,
    WriterKind,
)
from app.routers.auth import get_user
from app.routers.writers_admin import writers_admin_router
from app.services.writers.suggest import suggest_clients_for

ADMIN_EMAIL = "admin@verax.app"


@pytest.fixture()
def admin_user(session, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    u = User(email=ADMIN_EMAIL, username="admin", royalty_per_stream=0)
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def client(session, admin_user):
    app = FastAPI()
    app.include_router(writers_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


@pytest.fixture()
def pub(session):
    p = Publisher(name="Regalias Digitales")
    session.add(p)
    session.commit()
    return p


def mk_writer(session, pub, name, *, kind=WriterKind.CLIENT, catalogs=("YT",),
              cadence=Cadence.SEMIANNUAL, payee=None):
    w = Writer(publisher_id=pub.id, canonical_name=name, kind=kind, payee_name=payee,
               expected_catalogs=list(catalogs) if catalogs else None, cadence=cadence)
    session.add(w)
    session.flush()
    return w


def mk_account(session, writer, code, catalog=Catalog.YT, display_name=None, with_statement=True):
    a = BeneficiaryAccount(writer_id=writer.id, account_code=code, catalog=catalog,
                           display_name=display_name)
    session.add(a)
    session.flush()
    if with_statement:
        b = StatementBatch(publisher_id=writer.publisher_id, label="b",
                           period_code="PUB26H1", catalog=catalog)
        session.add(b)
        session.flush()
        session.add(Statement(batch_id=b.id, account_id=a.id, period_code="PUB26H1",
                              version=1, parse_status=ParseStatus.PARSED))
    session.flush()
    return a


def test_unmatched_account_is_offered_the_client_it_probably_is(session, pub):
    """The publisher should not have to search the roster by hand to answer
    'is this someone we already have?'."""
    mk_writer(session, pub, "Luna Negra Publishing")
    placeholder = mk_writer(session, pub, "Luna Negra Pub", kind=None, catalogs=None, cadence=None)
    mk_account(session, placeholder, "C00999", display_name="Luna Negra Publishing")
    session.commit()

    got = suggest_clients_for(session, [placeholder.id])
    assert got[placeholder.id]["name"] == "Luna Negra Publishing"
    assert got[placeholder.id]["method"] == "exact"


def test_a_name_nothing_resembles_gets_no_guess(session, pub):
    """A confident-looking wrong answer is worse than none — a bad merge sends
    one client's royalties to another."""
    mk_writer(session, pub, "Amenazzy")
    placeholder = mk_writer(session, pub, "Zzzq Unrelated Holdings", kind=None,
                            catalogs=None, cadence=None)
    mk_account(session, placeholder, "C00888", display_name="Zzzq Unrelated Holdings")
    session.commit()

    assert suggest_clients_for(session, [placeholder.id])[placeholder.id] is None


def test_the_roster_row_carries_the_guess(session, pub, client):
    mk_writer(session, pub, "Canserbero")
    placeholder = mk_writer(session, pub, "Canserbero Music", kind=None, catalogs=None, cadence=None)
    mk_account(session, placeholder, "C00777", display_name="Canserbero Music")
    session.commit()

    rows = client.get("/admin/writers", params={"unmatched": True}).json()["items"]
    row = next(r for r in rows if r["id"] == placeholder.id)
    assert row["is_unmatched"] is True
    assert row["account_name"] == "Canserbero Music"
    assert row["suggested_client"]["name"] == "Canserbero"


def test_missing_data_finds_no_data_and_partial_data(session, pub, client):
    """Partial is the one that hides: the roster says "has statements" while
    only one of two expected revenue types actually arrived."""
    complete = mk_writer(session, pub, "Complete Client", catalogs=("YT",))
    mk_account(session, complete, "C001", Catalog.YT)

    partial = mk_writer(session, pub, "Half Client", catalogs=("YT", "MECH"))
    mk_account(session, partial, "C002", Catalog.YT)          # MECH never arrived

    nothing = mk_writer(session, pub, "Silent Client", catalogs=("YT",))
    mk_account(session, nothing, "C003", Catalog.YT, with_statement=False)
    session.commit()

    names = {r["canonical_name"] for r in
             client.get("/admin/writers", params={"data_gap": True}).json()["items"]}
    assert "Half Client" in names
    assert "Silent Client" in names
    assert "Complete Client" not in names


def test_missing_data_covers_commission_partners_too(session, pub, client):
    """They are payees as well — a partner missing half their data is as much
    a hole as a client missing all of it."""
    partner = mk_writer(session, pub, "Partner Co", kind=WriterKind.COMMISSION_PARTNER,
                        catalogs=("YT", "MECH"))
    partner.is_commission_partner = True
    mk_account(session, partner, "C004", Catalog.YT)
    session.commit()

    names = {r["canonical_name"] for r in
             client.get("/admin/writers", params={"data_gap": True}).json()["items"]}
    assert "Partner Co" in names
