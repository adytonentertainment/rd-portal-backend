"""US-001: statement domain schema (PRD §5, Phase 1 tables only)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.models.statements import (
    AccountStatus,
    BeneficiaryAccount,
    Catalog,
    Cadence,
    FindingScope,
    FindingSeverity,
    FindingStatus,
    ParseStatus,
    Publisher,
    Statement,
    StatementBatch,
    StatementLine,
    StatementUpload,
    UploadStatus,
    ValidationFinding,
    ValidationRun,
    Writer,
    WriterAlias,
    ZeroPayReason,
)

EXPECTED_TABLES = {
    "publisher",
    "writer",
    "writer_alias",
    "beneficiary_account",
    "statement_upload",
    "statement_batch",
    "statement",
    "statement_line",
    "validation_run",
    "validation_finding",
    # identity layer (infra PRD §3.2, §7.2)
    "contact",
    "writer_contact",
    "client_import",
    "portal_invite",
    # distribution (ingestion PRD Stage C)
    "distribution",
}


def test_schema_creates_all_domain_tables(engine):
    tables = set(inspect(engine).get_table_names())
    assert EXPECTED_TABLES <= tables
    # not built yet (later phases add it)
    assert "account_ledger" not in tables


def test_statement_money_columns_are_numeric_14_6():
    money_cols = [
        "calculated",
        "recouped",
        "reserve_taken",
        "reserve_released",
        "carried_forward_in",
        "payable_prev",
        "settlement_paid",
        "payable",
        "detail_sum",
        "embedded_total",
    ]
    for name in money_cols:
        col = Statement.__table__.columns[name]
        assert col.type.precision == 14, name
        assert col.type.scale == 6, name


def _make_account(session, code="JN0261"):
    pub = Publisher(name="Regalias Digitales, LLC")
    session.add(pub)
    session.flush()
    writer = Writer(publisher_id=pub.id, canonical_name="Test Writer")
    session.add(writer)
    session.flush()
    account = BeneficiaryAccount(
        writer_id=writer.id, account_code=code, catalog=Catalog.MECH
    )
    session.add(account)
    session.flush()
    return pub, writer, account


def test_full_object_graph_round_trip(session):
    pub, writer, account = _make_account(session)

    session.add(WriterAlias(writer_id=writer.id, alias_name="Test Wrtier"))

    upload = StatementUpload(file_count=2, status=UploadStatus.UPLOADED, stats={"skipped": []})
    session.add(upload)
    session.flush()

    batch = StatementBatch(
        publisher_id=pub.id,
        label="Mechanical 2026H1",
        period_code="PUB26H1",
        catalog=Catalog.MECH,
        cadence=Cadence.SEMIANNUAL,
        upload_id=upload.id,
        stats={},
    )
    session.add(batch)
    session.flush()

    stmt = Statement(
        batch_id=batch.id,
        account_id=account.id,
        period_code="PUB26H1",
        statement_date=date(2026, 1, 31),
        calculated=Decimal("6663.270000"),
        payable=Decimal("45193.210000"),
        zero_pay_reason=None,
        parse_status=ParseStatus.PARSED,
    )
    session.add(stmt)
    session.flush()

    session.add(
        StatementLine(
            statement_id=stmt.id,
            row_no=1,
            song_title="118",
            earnings=Decimal("0.123456"),
        )
    )

    run = ValidationRun(batch_id=batch.id, blockers=1)
    session.add(run)
    session.flush()
    session.add(
        ValidationFinding(
            run_id=run.id,
            rule_id="V-FILE-1",
            severity=FindingSeverity.BLOCKER,
            scope=FindingScope.STATEMENT,
            scope_ref=str(stmt.id),
            message="XLSX missing its summary PDF",
            details={"account_code": "JN0261"},
        )
    )
    session.commit()

    fetched = session.get(Statement, stmt.id)
    assert fetched.payable == Decimal("45193.210000")
    assert fetched.batch.upload.id == upload.id
    assert fetched.account.account_code == "JN0261"
    assert fetched.lines[0].song_title == "118"

    finding = session.query(ValidationFinding).one()
    assert finding.status == FindingStatus.OPEN
    assert finding.waived_by is None and finding.waived_at is None


def test_account_code_unique(session):
    _make_account(session, code="C00650")
    _, writer2, _ = (None, None, None)
    pub2 = Publisher(name="Other Pub")
    session.add(pub2)
    session.flush()
    writer2 = Writer(publisher_id=pub2.id, canonical_name="Other Writer")
    session.add(writer2)
    session.flush()
    session.add(BeneficiaryAccount(writer_id=writer2.id, account_code="C00650"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_statement_unique_on_account_period_version(session):
    pub, writer, account = _make_account(session)
    batch = StatementBatch(
        publisher_id=pub.id,
        label="Mechanical 2026H1",
        period_code="PUB26H1",
        catalog=Catalog.MECH,
    )
    session.add(batch)
    session.flush()

    session.add(
        Statement(batch_id=batch.id, account_id=account.id, period_code="PUB26H1", version=1)
    )
    session.flush()
    session.add(
        Statement(batch_id=batch.id, account_id=account.id, period_code="PUB26H1", version=1)
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_zero_pay_reason_enum_values():
    assert {r.value for r in ZeroPayReason} == {
        "paid",
        "threshold_carryover",
        "recouped",
        "zero_earnings",
    }


def test_alembic_migration_exists_and_is_head():
    """Migration file exists for Postgres and is the single head of the chain."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    migrations_dir = Path(__file__).resolve().parents[1] / "migrations"
    cfg = Config(str(migrations_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(migrations_dir))
    script = ScriptDirectory.from_config(cfg)

    # Assert the SHAPE, not a specific revision: pinning the id here meant every
    # new migration failed this test for the wrong reason. What matters is that
    # the chain never forks — two heads is what actually breaks `upgrade head`.
    heads = script.get_heads()
    assert len(heads) == 1, f"chain must have exactly one head, found {heads}"

    # ingest worker lease chains onto roster membership
    lease = script.get_revision("v7w8x9y0z1a2")
    assert lease.down_revision == "u6v7w8x9y0z1"
    assert Path(lease.path).exists()

    # roster membership + account identity chain onto role/admin-approval
    roster = script.get_revision("u6v7w8x9y0z1")
    assert roster.down_revision == "t5u6v7w8x9y0"
    assert Path(roster.path).exists()

    # role/admin-approval columns chain onto distribution
    role_rev = script.get_revision("t5u6v7w8x9y0")
    assert role_rev.down_revision == "s4t5u6v7w8x9"
    assert Path(role_rev.path).exists()

    # distribution (ingestion PRD Stage C) chains onto portal_invite
    dist = script.get_revision("s4t5u6v7w8x9")
    assert dist.down_revision == "r3s4t5u6v7w8"
    assert Path(dist.path).exists()

    # portal_invite (infra PRD §7.2) chains onto the client identity layer
    inv = script.get_revision("r3s4t5u6v7w8")
    assert inv.down_revision == "q2r3s4t5u6v7"
    assert Path(inv.path).exists()

    # client identity layer (infra PRD §3.2) chains onto the statement domain
    rev = script.get_revision("q2r3s4t5u6v7")
    assert rev.down_revision == "p1q2r3s4t5u6"
    assert Path(rev.path).exists()

    prev = script.get_revision("p1q2r3s4t5u6")
    assert prev.down_revision == "o0p1q2r3s4t5"

    base = script.get_revision("n9o0p1q2r3s4")
    assert base.down_revision == "m8n9o0p1q2r3"


def test_migration_chain_covers_every_model_column():
    """Every column on the statement-domain models must be created by the
    migration chain — not by create_all() on a dev SQLite file. This is the
    guard for the class of bug where a column is added to a model (or by a raw
    ALTER on a dev database) and production Postgres never gets it."""
    import re

    migrations_dir = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    sql = "\n".join(p.read_text() for p in migrations_dir.glob("*.py"))

    from app.models.statements import (
        BeneficiaryAccount, Statement, StatementUpload, Writer,
    )

    for model in (Writer, BeneficiaryAccount, Statement, StatementUpload):
        table = model.__table__.name
        for column in model.__table__.columns:
            # the column name must appear somewhere in the chain (create_table
            # or add_column) — a cheap but effective drift check
            assert re.search(rf'["\']{re.escape(column.name)}["\']', sql), (
                f"{table}.{column.name} is in the model but in no migration — "
                "production Postgres would not have it"
            )
