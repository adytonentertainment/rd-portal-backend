"""GET /admin/statements/uploads/{id}/failures — WHICH files went wrong, and why.

The counts were always on screen ("3 failed"); the reasons were written to the
database and the server log and never read back. These tests pin the two things
that made the old panel useless:

  * a sort-stage rejection has no Statement row, so a statement-only report
    misses it entirely — including the worst case, a drop where every filename
    is malformed, which produced zero statements AND zero parse failures and
    reported itself as a clean run;
  * ownership resolves through `stats.sort.statement_ids`, not
    `batch.upload_id`, because batches are reused across uploads — the wrong
    key reports a previous upload's failures as this one's.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    BeneficiaryAccount,
    Catalog,
    ParseStatus,
    Publisher,
    Statement,
    StatementBatch,
    StatementUpload,
    Writer,
)
from app.routers.auth import get_user
from app.routers.statements_admin import statements_admin_router

ADMIN_EMAIL = "admin@verax.app"


@pytest.fixture()
def admin_user(session):
    u = User(email=ADMIN_EMAIL, username="admin", royalty_per_stream=0)
    session.add(u)
    session.commit()
    return u


@pytest.fixture()
def client(session, admin_user, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    app = FastAPI()
    app.include_router(statements_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


def _seed(session, *, sort_stats=None, upload_error=None):
    """One upload, one writer, two accounts, two statements (both FAILED)."""
    pub = Publisher(name="Regalias Digitales")
    session.add(pub)
    session.flush()

    up = StatementUpload(file_count=4)
    session.add(up)
    session.flush()

    b_yt = StatementBatch(publisher_id=pub.id, label="YT 2025H2", period_code="PUB25H2",
                          catalog=Catalog.YT, upload_id=up.id)
    b_me = StatementBatch(publisher_id=pub.id, label="MECH 2025H2", period_code="PUB25H2",
                          catalog=Catalog.MECH, upload_id=up.id)
    session.add_all([b_yt, b_me])
    session.flush()

    w = Writer(publisher_id=pub.id, canonical_name="RedZed")
    session.add(w)
    session.flush()

    a_yt = BeneficiaryAccount(writer_id=w.id, account_code="C00616", catalog=Catalog.YT)
    a_me = BeneficiaryAccount(writer_id=w.id, account_code="JN0232", catalog=Catalog.MECH)
    session.add_all([a_yt, a_me])
    session.flush()

    s1 = Statement(
        batch_id=b_yt.id, account_id=a_yt.id, period_code="PUB25H2", version=1,
        parse_status=ParseStatus.FAILED,
        xlsx_path="incoming/1/Ben_PUB25H2_C00616.xlsx",
        parse_error="ValueError: No header row starting with 'Period' found in x.xlsx",
    )
    s2 = Statement(
        batch_id=b_me.id, account_id=a_me.id, period_code="PUB25H2", version=1,
        parse_status=ParseStatus.FAILED,
        xlsx_path="incoming/1/Ben_PUB25H2_JN0232.xlsx",
        parse_error="worker crashed: killed by signal 9",
    )
    session.add_all([s1, s2])
    session.flush()

    stats = {"sort": dict(sort_stats or {})}
    stats["sort"].setdefault("statement_ids", [s1.id, s2.id])
    if upload_error:
        stats["error"] = upload_error
    up.stats = stats
    session.commit()
    return up.id


def test_parse_failures_are_named_and_explained(session, client):
    """Each failure identifies the account, client and period — and says what
    to do. Previously all of this existed only as the integer 2."""
    upload_id = _seed(session)
    r = client.get(f"/admin/statements/uploads/{upload_id}/failures")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["counts"]["blockers"] == 2
    by_account = {i["account_code"]: i for i in body["items"] if i["account_code"]}
    assert set(by_account) == {"C00616", "JN0232"}

    xlsx = by_account["C00616"]
    assert xlsx["writer_name"] == "RedZed"
    assert xlsx["period_code"] == "PUB25H2"
    assert xlsx["stage"] == "parse"
    assert xlsx["reason"] == "The XLSX has no recognisable detail sheet"
    assert "Re-export" in xlsx["hint"]
    # the raw exception survives alongside the sentence, for forwarding on
    assert "No header row" in xlsx["raw"]
    # the admin is told which file to go and fix, by name not by stored path
    assert xlsx["file"] == "Ben_PUB25H2_C00616.xlsx"

    assert by_account["JN0232"]["reason"] == "The parser process died on this statement"


def test_sort_rejections_appear_even_with_no_statement_row(session, client):
    """The class of problem the old panel could not represent at all: files the
    sorter threw out never become statements, so no parse counter sees them."""
    upload_id = _seed(session, sort_stats={
        "unparseable": ["random notes.pdf"],
        "unpaired": ["Ben_PUB25H2_C00616.xlsx"],
        "duplicates": ["Ben_PUB25H2_C00616 (1).pdf"],
    })
    body = client.get(f"/admin/statements/uploads/{upload_id}/failures").json()

    sort_items = {i["file"]: i for i in body["items"] if i["stage"] == "sort"}
    assert set(sort_items) == {
        "random notes.pdf",
        "Ben_PUB25H2_C00616.xlsx",
        "Ben_PUB25H2_C00616 (1).pdf",
    }
    # A name the sorter can't read loses the file outright -> blocker.
    assert sort_items["random notes.pdf"]["severity"] == "blocker"
    assert "naming convention" in sort_items["random notes.pdf"]["reason"]
    # A half-arrived or duplicate statement still ingested something -> warning.
    assert sort_items["Ben_PUB25H2_C00616.xlsx"]["severity"] == "warning"
    assert sort_items["Ben_PUB25H2_C00616 (1).pdf"]["severity"] == "warning"

    # Blockers sort ahead of warnings: a long warning tail must not bury the
    # files that actually failed.
    severities = [i["severity"] for i in body["items"]]
    assert severities == sorted(severities, key=lambda s: {"blocker": 0, "warning": 1}[s])


def test_upload_level_error_is_reported(session, client):
    """A stage that raised kills the whole upload. That error was shown as a
    bare string with no indication of what to do about it."""
    upload_id = _seed(session, upload_error="parsing: OperationalError: timeout")
    body = client.get(f"/admin/statements/uploads/{upload_id}/failures").json()
    top = [i for i in body["items"] if i["stage"] == "parsing"]
    assert len(top) == 1
    assert "The upload itself failed" in top[0]["reason"]
    assert "Re-run the ingest" in top[0]["hint"]


def test_failures_do_not_leak_across_uploads(session, client):
    """Batches are REUSED across uploads, so `batch.upload_id` names whichever
    upload created the batch first. Ownership must come from the sort stats, or
    a clean re-upload inherits the previous drop's failures."""
    first = _seed(session)

    # A second upload that reuses the same batches and produced nothing bad.
    second = StatementUpload(file_count=0, stats={"sort": {"statement_ids": []}})
    session.add(second)
    session.commit()

    body = client.get(f"/admin/statements/uploads/{second.id}/failures").json()
    assert body["counts"]["total"] == 0
    assert body["items"] == []
    # ...while the upload that really failed still reports both.
    assert client.get(
        f"/admin/statements/uploads/{first}/failures"
    ).json()["counts"]["blockers"] == 2


def test_csv_download_carries_every_column(session, client):
    upload_id = _seed(session, sort_stats={"unparseable": ["random notes.pdf"]})
    r = client.get(f"/admin/statements/uploads/{upload_id}/failures.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert f"upload-{upload_id}-failures.csv" in r.headers["content-disposition"]

    lines = r.text.strip().splitlines()
    assert lines[0].startswith("severity,stage,file,account_code,writer_name")
    assert len(lines) == 4  # header + 2 parse failures + 1 sort rejection
    assert "random notes.pdf" in r.text
    assert "C00616" in r.text


def test_unknown_upload_is_404(client):
    assert client.get("/admin/statements/uploads/9999/failures").status_code == 404
    assert client.get("/admin/statements/uploads/9999/failures.csv").status_code == 404
