"""GET /admin/statements/uploads/{id}/statements — real parsed amounts so the
upload UI shows Σ(line earnings), not a size-based estimate."""

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    BeneficiaryAccount,
    Catalog,
    ParseStatus,
    Publisher,
    Statement,
    StatementBatch,
    StatementUpload,
    Writer,
)
from app.routers.auth import get_user
from app.routers.statements_admin import statements_admin_router

ADMIN_EMAIL = "admin@verax.app"


@pytest.fixture()
def admin_user(session):
    u = User(email=ADMIN_EMAIL, username="admin", royalty_per_stream=0)
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def client(session, admin_user, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    app = FastAPI()
    app.include_router(statements_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


def _seed(session, upload_id_holder):
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()
    up = StatementUpload(file_count=2)
    session.add(up)
    session.flush()
    upload_id_holder["id"] = up.id
    b_yt = StatementBatch(publisher_id=pub.id, label="YT 2025H2", period_code="PUB25H2",
                          catalog=Catalog.YT, upload_id=up.id)
    b_me = StatementBatch(publisher_id=pub.id, label="MECH 2025H2", period_code="PUB25H2",
                          catalog=Catalog.MECH, upload_id=up.id)
    session.add_all([b_yt, b_me])
    session.flush()
    w = Writer(publisher_id=pub.id, canonical_name="RedZed")
    session.add(w)
    session.flush()
    a_yt = BeneficiaryAccount(writer_id=w.id, account_code="C00616", catalog=Catalog.YT)
    a_me = BeneficiaryAccount(writer_id=w.id, account_code="JN0232", catalog=Catalog.MECH)
    session.add_all([a_yt, a_me])
    session.flush()
    session.add(Statement(batch_id=b_yt.id, account_id=a_yt.id, period_code="PUB25H2",
                          version=1, parse_status=ParseStatus.PARSED,
                          detail_sum=Decimal("1280.275500"), line_count=3380))
    session.add(Statement(batch_id=b_me.id, account_id=a_me.id, period_code="PUB25H2",
                          version=1, parse_status=ParseStatus.PARSED,
                          detail_sum=Decimal("35014.662834"), line_count=13482))
    session.commit()


def test_upload_statements_returns_real_amounts(session, client):
    holder = {}
    _seed(session, holder)
    r = client.get(f"/admin/statements/uploads/{holder['id']}/statements")
    assert r.status_code == 200, r.text
    body = r.json()
    by_code = {s["account_code"]: s for s in body["statements"]}
    assert Decimal(by_code["C00616"]["amount"]) == Decimal("1280.275500")
    assert Decimal(by_code["JN0232"]["amount"]) == Decimal("35014.662834")
    # rounded whole-dollar totals the UI shows
    assert round(sum(float(s["amount"]) for s in body["statements"])) == 36295
    assert by_code["C00616"]["catalog"] == "YT"
    assert by_code["JN0232"]["line_count"] == 13482


def test_upload_statements_reports_unresolved_and_batches(session, client):
    """Feature B (US-B1): placeholder writers (kind IS NULL) surface as
    `completeness.unresolved_writers`, and the distinct batches are returned so
    the upload modal can link its post-ingest banner to the gate view."""
    holder = {}
    _seed(session, holder)
    body = client.get(f"/admin/statements/uploads/{holder['id']}/statements").json()
    # both statements belong to the one placeholder writer "RedZed"
    assert body["completeness"]["unresolved_writers"] == 1
    # two distinct batches (YT + MECH)
    assert len(body["batch_ids"]) == 2
    assert body["batch_ids"] == sorted(body["batch_ids"])
    # writer_name is now the canonical string, not a serialized object
    assert body["statements"][0]["writer_name"] == "RedZed"


def test_completeness_and_account_summary(session, client):
    """The endpoint reports pairing completeness, the PDF account summary, and
    reconciliation (Σ line earnings vs PDF 'Royalties calculated')."""
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()
    up = StatementUpload(file_count=3)
    session.add(up)
    session.flush()
    batch = StatementBatch(publisher_id=pub.id, label="YT 2025H2", period_code="PUB25H2",
                           catalog=Catalog.YT, upload_id=up.id)
    session.add(batch)
    session.flush()
    w = Writer(publisher_id=pub.id, canonical_name="W")
    session.add(w)
    session.flush()

    def acct(code):
        a = BeneficiaryAccount(writer_id=w.id, account_code=code, catalog=Catalog.YT)
        session.add(a)
        session.flush()
        return a

    # 1) paired + reconciled (detail == calculated within $0.01), full ledger
    s_ok = Statement(batch_id=batch.id, account_id=acct("C001").id, period_code="PUB25H2",
                     version=1, parse_status=ParseStatus.PARSED,
                     pdf_path="/x/a.pdf", xlsx_path="/x/a.xlsx",
                     detail_sum=Decimal("100.00"), calculated=Decimal("100.005"),
                     payable=Decimal("90.00"), cheque_amount=Decimal("90.00"))
    # 2) paired but NOT reconciled (detail vs calculated diverge)
    s_bad = Statement(batch_id=batch.id, account_id=acct("C002").id, period_code="PUB25H2",
                      version=1, parse_status=ParseStatus.PARSED,
                      pdf_path="/x/b.pdf", xlsx_path="/x/b.xlsx",
                      detail_sum=Decimal("50.00"), calculated=Decimal("999.00"))
    # 3) xlsx-only → missing_pdf, reconciliation not computable
    s_xonly = Statement(batch_id=batch.id, account_id=acct("C003").id, period_code="PUB25H2",
                        version=1, parse_status=ParseStatus.PARSED,
                        xlsx_path="/x/c.xlsx", detail_sum=Decimal("10.00"))
    session.add_all([s_ok, s_bad, s_xonly])
    session.flush()
    up.stats = {"sort": {"statement_ids": [s_ok.id, s_bad.id, s_xonly.id]}}
    session.commit()

    body = client.get(f"/admin/statements/uploads/{up.id}/statements").json()
    c = body["completeness"]
    assert c["total"] == 3
    assert c["paired"] == 2
    assert c["missing_pdf"] == 1
    assert c["reconciled"] == 1
    assert c["unreconciled"] == 1

    by = {s["account_code"]: s for s in body["statements"]}
    assert by["C001"]["reconciled"] is True
    assert by["C001"]["account_summary"]["payable"] == "90.000000"
    assert by["C001"]["account_summary"]["cheque_amount"] == "90.000000"
    assert by["C002"]["reconciled"] is False
    assert by["C003"]["reconciled"] is None   # no PDF → not computable
    assert by["C003"]["pdf_present"] is False


def test_upload_statements_404_for_unknown_upload(session, client):
    assert client.get("/admin/statements/uploads/999/statements").status_code == 404


def test_reused_batch_scopes_by_upload_statement_ids(session, client):
    """Two uploads add statements to the SAME batch (period+catalog reused).
    Each upload must return only its own statements — via stats.sort.
    statement_ids, not batch.upload_id (regression: PDF uploads into an
    existing period showed nothing)."""
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()
    up_a = StatementUpload(file_count=1)
    up_b = StatementUpload(file_count=1)
    session.add_all([up_a, up_b])
    session.flush()
    # one shared batch, created by upload A
    batch = StatementBatch(publisher_id=pub.id, label="YT 2025H2", period_code="PUB25H2",
                           catalog=Catalog.YT, upload_id=up_a.id)
    session.add(batch)
    session.flush()
    w1 = Writer(publisher_id=pub.id, canonical_name="RedZed")
    w2 = Writer(publisher_id=pub.id, canonical_name="Ben Sidran")
    session.add_all([w1, w2])
    session.flush()
    a1 = BeneficiaryAccount(writer_id=w1.id, account_code="C00616", catalog=Catalog.YT)
    a2 = BeneficiaryAccount(writer_id=w2.id, account_code="C00008", catalog=Catalog.YT)
    session.add_all([a1, a2])
    session.flush()
    s1 = Statement(batch_id=batch.id, account_id=a1.id, period_code="PUB25H2", version=1,
                   parse_status=ParseStatus.PARSED, detail_sum=Decimal("1280.28"))
    s2 = Statement(batch_id=batch.id, account_id=a2.id, period_code="PUB25H2", version=1,
                   parse_status=ParseStatus.PARSED, detail_sum=Decimal("5.25"))
    session.add_all([s1, s2])
    session.flush()
    up_a.stats = {"sort": {"statement_ids": [s1.id]}}
    up_b.stats = {"sort": {"statement_ids": [s2.id]}}  # B added s2 to A's batch
    session.commit()

    a_codes = [s["account_code"] for s in
               client.get(f"/admin/statements/uploads/{up_a.id}/statements").json()["statements"]]
    b_codes = [s["account_code"] for s in
               client.get(f"/admin/statements/uploads/{up_b.id}/statements").json()["statements"]]
    assert a_codes == ["C00616"]      # not both — scoped to A's own statements
    assert b_codes == ["C00008"]      # B sees its statement despite reusing A's batch
