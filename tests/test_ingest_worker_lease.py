"""The ingest worker must actually run, and must never run twice on one upload.

Two production failures are covered here:
  1. No runner at all -> uploads sit at UPLOADED forever, silently. The API now
     starts an in-process worker, so the pipeline cannot simply be absent.
  2. Two runners -> both advance the same upload and every StatementLine is
     inserted twice, doubling the earnings a writer sees. Uploads are leased.
"""

from datetime import datetime, timedelta

from app.models.statements import StatementUpload, UploadStatus
from app.services.statement_ingest import worker as W
from app.services.statement_ingest.runner import worker_status


def _upload(session, status=UploadStatus.UPLOADED):
    up = StatementUpload(file_count=1, status=status)
    session.add(up)
    session.commit()
    return up


def test_only_one_worker_can_claim_an_upload(session):
    up = _upload(session)
    assert W._claim_upload(session, up.id) is True
    # a second runner (different WORKER_ID) must be refused
    original = W.WORKER_ID
    W.WORKER_ID = "other-host:999"
    try:
        assert W._claim_upload(session, up.id) is False
    finally:
        W.WORKER_ID = original


def test_releasing_lets_the_next_stage_be_claimed(session):
    up = _upload(session)
    assert W._claim_upload(session, up.id) is True
    W._release_upload(session, up.id)
    session.expire_all()
    assert session.get(StatementUpload, up.id).claimed_at is None
    assert W._claim_upload(session, up.id) is True


def test_a_crashed_workers_lease_expires_and_is_retried(session):
    """A worker that dies mid-stage must not wedge the upload forever."""
    up = _upload(session)
    up.claimed_at = datetime.now() - timedelta(seconds=W.LEASE_SECONDS + 60)
    up.claimed_by = "dead-worker:1"
    session.commit()

    assert W._claim_upload(session, up.id) is True
    session.expire_all()
    assert session.get(StatementUpload, up.id).claimed_by == W.WORKER_ID


def test_a_live_lease_is_not_stolen(session):
    up = _upload(session)
    up.claimed_at = datetime.now()          # fresh — worker still alive
    up.claimed_by = "busy-worker:2"
    session.commit()
    assert W._claim_upload(session, up.id) is False


def test_terminal_uploads_are_never_claimed(session):
    for status in (UploadStatus.DONE, UploadStatus.FAILED):
        up = _upload(session, status=status)
        assert W._claim_upload(session, up.id) is False


def test_run_pending_jobs_skips_uploads_owned_by_another_worker(session):
    """The guard that stops two runners double-parsing the same upload."""
    up = _upload(session)
    up.claimed_at = datetime.now()
    up.claimed_by = "someone-else:3"
    session.commit()

    assert W.run_pending_jobs(session) == 0        # nothing advanced
    session.expire_all()
    assert session.get(StatementUpload, up.id).status == UploadStatus.UPLOADED


def test_worker_status_reports_whether_anything_will_run(monkeypatch):
    """Ops must be able to see that ingest is actually wired up."""
    monkeypatch.delenv("INGEST_WORKER_IN_PROCESS", raising=False)
    assert worker_status()["in_process_enabled"] is True
    monkeypatch.setenv("INGEST_WORKER_IN_PROCESS", "0")
    assert worker_status()["in_process_enabled"] is False


def test_two_workers_do_not_double_insert_statement_lines(session, tmp_path, monkeypatch):
    """The money bug: without the lease, both runners execute the parse stage
    and every line is inserted twice, doubling what the writer sees."""
    import os, shutil
    from app.models.statements import Statement, StatementLine
    from app.services.statement_ingest.storage import incoming_dir

    monkeypatch.setenv("STATEMENTS_STORAGE_ROOT", str(tmp_path / "storage"))
    fixtures = os.path.join(os.path.dirname(__file__), "fixtures", "statements")
    pair = [f for f in sorted(os.listdir(fixtures)) if "C00650" in f]
    assert len(pair) == 2

    up = _upload(session)
    dest = incoming_dir(up.id)
    os.makedirs(dest, exist_ok=True)
    for name in pair:
        shutil.copy2(os.path.join(fixtures, name), os.path.join(dest, name))

    # drive the pipeline to completion the way the poller does
    for _ in range(10):
        if W.run_pending_jobs(session) == 0:
            break
    session.expire_all()
    stmt = session.query(Statement).one()
    lines_once = session.query(StatementLine).filter(
        StatementLine.statement_id == stmt.id).count()
    assert lines_once > 0
    total_once = stmt.detail_sum

    # a second worker polls the same finished upload — must change nothing
    original = W.WORKER_ID
    W.WORKER_ID = "second-worker:2"
    try:
        for _ in range(3):
            W.run_pending_jobs(session)
    finally:
        W.WORKER_ID = original

    session.expire_all()
    assert session.query(StatementLine).filter(
        StatementLine.statement_id == stmt.id).count() == lines_once
    assert session.query(Statement).one().detail_sum == total_once
