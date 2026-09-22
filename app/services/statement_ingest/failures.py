"""What went wrong with an upload, in words an admin can act on.

The pipeline already RECORDED every failure — sort-stage rejections land in
`upload.stats["sort"]` and parse-stage ones in `Statement.parse_error` — but
nothing ever read them back out. The activity panel could only say "3 failed",
so the only way to find out WHICH three, and why, was to read the server log.

Two stages reject work, and they reject different things:

  * SORT drops FILES. A file whose name doesn't match the naming convention
    never becomes a Statement at all, so it has no row to carry an error —
    its name sits in a list on the upload. Report these by filename.
  * PARSE fails STATEMENTS. These have a row, an account, a period and a
    stored exception string. Report these by account + period.

Anything that only reports the second kind misses a whole class of problem:
2,000 files with a typo'd period code produce zero statements, zero parse
failures, and a dashboard reading $0 with nothing marked wrong.
"""

import re
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.statements import (
    BeneficiaryAccount,
    ParseStatus,
    Statement,
    StatementBatch,
    StatementUpload,
    Writer,
)

# Raw exception text -> (what happened, what to do about it).
#
# Ordered: first match wins, so put the specific patterns above the generic
# ones. The raw string is always kept alongside these — an admin gets the
# sentence, and we still get the traceback text when they forward it.
_PARSE_ERROR_PATTERNS: List[Tuple[str, str, str]] = [
    (
        r"No header row starting with 'Period'",
        "The XLSX has no recognisable detail sheet",
        "The sheet is missing its 'Period' header row — usually a hand-edited "
        "or re-saved export. Re-export it from the source and re-upload.",
    ),
    (
        r"BadZipFile|not a zip file|File is not a zip",
        "The XLSX is corrupt or is not really an XLSX",
        "The file didn't survive the transfer, or a .xls/.csv was renamed to "
        ".xlsx. Re-export and re-upload it.",
    ),
    (
        r"FileNotFoundError|No such file or directory",
        "The stored file is missing from disk",
        "The upload recorded this file but it isn't on the server — most "
        "likely an interrupted transfer. Re-upload this statement.",
    ),
    (
        r"PermissionError|Errno 13",
        "The server couldn't read the stored file",
        "A filesystem permission problem on the server, not a problem with "
        "the statement. Needs an engineer.",
    ),
    (
        r"cannot open|no objects found|FZ_ERROR|damaged|MuPDF",
        "The PDF couldn't be opened",
        "The PDF is damaged or password-protected. Re-export it from the "
        "source and re-upload.",
    ),
    (
        r"UnicodeDecodeError|codec can't decode",
        "The file has an unreadable character encoding",
        "Usually a statement exported as something other than UTF-8. "
        "Re-export it and re-upload.",
    ),
    (
        r"^persist:",
        "The statement parsed, but its rows couldn't be saved",
        "A database error while writing the line detail. The statement is "
        "safe to re-run — nothing partial was kept. Needs an engineer if it "
        "repeats.",
    ),
    (
        r"^worker crashed:",
        "The parser process died on this statement",
        "Often an unusually large file running the worker out of memory. "
        "Re-run the ingest; if it fails again it needs an engineer.",
    ),
    (
        r"MemoryError|Cannot allocate memory",
        "The server ran out of memory parsing this statement",
        "The file is unusually large. Re-run the ingest on its own, away from "
        "a big drop.",
    ),
    (
        r"OperationalError|timeout|deadlock",
        "A database error interrupted this statement",
        "Transient. Re-run the ingest — already-parsed statements are skipped.",
    ),
]


def explain_parse_error(raw: Optional[str]) -> Dict[str, Optional[str]]:
    """Raw exception text -> {reason, hint, raw}.

    An unrecognised error still gets reported: it keeps the raw text as the
    reason rather than being swallowed into a generic "failed", because an
    ugly sentence the admin can forward beats no sentence at all.
    """
    if not raw:
        return {
            "reason": "Failed without recording a reason",
            "hint": "Nothing was stored against this failure. The server log "
                    "for this upload will have it.",
            "raw": None,
        }
    for pattern, reason, hint in _PARSE_ERROR_PATTERNS:
        if re.search(pattern, raw, re.IGNORECASE):
            return {"reason": reason, "hint": hint, "raw": raw}
    return {
        "reason": raw.strip().splitlines()[0][:200],
        "hint": "Unrecognised parser error — forward this to engineering.",
        "raw": raw,
    }


# Sort-stage rejection lists, and what each one means. These are keyed by the
# stats field name the sorter writes.
_SORT_REASONS: Dict[str, Tuple[str, str, str]] = {
    "unparseable": (
        "blocker",
        "Filename doesn't match the naming convention",
        "The sorter couldn't read a period and account code out of this "
        "filename, so it was skipped entirely. Rename it to the "
        "ACCOUNT_PERIOD form and re-upload.",
    ),
    "unpaired": (
        "warning",
        "Only one half of the statement arrived",
        "A statement needs both its PDF and its XLSX. The half that arrived "
        "was ingested; upload the missing half to complete it.",
    ),
    "duplicates": (
        "warning",
        "A second file for the same account and period in this drop",
        "Two files claimed the same account+period. The first was used and "
        "this one skipped — check whether it's a genuine revision.",
    ),
}


def _own_statement_query(db: Session, upload: StatementUpload):
    """Statements belonging to THIS upload.

    Ownership is `stats.sort.statement_ids`, not `batch.upload_id`: batches are
    reused across uploads (keyed period+catalog), so batch.upload_id names
    whichever upload first created the batch. Falling back to it would report
    another upload's failures as this one's. Same resolution the upload
    statements endpoint uses — kept identical on purpose.
    """
    q = (
        db.query(Statement, BeneficiaryAccount.account_code, Writer.canonical_name)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
    )
    statement_ids = (upload.stats or {}).get("sort", {}).get("statement_ids")
    if statement_ids:
        return q.filter(Statement.id.in_(statement_ids))
    return q.join(StatementBatch, Statement.batch_id == StatementBatch.id).filter(
        StatementBatch.upload_id == upload.id
    )


def collect_failures(db: Session, upload: StatementUpload) -> Dict:
    """Every problem this upload hit, flattened into one reportable list.

    Rows are ordered blockers first, because that is the order they need
    acting on, and a 2,000-file drop can produce a long tail of warnings that
    would otherwise bury the three files that actually failed.
    """
    rows: List[Dict] = []
    stats = upload.stats or {}

    # 1. Whole-upload failure (transfer abandoned, a stage raised).
    if stats.get("error"):
        raw = str(stats["error"])
        stage, _, rest = raw.partition(": ")
        explained = explain_parse_error(rest or raw)
        rows.append({
            "severity": "blocker",
            "stage": stage or "upload",
            "file": None,
            "account_code": None,
            "writer_name": None,
            "period_code": None,
            "statement_id": None,
            "reason": f"The upload itself failed: {explained['reason']}",
            "hint": explained["hint"],
            "raw": raw,
        })

    # 2. Sort-stage file rejections — no Statement row exists for these.
    sort_stats = stats.get("sort") or {}
    for field, (severity, reason, hint) in _SORT_REASONS.items():
        for filename in sort_stats.get(field) or []:
            rows.append({
                "severity": severity,
                "stage": "sort",
                "file": filename,
                "account_code": None,
                "writer_name": None,
                "period_code": None,
                "statement_id": None,
                "reason": reason,
                "hint": hint,
                "raw": None,
            })

    # 3. Parse-stage statement failures.
    failed = _own_statement_query(db, upload).filter(
        Statement.parse_status == ParseStatus.FAILED
    ).all()
    for stmt, account_code, writer_name in failed:
        explained = explain_parse_error(stmt.parse_error)
        rows.append({
            "severity": "blocker",
            "stage": "parse",
            # Which half failed isn't recorded separately, so name both stored
            # files — the admin needs to know what to go and re-export.
            "file": ", ".join(
                os_basename(p) for p in (stmt.xlsx_path, stmt.pdf_path) if p
            ) or None,
            "account_code": account_code,
            "writer_name": writer_name,
            "period_code": stmt.period_code,
            "statement_id": stmt.id,
            "reason": explained["reason"],
            "hint": explained["hint"],
            "raw": explained["raw"],
        })

    order = {"blocker": 0, "warning": 1}
    rows.sort(key=lambda r: (order.get(r["severity"], 2), r["stage"], r["file"] or ""))

    return {
        "upload_id": upload.id,
        "status": upload.status.value,
        "counts": {
            "blockers": sum(1 for r in rows if r["severity"] == "blocker"),
            "warnings": sum(1 for r in rows if r["severity"] == "warning"),
            "total": len(rows),
        },
        "items": rows,
    }


def os_basename(path: str) -> str:
    """Basename without importing os into every call site; stored paths are
    relative to the storage root, and the admin only recognises the filename."""
    return path.replace("\\", "/").rsplit("/", 1)[-1]
