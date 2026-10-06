#!/usr/bin/env python
"""Backfill the mis-encoded song titles, over a REMOTE database connection.

Same repair as scripts/repair_statement_text.py and the same detection
functions — this differs only in how it reaches the rows.

The other script loads every StatementLine for a statement as ORM objects. On
a local SQLite file that is fine; against Render's Postgres over the public
internet it means pulling 6.75 million objects across the wire to change
roughly twenty thousand of them, which is slow enough to be unusable and holds
a transaction open the whole time.

This one works on DISTINCT (statement_id, song_title) pairs instead — about
74k rows rather than 6.75M — which is all the detection needs, since the fault
is a property of the text and the file it came from, not of the individual
royalty line. Repairs are then applied by value:

    UPDATE statement_line SET song_title = :new
     WHERE statement_id = :sid AND song_title = :old

so only rows that actually change are written, every duplicate of a title is
fixed in one statement, and nothing else in the table is touched.

Text only. No money, no identifiers, no reconciliation inputs.

    python scripts/repair_statement_text_remote.py --dry-run
    python scripts/repair_statement_text_remote.py
"""

import argparse
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("ENVIRONMENT", "DEVELOPMENT")

import sqlalchemy as sa  # noqa: E402

from app.services.statement_ingest.text_repair import (  # noqa: E402
    detect_encoding_fault,
    repair_text,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--samples", type=int, default=10, help="example repairs to print")
    ap.add_argument("--url", default=os.getenv("SQLALCHEMY_DATABASE_URL"),
                    help="database URL (defaults to SQLALCHEMY_DATABASE_URL)")
    args = ap.parse_args()

    url = (args.url or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if not url:
        sys.exit("No database URL. Set SQLALCHEMY_DATABASE_URL or pass --url.")

    engine = sa.create_engine(url, connect_args={"connect_timeout": 30})

    print(f"{'DRY RUN - ' if args.dry_run else ''}reading distinct titles...", flush=True)
    with engine.connect() as conn:
        pairs = conn.execute(sa.text(
            "select distinct statement_id, song_title from statement_line "
            "where song_title is not null"
        )).fetchall()
    print(f"  {len(pairs):,} distinct (statement, title) pairs", flush=True)

    by_statement = defaultdict(list)
    for sid, title in pairs:
        by_statement[sid].append(title)

    # Detection is per statement: one statement came from one exported file
    # written with one encoding. repair_text then decides per string, because a
    # file holds correct and corrupt titles side by side.
    edits = []          # (statement_id, old, new)
    faults = Counter()
    for sid, titles in by_statement.items():
        fault = detect_encoding_fault(titles)
        if fault is None:
            continue
        changed_here = False
        for old in titles:
            new = repair_text(old, fault)
            if new != old:
                edits.append((sid, old, new))
                changed_here = True
        if changed_here:
            faults[fault] += 1

    print(f"  {len(edits):,} distinct titles to repair across "
          f"{len(set(s for s, _, _ in edits)):,} statements", flush=True)
    for fault, n in faults.most_common():
        print(f"     {fault}: {n} statement(s)")

    print("\n  samples:")
    seen = set()
    for sid, old, new in edits:
        if old in seen:
            continue
        seen.add(old)
        print(f"     {old!r}\n       -> {new!r}")
        if len(seen) >= args.samples:
            break

    if args.dry_run:
        print("\nNothing was written (--dry-run).")
        return

    print("\napplying...", flush=True)
    written = 0
    rows_touched = 0
    with engine.begin() as conn:
        for i, (sid, old, new) in enumerate(edits, 1):
            res = conn.execute(
                sa.text("update statement_line set song_title = :new "
                        "where statement_id = :sid and song_title = :old"),
                {"new": new, "old": old, "sid": sid},
            )
            rows_touched += res.rowcount or 0
            written += 1
            if i % 500 == 0:
                print(f"     {i:,}/{len(edits):,} titles, {rows_touched:,} rows", flush=True)

    print(f"\ndone: {written:,} distinct titles repaired, {rows_touched:,} rows updated")


if __name__ == "__main__":
    main()
