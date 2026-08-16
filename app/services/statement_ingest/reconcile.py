"""Ingestion reconciliation: prove the DB faithfully mirrors the source files.

The statement FILENAMES are the ground truth (account code + display name +
period + catalog). This module re-derives that truth from the stored files and
checks it against what the app recorded, so ingestion correctness is a runnable
check instead of a manual comparison:

  1. file_identity — every statement's stored PDF/XLSX basename parses back to
     the same (account_code, period_code) its DB row carries.
  2. account_identity — every account's display_name matches its statement
     filenames (the identity the matcher runs on is real, not drifted).
  3. exact_owner — the "exact wins" invariant: an account whose own name
     exactly equals some non-house writer's canonical name must be OWNED by a
     writer of that name. Anything else means a group/fuzzy sweep stole it
     (the Luna Negra bug).
  4. distribution_owner — every distribution's writer_id equals the current
     owner of its statement's account (portals show the right person's money).

Run via `GET /admin/statements/reconcile`, the pytest suite, or
`scripts/reconcile_ingestion.py`.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.statements import (
    BeneficiaryAccount,
    Distribution,
    Statement,
    Writer,
)
from app.services.client_import.matcher import _pre_paren, _split_candidates, normalize
from app.services.statement_ingest.filename_parser import parse_statement_filename

_SAMPLE_CAP = 50


def _names_of(value: Optional[str]) -> set:
    """Normalized identity forms of a display name (full + pre-parenthetical)."""
    out = set()
    for form in (value or "", _pre_paren(value or "")):
        n = normalize(form)
        if n:
            out.add(n)
    return out


def reconcile_ingestion(db: Session) -> Dict:
    """Run every check; returns {"ok", "checked", "violations"} where each
    violations list is capped at 50 samples (counts are exact)."""
    violations: Dict[str, List[dict]] = {
        "file_identity": [],
        "account_identity": [],
        "exact_owner": [],
        "distribution_owner": [],
    }
    counts = {k: 0 for k in violations}

    def flag(check: str, item: dict) -> None:
        counts[check] += 1
        if len(violations[check]) < _SAMPLE_CAP:
            violations[check].append(item)

    accounts = db.query(BeneficiaryAccount).all()
    acct_by_id = {a.id: a for a in accounts}
    writers = db.query(Writer).all()
    writer_by_id = {w.id: w for w in writers}
    by_canon: Dict[str, List[Writer]] = defaultdict(list)
    for w in writers:
        if not w.is_house_account:
            by_canon[normalize(w.canonical_name)].append(w)

    # 1 + 2: statements' filenames vs DB rows and account identity
    statements = db.query(Statement).all()
    file_names_per_account: Dict[int, set] = defaultdict(set)
    for s in statements:
        acct = acct_by_id.get(s.account_id)
        for path in (s.pdf_path, s.xlsx_path):
            if not path:
                continue
            parsed = parse_statement_filename(os.path.basename(path))
            if parsed is None:
                continue  # unparseable names never produced statements
            if acct is None or parsed.account_code != acct.account_code or (
                parsed.period_code != s.period_code
            ):
                flag("file_identity", {
                    "statement_id": s.id,
                    "file": os.path.basename(path),
                    "db_account": acct.account_code if acct else None,
                    "db_period": s.period_code,
                })
            else:
                file_names_per_account[acct.id].add(normalize(parsed.display_name))

    for a in accounts:
        seen = file_names_per_account.get(a.id)
        if seen and a.display_name and normalize(a.display_name) not in seen:
            flag("account_identity", {
                "account": a.account_code,
                "stored_display": a.display_name,
                "file_displays": sorted(seen)[:3],
            })

    # 3: exact-owner invariant
    def _owner_names(w: Optional[Writer]) -> set:
        """Every name form the owner legitimately answers to — the same
        explosion the matcher uses on a client row: full name, pre-parenthetical
        ("Kristopher Norton" from "Kristopher Norton (M.I.M.E)"), each
        parenthetical group ("M.I.M.E"), and "/"-separated alias forms
        ("Outlawz" from "E.D.I. / Outlawz") — plus the payee."""
        if w is None:
            return set()
        out = set()
        for source in (w.canonical_name, w.payee_name):
            for cand in _split_candidates(source or ""):
                n = normalize(cand)
                if n:
                    out.add(n)
        return out

    for a in accounts:
        identity = _names_of(a.display_name)
        if not identity:
            continue
        exact_writers = [w for n in identity for w in by_canon.get(n, [])]
        if not exact_writers:
            continue  # no writer claims this exact name — consolidation is fine
        owner = writer_by_id.get(a.writer_id)
        owner_names = _owner_names(owner)
        if not (identity & owner_names):
            flag("exact_owner", {
                "account": a.account_code,
                "identity": a.display_name,
                "owner": owner.canonical_name if owner else None,
                "rightful": sorted({w.canonical_name for w in exact_writers})[:3],
            })

    # 4: distributions point at the account's current owner
    for d in db.query(Distribution).all():
        s = db.get(Statement, d.statement_id)
        acct = acct_by_id.get(s.account_id) if s else None
        if acct is not None and d.writer_id != acct.writer_id:
            flag("distribution_owner", {
                "distribution_id": d.id,
                "account": acct.account_code,
                "distribution_writer": d.writer_id,
                "account_writer": acct.writer_id,
            })

    checked = {
        "statements": len(statements),
        "accounts": len(accounts),
        "writers": len(writers),
        "distributions": db.query(Distribution).count(),
    }
    return {
        "ok": all(c == 0 for c in counts.values()),
        "checked": checked,
        "violation_counts": counts,
        "violations": violations,
    }
