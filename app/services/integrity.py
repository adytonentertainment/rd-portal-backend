"""Roster integrity: who owns which statements, checked against the rules RD
works by. Read-only; safe to run against production.

Every check here exists because its failure reached RD (October 2026):

* A person on BOTH client-list sheets is two entries — a client for their own
  catalog (C… / JN… codes) and a commission partner (CS… / CSJ… codes). An
  early import merged them by name, so commission statements sat on the
  writer's account. The importer was fixed; the merged rows were not split,
  and nothing noticed. MIXED_ROLES and KIND_MISMATCH notice.
* A fuzzy match filed the client "Rata Blanca (Adrian Barilari)" as a mere
  alias of Akwid. The next import would have handed Barilari's statements to
  Akwid. ROW_ABSORBED and ALIAS_UNRELATED notice.

Severity: "error" is wrong and must be fixed; "warning" needs a human look.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from types import SimpleNamespace
from typing import Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from app.models.statements import (
    BeneficiaryAccount,
    ClientImport,
    Distribution,
    Statement,
    Writer,
    WriterAlias,
    WriterContact,
    WriterKind,
    WriterStatus,
)
from app.services.client_import.matcher import normalize


def is_commission_code(code: str) -> bool:
    """Commission statements carry beneficiary codes starting with CS."""
    return (code or "").upper().startswith("CS")


def catalog_for_code(code: str):
    """Mechanical codes start JN… (commission: CSJ…), YouTube codes C… (CS…)."""
    from app.models.statements import Catalog

    code = (code or "").upper()
    if code.startswith("CSJ") or code.startswith("JN"):
        return Catalog.MECH
    if code.startswith("C"):
        return Catalog.YT
    return None


@dataclass
class Finding:
    check: str
    severity: str
    writer_id: Optional[int]
    writer: str
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


def _kind(w: Writer) -> Optional[str]:
    return w.kind.value if w.kind is not None else None


def check_roles(writers: Iterable[Writer], codes: Dict[int, List[str]]) -> List[Finding]:
    out = []
    for w in writers:
        mine = codes.get(w.id, [])
        if not mine:
            continue
        comm = sorted(c for c in mine if is_commission_code(c))
        own = sorted(c for c in mine if not is_commission_code(c))
        if comm and own:
            out.append(Finding(
                "MIXED_ROLES", "error", w.id, w.canonical_name,
                f"holds writer codes {', '.join(own)} AND commission codes {', '.join(comm)}; "
                f"the commission codes belong on a separate commission-partner entry",
            ))
        elif comm and w.kind == WriterKind.CLIENT:
            out.append(Finding(
                "KIND_MISMATCH", "error", w.id, w.canonical_name,
                f"is a client but holds only commission codes ({', '.join(comm)})",
            ))
        elif own and w.kind == WriterKind.COMMISSION_PARTNER:
            out.append(Finding(
                "KIND_MISMATCH", "error", w.id, w.canonical_name,
                f"is a commission partner but holds only writer codes ({', '.join(own)})",
            ))
    return out


def _tokens(name: Optional[str]) -> set:
    return {t for t in normalize(name or "").split() if len(t) > 1}


def check_aliases(writers: Dict[int, Writer], aliases: Iterable[WriterAlias]) -> List[Finding]:
    """An alias is another name for THIS client. One that names a different
    roster entry, or shares nothing with the client's name or payee, is how a
    separate client gets swallowed."""
    by_name: Dict[str, List[Writer]] = defaultdict(list)
    for w in writers.values():
        if w.status != WriterStatus.OFFBOARDED:
            by_name[normalize(w.canonical_name)].append(w)
    out = []
    for al in aliases:
        w = writers.get(al.writer_id)
        if w is None or w.status == WriterStatus.OFFBOARDED:
            continue
        others = [o for o in by_name.get(normalize(al.alias_name), []) if o.id != w.id and o.kind == w.kind]
        if others:
            out.append(Finding(
                "ALIAS_OF_OTHER", "error", w.id, w.canonical_name,
                f'alias "{al.alias_name}" is the name of another entry (#{others[0].id})',
            ))
            continue
        alias_t = _tokens(al.alias_name)
        if alias_t and not alias_t & (_tokens(w.canonical_name) | _tokens(w.payee_name)):
            out.append(Finding(
                "ALIAS_UNRELATED", "warning", w.id, w.canonical_name,
                f'alias "{al.alias_name}" shares nothing with the name or payee "{w.payee_name or "—"}"',
            ))
    return out


def check_client_list(writers: Dict[int, Writer], aliases: Iterable[WriterAlias], rows) -> List[Finding]:
    """Every client-list row should be an entry of its own, of its own kind."""
    entries: Dict[tuple, Writer] = {}
    for w in writers.values():
        if w.status != WriterStatus.OFFBOARDED and w.kind is not None:
            entries[(normalize(w.canonical_name), w.kind)] = w
    alias_owner: Dict[str, Writer] = {}
    for al in aliases:
        w = writers.get(al.writer_id)
        if w is not None:
            alias_owner.setdefault(normalize(al.alias_name), w)
    out = []
    for r in rows:
        # Parsed rows carry the parser's own enum, stored rows a plain string.
        kind = WriterKind(getattr(r.kind, "value", r.kind))
        key = (normalize(r.name), kind)
        if key in entries:
            continue
        owner = alias_owner.get(key[0])
        sheet = "client" if kind == WriterKind.CLIENT else "commission partner"
        if owner is not None and normalize(owner.canonical_name) != key[0]:
            out.append(Finding(
                "ROW_ABSORBED", "error", owner.id, owner.canonical_name,
                f'{sheet} "{r.name}" has no entry of its own; it is only an alias of this one',
            ))
        else:
            out.append(Finding(
                "ROW_MISSING", "error", None, r.name,
                f"{sheet} on the client list (sheet row {r.row_no}) has no {sheet} entry",
            ))
    return out


def check_codes(accounts: Iterable[BeneficiaryAccount]) -> List[Finding]:
    seen: Dict[str, BeneficiaryAccount] = {}
    out = []
    for a in accounts:
        code = (a.account_code or "").strip().upper()
        if code in seen and seen[code].writer_id != a.writer_id:
            out.append(Finding(
                "DUPLICATE_CODE", "error", a.writer_id, "",
                f"code {code} is on entry #{seen[code].writer_id} and #{a.writer_id}",
            ))
        seen.setdefault(code, a)
    return out


def check_published_to_placeholders(db: Session, writers: Dict[int, Writer]) -> List[Finding]:
    """Statements visible in a portal that no real client owns."""
    rows = (
        db.query(BeneficiaryAccount.writer_id, BeneficiaryAccount.account_code)
        .join(Statement, Statement.account_id == BeneficiaryAccount.id)
        .join(Distribution, Distribution.statement_id == Statement.id)
        .filter(Distribution.portal_visible.is_(True))
        .distinct()
        .all()
    )
    by_writer: Dict[int, set] = defaultdict(set)
    for wid, code in rows:
        w = writers.get(wid)
        if w is not None and w.kind is None:
            by_writer[wid].add(code)
    return [
        Finding("PUBLISHED_TO_PLACEHOLDER", "warning", wid, writers[wid].canonical_name,
                f"published statements under {', '.join(sorted(codes))} belong to no client")
        for wid, codes in sorted(by_writer.items())
    ]


def check_access(db: Session, writers: Dict[int, Writer]) -> List[Finding]:
    """A claimed portal login on an offboarded client still reads its data."""
    out = []
    for link in db.query(WriterContact).filter(WriterContact.user_id.isnot(None)).all():
        w = writers.get(link.writer_id)
        if w is not None and w.status == WriterStatus.OFFBOARDED:
            out.append(Finding("ACCESS_ON_OFFBOARDED", "warning", w.id, w.canonical_name,
                               f"contact #{link.contact_id} still has portal access"))
    return out


def client_rows_from_last_import(db: Session):
    """The rows of the most recently applied client list, as stored with the
    import — so the roster can be checked against it without the file."""
    imp = (
        db.query(ClientImport)
        .filter(ClientImport.applied_at.isnot(None))
        .order_by(ClientImport.applied_at.desc())
        .first()
    )
    if imp is None or not imp.diff:
        return None
    return [
        SimpleNamespace(name=r.get("name") or "", kind=r.get("kind"), row_no=r.get("row_no"))
        for r in imp.diff.get("rows", [])
        if r.get("name") and r.get("kind") in ("client", "commission_partner")
    ]


def audit(db: Session, client_rows=None) -> List[Finding]:
    writers = {w.id: w for w in db.query(Writer).all()}
    accounts = db.query(BeneficiaryAccount).all()
    codes: Dict[int, List[str]] = defaultdict(list)
    for a in accounts:
        codes[a.writer_id].append(a.account_code)
    aliases = db.query(WriterAlias).all()
    active = [w for w in writers.values() if w.status != WriterStatus.OFFBOARDED and w.kind is not None]
    findings: List[Finding] = []
    findings += check_roles(active, codes)
    findings += check_aliases(writers, aliases)
    if client_rows is not None:
        findings += check_client_list(writers, aliases, client_rows)
    findings += check_codes(accounts)
    findings += check_published_to_placeholders(db, writers)
    findings += check_access(db, writers)
    order = {"error": 0, "warning": 1}
    findings.sort(key=lambda f: (order.get(f.severity, 2), f.check, f.writer.lower()))
    return findings
