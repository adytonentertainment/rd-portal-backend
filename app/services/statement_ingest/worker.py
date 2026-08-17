"""Pipeline orchestration: DB-polled ingest worker (PRD §6, §12.1).

The statement_upload row IS the job: its status column drives the stage
machine (uploaded -> sorting -> parsing -> validating -> done/failed) and
its stats JSON carries per-stage counters. A worker claims a stage simply
by advancing the status and commits as it goes — no queue, no Redis,
deliberately dumb (PRD §12.1 decision).

Fault isolation: each statement parse is independent — one corrupt file
marks that statement parse_status=failed (error recorded) and the pipeline
continues; the upload still reaches DONE. A stage-level crash marks the
upload FAILED with the error in stats. Re-running after a crash is safe:
only parse_status=pending statements are (re)parsed, and a previous
half-written attempt's lines are deleted before re-insert.
"""

import concurrent.futures
import os
import socket
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, or_, update
from sqlalchemy.orm import Session

from app.logger.logger import get_logger
from app.models.statements import (
    ParseStatus,
    Statement,
    StatementLine,
    StatementUpload,
    UploadStatus,
)
from app.services.statement_ingest.pdf_parser import parse_statement_pdf
from app.services.statement_ingest.sorter import sort_upload
from app.services.statement_ingest.storage import (
    get_storage_root,
    incoming_dir,
    resolve_stored_path,
)
from app.services.statement_ingest.xlsx_parser import parse_statement_xlsx, persist_lines

logger = get_logger("statement_worker")

TERMINAL_STATUSES = (UploadStatus.DONE, UploadStatus.FAILED)

# pdf_parser result key -> statement column
PDF_FIELD_TO_COLUMN = {
    "calculated": "calculated",
    "recouped": "recouped",
    "reserve_taken": "reserve_taken",
    "reserve_released": "reserve_released",
    "carried_forward": "carried_forward_in",
    "carried_forward_out": "carried_forward_out",
    "before_tax": "before_tax",
    "payable_this": "payable_this",
    "payable_prev": "payable_prev",
    "settlement": "settlement_paid",
    "payable": "payable",
    "cheque_amount": "cheque_amount",
}


# How long a worker's claim on an upload stays valid without being refreshed.
# A stage step (sorting/parsing thousands of files) can legitimately run for
# minutes, so this must comfortably exceed one step; anything longer means the
# worker died and the upload should be retried by someone else.
LEASE_SECONDS = int(os.getenv("INGEST_LEASE_SECONDS", "900"))

# Identifies the runner holding a lease — useful when an upload is stuck and
# you need to know which process was on it.
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


def _claim_upload(session: Session, upload_id: int) -> bool:
    """Atomically take the lease on an upload. Returns False when another live
    worker already holds it.

    The UPDATE ... WHERE is the whole point: two workers racing on the same row
    both issue it, the database serialises them, and exactly one sees rowcount
    1. Without this, both would run the parse stage and every StatementLine
    would be inserted twice.
    """
    now = datetime.now()
    cutoff = now - timedelta(seconds=LEASE_SECONDS)
    result = session.execute(
        update(StatementUpload)
        .where(
            StatementUpload.id == upload_id,
            StatementUpload.status.notin_(TERMINAL_STATUSES),
            or_(
                StatementUpload.claimed_at.is_(None),
                StatementUpload.claimed_at < cutoff,  # previous holder died
            ),
        )
        .values(claimed_at=now, claimed_by=WORKER_ID)
    )
    session.commit()
    return result.rowcount == 1


def _release_upload(session: Session, upload_id: int) -> None:
    """Drop the lease so the next stage can be picked up immediately."""
    session.execute(
        update(StatementUpload)
        .where(StatementUpload.id == upload_id, StatementUpload.claimed_by == WORKER_ID)
        .values(claimed_at=None, claimed_by=None)
    )
    session.commit()


# A batched upload whose browser was closed mid-drop stays `receiving` forever:
# the worker skips it, so it would sit invisible on disk and never be ingested.
# After this long with no new batch we mark it FAILED — visible, and its files
# can be reclaimed. Half a royalty drop silently ingested as "done" would be
# far worse, so it is never auto-finalized.
ABANDONED_UPLOAD_MINUTES = 30


def abandon_stale_uploads(session: Session, candidates) -> int:
    """Fail uploads that stopped receiving batches and were never finalized."""
    cutoff = datetime.now() - timedelta(minutes=ABANDONED_UPLOAD_MINUTES)
    failed = 0
    for upload_id, stats in candidates:
        stats = stats or {}
        if not stats.get("receiving"):
            continue
        stamp = stats.get("last_batch_at")
        try:
            last = datetime.fromisoformat(stamp) if stamp else None
        except ValueError:
            last = None
        if last is None or last > cutoff:
            continue
        upload = session.get(StatementUpload, upload_id)
        if upload is None or upload.status in TERMINAL_STATUSES:
            continue
        new_stats = dict(upload.stats or {})
        new_stats["receiving"] = False
        new_stats["error"] = (
            f"Upload abandoned: no files received for "
            f"{ABANDONED_UPLOAD_MINUTES} minutes and it was never finalized. "
            f"{upload.file_count} file(s) arrived. Re-upload to ingest them."
        )
        upload.stats = new_stats
        upload.status = UploadStatus.FAILED
        session.commit()
        logger.warning(f"Statement upload {upload_id} abandoned mid-transfer; marked failed")
        failed += 1
    return failed


def run_pending_jobs(session: Session) -> int:
    """Advance every in-flight upload by one stage step.

    Returns the number of stage steps performed (0 == nothing to do, the
    polling runner sleeps on that). Only uploads this worker can claim are
    touched, so running several workers is safe.
    """
    candidates = (
        session.query(StatementUpload.id, StatementUpload.stats)
        .filter(StatementUpload.status.notin_(TERMINAL_STATUSES))
        .order_by(StatementUpload.id)
        .all()
    )
    # Skip uploads still receiving batches — sorting a half-delivered drop would
    # ingest a partial set and mark it done.
    upload_ids = [
        upload_id
        for upload_id, stats in candidates
        if not (stats or {}).get("receiving")
    ]
    abandon_stale_uploads(session, candidates)
    steps = 0
    for upload_id in upload_ids:
        if not _claim_upload(session, upload_id):
            continue  # another worker owns this one
        try:
            if advance_upload(upload_id, session):
                steps += 1
        finally:
            _release_upload(session, upload_id)
    return steps


def run_upload_pipeline(upload_id: int, session: Session) -> StatementUpload:
    """Drive one upload through all stages synchronously (INGEST_INLINE=1)."""
    while advance_upload(upload_id, session):
        pass
    return session.get(StatementUpload, upload_id)


def advance_upload(upload_id: int, session: Session) -> bool:
    """Run one stage step for one upload. Returns False when terminal.

    A stage exception never propagates: the upload is marked FAILED with
    the error recorded in stats (the worker loop must survive anything).
    """
    upload = session.get(StatementUpload, upload_id)
    if upload is None:
        raise ValueError(f"statement_upload {upload_id} not found")
    if upload.status in TERMINAL_STATUSES:
        return False

    stage = upload.status
    try:
        if stage in (UploadStatus.UPLOADED, UploadStatus.SORTING):
            # SORTING == a previous run died mid-sort; sort_upload is
            # idempotent, so just re-run it. Sets status to PARSING.
            sort_upload(upload_id, session)
        elif stage == UploadStatus.PARSING:
            _run_parse_stage(upload_id, session)
        elif stage == UploadStatus.VALIDATING:
            _run_validate_stage(upload_id, session)
        else:  # pragma: no cover — enum is exhaustive
            return False
    except Exception as exc:
        logger.exception(f"Upload {upload_id} failed in stage {stage.value}")
        session.rollback()
        upload = session.get(StatementUpload, upload_id)
        stats = dict(upload.stats or {})
        stats["error"] = f"{stage.value}: {exc}"
        upload.stats = stats
        upload.status = UploadStatus.FAILED
        session.commit()
    return True


def _statement_ids(upload: StatementUpload) -> List[int]:
    """Statements owned by this upload (Statement has no upload_id column —
    ownership lives in the sort stats, see sorter.py)."""
    return list((upload.stats or {}).get("sort", {}).get("statement_ids", []))


def _refresh_parse_counters(upload: StatementUpload, session: Session) -> Dict:
    """Recompute stats['parse'] counters from parse_status. Flushes first so
    the just-updated statement is counted; does not commit."""
    session.flush()
    ids = _statement_ids(upload)
    counts = {status: 0 for status in ParseStatus}
    if ids:
        rows = (
            session.query(Statement.parse_status, func.count(Statement.id))
            .filter(Statement.id.in_(ids))
            .group_by(Statement.parse_status)
            .all()
        )
        for status, count in rows:
            counts[status] = count
    parse_stats = {
        "total": len(ids),
        "parsed": counts[ParseStatus.PARSED],
        "failed": counts[ParseStatus.FAILED],
        "remaining": counts[ParseStatus.PENDING],
    }
    # JSON column: reassign a new dict so SQLAlchemy sees the change
    stats = dict(upload.stats or {})
    stats["parse"] = parse_stats
    upload.stats = stats
    return parse_stats


# How many processes parse files in parallel. Parsing a statement (openpyxl +
# pdfplumber) is CPU-bound at ~0.3s/file and is the dominant ingest cost, so a
# full drop of thousands of files is spread across cores. DB writes stay serial
# in the main process (SQLite has a single writer). Default: cores-2, capped 8.
# Override with INGEST_PARSE_WORKERS (set 1 to force serial, e.g. for debugging).
_PARSE_WORKERS = min(8, max(1, int(os.getenv("INGEST_PARSE_WORKERS", str((os.cpu_count() or 2) - 2)))))
# Below this many files, the pool's spawn overhead isn't worth it — parse serially.
_PARALLEL_MIN_FILES = 8


def _parse_files(paths: Tuple[Optional[str], Optional[str]]) -> Dict:
    """Pure, DB-free parse of a statement's file halves — safe to run in a
    separate process. Returns parsed fields; raising propagates as a failed
    future. Missing halves stay None (parsing doesn't flag unpaired files)."""
    # Stored paths are relative to the storage root (older rows are absolute);
    # resolve before opening, or every parse fails with "file not found" while
    # the row itself looks perfectly correct.
    xlsx_path, pdf_path = (resolve_stored_path(p) if p else p for p in paths)
    res: Dict = {"lines": None, "detail_sum": None, "embedded_total": None,
                 "line_count": None, "pdf": None}
    if xlsx_path:
        lines, detail_sum, embedded_total, line_count = parse_statement_xlsx(xlsx_path)
        res.update(lines=lines, detail_sum=detail_sum,
                   embedded_total=embedded_total, line_count=line_count)
    if pdf_path:
        res["pdf"] = parse_statement_pdf(pdf_path)
    return res


# --- child-persist parse path -------------------------------------------------
#
# With the parsers fast (calamine + PyMuPDF) the old fan-out became the
# bottleneck itself: children parsed, then PICKLED every line back to the
# parent — hundreds of MB through a pipe for the biggest statement — and the
# parent ran every COPY and commit serially. Under child-persist each worker
# process opens its own database connection, COPYs its statement's lines
# directly, updates the statement row and commits. Only counters cross the
# process boundary, inserts run in parallel, and durability improves to
# per-statement (a killed worker loses only statements mid-flight).
#
# Gated to Postgres: SQLite does not take concurrent writers, so dev/tests keep
# the in-parent path. INGEST_CHILD_PERSIST=0 forces the old path anywhere.

_CHILD_ENGINE = None
_CHILD_SESSION = None


def _child_session(db_url: str):
    """One engine per worker process, one pooled connection, lazily built.
    The parent's engine must never be reused across fork — connections are not
    fork-safe — so the child builds its own from the URL string."""
    global _CHILD_ENGINE, _CHILD_SESSION
    if _CHILD_ENGINE is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        _CHILD_ENGINE = create_engine(db_url, pool_size=1, max_overflow=0, pool_pre_ping=True)
        _CHILD_SESSION = sessionmaker(bind=_CHILD_ENGINE, autocommit=False, autoflush=False)
    return _CHILD_SESSION()


def _parse_and_persist(statement_id: int, paths, db_url: str):
    """Runs IN a worker process: parse both halves, COPY the lines, write the
    summary fields, commit. Returns (statement_id, line_count, error|None)."""
    session = _child_session(db_url)
    try:
        statement = session.get(Statement, statement_id)
        if statement is None or statement.parse_status != ParseStatus.PENDING:
            return statement_id, 0, None  # already handled elsewhere
        try:
            res = _parse_files(paths)
        except Exception as exc:
            statement.parse_status = ParseStatus.FAILED
            statement.parse_error = str(exc)[:1000]
            session.commit()
            return statement_id, 0, str(exc)
        _persist_parsed(statement, res, session)
        session.commit()
        return statement_id, res.get("line_count") or 0, None
    except Exception as exc:
        session.rollback()
        try:
            st = session.get(Statement, statement_id)
            if st is not None and st.parse_status == ParseStatus.PENDING:
                st.parse_status = ParseStatus.FAILED
                st.parse_error = f"persist: {exc}"[:1000]
                session.commit()
        except Exception:
            session.rollback()
        return statement_id, 0, str(exc)
    finally:
        session.close()


def _persist_parsed(statement: Statement, res: Dict, session: Session) -> None:
    """Write a parse result to the DB (main process only)."""
    if res["lines"] is not None:
        # A previous attempt may have died after inserting some lines
        session.query(StatementLine).filter(
            StatementLine.statement_id == statement.id
        ).delete(synchronize_session=False)
        persist_lines(statement.id, res["lines"], session)
        statement.detail_sum = res["detail_sum"]
        statement.embedded_total = res["embedded_total"]
        statement.line_count = res["line_count"]
    if res["pdf"] is not None:
        for field, column in PDF_FIELD_TO_COLUMN.items():
            setattr(statement, column, res["pdf"][field])


def _parse_one_statement(statement: Statement, session: Session) -> None:
    """Serial parse + persist of one statement (in-process)."""
    _persist_parsed(statement, _parse_files((statement.xlsx_path, statement.pdf_path)), session)


# Commit every N statements instead of once per statement. On SQLite each
# commit is an fsync, so per-statement commits made a large drop crawl; batching
# cuts fsyncs ~50x. Per-file fault isolation is preserved via a SAVEPOINT around
# each parse, so one corrupt file rolls back only its own partial line inserts —
# not the whole batch. Crash-resumable: an uncommitted statement is still
# PENDING on restart and gets reparsed (parsing deletes prior lines first).
# How often parse progress is written back. This is a crash-recovery interval,
# not a performance knob: nothing is durable until a commit, so on a host where
# the process can be killed mid-stage (an OOM on a small instance) a large batch
# means every restart begins from zero and dies at the same point — an infinite
# loop that never records a single parsed statement. Lower it there.
PARSE_COMMIT_BATCH = max(1, int(os.getenv("INGEST_PARSE_COMMIT_BATCH", "50")))


def _run_parse_stage(upload_id: int, session: Session) -> None:
    """Parse every still-pending statement of the upload in batched commits,
    then advance to VALIDATING."""
    upload = session.get(StatementUpload, upload_id)
    ids = _statement_ids(upload)
    total = len(ids)

    # Settled counts from any prior partial run — ONE query up front, then keep
    # running counters in memory. (The old code re-ran this GROUP BY over every
    # id for every statement: O(N^2).)
    parsed = failed = 0
    pending_ids = []
    if ids:
        for status, count in (
            session.query(Statement.parse_status, func.count(Statement.id))
            .filter(Statement.id.in_(ids))
            .group_by(Statement.parse_status)
        ):
            if status == ParseStatus.PARSED:
                parsed = count
            elif status == ParseStatus.FAILED:
                failed = count
        pending_ids = [
            statement_id
            for (statement_id,) in session.query(Statement.id)
            .filter(
                Statement.id.in_(ids),
                Statement.parse_status == ParseStatus.PENDING,
            )
            .order_by(Statement.id)
        ]

    since_commit = 0

    def _write_progress():
        # Cheap in-memory progress snapshot (no DB scan), so GET on the upload
        # still shows parsed/remaining climbing between batch commits.
        stats = dict(upload.stats or {})
        stats["parse"] = {
            "total": total,
            "parsed": parsed,
            "failed": failed,
            "remaining": total - parsed - failed,
        }
        upload.stats = stats

    def _apply(statement_id, result=None, error=None):
        """Persist one statement's parse outcome; commit on batch boundary."""
        nonlocal parsed, failed, upload, since_commit
        if error is None:
            try:
                statement = session.get(Statement, statement_id)
                with session.begin_nested():  # SAVEPOINT: isolate this file's writes
                    _persist_parsed(statement, result, session)
                    statement.parse_status = ParseStatus.PARSED
                    statement.parse_error = None
                parsed += 1
            except Exception as exc:  # a DB/persist failure for this file
                error = exc
        if error is not None:
            # The savepoint already discarded this file's partial inserts;
            # record the failure in the still-open outer transaction.
            logger.error(f"Statement {statement_id} parse failed (upload {upload_id}): {error}")
            statement = session.get(Statement, statement_id)
            statement.parse_status = ParseStatus.FAILED
            statement.parse_error = str(error)
            failed += 1

        since_commit += 1
        if since_commit >= PARSE_COMMIT_BATCH:
            _write_progress()
            session.commit()
            upload = session.get(StatementUpload, upload_id)
            since_commit = 0

    use_parallel = _PARSE_WORKERS > 1 and len(pending_ids) >= _PARALLEL_MIN_FILES
    child_persist = (
        use_parallel
        and session.get_bind().dialect.name == "postgresql"
        and os.getenv("INGEST_CHILD_PERSIST", "1") != "0"
    )
    if child_persist:
        paths = {
            sid: (xp, pp)
            for sid, xp, pp in session.query(
                Statement.id, Statement.xlsx_path, Statement.pdf_path
            ).filter(Statement.id.in_(pending_ids))
        }
        db_url = session.get_bind().engine.url.render_as_string(hide_password=False)
        done = 0
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=_PARSE_WORKERS) as ex:
                futs = {
                    ex.submit(_parse_and_persist, sid, paths[sid], db_url): sid
                    for sid in pending_ids
                }
                for fut in concurrent.futures.as_completed(futs):
                    sid = futs[fut]
                    try:
                        _sid, _count, err = fut.result()
                        if err:
                            logger.warning(f"Statement {sid} failed in worker: {err[:200]}")
                    except Exception as exc:
                        # The child process died outright — record it here.
                        st = session.get(Statement, sid)
                        if st is not None and st.parse_status == ParseStatus.PENDING:
                            st.parse_status = ParseStatus.FAILED
                            st.parse_error = f"worker crashed: {exc}"[:1000]
                            session.commit()
                    done += 1
                    if done % 50 == 0:
                        # Children own the rows; the parent just refreshes the
                        # visible counters and heartbeats its lease.
                        session.expire_all()
                        upload = session.get(StatementUpload, upload_id)
                        _refresh_parse_counters(upload, session)
                        upload.claimed_at = datetime.now()
                        session.commit()
        except Exception:
            logger.exception("Child-persist pool failed; remaining statements go serial")
            session.expire_all()
            for sid in pending_ids:
                statement = session.get(Statement, sid)
                if statement is None or statement.parse_status != ParseStatus.PENDING:
                    continue
                try:
                    _apply(sid, result=_parse_files((statement.xlsx_path, statement.pdf_path)))
                except Exception as exc:
                    _apply(sid, error=exc)
        session.expire_all()
    elif use_parallel:
        # Read the file paths up front so worker processes get plain strings
        # (no ORM/session crosses the process boundary).
        paths = {
            sid: (xp, pp)
            for sid, xp, pp in session.query(
                Statement.id, Statement.xlsx_path, Statement.pdf_path
            ).filter(Statement.id.in_(pending_ids))
        }
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=_PARSE_WORKERS) as ex:
                fut_to_sid = {
                    ex.submit(_parse_files, paths[sid]): sid for sid in pending_ids
                }
                for fut in concurrent.futures.as_completed(fut_to_sid):
                    sid = fut_to_sid[fut]
                    try:
                        _apply(sid, result=fut.result())
                    except Exception as exc:  # parse raised in the worker process
                        _apply(sid, error=exc)
        except Exception:
            # Pool couldn't be used (e.g. spawn failure); fall back to serial so
            # the drop still ingests rather than failing the whole stage.
            logger.exception("Parallel parse pool failed; falling back to serial")
            for sid in pending_ids:
                statement = session.get(Statement, sid)
                if statement.parse_status != ParseStatus.PENDING:
                    continue  # already handled before the pool died
                try:
                    _apply(sid, result=_parse_files((statement.xlsx_path, statement.pdf_path)))
                except Exception as exc:
                    _apply(sid, error=exc)
    else:
        for sid in pending_ids:
            statement = session.get(Statement, sid)
            try:
                _apply(sid, result=_parse_files((statement.xlsx_path, statement.pdf_path)))
            except Exception as exc:
                _apply(sid, error=exc)

    # Final authoritative counts (one query) + advance.
    upload = session.get(StatementUpload, upload_id)
    parse_stats = _refresh_parse_counters(upload, session)
    upload.status = UploadStatus.VALIDATING
    session.commit()
    logger.info(
        f"Parsed upload {upload_id}: {parse_stats['parsed']} parsed, "
        f"{parse_stats['failed']} failed of {parse_stats['total']}"
    )


def _run_validate_stage(upload_id: int, session: Session) -> None:
    """Statement auditing is intentionally disabled: we do NOT check whether the
    statements reconcile or "make sense" — the parsed totals are enough. This
    stage just marks the upload DONE so the totals are visible and the batch is
    distributable."""
    upload = session.get(StatementUpload, upload_id)
    stats = dict(upload.stats or {})
    stats["validate"] = {"runs": 0, "blockers": 0, "warnings": 0, "infos": 0, "skipped": True}
    upload.stats = stats
    upload.status = UploadStatus.DONE
    session.commit()
    logger.info(f"Upload {upload_id} done (statement validation disabled)")
    _discard_incoming(upload_id)


def _discard_incoming(upload_id: int) -> None:
    """Drop the upload's incoming copies once its files are sorted and parsed.

    The sorter COPIES into {root}/{period}/{catalog}/, so every ingested file
    existed twice. Two drops had left 2.0 GB of exact duplicates behind on a
    disk that was 100% full. The sorted copy is the one statements point at, so
    only delete a file we can still see there.
    """
    src = incoming_dir(upload_id)
    if not os.path.isdir(src):
        return
    kept, freed = [], 0
    for name in os.listdir(src):
        path = os.path.join(src, name)
        if not os.path.isfile(path):
            continue
        size = os.path.getsize(path)
        if _sorted_copy_exists(name):
            os.remove(path)
            freed += size
        else:
            kept.append(name)
    if not kept:
        try:
            os.rmdir(src)
        except OSError:
            pass
    logger.info(
        f"Upload {upload_id}: reclaimed {freed / 1_000_000:.0f} MB of incoming "
        f"copies ({len(kept)} file(s) kept — no sorted copy found)"
    )


def _sorted_copy_exists(filename: str) -> bool:
    """True if this filename is present somewhere under the sorted tree."""
    root = get_storage_root()
    for entry in os.listdir(root):
        if entry == "incoming":
            continue
        period = os.path.join(root, entry)
        if not os.path.isdir(period):
            continue
        for catalog in os.listdir(period):
            if os.path.exists(os.path.join(period, catalog, filename)):
                return True
    return False
