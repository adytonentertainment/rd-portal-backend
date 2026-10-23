"""Check the roster against RD's rules (see app/services/integrity.py).

Read-only. Exits 1 when any error-level finding exists, so it can gate a deploy.

    ENVIRONMENT=DEVELOPMENT SQLALCHEMY_DATABASE_URL=sqlite:///verax_livetest.db \\
        python scripts/integrity_audit.py [--client-list "Client List.xlsx"] [--csv out.csv]

On Render (production), as a one-off job:
    python scripts/integrity_audit.py --csv /tmp/integrity.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.database import SessionLocal  # noqa: E402
from app.services.integrity import audit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--client-list", help="RD client list XLSX to check the roster against")
    ap.add_argument("--csv", help="write every finding to this CSV")
    ap.add_argument("--quiet", action="store_true", help="summary only")
    args = ap.parse_args()

    rows = None
    if args.client_list:
        from app.services.client_import.parser import parse_client_list

        rows = parse_client_list(args.client_list)

    db = SessionLocal()
    try:
        if rows is None:
            # No file given: check against the client list last applied.
            from app.services.integrity import client_rows_from_last_import

            rows = client_rows_from_last_import(db)
        findings = audit(db, client_rows=rows)
    finally:
        db.close()

    counts = Counter((f.severity, f.check) for f in findings)
    errors = sum(n for (sev, _), n in counts.items() if sev == "error")
    if not args.quiet:
        for f in findings:
            who = f"#{f.writer_id} {f.writer}" if f.writer_id else f.writer
            print(f"[{f.severity.upper():7}] {f.check:24} {who}: {f.detail}")
    print("\nSummary:")
    for (sev, check), n in sorted(counts.items()):
        print(f"  {sev:7} {check:24} {n}")
    print(f"  {'errors' if errors else 'no errors'}: {errors}, warnings: {len(findings) - errors}")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["severity", "check", "writer_id", "writer", "detail"])
            w.writeheader()
            for f in findings:
                w.writerow({k: getattr(f, k) for k in w.fieldnames})
        print(f"  written: {args.csv}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
