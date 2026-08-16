"""US-005: XLSX detail parser tests against ALL 7 real fixture files."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.statements import Catalog, Publisher, Statement, StatementLine, Writer
from app.models.statements import BeneficiaryAccount, Cadence, StatementBatch, StatementUpload, UploadStatus
from app.services.statement_ingest.filename_parser import parse_statement_filename
from app.services.statement_ingest.xlsx_parser import parse_statement_xlsx, persist_lines

FIXTURES = Path(__file__).parent / "fixtures" / "statements"
EXPECTED = json.loads((FIXTURES / "expected_values.json").read_text())
XLSX_FILES = sorted(FIXTURES.glob("*.xlsx"))
TOL = Decimal("0.0001")


def _key(path: Path) -> str:
    parsed = parse_statement_filename(path.name)
    return "{}_{}".format(parsed.period_code, parsed.account_code)


def _fixture(name: str) -> Path:
    return FIXTURES / name


def test_all_seven_fixture_xlsx_are_covered():
    assert len(XLSX_FILES) == 7
    assert sorted(_key(p) for p in XLSX_FILES) == sorted(EXPECTED.keys())


@pytest.mark.parametrize("path", XLSX_FILES, ids=lambda p: p.name)
def test_fixture_matches_expected_values(path):
    exp = EXPECTED[_key(path)]
    lines, detail_sum, embedded_total, line_count = parse_statement_xlsx(path)

    assert line_count == exp["xlsx_line_count"]
    assert len(lines) == line_count
    assert abs(detail_sum - Decimal(str(exp["xlsx_detail_sum"]))) <= TOL
    assert embedded_total is not None
    assert abs(embedded_total - Decimal(str(exp["xlsx_embedded_total"]))) <= TOL


@pytest.mark.parametrize(
    "name",
    [
        "Ben_PUB26H1_JN0080_Kill Bill The Rapper (Mechanical Royalties).xlsx",
        "Ben_PUB25Q4_C00139_Luna Negra (YouTube Publishing).xlsx",
    ],
)
def test_blank_first_row_files_parse(name):
    # These two fixtures have a blank row before the header (PRD §2.4 #2)
    lines, _, embedded_total, line_count = parse_statement_xlsx(_fixture(name))
    assert line_count > 0
    assert embedded_total is not None
    assert lines[0]["row_no"] == 1


def test_mechanical_schema_maps_songcode_and_wrtiersplit():
    lines, _, _, _ = parse_statement_xlsx(
        _fixture("Ben_PUB25H2_CSJ002_Javier Solis (Mechanical Royalties).xlsx")
    )
    first = lines[0]
    assert isinstance(first["song_code"], str)
    assert first.get("asset_id") is None and first.get("custom_id") is None
    # 'WrtierSplit%' (real header typo) must land in writer_split_pct
    assert isinstance(first["writer_split_pct"], Decimal)
    assert isinstance(first["ben_split_pct"], Decimal)


def test_youtube_schema_maps_assetid_customid_contper():
    lines, _, _, _ = parse_statement_xlsx(
        _fixture("Ben_PUB26Q2_C00139a_Bello Musical (Luna Negra) (YouTube Publishing).xlsx")
    )
    first = lines[0]
    assert isinstance(first["asset_id"], str)
    assert isinstance(first["custom_id"], str)
    assert first.get("song_code") is None
    # ContPer occupies WrtierSplit%'s slot in the YouTube layout
    assert isinstance(first["writer_split_pct"], Decimal)


def test_identity_fields_are_str_and_money_is_decimal():
    lines, detail_sum, embedded_total, _ = parse_statement_xlsx(
        _fixture("Ben_PUB26H1_C00739-New_Swifty Blue NEW (YouTube Publishing).xlsx")
    )
    for line in lines:
        for field in ("song_code", "asset_id", "custom_id", "song_title"):
            assert line.get(field) is None or isinstance(line[field], str)
        assert line["earnings"] is None or isinstance(line["earnings"], Decimal)
    assert isinstance(detail_sum, Decimal)
    assert isinstance(embedded_total, Decimal)


def test_total_row_excluded_from_lines():
    lines, _, _, _ = parse_statement_xlsx(
        _fixture("Ben_PUB26H1_C00739-New_Swifty Blue NEW (YouTube Publishing).xlsx")
    )
    # No surviving line may look like the grand-total row
    for line in lines:
        non_null = [
            v for k, v in line.items() if k not in ("row_no", "earnings") and v is not None
        ]
        assert non_null, "grand-total row leaked into lines: %r" % (line,)
    assert [line["row_no"] for line in lines] == list(range(1, len(lines) + 1))


def test_unreadable_file_raises():
    with pytest.raises(Exception):
        parse_statement_xlsx(_fixture("expected_values.json"))


def _make_statement(session, code="C00739-New", period="PUB26H1"):
    pub = Publisher(name="Regalias Digitales, LLC")
    session.add(pub)
    session.flush()
    writer = Writer(publisher_id=pub.id, canonical_name="Swifty Blue NEW")
    session.add(writer)
    session.flush()
    account = BeneficiaryAccount(writer_id=writer.id, account_code=code, catalog=Catalog.YT)
    session.add(account)
    session.flush()
    upload = StatementUpload(file_count=1, status=UploadStatus.UPLOADED, stats={})
    session.add(upload)
    session.flush()
    batch = StatementBatch(
        publisher_id=pub.id,
        label="YouTube 2026H1",
        period_code=period,
        catalog=Catalog.YT,
        cadence=Cadence.SEMIANNUAL,
        upload_id=upload.id,
        stats={},
    )
    session.add(batch)
    session.flush()
    stmt = Statement(batch_id=batch.id, account_id=account.id, period_code=period)
    session.add(stmt)
    session.flush()
    return stmt


def test_persist_lines_bulk_inserts(session):
    stmt = _make_statement(session)
    lines, detail_sum, _, line_count = parse_statement_xlsx(
        _fixture("Ben_PUB26H1_C00739-New_Swifty Blue NEW (YouTube Publishing).xlsx")
    )

    inserted = persist_lines(stmt.id, lines, session)
    session.commit()

    assert inserted == line_count == 84
    rows = (
        session.query(StatementLine)
        .filter(StatementLine.statement_id == stmt.id)
        .order_by(StatementLine.row_no)
        .all()
    )
    assert len(rows) == line_count
    assert rows[0].row_no == 1 and rows[-1].row_no == line_count
    total = sum((r.earnings for r in rows), Decimal("0"))
    assert abs(total - detail_sum) <= TOL


def test_fixture_files_not_modified_by_parsing():
    path = _fixture("Ben_PUB25H2_CSJ002_Javier Solis (Mechanical Royalties).xlsx")
    before = path.read_bytes()
    parse_statement_xlsx(path)
    assert path.read_bytes() == before
