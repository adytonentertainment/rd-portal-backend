"""Sort stage of the statement import pipeline (PRD §6 stage 2).

Takes an upload's loose incoming files, parses every filename, derives the
batches per (period_code, catalog), and pairs each detail XLSX with its one
summarizing PDF by (period_code, account_code) — never by display name, which
drifts between the two files of a pair (PRD §2.3).

Problems (unpaired files, unparseable names, duplicates of already-ingested
statements) are recorded in the upload stats for later validation, never
raised — sorting always continues.

Files are copied (not moved) from incoming/ to {root}/{period}/{catalog}/ so
the incoming originals remain an immutable record and re-runs are idempotent.
"""

import os
import re
import shutil
from collections import defaultdict
from typing import Dict, Optional

from sqlalchemy.orm import Session

from app.logger.logger import get_logger
from app.models.statements import (
    BatchStatus,
    BeneficiaryAccount,
    Catalog,
    ParseStatus,
    Publisher,
    Statement,
    StatementBatch,
    StatementUpload,
    UploadStatus,
    Writer,
)
from app.services.client_import.matcher import _pre_paren, _token_set, normalize
from app.services.statement_ingest.filename_parser import (
    ParsedStatementFilename,
    parse_statement_filename,
)
from app.services.statement_ingest.storage import incoming_dir, sorted_dir, to_storage_relative

logger = get_logger("statement_sorter")

# Single-publisher world for Phase 1 (PRD §5 example row)
DEFAULT_PUBLISHER_NAME = "Regalias Digitales, LLC"

_CATALOG_LABEL = {
    Catalog.MECH: "Mechanical",
    Catalog.YT: "YouTube",
    Catalog.PERF: "Performance",
}

# Account code conventions per PRD §2.3, for files whose name carries no
# royalty-type parens. Order matters: CSJ/CPJ before the bare CS/C patterns.
_CODE_CATALOG_PATTERNS = (
    (re.compile(r"^(?:CSJ|JN)"), Catalog.MECH),
    (re.compile(r"^CPJ\d"), Catalog.PERF),  # house performance
    (re.compile(r"^CS\d"), Catalog.YT),  # house/special YouTube
    (re.compile(r"^C\d"), Catalog.YT),
)


def _batch_label(period_code: str, catalog: Catalog) -> str:
    # PUB26H1 -> "YouTube 2026H1"
    return f"{_CATALOG_LABEL[catalog]} 20{period_code[3:]}"


def _infer_catalog_from_code(account_code: str) -> Optional[Catalog]:
    for pattern, catalog in _CODE_CATALOG_PATTERNS:
        if pattern.match(account_code):
            return catalog
    return None


def _get_or_create_publisher(session: Session) -> Publisher:
    publisher = (
        session.query(Publisher).filter(Publisher.name == DEFAULT_PUBLISHER_NAME).first()
    )
    if publisher is None:
        publisher = Publisher(name=DEFAULT_PUBLISHER_NAME)
        session.add(publisher)
        session.flush()
    return publisher


def _get_or_create_batch(
    session: Session,
    upload: StatementUpload,
    publisher: Publisher,
    period_code: str,
    catalog: Catalog,
    parsed: ParsedStatementFilename,
) -> StatementBatch:
    batch = (
        session.query(StatementBatch)
        .filter(
            StatementBatch.period_code == period_code,
            StatementBatch.catalog == catalog,
            StatementBatch.publisher_id == publisher.id,
        )
        .first()
    )
    if batch is None:
        batch = StatementBatch(
            publisher_id=publisher.id,
            label=_batch_label(period_code, catalog),
            period_code=period_code,
            catalog=catalog,
            cadence=parsed.cadence,
            upload_id=upload.id,
            uploaded_by=upload.uploaded_by,
            uploaded_at=upload.uploaded_at,
            status=BatchStatus.UPLOADED,
        )
        session.add(batch)
        session.flush()
    return batch


# Ingest attaches money to people, so it NEVER guesses: an account folds into
# an existing writer only on an exact normalized name, or a strict token-subset
# ("Swifty Blue NEW" ⊇ "Swifty Blue", "Luciano Luna Diaz" ⊇ "Luciano Luna")
# where the shorter name still has ≥2 distinctive tokens. Anything looser stays
# a placeholder and surfaces as an unmatched account for a human to resolve.
_SUBSET_MIN_TOKENS = 2


def _match_existing_writer(session: Session, parsed: ParsedStatementFilename):
    """Find an existing writer that is the same person as this statement's
    display name — exact normalized name, or a token-set-Jaccard near-miss —
    preferring a resolved client identity (kind IS NOT NULL) over a placeholder.
    Returns None if nothing clears the fuzzy bar (then the caller mints a new
    placeholder). Reuses the client-import matcher's normalize/token logic so
    ingest and the resolver agree on what "the same client" means."""
    target = parsed.display_name or ""
    target_keys = {normalize(target), normalize(_pre_paren(target))} - {""}
    # Fuzzy matching compares the PRE-PARENTHETICAL name (the actual entity),
    # never the full string: a group tag like "(Luna Negra)" is shared by every
    # member of that group ("Edimusin (Luna Negra)", "Isa Music (Luna Negra)"),
    # so scoring on the full token set would collapse the whole group into the
    # group parent. Pre-paren tokens keep distinct members distinct.
    target_tokens = _token_set(_pre_paren(target))

    # candidate pool: same house-account status only (a house account must never
    # fold into a real client, or vice-versa)
    candidates = (
        session.query(Writer)
        .filter(Writer.is_house_account == parsed.is_house)
        .all()
    )

    def _prefer(w):
        # resolved identities first, then lowest id, for stable/repeatable picks
        return (w.kind is None, w.id)

    # 1) exact normalized name (incl. group-parenthetical-stripped forms)
    exact = [
        w
        for w in candidates
        if {normalize(w.canonical_name), normalize(_pre_paren(w.canonical_name))}
        & target_keys
    ]
    if exact:
        return sorted(exact, key=_prefer)[0]

    # 2) strict token-subset only ("X" vs "X NEW"/"X <surname>"): the shorter
    # side must keep ≥2 distinctive tokens so a single shared word never folds
    # two different people together. No similarity scores — money doesn't guess.
    if len(target_tokens) < 1:
        return None
    subset_hits = []
    for w in candidates:
        wt = _token_set(_pre_paren(w.canonical_name))
        if not wt:
            continue
        if (target_tokens <= wt or wt <= target_tokens) and min(
            len(target_tokens), len(wt)
        ) >= _SUBSET_MIN_TOKENS:
            subset_hits.append(w)
    if len(subset_hits) == 1:
        return subset_hits[0]
    # 0 hits: unknown -> placeholder. >1 hits: ambiguous -> placeholder too.
    return None


def _get_or_create_account(
    session: Session,
    publisher: Publisher,
    parsed: ParsedStatementFilename,
    catalog: Catalog,
) -> BeneficiaryAccount:
    account = (
        session.query(BeneficiaryAccount)
        .filter(BeneficiaryAccount.account_code == parsed.account_code)
        .first()
    )
    if account is None:
        # New account. Before minting a placeholder writer, try to fold it into
        # an existing writer for the same person, so re-uploading a client's
        # statements doesn't create a duplicate roster entry. Matches exact
        # normalized names AND near-misses ("Swifty Blue" vs "Swifty Blue NEW",
        # "Bello Musical" vs "Bello Musical (Luna Negra)") via the SAME
        # normalize + token-set-Jaccard≥0.6 the client-import resolver uses, so
        # ingest and the resolution queue agree. Matched across the whole roster
        # (not scoped to publisher.id — the sort stage and importer resolve
        # slightly different default publisher names); house accounts only fold
        # into house accounts.
        writer = _match_existing_writer(session, parsed)
        if writer is None:
            writer = Writer(
                publisher_id=publisher.id,
                canonical_name=parsed.display_name,
                cadence=parsed.cadence,
                is_house_account=parsed.is_house,
            )
            session.add(writer)
            session.flush()
        account = BeneficiaryAccount(
            writer_id=writer.id,
            account_code=parsed.account_code,
            display_name=parsed.display_name,
            catalog=catalog,
            cadence=parsed.cadence,
            opened_period=parsed.period_code,
        )
        session.add(account)
        session.flush()
    elif not account.display_name:
        # backfill the immutable filename identity on accounts created before
        # the column existed
        account.display_name = parsed.display_name
        session.flush()
    return account


def sort_upload(upload_id: int, session: Session) -> Dict:
    """Sort an upload's incoming files into batches and paired statements.

    Idempotent: re-running on the same upload reuses existing batches,
    accounts and statements (refreshing file paths) and creates nothing new.
    Returns the sort stats dict also persisted on upload.stats["sort"].
    """
    upload = session.get(StatementUpload, upload_id)
    if upload is None:
        raise ValueError(f"statement_upload {upload_id} not found")

    upload.status = UploadStatus.SORTING
    # Commit the stage claim right away: sorting a full drop copies thousands
    # of files, and holding one giant write transaction through that starves
    # every API write ("database is locked" on new uploads).
    session.commit()

    # Statements created by a previous sort of THIS upload (re-run support).
    # An existing statement not in this set was ingested by another upload —
    # that's a duplicate to report, not a row to refresh.
    own_statement_ids = set(
        (upload.stats or {}).get("sort", {}).get("statement_ids", [])
    )

    publisher = _get_or_create_publisher(session)

    src_dir = incoming_dir(upload_id)
    filenames = sorted(os.listdir(src_dir)) if os.path.isdir(src_dir) else []

    unparseable = []
    duplicates = []
    replaced = []
    # (period_code, account_code) -> {"pdf": (filename, parsed), "xlsx": ...}
    groups = defaultdict(dict)

    for filename in filenames:
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".pdf", ".xlsx"):
            continue  # already flagged in stats.skipped at upload time
        parsed = parse_statement_filename(filename)
        if parsed is None:
            unparseable.append(filename)
            continue
        key = (parsed.period_code, parsed.account_code)
        if parsed.file_kind in groups[key]:
            # Second PDF (or XLSX) for the same (period, account) in this drop
            duplicates.append(filename)
            continue
        groups[key][parsed.file_kind] = (filename, parsed)

    batches_touched = set()
    statement_ids = []
    paired = 0
    unpaired = []

    for (period_code, account_code), files in sorted(groups.items()):
        # Prefer the royalty type written in either filename; fall back to
        # the account-code convention for paren-less names (PRD §2.3).
        parsed_any = (files.get("pdf") or files.get("xlsx"))[1]
        catalog = next(
            (p.royalty_type for _, p in files.values() if p.royalty_type is not None),
            None,
        ) or _infer_catalog_from_code(account_code)
        if catalog is None:
            unparseable.extend(name for name, _ in files.values())
            continue

        batch = _get_or_create_batch(
            session, upload, publisher, period_code, catalog, parsed_any
        )
        batches_touched.add(batch.id)
        account = _get_or_create_account(session, publisher, parsed_any, catalog)

        statement = (
            session.query(Statement)
            .filter(
                Statement.account_id == account.id,
                Statement.period_code == period_code,
            )
            .first()
        )
        # A statement for this (account, period) that THIS upload didn't create
        # was ingested by an earlier upload. Re-uploading its files is treated as
        # a CORRECTION: overwrite the old data and re-parse, rather than skipping
        # as a duplicate — so mistakes can be fixed by re-uploading the file.
        is_overwrite = statement is not None and statement.id not in own_statement_ids

        dest_dir = sorted_dir(period_code, catalog.name)
        os.makedirs(dest_dir, exist_ok=True)
        paths = {}
        for kind, (filename, _) in files.items():
            dest = os.path.join(dest_dir, filename)
            shutil.copy2(os.path.join(src_dir, filename), dest)
            # Stored relative to the storage root so the row stays valid on any
            # machine — an absolute path here 404s every download after a deploy.
            paths[kind] = to_storage_relative(dest)

        if statement is None:
            statement = Statement(
                batch_id=batch.id,
                account_id=account.id,
                period_code=period_code,
                parse_status=ParseStatus.PENDING,
            )
            session.add(statement)
            session.flush()
        elif is_overwrite:
            # Re-point at the (possibly new) batch and force a fresh parse so the
            # corrected numbers replace the old ones. Re-parsing deletes prior
            # StatementLine rows first, so no stale detail survives.
            statement.batch_id = batch.id
            statement.parse_status = ParseStatus.PENDING
            statement.parse_error = None
            statement.detail_sum = None
            statement.embedded_total = None
            statement.line_count = None
            replaced.extend(name for name, _ in files.values())
        statement.pdf_path = paths.get("pdf", statement.pdf_path)
        statement.xlsx_path = paths.get("xlsx", statement.xlsx_path)

        statement_ids.append(statement.id)
        if "pdf" in files and "xlsx" in files:
            paired += 1
        else:
            unpaired.extend(name for name, _ in files.values())

        # Release the write lock periodically — sort_upload is re-run-safe
        # (own_statement_ids), so chunked commits cost nothing and keep the API
        # responsive during a multi-thousand-file sort.
        if len(statement_ids) % 250 == 0:
            session.commit()

    sort_stats = {
        "batches": len(batches_touched),
        "statements": len(statement_ids),
        "statement_ids": statement_ids,
        "paired": paired,
        "unpaired": unpaired,
        "unparseable": unparseable,
        "duplicates": duplicates,
        "replaced": replaced,
    }
    # JSON column: reassign a new dict so SQLAlchemy sees the change
    stats = dict(upload.stats or {})
    stats["sort"] = sort_stats
    upload.stats = stats
    upload.status = UploadStatus.PARSING
    session.commit()

    logger.info(
        f"Sorted upload {upload_id}: {sort_stats['batches']} batches, "
        f"{sort_stats['statements']} statements ({paired} paired, "
        f"{len(unpaired)} unpaired files, {len(unparseable)} unparseable, "
        f"{len(replaced)} replaced, {len(duplicates)} duplicates)"
    )
    return sort_stats
