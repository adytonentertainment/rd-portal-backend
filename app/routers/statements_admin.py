import os
import shutil
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.session import get_session
from app.logger.logger import get_logger
from app.models.models import User
from app.models.statements import (
    BatchStatus,
    BeneficiaryAccount,
    Distribution,
    FindingSeverity,
    FindingStatus,
    ParseStatus,
    Statement,
    StatementBatch,
    StatementLine,
    StatementUpload,
    UploadStatus,
    ValidationFinding,
    ValidationRun,
    Writer,
    WriterStatus,
)
from app.routers.auth import get_user
from app.utils.roles import is_effective_admin
from app.schemas.statements import (
    BatchDetail,
    BatchSummary,
    FindingCounts,
    FindingOut,
    StatementDetail,
    StatementKeyFigures,
    StatementLinesPage,
    StatementLineOut,
    ValidationRunSummary,
    WaiveRequest,
)
from app.services.statement_ingest.storage import incoming_dir
from app.services.statement_ingest.upload_stream import (
    UploadStreamError,
    stream_upload_to_dir,
)
from app.services.statement_ingest.worker import run_upload_pipeline
from app.services.validation.engine import run_validation

logger = get_logger("statements_admin")

statements_admin_router = APIRouter(
    prefix="/admin/statements",
    tags=["Statements Admin"],
)

# Waive/acknowledge are finding-id addressed (PRD §9), not batch-nested
findings_admin_router = APIRouter(
    prefix="/admin/findings",
    tags=["Statements Admin"],
)

# Distribution is publish-oriented and addressed by distribution id
distributions_admin_router = APIRouter(
    prefix="/admin/distributions",
    tags=["Distribution Admin"],
)

ALLOWED_EXTENSIONS = {".pdf", ".xlsx"}
# A real semiannual drop is ~2,600 files (PDF+XLSX pairs). Starlette's multipart
# parser defaults to max_files=1000, which rejects a full batch, so parse the
# upload form with a raised ceiling.
MAX_UPLOAD_FILES = 20000


def require_admin(user: User = Depends(get_user)) -> User:
    """Admin gate: the user must be an *effective* admin — an approved admin
    account (role='admin' AND admin_approved), or a bootstrap ADMIN_EMAILS
    address. A pending (unapproved) admin is rejected with 403."""
    if not is_effective_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _record_files(upload: StatementUpload, written, ignored) -> dict:
    """Merge a batch of received filenames into the upload's stats."""
    stats = dict(upload.stats or {})
    received = list(stats.get("received_files") or [])
    # A retried batch re-sends names already recorded — they overwrote the same
    # files on disk, so counting them twice would report 800 files for a 600-file
    # drop. Keep first-seen order, drop repeats.
    seen = set(received)
    for name in written:
        if name not in seen:
            seen.add(name)
            received.append(name)
    skipped = [
        name for name in received
        if os.path.splitext(name)[1].lower() not in ALLOWED_EXTENSIONS
    ]
    stats["received_files"] = received
    stats["received"] = len(received)
    # Stamped on every batch so the worker can tell a transfer still in flight
    # from one whose browser was closed mid-drop (see abandon_stale_uploads).
    stats["last_batch_at"] = datetime.now().isoformat()
    stats["stored"] = len(received)
    stats["skipped"] = skipped
    if ignored:
        stats["ignored_fields"] = list(stats.get("ignored_fields") or []) + list(ignored)
    upload.stats = stats
    upload.file_count = len(received)
    return stats


def _assert_accepting(upload: StatementUpload) -> None:
    """Files may only be added while the upload is still being received."""
    if upload.status != UploadStatus.UPLOADED:
        raise HTTPException(
            status_code=409,
            detail=f"Upload {upload.id} is already {upload.status.value}; "
            "start a new upload instead.",
        )


@statements_admin_router.post("/uploads", status_code=202)
async def create_statement_upload(
    request: Request,
    finalize: bool = True,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Dump loose statement files (PDF+XLSX, any mix, unsorted) in one upload.

    The body is STREAMED to {storage_root}/incoming/{upload_id}/ — each part is
    written to its final path as its bytes arrive. `await request.form()` would
    instead materialise all ~5,200 parts first (~1 GB resident, hundreds of open
    descriptors) and then block the event loop copying them, freezing the API
    for every other user and risking an OOM kill.

    Large drops can be sent in batches: POST here with `finalize=false`, add
    more with POST /uploads/{id}/files, then POST /uploads/{id}/finalize. A
    batch that fails can simply be re-sent — only the files it carried are
    missing — so a dropped connection no longer means starting over.
    """
    upload = StatementUpload(
        uploaded_by=user.id, file_count=0, status=UploadStatus.UPLOADED,
        stats={"receiving": True},
    )
    db.add(upload)
    db.flush()  # assigns upload.id for the storage path

    try:
        result = await stream_upload_to_dir(
            request, incoming_dir(upload.id), max_files=MAX_UPLOAD_FILES
        )
    except UploadStreamError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Could not read upload: {exc}")

    if not result["written"] and finalize:
        db.rollback()
        raise HTTPException(status_code=400, detail="No files uploaded")

    stats = _record_files(upload, result["written"], result.get("ignored_fields"))
    # `receiving` keeps the ingest worker from starting while more batches are
    # still on the way — it would otherwise sort a half-delivered drop.
    stats["receiving"] = not finalize
    upload.stats = dict(stats)
    db.commit()

    logger.info(
        f"Statement upload {upload.id}: {len(result['written'])} files streamed "
        f"({result['bytes'] / 1_000_000:.1f} MB), finalize={finalize}"
    )

    if finalize and os.getenv("INGEST_INLINE") == "1":
        upload = run_upload_pipeline(upload.id, db)

    return {
        "upload_id": upload.id,
        "file_count": upload.file_count,
        "status": upload.status.value,
        "receiving": bool(upload.stats.get("receiving")),
    }


@statements_admin_router.post("/uploads/{upload_id}/files", status_code=202)
async def add_statement_upload_files(
    upload_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Add another batch of files to an upload that is still being received.

    Re-sending a batch is safe: files are written by name, so a retry after a
    dropped connection overwrites the partial copies rather than duplicating
    them.
    """
    upload = db.get(StatementUpload, upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    _assert_accepting(upload)

    try:
        result = await stream_upload_to_dir(
            request, incoming_dir(upload.id), max_files=MAX_UPLOAD_FILES
        )
    except UploadStreamError as exc:
        raise HTTPException(status_code=400, detail=f"Could not read upload: {exc}")

    stats = _record_files(upload, result["written"], result.get("ignored_fields"))
    stats["receiving"] = True
    upload.stats = dict(stats)
    db.commit()
    return {
        "upload_id": upload.id,
        "added": len(result["written"]),
        "file_count": upload.file_count,
        "status": upload.status.value,
    }


@statements_admin_router.post("/uploads/{upload_id}/finalize", status_code=202)
async def finalize_statement_upload(
    upload_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Close an upload to further files and release it to the ingest worker."""
    upload = db.get(StatementUpload, upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    _assert_accepting(upload)
    if not upload.file_count:
        raise HTTPException(status_code=400, detail="No files uploaded")

    stats = dict(upload.stats or {})
    stats["receiving"] = False
    upload.stats = stats
    db.commit()
    logger.info(f"Statement upload {upload.id} finalized: {upload.file_count} files")

    if os.getenv("INGEST_INLINE") == "1":
        upload = run_upload_pipeline(upload.id, db)

    return {
        "upload_id": upload.id,
        "file_count": upload.file_count,
        "status": upload.status.value,
    }


@statements_admin_router.post("/batches/{batch_id}/revalidate")
async def revalidate_batch(
    batch_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Re-run the validation rules engine on a batch. Findings keep their
    stable identity across runs: waived ones stay waived, fixed ones close."""
    batch = db.get(StatementBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    run = run_validation(batch_id, db)
    return {
        "run_id": run.id,
        "batch_id": batch_id,
        "batch_status": batch.status.value,
        "rules_version": run.rules_version,
        "blockers": run.blockers,
        "warnings": run.warnings,
        "infos": run.infos,
    }


@statements_admin_router.get("/uploads/{upload_id}")
async def get_statement_upload(
    upload_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    upload = (
        db.query(StatementUpload).filter(StatementUpload.id == upload_id).first()
    )
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    return {
        "upload_id": upload.id,
        "status": upload.status.value,
        # pipeline stage + per-stage counters (stats.sort / stats.parse),
        # refreshed by the worker after every statement — poll this for
        # live progress
        "stage": upload.status.value,
        "file_count": upload.file_count,
        "uploaded_at": upload.uploaded_at.isoformat() if upload.uploaded_at else None,
        "stats": upload.stats,
    }


@statements_admin_router.get("/uploads/{upload_id}/statements")
async def get_upload_statements(
    upload_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Real parsed figures for every statement an upload produced — so the
    upload UI can show the true amount (Σ line earnings), not an estimate.

    Ownership is per-upload via `stats.sort.statement_ids` (the same source the
    worker uses): a Statement has no upload_id column, and batches are REUSED
    across uploads (keyed by period+catalog), so `batch.upload_id` points at
    whichever upload first created the batch — not necessarily this one. Fall
    back to batch.upload_id only when the sort stats aren't populated yet."""
    upload = db.get(StatementUpload, upload_id)
    if upload is None:
        raise HTTPException(status_code=404, detail="Upload not found")

    q = (
        db.query(Statement, BeneficiaryAccount.account_code, Writer)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
    )
    statement_ids = (upload.stats or {}).get("sort", {}).get("statement_ids")
    if statement_ids:
        q = q.filter(Statement.id.in_(statement_ids))
    else:
        q = q.join(StatementBatch, Statement.batch_id == StatementBatch.id).filter(
            StatementBatch.upload_id == upload_id
        )
    rows = q.order_by(BeneficiaryAccount.account_code).all()

    RECON_TOLERANCE = Decimal("0.01")

    def _d(v):
        return str(v) if v is not None else None

    out = []
    completeness = {
        "total": 0, "paired": 0, "missing_pdf": 0, "missing_xlsx": 0,
        "reconciled": 0, "unreconciled": 0, "unparsed": 0,
        # placeholder writers this upload created that haven't been matched to a
        # real client-list identity yet (kind IS NULL) — these are what the
        # client-import resolution queue exists to resolve, and what the upload
        # modal's post-ingest banner points the admin at.
        "unresolved_writers": 0,
    }
    batch_ids = set()
    unresolved_writer_ids = set()
    for stmt, account_code, writer in rows:
        pdf_present = bool(stmt.pdf_path)
        xlsx_present = bool(stmt.xlsx_path)
        # Reconciliation invariant: Σ(line earnings) == PDF "Royalties calculated".
        # Only meaningful when both sides exist.
        recon_delta = None
        reconciled = None
        if stmt.detail_sum is not None and stmt.calculated is not None:
            recon_delta = stmt.detail_sum - stmt.calculated
            reconciled = abs(recon_delta) <= RECON_TOLERANCE

        completeness["total"] += 1
        if pdf_present and xlsx_present:
            completeness["paired"] += 1
        if not pdf_present:
            completeness["missing_pdf"] += 1
        if not xlsx_present:
            completeness["missing_xlsx"] += 1
        if stmt.parse_status != ParseStatus.PARSED:
            completeness["unparsed"] += 1
        if reconciled is True:
            completeness["reconciled"] += 1
        elif reconciled is False:
            completeness["unreconciled"] += 1
        if stmt.batch_id is not None:
            batch_ids.add(stmt.batch_id)
        if writer is not None and writer.kind is None:
            unresolved_writer_ids.add(writer.id)

        out.append({
            "statement_id": stmt.id,
            "account_code": account_code,
            "writer_name": writer.canonical_name if writer else None,
            "period_code": stmt.period_code,
            "catalog": stmt.batch.catalog.value if stmt.batch else None,
            "parse_status": stmt.parse_status.value,
            # completeness signals
            "pdf_present": pdf_present,
            "xlsx_present": xlsx_present,
            "paired": pdf_present and xlsx_present,
            "reconciled": reconciled,
            "recon_delta": _d(recon_delta),
            # detail_sum is Σ(line earnings) — the reconciled real amount;
            # fall back to payable if a profile lacks per-line detail
            "amount": _d(stmt.detail_sum if stmt.detail_sum is not None else stmt.payable),
            "detail_sum": _d(stmt.detail_sum),
            "line_count": stmt.line_count,
            # account summary — the PDF payment-of-record ledger (§2.5)
            "account_summary": {
                "calculated": _d(stmt.calculated),
                "recouped": _d(stmt.recouped),
                "reserve_taken": _d(stmt.reserve_taken),
                "reserve_released": _d(stmt.reserve_released),
                "carried_forward_in": _d(stmt.carried_forward_in),
                "carried_forward_out": _d(stmt.carried_forward_out),
                "payable_prev": _d(stmt.payable_prev),
                "payable_this": _d(stmt.payable_this),
                "settlement_paid": _d(stmt.settlement_paid),
                "before_tax": _d(stmt.before_tax),
                "payable": _d(stmt.payable),
                "cheque_amount": _d(stmt.cheque_amount),
            },
        })

    completeness["unresolved_writers"] = len(unresolved_writer_ids)
    # Sort-stage outcome, so the UI can explain a "0 new statements" upload:
    # files already ingested show up here as duplicates, not as new statements.
    sort_stats = (upload.stats or {}).get("sort", {})
    sort_summary = {
        "statements": sort_stats.get("statements", len(out)),
        "duplicates": len(sort_stats.get("duplicates", []) or []),
        "unpaired": len(sort_stats.get("unpaired", []) or []),
        "unparseable": len(sort_stats.get("unparseable", []) or []),
    }
    return {
        "upload_id": upload_id,
        "status": upload.status.value,
        "completeness": completeness,
        "sort": sort_summary,
        # distinct batches these statements landed in — the modal links the
        # post-ingest "needs matching" banner to the batch gate view.
        "batch_ids": sorted(batch_ids),
        "statements": out,
    }


# --- Read API (PRD §9 Phase-1 subset) ---------------------------------------
# Route order matters: every literal path (/batches...) must be registered
# before the GET /{statement_id} catch-all below.


def _parse_enum(enum_cls, value: str, param: str):
    """Map a ?param=value query string to its enum, 422 on garbage."""
    try:
        return enum_cls(value)
    except ValueError:
        valid = ", ".join(e.value for e in enum_cls)
        raise HTTPException(
            status_code=422, detail=f"Invalid {param} '{value}'; expected one of: {valid}"
        )


def _value(enum_member) -> Optional[str]:
    return enum_member.value if enum_member is not None else None


def _finding_counts(db: Session, batch_id: int, scope_ref: Optional[str] = None) -> FindingCounts:
    """Open findings by severity for a batch (optionally one scope_ref).
    Findings have one row per identity for life, re-attached to the latest
    run, so joining over all of the batch's runs sees each finding once."""
    q = (
        db.query(ValidationFinding.severity, func.count(ValidationFinding.id))
        .join(ValidationRun, ValidationFinding.run_id == ValidationRun.id)
        .filter(
            ValidationRun.batch_id == batch_id,
            ValidationFinding.status == FindingStatus.OPEN,
        )
    )
    if scope_ref is not None:
        q = q.filter(ValidationFinding.scope_ref == scope_ref)
    counts = {severity: count for severity, count in q.group_by(ValidationFinding.severity)}
    return FindingCounts(
        blocker=counts.get(FindingSeverity.BLOCKER, 0),
        warning=counts.get(FindingSeverity.WARNING, 0),
        info=counts.get(FindingSeverity.INFO, 0),
    )


def _batch_summary(batch: StatementBatch, statement_count: int) -> dict:
    return {
        "id": batch.id,
        "label": batch.label,
        "period_code": batch.period_code,
        "catalog": batch.catalog.value,
        "cadence": _value(batch.cadence),
        "status": batch.status.value,
        "uploaded_at": batch.uploaded_at,
        "statement_count": statement_count,
        "stats": batch.stats,
    }


def _finding_out(f: ValidationFinding) -> FindingOut:
    return FindingOut(
        id=f.id,
        run_id=f.run_id,
        rule_id=f.rule_id,
        severity=f.severity.value,
        scope=f.scope.value,
        scope_ref=f.scope_ref,
        message=f.message,
        details=f.details,
        status=f.status.value,
        waived_by=f.waived_by,
        waived_reason=f.waived_reason,
        waived_at=f.waived_at,
        acknowledged_by=f.acknowledged_by,
        acknowledged_at=f.acknowledged_at,
    )


def _get_batch_or_404(db: Session, batch_id: int) -> StatementBatch:
    batch = db.get(StatementBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Batch not found")
    return batch


@statements_admin_router.get("/batches", response_model=List[BatchSummary])
async def list_batches(
    period: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """List batches (auto-derived from uploads), filterable by period/status."""
    q = db.query(StatementBatch)
    if period:
        q = q.filter(StatementBatch.period_code == period)
    if status:
        q = q.filter(StatementBatch.status == _parse_enum(BatchStatus, status, "status"))
    batches = q.order_by(StatementBatch.period_code, StatementBatch.id).all()

    counts = dict(
        db.query(Statement.batch_id, func.count(Statement.id))
        .filter(Statement.batch_id.in_([b.id for b in batches] or [0]))
        .group_by(Statement.batch_id)
        .all()
    )
    return [BatchSummary(**_batch_summary(b, counts.get(b.id, 0))) for b in batches]


@statements_admin_router.get("/batches/{batch_id}", response_model=BatchDetail)
async def get_batch(
    batch_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Batch detail: stats plus open finding counts by severity + last run."""
    batch = _get_batch_or_404(db, batch_id)
    statement_count = (
        db.query(func.count(Statement.id)).filter(Statement.batch_id == batch.id).scalar()
    )
    last_run = (
        db.query(ValidationRun)
        .filter(ValidationRun.batch_id == batch.id)
        .order_by(ValidationRun.id.desc())
        .first()
    )
    return BatchDetail(
        **_batch_summary(batch, statement_count),
        finding_counts=_finding_counts(db, batch.id),
        last_run=ValidationRunSummary(
            id=last_run.id,
            started_at=last_run.started_at,
            finished_at=last_run.finished_at,
            rules_version=last_run.rules_version,
            blockers=last_run.blockers,
            warnings=last_run.warnings,
            infos=last_run.infos,
        )
        if last_run
        else None,
    )


@statements_admin_router.get("/batches/{batch_id}/findings", response_model=List[FindingOut])
async def list_batch_findings(
    batch_id: int,
    severity: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    batch = _get_batch_or_404(db, batch_id)
    q = (
        db.query(ValidationFinding)
        .join(ValidationRun, ValidationFinding.run_id == ValidationRun.id)
        .filter(ValidationRun.batch_id == batch.id)
    )
    if severity:
        q = q.filter(
            ValidationFinding.severity == _parse_enum(FindingSeverity, severity, "severity")
        )
    if status:
        q = q.filter(ValidationFinding.status == _parse_enum(FindingStatus, status, "status"))
    return [_finding_out(f) for f in q.order_by(ValidationFinding.id).all()]


@statements_admin_router.get(
    "/batches/{batch_id}/statements", response_model=List[StatementKeyFigures]
)
async def list_batch_statements(
    batch_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Key figures per statement — the batch drill-down table."""
    batch = _get_batch_or_404(db, batch_id)
    rows = (
        db.query(Statement, BeneficiaryAccount.account_code, Writer.canonical_name)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
        .filter(Statement.batch_id == batch.id)
        .order_by(BeneficiaryAccount.account_code)
        .all()
    )
    return [
        StatementKeyFigures(
            id=stmt.id,
            account_code=account_code,
            writer_name=writer_name,
            period_code=stmt.period_code,
            version=stmt.version,
            parse_status=stmt.parse_status.value,
            calculated=stmt.calculated,
            payable=stmt.payable,
            detail_sum=stmt.detail_sum,
            embedded_total=stmt.embedded_total,
            line_count=stmt.line_count,
            zero_pay_reason=_value(stmt.zero_pay_reason),
        )
        for stmt, account_code, writer_name in rows
    ]


@statements_admin_router.get("/batches/{batch_id}/gate")
async def get_batch_gate(
    batch_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Readiness gate: is this batch safe to distribute, and if not, why."""
    from app.services.distribution.gate import compute_gate

    _get_batch_or_404(db, batch_id)
    return compute_gate(db, batch_id)


@statements_admin_router.post("/batches/{batch_id}/distribute")
async def distribute_batch_endpoint(
    batch_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Gated publish of a batch to writer portals. 409 if the gate isn't green,
    returning the gate state so the UI can show exactly what's blocking."""
    from app.services.distribution.publish import GateNotReady, distribute_batch

    _get_batch_or_404(db, batch_id)
    try:
        return distribute_batch(db, batch_id, published_by=user.id)
    except GateNotReady as e:
        raise HTTPException(status_code=409, detail={"error": "gate_not_ready", "gate": e.gate})


@statements_admin_router.get("/reconcile")
async def reconcile_ingestion_endpoint(
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Automated ingestion audit: re-derives the ground truth from the stored
    statement filenames and verifies the DB matches — file identity, account
    identity, exact-name ownership, and distribution ownership. ok=true means
    ingestion is provably faithful to the source; violations list what's off."""
    from app.services.statement_ingest.reconcile import reconcile_ingestion

    return reconcile_ingestion(db)


@statements_admin_router.get("/{statement_id}", response_model=StatementDetail)
async def get_statement(
    statement_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Full account summary fields + validation status for one statement."""
    row = (
        db.query(Statement, BeneficiaryAccount.account_code, Writer.canonical_name)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
        .filter(Statement.id == statement_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Statement not found")
    stmt, account_code, writer_name = row
    return StatementDetail(
        id=stmt.id,
        batch_id=stmt.batch_id,
        account_code=account_code,
        writer_name=writer_name,
        period_code=stmt.period_code,
        version=stmt.version,
        pdf_path=stmt.pdf_path,
        xlsx_path=stmt.xlsx_path,
        parse_status=stmt.parse_status.value,
        parse_error=stmt.parse_error,
        zero_pay_reason=_value(stmt.zero_pay_reason),
        calculated=stmt.calculated,
        recouped=stmt.recouped,
        reserve_taken=stmt.reserve_taken,
        reserve_released=stmt.reserve_released,
        carried_forward_in=stmt.carried_forward_in,
        carried_forward_out=stmt.carried_forward_out,
        payable_prev=stmt.payable_prev,
        payable_this=stmt.payable_this,
        settlement_paid=stmt.settlement_paid,
        before_tax=stmt.before_tax,
        payable=stmt.payable,
        cheque_amount=stmt.cheque_amount,
        detail_sum=stmt.detail_sum,
        embedded_total=stmt.embedded_total,
        line_count=stmt.line_count,
        finding_counts=_finding_counts(
            db, stmt.batch_id, scope_ref=f"statement:{stmt.id}"
        ),
    )


@statements_admin_router.get("/{statement_id}/lines", response_model=StatementLinesPage)
async def get_statement_lines(
    statement_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=1000),
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Paginated line items (C00139a alone has 10,035 rows — never unpaged)."""
    if db.get(Statement, statement_id) is None:
        raise HTTPException(status_code=404, detail="Statement not found")
    total = (
        db.query(func.count(StatementLine.id))
        .filter(StatementLine.statement_id == statement_id)
        .scalar()
    )
    lines = (
        db.query(StatementLine)
        .filter(StatementLine.statement_id == statement_id)
        .order_by(StatementLine.row_no)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return StatementLinesPage(
        statement_id=statement_id,
        page=page,
        page_size=page_size,
        total=total,
        items=[
            StatementLineOut(
                id=line.id,
                row_no=line.row_no,
                song_code=line.song_code,
                asset_id=line.asset_id,
                custom_id=line.custom_id,
                song_title=line.song_title,
                country=line.country,
                channel=line.channel,
                income_source=line.income_source,
                income_type=line.income_type,
                price=line.price,
                commission_pct=line.commission_pct,
                rbp=line.rbp,
                rate_applied=line.rate_applied,
                writer_split_pct=line.writer_split_pct,
                ben_split_pct=line.ben_split_pct,
                units=line.units,
                earnings=line.earnings,
            )
            for line in lines
        ],
    )


# --- Waiver / acknowledgement workflow (PRD §7.3) ----------------------------


def _get_finding_or_404(db: Session, finding_id: int) -> ValidationFinding:
    finding = db.get(ValidationFinding, finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="Finding not found")
    return finding


@findings_admin_router.post("/{finding_id}/waive", response_model=FindingOut)
async def waive_finding(
    finding_id: int,
    payload: WaiveRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Waive an open blocker/warning with a required reason. The finding's
    stable identity means the waiver survives re-validation runs."""
    finding = _get_finding_or_404(db, finding_id)
    if finding.severity not in (FindingSeverity.BLOCKER, FindingSeverity.WARNING):
        raise HTTPException(
            status_code=409, detail="Only blocker or warning findings can be waived"
        )
    if finding.status != FindingStatus.OPEN:
        raise HTTPException(
            status_code=409, detail=f"Finding is {finding.status.value}, not open"
        )
    finding.status = FindingStatus.WAIVED
    finding.waived_by = user.id
    finding.waived_reason = payload.reason
    finding.waived_at = datetime.now()
    db.commit()
    logger.info(f"Finding {finding.id} ({finding.rule_id}) waived by user {user.id}")
    return _finding_out(finding)


@findings_admin_router.post("/{finding_id}/acknowledge", response_model=FindingOut)
async def acknowledge_finding(
    finding_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Log that an admin saw an open finding. Metadata only — status stays
    open (PRD §5); the Phase-3 distribution gate checks acknowledged_at."""
    finding = _get_finding_or_404(db, finding_id)
    if finding.status != FindingStatus.OPEN:
        raise HTTPException(
            status_code=409, detail=f"Finding is {finding.status.value}, not open"
        )
    finding.acknowledged_by = user.id
    finding.acknowledged_at = datetime.now()
    db.commit()
    logger.info(f"Finding {finding.id} ({finding.rule_id}) acknowledged by user {user.id}")
    return _finding_out(finding)


# --- Distribution (Stage C) --------------------------------------------------


@distributions_admin_router.post("/{distribution_id}/unpublish")
async def unpublish_distribution(
    distribution_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Hide a published statement from the writer's portal, keeping the record
    (reversible — the audit trail survives)."""
    from app.services.distribution.publish import unpublish

    try:
        result = unpublish(db, distribution_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Distribution not found")
    logger.info(f"Distribution {distribution_id} unpublished by user {user.id}")
    return result


def _period_label(code: str) -> str:
    import re

    g = re.search(r"PUB(\d{2})([QH]\d)", code or "")
    return f"{g.group(2)} 20{g.group(1)}" if g else (code or "")


@distributions_admin_router.get("/periods")
async def list_distribution_periods(
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Per reporting-period rollup for the admin Distributions page: statement
    count, total NET payable to writers, and distribution status/date. NET only
    — the gross/publisher-cut split isn't stored per period, so we don't invent
    it. Counts only distributable writer statements (excludes house accounts,
    offboarded clients, unmatched placeholders, and unparsed rows)."""
    from collections import defaultdict

    rows = (
        db.query(
            Statement.id,
            Statement.period_code,
            Statement.detail_sum,
            Statement.payable,
        )
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
        .filter(
            Writer.is_house_account.is_(False),
            Writer.status != WriterStatus.OFFBOARDED,
            Writer.kind.isnot(None),
            Statement.parse_status == ParseStatus.PARSED,
        )
        .all()
    )
    distributed_ids = {
        r[0]
        for r in db.query(Distribution.statement_id)
        .filter(Distribution.portal_visible.is_(True))
        .distinct()
    }
    latest_pub = dict(
        db.query(Distribution.period_code, func.max(Distribution.published_at))
        .filter(Distribution.portal_visible.is_(True))
        .group_by(Distribution.period_code)
        .all()
    )

    agg = defaultdict(lambda: {"statements": 0, "distributed": 0, "net": Decimal("0")})
    for sid, period, detail_sum, payable in rows:
        a = agg[period]
        a["statements"] += 1
        amount = detail_sum if detail_sum is not None else payable
        if amount is not None:
            a["net"] += amount
        if sid in distributed_ids:
            a["distributed"] += 1

    out = []
    for period, a in agg.items():
        if a["distributed"] == 0:
            status = "pending"
        elif a["distributed"] >= a["statements"]:
            status = "distributed"
        else:
            status = "partial"
        pub_at = latest_pub.get(period)
        out.append(
            {
                "period_code": period,
                "label": _period_label(period),
                "statements": a["statements"],
                "distributed": a["distributed"],
                "net_total": str(a["net"]),
                "status": status,
                "distributed_at": pub_at.isoformat() if pub_at else None,
            }
        )
    out.sort(key=lambda r: r["period_code"], reverse=True)
    return out
