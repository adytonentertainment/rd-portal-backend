"""Gated publish / unpublish to writer portals (ingestion PRD Stage C).

Rules:
  - Enforce the readiness gate (gate.compute_gate.ready) — refuse otherwise.
  - Idempotent: a statement already actively distributed is skipped.
  - Supersede-on-reingest: a newer statement for the same (writer, ACCOUNT,
    catalog, period) hides the prior active distribution and links
    `superseded_by`. The account is part of the key — one writer can hold
    several accounts in a catalog, and each issues its own statement.
  - Cadence de-dup: an account's period is seen once. A semiannual statement
    supersedes an already-distributed quarterly it covers; a quarterly is
    skipped when a covering semiannual is already active.
  - Reversible: unpublish flips portal_visible off, keeps the row.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.statements import (
    BatchStatus,
    BeneficiaryAccount,
    Distribution,
    ParseStatus,
    Statement,
    StatementBatch,
    Writer,
)

from .gate import compute_gate
from .periods import covers


class GateNotReady(Exception):
    def __init__(self, gate: dict):
        self.gate = gate
        super().__init__("; ".join(gate.get("reasons", [])) or "gate not ready")


def _active_distributions_for_account(db: Session, writer_id: int, account_id: int, catalog):
    """Active distributions for ONE beneficiary account.

    Scoped to the account, not just the writer: a writer can hold several
    accounts in the same catalog (J. Stalin has Mechanical CSJ024 *and* JN0191),
    and each issues its own statement for the same period. Keyed only on
    (writer, catalog, period) those look like re-ingests of each other, so
    publishing one silently superseded the other — the writer lost a statement,
    its PDF, and its line items, with no error anywhere. Every rule that relies
    on this (supersede-on-reingest, semiannual-covers-quarterly) is per-account
    anyway: verified against the real data, every cadence-covering pair shares
    an account and none crosses accounts.
    """
    return (
        db.query(Distribution)
        .join(Statement, Distribution.statement_id == Statement.id)
        .filter(
            Distribution.writer_id == writer_id,
            Statement.account_id == account_id,
            Distribution.catalog == catalog,
            Distribution.portal_visible.is_(True),
            Distribution.superseded_by.is_(None),
        )
        .all()
    )


def _distributable_statements(db: Session, batch_id: int):
    rows = (
        db.query(Statement, BeneficiaryAccount, Writer)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
        .filter(Statement.batch_id == batch_id)
        .all()
    )
    from app.models.statements import WriterStatus

    out = []
    for stmt, acct, writer in rows:
        if writer.is_house_account or writer.kind is None:
            continue
        if writer.status == WriterStatus.OFFBOARDED:
            continue
        if stmt.parse_status != ParseStatus.PARSED:
            continue
        out.append((stmt, acct, writer))
    return out


def distribute_batch(
    db: Session, batch_id: int, published_by: Optional[int] = None
) -> dict:
    gate = compute_gate(db, batch_id)
    if not gate["ready"]:
        raise GateNotReady(gate)

    batch = db.get(StatementBatch, batch_id)
    now = datetime.now()
    published = superseded = skipped_dedup = already = 0

    for stmt, acct, writer in _distributable_statements(db, batch_id):
        actives = _active_distributions_for_account(db, writer.id, acct.id, batch.catalog)
        skip = False
        to_supersede: List[Distribution] = []

        for d in actives:
            if d.statement_id == stmt.id:
                already += 1
                skip = True
                break
            if d.period_code == stmt.period_code:
                # same period, different (re-ingested) statement -> supersede old
                to_supersede.append(d)
            elif covers(d.period_code, stmt.period_code):
                # an active semiannual already covers this quarterly -> skip
                skip = True
                break
            elif covers(stmt.period_code, d.period_code):
                # this semiannual covers an active quarterly -> supersede it
                to_supersede.append(d)
        if skip:
            if not to_supersede:
                skipped_dedup += 1
            continue

        dist = Distribution(
            statement_id=stmt.id,
            writer_id=writer.id,
            batch_id=batch_id,
            period_code=stmt.period_code,
            catalog=batch.catalog,
            published_at=now,
            published_by=published_by,
            portal_visible=True,
            gate_snapshot=gate,
        )
        db.add(dist)
        db.flush()
        for old in to_supersede:
            old.portal_visible = False
            old.superseded_by = dist.id
            superseded += 1
        published += 1

    if batch is not None:
        batch.status = BatchStatus.DISTRIBUTED
    db.commit()
    return {
        "batch_id": batch_id,
        "published": published,
        "superseded": superseded,
        "skipped_cadence_dedup": skipped_dedup,
        "already_distributed": already,
    }


def unpublish(db: Session, distribution_id: int) -> dict:
    """Hide a distribution from the portal, keeping the record (reversible)."""
    dist = db.get(Distribution, distribution_id)
    if dist is None:
        raise ValueError("distribution not found")
    dist.portal_visible = False
    db.commit()
    return {"distribution_id": distribution_id, "portal_visible": False}
