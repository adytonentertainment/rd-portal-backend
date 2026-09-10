"""Wiping the roster has to survive foreign keys being enforced.

SQLite does not enforce them unless asked, Postgres always does. A table left
out of the reset order therefore passed every local run and failed in the
deployed environment — half-done, with the roster already deleted. These tests
turn enforcement ON so local runs behave like production.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    AliasSource,
    BeneficiaryAccount,
    Catalog,
    Contact,
    ContactRole,
    Distribution,
    ParseStatus,
    PortalInvite,
    Publisher,
    Statement,
    StatementBatch,
    StatementLine,
    Writer,
    WriterAlias,
    WriterContact,
    WriterKind,
)
from app.routers.auth import get_user
from app.routers.writers_admin import _RESET_TABLES, writers_admin_router

ADMIN_EMAIL = "admin@verax.app"


@pytest.fixture()
def fk_session(session):
    """Postgres-like: enforce foreign keys."""
    conn = session.connection()
    conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    return session


@pytest.fixture()
def client(fk_session, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("ENVIRONMENT", "STAGING")
    u = User(email=ADMIN_EMAIL, username="admin", royalty_per_stream=0)
    fk_session.add(u)
    fk_session.commit()
    app = FastAPI()
    app.include_router(writers_admin_router)
    app.dependency_overrides[get_session] = lambda: fk_session
    app.dependency_overrides[get_user] = lambda: u
    return TestClient(app)


@pytest.fixture()
def full_roster(fk_session):
    """One of everything that points at something else."""
    s = fk_session
    pub = Publisher(name="Regalias Digitales")
    s.add(pub)
    s.flush()
    w = Writer(publisher_id=pub.id, canonical_name="Amenazzy", kind=WriterKind.CLIENT)
    s.add(w)
    s.flush()
    s.add(WriterAlias(writer_id=w.id, alias_name="Amenazzy (YouTube)", source=AliasSource.IMPORT))
    acct = BeneficiaryAccount(writer_id=w.id, account_code="C001", catalog=Catalog.YT)
    s.add(acct)
    s.flush()
    up = StatementBatch(publisher_id=pub.id, label="b", period_code="PUB26H1", catalog=Catalog.YT)
    s.add(up)
    s.flush()
    stmt = Statement(batch_id=up.id, account_id=acct.id, period_code="PUB26H1",
                     version=1, parse_status=ParseStatus.PARSED)
    s.add(stmt)
    s.flush()
    s.add(StatementLine(statement_id=stmt.id, row_no=1))
    s.add(Distribution(statement_id=stmt.id, writer_id=w.id, batch_id=up.id,
                       period_code="PUB26H1", catalog=Catalog.YT))
    c = Contact(email="a@b.com")
    s.add(c)
    s.flush()
    s.add(WriterContact(writer_id=w.id, contact_id=c.id, role=ContactRole.PRIMARY))
    s.add(PortalInvite(writer_id=w.id, email="a@b.com", token_hash="t",
                       expires_at=__import__("datetime").datetime.now()))
    s.commit()
    return w


def test_the_reset_clears_everything_with_foreign_keys_enforced(client, fk_session, full_roster):
    """The failure this catches: writer_alias was not in the list, so deleting
    the writers it pointed at raised a constraint error on Postgres."""
    res = client.post("/admin/writers/reset-all")
    assert res.status_code == 200, res.text

    for table in _RESET_TABLES:
        n = fk_session.execute(text(f"SELECT count(*) FROM {table}")).scalar()
        assert n == 0, f"{table} still holds {n} row(s)"


def test_the_admin_and_publisher_survive(client, fk_session, full_roster):
    """Wiping the roster must not sign the admin out or delete the publisher."""
    client.post("/admin/writers/reset-all")
    assert fk_session.query(User).count() == 1
    assert fk_session.query(Publisher).count() == 1


def test_every_table_pointing_into_the_reset_set_is_in_it(fk_session):
    """Structural guard: a new table with a FK into anything being cleared must
    be added to the order, or the reset breaks in production only."""
    from app.models.statements import Base

    cleared = set(_RESET_TABLES)
    targets = cleared | {"publisher"}
    missing = []
    for name, table in Base.metadata.tables.items():
        for fk in table.foreign_keys:
            parent = fk.column.table.name
            if parent in cleared and name not in cleared and name not in ("publisher", "User"):
                missing.append((name, parent))
    assert not missing, f"tables with a FK into the reset set but not in it: {missing}"
