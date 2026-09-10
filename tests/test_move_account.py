"""Moving a beneficiary account to the client it actually belongs to.

The publisher's corrections all reduce to this: two clients share a name and the
statements landed on the wrong one (Rata Blanca), or one entry is holding both a
person's own catalog and their commission statements and has to be split
(Likybo, Dante Storch, J Swey). Until this existed there was no way to say "this
account is somebody else's" without re-importing the whole client list.
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
    Distribution,
    ParseStatus,
    Publisher,
    Statement,
    StatementBatch,
    Writer,
    WriterAlias,
    WriterKind,
    WriterStatus,
)
from app.routers.auth import get_user
from app.routers.writers_admin import writers_admin_router

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
def world(session):
    """The Rata Blanca case: two clients, one name, YouTube on the wrong one."""
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()
    mech = Writer(publisher_id=pub.id, canonical_name="Rata Blanca",
                  kind=WriterKind.CLIENT, expected_catalogs=["MECH"],
                  cadence=Cadence.SEMIANNUAL)
    yt = Writer(publisher_id=pub.id, canonical_name="Rata Blanca (Adrian Barilari)",
                kind=WriterKind.CLIENT, expected_catalogs=["YT"],
                cadence=Cadence.SEMIANNUAL)
    session.add_all([mech, yt])
    session.flush()
    # C00122 is YouTube; it is sitting on the mechanical entry by mistake
    acct = BeneficiaryAccount(writer_id=mech.id, account_code="C00122",
                              catalog=Catalog.YT, display_name="Rata Blanca (YouTube Publishing)")
    session.add(acct)
    session.flush()
    batch = StatementBatch(publisher_id=pub.id, label="YT", period_code="PUB26H1",
                           catalog=Catalog.YT)
    session.add(batch)
    session.flush()
    stmt = Statement(batch_id=batch.id, account_id=acct.id, period_code="PUB26H1",
                     version=1, parse_status=ParseStatus.PARSED)
    session.add(stmt)
    session.flush()
    session.commit()
    return {"mech": mech, "yt": yt, "acct": acct, "stmt": stmt, "batch": batch, "pub": pub}


def move(client, writer_id, account_id, target_id):
    return client.post(f"/admin/writers/{writer_id}/accounts/{account_id}/move",
                       json={"target_writer_id": target_id})


def test_the_account_and_its_statements_change_hands(client, session, world):
    res = move(client, world["mech"].id, world["acct"].id, world["yt"].id)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["account_code"] == "C00122"
    assert body["to"]["name"] == "Rata Blanca (Adrian Barilari)"
    assert body["statements_moved"] == 1

    session.expire_all()
    assert session.get(BeneficiaryAccount, world["acct"].id).writer_id == world["yt"].id


def test_the_filing_name_is_remembered_as_an_alias(client, session, world):
    """So the next client-list import recognises the spelling instead of
    creating the same orphan again."""
    move(client, world["mech"].id, world["acct"].id, world["yt"].id)
    aliases = [a.alias_name for a in session.query(WriterAlias)
               .filter(WriterAlias.writer_id == world["yt"].id)]
    assert "Rata Blanca (YouTube Publishing)" in aliases


def test_already_distributed_statements_can_still_be_corrected(client, session, world):
    """The common case for a mis-filed account is that it already went out. The
    response says how many, so the admin is told what changed hands."""
    session.add(Distribution(writer_id=world["mech"].id, statement_id=world["stmt"].id,
                             batch_id=world["batch"].id, period_code="PUB26H1",
                             catalog=Catalog.YT))
    session.commit()

    res = move(client, world["mech"].id, world["acct"].id, world["yt"].id)
    assert res.status_code == 200, res.text
    assert res.json()["distributed_statements_affected"] == 1
    session.expire_all()
    assert session.get(BeneficiaryAccount, world["acct"].id).writer_id == world["yt"].id


def test_an_account_not_on_this_client_is_refused(client, session, world):
    res = move(client, world["yt"].id, world["acct"].id, world["mech"].id)
    assert res.status_code == 404


def test_moving_onto_the_same_client_is_refused(client, session, world):
    res = move(client, world["mech"].id, world["acct"].id, world["mech"].id)
    assert res.status_code == 422


def test_an_offboarded_target_is_refused(client, session, world):
    world["yt"].status = WriterStatus.OFFBOARDED
    session.commit()
    res = move(client, world["mech"].id, world["acct"].id, world["yt"].id)
    assert res.status_code == 409


def test_a_missing_target_is_refused(client, session, world):
    assert move(client, world["mech"].id, world["acct"].id, 999999).status_code == 404
