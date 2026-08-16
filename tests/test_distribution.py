"""Distribution gate, gated publish, cadence de-dup, supersede, unpublish."""

from decimal import Decimal

import pytest

from app.models.statements import (
    BatchStatus,
    BeneficiaryAccount,
    Catalog,
    Distribution,
    FindingScope,
    FindingSeverity,
    FindingStatus,
    ParseStatus,
    Publisher,
    Statement,
    StatementBatch,
    ValidationFinding,
    ValidationRun,
    Writer,
    WriterKind,
)
from app.services.distribution import publish as pub
from app.services.distribution.gate import compute_gate
from app.services.distribution.periods import covers, parse_period


# --- period math -------------------------------------------------------------

def test_period_coverage():
    assert covers("PUB25H2", "PUB25Q4")   # Jul-Dec covers Oct-Dec
    assert covers("PUB25H2", "PUB25Q3")
    assert covers("PUB26H1", "PUB26Q2")
    assert not covers("PUB25H2", "PUB25Q2")   # different half
    assert not covers("PUB25H2", "PUB26Q4")   # different year
    assert not covers("PUB25H2", "PUB25H2")   # equal is not "covers"
    assert parse_period("PUB25H2") == (2025, 7, 12)


# --- fixtures ----------------------------------------------------------------

def _pub(session):
    p = session.query(Publisher).first()
    if p is None:
        p = Publisher(name="Regalias Digitales")
        session.add(p)
        session.flush()
    return p


def _batch(session, period, catalog):
    b = StatementBatch(publisher_id=_pub(session).id, label=f"{catalog.value} {period}",
                       period_code=period, catalog=catalog, status=BatchStatus.APPROVED)
    session.add(b)
    session.flush()
    return b


def _writer(session, name, kind=WriterKind.CLIENT, house=False):
    w = Writer(publisher_id=_pub(session).id, canonical_name=name, kind=kind,
               is_house_account=house)
    session.add(w)
    session.flush()
    return w


def _stmt(session, batch, writer, code, period, payable="100.00",
          parse=ParseStatus.PARSED):
    acct = session.query(BeneficiaryAccount).filter(
        BeneficiaryAccount.account_code == code).first()
    if acct is None:
        acct = BeneficiaryAccount(writer_id=writer.id, account_code=code,
                                  catalog=batch.catalog)
        session.add(acct)
        session.flush()
    s = Statement(batch_id=batch.id, account_id=acct.id, period_code=period,
                  version=1, parse_status=parse, payable=Decimal(payable), line_count=3,
                  # a normal complete statement has both halves (PDF + XLSX)
                  pdf_path=f"{code}.pdf", xlsx_path=f"{code}.xlsx")
    session.add(s)
    session.flush()
    return s


def _blocker(session, batch):
    run = ValidationRun(batch_id=batch.id, blockers=1)
    session.add(run)
    session.flush()
    f = ValidationFinding(run_id=run.id, rule_id="V-STMT-RECON",
                          severity=FindingSeverity.BLOCKER, scope=FindingScope.BATCH,
                          message="recon mismatch", status=FindingStatus.OPEN)
    session.add(f)
    session.flush()
    return f


# --- gate --------------------------------------------------------------------

def test_gate_ignores_findings_and_placeholders(session):
    """Statement auditing is disabled: an open blocker finding and unmatched
    placeholders never block. The gate is ready as long as there's ≥1
    distributable (matched, non-house, non-offboarded) statement."""
    b = _batch(session, "PUB26H1", Catalog.YT)
    resolved = _writer(session, "RedZed")
    placeholder = _writer(session, "Placeholder Co", kind=None)
    house = _writer(session, "Regalias Digitales, LLC", kind=None, house=True)
    _stmt(session, b, resolved, "C00616", "PUB26H1")
    _stmt(session, b, placeholder, "C00999", "PUB26H1")
    _stmt(session, b, house, "CS0001", "PUB26H1")
    _blocker(session, b)  # a finding no longer gates anything
    session.commit()

    gate = compute_gate(session, b.id)
    assert gate["ready"] is True
    assert gate["open_blockers"] == 0
    assert gate["counts"]["house_excluded"] == 1
    assert gate["counts"]["distributable"] == 1  # only the resolved writer
    assert gate["reasons"] == []


# --- publish -----------------------------------------------------------------

def test_distribute_publishes_regardless_of_findings(session):
    b = _batch(session, "PUB26H1", Catalog.YT)
    w = _writer(session, "RedZed")
    _stmt(session, b, w, "C00616", "PUB26H1")
    _blocker(session, b)  # audit findings do not block distribution anymore
    session.commit()

    result = pub.distribute_batch(session, b.id)
    assert result["published"] == 1
    # idempotent
    again = pub.distribute_batch(session, b.id)
    assert again["published"] == 0 and again["already_distributed"] == 1
    assert session.get(StatementBatch, b.id).status == BatchStatus.DISTRIBUTED


def test_house_and_unresolved_never_distributed(session):
    b = _batch(session, "PUB26H1", Catalog.YT)
    house = _writer(session, "House", kind=None, house=True)
    placeholder = _writer(session, "Placeholder", kind=None)
    _stmt(session, b, house, "CS0001", "PUB26H1")
    _stmt(session, b, placeholder, "C00999", "PUB26H1")
    session.commit()
    # gate is not ready (placeholder unresolved); force-resolve nothing, expect refusal
    with pytest.raises(pub.GateNotReady):
        pub.distribute_batch(session, b.id)


def test_cadence_dedup_semiannual_supersedes_quarterly(session):
    w = _writer(session, "Luna Negra Sub")
    # Quarterly Q4 distributed first
    bq = _batch(session, "PUB25Q4", Catalog.YT)
    _stmt(session, bq, w, "C00139a", "PUB25Q4", payable="10.00")
    session.commit()
    r1 = pub.distribute_batch(session, bq.id)
    assert r1["published"] == 1

    # Semiannual H2 covers Q4 -> supersedes the quarterly, writer sees H2 only
    bh = _batch(session, "PUB25H2", Catalog.YT)
    _stmt(session, bh, w, "C00139a", "PUB25H2", payable="25.00")
    session.commit()
    r2 = pub.distribute_batch(session, bh.id)
    assert r2["published"] == 1 and r2["superseded"] == 1

    active = (session.query(Distribution)
              .filter(Distribution.writer_id == w.id,
                      Distribution.portal_visible.is_(True),
                      Distribution.superseded_by.is_(None)).all())
    assert len(active) == 1
    assert active[0].period_code == "PUB25H2"


def test_quarterly_skipped_when_covering_semiannual_already_active(session):
    w = _writer(session, "Luna Negra Sub2")
    bh = _batch(session, "PUB25H2", Catalog.YT)
    _stmt(session, bh, w, "C00139b", "PUB25H2")
    session.commit()
    pub.distribute_batch(session, bh.id)

    bq = _batch(session, "PUB25Q4", Catalog.YT)
    _stmt(session, bq, w, "C00139b", "PUB25Q4")
    session.commit()
    r = pub.distribute_batch(session, bq.id)
    assert r["published"] == 0 and r["skipped_cadence_dedup"] == 1


def test_unpublish_hides_but_keeps_row(session):
    b = _batch(session, "PUB26H1", Catalog.YT)
    w = _writer(session, "RedZed")
    _stmt(session, b, w, "C00616", "PUB26H1")
    session.commit()
    pub.distribute_batch(session, b.id)
    dist = session.query(Distribution).one()
    pub.unpublish(session, dist.id)
    assert session.get(Distribution, dist.id).portal_visible is False
    assert session.query(Distribution).count() == 1  # row kept


def test_two_accounts_same_catalog_and_period_both_publish(session):
    """Regression: a writer with several accounts in one catalog must not lose
    a statement to dedup. Real case — J. Stalin holds Mechanical CSJ024 AND
    JN0191; both issue an H1 2026 statement. Keyed only on (writer, catalog,
    period) the second looked like a re-ingest of the first and superseded it,
    hiding $8,535 from the writer with no error."""
    b = _batch(session, "PUB26H1", Catalog.MECH)
    w = _writer(session, "J. Stalin")
    _stmt(session, b, w, "CSJ024", "PUB26H1", payable="2388.21")
    _stmt(session, b, w, "JN0191", "PUB26H1", payable="8535.22")
    session.commit()

    result = pub.distribute_batch(session, b.id)
    assert result["published"] == 2
    assert result["superseded"] == 0
    assert result["skipped_cadence_dedup"] == 0

    visible = (session.query(Distribution)
               .filter(Distribution.writer_id == w.id,
                       Distribution.portal_visible.is_(True),
                       Distribution.superseded_by.is_(None)).all())
    assert len(visible) == 2, "both accounts' statements must reach the portal"
    paid = sum(session.get(Statement, d.statement_id).payable for d in visible)
    assert paid == Decimal("2388.21") + Decimal("8535.22")


def test_supersede_still_applies_within_one_account(session):
    """The rule dedup exists for: re-ingesting a correction of the SAME account
    must still replace the old distribution rather than duplicating it."""
    b1 = _batch(session, "PUB26H1", Catalog.MECH)
    w = _writer(session, "Solo Account Writer")
    _stmt(session, b1, w, "JN0500", "PUB26H1", payable="100.00")
    session.commit()
    assert pub.distribute_batch(session, b1.id)["published"] == 1

    # a corrected statement for the same account+period, in a new batch
    b2 = _batch(session, "PUB26H1", Catalog.MECH)
    b2.label = "Mechanical 2026H1 (corrected)"
    session.flush()
    acct = (session.query(BeneficiaryAccount)
            .filter(BeneficiaryAccount.account_code == "JN0500").one())
    corrected = Statement(
        batch_id=b2.id, account_id=acct.id, period_code="PUB26H1", version=2,
        parse_status=ParseStatus.PARSED, payable=Decimal("175.00"), line_count=3,
        pdf_path="JN0500v2.pdf", xlsx_path="JN0500v2.xlsx",
    )
    session.add(corrected)
    session.commit()

    result = pub.distribute_batch(session, b2.id)
    assert result["published"] == 1
    assert result["superseded"] == 1
    visible = (session.query(Distribution)
               .filter(Distribution.writer_id == w.id,
                       Distribution.portal_visible.is_(True),
                       Distribution.superseded_by.is_(None)).all())
    assert len(visible) == 1
    assert session.get(Statement, visible[0].statement_id).payable == Decimal("175.00")
