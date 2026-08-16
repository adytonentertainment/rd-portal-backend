"""Uploads stream to disk, and large drops can be sent in resumable batches.

`await request.form()` materialised every part before the handler ran: a real
~5,200-file drop meant roughly a gigabyte resident plus hundreds of open
descriptors, and the copy loop then blocked the event loop for minutes. Files
are now written to their final path as bytes arrive, so peak memory is one
chunk no matter how many files are sent.
"""

import asyncio
import os
import tracemalloc

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import StatementUpload, UploadStatus
from app.routers.auth import get_user
from app.routers.statements_admin import statements_admin_router
from app.services.statement_ingest.storage import incoming_dir

ADMIN_EMAIL = "upload-admin@verax.app"


@pytest.fixture()
def admin_user(session):
    u = User(email=ADMIN_EMAIL, username="upload-admin", royalty_per_stream=0)
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def client(session, admin_user, tmp_path, monkeypatch):
    monkeypatch.setenv("STATEMENTS_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.delenv("INGEST_INLINE", raising=False)
    app = FastAPI()
    app.include_router(statements_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


def _files(n, size=2048, prefix="Ben_PUB26H1_C"):
    return [
        ("files", (f"{prefix}{i:05d}_Writer {i} (YouTube Publishing).xlsx", b"x" * size))
        for i in range(n)
    ]


def test_files_land_on_disk_with_their_names(client, session):
    r = client.post("/admin/statements/uploads", files=_files(5))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["file_count"] == 5

    stored = sorted(os.listdir(incoming_dir(body["upload_id"])))
    assert len(stored) == 5
    assert all(name.endswith(".xlsx") for name in stored)
    # content survived the streaming write intact
    first = os.path.join(incoming_dir(body["upload_id"]), stored[0])
    assert os.path.getsize(first) == 2048


class _FakeStreamRequest:
    """A multipart request delivered in chunks, without a client encoding the
    whole body in memory first (which is what TestClient does)."""

    def __init__(self, chunks, boundary):
        self._chunks = chunks
        self.headers = {"content-type": f"multipart/form-data; boundary={boundary}"}

    async def stream(self):
        for chunk in self._chunks:
            yield chunk


def _multipart_chunks(n_files, file_size, boundary="BOUND", chunk=64 * 1024):
    """Yield the wire bytes of an n-file multipart body in fixed-size chunks,
    so total payload size never exists in memory at once."""
    body = bytearray()
    for i in range(n_files):
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; '
            f'filename="Ben_PUB26H1_C{i:05d}_W{i} (YouTube Publishing).xlsx"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        body += b"x" * file_size + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return [bytes(body[i:i + chunk]) for i in range(0, len(body), chunk)], len(body)


def test_peak_memory_is_bounded_by_chunk_not_payload(tmp_path):
    """The fix itself: writing 800 files (~8 MB on the wire) must not allocate
    anything like 8 MB — memory should track the chunk size, not the drop size.
    The old path held every part open at once (~1 GB for a real 5,200-file
    upload) and could OOM the box."""
    from app.services.statement_ingest.upload_stream import stream_upload_to_dir

    chunks, total_bytes = _multipart_chunks(800, 10_000)
    dest = str(tmp_path / "incoming")

    tracemalloc.start()
    result = asyncio.run(
        stream_upload_to_dir(_FakeStreamRequest(chunks, "BOUND"), dest, max_files=20000)
    )
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert len(result["written"]) == 800
    assert len(os.listdir(dest)) == 800
    assert total_bytes > 8_000_000        # a meaningful payload
    # peak allocation must be a small multiple of the 64 KB chunk, NOT the payload
    assert peak < total_bytes / 4, (
        f"peak {peak:,} bytes vs {total_bytes:,} on the wire — still buffering"
    )


def test_batched_upload_then_finalize(client, session):
    """Large drops: open, add batches, finalize. The worker must not start
    until the last batch has landed."""
    r = client.post("/admin/statements/uploads?finalize=false", files=_files(3))
    assert r.status_code == 202
    uid = r.json()["upload_id"]
    assert r.json()["receiving"] is True

    upload = session.get(StatementUpload, uid)
    session.refresh(upload)
    assert upload.stats["receiving"] is True  # worker skips it

    r2 = client.post(f"/admin/statements/uploads/{uid}/files",
                     files=_files(4, prefix="Ben_PUB26H1_D"))
    assert r2.status_code == 202
    assert r2.json()["file_count"] == 7

    r3 = client.post(f"/admin/statements/uploads/{uid}/finalize")
    assert r3.status_code == 202
    assert r3.json()["file_count"] == 7

    session.refresh(upload)
    assert upload.stats["receiving"] is False   # now the worker may claim it
    assert len(os.listdir(incoming_dir(uid))) == 7


def test_worker_ignores_an_upload_still_receiving(client, session):
    from app.services.statement_ingest.worker import run_pending_jobs

    r = client.post("/admin/statements/uploads?finalize=false", files=_files(2))
    uid = r.json()["upload_id"]
    assert run_pending_jobs(session) == 0        # nothing advanced

    session.refresh(session.get(StatementUpload, uid))
    assert session.get(StatementUpload, uid).status == UploadStatus.UPLOADED


def test_resending_a_failed_batch_does_not_duplicate(client, session):
    """A retry after a dropped connection overwrites by name — on disk AND in
    the counters. Counting the retry twice reported 800 files for a 600-file
    drop, which is what the admin sees and what reconciliation checks."""
    r = client.post("/admin/statements/uploads?finalize=false", files=_files(3))
    uid = r.json()["upload_id"]
    r2 = client.post(f"/admin/statements/uploads/{uid}/files", files=_files(3))  # same names
    assert len(os.listdir(incoming_dir(uid))) == 3
    assert r2.json()["file_count"] == 3

    upload = session.get(StatementUpload, uid)
    session.refresh(upload)
    assert upload.file_count == 3
    assert len(upload.stats["received_files"]) == 3


def test_cannot_add_files_once_processing_started(client, session):
    r = client.post("/admin/statements/uploads", files=_files(2))
    uid = r.json()["upload_id"]
    upload = session.get(StatementUpload, uid)
    upload.status = UploadStatus.PARSING
    session.commit()

    r2 = client.post(f"/admin/statements/uploads/{uid}/files", files=_files(1))
    assert r2.status_code == 409


def test_path_traversal_in_a_filename_is_neutralised(client, session):
    r = client.post(
        "/admin/statements/uploads",
        files=[("files", ("../../etc/evil.xlsx", b"data"))],
    )
    assert r.status_code == 202
    stored = os.listdir(incoming_dir(r.json()["upload_id"]))
    assert stored == ["evil.xlsx"]


def test_empty_upload_is_rejected(client, session):
    assert client.post(
        "/admin/statements/uploads",
        files=[("other_field", ("x.xlsx", b"data"))],
    ).status_code == 400


def test_abandoned_upload_is_failed_not_silently_ingested(client, session):
    """A browser closed mid-drop leaves the upload `receiving`. It must not sit
    invisible forever, and a half-delivered drop must never be marked done."""
    from datetime import datetime, timedelta

    from app.services.statement_ingest.worker import (
        ABANDONED_UPLOAD_MINUTES,
        abandon_stale_uploads,
    )

    r = client.post("/admin/statements/uploads?finalize=false", files=_files(2))
    upload_id = r.json()["upload_id"]
    upload = session.get(StatementUpload, upload_id)

    # still in flight: leave it alone
    candidates = [(upload.id, upload.stats)]
    assert abandon_stale_uploads(session, candidates) == 0
    session.refresh(upload)
    assert upload.status == UploadStatus.UPLOADED

    stale = dict(upload.stats)
    stale["last_batch_at"] = (
        datetime.now() - timedelta(minutes=ABANDONED_UPLOAD_MINUTES + 1)
    ).isoformat()
    upload.stats = stale
    session.commit()

    assert abandon_stale_uploads(session, [(upload.id, upload.stats)]) == 1
    session.refresh(upload)
    assert upload.status == UploadStatus.FAILED
    assert "abandoned" in upload.stats["error"].lower()
