"""Level 1 — file integrity rules V-FILE-1..6 (PRD §7.2).

All checks read state persisted by earlier pipeline stages (statement rows,
the creating upload's sort stats) — no statement file is re-opened here.

scope_ref conventions (stable identity across runs):
- statement-scope findings: ``statement:{id}`` (statement rows persist; a
  re-upload becomes a new version/row, correctly yielding a new identity)
- file-scope findings (no statement row exists): ``file:{filename}``

Attribution caveat, Phase 1: sort-stage problems (unparseable names,
duplicate files) live on the upload, and an upload can spawn several
batches. Unparseable names carry no period/catalog, so they surface on
every batch of that upload; duplicate files DO parse, so they are filtered
to the batch whose period+catalog they match.
"""

from typing import List

from app.models.statements import FindingScope, FindingSeverity, ParseStatus, Statement
from app.services.statement_ingest.filename_parser import parse_statement_filename
from app.services.statement_ingest.sorter import _infer_catalog_from_code
from app.services.validation.engine import Rule, RuleContext, RuleFinding, register


def _statement_ref(statement: Statement) -> str:
    return f"statement:{statement.id}"


def _file_ref(filename: str) -> str:
    return f"file:{filename}"


def _statement_details(statement: Statement) -> dict:
    return {
        "statement_id": statement.id,
        "account_code": statement.account.account_code,
        "period_code": statement.period_code,
    }


def check_pair_completeness(ctx: RuleContext) -> List[RuleFinding]:
    """V-FILE-1: every PDF has its XLSX and vice versa, except accounts
    flagged pdf_only/xlsx_only (house accounts CPJ001, CS0001)."""
    findings = []
    for statement in ctx.statements:
        account = statement.account
        if statement.pdf_path and not statement.xlsx_path and not account.pdf_only:
            findings.append(
                RuleFinding(
                    scope_ref=_statement_ref(statement),
                    message=(
                        f"{account.account_code} {statement.period_code}: summary PDF "
                        "has no detail XLSX"
                    ),
                    details=dict(_statement_details(statement), missing="xlsx"),
                )
            )
        if statement.xlsx_path and not statement.pdf_path and not account.xlsx_only:
            findings.append(
                RuleFinding(
                    scope_ref=_statement_ref(statement),
                    message=(
                        f"{account.account_code} {statement.period_code}: detail XLSX "
                        "has no summary PDF"
                    ),
                    details=dict(_statement_details(statement), missing="pdf"),
                )
            )
    return findings


def check_filenames_parse(ctx: RuleContext) -> List[RuleFinding]:
    """V-FILE-2: filename parses to (period, account_code, type), and a
    statement's file period matches its batch period."""
    findings = [
        RuleFinding(
            scope_ref=_file_ref(filename),
            message=f"Filename could not be parsed to (period, account, type): {filename}",
            details={"filename": filename},
            scope=FindingScope.BATCH,
        )
        for filename in ctx.sort_stat("unparseable")
    ]
    for statement in ctx.statements:
        if statement.period_code != ctx.batch.period_code:
            findings.append(
                RuleFinding(
                    scope_ref=_statement_ref(statement),
                    message=(
                        f"{statement.account.account_code}: file period "
                        f"{statement.period_code} != batch period {ctx.batch.period_code}"
                    ),
                    details=dict(
                        _statement_details(statement),
                        batch_period=ctx.batch.period_code,
                    ),
                )
            )
    return findings


def check_xlsx_parsed(ctx: RuleContext) -> List[RuleFinding]:
    """V-FILE-3: XLSX opens, header row found, required columns mapped —
    i.e. the parse stage succeeded for statements that have an XLSX (the
    parser raises on every one of those failure modes)."""
    findings = []
    for statement in ctx.statements:
        if statement.xlsx_path and statement.parse_status == ParseStatus.FAILED:
            findings.append(
                RuleFinding(
                    scope_ref=_statement_ref(statement),
                    message=(
                        f"{statement.account.account_code} {statement.period_code}: "
                        "statement failed to parse"
                    ),
                    details=dict(_statement_details(statement), error=statement.parse_error),
                )
            )
    return findings


def check_grand_total_row(ctx: RuleContext) -> List[RuleFinding]:
    """V-FILE-4: the XLSX contains a grand-total row (parsed statements
    missing embedded_total had none)."""
    findings = []
    for statement in ctx.statements:
        if (
            statement.xlsx_path
            and statement.parse_status == ParseStatus.PARSED
            and statement.embedded_total is None
        ):
            findings.append(
                RuleFinding(
                    scope_ref=_statement_ref(statement),
                    message=(
                        f"{statement.account.account_code} {statement.period_code}: "
                        "no grand-total row found in detail XLSX"
                    ),
                    details=_statement_details(statement),
                )
            )
    return findings


def check_pdf_summary_extracted(ctx: RuleContext) -> List[RuleFinding]:
    """V-FILE-5: PDF account summary extracted — calculated and payable both
    found. A PDF-only statement whose parse failed lands here too (a failed
    pair is already V-FILE-3, where the error detail lives)."""
    findings = []
    for statement in ctx.statements:
        if not statement.pdf_path:
            continue
        missing = None
        if statement.parse_status == ParseStatus.PARSED:
            missing = [
                f
                for f in ("calculated", "payable")
                if getattr(statement, f) is None
            ]
        elif statement.parse_status == ParseStatus.FAILED and not statement.xlsx_path:
            missing = ["calculated", "payable"]
        if missing:
            findings.append(
                RuleFinding(
                    scope_ref=_statement_ref(statement),
                    message=(
                        f"{statement.account.account_code} {statement.period_code}: "
                        f"PDF account summary incomplete ({', '.join(missing)} not found)"
                    ),
                    details=dict(
                        _statement_details(statement),
                        missing_fields=missing,
                        error=statement.parse_error,
                    ),
                )
            )
    return findings


def check_duplicate_files(ctx: RuleContext) -> List[RuleFinding]:
    """V-FILE-6: duplicate file for the same (account, period) in one drop.
    The sorter recorded the duplicates' filenames; they parse, so each is
    attributed to the batch matching its period + catalog."""
    findings = []
    for filename in ctx.sort_stat("duplicates"):
        parsed = parse_statement_filename(filename)
        if parsed is None or parsed.period_code != ctx.batch.period_code:
            continue
        catalog = parsed.royalty_type or _infer_catalog_from_code(parsed.account_code)
        if catalog is not None and catalog != ctx.batch.catalog:
            continue
        findings.append(
            RuleFinding(
                scope_ref=_file_ref(filename),
                message=(
                    f"Duplicate file for ({parsed.account_code}, "
                    f"{parsed.period_code}): {filename}"
                ),
                details={
                    "filename": filename,
                    "account_code": parsed.account_code,
                    "period_code": parsed.period_code,
                },
                scope=FindingScope.BATCH,
            )
        )
    return findings


register(
    Rule(
        id="V-FILE-1",
        level=1,
        default_severity=FindingSeverity.BLOCKER,
        scope=FindingScope.STATEMENT,
        description="Every PDF has its XLSX and vice versa (pdf_only/xlsx_only accounts exempt)",
        check=check_pair_completeness,
    )
)
register(
    Rule(
        id="V-FILE-2",
        level=1,
        default_severity=FindingSeverity.BLOCKER,
        scope=FindingScope.STATEMENT,
        description="Filename parses to (period, account_code, type); file period == batch period",
        check=check_filenames_parse,
    )
)
register(
    Rule(
        id="V-FILE-3",
        level=1,
        default_severity=FindingSeverity.BLOCKER,
        scope=FindingScope.STATEMENT,
        description="XLSX opens, header row found, all required columns mapped",
        check=check_xlsx_parsed,
    )
)
register(
    Rule(
        id="V-FILE-4",
        level=1,
        default_severity=FindingSeverity.WARNING,
        scope=FindingScope.STATEMENT,
        description="XLSX contains exactly one grand-total row, at the end",
        check=check_grand_total_row,
    )
)
register(
    Rule(
        id="V-FILE-5",
        level=1,
        default_severity=FindingSeverity.BLOCKER,
        scope=FindingScope.STATEMENT,
        description="PDF account summary extracted: calculated and payable both found",
        check=check_pdf_summary_extracted,
    )
)
register(
    Rule(
        id="V-FILE-6",
        level=1,
        default_severity=FindingSeverity.BLOCKER,
        scope=FindingScope.BATCH,
        description="Duplicate file for same (account, period) in one drop",
        check=check_duplicate_files,
    )
)
