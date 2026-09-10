"""Client list FIRST, statements second — and roster counts from the sheets.

The roster is the client list: uploading it into an empty database must create
every row as a client (they simply have no statements yet), and statements
arriving later must attach to those existing identities rather than minting
duplicates. Roster counts come from sheet membership, so a person listed on
BOTH sheets counts as a client AND a commission partner.
"""

import glob
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import BeneficiaryAccount, Writer, WriterKind
from app.routers.auth import get_user
from app.routers.statements_admin import statements_admin_router
from app.services.client_import import importer
from app.services.client_import.parser import ClientRow, ParsedEmail
from app.services.client_import.parser import WriterKind as ParsedKind
from app.services.statement_ingest.reconcile import reconcile_ingestion

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "statements")
ADMIN_EMAIL = "listfirst-admin@verax.app"


@pytest.fixture()
def admin_user(session):
    user = User(email=ADMIN_EMAIL, username="listfirst-admin", royalty_per_stream=0)
    session.add(user)
    session.commit()
    return user


@pytest.fixture()
def client(session, admin_user, tmp_path, monkeypatch):
    monkeypatch.setenv("STATEMENTS_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("INGEST_INLINE", "1")
    app = FastAPI()
    app.include_router(statements_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


def _row(name, kind=ParsedKind.CLIENT, sheet="Client List", payee=None):
    return ClientRow(
        sheet=sheet, row_no=1, kind=kind, name=name, payee_name=payee or name,
        emails=[ParsedEmail("x@y.com", True)], contact_names=["Mgr"],
        catalogs=["YT", "MECH"], unknown_catalog_tokens=[], preferred_language="en",
    )


def _ingest_fixtures(client):
    parts = []
    for path in sorted(
        glob.glob(os.path.join(FIXTURES_DIR, "*.pdf"))
        + glob.glob(os.path.join(FIXTURES_DIR, "*.xlsx"))
    ):
        with open(path, "rb") as f:
            parts.append(("files", (os.path.basename(path), f.read())))
    assert client.post("/admin/statements/uploads", files=parts).status_code == 202


def test_client_list_before_any_statements_creates_the_whole_roster(session, client):
    """Import into an EMPTY database: every row becomes a client with no
    statements, then statements attach to those same identities."""
    rows = [
        _row("El Taiger"),
        _row("Javier Solis"),
        _row("Bello Musical"),
        _row("Client With No Earnings This Period"),
    ]
    importer.apply_rows(session, rows, confirmed_only=True)

    clients = session.query(Writer).filter(Writer.kind.isnot(None)).all()
    assert len(clients) == 4  # nothing matched yet — the list alone is the roster
    assert session.query(BeneficiaryAccount).count() == 0

    _ingest_fixtures(client)
    importer.apply_rows(session, rows, confirmed_only=True)

    # statements attached to the pre-existing identities; no duplicate clients
    named = {w.canonical_name for w in session.query(Writer).filter(Writer.kind.isnot(None))}
    assert {"El Taiger", "Javier Solis", "Bello Musical"} <= named
    for name, code in [("El Taiger", "C00650"), ("Javier Solis", "CSJ002"),
                       ("Bello Musical", "C00139a")]:
        acct = session.query(BeneficiaryAccount).filter(
            BeneficiaryAccount.account_code == code).one()
        assert session.get(Writer, acct.writer_id).canonical_name == name

    # the earnings-free client is still on the roster, just without statements
    empty = session.query(Writer).filter(
        Writer.canonical_name == "Client With No Earnings This Period").one()
    assert session.query(BeneficiaryAccount).filter(
        BeneficiaryAccount.writer_id == empty.id).count() == 0

    assert reconcile_ingestion(session)["ok"] is True


def test_a_name_on_both_sheets_becomes_two_entries(session):
    """One entry, one role.

    The same person is a client for their own catalog and a commission partner
    on other people's. Those are different bodies of money, so they are separate
    entries — the roster row is the unit that owns accounts, that the portal
    switcher toggles between, and that `publisher_owned` applies to.

    Merging them onto one row put commission statements (CS*) and royalty
    statements on a single entry, where nothing downstream could tell them
    apart. 18 entries in the delivered data are in that state.
    """
    rows = [
        _row("Solo Client"),
        _row("Dual Person"),
        _row("Dual Person", kind=ParsedKind.COMMISSION_PARTNER,
             sheet="Commission Partner List"),
        _row("Solo Partner", kind=ParsedKind.COMMISSION_PARTNER,
             sheet="Commission Partner List"),
    ]
    importer.apply_rows(session, rows, confirmed_only=True)

    clients = session.query(Writer).filter(Writer.is_client.is_(True)).count()
    partners = session.query(Writer).filter(Writer.is_commission_partner.is_(True)).count()
    assert (clients, partners) == (2, 2)          # 2 client rows, 2 partner rows

    # FOUR entries, not three: the dual person is two of them
    assert session.query(Writer).filter(Writer.kind.isnot(None)).count() == 4

    dual = session.query(Writer).filter(Writer.canonical_name == "Dual Person").all()
    assert len(dual) == 2
    # and neither is flagged as both, which is what let the money mix
    assert {(w.is_client, w.is_commission_partner) for w in dual} == {(True, False), (False, True)}


def test_reimporting_a_row_reuses_its_own_entry(session):
    """Idempotent per role: a second import must not keep minting entries."""
    rows = [
        _row("Dual Person"),
        _row("Dual Person", kind=ParsedKind.COMMISSION_PARTNER,
             sheet="Commission Partner List"),
    ]
    importer.apply_rows(session, rows, confirmed_only=True)
    importer.apply_rows(session, rows, confirmed_only=True)
    assert session.query(Writer).filter(Writer.canonical_name == "Dual Person").count() == 2


def test_dropping_a_row_drops_that_membership(session):
    """The uploaded list is the authority for who is on the roster."""
    importer.apply_rows(session, [
        _row("Solo Client"),
        _row("Solo Partner", kind=ParsedKind.COMMISSION_PARTNER,
             sheet="Commission Partner List"),
    ], confirmed_only=True)

    importer.apply_rows(session, [_row("Solo Client")], confirmed_only=True)
    assert session.query(Writer).filter(Writer.is_commission_partner.is_(True)).count() == 0
    assert session.query(Writer).filter(Writer.is_client.is_(True)).count() == 1


def test_payee_is_not_the_identity(session):
    """Distinct artists sharing one payee stay distinct clients."""
    rows = [
        _row("Artist One", payee="Shared Payee LLC"),
        _row("Artist Two", payee="Shared Payee LLC"),
    ]
    importer.apply_rows(session, rows, confirmed_only=True)
    names = {w.canonical_name for w in session.query(Writer).filter(Writer.kind.isnot(None))}
    assert names == {"Artist One", "Artist Two"}
    assert all(w.payee_name == "Shared Payee LLC"
               for w in session.query(Writer).filter(Writer.kind.isnot(None)))


def test_unmatched_accounts_are_not_clients(session, client):
    """Statements for names the client list doesn't have must NOT become
    clients: the account is still ingested (money is never dropped) but it is
    held as an unmatched record, excluded from the roster and from sending."""
    from app.models.statements import Statement
    from app.routers.writers_admin import list_writers

    # roster: only El Taiger. The fixtures also contain Javier Solis, Bello
    # Musical, etc. — none of them on the list.
    importer.apply_rows(session, [_row("El Taiger")], confirmed_only=True)
    _ingest_fixtures(client)

    unmatched = [w for w in session.query(Writer).filter(Writer.kind.is_(None)).all()
                 if session.query(BeneficiaryAccount).filter(
                     BeneficiaryAccount.writer_id == w.id).count()]
    assert unmatched, "unmatched statements must still be ingested, never dropped"

    # every unmatched record holds real statement data (money is preserved)
    for w in unmatched:
        accts = session.query(BeneficiaryAccount).filter(
            BeneficiaryAccount.writer_id == w.id).all()
        assert any(session.query(Statement).filter(
            Statement.account_id == a.id).count() for a in accts)

    # ...but none of them is a client on the roster
    assert all(not w.is_client and not w.is_commission_partner for w in unmatched)
