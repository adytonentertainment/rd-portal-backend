"""Admin API for client-list import (infra PRD §3.2, §10).

Upload the spreadsheet -> a ClientImport row holding the computed diff +
findings for review. Apply resolves exact matches into the identity graph.
All routes are admin-gated (reuses statements_admin.require_admin).
"""

import hashlib
import os
import tempfile
from datetime import datetime

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.database.session import get_session
from app.logger.logger import get_logger
from app.models.models import User
from app.models.statements import (
    BeneficiaryAccount,
    ClientImport,
    ClientImportStatus,
)
from app.routers.statements_admin import require_admin
from app.services.client_import import importer
from app.services.client_import.parser import parse_client_list

logger = get_logger("clients_import_admin")

client_import_admin_router = APIRouter(
    prefix="/admin/client-imports",
    tags=["Client Import Admin"],
)

_ALLOWED = {".xlsx", ".csv"}


@client_import_admin_router.post("", status_code=201)
async def upload_client_list(
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Upload the client list; compute (but do not apply) the diff + findings."""
    filename = os.path.basename(file.filename or "")
    if os.path.splitext(filename)[1].lower() not in _ALLOWED:
        raise HTTPException(status_code=400, detail="Expected a .xlsx or .csv file")

    data = await file.read()
    sha = hashlib.sha256(data).hexdigest()

    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(filename)[1], delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        result = importer.preview(db, tmp_path)
    finally:
        os.unlink(tmp_path)

    record = ClientImport(
        filename=filename,
        sha256=sha,
        uploaded_by=user.id,
        status=ClientImportStatus.PENDING_REVIEW,
        row_count=result["row_count"],
        diff={"rows": result["rows"]},
        findings=result["findings"],
        stats={**result["stats"], "findings_summary": result["findings_summary"]},
    )
    db.add(record)
    db.commit()
    logger.info(
        f"client-import {record.id}: {result['row_count']} rows, "
        f"{result['stats']['rows_matched']} matched, "
        f"{len(result['findings'])} findings"
    )
    return {
        "id": record.id,
        "status": record.status.value,
        "row_count": record.row_count,
        "stats": record.stats,
        "findings_summary": result["findings_summary"],
    }


@client_import_admin_router.get("/{import_id}")
async def get_client_import(
    import_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    rec = db.get(ClientImport, import_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Client import not found")
    return {
        "id": rec.id,
        "filename": rec.filename,
        "status": rec.status.value,
        "row_count": rec.row_count,
        "stats": rec.stats,
        "findings": rec.findings,
        "diff": rec.diff,
        "uploaded_at": rec.uploaded_at.isoformat() if rec.uploaded_at else None,
        "applied_at": rec.applied_at.isoformat() if rec.applied_at else None,
    }


@client_import_admin_router.get("/{import_id}/queue")
async def get_resolution_queue(
    import_id: int,
    kind: str = Query(default="all", pattern="^(all|probable|unmatched|unlisted)$"),
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """The manual work list (infra PRD §7.2): client rows that didn't auto-
    apply (probable / unmatched) plus statement accounts with no client row
    (unlisted). Exact rows are excluded — they applied automatically."""
    rec = db.get(ClientImport, import_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Client import not found")

    rows = (rec.diff or {}).get("rows", [])
    probable, unmatched = [], []
    for r in rows:
        if r.get("resolved"):
            continue
        conf = r.get("match", {}).get("confidence")
        if conf == "probable":
            probable.append(r)
        elif conf == "none":
            unmatched.append(r)

    unlisted = [
        f["subject"] for f in (rec.findings or [])
        if f.get("rule_id") == "C-UNLISTED-ACCOUNT"
    ]

    payload = {
        "import_id": rec.id,
        "counts": {
            "probable": len(probable),
            "unmatched": len(unmatched),
            "unlisted_accounts": len(unlisted),
        },
    }
    if kind in ("all", "probable"):
        payload["probable"] = probable
    if kind in ("all", "unmatched"):
        payload["unmatched"] = unmatched
    if kind in ("all", "unlisted"):
        payload["unlisted_accounts"] = unlisted
    return payload


@client_import_admin_router.post("/{import_id}/resolve")
async def resolve_queue_row(
    import_id: int,
    sheet: str = Body(...),
    row_no: int = Body(...),
    account_codes: list[str] = Body(default=[]),
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Confirm a match for one queued row: wire the client row's identity to
    the admin-chosen account codes (empty list = create the writer/contacts
    for someone with no earnings this period). The decision is recorded on the
    import's diff so the queue shrinks and re-review is idempotent."""
    rec = db.get(ClientImport, import_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Client import not found")

    rows = (rec.diff or {}).get("rows", [])
    entry = next(
        (r for r in rows if r.get("sheet") == sheet and r.get("row_no") == row_no), None
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Row not found in this import")
    if entry.get("resolved"):
        raise HTTPException(status_code=409, detail="Row already resolved")

    # Validate the chosen accounts exist before touching identity.
    for code in account_codes:
        if db.query(BeneficiaryAccount).filter(
            BeneficiaryAccount.account_code == code
        ).first() is None:
            raise HTTPException(status_code=422, detail=f"Unknown account_code {code!r}")

    # Record the decision on the diff first, then resolve — resolve_row's
    # commit persists both the identity changes and this bookkeeping in one
    # transaction (flagging after the commit would hit an expired instance).
    entry["resolved"] = True
    entry["resolved_account_codes"] = account_codes
    entry["resolved_by"] = user.id
    flag_modified(rec, "diff")

    row = importer.client_row_from_dict(entry)
    summary = importer.resolve_row(db, row, account_codes)
    logger.info(
        f"client-import {rec.id} row {sheet}:{row_no} resolved to "
        f"{account_codes or '[]'} by user {user.id}"
    )
    return {"import_id": rec.id, "row": {"sheet": sheet, "row_no": row_no},
            "account_codes": account_codes, **summary}


@client_import_admin_router.post("/{import_id}/apply")
async def apply_client_import(
    import_id: int,
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Apply exact matches from a reviewed import. The spreadsheet is
    re-supplied and its hash must match the reviewed upload (the diff was
    computed from that exact file — guards against applying a different one)."""
    rec = db.get(ClientImport, import_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Client import not found")
    if rec.status == ClientImportStatus.APPLIED:
        raise HTTPException(status_code=409, detail="Import already applied")

    data = await file.read()
    if rec.sha256 and hashlib.sha256(data).hexdigest() != rec.sha256:
        raise HTTPException(
            status_code=409,
            detail="Uploaded file does not match the reviewed import (hash mismatch)",
        )

    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        rows = parse_client_list(tmp_path)
        summary = importer.apply_rows(db, rows, confirmed_only=True)
    finally:
        os.unlink(tmp_path)

    rec.status = ClientImportStatus.APPLIED
    rec.applied_at = datetime.now()
    rec.applied_by = user.id
    rec.stats = {**(rec.stats or {}), "apply": summary}
    db.commit()
    logger.info(f"client-import {rec.id} applied by user {user.id}: {summary}")
    return {"id": rec.id, "status": rec.status.value, "apply": summary}
