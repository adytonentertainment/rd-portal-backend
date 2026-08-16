"""US-007: DB-polled pipeline worker (sort -> parse -> validate -> done)."""

import json
import os
import shutil
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    ParseStatus,
    Statement,
    StatementLine,
    StatementUpload,
    UploadStatus,
)
from app.routers.auth import get_user
from app.routers.statements_admin import statements_admin_router
from app.services.statement_ingest import worker
from app.services.statement_ingest.storage import incoming_dir
from app.services.statement_ingest.worker import (
    advance_upload,
    run_pending_jobs,
    run_upload_pipeline,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "statements")

ALL_FIXTURE_FILES = sorted(
    name
    for name in os.listdir(FIXTURES_DIR)
    if os.path.splitext(name)[1] in (".pdf", ".xlsx")
)

CSJ002_PAIR = [
    "Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf",
    "Ben_PUB25H2_CSJ002_Javier Solis (Mechanical Royalties).xlsx",
]
JN0249_PAIR = [
    "Ben_PUB26H1_JN0249 - OMB Peezy (Mechanical Royalties).pdf",
    "Ben_PUB26H1_JN0249_OMB Peezy (Mechanical Royalties).xlsx",
]

ADMIN_EMAIL = "admin@verax.app"

with open(os.path.join(FIXTURES_DIR, "expected_values.json")) as f:
    EXPECTED = json.load(f)


@pytest.fixture()
def storage_root(tmp_path, monkeypatch):
    root = tmp_path / "statements-storage"
    monkeypatch.setenv("STATEMENTS_STORAGE_ROOT", str(root))
    return root


def make_upload(session, filenames):
    """Create an upload row + incoming files, as the POST endpoint would."""
    upload = StatementUpload(
        file_count=len(filenames),
        status=UploadStatus.UPLOADED,
        stats={"received": len(filenames), "stored": len(filenames), "skipped": []},
    )
    session.add(upload)
    session.commit()
    dest = incoming_dir(upload.id)
    os.makedirs(dest)
    for name in filenames:
        shutil.copy2(os.path.join(FIXTURES_DIR, name), os.path.join(dest, name))
    return upload


def drive(session, max_steps=50):
    """Poll like the runner script until the worker finds nothing to do."""
    for _ in range(max_steps):
        if run_pending_jobs(session) == 0:
            return
    raise AssertionError("pipeline did not reach a terminal state")


def expected_for(statement):
    return EXPECTED[f"{statement.period_code}_{statement.account.account_code}"]


def as_decimal(value):
    return None if value is None else Decimal(str(value))


# ---------------------------------------------------------------- worker core


def test_run_pending_jobs_with_nothing_to_do(session):
    assert run_pending_jobs(session) == 0


def test_advance_unknown_upload_raises(session):
    with pytest.raises(ValueError):
        advance_upload(99999, session)


def test_worker_drives_upload_through_all_stages(session, storage_root):
    upload = make_upload(session, CSJ002_PAIR)

    # one stage step per poll: sort, parse, validate — then nothing to do
    assert run_pending_jobs(session) == 1  # UPLOADED -> (sort) -> PARSING
    assert session.get(StatementUpload, upload.id).status == UploadStatus.PARSING
    assert run_pending_jobs(session) == 1  # PARSING -> VALIDATING
    assert run_pending_jobs(session) == 1  # VALIDATING -> DONE
    assert run_pending_jobs(session) == 0

    upload = session.get(StatementUpload, upload.id)
    assert upload.status == UploadStatus.DONE
    assert upload.stats["parse"] == {
        "total": 1,
        "parsed": 1,
        "failed": 0,
        "remaining": 0,
    }

    statement = session.query(Statement).one()
    expected = expected_for(statement)
    assert statement.parse_status == ParseStatus.PARSED
    assert statement.parse_error is None
    assert statement.line_count == expected["xlsx_line_count"]
    assert abs(statement.detail_sum - as_decimal(expected["xlsx_detail_sum"])) <= Decimal(
        "0.0001"
    )
    assert statement.calculated == as_decimal(expected["calculated"])
    assert statement.payable == as_decimal(expected["payable"])


def test_pipeline_idempotent_after_done(session, storage_root):
    upload = make_upload(session, CSJ002_PAIR)
    drive(session)

    statements_before = session.query(Statement.id).count()
    lines_before = session.query(StatementLine.id).count()
    stats_before = session.get(StatementUpload, upload.id).stats

    assert run_pending_jobs(session) == 0
    assert session.query(Statement.id).count() == statements_before
    assert session.query(StatementLine.id).count() == lines_before
    assert session.get(StatementUpload, upload.id).stats == stats_before


def test_resume_skips_already_parsed_statements(session, storage_root):
    upload = make_upload(session, CSJ002_PAIR)
    drive(session)

    # crash simulation: status rewound to PARSING, statement already PARSED
    line_ids_before = sorted(i for (i,) in session.query(StatementLine.id))
    upload = session.get(StatementUpload, upload.id)
    upload.status = UploadStatus.PARSING
    session.commit()

    drive(session)

    upload = session.get(StatementUpload, upload.id)
    assert upload.status == UploadStatus.DONE
    # untouched line rows == the parsed statement was skipped, not re-parsed
    assert sorted(i for (i,) in session.query(StatementLine.id)) == line_ids_before
    assert upload.stats["parse"]["parsed"] == 1


def test_corrupt_file_fails_one_statement_not_the_upload(session, storage_root):
    upload = make_upload(session, CSJ002_PAIR + JN0249_PAIR)
    corrupt = os.path.join(incoming_dir(upload.id), JN0249_PAIR[1])
    with open(corrupt, "wb") as f:
        f.write(b"this is not a spreadsheet")

    drive(session)

    upload = session.get(StatementUpload, upload.id)
    assert upload.status == UploadStatus.DONE  # never FAILED by one bad file
    assert upload.stats["parse"] == {
        "total": 2,
        "parsed": 1,
        "failed": 1,
        "remaining": 0,
    }

    by_code = {
        s.account.account_code: s for s in session.query(Statement).all()
    }
    assert by_code["CSJ002"].parse_status == ParseStatus.PARSED
    failed = by_code["JN0249"]
    assert failed.parse_status == ParseStatus.FAILED
    assert failed.parse_error
    assert failed.detail_sum is None
    assert (
        session.query(StatementLine)
        .filter(StatementLine.statement_id == failed.id)
        .count()
        == 0
    )


def test_stage_crash_marks_upload_failed(session, storage_root, monkeypatch):
    upload = make_upload(session, CSJ002_PAIR)

    def boom(upload_id, session):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(worker, "sort_upload", boom)
    drive(session)

    upload = session.get(StatementUpload, upload.id)
    assert upload.status == UploadStatus.FAILED
    assert "uploaded" in upload.stats["error"]
    assert "disk on fire" in upload.stats["error"]
    # terminal: the worker won't pick it up again
    assert run_pending_jobs(session) == 0


# --------------------------------------------- full pipeline via INGEST_INLINE


@pytest.fixture()
def client(session, storage_root, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("INGEST_INLINE", "1")
    admin = User(email=ADMIN_EMAIL, username="statements-admin", royalty_per_stream=0)
    session.add(admin)
    session.commit()
    app = FastAPI()
    app.include_router(statements_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin
    return TestClient(app)


def test_full_pipeline_inline_on_all_fixtures(client, session):
    parts = []
    for name in ALL_FIXTURE_FILES:
        with open(os.path.join(FIXTURES_DIR, name), "rb") as f:
            parts.append(("files", (name, f.read())))

    response = client.post("/admin/statements/uploads", files=parts)
    assert response.status_code == 202
    body = response.json()
    assert body["file_count"] == 14
    assert body["status"] == "done"
    upload_id = body["upload_id"]

    # live stage + counters on the status endpoint
    status = client.get(f"/admin/statements/uploads/{upload_id}").json()
    assert status["stage"] == "done"
    assert status["stats"]["sort"]["batches"] == 5
    assert status["stats"]["sort"]["paired"] == 7
    assert status["stats"]["parse"] == {
        "total": 7,
        "parsed": 7,
        "failed": 0,
        "remaining": 0,
    }

    statements = session.query(Statement).all()
    assert len(statements) == 7
    for statement in statements:
        expected = expected_for(statement)
        assert statement.parse_status == ParseStatus.PARSED
        assert statement.detail_sum is not None
        assert abs(
            statement.detail_sum - as_decimal(expected["xlsx_detail_sum"])
        ) <= Decimal("0.0001")
        assert statement.line_count == expected["xlsx_line_count"]
        # official PDF figures persisted; None == absent, never fabricated 0
        assert statement.calculated == as_decimal(expected["calculated"])
        assert statement.recouped == as_decimal(expected["recouped"])
        assert statement.payable == as_decimal(expected["payable"])
        assert statement.payable_prev == as_decimal(expected["payable_prev"])
        assert statement.settlement_paid == as_decimal(expected["settlement"])
        assert statement.carried_forward_in == as_decimal(expected["carried_forward"])
        assert statement.before_tax == as_decimal(expected["before_tax"])

    by_code = {s.account.account_code: s for s in statements}
    # below-threshold money leaving the account (US-009 V-STMT-3 needs this)
    assert by_code["C00739-New"].carried_forward_out == Decimal("27.87")
    assert by_code["CSJ002"].carried_forward_out is None

    # second inline run of the worker changes nothing (idempotency)
    lines_before = session.query(StatementLine.id).count()
    assert run_pending_jobs(session) == 0
    assert session.query(Statement.id).count() == 7
    assert session.query(StatementLine.id).count() == lines_before


def test_fixture_files_unmodified_by_pipeline(client, session):
    """Statement files are immutable records — the pipeline never writes them."""
    name = CSJ002_PAIR[1]
    with open(os.path.join(FIXTURES_DIR, name), "rb") as f:
        before = f.read()
    with open(os.path.join(FIXTURES_DIR, name), "rb") as f:
        client.post("/admin/statements/uploads", files=[("files", (name, f.read()))])
    with open(os.path.join(FIXTURES_DIR, name), "rb") as f:
        assert f.read() == before
