"""Declarative validation rules engine (PRD §7.1).

Each rule is data — ``{id, level, default_severity, scope, check(ctx)}`` — in a
registry; ~30 rules across phases share this one engine. ``check(ctx)`` returns
plain :class:`RuleFinding` values; severity comes from the registry (a rule may
override per finding, e.g. V-STMT-2's warning-vs-blocker by magnitude), never
from code branches in the engine.

Findings carry a stable identity ``(rule_id, scope_ref)`` within a batch:

- A re-run RE-ATTACHES the existing finding row to the new validation_run
  (refreshing message/details), so waived findings stay waived — one row per
  identity for the finding's whole life, and the finding id an admin waived
  keeps existing.
- An identity that is no longer produced is closed (status=resolved, keeping
  its last run for the audit trail). If it later reappears, the row is
  reopened and re-attached.

V-FILE rules read only what earlier pipeline stages persisted (statement rows
+ the upload's sort stats) — validation never re-opens statement files.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.logger.logger import get_logger
from app.models.statements import (
    BatchStatus,
    FindingScope,
    FindingSeverity,
    FindingStatus,
    Statement,
    StatementBatch,
    StatementUpload,
    ValidationFinding,
    ValidationRun,
)

logger = get_logger("validation_engine")

RULES_VERSION = "phase1.v-stmt.1"


@dataclass(frozen=True)
class RuleFinding:
    """One problem instance produced by a rule's check(ctx)."""

    scope_ref: str  # stable within the batch, e.g. "statement:42" / "file:a.pdf"
    message: str
    details: Optional[Dict] = None
    # Per-finding overrides; None -> take the rule's registry values
    severity: Optional[FindingSeverity] = None
    scope: Optional[FindingScope] = None


@dataclass(frozen=True)
class RuleContext:
    """Everything a check(ctx) may look at. Keep generic — rules of all
    levels (file/statement/ledger/batch) share this signature."""

    session: Session
    batch: StatementBatch
    statements: List[Statement]
    # The upload that created the batch (sort-stage problems — unparseable
    # names, duplicate files — live in its stats, not on any statement row)
    upload: Optional[StatementUpload]

    def sort_stat(self, key: str) -> List:
        if self.upload is None:
            return []
        return list(((self.upload.stats or {}).get("sort", {})).get(key, []))


@dataclass(frozen=True)
class Rule:
    id: str  # e.g. "V-FILE-1"
    level: int  # PRD §7.2 level: 1=file, 2=statement, 3=ledger, 4=batch
    default_severity: FindingSeverity
    scope: FindingScope
    description: str
    check: Callable[[RuleContext], List[RuleFinding]] = field(compare=False)


# Registration order == execution order (rule levels first to last)
REGISTRY: Dict[str, Rule] = {}


def register(rule: Rule) -> Rule:
    if rule.id in REGISTRY:
        raise ValueError(f"validation rule {rule.id} registered twice")
    REGISTRY[rule.id] = rule
    return rule


def run_validation(batch_id: int, session: Session) -> ValidationRun:
    """Run every registered rule against a batch; persist validation_run +
    findings (reconciled by stable identity, see module docstring). Commits.

    Run counters count OPEN findings only — waived ones are out of the gate
    by definition (PRD §7.3). Batch status: any open blocker/warning ->
    needs_review, otherwise approved-eligible (PRD §6 stage 6).
    """
    batch = session.get(StatementBatch, batch_id)
    if batch is None:
        raise ValueError(f"statement_batch {batch_id} not found")

    statements = (
        session.query(Statement)
        .filter(Statement.batch_id == batch.id)
        .order_by(Statement.id)
        .all()
    )
    upload = (
        session.get(StatementUpload, batch.upload_id) if batch.upload_id else None
    )
    ctx = RuleContext(session=session, batch=batch, statements=statements, upload=upload)

    run = ValidationRun(batch_id=batch.id, rules_version=RULES_VERSION)
    session.add(run)
    session.flush()

    # (rule_id, scope_ref) -> (rule, finding); same-run duplicates collapse
    computed: Dict[Tuple[str, str], Tuple[Rule, RuleFinding]] = {}
    for rule in REGISTRY.values():
        for finding in rule.check(ctx):
            computed[(rule.id, finding.scope_ref)] = (rule, finding)

    # Latest existing row per identity across this batch's previous runs
    existing = (
        session.query(ValidationFinding)
        .join(ValidationRun, ValidationFinding.run_id == ValidationRun.id)
        .filter(ValidationRun.batch_id == batch.id, ValidationFinding.run_id != run.id)
        .all()
    )
    by_identity: Dict[Tuple[str, Optional[str]], ValidationFinding] = {}
    for row in existing:
        key = (row.rule_id, row.scope_ref)
        if key not in by_identity or row.id > by_identity[key].id:
            by_identity[key] = row

    for key, (rule, finding) in computed.items():
        previous = by_identity.get(key)
        severity = finding.severity or rule.default_severity
        scope = finding.scope or rule.scope
        if previous is not None:
            previous.run_id = run.id
            previous.severity = severity
            previous.scope = scope
            previous.message = finding.message
            previous.details = finding.details
            if previous.status == FindingStatus.RESOLVED:
                previous.status = FindingStatus.OPEN  # problem reappeared
        else:
            session.add(
                ValidationFinding(
                    run_id=run.id,
                    rule_id=rule.id,
                    severity=severity,
                    scope=scope,
                    scope_ref=finding.scope_ref,
                    message=finding.message,
                    details=finding.details,
                    status=FindingStatus.OPEN,
                )
            )

    for key, previous in by_identity.items():
        if key not in computed and previous.status != FindingStatus.RESOLVED:
            previous.status = FindingStatus.RESOLVED
    session.flush()

    open_counts = {severity: 0 for severity in FindingSeverity}
    for (severity, count) in (
        session.query(ValidationFinding.severity, func.count(ValidationFinding.id))
        .filter(
            ValidationFinding.run_id == run.id,
            ValidationFinding.status == FindingStatus.OPEN,
        )
        .group_by(ValidationFinding.severity)
        .all()
    ):
        open_counts[severity] = count
    run.blockers = open_counts[FindingSeverity.BLOCKER]
    run.warnings = open_counts[FindingSeverity.WARNING]
    run.infos = open_counts[FindingSeverity.INFO]
    run.finished_at = datetime.now()

    if run.blockers or run.warnings:
        batch.status = BatchStatus.NEEDS_REVIEW
    elif batch.status not in (BatchStatus.APPROVED, BatchStatus.DISTRIBUTED):
        batch.status = BatchStatus.APPROVED  # approved-eligible (PRD §6)
    session.commit()

    logger.info(
        f"Validated batch {batch.id} (run {run.id}): {run.blockers} blockers, "
        f"{run.warnings} warnings, {run.infos} infos"
    )
    return run


# Registers the rules on import (same bottom-import pattern as
# app/models/models.py -> statements); import order == execution order
from app.services.validation import file_rules  # noqa: E402,F401
from app.services.validation import stmt_rules  # noqa: E402,F401
