"""/me/transactions must stay bounded without changing what the charts show.

The endpoint previously emitted one row per (song x country x source x income
type). For the heaviest real writer that was 475,886 rows / 179 MB / 826 MB of
server memory for ONE page load — an OOM on a small VPS, which takes every
writer's portal down. Song title is dropped from the dimension key and returned
separately as top works; the aggregate totals every chart draws must be
identical.
"""

from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    BeneficiaryAccount, Catalog, Contact, Distribution, ParseStatus, Publisher,
    Statement, StatementBatch, StatementLine, Writer, WriterContact, WriterKind,
)
from app.routers.auth import get_user
from app.routers.portal import me_router

EMAIL = "scale@example.com"


@pytest.fixture()
def world(session):
    user = User(email=EMAIL, username="scale", royalty_per_stream=0)
    session.add(user)
    session.flush()
    pub = Publisher(name="RD")
    session.add(pub)
    session.flush()
    writer = Writer(publisher_id=pub.id, canonical_name="Heavy Writer", kind=WriterKind.CLIENT)
    other = Writer(publisher_id=pub.id, canonical_name="Other Writer", kind=WriterKind.CLIENT)
    session.add_all([writer, other])
    session.flush()
    contact = Contact(email=EMAIL, user_id=user.id)
    session.add(contact)
    session.flush()
    session.add(WriterContact(writer_id=writer.id, contact_id=contact.id))
    acct = BeneficiaryAccount(writer_id=writer.id, account_code="C1", catalog=Catalog.YT)
    other_acct = BeneficiaryAccount(writer_id=other.id, account_code="C2", catalog=Catalog.YT)
    session.add_all([acct, other_acct])
    session.flush()
    batch = StatementBatch(publisher_id=pub.id, label="b", period_code="PUB26H1",
                           catalog=Catalog.YT)
    session.add(batch)
    session.flush()
    session.commit()
    return {"user": user, "writer": writer, "other": other, "account": acct,
            "other_account": other_acct, "batch": batch}


def _statement_with_lines(session, world, account, n_songs, countries, sources, types,
                          per_line="1.00", distribute=True, writer=None):
    st = Statement(batch_id=world["batch"].id, account_id=account.id,
                   period_code="PUB26H1", version=1, parse_status=ParseStatus.PARSED,
                   payable=Decimal("1"), line_count=0)
    session.add(st)
    session.flush()
    rows = 0
    for song in range(n_songs):
        for c in countries:
            for src in sources:
                for t in types:
                    session.add(StatementLine(
                        statement_id=st.id, row_no=rows + 1, song_title=f"Song {song}",
                        country=c, income_source=src, income_type=t,
                        units=Decimal("1"), earnings=Decimal(per_line),
                    ))
                    rows += 1
    st.line_count = rows
    session.flush()
    if distribute:
        session.add(Distribution(
            statement_id=st.id, writer_id=(writer or world["writer"]).id,
            batch_id=world["batch"].id, period_code="PUB26H1", catalog=Catalog.YT,
            portal_visible=True,
        ))
    session.commit()
    return st, rows


@pytest.fixture()
def client(session, world):
    app = FastAPI()
    app.include_router(me_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: world["user"]
    return TestClient(app)


def test_payload_does_not_grow_with_the_number_of_songs(session, world, client):
    """The whole point: 300 songs must not mean 300x the rows."""
    countries, sources, types = ["US", "GB", "CA"], ["YouTube Pub"], ["Embedded", "In Master"]
    _statement_with_lines(session, world, world["account"], 300, countries, sources, types)

    body = client.get("/me/transactions").json()
    dimension_rows = [r for r in body if not r.get("is_song_row")]

    # dimensions are bounded by country x source x type, NOT by song count
    assert len(dimension_rows) == len(countries) * len(sources) * len(types)
    # the old shape would have been 300 songs x that same product
    assert len(dimension_rows) < 300


def test_totals_are_identical_to_the_raw_line_sum(session, world, client):
    """Charts must draw exactly the same money after the change."""
    _, n = _statement_with_lines(session, world, world["account"], 50,
                                 ["US", "MX"], ["YouTube Pub", "YouTube Red"],
                                 ["Embedded"], per_line="0.50")
    expected = Decimal("0.50") * n

    body = client.get("/me/transactions").json()
    dimension_total = sum(Decimal(str(r["amount"])) for r in body if not r.get("is_song_row"))
    assert dimension_total == expected

    # and each dimension still splits correctly
    by_territory = {}
    for r in body:
        if r.get("is_song_row"):
            continue
        by_territory[r["territory"]] = by_territory.get(r["territory"], Decimal(0)) + Decimal(str(r["amount"]))
    assert set(by_territory) == {"US", "MX"}
    assert by_territory["US"] == expected / 2


def test_song_rows_are_flagged_and_carry_titles(session, world, client):
    _statement_with_lines(session, world, world["account"], 5, ["US"], ["YouTube Pub"],
                          ["Embedded"])
    body = client.get("/me/transactions").json()
    songs = [r for r in body if r.get("is_song_row")]
    dims = [r for r in body if not r.get("is_song_row")]

    assert songs, "top works must still be available for the songs list"
    assert all(r.get("title") for r in songs)
    # dimension rows carry no title, so a client cannot mistake them for works
    assert all("title" not in r for r in dims)


def test_still_scoped_to_the_contacts_own_writers(session, world, client):
    """Aggregation must not widen access to another client's data."""
    _statement_with_lines(session, world, world["account"], 3, ["US"], ["YouTube Pub"],
                          ["Embedded"], per_line="1.00")
    _statement_with_lines(session, world, world["other_account"], 3, ["JP"],
                          ["YouTube Pub"], ["Embedded"], per_line="99.00",
                          writer=world["other"])

    body = client.get("/me/transactions").json()
    assert all(r.get("territory") != "JP" for r in body if not r.get("is_song_row"))
    total = sum(Decimal(str(r["amount"])) for r in body if not r.get("is_song_row"))
    assert total == Decimal("3")  # 3 lines x $1, none of the other writer's $99s


def test_undistributed_statements_are_excluded(session, world, client):
    _statement_with_lines(session, world, world["account"], 2, ["US"], ["YouTube Pub"],
                          ["Embedded"], distribute=False)
    assert client.get("/me/transactions").json() == []
