"""Dry-run the client-list importer against real data (infra PRD §3.2).

Builds the statement-account roster straight from the WeTransfer drop's
filenames (no DB needed), parses the client list, and reports coverage:
how many client rows match an account, how many accounts have no client row,
and the validation findings. This is the pre-flight for the real import.

Usage:
    python scripts/client_import_dryrun.py \
        "/path/to/Client List for Verax.xlsx" \
        "/path/to/wetransfer_.../"
"""

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.client_import.matcher import AccountIndex, AccountRef  # noqa: E402
from app.services.client_import.parser import parse_client_list  # noqa: E402
from app.services.statement_ingest.filename_parser import (  # noqa: E402
    parse_statement_filename,
)


def build_roster(drop_dir: str):
    """One AccountRef per (account_code, catalog) seen in the drop."""
    seen = {}
    for root, _dirs, files in os.walk(drop_dir):
        for fn in files:
            parsed = parse_statement_filename(fn)
            if parsed is None:
                continue
            key = (parsed.account_code, parsed.royalty_type)
            if key not in seen:
                seen[key] = AccountRef(
                    account_code=parsed.account_code,
                    display_name=parsed.display_name,
                    catalog=parsed.royalty_type.value if parsed.royalty_type else None,
                    is_house=parsed.is_house,
                )
    return list(seen.values())


def main(client_path: str, drop_dir: str):
    roster = build_roster(drop_dir)
    index = AccountIndex(roster)
    rows = parse_client_list(client_path)

    non_house = [a for a in roster if not a.is_house]
    print(f"Statement accounts (distinct code+catalog): {len(roster)} "
          f"({len(non_house)} non-house)")
    print(f"Client-list rows: {len(rows)}")

    conf = Counter()
    matched_codes = set()
    unmatched_rows = []
    for r in rows:
        m = index.match(r.name, r.payee_name)
        conf[m.confidence] += 1
        if m.matched:
            matched_codes.update(m.account_codes)
        else:
            unmatched_rows.append((r.sheet, r.name, r.payee_name, m.score, m.matched_display))

    print("\n-- Row match confidence --")
    for k in ("exact", "probable", "none"):
        print(f"  {k:9}: {conf[k]}")

    all_codes = {a.account_code for a in non_house}
    unlisted = sorted(all_codes - matched_codes)
    print(f"\nNon-house accounts covered by >=1 client row: "
          f"{len(all_codes & matched_codes)}/{len(all_codes)}")
    print(f"Accounts with NO client row (C-UNLISTED-ACCOUNT queue): {len(unlisted)}")
    print("  e.g.:", unlisted[:12])

    print("\n-- Findings --")
    bad_email = [(r.sheet, r.name) for r in rows
                 if any(not e.is_valid for e in r.emails)]
    bad_cat = [(r.name, r.unknown_catalog_tokens) for r in rows
               if r.unknown_catalog_tokens]
    no_email = [(r.sheet, r.name) for r in rows if not r.valid_emails]
    print(f"  C-BAD-EMAIL: {len(bad_email)} -> {bad_email[:5]}")
    print(f"  C-BAD-CATALOG: {len(bad_cat)} -> {bad_cat}")
    print(f"  C-NO-EMAIL: {len(no_email)} -> {no_email[:5]}")

    print("\n-- Sample unmatched rows (resolution queue) --")
    for s, name, payee, score, near in unmatched_rows[:15]:
        print(f"  [{s[:6]}] {name!r} payee={payee!r} best={score} near={near!r}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
