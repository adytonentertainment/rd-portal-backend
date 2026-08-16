import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import StatementUpload, UploadStatus
from app.routers.auth import get_user
from app.routers.statements_admin import statements_admin_router

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "statements")

UPLOAD_FILES = [
    "Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf",
    "Ben_PUB25H2_CSJ002_Javier Solis (Mechanical Royalties).xlsx",
    "Ben_PUB26H1_JN0249 - OMB Peezy (Mechanical Royalties).pdf",
]

ADMIN_EMAIL = "admin@verax.app"


@pytest.fixture()
def admin_user(session):
    user = User(email=ADMIN_EMAIL, username="statements-admin", royalty_per_stream=0)
    session.add(user)
    session.commit()
    return user


@pytest.fixture()
def storage_root(tmp_path, monkeypatch):
    root = tmp_path / "statements-storage"
    monkeypatch.setenv("STATEMENTS_STORAGE_ROOT", str(root))
    return root


@pytest.fixture()
def client(session, admin_user, storage_root, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", f"{ADMIN_EMAIL}, second.admin@verax.app")
    app = FastAPI()
    app.include_router(statements_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


def _multipart(filenames):
    parts = []
    for name in filenames:
        with open(os.path.join(FIXTURES_DIR, name), "rb") as f:
            parts.append(("files", (name, f.read())))
    return parts


def test_upload_stores_fixture_files(client, session, storage_root):
    response = client.post("/admin/statements/uploads", files=_multipart(UPLOAD_FILES))

    assert response.status_code == 202
    body = response.json()
    assert body["file_count"] == 3
    upload_id = body["upload_id"]

    upload = session.get(StatementUpload, upload_id)
    assert upload is not None
    assert upload.status == UploadStatus.UPLOADED
    assert upload.file_count == 3
    assert upload.stats["stored"] == 3
    assert upload.stats["skipped"] == []

    incoming = storage_root / "incoming" / str(upload_id)
    for name in UPLOAD_FILES:
        stored = incoming / name
        assert stored.exists(), f"missing stored file: {name}"
        with open(os.path.join(FIXTURES_DIR, name), "rb") as f:
            assert stored.read_bytes() == f.read()


def test_empty_upload_rejected_with_400(client):
    response = client.post("/admin/statements/uploads")
    assert response.status_code == 400


def test_non_pdf_xlsx_files_stored_but_flagged(client, session, storage_root):
    parts = _multipart(UPLOAD_FILES[:1])
    parts.append(("files", ("notes.txt", b"not a statement")))
    response = client.post("/admin/statements/uploads", files=parts)

    assert response.status_code == 202
    upload_id = response.json()["upload_id"]
    upload = session.get(StatementUpload, upload_id)
    assert upload.stats["skipped"] == ["notes.txt"]
    assert (storage_root / "incoming" / str(upload_id) / "notes.txt").exists()


def test_non_admin_email_gets_403(client, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "someone.else@verax.app")
    response = client.post("/admin/statements/uploads", files=_multipart(UPLOAD_FILES[:1]))
    assert response.status_code == 403


def test_empty_admin_emails_denies_everyone(client, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "")
    response = client.get("/admin/statements/uploads/1")
    assert response.status_code == 403


def test_get_upload_returns_status_and_stats(client):
    upload_id = client.post(
        "/admin/statements/uploads", files=_multipart(UPLOAD_FILES)
    ).json()["upload_id"]

    response = client.get(f"/admin/statements/uploads/{upload_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["upload_id"] == upload_id
    assert body["status"] == "uploaded"
    assert body["file_count"] == 3
    assert body["stats"]["skipped"] == []
    assert body["uploaded_at"] is not None


def test_get_unknown_upload_404(client):
    assert client.get("/admin/statements/uploads/99999").status_code == 404


def test_router_registered_in_api_router():
    from app.routers.router import api_router

    paths = {route.path for route in api_router.routes}
    assert "/admin/statements/uploads" in paths
    assert "/admin/statements/uploads/{upload_id}" in paths
