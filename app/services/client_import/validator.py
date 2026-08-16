"""Client-list validation rules (infra PRD §3.2).

Pure functions over parsed rows (+ their match results) -> findings, mirroring
the statement validation engine's shape. Severity vocabulary matches
FindingSeverity: blocker | warning | info.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from app.services.statement_ingest.filename_parser import HOUSE_ACCOUNT_CODES
from .matcher import MatchResult, normalize
from .parser import ClientRow

# HOUSE_ACCOUNT_CODES is imported from the ingest filename parser so the ingest
# and client-import sides can never drift on what counts as a house account
# (infra PRD §2.5: a house account must never be claimed by a client row).


@dataclass
class ClientFinding:
    rule_id: str
    severity: str          # "blocker" | "warning" | "info"
    sheet: str
    row_no: Optional[int]
    subject: str           # the row's name (or account code)
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


def validate_rows(
    rows: List[ClientRow],
    matches: Optional[Dict[int, MatchResult]] = None,
    all_account_codes: Optional[set] = None,
) -> List[ClientFinding]:
    """`matches` is keyed by id(row); pass it to enable match-dependent rules
    (C-UNMATCHED-ROW, C-HOUSE-COLLISION). `all_account_codes` enables
    C-UNLISTED-ACCOUNT (accounts with no client row)."""
    findings: List[ClientFinding] = []
    matches = matches or {}

    # Per-row rules
    for r in rows:
        for e in r.emails:
            if not e.is_valid:
                findings.append(ClientFinding(
                    "C-BAD-EMAIL", "blocker", r.sheet, r.row_no, r.name,
                    f"Unparseable email address: {e.address!r}"))
        if r.unknown_catalog_tokens:
            findings.append(ClientFinding(
                "C-BAD-CATALOG", "warning", r.sheet, r.row_no, r.name,
                f"Unrecognized Admin Type token(s): {r.unknown_catalog_tokens} "
                f"(raw={r.raw_admin_type!r})"))
        if not r.valid_emails:
            findings.append(ClientFinding(
                "C-NO-EMAIL", "info", r.sheet, r.row_no, r.name,
                "No usable email; writer exists but cannot be invited yet"))

        m = matches.get(id(r))
        if m is not None:
            if not m.matched:
                findings.append(ClientFinding(
                    "C-UNMATCHED-ROW", "warning", r.sheet, r.row_no, r.name,
                    f"No statement account matched (best score {m.score}); "
                    "goes to the resolution queue"))
            if set(m.account_codes) & HOUSE_ACCOUNT_CODES:
                findings.append(ClientFinding(
                    "C-HOUSE-COLLISION", "blocker", r.sheet, r.row_no, r.name,
                    f"Row matched a house account {set(m.account_codes) & HOUSE_ACCOUNT_CODES}"))

    # Cross-row: duplicate normalized payee names
    by_payee: Dict[str, List[ClientRow]] = defaultdict(list)
    for r in rows:
        key = normalize(r.payee_name or r.name)
        if key:
            by_payee[key].append(r)
    for key, dupes in by_payee.items():
        if len(dupes) > 1:
            names = ", ".join(f"{d.sheet}:{d.row_no}" for d in dupes)
            findings.append(ClientFinding(
                "C-NAME-DUP", "warning", dupes[0].sheet, dupes[0].row_no,
                dupes[0].payee_name or dupes[0].name,
                f"{len(dupes)} rows share normalized payee {key!r} ({names})"))

    # Accounts with no client row at all
    if all_account_codes is not None:
        covered = set()
        for m in matches.values():
            if m.matched:
                covered.update(m.account_codes)
        for code in sorted(all_account_codes - covered - HOUSE_ACCOUNT_CODES):
            findings.append(ClientFinding(
                "C-UNLISTED-ACCOUNT", "warning", "-", None, code,
                "Statement account has no client-list row; earning with no "
                "roster entry — add to resolution queue"))

    return findings


def summarize(findings: List[ClientFinding]) -> Dict[str, int]:
    return dict(Counter(f.rule_id for f in findings))
