"""US-006: PDF account summary parser tests against ALL 7 real fixture files."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.statement_ingest.filename_parser import parse_statement_filename
from app.services.statement_ingest.pdf_parser import parse_statement_pdf, parse_summary_lines

FIXTURES = Path(__file__).parent / "fixtures" / "statements"
EXPECTED = json.loads((FIXTURES / "expected_values.json").read_text())
PDF_FILES = sorted(FIXTURES.glob("*.pdf"))

# expected_values.json keys asserted exactly (null in JSON == None in Python)
SUMMARY_FIELDS = (
    "calculated",
    "recouped",
    "reserve_taken",
    "reserve_released",
    "carried_forward",
    "before_tax",
    "payable_this",
    "payable_prev",
    "settlement",
    "payable",
)

# Page-1 cheque line amounts; None where the letter carries no cheque
# (below-threshold C00739-New, fully-recouped JN0249).
CHEQUE_AMOUNTS = {
    "PUB25H2_CSJ002": Decimal("936.21"),
    "PUB25Q4_C00139": Decimal("8996.73"),
    "PUB26H1_C00650": Decimal("45193.21"),
    "PUB26H1_C00739-New": None,
    "PUB26H1_JN0080": Decimal("1759.60"),
    "PUB26H1_JN0249": None,
    "PUB26Q2_C00139a": Decimal("27230.58"),
}


def _key(path: Path) -> str:
    parsed = parse_statement_filename(path.name)
    return "{}_{}".format(parsed.period_code, parsed.account_code)


def _fixture(name: str) -> Path:
    return FIXTURES / name


def test_all_seven_fixture_pdfs_are_covered():
    assert len(PDF_FILES) == 7
    assert sorted(_key(p) for p in PDF_FILES) == sorted(EXPECTED.keys())


@pytest.mark.parametrize("path", PDF_FILES, ids=lambda p: p.name)
def test_every_summary_field_matches_expected_values(path):
    exp = EXPECTED[_key(path)]
    result = parse_statement_pdf(str(path))

    for field in SUMMARY_FIELDS:
        if exp[field] is None:
            assert result[field] is None, "%s: expected None, got %r" % (field, result[field])
        else:
            assert result[field] == Decimal(str(exp[field])), "%s mismatch: %r != %r" % (
                field,
                result[field],
                exp[field],
            )


@pytest.mark.parametrize("path", PDF_FILES, ids=lambda p: p.name)
def test_cheque_line_amount(path):
    result = parse_statement_pdf(str(path))
    assert result["cheque_amount"] == CHEQUE_AMOUNTS[_key(path)]


def test_old_layout_leaves_new_layout_fields_none():
    # CSJ002 is the 4-line 'Total payable amount' generation
    result = parse_statement_pdf(
        str(_fixture("Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf"))
    )
    for field in ("reserve_taken", "reserve_released", "carried_forward",
                  "payable_this", "payable_prev", "settlement"):
        assert result[field] is None
    assert result["calculated"] == Decimal("936.21")
    assert result["payable"] == Decimal("936.21")


def test_carried_forward_to_next_period_is_separate_field():
    # C00739-New carries its below-threshold balance OUT — that must not
    # populate carried_forward (which is strictly 'from previous period')
    result = parse_statement_pdf(
        str(_fixture("Ben_PUB26H1_C00739-New - Swifty Blue NEW (YouTube Publishing).pdf"))
    )
    assert result["carried_forward"] is None
    assert result["carried_forward_out"] == Decimal("27.87")


def test_negative_recouped_parses():
    # JN0249: full recoupment, recouped is shown negative on the PDF
    result = parse_statement_pdf(
        str(_fixture("Ben_PUB26H1_JN0249 - OMB Peezy (Mechanical Royalties).pdf"))
    )
    assert result["recouped"] == Decimal("-38009.34")
    assert result["payable"] == Decimal("0.00")


def test_patterns_tolerate_spaces_inside_words():
    # pdftotext-style artifacts per PRD §2.5: spaces injected mid-word
    lines = [
        "Royalties calc ulated: 1,234.56",
        "Amount reco uped: -7.89",
        "Carried forward from previous pe riod: 38,529.94",
        "Payable am ount for this period 45,193.21",
        "Payable am ount 45,193.21",
        "For your payable amount you will find enclosed a che que of USD 936.21.",
    ]
    result = parse_summary_lines(lines)
    assert result["calculated"] == Decimal("1234.56")
    assert result["recouped"] == Decimal("-7.89")
    assert result["carried_forward"] == Decimal("38529.94")
    assert result["payable_this"] == Decimal("45193.21")
    assert result["payable"] == Decimal("45193.21")
    assert result["cheque_amount"] == Decimal("936.21")


def test_payable_variants_do_not_cross_match():
    # the bare 'Payable amount' pattern must not swallow the period variants
    result = parse_summary_lines(["Payable amount for this period 10.00"])
    assert result["payable_this"] == Decimal("10.00")
    assert result["payable"] is None
    assert result["payable_prev"] is None

    result = parse_summary_lines(["Payable amount from previous period 5.00"])
    assert result["payable_prev"] == Decimal("5.00")
    assert result["payable"] is None


def test_unrelated_lines_yield_all_none():
    result = parse_summary_lines(
        ["VAT (0 %): 0.00", "Total: 58,518.21 -8,053.34 38,009.34 12,455.53", "Royalty Pro"]
    )
    assert all(v is None for v in result.values())


def test_unreadable_file_raises():
    with pytest.raises(Exception):
        parse_statement_pdf(str(_fixture("expected_values.json")))


def test_fixture_files_not_modified_by_parsing():
    path = _fixture("Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf")
    before = path.read_bytes()
    parse_statement_pdf(str(path))
    assert path.read_bytes() == before
