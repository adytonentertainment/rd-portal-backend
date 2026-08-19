"""Readiness gate (ingestion PRD §5, Stage C precondition).

A batch is `ready` to distribute only when nothing in it still needs
attention: every non-house, non-offboarded statement is parsed, matched to a
real client (not an ingestion placeholder), and that client has the baseline
info a send depends on.

Validation findings, reconciliation and PDF/XLSX pairing do NOT gate —
statement auditing is off by design.
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

    # NOTHING THAT NEEDS ATTENTION MAY BE IN A SEND (Steven, 2026-08-18).
    #
    # This previously EXCLUDED unresolved rows and sent the rest: a statement
    # whose account no client-list row claims was quietly dropped from the
    # batch and the publisher was told the send succeeded. The money for that
    # account then sits undelivered with nothing on screen saying so.
    #
    # So an unresolved row now BLOCKS the batch it appears in. Fixing one is
    # not busywork — it is deciding who gets paid — and the reasons below name
    # each blocker so the fix list is the gate itself.
    #
    # Still not blocking: validation findings, reconciliation and PDF/XLSX
    # pairing (auditing stays off by design), house accounts, and clients
    # already offboarded.
    total = len(rows)
    house = distributable = offboarded = unparsed = 0
    unmatched_names, missing_info_names = [], []

    for stmt, acct, writer in rows:
        if writer.is_house_account:
            house += 1
            continue
        if writer.status == WriterStatus.OFFBOARDED:
            offboarded += 1
            continue
        if stmt.parse_status != ParseStatus.PARSED:
            # Not yet readable, so not yet sendable — and not yet a decision
            # anyone can make. Counted, not blamed on the client.
            unparsed += 1
            continue
        if writer.kind is None:
            # A placeholder holding somebody's statement. Who it belongs to is
            # unanswered, so sending is guesswork.
            name = acct.display_name or writer.canonical_name
            if name not in unmatched_names:
                unmatched_names.append(name)
            continue
        if not writer.expected_catalogs or writer.cadence is None:
            name = writer.canonical_name
            if name not in missing_info_names:
                missing_info_names.append(name)
            continue
        distributable += 1

    reasons = []
    if unmatched_names:
        reasons.append(
            f"{len(unmatched_names)} account(s) not matched to a client: "
            + ", ".join(unmatched_names[:5])
            + ("…" if len(unmatched_names) > 5 else "")
        )
    if missing_info_names:
        reasons.append(
            f"{len(missing_info_names)} client(s) missing revenue type or cadence: "
            + ", ".join(missing_info_names[:5])
            + ("…" if len(missing_info_names) > 5 else "")
        )
    if unparsed:
        reasons.append(f"{unparsed} statement(s) not parsed yet")
    if not reasons and distributable == 0:
        reasons.append("nothing to distribute in this batch")

    return {
        "batch_id": batch_id,
        "ready": not reasons,
        "open_blockers": len(unmatched_names) + len(missing_info_names),
        "counts": {
            "total": total,
            "distributable": distributable,
            "house_excluded": house,
            "offboarded_excluded": offboarded,
            "unparsed": unparsed,
            "unmatched": len(unmatched_names),
            "missing_info": len(missing_info_names),
        },
        # The fix list, in the order a publisher would work it.
        "needs_attention": {
            "unmatched": unmatched_names,
            "missing_info": missing_info_names,
        },
        "reasons": reasons,
    }
