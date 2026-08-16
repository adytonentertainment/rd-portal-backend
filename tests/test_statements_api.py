"""US-010: read APIs + waiver workflow + end-to-end pipeline test.

The e2e test drives the REAL 14 fixture files through upload -> inline
pipeline -> validation, then asserts everything through the public API
surface only (the way the RD frontend will consume it).
"""

import glob
import json
import os
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database.session import get_session
from app.models.models import User
from app.models.statements import (
    BeneficiaryAccount,
    Catalog,
    FindingScope,
    FindingSeverity,
    FindingStatus,
    Publisher,
    Statement,
    StatementBatch,
    ValidationFinding,
    ValidationRun,
    Writer,
)
from app.routers.auth import get_user
from app.routers.statements_admin import findings_admin_router, statements_admin_router

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "statements")
ADMIN_EMAIL = "admin@verax.app"

with open(os.path.join(FIXTURES_DIR, "expected_values.json")) as f:
    EXPECTED = json.load(f)


@pytest.fixture()
def admin_user(session):
    user = User(email=ADMIN_EMAIL, username="statements-admin", royalty_per_stream=0)
    session.add(user)
    session.commit()
    return user


@pytest.fixture()
def client(session, admin_user, tmp_path, monkeypatch):
    monkeypatch.setenv("STATEMENTS_STORAGE_ROOT", str(tmp_path / "statements-storage"))
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("INGEST_INLINE", "1")
    app = FastAPI()
    app.include_router(statements_admin_router)
    app.include_router(findings_admin_router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_user] = lambda: admin_user
    return TestClient(app)


def _all_fixture_files():
    return sorted(
        glob.glob(os.path.join(FIXTURES_DIR, "*.pdf"))
        + glob.glob(os.path.join(FIXTURES_DIR, "*.xlsx"))
    )


def _multipart(paths):
    parts = []
    for path in paths:
        with open(path, "rb") as f:
            parts.append(("files", (os.path.basename(path), f.read())))
    return parts


def _money(value):
    """API money fields arrive as JSON strings (pydantic Decimal)."""
    return None if value is None else Decimal(str(value))


def test_end_to_end_pipeline_via_api(client, session, admin_user):
    files = _all_fixture_files()
    assert len(files) == 14

    # --- upload all 14 loose files; INGEST_INLINE runs the whole pipeline ---
    response = client.post("/admin/statements/uploads", files=_multipart(files))
    assert response.status_code == 202
    assert response.json()["status"] == "done"

    # --- batches: 5 auto-derived, all clean -> approved ---
    batches = client.get("/admin/statements/batches").json()
    assert len(batches) == 5
    assert {(b["period_code"], b["catalog"]) for b in batches} == {
        ("PUB25H2", "MECH"),
        ("PUB26H1", "MECH"),
        ("PUB26H1", "YT"),
        ("PUB25Q4", "YT"),
        ("PUB26Q2", "YT"),
    }
    # Statement auditing is disabled in the pipeline: batches stay "uploaded"
    # (the gate distributes on parsed totals alone; approval is not required).
    assert all(b["status"] == "uploaded" for b in batches)
    assert sum(b["statement_count"] for b in batches) == 7

    # list filters
    assert len(client.get("/admin/statements/batches?period=PUB26H1").json()) == 2
    assert len(client.get("/admin/statements/batches?status=uploaded").json()) == 5
    assert client.get("/admin/statements/batches?status=needs_review").json() == []
    assert client.get("/admin/statements/batches?status=bogus").status_code == 422

    # --- batch detail: zero open findings, no validation run in the pipeline ---
    for b in batches:
        detail = client.get(f"/admin/statements/batches/{b['id']}").json()
        assert detail["finding_counts"] == {"blocker": 0, "warning": 0, "info": 0}
        assert detail["last_run"] is None
        assert client.get(f"/admin/statements/batches/{b['id']}/findings").json() == []

    # --- statements per batch: 7 total, zero-pay classification correct ---
    by_account = {}
    for b in batches:
        for s in client.get(f"/admin/statements/batches/{b['id']}/statements").json():
            by_account[s["account_code"]] = s
    assert set(by_account) == {
        "CSJ002", "C00139", "C00650", "C00739-New", "JN0080", "JN0249", "C00139a",
    }
    assert all(s["parse_status"] == "parsed" for s in by_account.values())
    # zero-pay classification is part of validation (V-STMT-5), which no longer
    # runs in the pipeline — it appears only after an explicit /revalidate.
    assert all(s["zero_pay_reason"] is None for s in by_account.values())

    # --- statement detail matches the verified ground truth (C00650: the
    # carryforward-into-payable case) ---
    expected = EXPECTED["PUB26H1_C00650"]
    detail = client.get(f"/admin/statements/{by_account['C00650']['id']}").json()
    assert detail["account_code"] == "C00650"
    assert detail["period_code"] == "PUB26H1"
    for api_field, json_field in [
        ("calculated", "calculated"),
        ("recouped", "recouped"),
        ("carried_forward_in", "carried_forward"),
        ("payable_prev", "payable_prev"),
        ("payable_this", "payable_this"),
        ("settlement_paid", "settlement"),
        ("before_tax", "before_tax"),
        ("payable", "payable"),
    ]:
        exp = expected[json_field]
        got = _money(detail[api_field])
        if exp is None:
            assert got is None, api_field
        else:
            assert got == Decimal(str(exp)), api_field
    assert detail["line_count"] == expected["xlsx_line_count"]
    assert abs(_money(detail["detail_sum"]) - Decimal(str(expected["xlsx_detail_sum"]))) <= Decimal(
        "0.0001"
    )
    assert detail["finding_counts"] == {"blocker": 0, "warning": 0, "info": 0}

    # --- lines paginate (C00139a: 10,035 rows) ---
    stmt_id = by_account["C00139a"]["id"]
    page1 = client.get(f"/admin/statements/{stmt_id}/lines?page=1&page_size=100").json()
    assert page1["total"] == 10035
    assert len(page1["items"]) == 100
    assert [line["row_no"] for line in page1["items"]] == list(range(1, 101))
    last = client.get(f"/admin/statements/{stmt_id}/lines?page=101&page_size=100").json()
    assert len(last["items"]) == 35
    assert last["items"][-1]["row_no"] == 10035
    beyond = client.get(f"/admin/statements/{stmt_id}/lines?page=102&page_size=100").json()
    assert beyond["items"] == [] and beyond["total"] == 10035
    assert client.get(f"/admin/statements/{stmt_id}/lines?page=0").status_code == 422
    assert client.get(f"/admin/statements/{stmt_id}/lines?page_size=2000").status_code == 422

    # --- waive flow round-trip: corrupt one statement -> blockers appear ---
    csj = session.get(Statement, by_account["CSJ002"]["id"])
    original_payable = csj.payable
    original_before_tax = csj.before_tax
    csj.payable = Decimal("-5")
    # break the payable identity too (V-STMT-3 reconciles against before_tax OR
    # payable, so a bad payable alone no longer trips it; corrupting calculated
    # instead would also trip the V-STMT-2 rounding blocker)
    csj.before_tax = Decimal("777777")
    session.commit()
    batch_id = csj.batch_id

    revalidate = client.post(f"/admin/statements/batches/{batch_id}/revalidate").json()
    assert revalidate["blockers"] == 2  # V-STMT-3 identity + V-STMT-4 negative
    assert revalidate["batch_status"] == "needs_review"
    findings = client.get(
        f"/admin/statements/batches/{batch_id}/findings?severity=blocker&status=open"
    ).json()
    assert {f["rule_id"] for f in findings} == {"V-STMT-3", "V-STMT-4"}

    # reason is required -> 422 without it
    assert client.post(f"/admin/findings/{findings[0]['id']}/waive", json={}).status_code == 422

    waived = client.post(
        f"/admin/findings/{findings[0]['id']}/waive", json={"reason": "known carryover quirk"}
    ).json()
    assert waived["status"] == "waived"
    assert waived["waived_by"] == admin_user.id
    assert waived["waived_reason"] == "known carryover quirk"
    assert waived["waived_at"] is not None

    acked = client.post(f"/admin/findings/{findings[1]['id']}/acknowledge").json()
    assert acked["status"] == "open"  # acknowledgement is metadata, not status
    assert acked["acknowledged_by"] == admin_user.id
    assert acked["acknowledged_at"] is not None

    # waiver + acknowledgement survive a re-run (stable finding identity)
    revalidate = client.post(f"/admin/statements/batches/{batch_id}/revalidate").json()
    assert revalidate["blockers"] == 1  # only the acknowledged one still counts
    waived_list = client.get(
        f"/admin/statements/batches/{batch_id}/findings?status=waived"
    ).json()
    assert [f["id"] for f in waived_list] == [findings[0]["id"]]
    assert waived_list[0]["waived_reason"] == "known carryover quirk"
    still_open = client.get(
        f"/admin/statements/batches/{batch_id}/findings?status=open"
    ).json()
    assert [f["id"] for f in still_open] == [findings[1]["id"]]
    assert still_open[0]["acknowledged_at"] is not None

    # fixing the data resolves everything and the batch goes green again
    fixed = session.get(Statement, csj.id)
    fixed.payable = original_payable
    fixed.before_tax = original_before_tax
    session.commit()
    revalidate = client.post(f"/admin/statements/batches/{batch_id}/revalidate").json()
    assert revalidate["blockers"] == 0
    assert revalidate["batch_status"] == "approved"
    assert (
        client.get(f"/admin/statements/batches/{batch_id}/findings?status=open").json() == []
    )


# --- cheap direct-DB edge cases (no files, no pipeline) ----------------------


def _make_world(session):
    publisher = Publisher(name="Test Pub")
    session.add(publisher)
    session.flush()
    writer = Writer(publisher_id=publisher.id, canonical_name="Test Writer")
    session.add(writer)
    session.flush()
    account = BeneficiaryAccount(writer_id=writer.id, account_code="T00001")
    session.add(account)
    session.flush()
    batch = StatementBatch(
        publisher_id=publisher.id,
        label="YouTube 2026H1",
        period_code="PUB26H1",
        catalog=Catalog.YT,
    )
    session.add(batch)
    session.flush()
    run = ValidationRun(batch_id=batch.id)
    session.add(run)
    session.flush()
    return batch, run


def _make_finding(session, run, severity, status=FindingStatus.OPEN):
    finding = ValidationFinding(
        run_id=run.id,
        rule_id="V-TEST-1",
        severity=severity,
        scope=FindingScope.STATEMENT,
        scope_ref="statement:1",
        message="test finding",
        status=status,
    )
    session.add(finding)
    session.commit()
    return finding


def test_waive_unknown_finding_404(client):
    assert client.post("/admin/findings/99999/waive", json={"reason": "x"}).status_code == 404
    assert client.post("/admin/findings/99999/acknowledge").status_code == 404


def test_waive_info_severity_409(client, session):
    _, run = _make_world(session)
    finding = _make_finding(session, run, FindingSeverity.INFO)
    response = client.post(f"/admin/findings/{finding.id}/waive", json={"reason": "x"})
    assert response.status_code == 409


def test_waive_and_acknowledge_require_open_status(client, session):
    _, run = _make_world(session)
    resolved = _make_finding(session, run, FindingSeverity.BLOCKER, FindingStatus.RESOLVED)
    assert (
        client.post(f"/admin/findings/{resolved.id}/waive", json={"reason": "x"}).status_code
        == 409
    )
    assert client.post(f"/admin/findings/{resolved.id}/acknowledge").status_code == 409
    waived = _make_finding(session, run, FindingSeverity.WARNING, FindingStatus.WAIVED)
    assert client.post(f"/admin/findings/{waived.id}/acknowledge").status_code == 409


def test_blank_waive_reason_422(client, session):
    _, run = _make_world(session)
    finding = _make_finding(session, run, FindingSeverity.WARNING)
    assert (
        client.post(f"/admin/findings/{finding.id}/waive", json={"reason": ""}).status_code
        == 422
    )


def test_findings_filter_invalid_enum_422(client, session):
    batch, _ = _make_world(session)
    url = f"/admin/statements/batches/{batch.id}/findings"
    assert client.get(f"{url}?severity=fatal").status_code == 422
    assert client.get(f"{url}?status=closed").status_code == 422


def test_batch_detail_without_runs_has_no_last_run(client, session):
    batch, _ = _make_world(session)
    # _make_world creates a run; build a second bare batch
    bare = StatementBatch(label="Mechanical 2025H2", period_code="PUB25H2", catalog=Catalog.MECH)
    session.add(bare)
    session.commit()
    detail = client.get(f"/admin/statements/batches/{bare.id}").json()
    assert detail["last_run"] is None
    assert detail["statement_count"] == 0
    assert detail["finding_counts"] == {"blocker": 0, "warning": 0, "info": 0}


def test_unknown_ids_404(client):
    assert client.get("/admin/statements/batches/99999").status_code == 404
    assert client.get("/admin/statements/batches/99999/findings").status_code == 404
    assert client.get("/admin/statements/batches/99999/statements").status_code == 404
    assert client.get("/admin/statements/99999").status_code == 404
    assert client.get("/admin/statements/99999/lines").status_code == 404


def test_routes_registered_in_api_router():
    from app.routers.router import api_router

    paths = {route.path for route in api_router.routes}
    assert "/admin/statements/batches" in paths
    assert "/admin/statements/batches/{batch_id}" in paths
    assert "/admin/statements/batches/{batch_id}/findings" in paths
    assert "/admin/statements/batches/{batch_id}/statements" in paths
    assert "/admin/statements/{statement_id}" in paths
    assert "/admin/statements/{statement_id}/lines" in paths
    assert "/admin/findings/{finding_id}/waive" in paths
    assert "/admin/findings/{finding_id}/acknowledge" in paths
