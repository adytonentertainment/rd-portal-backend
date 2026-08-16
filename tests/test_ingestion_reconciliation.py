"""Automated ingestion-correctness harness.

Drives the REAL fixture statements through the full pipeline (upload -> sort
-> parse) plus a client-list import that reproduces the group/member shape of
the production data (a "(Luna Negra)" group parent alongside exact member
rows), then uses the reconciliation engine to PROVE the DB matches the ground
truth in the filenames — no manual comparison.

Also proves the checker has teeth: deliberately corrupting ownership must be
flagged.
"""

import glob
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import BeneficiaryAccount, Writer
from app.routers.auth import get_user
from app.routers.statements_admin import statements_admin_router
from app.services.client_import import importer
from app.services.client_import.parser import ClientRow, ParsedEmail
from app.services.client_import.parser import WriterKind as ParsedKind
from app.services.statement_ingest.reconcile import reconcile_ingestion

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "statements")
ADMIN_EMAIL = "recon-admin@verax.app"


@pytest.fixture()
def admin_user(session):
    user = User(email=ADMIN_EMAIL, username="recon-admin", royalty_per_stream=0)
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


def _row(name, payee=None):
    return ClientRow(
        sheet="Client List", row_no=1, kind=ParsedKind.CLIENT, name=name,
        payee_name=payee or name,
        emails=[ParsedEmail(f"{name.split()[0].lower()}@x.com", True)],
        contact_names=["Mgr"], catalogs=["YT", "MECH"],
        unknown_catalog_tokens=[], preferred_language="en",
    )


def _ingest_fixtures(client):
    parts = []
    for path in sorted(
        glob.glob(os.path.join(FIXTURES_DIR, "*.pdf"))
        + glob.glob(os.path.join(FIXTURES_DIR, "*.xlsx"))
    ):
        with open(path, "rb") as f:
            parts.append(("files", (os.path.basename(path), f.read())))
    response = client.post("/admin/statements/uploads", files=parts)
    assert response.status_code == 202
    assert response.json()["status"] == "done"


def test_pipeline_plus_import_reconciles_clean(client, session):
    _ingest_fixtures(client)

    # Client list mirrors production's trap: "Luna Negra" claims the group,
    # "Bello Musical" is an exact member (C00139a "Bello Musical (Luna Negra)").
    rows = [
        _row("Bello Musical"),
        _row("El Taiger"),
        _row("Javier Solis"),
        _row("Luna Negra"),  # group parent applied AFTER the members
    ]
    importer.apply_rows(session, rows, confirmed_only=False)

    report = reconcile_ingestion(session)
    assert report["violation_counts"] == {
        "file_identity": 0,
        "account_identity": 0,
        "exact_owner": 0,
        "distribution_owner": 0,
    }
    assert report["ok"] is True

    # and specifically: the member kept its account, the group parent didn't steal it
    bello_acct = session.query(BeneficiaryAccount).filter(
        BeneficiaryAccount.account_code == "C00139a").one()
    assert session.get(Writer, bello_acct.writer_id).canonical_name == "Bello Musical"


def test_reconcile_detects_a_stolen_account(client, session):
    _ingest_fixtures(client)
    report = reconcile_ingestion(session)
    assert report["ok"] is True  # pipeline alone is clean

    # steal El Taiger's account for another writer -> must be flagged
    acct = session.query(BeneficiaryAccount).filter(
        BeneficiaryAccount.account_code == "C00650").one()
    thief = session.query(Writer).filter(Writer.id != acct.writer_id).first()
    acct.writer_id = thief.id
    session.commit()

    report = reconcile_ingestion(session)
    assert report["ok"] is False
    assert report["violation_counts"]["exact_owner"] == 1
    assert report["violations"]["exact_owner"][0]["account"] == "C00650"
