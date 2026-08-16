import os
import shutil

import pytest

from app.models.statements import (
    BeneficiaryAccount,
    Catalog,
    ParseStatus,
    Statement,
    StatementBatch,
    StatementUpload,
    UploadStatus,
    Writer,
)
from app.services.statement_ingest.storage import resolve_stored_path
from app.services.statement_ingest.sorter import sort_upload
from app.services.statement_ingest.storage import incoming_dir

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "statements")

ALL_FIXTURE_FILES = sorted(
    f for f in os.listdir(FIXTURES_DIR) if f.endswith((".pdf", ".xlsx"))
)

# The 14 fixtures span exactly these (period, catalog) combos
EXPECTED_BATCHES = {
    ("PUB25H2", Catalog.MECH),
    ("PUB26H1", Catalog.MECH),
    ("PUB26H1", Catalog.YT),
    ("PUB25Q4", Catalog.YT),
    ("PUB26Q2", Catalog.YT),
}


@pytest.fixture()
def storage_root(tmp_path, monkeypatch):
    root = tmp_path / "statements-storage"
    monkeypatch.setenv("STATEMENTS_STORAGE_ROOT", str(root))
    return root


def make_upload(session, filenames, content_dir=FIXTURES_DIR):
    """Create an upload row with the given files staged in its incoming dir.

    Filenames not present in content_dir are created as dummy files — the
    sorter only reads names, never content.
    """
    upload = StatementUpload(file_count=len(filenames), status=UploadStatus.UPLOADED)
    session.add(upload)
    session.commit()
    dest = incoming_dir(upload.id)
    os.makedirs(dest, exist_ok=True)
    for name in filenames:
        src = os.path.join(content_dir, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, name))
        else:
            with open(os.path.join(dest, name), "wb") as f:
                f.write(b"dummy")
    return upload


def test_sort_all_fixtures_integration(session, storage_root):
    upload = make_upload(session, ALL_FIXTURE_FILES)

    stats = sort_upload(upload.id, session)

    assert stats["batches"] == 5
    assert stats["statements"] == 7
    assert stats["paired"] == 7
    assert stats["unpaired"] == []
    assert stats["unparseable"] == []
    assert stats["duplicates"] == []

    batches = session.query(StatementBatch).all()
    assert {(b.period_code, b.catalog) for b in batches} == EXPECTED_BATCHES

    statements = session.query(Statement).all()
    assert len(statements) == 7
    for stmt in statements:
        # Paths are stored RELATIVE to the storage root so the same row works on
        # a laptop and on a deployed disk; resolve_stored_path is how production
        # turns one back into a file.
        assert stmt.pdf_path and not os.path.isabs(stmt.pdf_path)
        assert stmt.xlsx_path and not os.path.isabs(stmt.xlsx_path)
        assert os.path.exists(resolve_stored_path(stmt.pdf_path))
        assert os.path.exists(resolve_stored_path(stmt.xlsx_path))
        assert stmt.parse_status == ParseStatus.PENDING

    # Files land under {root}/{period}/{catalog}/
    csj002 = (
        session.query(Statement)
        .join(BeneficiaryAccount)
        .filter(BeneficiaryAccount.account_code == "CSJ002")
        .one()
    )
    # The stored value is root-relative; the file lands under {root}/{period}/{catalog}/
    assert csj002.pdf_path.startswith(os.path.join("PUB25H2", "MECH"))
    assert resolve_stored_path(csj002.pdf_path).startswith(
        str(storage_root / "PUB25H2" / "MECH")
    )

    # Upload stats persisted and status advanced past sorting
    session.refresh(upload)
    assert upload.stats["sort"]["statements"] == 7
    assert upload.status == UploadStatus.PARSING


def test_sort_is_idempotent(session, storage_root):
    upload = make_upload(session, ALL_FIXTURE_FILES)

    first = sort_upload(upload.id, session)
    second = sort_upload(upload.id, session)

    assert second == first
    assert session.query(StatementBatch).count() == 5
    assert session.query(Statement).count() == 7
    assert session.query(BeneficiaryAccount).count() == 7
    assert session.query(Writer).count() == 7


def test_pairing_keys_on_period_and_account_not_display_name(session, storage_root):
    # The JN0080 pair has drifted display names ('Kill Bill- The Rapper' pdf
    # vs 'Kill Bill The Rapper' xlsx) — must still pair.
    pair = [f for f in ALL_FIXTURE_FILES if "JN0080" in f]
    assert len(pair) == 2
    upload = make_upload(session, pair)

    stats = sort_upload(upload.id, session)

    assert stats["statements"] == 1
    assert stats["paired"] == 1
    stmt = session.query(Statement).one()
    assert stmt.pdf_path and stmt.xlsx_path


def test_auto_creates_account_and_placeholder_writer(session, storage_root):
    upload = make_upload(session, ALL_FIXTURE_FILES)
    sort_upload(upload.id, session)

    account = (
        session.query(BeneficiaryAccount)
        .filter(BeneficiaryAccount.account_code == "C00739-New")
        .one()
    )
    assert account.catalog == Catalog.YT
    assert account.writer.canonical_name == "Swifty Blue NEW"
    assert account.writer.is_house_account is False


def _seed_writer(session, name, kind=None):
    from app.models.statements import Publisher, WriterKind

    pub = session.query(Publisher).first() or Publisher(name="Regalias Digitales")
    if pub.id is None:
        session.add(pub)
        session.flush()
    w = Writer(publisher_id=pub.id, canonical_name=name,
               kind=WriterKind.CLIENT if kind == "client" else None)
    session.add(w)
    session.commit()
    return w


def test_ingest_folds_into_existing_writer_exact_name(session, storage_root):
    """An account whose statement name exactly matches an existing writer folds
    into that writer instead of creating a duplicate roster entry."""
    existing = _seed_writer(session, "Swifty Blue NEW", kind="client")
    upload = make_upload(
        session,
        ["Ben_PUB26H1_C00739-New_Swifty Blue NEW (YouTube Publishing).xlsx"],
    )
    sort_upload(upload.id, session)

    account = (
        session.query(BeneficiaryAccount)
        .filter(BeneficiaryAccount.account_code == "C00739-New")
        .one()
    )
    assert account.writer_id == existing.id
    # no duplicate created
    assert session.query(Writer).filter(Writer.canonical_name == "Swifty Blue NEW").count() == 1


def test_ingest_folds_near_miss_into_existing_writer(session, storage_root):
    """A near-miss ("Swifty Blue" already on the roster, statement says "Swifty
    Blue NEW") folds in via the same fuzzy bar the resolver uses."""
    existing = _seed_writer(session, "Swifty Blue", kind="client")
    upload = make_upload(
        session,
        ["Ben_PUB26H1_C00739-New_Swifty Blue NEW (YouTube Publishing).xlsx"],
    )
    sort_upload(upload.id, session)

    account = (
        session.query(BeneficiaryAccount)
        .filter(BeneficiaryAccount.account_code == "C00739-New")
        .one()
    )
    assert account.writer_id == existing.id
    # no separate "Swifty Blue NEW" placeholder was minted
    assert session.query(Writer).filter(Writer.canonical_name == "Swifty Blue NEW").count() == 0


def test_ingest_does_not_fold_unrelated_name(session, storage_root):
    """A genuinely different artist is NOT folded into an unrelated writer."""
    _seed_writer(session, "Totally Different Artist", kind="client")
    upload = make_upload(
        session,
        ["Ben_PUB26H1_C00739-New_Swifty Blue NEW (YouTube Publishing).xlsx"],
    )
    sort_upload(upload.id, session)

    account = (
        session.query(BeneficiaryAccount)
        .filter(BeneficiaryAccount.account_code == "C00739-New")
        .one()
    )
    assert account.writer.canonical_name == "Swifty Blue NEW"  # new placeholder


def test_house_codes_are_ordinary_accounts(session, storage_root):
    upload = make_upload(
        session, ["Ben_PUB25H2_CPJ001 - Regalias Digitales (Performance Royalties).pdf"]
    )
    stats = sort_upload(upload.id, session)

    assert stats["statements"] == 1
    account = (
        session.query(BeneficiaryAccount)
        .filter(BeneficiaryAccount.account_code == "CPJ001")
        .one()
    )
    assert account.writer.is_house_account is False  # house special-casing disabled
    assert account.catalog == Catalog.PERF


def test_unpaired_and_unparseable_recorded_not_raised(session, storage_root):
    files = [
        "Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf",  # no xlsx
        "garbage filename.pdf",
        "Ben_PUB26H1_JN0249 - OMB Peezy (Mechanical Royalties).pdf",
        "Ben_PUB26H1_JN0249_OMB Peezy (Mechanical Royalties).xlsx",
    ]
    upload = make_upload(session, files)

    stats = sort_upload(upload.id, session)

    assert stats["unparseable"] == ["garbage filename.pdf"]
    assert stats["unpaired"] == [files[0]]
    assert stats["statements"] == 2  # unpaired statement still created
    assert stats["paired"] == 1

    csj002 = (
        session.query(Statement)
        .join(BeneficiaryAccount)
        .filter(BeneficiaryAccount.account_code == "CSJ002")
        .one()
    )
    assert csj002.pdf_path is not None
    assert csj002.xlsx_path is None


def test_catalog_inferred_from_account_code_when_no_parens(session, storage_root):
    # PRD §2.3 example: no royalty-type parens; C##### convention -> YouTube
    upload = make_upload(session, ["Ben_PUB25H2_C00001b - AkwidAfterVydia.pdf"])
    stats = sort_upload(upload.id, session)

    assert stats["statements"] == 1
    batch = session.query(StatementBatch).one()
    assert batch.catalog == Catalog.YT
    assert batch.period_code == "PUB25H2"


def test_reupload_replaces_already_ingested_statement(session, storage_root):
    """Re-uploading files for an already-ingested (account, period) is a
    CORRECTION: it overwrites the existing statement and re-queues it for
    parsing, rather than being skipped as a duplicate."""
    pair = [f for f in ALL_FIXTURE_FILES if "CSJ002" in f]
    first_upload = make_upload(session, pair)
    sort_upload(first_upload.id, session)
    stmt = session.query(Statement).one()
    stmt.parse_status = ParseStatus.PARSED  # simulate the worker having parsed it
    session.flush()

    second_upload = make_upload(session, pair)
    stats = sort_upload(second_upload.id, session)

    assert stats["duplicates"] == []
    assert sorted(stats["replaced"]) == sorted(pair)
    assert stats["statements"] == 1  # the replaced statement, re-queued
    assert session.query(Statement).count() == 1  # overwritten, not duplicated
    # reset to PENDING so the worker re-parses the corrected file
    assert session.query(Statement).one().parse_status == ParseStatus.PENDING


def test_duplicate_file_kind_within_upload_recorded(session, storage_root):
    # Two PDFs keying to the same (period, account) in one drop: the second
    # parses identically, so it must be flagged, not silently dropped.
    upload = make_upload(
        session,
        [
            "Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf",
            "Ben_PUB25H2_CSJ002_Javier Solis (Mechanical Royalties).xlsx",
        ],
    )
    # Stage an extra PDF for the same account under a drifted display name
    extra = "Ben_PUB25H2_CSJ002 - Javier Solis Duplicate (Mechanical Royalties).pdf"
    with open(os.path.join(incoming_dir(upload.id), extra), "wb") as f:
        f.write(b"dummy")

    stats = sort_upload(upload.id, session)

    assert stats["duplicates"] == [extra]
    assert stats["statements"] == 1
    assert stats["paired"] == 1


def test_batch_not_duplicated_across_uploads(session, storage_root):
    first = make_upload(
        session, ["Ben_PUB26H1_JN0249 - OMB Peezy (Mechanical Royalties).pdf"]
    )
    sort_upload(first.id, session)
    second = make_upload(
        session, ["Ben_PUB26H1_JN0080 - Kill Bill- The Rapper (Mechanical Royalties).pdf"]
    )
    sort_upload(second.id, session)

    batches = session.query(StatementBatch).all()
    assert len(batches) == 1
    assert batches[0].period_code == "PUB26H1"
    assert batches[0].catalog == Catalog.MECH
    assert batches[0].label == "Mechanical 2026H1"
    # Both statements attached to the one batch
    assert session.query(Statement).count() == 2


def test_sort_unknown_upload_raises(session, storage_root):
    with pytest.raises(ValueError):
        sort_upload(99999, session)
