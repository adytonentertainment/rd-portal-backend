"""Two things an admin needs per client: send them their statements on request,
and delete one that arrived wrong.

Both existed only in all-or-nothing form: distribution gated on a whole batch,
deletion only as "wipe everything".
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
    StatementLine,
    Writer,
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
    w = Writer(publisher_id=pub.id, canonical_name="Amenazzy", kind=WriterKind.CLIENT,
               expected_catalogs=["YT"], cadence=Cadence.SEMIANNUAL)
    session.add(w)
    session.flush()
    acct = BeneficiaryAccount(writer_id=w.id, account_code="C00616", catalog=Catalog.YT)
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
    session.add_all([
        StatementLine(statement_id=stmt.id, row_no=1),
        StatementLine(statement_id=stmt.id, row_no=2),
    ])
    session.commit()
    return {"w": w, "acct": acct, "stmt": stmt, "batch": batch, "pub": pub}


# --- send on request ---------------------------------------------------------

def test_one_client_can_be_sent_without_the_whole_batch_being_clean(client, session, world):
    """Somebody else's unresolved account is not this client's problem."""
    orphan = Writer(publisher_id=world["pub"].id, canonical_name="Unmatched Co")
    session.add(orphan)
    session.flush()
    oa = BeneficiaryAccount(writer_id=orphan.id, account_code="C99999", catalog=Catalog.YT)
    session.add(oa)
    session.flush()
    session.add(Statement(batch_id=world["batch"].id, account_id=oa.id,
                          period_code="PUB26H1", version=1, parse_status=ParseStatus.PARSED))
    session.commit()

    res = client.post(f"/admin/writers/{world['w'].id}/distribute")
    assert res.status_code == 200, res.text
    assert res.json()["published"] == 1
    assert session.query(Distribution).filter(Distribution.writer_id == world["w"].id).count() == 1


def test_sending_twice_does_not_publish_it_again(client, session, world):
    client.post(f"/admin/writers/{world['w'].id}/distribute")
    again = client.post(f"/admin/writers/{world['w'].id}/distribute").json()
    assert again["published"] == 0
    assert again["already_distributed"] == 1


def test_an_unresolved_client_is_refused_with_the_reason(client, session, world):
    """A wait is the wrong answer when the client is the thing to fix."""
    world["w"].cadence = None
    world["w"].kind = None
    session.commit()

    res = client.post(f"/admin/writers/{world['w'].id}/distribute")
    assert res.status_code == 409
    reasons = res.json()["detail"]["reasons"]
    assert "not matched to a client on the list" in reasons
    assert "no payment cadence set" in reasons


def test_an_acquired_catalog_is_never_sent(client, session, world):
    world["w"].publisher_owned = True
    session.commit()
    res = client.post(f"/admin/writers/{world['w'].id}/distribute")
    assert res.status_code == 409
    assert "belongs to the publisher" in " ".join(res.json()["detail"]["reasons"])


# --- delete one statement ----------------------------------------------------

def test_deleting_a_statement_takes_its_lines_with_it(client, session, world):
    res = client.delete(f"/admin/writers/{world['w'].id}/statements/{world['stmt'].id}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["account_code"] == "C00616"
    assert body["lines_deleted"] == 2

    assert session.get(Statement, world["stmt"].id) is None
    assert session.query(StatementLine).count() == 0


def test_deleting_a_published_statement_withdraws_it_from_the_portal(client, session, world):
    """The one action here that takes something away from a client who could
    already read it, so the response says so."""
    client.post(f"/admin/writers/{world['w'].id}/distribute")
    assert session.query(Distribution).count() == 1

    body = client.delete(
        f"/admin/writers/{world['w'].id}/statements/{world['stmt'].id}"
    ).json()
    assert body["withdrawn_from_portal"] == 1
    assert session.query(Distribution).count() == 0


def test_a_statement_belonging_to_another_client_is_refused(client, session, world):
    other = Writer(publisher_id=world["pub"].id, canonical_name="Someone Else",
                   kind=WriterKind.CLIENT)
    session.add(other)
    session.commit()
    res = client.delete(f"/admin/writers/{other.id}/statements/{world['stmt'].id}")
    assert res.status_code == 404
    assert session.get(Statement, world["stmt"].id) is not None


def test_the_detail_says_which_statements_are_live_in_the_portal(client, session, world):
    """The delete confirmation has to warn when it is taking something back from
    a client who can already read it, so the detail must say which those are."""
    before = client.get(f"/admin/writers/{world['w'].id}").json()
    assert before["statements"][0]["distributed"] is False

    client.post(f"/admin/writers/{world['w'].id}/distribute")

    after = client.get(f"/admin/writers/{world['w'].id}").json()
    assert after["statements"][0]["distributed"] is True
