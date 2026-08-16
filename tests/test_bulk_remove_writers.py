"""Bulk cleanup from the "needs attention" view must not destroy royalties.

Two different things get called "delete" in this UI:

  * an empty junk row (a typo'd import line, a client added twice) owns nothing
    and is genuinely deleted;
  * a row that owns statement accounts is holding real money — deleting it
    would orphan those statements, so it is offboarded instead.

Getting that distinction wrong silently loses client royalties, so it is
asserted here rather than trusted to the caller.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    BeneficiaryAccount,
    Contact,
    ContactRole,
    Publisher,
    Writer,
    WriterContact,
    WriterKind,
    WriterStatus,
)
from app.routers.auth import get_user
from app.routers.writers_admin import writers_admin_router

ADMIN_EMAIL = "bulk-admin@verax.app"


@pytest.fixture()
def client(session, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    admin = User(email=ADMIN_EMAIL, username="bulk-admin", royalty_per_stream=0)
    session.add(admin)
    session.commit()
    app = FastAPI()
    app.include_router(writers_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin
    return TestClient(app)


@pytest.fixture()
def pub(session):
    p = Publisher(name="Regalias Digitales")
    session.add(p)
    session.commit()
    return p


def _writer(session, pub, name, **kw):
    w = Writer(
        canonical_name=name, publisher_id=pub.id, status=WriterStatus.ACTIVE,
        kind=kw.pop("kind", WriterKind.CLIENT), **kw
    )
    session.add(w)
    session.commit()
    return w


def _remove(client, ids):
    r = client.post("/admin/writers/bulk-remove", json={"writer_ids": ids})
    assert r.status_code == 200, r.text
    return r.json()


def test_empty_rows_are_actually_deleted(client, session, pub):
    """The point of the feature: clear junk out of the needs-attention list."""
    a = _writer(session, pub, "Typo Entry", kind=None)
    b = _writer(session, pub, "Added Twice", kind=None)

    out = _remove(client, [a.id, b.id])

    assert {d["id"] for d in out["deleted"]} == {a.id, b.id}
    assert out["archived"] == []
    assert session.get(Writer, a.id) is None
    assert session.get(Writer, b.id) is None


def test_a_client_holding_statement_accounts_is_offboarded_not_destroyed(client, session, pub):
    """This is the money-losing case. The row owns a statement account, so it
    must survive with its accounts intact."""
    w = _writer(session, pub, "Los Tucanes De Tijuana", kind=None)
    acct = BeneficiaryAccount(writer_id=w.id, account_code="C00123", display_name="Los Tucanes")
    session.add(acct)
    session.commit()

    out = _remove(client, [w.id])

    assert out["deleted"] == [], "a client with royalty accounts must not be deleted"
    assert [a["id"] for a in out["archived"]] == [w.id]

    survivor = session.get(Writer, w.id)
    assert survivor is not None
    assert survivor.status == WriterStatus.OFFBOARDED
    assert session.get(BeneficiaryAccount, acct.id) is not None
    assert session.get(BeneficiaryAccount, acct.id).writer_id == w.id


def test_a_mixed_selection_splits_correctly(client, session, pub):
    """Selecting all 87 needs-attention rows must not take the 34 with money
    down with the 53 empty ones."""
    empty = [_writer(session, pub, f"Empty {i}", kind=None) for i in range(3)]
    holding = _writer(session, pub, "Holds Money", kind=None)
    session.add(BeneficiaryAccount(writer_id=holding.id, account_code="C00999"))
    session.commit()

    out = _remove(client, [w.id for w in empty] + [holding.id])

    assert {d["id"] for d in out["deleted"]} == {w.id for w in empty}
    assert [a["id"] for a in out["archived"]] == [holding.id]
    assert session.get(Writer, holding.id).status == WriterStatus.OFFBOARDED


def test_house_accounts_are_never_touched(client, session, pub):
    """The publisher's own books are not a cleanup target."""
    house = _writer(session, pub, "Regalias Digitales, LLC", kind=None, is_house_account=True)

    out = _remove(client, [house.id])

    assert out["deleted"] == [] and out["archived"] == []
    assert out["skipped"][0]["reason"] == "house account"
    assert session.get(Writer, house.id).status == WriterStatus.ACTIVE


def test_contact_links_are_cleared_so_the_delete_does_not_fail(client, session, pub):
    """WriterContact.writer_id is NOT NULL — leaving a link behind would raise
    an integrity error mid-batch and roll back the whole cleanup."""
    w = _writer(session, pub, "Has A Contact", kind=None)
    c = Contact(email="someone@example.com")
    session.add(c)
    session.commit()
    session.add(WriterContact(writer_id=w.id, contact_id=c.id, role=ContactRole.PRIMARY))
    session.commit()

    out = _remove(client, [w.id])

    assert [d["id"] for d in out["deleted"]] == [w.id]
    assert session.get(Writer, w.id) is None
    # the Contact itself belongs to the person, not the client row
    assert session.get(Contact, c.id) is not None


def test_unknown_ids_are_reported_not_fatal(client, session, pub):
    w = _writer(session, pub, "Real One", kind=None)
    out = _remove(client, [w.id, 999999])
    assert [d["id"] for d in out["deleted"]] == [w.id]
    assert out["skipped"][0]["reason"] == "not found"


def test_duplicate_ids_are_processed_once(client, session, pub):
    w = _writer(session, pub, "Selected Twice", kind=None)
    out = _remove(client, [w.id, w.id, w.id])
    assert out["requested"] == 1
    assert len(out["deleted"]) == 1


def test_an_empty_selection_is_rejected(client):
    r = client.post("/admin/writers/bulk-remove", json={"writer_ids": []})
    assert r.status_code == 422


def test_an_absurd_batch_is_rejected(client):
    r = client.post("/admin/writers/bulk-remove", json={"writer_ids": list(range(1, 700))})
    assert r.status_code == 422
