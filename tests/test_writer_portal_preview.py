"""GET /admin/writers/{id}/portal-view — read-only preview of a client's portal.

RD asked to click into a client and see their data. The implementation NOT
chosen was impersonation: minting the admin a client session puts a writable
client context in an admin browser, and everything after that is a promise
rather than a mechanism. These tests pin the two properties that make the
chosen design safe instead:

  * it is admin-only, and
  * it has no write counterpart — read-only is structural, not a hidden button.

Plus the property that makes it USEFUL: the numbers are the portal's own,
produced by the portal's own aggregation functions, so the preview cannot
drift from the page it claims to be previewing.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    BeneficiaryAccount,
    Catalog,
    Distribution,
    Publisher,
    Statement,
    StatementBatch,
    StatementLine,
    Writer,
)
from app.routers.auth import get_user
from app.routers.portal import earnings_for_writers, transactions_for_writers
from app.routers.writers_admin import writers_admin_router

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
    app.include_router(writers_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


def _seed(session):
    """One client with one published statement and two song lines."""
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()

    writer = Writer(publisher_id=pub.id, canonical_name="RedZed", payee_name="R. Zed")
    session.add(writer)
    session.flush()

    account = BeneficiaryAccount(writer_id=writer.id, account_code="C00616", catalog=Catalog.YT)
    session.add(account)
    session.flush()

    batch = StatementBatch(publisher_id=pub.id, label="YT 2025H2",
                           period_code="PUB25H2", catalog=Catalog.YT)
    session.add(batch)
    session.flush()

    stmt = Statement(
        batch_id=batch.id, account_id=account.id, period_code="PUB25H2", version=1,
        detail_sum=Decimal("1000.00"), payable=Decimal("800.00"),
        before_tax=Decimal("900.00"), statement_date=date(2026, 1, 31),
    )
    session.add(stmt)
    session.flush()

    session.add_all([
        StatementLine(statement_id=stmt.id, row_no=1, song_title="Adiós Amigo",
                      country="US", income_source="YouTube Pub", income_type="Embedded",
                      earnings=Decimal("600.00"), units=Decimal("100")),
        StatementLine(statement_id=stmt.id, row_no=2, song_title="El Café",
                      country="ROW", income_source="YouTube Pub", income_type="Embedded",
                      earnings=Decimal("400.00"), units=Decimal("50")),
    ])
    session.add(Distribution(
        statement_id=stmt.id, writer_id=writer.id, batch_id=batch.id,
        period_code="PUB25H2", catalog=Catalog.YT, portal_visible=True,
    ))
    session.commit()
    return writer.id


def test_preview_returns_the_clients_own_figures(session, client):
    writer_id = _seed(session)
    r = client.get(f"/admin/writers/{writer_id}/portal-view")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["writer"]["name"] == "RedZed"
    assert body["writer"]["payee_name"] == "R. Zed"
    assert body["read_only"] is True
    assert body["preview"] is True
    assert Decimal(body["earnings"]["payable"]) == Decimal("800.00")


def test_preview_matches_the_portal_exactly(session, client):
    """The preview must BE the portal's numbers, not a second opinion on them.

    Both come from the same functions, so this fails the moment someone
    reimplements one of them — which is precisely when a client would be shown
    one total and the admin another.
    """
    writer_id = _seed(session)
    body = client.get(f"/admin/writers/{writer_id}/portal-view").json()

    assert body["earnings"] == earnings_for_writers(session, [writer_id])
    assert body["transactions"] == transactions_for_writers(session, [writer_id])


def test_preview_is_scoped_to_one_client(session, client):
    """A preview of client A must never leak client B's money."""
    writer_id = _seed(session)
    pub_id = session.query(Publisher).first().id
    other = Writer(publisher_id=pub_id, canonical_name="Someone Else")
    session.add(other)
    session.commit()

    body = client.get(f"/admin/writers/{writer_id}/portal-view").json()
    assert body["writer"]["id"] == writer_id
    # The other client has no statements, so its preview is empty rather than
    # inheriting the first one's.
    other_body = client.get(f"/admin/writers/{other.id}/portal-view").json()
    assert Decimal(other_body["earnings"]["payable"]) == Decimal("0")
    assert other_body["transactions"] == []


def test_unknown_client_is_404(client):
    assert client.get("/admin/writers/999999/portal-view").status_code == 404


def test_preview_has_no_write_counterpart(session, client):
    """Read-only is structural. If a POST/PATCH/DELETE ever appears on this
    path, hiding a button in the UI will not be what stops it being used."""
    writer_id = _seed(session)
    for method in ("post", "patch", "put", "delete"):
        resp = getattr(client, method)(f"/admin/writers/{writer_id}/portal-view")
        assert resp.status_code == 405, f"{method.upper()} should not be routed"


def test_preview_requires_admin(session, admin_user, monkeypatch):
    """Without the admin allow-list the route must refuse, not degrade."""
    writer_id = _seed(session)
    monkeypatch.setenv("ADMIN_EMAILS", "someone-else@verax.app")
    app = FastAPI()
    app.include_router(writers_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    non_admin = TestClient(app)
    assert non_admin.get(f"/admin/writers/{writer_id}/portal-view").status_code in (401, 403)
