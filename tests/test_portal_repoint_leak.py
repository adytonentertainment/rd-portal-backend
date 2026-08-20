"""Re-pointing an account must move portal access with it.

`Distribution.writer_id` is frozen at publish time. Accounts get re-pointed
afterwards — a client-list import correcting who an account belongs to — and
nothing rewrites those rows. Scoping portal reads on that frozen value meant
the PREVIOUS client kept seeing the statement, its PDF and its full line-item
detail (song titles, territories, earnings) after the money had been
reassigned, while the rightful owner saw nothing.
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
    Statement, StatementBatch, StatementLine, Writer, WriterContact, WriterKind,
)
from app.routers.auth import get_user
from app.routers.portal import me_router

OLD_EMAIL, NEW_EMAIL = "old@example.com", "new@example.com"


@pytest.fixture()
def world(session):
    pub = Publisher(name="RD")
    session.add(pub)
    session.flush()

    old_w = Writer(publisher_id=pub.id, canonical_name="Previous Owner", kind=WriterKind.CLIENT)
    new_w = Writer(publisher_id=pub.id, canonical_name="Rightful Owner", kind=WriterKind.CLIENT)
    session.add_all([old_w, new_w])
    session.flush()

    users = {}
    for email, writer in ((OLD_EMAIL, old_w), (NEW_EMAIL, new_w)):
        u = User(email=email, username=email.split("@")[0], royalty_per_stream=0)
        session.add(u)
        session.flush()
        c = Contact(email=email, user_id=u.id)
        session.add(c)
        session.flush()
        session.add(WriterContact(writer_id=writer.id, contact_id=c.id, user_id=u.id))
        session.add(PortalInvite(writer_id=writer.id, email=email,
                                 token_hash=f"granted-{email}-{writer.id}",
                                 expires_at=datetime.now(), accepted_at=datetime.now()))
        users[email] = u

    acct = BeneficiaryAccount(writer_id=old_w.id, account_code="C00190",
                              display_name="Los Tucanes DeTijuana", catalog=Catalog.YT)
    session.add(acct)
    session.flush()
    batch = StatementBatch(publisher_id=pub.id, label="b", period_code="PUB26H1",
                           catalog=Catalog.YT)
    session.add(batch)
    session.flush()
    st = Statement(batch_id=batch.id, account_id=acct.id, period_code="PUB26H1",
                   version=1, parse_status=ParseStatus.PARSED,
                   payable=Decimal("188667.45"), detail_sum=Decimal("188667.45"),
                   line_count=1, pdf_path="x.pdf", xlsx_path="x.xlsx")
    session.add(st)
    session.flush()
    session.add(StatementLine(statement_id=st.id, row_no=1, song_title="Secret Song",
                              country="US", income_source="YouTube Pub",
                              income_type="Embedded", units=Decimal("1"),
                              earnings=Decimal("188667.45")))
    # published while the account still belonged to the OLD writer
    dist = Distribution(statement_id=st.id, writer_id=old_w.id, batch_id=batch.id,
                        period_code="PUB26H1", catalog=Catalog.YT, portal_visible=True)
    session.add(dist)
    session.commit()
    return {"users": users, "old": old_w, "new": new_w, "account": acct, "dist": dist}


def _client(session, user):
    app = FastAPI()
    app.include_router(me_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: user
    return TestClient(app)


def _repoint(session, world):
    """What a client-list import does: the account's real owner is corrected."""
    world["account"].writer_id = world["new"].id
    session.commit()


def test_previous_owner_loses_access_after_repointing(session, world):
    old = _client(session, world["users"][OLD_EMAIL])
    assert len(old.get("/me/statements").json()) == 1      # before: theirs

    _repoint(session, world)

    assert old.get("/me/statements").json() == []          # statement gone
    assert old.get("/me/transactions").json() == []        # line detail gone
    assert Decimal(old.get("/me/earnings").json()["payable"]) == Decimal("0")
    # and the PDF / breakdown are refused, not just hidden from the list
    dist_id = world["dist"].id
    assert old.get(f"/me/statements/{dist_id}").status_code == 404
    assert old.get(f"/me/statements/{dist_id}/breakdown").status_code == 404
    assert old.get(f"/me/statements/{dist_id}/pdf").status_code == 404


def test_rightful_owner_gains_access_after_repointing(session, world):
    new = _client(session, world["users"][NEW_EMAIL])
    assert new.get("/me/statements").json() == []          # before: not theirs

    _repoint(session, world)

    statements = new.get("/me/statements").json()
    assert len(statements) == 1
    assert statements[0]["writer_name"] == "Rightful Owner"   # current owner, not frozen
    assert Decimal(new.get("/me/earnings").json()["payable"]) == Decimal("188667.45")
    assert new.get(f"/me/statements/{world['dist'].id}").status_code == 200
    # the line detail follows too
    rows = new.get("/me/transactions").json()
    assert any(r.get("territory") == "US" for r in rows if not r.get("is_song_row"))


def test_the_leak_is_not_merely_hidden_in_the_list(session, world):
    """Direct-id access must be refused, not just filtered out of the listing —
    the old client knows the distribution id from before."""
    _repoint(session, world)
    old = _client(session, world["users"][OLD_EMAIL])
    for path in ("", "/breakdown", "/pdf"):
        assert old.get(f"/me/statements/{world['dist'].id}{path}").status_code == 404
