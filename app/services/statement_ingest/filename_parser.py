"""Filename parser for publisher statement files (PRD §2.3).

Statement files arrive as a loose dump named:

    Ben_<PERIOD>_<ACCOUNT_CODE>[ -_]<Display Name> (<Royalty Type>).<ext>

    Ben_PUB25H2_CSJ002 - Javier Solis (Mechanical Royalties).pdf   <- PDF form
    Ben_PUB25H2_CSJ002_Javier Solis (Mechanical Royalties).xlsx    <- XLSX form

Everything downstream (sorting, pairing, batching) keys off what this module
extracts. Pairing is done on (period_code, account_code) only — display names
drift between the PDF and XLSX of the same statement (e.g. JN0080
'Kill Bill- The Rapper' vs 'Kill Bill The Rapper').
"""

import os
import re
from dataclasses import dataclass
from typing import Optional

from app.models.statements import Cadence, Catalog

# Account codes may carry a -New suffix (re-contracted writers, e.g.
# C00739-New). The code itself is strictly alphanumeric — a greedy or
# hyphen-naive regex mis-keys the -New accounts (PRD §2.3).
_STEM_RE = re.compile(
    r"^Ben_"
    r"(?P<period>PUB\d{2}[HQ]\d)"
    r"_(?P<account>[A-Za-z0-9]+(?:-New)?)"
    r"(?: - |_)"
    r"(?P<display>.+?)"
    r"(?:\s*\((?P<rtype>Mechanical Royalties|YouTube Publishing|Performance Royalties)\))?"
    r"$"
)

# House accounts are DISABLED: every beneficiary account is treated as a
# regular account, so all of them appear in the roster and all money shows up
# in the totals. (Regalias Digitales' own CS0001/CPJ001 accounts are still
# real accounts — they simply behave like any other client rather than being
# hidden from counts, completeness and distribution.) Kept as an empty set so
# the client-import validator's "a client row must not claim a house account"
# rule and every import stay wired without special-casing.
HOUSE_ACCOUNT_CODES = frozenset()

_ROYALTY_TYPE_TO_CATALOG = {
    "Mechanical Royalties": Catalog.MECH,
    "YouTube Publishing": Catalog.YT,
    "Performance Royalties": Catalog.PERF,
}

_FILE_KINDS = ("pdf", "xlsx")


@dataclass(frozen=True)
class ParsedStatementFilename:
    period_code: str           # e.g. 'PUB26H1'
    cadence: Cadence           # H -> SEMIANNUAL, Q -> QUARTERLY
    account_code: str          # e.g. 'C00739-New', 'C00139a', 'CSJ002'
    display_name: str          # as written in the filename; NOT a pairing key
    royalty_type: Optional[Catalog]  # None when filename has no royalty parens
    file_kind: str             # 'pdf' | 'xlsx'
    is_house: bool             # CS\d+ / CPJ\d+ house accounts


def parse_statement_filename(filename: str) -> Optional[ParsedStatementFilename]:
    """Parse a statement filename; returns None for anything unrecognized."""
    name = os.path.basename(filename)
    stem, dot, ext = name.rpartition(".")
    if not dot:
        return None
    file_kind = ext.lower()
    if file_kind not in _FILE_KINDS:
        return None

    match = _STEM_RE.match(stem)
    if not match:
        return None

    period_code = match.group("period")
    cadence = Cadence.SEMIANNUAL if period_code[5] == "H" else Cadence.QUARTERLY
    rtype = match.group("rtype")

    return ParsedStatementFilename(
        period_code=period_code,
        cadence=cadence,
        account_code=match.group("account"),
        display_name=match.group("display").strip(),
        royalty_type=_ROYALTY_TYPE_TO_CATALOG[rtype] if rtype else None,
        file_kind=file_kind,
        is_house=match.group("account") in HOUSE_ACCOUNT_CODES,
    )
