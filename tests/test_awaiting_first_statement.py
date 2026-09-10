"""A client signed after the last statement run has no statements, and should not.

The roster reported them exactly like a client whose statements failed to
arrive: red, "no statements", counted as a reason to hesitate before sending.
That is noise on the one screen whose job is telling an admin what still needs
doing, and it buries the clients who really are missing something.
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
    """One established client with a statement, and two with none: one newly
    signed, one whose statement genuinely did not arrive."""
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()

    def mk(name):
        w = Writer(publisher_id=pub.id, canonical_name=name, kind=WriterKind.CLIENT,
                   is_client=True, expected_catalogs=["YT"], cadence=Cadence.SEMIANNUAL)
        session.add(w)
        session.flush()
        return w

    established, newly_signed, missing = mk("Amenazzy"), mk("Just Signed"), mk("Should Have One")
    acct = BeneficiaryAccount(writer_id=established.id, account_code="C001", catalog=Catalog.YT)
    session.add(acct)
    session.flush()
    batch = StatementBatch(publisher_id=pub.id, label="b", period_code="PUB26H1",
                           catalog=Catalog.YT)
    session.add(batch)
    session.flush()
    session.add(Statement(batch_id=batch.id, account_id=acct.id, period_code="PUB26H1",
                          version=1, parse_status=ParseStatus.PARSED))
    newly_signed.awaiting_first_statement = True
    session.commit()
    return {"established": established, "new": newly_signed, "missing": missing}


def test_a_newly_signed_client_is_not_in_needs_attention(client, session, world):
    names = {r["canonical_name"] for r in
             client.get("/admin/writers", params={"needs_fix": True}).json()["items"]}
    assert "Should Have One" in names      # genuinely missing — still surfaced
    assert "Just Signed" not in names      # nothing is missing


def test_they_are_not_counted_as_clients_without_statements(client, session, world):
    summary = client.get("/admin/writers/summary").json()
    assert summary["clients_without_statements"] == 1
    listed = {c["name"] for c in summary["issues"]["no_statements"]}
    assert listed == {"Should Have One"}


def test_they_do_not_make_a_send_ask_for_confirmation(client, session, world):
    """The guard exists to stop somebody sending while a statement is missing.
    A client who is not owed one is not that."""
    world["missing"].awaiting_first_statement = True
    session.commit()
    res = client.post("/admin/writers/distribute-all", json={})
    assert res.status_code != 409, res.text


def test_the_roster_still_reports_the_fact_and_the_reason_apart(client, session, world):
    """`no_statements` stays factual — they hold none. The flag says whether
    that is a problem, so the UI can show a neutral badge instead of red."""
    rows = client.get("/admin/writers", params={"page_size": 50}).json()["items"]
    by_name = {r["canonical_name"]: r for r in rows}
    assert by_name["Just Signed"]["no_statements"] is True
    assert by_name["Just Signed"]["awaiting_first_statement"] is True
    assert by_name["Should Have One"]["awaiting_first_statement"] is False


def test_the_flag_can_be_set_and_cleared(client, session, world):
    wid = world["missing"].id
    assert client.patch(f"/admin/writers/{wid}",
                        json={"awaiting_first_statement": True}).status_code == 200
    session.expire_all()
    assert session.get(Writer, wid).awaiting_first_statement is True

    client.patch(f"/admin/writers/{wid}", json={"awaiting_first_statement": False})
    session.expire_all()
    assert session.get(Writer, wid).awaiting_first_statement is False
