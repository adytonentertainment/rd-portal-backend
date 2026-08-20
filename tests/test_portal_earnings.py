"""The writer portal must report NET (payable), not gross line earnings.

A statement is a waterfall, not a single number: this period's royalties, plus
any balance brought forward, minus recoupment and below-threshold carry-out,
minus commission. So payable can EXCEED this period's gross (carry-forward) or
be zero despite real earnings (fully recouped). The portal headline must equal
what the PDF says is payable — anything else contradicts the writer's own
statement.
"""

from decimal import Decimal

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    PortalInvite,
    BeneficiaryAccount, Catalog, Contact, Distribution, ParseStatus, Publisher,
    Statement, StatementBatch, Writer, WriterContact, WriterKind,
)
from app.routers.auth import get_user
from app.routers.portal import me_router

EMAIL = "writer@example.com"


@pytest.fixture()
def world(session):
    user = User(email=EMAIL, username="writer", royalty_per_stream=0)
    session.add(user)
    session.flush()
    pub = Publisher(name="RD")
    session.add(pub)
    session.flush()
    writer = Writer(publisher_id=pub.id, canonical_name="A Writer", kind=WriterKind.CLIENT)
    session.add(writer)
    session.flush()
    contact = Contact(email=EMAIL, user_id=user.id)
    session.add(contact)
    session.flush()
    session.add(WriterContact(writer_id=writer.id, contact_id=contact.id, user_id=user.id))
    session.add(PortalInvite(writer_id=writer.id, email=EMAIL, token_hash="granted-1",
                             expires_at=datetime.now(), accepted_at=datetime.now()))
    acct = BeneficiaryAccount(writer_id=writer.id, account_code="C00001", catalog=Catalog.YT)
    session.add(acct)
    session.flush()
    session.commit()
    return {"user": user, "writer": writer, "account": acct, "publisher": pub}


def _distribute(session, world, period, **amounts):
    batch = StatementBatch(
        publisher_id=world["publisher"].id, label=period, period_code=period,
        catalog=Catalog.YT,
    )
    session.add(batch)
    session.flush()
    st = Statement(
        batch_id=batch.id, account_id=world["account"].id, period_code=period,
        version=1, parse_status=ParseStatus.PARSED,
        pdf_path=f"{period}.pdf", xlsx_path=f"{period}.xlsx", line_count=1,
        **{k: Decimal(str(v)) for k, v in amounts.items()},
    )
    session.add(st)
    session.flush()
    session.add(Distribution(
        statement_id=st.id, writer_id=world["writer"].id, batch_id=batch.id,
        period_code=period, catalog=Catalog.YT, portal_visible=True,
    ))
    session.commit()
    return st


@pytest.fixture()
def client(session, world):
    app = FastAPI()
    app.include_router(me_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: world["user"]
    return TestClient(app)


def test_payable_can_exceed_this_periods_gross(session, world, client):
    """Carry-forward case (real: Charlie Hunter earned $54.64, was paid $211.43).
    Reporting gross would UNDERSTATE what the writer is owed."""
    _distribute(session, world, "PUB25H2",
                detail_sum="54.64", calculated="54.64", carried_forward_in="156.79",
                recouped="0", before_tax="211.43", payable="211.43")

    body = client.get("/me/earnings").json()
    assert Decimal(body["gross"]) == Decimal("54.64")
    assert Decimal(body["carried_forward_in"]) == Decimal("156.79")
    assert Decimal(body["payable"]) == Decimal("211.43")   # the headline
    assert Decimal(body["payable"]) > Decimal(body["gross"])


def test_fully_recouped_writer_is_owed_nothing_despite_earnings(session, world, client):
    """543 real statements look like this. Showing gross would tell a writer
    they have money coming when they do not."""
    _distribute(session, world, "PUB26H1",
                detail_sum="38009.34", calculated="38009.34", recouped="-38009.34",
                before_tax="0", payable="0")

    body = client.get("/me/earnings").json()
    assert Decimal(body["gross"]) == Decimal("38009.34")
    assert Decimal(body["recouped"]) == Decimal("-38009.34")
    assert Decimal(body["payable"]) == Decimal("0")


def test_commission_is_the_gap_between_before_tax_and_payable(session, world, client):
    """Real: Malagon Publishing, before_tax 40,844.61 -> payable 36,760.15."""
    _distribute(session, world, "PUB25H2",
                detail_sum="40844.61", calculated="40844.61",
                before_tax="40844.61", payable="36760.15")

    body = client.get("/me/earnings").json()
    assert Decimal(body["commission"]) == Decimal("40844.61") - Decimal("36760.15")
    assert Decimal(body["payable"]) == Decimal("36760.15")


def test_earnings_headline_equals_the_statements_page(session, world, client):
    """The two portal surfaces must never disagree — that contradiction is the
    bug this endpoint exists to remove."""
    _distribute(session, world, "PUB25H2", detail_sum="100", calculated="100",
                before_tax="100", payable="90")
    _distribute(session, world, "PUB26H1", detail_sum="200", calculated="200",
                carried_forward_in="10", before_tax="210", payable="150")

    earnings = client.get("/me/earnings").json()
    statements = client.get("/me/statements").json()
    assert Decimal(earnings["payable"]) == sum(Decimal(s["payable"]) for s in statements)
    assert {p["period_code"] for p in earnings["periods"]} == {"PUB25H2", "PUB26H1"}


def test_undistributed_statements_are_not_counted(session, world, client):
    """Only what the publisher actually released to the portal."""
    st = _distribute(session, world, "PUB25H2", detail_sum="100", calculated="100",
                     before_tax="100", payable="90")
    dist = session.query(Distribution).filter(Distribution.statement_id == st.id).one()
    dist.portal_visible = False
    session.commit()

    body = client.get("/me/earnings").json()
    assert Decimal(body["payable"]) == Decimal("0")
    assert body["statements"] == 0
