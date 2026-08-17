"""PDF account summary parser (PRD §2.5).

The PDF is the *payment of record*: page 2 carries an "Account Summary"
block in one of two layout generations:

  Old 4-line form              New 9-line form
  ---------------------------  -------------------------------------
  Royalties calculated:        Royalties calculated:
  Amount recouped:             Amount recouped:
  Total amount before tax:     Amount reserve taken:
  Total payable amount         Amount reserve released:
                               Carried forward from previous period:
                               (or: Carried forward to Next period:)
                               Amount before tax:
                               Payable amount for this period
                               Payable amount from previous period
                               Settlement paid
                               Payable amount

Fields absent from a layout stay None — a missing value is not 0.00.

Extracted PDF text carries artifacts: spaces inserted inside words
('Payable am ount', 'pe riod'). Each line is therefore stripped of ALL
whitespace and lowercased before matching, so the patterns below are
written against the whitespace-free form and the artifacts vanish.
"""
import re
from decimal import Decimal
from typing import Dict, Iterable, Optional

import pdfplumber

# Amount: optional minus, digits with comma thousands separators, optional decimals.
_NUM = r"(-?\d[\d,]*(?:\.\d+)?)"

# Ordered (field, pattern) pairs matched against whitespace-free lowercase lines.
# Anchored ^...$ so 'payableamount...' never swallows the 'forthisperiod' variants.
_FIELD_PATTERNS = (
    ("calculated", re.compile(r"^royaltiescalculated:?" + _NUM + r"$")),
    ("recouped", re.compile(r"^amountrecouped:?" + _NUM + r"$")),
    ("reserve_taken", re.compile(r"^amountreservetaken:?" + _NUM + r"$")),
    ("reserve_released", re.compile(r"^amountreservereleased:?" + _NUM + r"$")),
    ("carried_forward", re.compile(r"^carriedforwardfrompreviousperiod:?" + _NUM + r"$")),
    ("carried_forward_out", re.compile(r"^carriedforwardtonextperiod:?" + _NUM + r"$")),
    # old layout says 'Total amount before tax', new layout 'Amount before tax'
    ("before_tax", re.compile(r"^(?:total)?amountbeforetax:?" + _NUM + r"$")),
    ("payable_this", re.compile(r"^payableamountforthisperiod:?" + _NUM + r"$")),
    ("payable_prev", re.compile(r"^payableamountfrompreviousperiod:?" + _NUM + r"$")),
    ("settlement", re.compile(r"^settlementpaid:?" + _NUM + r"$")),
    # old layout 'Total payable amount', new layout 'Payable amount'
    ("payable", re.compile(r"^(?:total)?payableamount:?" + _NUM + r"$")),
)

# Page-1 statement letter: '... you will find enclosed a cheque of USD 936.21.'
_CHEQUE = re.compile(r"chequeofusd" + _NUM)

SUMMARY_FIELDS = tuple(name for name, _ in _FIELD_PATTERNS) + ("cheque_amount",)


def _to_decimal(raw: str) -> Decimal:
    return Decimal(raw.replace(",", ""))


def _normalize(line: str) -> str:
    return re.sub(r"\s+", "", line.replace("−", "-")).lower()


def parse_summary_lines(lines: Iterable[str]) -> Dict[str, Optional[Decimal]]:
    """Match account-summary fields + cheque line over raw text lines.

    First occurrence of each field wins; unmatched fields stay None.
    """
    result = {name: None for name in SUMMARY_FIELDS}
    for line in lines:
        norm = _normalize(line)
        if not norm:
            continue
        for name, pattern in _FIELD_PATTERNS:
            if result[name] is not None:
                continue
            match = pattern.match(norm)
            if match:
                result[name] = _to_decimal(match.group(1))
                break
        else:
            if result["cheque_amount"] is None:
                match = _CHEQUE.search(norm)
                if match:
                    result["cheque_amount"] = _to_decimal(match.group(1))
    return result


def parse_statement_pdf(path: str) -> Dict[str, Optional[Decimal]]:
    """Extract the account summary figures from a statement PDF.

    Reads pages 1-3 only (summary is on page 2, cheque line on page 1;
    page 4+ is track detail, irrelevant and potentially huge).
    """
    return parse_summary_lines(_extract_lines(path))


try:
    import fitz  # PyMuPDF

    _HAVE_FITZ = True
except ImportError:  # pragma: no cover
    _HAVE_FITZ = False


def _extract_lines(path: str):
    """Text lines from the first 3 pages.

    PyMuPDF extracts the same summary text ~10-20x faster than pdfplumber,
    which matters at 2,600 PDFs per drop. The summary parser is line-regex
    based, so what must hold is that every regex-matched line survives with
    its label and figure on one line — verified against the full real corpus
    (all statements re-parsed and compared to the previously stored figures)
    before this path shipped. pdfplumber stays as the fallback.
    """
    lines = []
    if _HAVE_FITZ:
        # NOT get_text("text"): that mode splits label and figure onto separate
        # lines for these statements ("Royalties calculated:" / "6,663.27"),
        # which silently produces None for every summary field. Reconstructing
        # lines from words — grouped by baseline, ordered by x — yields exactly
        # pdfplumber's line text, verified across the full corpus.
        with fitz.open(path) as doc:
            for page_no in range(min(3, doc.page_count)):
                rows = {}
                for x0, _y0, _x1, _y1, word, *_ in doc[page_no].get_text("words"):
                    rows.setdefault(round(_y0, 1), []).append((x0, word))
                for y in sorted(rows):
                    lines.append(" ".join(w for _x, w in sorted(rows[y])))
        return lines

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages[:3]:
            text = page.extract_text() or ""
            lines.extend(text.splitlines())
    return lines
