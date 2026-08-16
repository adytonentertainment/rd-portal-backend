import os

import pytest

from app.models.statements import Cadence, Catalog
from app.services.statement_ingest.filename_parser import parse_statement_filename

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "statements")

# (filename, period, cadence, account, display, royalty_type, kind, is_house)
FIXTURE_EXPECTATIONS = [
    ("Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf",
     "PUB25H2", Cadence.SEMIANNUAL, "CSJ002", "Javier Solis", Catalog.MECH, "pdf", False),
    ("Ben_PUB25H2_CSJ002_Javier Solis (Mechanical Royalties).xlsx",
     "PUB25H2", Cadence.SEMIANNUAL, "CSJ002", "Javier Solis", Catalog.MECH, "xlsx", False),
    ("Ben_PUB25Q4_C00139 - Luna Negra (YouTube Publishing).pdf",
     "PUB25Q4", Cadence.QUARTERLY, "C00139", "Luna Negra", Catalog.YT, "pdf", False),
    ("Ben_PUB25Q4_C00139_Luna Negra (YouTube Publishing).xlsx",
     "PUB25Q4", Cadence.QUARTERLY, "C00139", "Luna Negra", Catalog.YT, "xlsx", False),
    ("Ben_PUB26H1_C00650 - El Taiger (YouTube Publishing).pdf",
     "PUB26H1", Cadence.SEMIANNUAL, "C00650", "El Taiger", Catalog.YT, "pdf", False),
    ("Ben_PUB26H1_C00650_El Taiger (YouTube Publishing).xlsx",
     "PUB26H1", Cadence.SEMIANNUAL, "C00650", "El Taiger", Catalog.YT, "xlsx", False),
    ("Ben_PUB26H1_C00739-New - Swifty Blue NEW (YouTube Publishing).pdf",
     "PUB26H1", Cadence.SEMIANNUAL, "C00739-New", "Swifty Blue NEW", Catalog.YT, "pdf", False),
    ("Ben_PUB26H1_C00739-New_Swifty Blue NEW (YouTube Publishing).xlsx",
     "PUB26H1", Cadence.SEMIANNUAL, "C00739-New", "Swifty Blue NEW", Catalog.YT, "xlsx", False),
    ("Ben_PUB26H1_JN0080 - Kill Bill- The Rapper (Mechanical Royalties).pdf",
     "PUB26H1", Cadence.SEMIANNUAL, "JN0080", "Kill Bill- The Rapper", Catalog.MECH, "pdf", False),
    ("Ben_PUB26H1_JN0080_Kill Bill The Rapper (Mechanical Royalties).xlsx",
     "PUB26H1", Cadence.SEMIANNUAL, "JN0080", "Kill Bill The Rapper", Catalog.MECH, "xlsx", False),
    ("Ben_PUB26H1_JN0249 - OMB Peezy (Mechanical Royalties).pdf",
     "PUB26H1", Cadence.SEMIANNUAL, "JN0249", "OMB Peezy", Catalog.MECH, "pdf", False),
    ("Ben_PUB26H1_JN0249_OMB Peezy (Mechanical Royalties).xlsx",
     "PUB26H1", Cadence.SEMIANNUAL, "JN0249", "OMB Peezy", Catalog.MECH, "xlsx", False),
    ("Ben_PUB26Q2_C00139a - Bello Musical (Luna Negra) (YouTube Publishing).pdf",
     "PUB26Q2", Cadence.QUARTERLY, "C00139a", "Bello Musical (Luna Negra)", Catalog.YT, "pdf", False),
    ("Ben_PUB26Q2_C00139a_Bello Musical (Luna Negra) (YouTube Publishing).xlsx",
     "PUB26Q2", Cadence.QUARTERLY, "C00139a", "Bello Musical (Luna Negra)", Catalog.YT, "xlsx", False),
]


@pytest.mark.parametrize(
    "filename,period,cadence,account,display,rtype,kind,is_house",
    FIXTURE_EXPECTATIONS,
    ids=[row[0] for row in FIXTURE_EXPECTATIONS],
)
def test_all_real_fixture_filenames(filename, period, cadence, account, display, rtype, kind, is_house):
    parsed = parse_statement_filename(filename)
    assert parsed is not None
    assert parsed.period_code == period
    assert parsed.cadence == cadence
    assert parsed.account_code == account
    assert parsed.display_name == display
    assert parsed.royalty_type == rtype
    assert parsed.file_kind == kind
    assert parsed.is_house == is_house


def test_expectations_cover_every_fixture_file():
    fixture_files = sorted(
        f for f in os.listdir(FIXTURES_DIR) if f.lower().endswith((".pdf", ".xlsx"))
    )
    assert fixture_files == sorted(row[0] for row in FIXTURE_EXPECTATIONS)


def test_name_drift_pair_keys_to_same_account():
    # The PDF and XLSX of the same statement disagree on display name;
    # they must still pair on (period_code, account_code).
    pdf = parse_statement_filename(
        "Ben_PUB26H1_JN0080 - Kill Bill- The Rapper (Mechanical Royalties).pdf"
    )
    xlsx = parse_statement_filename(
        "Ben_PUB26H1_JN0080_Kill Bill The Rapper (Mechanical Royalties).xlsx"
    )
    assert (pdf.period_code, pdf.account_code) == (xlsx.period_code, xlsx.account_code) == ("PUB26H1", "JN0080")
    assert pdf.display_name != xlsx.display_name


def test_new_suffix_account_code_not_truncated():
    parsed = parse_statement_filename(
        "Ben_PUB26H1_C00739-New - Swifty Blue NEW (YouTube Publishing).pdf"
    )
    assert parsed.account_code == "C00739-New"
    assert parsed.display_name == "Swifty Blue NEW"


def test_sub_account_code():
    parsed = parse_statement_filename(
        "Ben_PUB26Q2_C00139a_Bello Musical (Luna Negra) (YouTube Publishing).xlsx"
    )
    assert parsed.account_code == "C00139a"
    # inner parens belong to the display name, only the trailing royalty parens are stripped
    assert parsed.display_name == "Bello Musical (Luna Negra)"
    assert parsed.royalty_type == Catalog.YT


def test_no_royalty_parens_returns_none_type():
    parsed = parse_statement_filename("Ben_PUB25H2_C00001b - AkwidAfterVydia.pdf")
    assert parsed is not None
    assert parsed.account_code == "C00001b"
    assert parsed.display_name == "AkwidAfterVydia"
    assert parsed.royalty_type is None


def test_unknown_trailing_parens_stay_in_display_name():
    parsed = parse_statement_filename("Ben_PUB25H2_C00002 - Some Writer (Deluxe).pdf")
    assert parsed.royalty_type is None
    assert parsed.display_name == "Some Writer (Deluxe)"


def test_house_account_codes():
    """House special-casing is disabled: EVERY account is a regular account so
    all of them show in the roster and all money lands in the totals."""
    cpj = parse_statement_filename(
        "Ben_PUB25H2_CPJ001 - Regalias Digitales (Performance Royalties).pdf"
    )
    assert cpj.is_house is False
    assert cpj.royalty_type == Catalog.PERF

    cs = parse_statement_filename(
        "Ben_PUB25H2_CS0001_Regalias Digitales (YouTube Publishing).xlsx"
    )
    assert cs.is_house is False

    # CSJ### is a regular Mechanical writer code, NOT a house account
    csj = parse_statement_filename(
        "Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf"
    )
    assert csj.is_house is False


def test_period_and_cadence():
    semi = parse_statement_filename("Ben_PUB26H1_JN0249 - OMB Peezy (Mechanical Royalties).pdf")
    assert semi.period_code == "PUB26H1"
    assert semi.cadence == Cadence.SEMIANNUAL

    quarterly = parse_statement_filename("Ben_PUB25Q4_C00139 - Luna Negra (YouTube Publishing).pdf")
    assert quarterly.period_code == "PUB25Q4"
    assert quarterly.cadence == Cadence.QUARTERLY


def test_accepts_full_paths():
    parsed = parse_statement_filename(
        "/some/storage/dir/Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf"
    )
    assert parsed is not None
    assert parsed.account_code == "CSJ002"


def test_uppercase_extension():
    parsed = parse_statement_filename("Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).PDF")
    assert parsed is not None
    assert parsed.file_kind == "pdf"


@pytest.mark.parametrize(
    "garbage",
    [
        "expected_values.json",
        ".DS_Store",
        "random.pdf",
        "notes.txt",
        "Ben_PUB25H2.pdf",                                   # no account / display
        "Ben_PUB25H2_CSJ002.pdf",                            # no display name
        "Ben_2025H2_CSJ002 - Javier Solis.pdf",              # bad period format
        "Ben_PUB25X2_CSJ002 - Javier Solis.pdf",             # bad cadence letter
        "PUB25H2_CSJ002 - Javier Solis.pdf",                 # missing Ben_ prefix
        "Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).txt",  # bad extension
        "Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties)",      # no extension
        "",
    ],
)
def test_garbage_filenames_return_none(garbage):
    assert parse_statement_filename(garbage) is None
