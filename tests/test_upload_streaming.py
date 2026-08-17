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


def test_a_disconnect_leaves_no_truncated_file(tmp_path):
    """The data-integrity guarantee: a part that never finished must not exist
    under its real filename.

    The sorter reads the DIRECTORY, not stats["received_files"], so a truncated
    file would be paired, copied into the canonical tree, given a Statement row,
    and its good original deleted — and with statement validation disabled,
    nothing downstream would ever notice the wrong number.
    """
    from app.services.statement_ingest.upload_stream import (
        UploadStreamError,
        stream_upload_to_dir,
    )

    dest = str(tmp_path / "incoming")
    body = (
        b"--BOUND\r\n"
        b'Content-Disposition: form-data; name="files"; filename="Good.xlsx"\r\n\r\n'
        + b"A" * 500
        + b"\r\n--BOUND\r\n"
        b'Content-Disposition: form-data; name="files"; filename="Truncated.xlsx"\r\n\r\n'
        + b"B" * 200  # part starts, body ends abruptly — no closing boundary
    )

    class _Dying:
        headers = {"content-type": "multipart/form-data; boundary=BOUND"}

        async def stream(self):
            yield body
            raise ConnectionError("client went away mid-part")

    with pytest.raises(UploadStreamError):
        asyncio.run(stream_upload_to_dir(_Dying(), dest, max_files=100))

    on_disk = os.listdir(dest)
    assert "Good.xlsx" in on_disk, "the completed file should survive"
    assert "Truncated.xlsx" not in on_disk, (
        "a truncated part must NOT appear under its real name — the sorter would "
        f"ingest it as a valid statement. Found: {on_disk}"
    )
    assert not [f for f in on_disk if f.endswith(".part")], (
        f"sidecar left behind: {on_disk}"
    )


def test_completed_files_are_never_left_as_sidecars(client, session):
    """The rename must actually happen — otherwise nothing is ingested at all."""
    r = client.post("/admin/statements/uploads", files=_files(4))
    uid = r.json()["upload_id"]
    on_disk = os.listdir(incoming_dir(uid))
    assert len(on_disk) == 4
    assert not [f for f in on_disk if f.startswith(".") or f.endswith(".part")]


# --- server-verified completeness -------------------------------------------
#
# The client declares what it will send; the server refuses to release the
# upload until every declared file is present at its declared size. A client
# that just lost a batch must never be able to assert that a royalty period is
# complete.

def _manifest(names_sizes):
    return {"files": [{"name": n, "size": s} for n, s in names_sizes]}


def test_manifest_create_takes_no_files_and_is_cheap(client, session):
    """POST /uploads is the only non-idempotent call; a retry of it mints a
    duplicate upload. So it carries zero bytes."""
    r = client.post(
        "/admin/statements/uploads?finalize=false",
        json=_manifest([("a.xlsx", 10), ("b.xlsx", 20)]),
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["file_count"] == 0
    assert body["receiving"] is True
    assert body["expected"] == 2


def test_finalize_refuses_an_incomplete_drop(client, session):
    """The core guarantee. 4 declared, 2 sent -> finalize must NOT release it."""
    declared = [(f"Ben_PUB26H1_C{i:05d}_W{i} (YouTube Publishing).xlsx", 2048) for i in range(4)]
    r = client.post("/admin/statements/uploads?finalize=false", json=_manifest(declared))
    uid = r.json()["upload_id"]

    client.post(f"/admin/statements/uploads/{uid}/files", files=_files(2))

    r2 = client.post(f"/admin/statements/uploads/{uid}/finalize")
    assert r2.status_code == 409, "an incomplete period must not be releasable"
    detail = r2.json()["detail"]
    assert detail["error"] == "incomplete_upload"
    assert detail["missing_count"] == 4

    upload = session.get(StatementUpload, uid)
    session.refresh(upload)
    assert upload.stats["receiving"] is True, "upload must stay closed to the worker"


def test_finalize_refuses_a_truncated_file(client, session):
    """A file that arrived the wrong size is the dangerous case: it looks valid
    on disk, and statement validation is disabled downstream."""
    name = "Ben_PUB26H1_C00000_Writer 0 (YouTube Publishing).xlsx"
    r = client.post("/admin/statements/uploads?finalize=false",
                    json=_manifest([(name, 2048)]))
    uid = r.json()["upload_id"]
    client.post(f"/admin/statements/uploads/{uid}/files", files=_files(1))

    # simulate a file that landed short of its declared size
    with open(os.path.join(incoming_dir(uid), name), "wb") as fh:
        fh.write(b"x" * 100)

    r2 = client.post(f"/admin/statements/uploads/{uid}/finalize")
    assert r2.status_code == 409
    short = r2.json()["detail"]["short"]
    assert short and short[0]["name"] == name
    assert short[0]["expected"] == 2048 and short[0]["actual"] == 100


def test_finalize_accepts_a_complete_drop(client, session):
    declared = [(f"Ben_PUB26H1_C{i:05d}_Writer {i} (YouTube Publishing).xlsx", 2048)
                for i in range(3)]
    r = client.post("/admin/statements/uploads?finalize=false", json=_manifest(declared))
    uid = r.json()["upload_id"]
    client.post(f"/admin/statements/uploads/{uid}/files", files=_files(3))

    r2 = client.post(f"/admin/statements/uploads/{uid}/finalize")
    assert r2.status_code == 202, r2.text
    upload = session.get(StatementUpload, uid)
    session.refresh(upload)
    assert upload.stats["receiving"] is False


def test_missing_endpoint_drives_resume(client, session):
    declared = [(f"Ben_PUB26H1_C{i:05d}_Writer {i} (YouTube Publishing).xlsx", 2048)
                for i in range(5)]
    r = client.post("/admin/statements/uploads?finalize=false", json=_manifest(declared))
    uid = r.json()["upload_id"]
    client.post(f"/admin/statements/uploads/{uid}/files", files=_files(2))

    m = client.get(f"/admin/statements/uploads/{uid}/missing").json()
    assert m["expected"] == 5 and m["on_disk"] == 2
    assert len(m["missing"]) == 3

    # resuming sends only what is missing, then finalize succeeds
    rest = [("files", (n, b"x" * 2048)) for n, _ in declared[2:]]
    client.post(f"/admin/statements/uploads/{uid}/files", files=rest)
    assert client.post(f"/admin/statements/uploads/{uid}/finalize").status_code == 202


def test_stuck_uploads_are_discoverable(client, session):
    """An admin who lost the upload id must still be able to find it."""
    client.post("/admin/statements/uploads?finalize=false",
                json=_manifest([("a.xlsx", 1)]))
    rows = client.get("/admin/statements/uploads", params={"receiving": True}).json()["items"]
    assert rows and rows[0]["receiving"] is True
    assert rows[0]["expected"] == 1


def test_duplicate_names_in_manifest_are_rejected(client, session):
    r = client.post("/admin/statements/uploads?finalize=false",
                    json=_manifest([("same.xlsx", 1), ("same.xlsx", 2)]))
    assert r.status_code == 400


def test_zero_files_against_a_manifest_reports_what_is_missing(client, session):
    """A drop where nothing arrived must still say WHAT is missing — the generic
    "No files uploaded" gave the caller nothing to resume from."""
    r = client.post("/admin/statements/uploads?finalize=false",
                    json=_manifest([("a.xlsx", 10), ("b.xlsx", 20)]))
    uid = r.json()["upload_id"]
    r2 = client.post(f"/admin/statements/uploads/{uid}/finalize")
    assert r2.status_code == 409
    assert r2.json()["detail"]["missing_count"] == 2


def test_upload_list_carries_pipeline_progress(client, session):
    """The activity panel renders from the LIST endpoint alone — it must carry
    compact sort/parse progress, not force a per-upload fetch of the full
    stats blob (received_files can be 5,000 names)."""
    r = client.post("/admin/statements/uploads", files=_files(2))
    uid = r.json()["upload_id"]
    upload = session.get(StatementUpload, uid)
    stats = dict(upload.stats or {})
    stats["sort"] = {"statements": 2, "batches": 1}
    stats["parse"] = {"parsed": 1, "total": 2, "failed": 0}
    upload.stats = stats
    session.commit()

    rows = client.get("/admin/statements/uploads").json()["items"]
    row = next(x for x in rows if x["upload_id"] == uid)
    assert row["progress"] == {
        "sorted": 2, "batches": 1, "parsed": 1, "parse_total": 2, "parse_failed": 0,
    }
    assert "received_files" not in str(row), "full stats blob must not leak into the list"
