"""HTTP surface for client import: upload -> queue -> resolve (infra PRD §10)."""

import io

import openpyxl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import BeneficiaryAccount, Catalog, Publisher, Writer
from app.routers.auth import get_user
from app.routers.clients_import_admin import client_import_admin_router

ADMIN_EMAIL = "admin@verax.app"


@pytest.fixture()
def admin_user(session):
    user = User(email=ADMIN_EMAIL, username="ci-admin", royalty_per_stream=0)
    session.add(user)
    session.commit()
    return user


@pytest.fixture()
def client(session, admin_user, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    app = FastAPI()
    app.include_router(client_import_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


def _seed_account(session, code, display, catalog):
    pub = session.query(Publisher).first() or Publisher(name="Regalias Digitales")
    if pub.id is None:
        session.add(pub)
        session.flush()
    w = Writer(publisher_id=pub.id, canonical_name=display)
    session.add(w)
    session.flush()
    session.add(BeneficiaryAccount(writer_id=w.id, account_code=code, catalog=catalog))
    session.commit()


def _make_xlsx(rows):
    """rows: list of (name, email, contact, payee, admin_type, lang, quarterly)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Client List"
    ws.append(["Artist / Publisher Name", "Contact Email", "Contact Name",
               "Payee Name", "Admin Type (YT Only / MLC Only / both)",
               "Preferred Language (EN/ES)", "Quarterly Client?"])
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _upload(client, data):
    return client.post(
        "/admin/client-imports",
        files={"file": ("Client List for Verax.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )


def test_upload_queue_resolve_flow(client, session):
    # One exact-matching account and one that won't auto-match (unrelated name).
    _seed_account(session, "JN0232", "RedZed", Catalog.MECH)
    _seed_account(session, "C00901", "Zed Kollective", Catalog.YT)

    data = _make_xlsx([
        ("RedZed", "redzed@x.com", "RZ", "RedZed", "MLC", "EN", None),
        ("Atu", "atu@x.com", "Atu", "Atupele Ndisale", "YT", "EN", None),
    ])

    r = _upload(client, data)
    assert r.status_code == 201, r.text
    body = r.json()
    import_id = body["id"]
    assert body["stats"]["rows_matched"] == 1  # only RedZed auto-matches

    # Queue lists the one unmatched row (Atu) and the unlisted account (C00901).
    q = client.get(f"/admin/client-imports/{import_id}/queue").json()
    assert q["counts"]["unmatched"] == 1
    assert "C00901" in q["unlisted_accounts"]
    unmatched = q["unmatched"][0]
    assert unmatched["name"] == "Atu"

    # Resolving to a bogus account is rejected.
    bad = client.post(f"/admin/client-imports/{import_id}/resolve",
                      json={"sheet": "Client List", "row_no": unmatched["row_no"],
                            "account_codes": ["NOPE999"]})
    assert bad.status_code == 422

    # Resolve Atu -> C00901.
    ok = client.post(f"/admin/client-imports/{import_id}/resolve",
                     json={"sheet": "Client List", "row_no": unmatched["row_no"],
                           "account_codes": ["C00901"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["repointed_accounts"] == 1

    # Queue shrinks; the same row can't be resolved twice.
    q2 = client.get(f"/admin/client-imports/{import_id}/queue").json()
    assert q2["counts"]["unmatched"] == 0
    again = client.post(f"/admin/client-imports/{import_id}/resolve",
                        json={"sheet": "Client List", "row_no": unmatched["row_no"],
                              "account_codes": ["C00901"]})
    assert again.status_code == 409

    # C00901 now belongs to a real client writer named after the payee.
    acct = session.query(BeneficiaryAccount).filter(
        BeneficiaryAccount.account_code == "C00901").one()
    w = session.get(Writer, acct.writer_id)
    assert w.canonical_name == "Atu"  # artist identity, not the payee
    assert w.payee_name == "Atupele Ndisale"
    assert w.kind is not None


def test_apply_hash_guard(client, session):
    _seed_account(session, "JN0232", "RedZed", Catalog.MECH)
    data = _make_xlsx([("RedZed", "r@x.com", "RZ", "RedZed", "MLC", "EN", None)])
    import_id = _upload(client, data).json()["id"]

    # Applying a different file than was reviewed is refused.
    other = _make_xlsx([("RedZed", "r@x.com", "RZ", "RedZed", "YT", "EN", None)])
    r = client.post(f"/admin/client-imports/{import_id}/apply",
                    files={"file": ("x.xlsx", other, "application/octet-stream")})
    assert r.status_code == 409

    # The reviewed file applies.
    r = client.post(f"/admin/client-imports/{import_id}/apply",
                    files={"file": ("x.xlsx", data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    assert r.json()["apply"]["repointed_accounts"] == 1
