"""One-off fix (October 2026): split the entries the first client-list import
merged, and move C00122 to Rata Blanca (Adrian Barilari). Kenny's points 1 and 3.

A person on both client-list sheets is two entries: a client (writer codes
C…/JN…) and a commission partner (commission codes CS…/CSJ…). The first import
merged them by name. For each merged entry this moves the codes of the OTHER
role to that person's entry of that role:

  * an existing active entry with the same name and that role, else
  * a new entry made from the client-list row with that exact name and role
    (contacts, payee, catalogs, language, cadence from the row).

Only the clear-cut cases. Anything else — no entry and no row for the other
role, more than one candidate — is skipped and listed for RD. Codes move the
way Move does: statements follow the code; nothing has been published on
these entries. Acquired catalogs (publisher_owned) keep their writer codes;
their commission codes go to the writer's commission entry.

Dry run by default (prints the plan, changes nothing):
    python scripts/fix_merged_accounts.py
    python scripts/fix_merged_accounts.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.database import SessionLocal  # noqa: E402
from app.models.statements import (  # noqa: E402
    AliasSource,
    BeneficiaryAccount,
    Cadence,
    Contact,
    ContactRole,
    Writer,
    WriterAlias,
    WriterContact,
    WriterKind,
    WriterStatus,
)
from app.services.client_import.matcher import normalize  # noqa: E402
from app.services.integrity import audit, client_rows_from_last_import, is_commission_code  # noqa: E402

# RD's own house account mixes kinds by design.
NOT_A_CLIENT = {"Regalias Digitales, LLC"}
BARILARI = ("C00122", "Rata Blanca (Adrian Barilari)")


def last_import_rows(db):
    """The full stored rows of the last applied client list (with contacts)."""
    from app.models.statements import ClientImport

    imp = (
        db.query(ClientImport)
        .filter(ClientImport.applied_at.isnot(None))
        .order_by(ClientImport.applied_at.desc())
        .first()
    )
    return (imp.diff or {}).get("rows", []) if imp else []


def move(db, account, target, log):
    owner = db.get(Writer, account.writer_id)
    log.append(f"MOVE   {account.account_code}: #{owner.id} {owner.canonical_name} [{owner.kind.value}] "
               f"-> #{target.id or 'new'} {target.canonical_name} [{target.kind.value}]")
    account.writer = target
    # As Move does: remember the name this code files under.
    filed_as = account.display_name
    taken = {normalize(n) for (n,) in db.query(Writer.canonical_name).filter(
        Writer.id != target.id, Writer.status != WriterStatus.OFFBOARDED)}
    if filed_as and filed_as != target.canonical_name and target.id and normalize(filed_as) not in taken:
        if not db.query(WriterAlias).filter_by(writer_id=target.id, alias_name=filed_as).first():
            db.add(WriterAlias(writer_id=target.id, alias_name=filed_as, source=AliasSource.MANUAL))


def entry_from_row(db, row, kind, like: Writer, log):
    w = Writer(
        publisher_id=like.publisher_id,
        canonical_name=row["name"].strip(),
        kind=kind,
        payee_name=row.get("payee_name"),
        expected_catalogs=row.get("catalogs") or None,
        preferred_language=row.get("language"),
        cadence=Cadence.QUARTERLY if row.get("quarterly") else Cadence.SEMIANNUAL,
        is_client=kind == WriterKind.CLIENT,
        is_commission_partner=kind == WriterKind.COMMISSION_PARTNER,
    )
    db.add(w)
    db.flush()
    for i, email in enumerate(row.get("emails") or []):
        email = email.strip().lower()
        contact = db.query(Contact).filter(Contact.email == email).first()
        if contact is None:
            contact = Contact(email=email, preferred_language=row.get("language"))
            db.add(contact)
            db.flush()
        db.add(WriterContact(writer_id=w.id, contact_id=contact.id,
                             role=ContactRole.PRIMARY if i == 0 else ContactRole.MANAGER))
    db.add(WriterAlias(writer_id=w.id, alias_name=w.canonical_name, source=AliasSource.MANUAL))
    log.append(f"CREATE #{w.id} {w.canonical_name} [{kind.value}] from client-list row "
               f"{row.get('sheet')} {row.get('row_no')} ({len(row.get('emails') or [])} contact(s))")
    return w


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="commit the changes (default: dry run)")
    args = ap.parse_args()

    db = SessionLocal()
    log, skipped = [], []
    rows = last_import_rows(db)
    by_name_kind = {}
    for r in rows:
        by_name_kind.setdefault((normalize(r.get("name") or ""), r.get("kind")), []).append(r)

    found_before = [f for f in audit(db, client_rows=client_rows_from_last_import(db)) if f.severity == "error"]
    before = Counter(f.check for f in found_before)
    # Merged entries, and entries whose type does not match their codes (T.K.:
    # a client holding only commission codes).
    mixed = [f for f in audit(db) if f.check in ("MIXED_ROLES", "KIND_MISMATCH")]
    for f in mixed:
        w = db.get(Writer, f.writer_id)
        if w.canonical_name in NOT_A_CLIENT:
            skipped.append(f"SKIP   #{w.id} {w.canonical_name}: RD's own house account")
            continue
        accts = db.query(BeneficiaryAccount).filter_by(writer_id=w.id).all()
        # Codes that do not belong to this entry's role move to the other role.
        other = WriterKind.COMMISSION_PARTNER if w.kind == WriterKind.CLIENT else WriterKind.CLIENT
        moving = [a for a in accts if is_commission_code(a.account_code) == (other == WriterKind.COMMISSION_PARTNER)]
        # Same person, other role — compared the importer's way ("Mistah Fab"
        # and "Mistah FAB" are one name), never by exact spelling.
        siblings = [
            o for o in db.query(Writer).filter(Writer.kind == other, Writer.status != WriterStatus.OFFBOARDED,
                                               Writer.id != w.id)
            if normalize(o.canonical_name) == normalize(w.canonical_name)
        ]
        if len(siblings) == 1:
            target = siblings[0]
        elif len(siblings) > 1:
            skipped.append(f"SKIP   #{w.id} {w.canonical_name}: {len(siblings)} {other.value} entries with that name")
            continue
        else:
            # The client list decides: the other sheet's row with this name, or
            # failing that, the one row there whose PAYEE is this person
            # ("Los Yes Yes / Julian Tapia Sena", payee Julian Tapia Sena).
            candidates = by_name_kind.get((normalize(w.canonical_name), other.value), [])
            if not candidates:
                me = {normalize(w.canonical_name), normalize(w.payee_name or "")} - {""}
                candidates = [r for r in rows if r.get("kind") == other.value
                              and normalize(r.get("payee_name") or "") in me]
            if len(candidates) != 1:
                skipped.append(f"SKIP   #{w.id} {w.canonical_name}: no {other.value} entry, and "
                               f"{len(candidates)} client-list rows for that role — RD to decide")
                continue
            row = candidates[0]
            existing = [
                o for o in db.query(Writer).filter(Writer.kind == other, Writer.status != WriterStatus.OFFBOARDED)
                if normalize(o.canonical_name) == normalize(row.get("name") or "")
            ]
            if len(existing) > 1:
                skipped.append(f"SKIP   #{w.id} {w.canonical_name}: {len(existing)} entries named '{row.get('name')}'")
                continue
            target = existing[0] if existing else entry_from_row(db, row, other, w, log)
        for a in sorted(moving, key=lambda a: a.account_code):
            move(db, a, target, log)
        db.flush()

    code, name = BARILARI
    acct = db.query(BeneficiaryAccount).filter_by(account_code=code).first()
    dest = db.query(Writer).filter(Writer.canonical_name == name, Writer.status != WriterStatus.OFFBOARDED).all()
    if acct is not None and len(dest) == 1 and acct.writer_id != dest[0].id:
        move(db, acct, dest[0], log)
    else:
        skipped.append(f"SKIP   {code} -> {name}: account {'missing' if acct is None else 'found'}, "
                       f"{len(dest)} destination entr{'y' if len(dest) == 1 else 'ies'}")
    db.flush()

    found_after = [f for f in audit(db, client_rows=client_rows_from_last_import(db)) if f.severity == "error"]
    after = Counter(f.check for f in found_after)
    key = lambda f: (f.check, f.writer_id, f.detail)  # noqa: E731
    was, now = {key(f) for f in found_before}, {key(f) for f in found_after}
    for line in log + skipped:
        print(line)
    print(f"SUMMARY changes={len(log)} skipped={len(skipped)}")
    for check, wid, detail in sorted(now - was, key=str):
        print(f"NEWPROBLEM {check} #{wid}: {detail}")
    print(f"SUMMARY resolved={len(was - now)} new={len(now - was)}")
    print(f"SUMMARY errors before={dict(before)}")
    print(f"SUMMARY errors after ={dict(after)}")
    if args.apply:
        db.commit()
        print("SUMMARY APPLIED")
    else:
        db.rollback()
        print("SUMMARY DRY RUN - nothing changed")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
