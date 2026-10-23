"""Adding beneficiary codes to a client before statements arrive (RD, Oct 2026:
"we can't insert beneficiary codes in the client detail")."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import BeneficiaryAccount, Catalog, Publisher, Statement, Writer, WriterKind
from app.routers.auth import get_user
from app.routers.writers_admin import writers_admin_router


@pytest.fixture()
def api(session):
    app = FastAPI()
    app.include_router(writers_admin_router)
    admin = User(email="admin@example.com", username="admin", role="admin", admin_approved=True, royalty_per_stream=0)
    session.add(admin)
    session.commit()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin
    return TestClient(app)


@pytest.fixture()
def clients(session):
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()
    client = Writer(publisher_id=pub.id, canonical_name="B-Legit", kind=WriterKind.CLIENT)
    partner = Writer(publisher_id=pub.id, canonical_name="B-Legit", kind=WriterKind.COMMISSION_PARTNER)
    other = Writer(publisher_id=pub.id, canonical_name="Rata Blanca", kind=WriterKind.CLIENT)
    session.add_all([client, partner, other])
    session.flush()
    session.add(BeneficiaryAccount(writer_id=other.id, account_code="C00122", catalog=Catalog.YT))
    session.commit()
    return client, partner, other


def test_codes_are_added_with_their_catalog(api, session, clients):
    client, partner, _ = clients
    r = api.post(f"/admin/writers/{client.id}/accounts", json={"account_code": " jn0188 "})
    assert r.status_code == 201 and r.json()["catalog"] == "MECH"
    assert api.post(f"/admin/writers/{client.id}/accounts", json={"account_code": "C00354"}).json()["catalog"] == "YT"
    assert api.post(f"/admin/writers/{partner.id}/accounts", json={"account_code": "CSJ042"}).json()["catalog"] == "MECH"
    assert api.post(f"/admin/writers/{partner.id}/accounts", json={"account_code": "CS0065"}).json()["catalog"] == "YT"
    codes = {a.account_code: a.writer_id for a in session.query(BeneficiaryAccount).all()}
    assert codes["JN0188"] == client.id and codes["CS0065"] == partner.id


def test_commission_code_refused_on_a_client_and_vice_versa(api, clients):
    client, partner, _ = clients
    r = api.post(f"/admin/writers/{client.id}/accounts", json={"account_code": "CS0065"})
    assert r.status_code == 422 and "commission" in r.json()["detail"]
    assert api.post(f"/admin/writers/{partner.id}/accounts", json={"account_code": "C00354"}).status_code == 422


def test_code_owned_by_someone_else_points_to_move(api, clients):
    client, _, _ = clients
    r = api.post(f"/admin/writers/{client.id}/accounts", json={"account_code": "C00122"})
    assert r.status_code == 409 and "Rata Blanca" in r.json()["detail"] and "Move" in r.json()["detail"]


def test_bad_code_refused(api, clients):
    client, _, _ = clients
    assert api.post(f"/admin/writers/{client.id}/accounts", json={"account_code": "C0"}).status_code == 422
    assert api.post(f"/admin/writers/{client.id}/accounts", json={"account_code": "C00-1"}).status_code == 422


def test_unused_code_can_be_removed_but_not_once_statements_exist(api, session, clients):
    client, _, other = clients
    added = api.post(f"/admin/writers/{client.id}/accounts", json={"account_code": "C00999"}).json()
    assert api.delete(f"/admin/writers/{client.id}/accounts/{added['id']}").status_code == 200
    used = session.query(BeneficiaryAccount).filter_by(account_code="C00122").one()
    session.add(Statement(batch_id=1, account_id=used.id, period_code="PUB26H1"))
    session.commit()
    r = api.delete(f"/admin/writers/{other.id}/accounts/{used.id}")
    assert r.status_code == 409 and "Move" in r.json()["detail"]


def test_detail_reports_statement_counts_per_code(api, session, clients):
    _, _, other = clients
    used = session.query(BeneficiaryAccount).filter_by(account_code="C00122").one()
    session.add(Statement(batch_id=1, account_id=used.id, period_code="PUB26H1"))
    session.commit()
    [acct] = api.get(f"/admin/writers/{other.id}").json()["accounts"]
    assert acct["account_code"] == "C00122" and acct["statement_count"] == 1
