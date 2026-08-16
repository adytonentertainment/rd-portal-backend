"""US-008: validation engine + V-FILE file-integrity rules."""

import json
import os
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    BatchStatus,
    BeneficiaryAccount,
    Catalog,
    FindingScope,
    FindingSeverity,
    FindingStatus,
    ParseStatus,
    Publisher,
    Statement,
    StatementBatch,
    StatementUpload,
    UploadStatus,
    ValidationFinding,
    ValidationRun,
    Writer,
)
from app.routers.auth import get_user
from app.routers.statements_admin import statements_admin_router
from app.services.validation.engine import REGISTRY, Rule, register, run_validation

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "statements")
ADMIN_EMAIL = "admin@verax.app"


# ------------------------------------------------------------------- builders


class World:
    """A publisher + batch + per-account statements, built directly in the DB
    — V-FILE rules only read DB state + upload sort stats, never files."""

    def __init__(self, session, period_code="PUB25H2", catalog=Catalog.MECH):
        self.session = session
        self.publisher = Publisher(name="Test Publisher")
        session.add(self.publisher)
        session.flush()
        self.upload = StatementUpload(file_count=0, status=UploadStatus.DONE, stats={})
        session.add(self.upload)
        session.flush()
        self.batch = StatementBatch(
            publisher_id=self.publisher.id,
            label="Mechanical 2025H2",
            period_code=period_code,
            catalog=catalog,
            upload_id=self.upload.id,
        )
        session.add(self.batch)
        session.flush()

    def set_sort_stats(self, **kwargs):
        stats = dict(self.upload.stats or {})
        sort = dict(stats.get("sort", {}))
        sort.update(kwargs)
        stats["sort"] = sort
        self.upload.stats = stats
        self.session.flush()

    def add_statement(self, account_code, **overrides):
        writer = Writer(publisher_id=self.publisher.id, canonical_name=account_code)
        self.session.add(writer)
        self.session.flush()
        account = BeneficiaryAccount(
            writer_id=writer.id,
            account_code=account_code,
            catalog=self.batch.catalog,
            pdf_only=overrides.pop("pdf_only", False),
            xlsx_only=overrides.pop("xlsx_only", False),
        )
        self.session.add(account)
        self.session.flush()
        # Defaults describe a fully clean, parsed, paired statement
        values = dict(
            batch_id=self.batch.id,
            account_id=account.id,
            period_code=self.batch.period_code,
            pdf_path=f"/sorted/{account_code}.pdf",
            xlsx_path=f"/sorted/{account_code}.xlsx",
            parse_status=ParseStatus.PARSED,
            calculated=Decimal("100"),
            payable=Decimal("100"),
            detail_sum=Decimal("100"),
            embedded_total=Decimal("100"),
            line_count=3,
        )
        values.update(overrides)
        statement = Statement(**values)
        self.session.add(statement)
        self.session.flush()
        return statement


def findings_for(session, run, rule_id=None):
    query = session.query(ValidationFinding).filter(ValidationFinding.run_id == run.id)
    if rule_id:
        query = query.filter(ValidationFinding.rule_id == rule_id)
    return query.order_by(ValidationFinding.id).all()


# ------------------------------------------------------------------- registry


def test_registry_holds_all_v_file_rules_with_prd_severities():
    """Severity is data in the registry (PRD §7.2 table), not code branches."""
    expected = {
        "V-FILE-1": FindingSeverity.BLOCKER,
        "V-FILE-2": FindingSeverity.BLOCKER,
        "V-FILE-3": FindingSeverity.BLOCKER,
        "V-FILE-4": FindingSeverity.WARNING,
        "V-FILE-5": FindingSeverity.BLOCKER,
        "V-FILE-6": FindingSeverity.BLOCKER,
    }
    for rule_id, severity in expected.items():
        rule = REGISTRY[rule_id]
        assert rule.default_severity == severity
        assert rule.level == 1
        assert callable(rule.check)


def test_registering_duplicate_rule_id_raises():
    rule = REGISTRY["V-FILE-1"]
    with pytest.raises(ValueError):
        register(
            Rule(
                id="V-FILE-1",
                level=1,
                default_severity=FindingSeverity.INFO,
                scope=FindingScope.BATCH,
                description="dup",
                check=rule.check,
            )
        )


def test_run_validation_unknown_batch_raises(session):
    with pytest.raises(ValueError):
        run_validation(99999, session)


# ----------------------------------------------------------------- rule checks


def test_missing_xlsx_yields_v_file_1_blocker(session):
    world = World(session)
    statement = world.add_statement("JN0001", xlsx_path=None, detail_sum=None,
                                    embedded_total=None, line_count=None)
    run = run_validation(world.batch.id, session)

    findings = findings_for(session, run, "V-FILE-1")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == FindingSeverity.BLOCKER
    assert finding.scope == FindingScope.STATEMENT
    assert finding.scope_ref == f"statement:{statement.id}"
    assert finding.status == FindingStatus.OPEN
    assert finding.details["missing"] == "xlsx"
    assert run.blockers == 1


def test_missing_pdf_yields_v_file_1_blocker(session):
    world = World(session)
    world.add_statement("JN0002", pdf_path=None, calculated=None, payable=None)
    run = run_validation(world.batch.id, session)
    findings = findings_for(session, run, "V-FILE-1")
    assert len(findings) == 1
    assert findings[0].details["missing"] == "pdf"


def test_pdf_only_and_xlsx_only_accounts_are_exempt_from_v_file_1(session):
    """House accounts (CPJ001, CS0001) legitimately ship one file kind."""
    world = World(session)
    world.add_statement("CPJ001", pdf_only=True, xlsx_path=None, detail_sum=None,
                        embedded_total=None, line_count=None)
    world.add_statement("CS0001", xlsx_only=True, pdf_path=None,
                        calculated=None, payable=None)
    run = run_validation(world.batch.id, session)
    assert findings_for(session, run, "V-FILE-1") == []


def test_unparseable_filename_yields_v_file_2(session):
    world = World(session)
    world.add_statement("JN0003")
    world.set_sort_stats(unparseable=["random_garbage.pdf"])
    run = run_validation(world.batch.id, session)

    findings = findings_for(session, run, "V-FILE-2")
    assert len(findings) == 1
    finding = findings[0]
    assert finding.severity == FindingSeverity.BLOCKER
    assert finding.scope == FindingScope.BATCH
    assert finding.scope_ref == "file:random_garbage.pdf"


def test_statement_period_mismatch_yields_v_file_2(session):
    world = World(session)
    statement = world.add_statement("JN0004", period_code="PUB26H1")
    run = run_validation(world.batch.id, session)
    findings = findings_for(session, run, "V-FILE-2")
    assert len(findings) == 1
    assert findings[0].scope_ref == f"statement:{statement.id}"


def test_failed_parse_with_xlsx_yields_v_file_3(session):
    world = World(session)
    world.add_statement(
        "JN0005",
        parse_status=ParseStatus.FAILED,
        parse_error="No header row starting with 'Period' found",
        detail_sum=None, embedded_total=None, line_count=None,
        calculated=None, payable=None,
    )
    run = run_validation(world.batch.id, session)
    findings = findings_for(session, run, "V-FILE-3")
    assert len(findings) == 1
    assert findings[0].severity == FindingSeverity.BLOCKER
    assert "Period" in findings[0].details["error"]
    # the failed pair is V-FILE-3's, not V-FILE-5's (error detail lives there)
    assert findings_for(session, run, "V-FILE-5") == []


def test_missing_grand_total_yields_v_file_4_warning(session):
    world = World(session)
    world.add_statement("JN0006", embedded_total=None)
    run = run_validation(world.batch.id, session)
    findings = findings_for(session, run, "V-FILE-4")
    assert len(findings) == 1
    assert findings[0].severity == FindingSeverity.WARNING
    assert run.warnings == 1
    assert run.blockers == 0


def test_pdf_summary_incomplete_yields_v_file_5(session):
    world = World(session)
    world.add_statement("JN0007", payable=None)
    run = run_validation(world.batch.id, session)
    findings = findings_for(session, run, "V-FILE-5")
    assert len(findings) == 1
    assert findings[0].details["missing_fields"] == ["payable"]


def test_failed_pdf_only_statement_yields_v_file_5(session):
    world = World(session)
    world.add_statement(
        "JN0008", xlsx_path=None, parse_status=ParseStatus.FAILED,
        parse_error="boom", calculated=None, payable=None,
        detail_sum=None, embedded_total=None, line_count=None, pdf_only=True,
    )
    run = run_validation(world.batch.id, session)
    assert len(findings_for(session, run, "V-FILE-5")) == 1
    assert findings_for(session, run, "V-FILE-3") == []


def test_duplicate_file_yields_v_file_6_on_matching_batch_only(session):
    world = World(session)  # PUB25H2 / MECH
    world.add_statement("CSJ002")
    world.set_sort_stats(
        duplicates=[
            "Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf",
            # different period -> belongs to some other batch, not this one
            "Ben_PUB26H1_C00650 - Nelly Banks (YouTube Publishing).pdf",
        ]
    )
    run = run_validation(world.batch.id, session)
    findings = findings_for(session, run, "V-FILE-6")
    assert len(findings) == 1
    assert findings[0].severity == FindingSeverity.BLOCKER
    assert findings[0].scope == FindingScope.BATCH
    assert "CSJ002" in findings[0].message


def test_clean_batch_yields_zero_findings_and_approved_status(session):
    world = World(session)
    world.add_statement("JN0009")
    world.add_statement("JN0010")
    run = run_validation(world.batch.id, session)
    assert findings_for(session, run) == []
    assert (run.blockers, run.warnings, run.infos) == (0, 0, 0)
    assert run.finished_at is not None
    assert world.batch.status == BatchStatus.APPROVED


def test_findings_flip_batch_to_needs_review(session):
    world = World(session)
    world.add_statement("JN0011", xlsx_path=None, detail_sum=None,
                        embedded_total=None, line_count=None)
    run_validation(world.batch.id, session)
    assert world.batch.status == BatchStatus.NEEDS_REVIEW


# ------------------------------------------------- stable identity across runs


def test_rerun_reattaches_same_finding_row_and_preserves_waiver(session):
    world = World(session)
    world.add_statement("JN0012", xlsx_path=None, detail_sum=None,
                        embedded_total=None, line_count=None)
    first = run_validation(world.batch.id, session)
    finding = findings_for(session, first, "V-FILE-1")[0]

    finding.status = FindingStatus.WAIVED
    finding.waived_reason = "publisher confirmed XLSX lost upstream"
    session.commit()

    second = run_validation(world.batch.id, session)
    refound = findings_for(session, second, "V-FILE-1")
    assert len(refound) == 1
    assert refound[0].id == finding.id  # same row, re-attached — not a copy
    assert refound[0].status == FindingStatus.WAIVED
    assert refound[0].waived_reason == "publisher confirmed XLSX lost upstream"
    # waived findings don't count against the gate
    assert second.blockers == 0
    # total finding rows did not grow
    assert session.query(ValidationFinding).count() == 1


def test_rerun_resolves_findings_that_no_longer_apply_and_reopens_regressions(session):
    world = World(session)
    statement = world.add_statement("JN0013", xlsx_path=None, detail_sum=None,
                                    embedded_total=None, line_count=None)
    first = run_validation(world.batch.id, session)
    finding = findings_for(session, first, "V-FILE-1")[0]

    # fix the data -> finding closes (kept on its last run for the audit trail)
    statement.xlsx_path = "/sorted/JN0013.xlsx"
    statement.detail_sum = Decimal("100")
    statement.embedded_total = Decimal("100")
    statement.line_count = 3
    session.commit()
    second = run_validation(world.batch.id, session)
    session.refresh(finding)
    assert finding.status == FindingStatus.RESOLVED
    assert finding.run_id == first.id
    assert findings_for(session, second) == []
    assert second.blockers == 0

    # regression -> the SAME row reopens (identity = rule_id + scope_ref)
    statement.xlsx_path = None
    session.commit()
    third = run_validation(world.batch.id, session)
    session.refresh(finding)
    assert finding.status == FindingStatus.OPEN
    assert finding.run_id == third.id
    assert session.query(ValidationFinding).count() == 1


def test_each_run_creates_its_own_validation_run_row(session):
    world = World(session)
    world.add_statement("JN0014")
    run_validation(world.batch.id, session)
    run_validation(world.batch.id, session)
    runs = session.query(ValidationRun).filter(
        ValidationRun.batch_id == world.batch.id
    ).all()
    assert len(runs) == 2


# ------------------------------------------------------------ revalidate route


@pytest.fixture()
def client(session, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    admin = User(email=ADMIN_EMAIL, username="statements-admin", royalty_per_stream=0)
    session.add(admin)
    session.commit()
    app = FastAPI()
    app.include_router(statements_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin
    return TestClient(app)


def test_revalidate_endpoint_runs_and_reports(client, session):
    world = World(session)
    world.add_statement("JN0015", xlsx_path=None, detail_sum=None,
                        embedded_total=None, line_count=None)
    response = client.post(f"/admin/statements/batches/{world.batch.id}/revalidate")
    assert response.status_code == 200
    body = response.json()
    assert body["batch_id"] == world.batch.id
    assert body["blockers"] == 1
    assert body["warnings"] == 0
    assert body["batch_status"] == "needs_review"
    assert session.get(ValidationRun, body["run_id"]) is not None


def test_revalidate_unknown_batch_404(client):
    assert client.post("/admin/statements/batches/99999/revalidate").status_code == 404


# ----------------------------------------- real pipeline: clean fixture batches


def test_full_fixture_pipeline_yields_zero_v_file_findings(client, session, tmp_path, monkeypatch):
    """All 14 real fixtures sort/parse/validate clean: 5 batches, 5 runs,
    0 findings of any V-FILE rule, every batch approved-eligible."""
    monkeypatch.setenv("STATEMENTS_STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("INGEST_INLINE", "1")

    parts = []
    for name in sorted(os.listdir(FIXTURES_DIR)):
        if os.path.splitext(name)[1] not in (".pdf", ".xlsx"):
            continue
        with open(os.path.join(FIXTURES_DIR, name), "rb") as f:
            parts.append(("files", (name, f.read())))
    assert len(parts) == 14

    response = client.post("/admin/statements/uploads", files=parts)
    assert response.status_code == 202
    assert response.json()["status"] == "done"

    status = client.get(
        f"/admin/statements/uploads/{response.json()['upload_id']}"
    ).json()
    # Statement auditing is intentionally disabled: the validate stage is a
    # no-op that marks the upload DONE without producing runs or findings.
    assert status["stats"]["validate"] == {
        "runs": 0,
        "blockers": 0,
        "warnings": 0,
        "infos": 0,
        "skipped": True,
    }
    assert session.query(ValidationFinding).count() == 0
    assert session.query(ValidationRun).count() == 0
