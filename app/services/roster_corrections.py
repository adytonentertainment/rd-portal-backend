"""Apply a corrected roster spreadsheet: the export (roster_export), edited.

RD asked for this (October 2026): export the clients with their beneficiary
codes, let Daniel correct them, upload the corrections in bulk. `plan` reads
the sheet and says exactly what would change, and what blocks it; `apply` runs
the same plan in one transaction, and only when nothing blocks it.

Deliberately additive. An upload can create entries, move codes (statements
follow, as with Move), edit fields, add contacts and replace an entry's
alternate names — it never deletes an entry, never takes a code away without
giving it to someone, and never removes anyone's portal access. A code listed
on no row stays where it is. And it refuses the mistake that started this: a
commission code on a client, or a writer code on a commission partner.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import openpyxl
from sqlalchemy.orm import Session

from app.models.statements import (
    AliasSource,
    BeneficiaryAccount,
    Cadence,
    Catalog,
    Contact,
    ContactRole,
    Publisher,
    Statement,
    Writer,
    WriterAlias,
    WriterContact,
    WriterKind,
    WriterStatus,
)
from app.services.integrity import catalog_for_code, is_commission_code
from app.services.roster_export import NAME_SEP, kind_label

REQUIRED = ["Entry ID", "Name", "Type", "Writer codes", "Commission codes"]
KINDS = {"client": WriterKind.CLIENT, "commission partner": WriterKind.COMMISSION_PARTNER,
         "commission_partner": WriterKind.COMMISSION_PARTNER}
CATALOGS = {"MECH", "YT", "PERF"}
CADENCES = {"semiannual", "quarterly", "mixed"}
LANGS = {"en", "es"}
_SPLIT = re.compile(r"[,;\s]+")


class CorrectionsRefused(ValueError):
    """The sheet has problems; nothing was applied."""


@dataclass
class Row:
    n: int
    entry_id: Optional[int]
    name: str
    kind_text: str
    payee: Optional[str]
    writer_codes: List[str]
    commission_codes: List[str]
    aliases: Optional[List[str]]
    catalogs: Optional[List[str]]
    cadence: Optional[str]
    language: Optional[str]
    contacts: Optional[List[str]]


@dataclass
class Plan:
    rows: int = 0
    ops: List[dict] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def summary(self) -> dict:
        counts: Dict[str, int] = {}
        for op in self.ops:
            counts[op["op"]] = counts.get(op["op"], 0) + 1
        return {
            "rows": self.rows,
            "changes": [{"op": o["op"], "text": o["text"]} for o in self.ops],
            "counts": counts,
            "warnings": self.warnings,
            "problems": self.problems,
        }


def _cell(r, idx, col) -> str:
    i = idx.get(col)
    if i is None or i >= len(r) or r[i] is None:
        return ""
    v = r[i]
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def parse(data: bytes) -> (List[Row], List[str]):
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        return [], ["This is not a spreadsheet the portal can read. Upload the roster export (.xlsx)."]
    ws = wb["Roster"] if "Roster" in wb.sheetnames else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = next(it, None) or ()
    idx = {str(h).strip(): i for i, h in enumerate(header) if h is not None}
    missing = [c for c in REQUIRED if c not in idx]
    if missing:
        return [], [f"Not a roster export: missing column(s) {', '.join(missing)}."]
    rows: List[Row] = []
    problems: List[str] = []
    for n, r in enumerate(it, start=2):
        if not any(v not in (None, "") for v in r):
            continue
        raw_id = _cell(r, idx, "Entry ID")
        entry_id = None
        if raw_id:
            try:
                entry_id = int(raw_id)
            except ValueError:
                problems.append(f"Row {n}: Entry ID '{raw_id}' is not a number.")
                continue

        def codes(col):
            return [c.upper() for c in _SPLIT.split(_cell(r, idx, col)) if c]

        def optional(col, split=None):
            if col not in idx:
                return None
            v = _cell(r, idx, col)
            if split is None:
                return v
            return [x.strip() for x in split(v) if x.strip()]

        rows.append(Row(
            n=n,
            entry_id=entry_id,
            name=_cell(r, idx, "Name"),
            kind_text=_cell(r, idx, "Type"),
            payee=optional("Payee"),
            writer_codes=codes("Writer codes"),
            commission_codes=codes("Commission codes"),
            aliases=optional("Alternate names", lambda v: v.split(NAME_SEP.strip())),
            catalogs=optional("Catalogs", _SPLIT.split),
            cadence=optional("Cadence"),
            language=optional("Language"),
            contacts=optional("Contacts", _SPLIT.split),
        ))
    return rows, problems


def plan(db: Session, data: bytes) -> Plan:
    rows, problems = parse(data)
    p = Plan(rows=len(rows), problems=list(problems))
    if not rows and not problems:
        p.problems.append("The Roster sheet has no rows.")
    writers = {w.id: w for w in db.query(Writer).all()}
    accounts = {a.account_code.upper(): a for a in db.query(BeneficiaryAccount).all()}
    owned: Dict[int, Set[str]] = {}
    for code, a in accounts.items():
        owned.setdefault(a.writer_id, set()).add(code)
    aliases: Dict[int, Set[str]] = {}
    for al in db.query(WriterAlias).all():
        aliases.setdefault(al.writer_id, set()).add(al.alias_name)
    linked: Dict[int, Set[str]] = {}
    for link, c in db.query(WriterContact, Contact).join(Contact, Contact.id == WriterContact.contact_id):
        linked.setdefault(link.writer_id, set()).add((c.email or "").lower())
    statements = _statement_counts(db)

    listed_on: Dict[str, int] = {}
    for row in rows:
        kind = KINDS.get(row.kind_text.lower()) if row.kind_text else None
        if row.kind_text and kind is None:
            p.problems.append(f"Row {row.n}: Type '{row.kind_text}' is not Client or Commission partner.")
            continue
        if row.entry_id is not None:
            w = writers.get(row.entry_id)
            if w is None or w.status == WriterStatus.OFFBOARDED:
                p.problems.append(f"Row {row.n}: entry #{row.entry_id} is not on the roster.")
                continue
            ref, label = w.id, f"#{w.id} {w.canonical_name}"
            kind = kind or w.kind
            if row.name and row.name != w.canonical_name:
                p.ops.append({"op": "update", "ref": ref, "field": "canonical_name", "value": row.name,
                              "text": f"Rename {label} to “{row.name}”"})
            if kind != w.kind:
                p.ops.append({"op": "update", "ref": ref, "field": "kind", "value": kind.value,
                              "text": f"Change {label} from {kind_label(w.kind)} to {kind_label(kind)}"})
            _field_ops(p, row, w, ref, label)
        else:
            if not row.name or kind is None:
                p.problems.append(f"Row {row.n}: a new entry needs a Name and a Type.")
                continue
            ref, label = f"row{row.n}", f"new {kind_label(kind).lower()} “{row.name}”"
            p.ops.append({"op": "create", "ref": ref, "name": row.name, "kind": kind.value,
                          "payee": row.payee or None, "catalogs": _catalogs(p, row),
                          "cadence": _choice(p, row, "cadence", CADENCES),
                          "language": _choice(p, row, "language", LANGS),
                          "text": f"Create {label} (row {row.n})"})
            w = None

        for col, codes in (("Writer codes", row.writer_codes), ("Commission codes", row.commission_codes)):
            for code in codes:
                commission = is_commission_code(code)
                if commission != (col == "Commission codes"):
                    p.problems.append(f"Row {row.n}: {code} is a {'commission' if commission else 'writer'} code; "
                                      f"put it under {'Commission' if commission else 'Writer'} codes.")
                    continue
                need = WriterKind.COMMISSION_PARTNER if commission else WriterKind.CLIENT
                acct = accounts.get(code)
                if acct is not None and acct.writer_id == ref:
                    # Already here: today's state, not a change. The integrity
                    # issues column says if it is wrong; it doesn't block.
                    listed_on.setdefault(code, row.n)
                    continue
                if kind != need:
                    p.problems.append(
                        f"Row {row.n}: {code} is a {'commission' if commission else 'writer'} code on a "
                        f"{kind_label(kind).lower()}. "
                        + ("Commission codes go on a commission-partner entry — add a row for it."
                           if commission else "Writer codes go on the client entry."))
                    continue
                if code in listed_on:
                    p.problems.append(f"{code} is listed on rows {listed_on[code]} and {row.n}; it can only have one owner.")
                    continue
                listed_on[code] = row.n
                if acct is None:
                    if catalog_for_code(code) is None:
                        p.problems.append(f"Row {row.n}: {code} is not a code the portal recognises (C…, CS…, CSJ…, JN…).")
                        continue
                    p.ops.append({"op": "add_code", "ref": ref, "code": code,
                                  "text": f"Add code {code} to {label}"})
                elif acct.writer_id != ref:
                    owner = writers.get(acct.writer_id)
                    n_st = statements.get(acct.id, 0)
                    p.ops.append({"op": "move", "ref": ref, "code": code, "account_id": acct.id,
                                  "text": f"Move {code} ({n_st} statement{'s' if n_st != 1 else ''}) from "
                                          f"#{acct.writer_id} {owner.canonical_name if owner else '?'} to {label}"})

        if row.aliases is not None:
            current = (aliases.get(w.id, set()) - {w.canonical_name}) if w else set()
            wanted = set(row.aliases) - {row.name}
            for a in sorted(current - wanted):
                p.ops.append({"op": "remove_alias", "ref": ref, "alias": a,
                              "text": f"Remove alternate name “{a}” from {label}"})
            for a in sorted(wanted - current):
                p.ops.append({"op": "add_alias", "ref": ref, "alias": a,
                              "text": f"Add alternate name “{a}” to {label}"})

        if row.contacts is not None:
            have = linked.get(w.id, set()) if w else set()
            for email in row.contacts:
                e = email.lower()
                if "@" not in e:
                    p.problems.append(f"Row {row.n}: '{email}' is not an email address.")
                elif e not in have:
                    p.ops.append({"op": "add_contact", "ref": ref, "email": e,
                                  "text": f"Add contact {e} to {label}"})
                    have = have | {e}

        # Codes the entry holds that its row no longer lists, and no row claims.
        if w is not None:
            left = sorted(c for c in owned.get(w.id, set())
                          if c not in row.writer_codes + row.commission_codes and c not in listed_on)
            if left:
                p.warnings.append(f"Row {row.n}: {label} keeps {', '.join(left)} — not listed on any row, "
                                  f"so it stays where it is.")

    # Codes taken off one row and listed nowhere are reported above; codes
    # listed on a LATER row were not yet known when the earlier row was read.
    p.warnings = [w for w in p.warnings if not _all_claimed(w, listed_on)]
    order = {"create": 0, "update": 1, "add_code": 2, "move": 2, "remove_alias": 3, "add_alias": 4, "add_contact": 5}
    p.ops.sort(key=lambda o: order[o["op"]])
    return p


def _all_claimed(warning: str, listed_on: Dict[str, int]) -> bool:
    m = re.search(r"keeps (.+?) — not listed", warning)
    return bool(m) and all(c.strip() in listed_on for c in m.group(1).split(","))


def _statement_counts(db: Session) -> Dict[int, int]:
    from sqlalchemy import func

    return dict(db.query(Statement.account_id, func.count(Statement.id)).group_by(Statement.account_id).all())


def _catalogs(p: Plan, row: Row) -> Optional[List[str]]:
    if row.catalogs is None:
        return None
    vals = [c.upper() for c in row.catalogs]
    bad = [c for c in vals if c not in CATALOGS]
    if bad:
        p.problems.append(f"Row {row.n}: catalog(s) {', '.join(bad)} — use MECH, YT, PERF.")
        return None
    return vals


def _choice(p: Plan, row: Row, attr: str, allowed: Set[str]) -> Optional[str]:
    v = getattr(row, attr)
    if v is None or v == "":
        return None
    v = v.lower()
    if v not in allowed:
        p.problems.append(f"Row {row.n}: {attr} '{getattr(row, attr)}' — use {', '.join(sorted(allowed))}.")
        return None
    return v


def _field_ops(p: Plan, row: Row, w: Writer, ref: int, label: str) -> None:
    if row.payee is not None and row.payee != (w.payee_name or ""):
        p.ops.append({"op": "update", "ref": ref, "field": "payee_name", "value": row.payee or None,
                      "text": f"Set payee of {label} to “{row.payee or '—'}”"})
    cats = _catalogs(p, row)
    if cats is not None and sorted(cats) != sorted(w.expected_catalogs or []):
        p.ops.append({"op": "update", "ref": ref, "field": "expected_catalogs", "value": cats,
                      "text": f"Set catalogs of {label} to {', '.join(cats) or '—'}"})
    cad = _choice(p, row, "cadence", CADENCES)
    if cad and cad != (w.cadence.value if w.cadence else None):
        p.ops.append({"op": "update", "ref": ref, "field": "cadence", "value": cad,
                      "text": f"Set cadence of {label} to {cad}"})
    lang = _choice(p, row, "language", LANGS)
    if lang and lang != (w.preferred_language or None):
        p.ops.append({"op": "update", "ref": ref, "field": "preferred_language", "value": lang,
                      "text": f"Set language of {label} to {lang.upper()}"})


def apply(db: Session, data: bytes, admin_id: Optional[int] = None) -> dict:
    p = plan(db, data)
    if p.problems:
        raise CorrectionsRefused("; ".join(p.problems[:5]))
    refs: Dict[str, int] = {}
    publisher = db.query(Publisher).order_by(Publisher.id).first()

    def target(ref) -> Writer:
        return db.get(Writer, refs.get(ref, ref))

    for op in p.ops:
        kind = op["op"]
        if kind == "create":
            k = WriterKind(op["kind"])
            w = Writer(
                publisher_id=publisher.id if publisher else None,
                canonical_name=op["name"],
                kind=k,
                payee_name=op["payee"],
                expected_catalogs=op["catalogs"],
                cadence=Cadence(op["cadence"]) if op["cadence"] else Cadence.SEMIANNUAL,
                preferred_language=op["language"],
                is_client=k == WriterKind.CLIENT,
                is_commission_partner=k == WriterKind.COMMISSION_PARTNER,
            )
            db.add(w)
            db.flush()
            refs[op["ref"]] = w.id
        elif kind == "update":
            w = target(op["ref"])
            value = op["value"]
            if op["field"] == "kind":
                value = WriterKind(value)
                w.is_client = value == WriterKind.CLIENT
                w.is_commission_partner = value == WriterKind.COMMISSION_PARTNER
            elif op["field"] == "cadence":
                value = Cadence(value)
            setattr(w, op["field"], value)
        elif kind == "add_code":
            w = target(op["ref"])
            db.add(BeneficiaryAccount(writer_id=w.id, account_code=op["code"],
                                      catalog=catalog_for_code(op["code"]) or Catalog.YT, cadence=w.cadence))
        elif kind == "move":
            w = target(op["ref"])
            acct = db.get(BeneficiaryAccount, op["account_id"])
            acct.writer = w
        elif kind == "remove_alias":
            w = target(op["ref"])
            db.query(WriterAlias).filter(WriterAlias.writer_id == w.id,
                                         WriterAlias.alias_name == op["alias"]).delete(synchronize_session=False)
        elif kind == "add_alias":
            w = target(op["ref"])
            db.add(WriterAlias(writer_id=w.id, alias_name=op["alias"], source=AliasSource.MANUAL))
        elif kind == "add_contact":
            w = target(op["ref"])
            contact = db.query(Contact).filter(Contact.email == op["email"]).first()
            if contact is None:
                contact = Contact(email=op["email"])
                db.add(contact)
                db.flush()
            first = db.query(WriterContact).filter(WriterContact.writer_id == w.id).first() is None
            db.add(WriterContact(writer_id=w.id, contact_id=contact.id,
                                 role=ContactRole.PRIMARY if first else ContactRole.MANAGER))
        db.flush()
    db.commit()
    summary = p.summary()
    summary["applied"] = len(p.ops)
    summary["created_ids"] = sorted(refs.values())
    return summary
