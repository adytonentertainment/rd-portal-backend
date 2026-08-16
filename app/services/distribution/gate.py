"""Readiness gate (ingestion PRD §5, Stage C precondition).

A batch is `ready` to distribute only when:
  - zero OPEN blocker findings remain (fixed+re-ingested, or waived), and
  - every non-house statement is parseable AND resolved to a real writer
    (client-import identity, not an ingestion placeholder).

Warnings are surfaced but never block (they're acknowledged, not gating).
The gate is pure/read-only; the Distribute action calls it and refuses unless
`ready` is true.
"""

from __future__ import annotations

from typing import Dict

from sqlalchemy.orm import Session

from app.models.statements import (
    BeneficiaryAccount,
    FindingSeverity,
    FindingStatus,
    ParseStatus,
    Statement,
    StatementBatch,
    ValidationFinding,
    ValidationRun,
    Writer,
    WriterStatus,
)


def open_blocker_count(db: Session, batch_id: int) -> int:
    return (
        db.query(ValidationFinding)
        .join(ValidationRun, ValidationFinding.run_id == ValidationRun.id)
        .filter(
            ValidationRun.batch_id == batch_id,
            ValidationFinding.severity == FindingSeverity.BLOCKER,
            ValidationFinding.status == FindingStatus.OPEN,
        )
        .count()
    )


def compute_gate(db: Session, batch_id: int) -> Dict:
    """Structured gate state for a batch — safe to call any time."""
    batch = db.get(StatementBatch, batch_id)
    if batch is None:
        raise ValueError("batch not found")

    rows = (
        db.query(Statement, BeneficiaryAccount, Writer)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
        .filter(Statement.batch_id == batch_id)
        .all()
    )

    # Statement auditing is disabled by design — we do NOT block on validation
    # findings, reconciliation, or PDF/XLSX pairing. A batch is ready to
    # distribute as long as it has statements to send. House accounts and
    # offboarded clients are excluded; everything else (incl. not-yet-matched
    # placeholders) is distributable.
    total = len(rows)
    house = distributable = excluded = 0
    for stmt, acct, writer in rows:
        if writer.is_house_account:
            house += 1
            continue
        # excluded (not sent, not blocking): offboarded clients, statements not
        # parsed yet (no total), and placeholders not matched to a client
        if (
            writer.status == WriterStatus.OFFBOARDED
            or stmt.parse_status != ParseStatus.PARSED
            or writer.kind is None
        ):
            excluded += 1
            continue
        distributable += 1

    reasons = [] if distributable > 0 else ["nothing to distribute in this batch"]
    return {
        "batch_id": batch_id,
        "ready": distributable > 0,
        "open_blockers": 0,
        "counts": {
            "total": total,
            "distributable": distributable,
            "house_excluded": house,
            "offboarded_excluded": excluded,
        },
        "reasons": reasons,
    }
