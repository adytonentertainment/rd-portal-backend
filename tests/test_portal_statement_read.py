"""Writer-portal statement PDF download + earnings breakdown, with scoping."""

from decimal import Decimal

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    PortalInvite,
    BeneficiaryAccount,
    Catalog,
    Contact,
    Distribution,
    ParseStatus,
    Publisher,
    Statement,
    StatementBatch,
    StatementLine,
    Writer,
    WriterContact,
    WriterKind,
)
from app.routers.auth import get_user
from app.routers.portal import me_router


@pytest.fixture()
def scenario(session, tmp_path):
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()
    b = StatementBatch(publisher_id=pub.id, label="YT 2026H1", period_code="PUB26H1",
                       catalog=Catalog.YT)
    session.add(b)
    session.flush()
    w = Writer(publisher_id=pub.id, canonical_name="RedZed", kind=WriterKind.CLIENT)
    session.add(w)
    session.flush()
    acct = BeneficiaryAccount(writer_id=w.id, account_code="C00616", catalog=Catalog.YT)
    session.add(acct)
    session.flush()

    pdf = tmp_path / "redzed.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake pdf bytes")
    s = Statement(batch_id=b.id, account_id=acct.id, period_code="PUB26H1", version=1,
                  parse_status=ParseStatus.PARSED, payable=Decimal("1280.28"),
                  pdf_path=str(pdf), line_count=4)
    session.add(s)
    session.flush()
    # line items across income types / countries / channels
    for it, src, country, chan, earn in [
        ("Streaming", "Spotify", "US", "Audio", "800.00"),
        ("Streaming", "Apple Music", "US", "Audio", "300.00"),
        ("Performance", "ICE", "DE", "Video", "150.00"),
        ("Streaming", "Spotify", "GB", "Audio", "30.28"),
    ]:
        session.add(StatementLine(
            statement_id=s.id, row_no=1, income_type=it, income_source=src,
            country=country, channel=chan, earnings=Decimal(earn)))
    session.flush()

    dist = Distribution(statement_id=s.id, writer_id=w.id, batch_id=b.id,
                        period_code="PUB26H1", catalog=Catalog.YT, portal_visible=True)
    session.add(dist)
    session.flush()

    rz_user = User(email="redzed@x.com", username="redzed", royalty_per_stream=0)
    st_user = User(email="stranger@x.com", username="stranger", royalty_per_stream=0)
    session.add_all([rz_user, st_user])
    session.flush()
    rz_contact = Contact(email="redzed@x.com", user_id=rz_user.id)
    st_contact = Contact(email="stranger@x.com", user_id=st_user.id)
    session.add_all([rz_contact, st_contact])
    session.flush()
    session.add(WriterContact(writer_id=w.id, contact_id=rz_contact.id, user_id=rz_user.id))
    session.add(PortalInvite(writer_id=w.id, email=rz_contact.email, token_hash="granted-rz",
                             expires_at=datetime.now(), accepted_at=datetime.now()))
    session.commit()
    return {"dist_id": dist.id, "rz_user": rz_user, "stranger_user": st_user}


def _client(session, holder):
    app = FastAPI()
    app.include_router(me_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: holder["user"]
    return TestClient(app)


def test_pdf_download_scoped(session, scenario):
    holder = {"user": scenario["rz_user"]}
    client = _client(session, holder)
    did = scenario["dist_id"]

    r = client.get(f"/me/statements/{did}/pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")

    # stranger cannot download
    holder["user"] = scenario["stranger_user"]
    assert client.get(f"/me/statements/{did}/pdf").status_code == 404


def test_breakdown_aggregates_and_scoped(session, scenario):
    holder = {"user": scenario["rz_user"]}
    client = _client(session, holder)
    did = scenario["dist_id"]

    b = client.get(f"/me/statements/{did}/breakdown").json()
    # income type: Streaming 1130.28 (sorted first), Performance 150.00
    by_type = {r["key"]: Decimal(r["earnings"]) for r in b["by_income_type"]}
    assert by_type["Streaming"] == Decimal("1130.28")
    assert by_type["Performance"] == Decimal("150.00")
    assert b["by_income_type"][0]["key"] == "Streaming"  # sorted desc
    # source: Spotify is top (800 + 30.28)
    assert b["by_source"][0]["key"] == "Spotify"
    assert Decimal(b["by_source"][0]["earnings"]) == Decimal("830.28")
    # country split present
    countries = {r["key"] for r in b["by_country"]}
    assert countries == {"US", "DE", "GB"}

    # stranger blocked
    holder["user"] = scenario["stranger_user"]
    assert client.get(f"/me/statements/{did}/breakdown").status_code == 404


def test_pdf_missing_file_is_404(session, scenario):
    # point the statement at a non-existent path
    from app.models.statements import Statement
    s = session.query(Statement).first()
    s.pdf_path = "/tmp/does-not-exist-xyz.pdf"
    session.commit()
    client = _client(session, {"user": scenario["rz_user"]})
    assert client.get(f"/me/statements/{scenario['dist_id']}/pdf").status_code == 404


# --- the spreadsheet, not just the PDF --------------------------------------
#
# Clients asked for their statements in a form they can work with: a PDF is for
# reading, not for summing. Every statement arrives as a PDF/XLSX pair and both
# are kept, so the portal hands back the publisher's own source file rather
# than a CSV rebuilt from parsed lines — a regenerated export would disagree
# with the PDF the moment parsing missed a column, and the two get read side by
# side.


@pytest.fixture()
def with_xlsx(session, scenario, tmp_path):
    """Attach a spreadsheet to the scenario's statement."""
    dist = session.get(Distribution, scenario["dist_id"])
    stmt = session.get(Statement, dist.statement_id)
    xlsx = tmp_path / "redzed.xlsx"
    xlsx.write_bytes(b"PK\x03\x04 fake xlsx bytes")
    stmt.xlsx_path = str(xlsx)
    session.commit()
    return scenario


def test_xlsx_download_scoped(session, with_xlsx):
    holder = {"user": with_xlsx["rz_user"]}
    client = _client(session, holder)
    did = with_xlsx["dist_id"]

    r = client.get(f"/me/statements/{did}/xlsx")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert r.content.startswith(b"PK")
    # named for a person filing it, not for the storage layout
    assert ".xlsx" in r.headers.get("content-disposition", "")

    # A download route must not become a way around the ownership check.
    holder["user"] = with_xlsx["stranger_user"]
    assert client.get(f"/me/statements/{did}/xlsx").status_code == 404


def test_xlsx_missing_is_404_not_a_broken_file(session, scenario):
    """Older statements may have no spreadsheet. Say so rather than serving
    zero bytes that open as a corrupt workbook."""
    client = _client(session, {"user": scenario["rz_user"]})
    r = client.get(f"/me/statements/{scenario['dist_id']}/xlsx")
    assert r.status_code == 404


def test_rows_say_which_files_exist(session, with_xlsx):
    """The list drives the buttons, so it has to say which files are there —
    offering a download that 404s is worse than not offering it."""
    client = _client(session, {"user": with_xlsx["rz_user"]})
    rows = client.get("/me/statements").json()
    assert rows
    assert rows[0]["has_pdf"] is True
    assert rows[0]["has_xlsx"] is True

    summary = client.get(f"/me/statements/{with_xlsx['dist_id']}").json()
    assert summary["has_pdf"] is True and summary["has_xlsx"] is True
