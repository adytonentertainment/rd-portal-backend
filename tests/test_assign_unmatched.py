"""Accepting the "did you mean X?" guess.

Assigning the wrong owner sends one client's royalties to another and is close
to unrecoverable, so this is narrow on purpose and every refusal is tested.
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
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()
    real = Writer(publisher_id=pub.id, canonical_name="Hyphy Music Publishing",
                  kind=WriterKind.CLIENT, expected_catalogs=["MECH"], cadence=Cadence.SEMIANNUAL)
    orphan = Writer(publisher_id=pub.id, canonical_name="Hyphy Music")
    session.add_all([real, orphan])
    session.flush()
    acct = BeneficiaryAccount(writer_id=orphan.id, account_code="JN0999",
                              catalog=Catalog.MECH, display_name="Hyphy Music")
    session.add(acct)
    session.commit()
    return {"real": real, "orphan": orphan, "acct": acct, "pub": pub}


def test_accepting_the_guess_moves_the_money_to_the_client(client, session, world):
    res = client.post(f"/admin/writers/{world['orphan'].id}/assign",
                      json={"target_writer_id": world["real"].id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["account_codes"] == ["JN0999"]
    assert body["assigned_to"]["name"] == "Hyphy Music Publishing"

    # the account now belongs to the client — portal reads resolve through this
    session.expire_all()
    assert session.get(BeneficiaryAccount, world["acct"].id).writer_id == world["real"].id
    # and the placeholder is gone rather than lingering as a phantom client
    assert session.get(Writer, world["orphan"].id) is None


def test_the_unmatched_spelling_is_remembered_as_an_alias(client, session, world):
    """So the next import resolves it instead of re-creating the same orphan."""
    client.post(f"/admin/writers/{world['orphan'].id}/assign",
                json={"target_writer_id": world["real"].id})
    aliases = [a.alias_name for a in session.query(WriterAlias)
               .filter(WriterAlias.writer_id == world["real"].id)]
    assert "Hyphy Music" in aliases


def test_a_real_client_is_never_moved_by_a_guess(client, session, world):
    other = Writer(publisher_id=world["pub"].id, canonical_name="Someone Else",
                   kind=WriterKind.CLIENT)
    session.add(other)
    session.commit()
    res = client.post(f"/admin/writers/{other.id}/assign",
                      json={"target_writer_id": world["real"].id})
    assert res.status_code == 409
    assert "unmatched" in res.json()["detail"]


def test_target_must_be_a_client_on_the_list(client, session, world):
    second_orphan = Writer(publisher_id=world["pub"].id, canonical_name="Another Orphan")
    session.add(second_orphan)
    session.commit()
    res = client.post(f"/admin/writers/{world['orphan'].id}/assign",
                      json={"target_writer_id": second_orphan.id})
    assert res.status_code == 409
    assert "client on your list" in res.json()["detail"]


def test_already_distributed_money_is_refused(client, session, world):
    """Somebody has already been shown that money. Moving it is a different and
    much bigger decision than resolving an identity."""
    batch = StatementBatch(publisher_id=world["pub"].id, label="b",
                           period_code="PUB26H1", catalog=Catalog.MECH)
    session.add(batch)
    session.flush()
    stmt = Statement(batch_id=batch.id, account_id=world["acct"].id,
                     period_code="PUB26H1", version=1, parse_status=ParseStatus.PARSED)
    session.add(stmt)
    session.flush()
    session.add(Distribution(writer_id=world["orphan"].id, statement_id=stmt.id,
                             batch_id=batch.id, period_code="PUB26H1",
                             catalog=Catalog.MECH))
    session.commit()
    res = client.post(f"/admin/writers/{world['orphan'].id}/assign",
                      json={"target_writer_id": world["real"].id})
    assert res.status_code == 409
    assert "already been distributed" in res.json()["detail"]
    # nothing moved
    session.expire_all()
    assert session.get(BeneficiaryAccount, world["acct"].id).writer_id == world["orphan"].id
