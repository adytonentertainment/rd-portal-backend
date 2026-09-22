#!/usr/bin/env python
"""Backfill: repair mis-encoded song titles already in the database.

The parser now repairs the source's mojibake on ingest (see
app/services/statement_ingest/text_repair), but everything ingested BEFORE
that is still sitting in statement_line — and it is what writers see in their
portal today. This repairs those rows in place.

Runs per STATEMENT, because that is the unit the encoding is consistent over:
one statement came from one exported file written with one encoding. Detection
uses every title in the statement, then each individual title is only touched
if it actually shows the fault (a file holds correct and corrupt titles side
by side — see the module docstring).

Only text is touched. No money, no identifiers, no reconciliation input.

    python scripts/repair_statement_text.py --dry-run     # report, change nothing
    python scripts/repair_statement_text.py               # apply
    python scripts/repair_statement_text.py --statement 42

Safe to re-run: a repaired title no longer carries an artifact, so a second
pass detects nothing and changes nothing.
"""

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENVIRONMENT", "DEVELOPMENT")

from app.database.session import SessionLocal  # noqa: E402
from app.models.statements import Statement, StatementLine  # noqa: E402
from app.services.statement_ingest.text_repair import (  # noqa: E402
    detect_encoding_fault,
    repair_text,
)

TEXT_FIELDS = ("song_title", "channel", "income_source", "income_type")


def repair_statement(session, statement_id, dry_run=False):
    """Repair one statement's lines. Returns (fault, values_changed)."""
    lines = (
        session.query(StatementLine)
        .filter(StatementLine.statement_id == statement_id)
        .all()
    )
    if not lines:
        return None, 0

    fault = detect_encoding_fault(
        getattr(line, field) for line in lines for field in TEXT_FIELDS
    )
    if fault is None:
        return None, 0

    changed = 0
    for line in lines:
        for field in TEXT_FIELDS:
            original = getattr(line, field)
            repaired = repair_text(original, fault)
            if repaired != original:
                if not dry_run:
                    setattr(line, field, repaired)
                changed += 1
    return fault, changed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only")
    ap.add_argument("--statement", type=int, help="one statement id")
    ap.add_argument("--limit", type=int, help="stop after N statements")
    ap.add_argument("--samples", type=int, default=8, help="example repairs to print")
    args = ap.parse_args()

    session = SessionLocal()
    try:
        q = session.query(Statement.id).order_by(Statement.id)
        if args.statement:
            q = q.filter(Statement.id == args.statement)
        ids = [row[0] for row in q.all()]
        if args.limit:
            ids = ids[: args.limit]

        print(f"{'DRY RUN — ' if args.dry_run else ''}scanning {len(ids)} statement(s)")

        faults = Counter()
        total_changed = 0
        touched = 0
        # Distinct titles already shown. A title repeats across every line it
        # earned on, so without this the sample is the same song ten times and
        # tells the operator nothing about the range of what is being changed.
        seen_samples = set()

        for sid in ids:
            # Capture a couple of before/afters for the operator to eyeball —
            # a silent "repaired 4,812 values" is not something anyone can check.
            before = set()
            if len(seen_samples) < args.samples:
                for line in (
                    session.query(StatementLine)
                    .filter(StatementLine.statement_id == sid)
                    .limit(400)
                    .all()
                ):
                    if line.song_title:
                        before.add(line.song_title)

            fault, changed = repair_statement(session, sid, dry_run=args.dry_run)
            if not changed:
                continue

            faults[fault] += 1
            total_changed += changed
            touched += 1

            for old_title in sorted(before):
                if len(seen_samples) >= args.samples:
                    break
                new_title = repair_text(old_title, fault)
                if new_title != old_title and old_title not in seen_samples:
                    seen_samples.add(old_title)
                    print(f"   [{fault}] {old_title!r}\n        -> {new_title!r}")

            if not args.dry_run and touched % 50 == 0:
                session.commit()

        if not args.dry_run:
            session.commit()

        print()
        print(f"statements repaired : {touched}")
        print(f"values changed      : {total_changed}")
        for fault, n in faults.most_common():
            print(f"   {fault}: {n} statement(s)")
        if args.dry_run:
            print("\nNothing was written (--dry-run).")
    finally:
        session.close()


if __name__ == "__main__":
    main()
