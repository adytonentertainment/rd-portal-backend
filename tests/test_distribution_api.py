"""End-to-end over HTTP: gate -> distribute -> a linked contact sees the
statement in /me/statements; an unlinked contact does not (infra PRD §7.3)."""

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    Cadence,
    BatchStatus,
    BeneficiaryAccount,
    Catalog,
    Contact,
    ParseStatus,
    Publisher,
    Statement,
    StatementBatch,
    Writer,
    WriterContact,
    WriterKind,
)
from app.routers.auth import get_user
from app.routers.portal import me_router
from app.routers.statements_admin import (
    distributions_admin_router,
    statements_admin_router,
)

ADMIN_EMAIL = "admin@verax.app"


@pytest.fixture()
def admin_user(session):
    u = User(email=ADMIN_EMAIL, username="admin", royalty_per_stream=0)
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def scenario(session):
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()
    b = StatementBatch(publisher_id=pub.id, label="YT 2026H1", period_code="PUB26H1",
                       catalog=Catalog.YT, status=BatchStatus.APPROVED)
    session.add(b)
    session.flush()
    # revenue type + cadence set: the gate blocks any batch still holding a
    # client that needs attention.
    w = Writer(publisher_id=pub.id, canonical_name="RedZed", kind=WriterKind.CLIENT,
               expected_catalogs=["YT"], cadence=Cadence.SEMIANNUAL)
    session.add(w)
    session.flush()
    acct = BeneficiaryAccount(writer_id=w.id, account_code="C00616", catalog=Catalog.YT)
    session.add(acct)
    session.flush()
    session.add(Statement(batch_id=b.id, account_id=acct.id, period_code="PUB26H1",
                          version=1, parse_status=ParseStatus.PARSED,
                          payable=Decimal("1280.28"), line_count=42,
                          pdf_path="rz.pdf", xlsx_path="rz.xlsx"))
    # RedZed's contact (linked) and a stranger's contact (not linked)
    rz_user = User(email="redzed@x.com", username="redzed", royalty_per_stream=0)
    st_user = User(email="stranger@x.com", username="stranger", royalty_per_stream=0)
    session.add_all([rz_user, st_user])
    session.flush()
    rz_contact = Contact(email="redzed@x.com", user_id=rz_user.id)
    st_contact = Contact(email="stranger@x.com", user_id=st_user.id)
    session.add_all([rz_contact, st_contact])
    session.flush()
    session.add(WriterContact(writer_id=w.id, contact_id=rz_contact.id))
    session.commit()
    return {"batch_id": b.id, "writer_id": w.id,
            "rz_user": rz_user, "stranger_user": st_user}


def _app(session, holder):
    app = FastAPI()
    app.include_router(statements_admin_router)
    app.include_router(distributions_admin_router)
    app.include_router(me_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: holder["user"]
    return TestClient(app)


def test_gate_distribute_and_portal_visibility(session, admin_user, scenario, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    holder = {"user": admin_user}
    client = _app(session, holder)
    bid = scenario["batch_id"]

    # Gate is green (resolved writer, parsed, no blockers).
    gate = client.get(f"/admin/statements/batches/{bid}/gate").json()
    assert gate["ready"] is True and gate["counts"]["distributable"] == 1

    # Before distribution, RedZed's portal is empty.
    holder["user"] = scenario["rz_user"]
    assert client.get("/me/statements").json() == []

    # Admin distributes.
    holder["user"] = admin_user
    d = client.post(f"/admin/statements/batches/{bid}/distribute")
    assert d.status_code == 200, d.text
    assert d.json()["published"] == 1

    # RedZed's contact now sees exactly one statement.
    holder["user"] = scenario["rz_user"]
    mine = client.get("/me/statements").json()
    assert len(mine) == 1
    assert mine[0]["period_code"] == "PUB26H1"
    assert Decimal(mine[0]["payable"]) == Decimal("1280.28")
    dist_id = mine[0]["distribution_id"]
    assert client.get(f"/me/statements/{dist_id}").json()["writer_name"] == "RedZed"

    # The stranger sees nothing and can't fetch RedZed's statement (404).
    holder["user"] = scenario["stranger_user"]
    assert client.get("/me/statements").json() == []
    assert client.get(f"/me/statements/{dist_id}").status_code == 404

    # Admin unpublishes -> disappears from RedZed's portal (row kept).
    holder["user"] = admin_user
    assert client.post(f"/admin/distributions/{dist_id}/unpublish").status_code == 200
    holder["user"] = scenario["rz_user"]
    assert client.get("/me/statements").json() == []


def test_distribute_blocked_returns_gate(session, admin_user, scenario, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    # Make the writer a placeholder again -> gate not ready.
    w = session.get(Writer, scenario["writer_id"])
    w.kind = None
    session.commit()
    client = _app(session, {"user": admin_user})
    r = client.post(f"/admin/statements/batches/{scenario['batch_id']}/distribute")
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "gate_not_ready"
