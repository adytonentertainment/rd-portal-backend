"""Client-list importer: parse -> match -> validate -> diff -> apply.

`preview` computes everything and writes nothing (returns a JSON-able diff +
findings for admin review). `apply` performs the resolution: it creates the
real client Writer identity, re-points matched placeholder accounts to it,
merges the now-empty placeholder writers, and wires Contacts/WriterContacts.
Both are idempotent — re-running yields no new rows (infra PRD §3.2, §7.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.statements import (
    BeneficiaryAccount,
    Cadence,
    Catalog,
    Contact,
    ContactRole,
    Publisher,
    Writer,
    WriterAlias,
    WriterContact,
    WriterKind,
)

from .matcher import AccountIndex, AccountRef, MatchResult, normalize
from .parser import ClientRow, WriterKind as ParsedKind, parse_client_list
from .validator import summarize, validate_rows
import re
from app.services.client_import.matcher import strip_accents

_CATALOG_MAP = {"MECH": Catalog.MECH, "YT": Catalog.YT, "PERF": Catalog.PERF}
_KIND_MAP = {
    ParsedKind.CLIENT: WriterKind.CLIENT,
    ParsedKind.COMMISSION_PARTNER: WriterKind.COMMISSION_PARTNER,
}


def build_index_from_db(db: Session) -> AccountIndex:
    """AccountIndex over every beneficiary account. Matching runs against the
    account's OWN statement-filename identity (display_name) — the current
    owner's canonical_name is only a fallback for legacy rows. Using the owner
    name here made bad merges self-reinforcing: once an account was swept into
    the wrong writer, its real name vanished from the index and the rightful
    client-list row could never claim it back."""
    rows = (
        db.query(BeneficiaryAccount, Writer)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
        .all()
    )
    refs = [
        AccountRef(
            account_code=acct.account_code,
            display_name=acct.display_name or writer.canonical_name,
            catalog=acct.catalog.value if acct.catalog else None,
            is_house=writer.is_house_account,
        )
        for acct, writer in rows
    ]
    return AccountIndex(refs)


@dataclass
class RowPlan:
    row: ClientRow
    match: MatchResult
    account_codes: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "sheet": self.row.sheet,
            "row_no": self.row.row_no,
            "name": self.row.name,
            "payee_name": self.row.payee_name,
            "kind": self.row.kind.value,
            "emails": self.row.valid_emails,
            "contact_names": self.row.contact_names,
            "catalogs": self.row.catalogs,
            "language": self.row.preferred_language,
            "quarterly": self.row.is_quarterly,
            "match": {
                "matched": self.match.matched,
                "confidence": self.match.confidence,
                "score": self.match.score,
                "account_codes": self.account_codes,
                "method": self.match.method,
                "matched_on": self.match.matched_on,
            },
        }


def _plan(db: Session, rows: List[ClientRow]):
    index = build_index_from_db(db)
    plans: List[RowPlan] = []
    matches: Dict[int, MatchResult] = {}
    for r in rows:
        m = index.match(r.name, r.payee_name)
        matches[id(r)] = m
        plans.append(RowPlan(row=r, match=m, account_codes=list(m.account_codes)))

    # EXACT WINS, GLOBALLY. A group tag ("(Luna Negra)") or fuzzy sibling sweep
    # can pull in accounts whose display name exactly matches a DIFFERENT client
    # row ("Edipurepecha (Luna Negra)" belongs to the "Edipurepecha" row, not to
    # whichever row claims the Luna Negra group). Without this pass, apply order
    # decided the owner — the last row applied stole the account. Here, any code
    # exactly claimed by some row is removed from every row that did NOT exactly
    # claim it, making ownership deterministic and name-faithful.
    # Rank exact claims by specificity: a hit on the account's FULL name (2)
    # outranks a hit on only its pre-parenthetical part (1) — so the
    # "Don Kalavera (Loudness Music)" row beats "Don Kalavera (Mastered Trax)"
    # for the Loudness account. Rows tied at the top leave the code CONTESTED:
    # nobody auto-claims it and it surfaces in the resolution queue, instead of
    # apply order silently picking a winner.
    exact_owner_ids: Dict[str, set] = {}
    best_strength: Dict[str, int] = {}
    for p in plans:
        for code in p.match.exact_codes:
            strength = p.match.exact_strengths.get(code, 1)
            exact_owner_ids.setdefault(code, set()).add(id(p.row))
            if strength > best_strength.get(code, 0):
                best_strength[code] = strength
    top_claimers: Dict[str, set] = {}
    for p in plans:
        for code in p.match.exact_codes:
            if p.match.exact_strengths.get(code, 1) == best_strength.get(code, 0):
                top_claimers.setdefault(code, set()).add(id(p.row))
    for p in plans:
        rid = id(p.row)
        kept = [
            c for c in p.account_codes
            if (c not in exact_owner_ids)                        # nobody exact-claims it
            or (top_claimers.get(c) == {rid})                    # sole most-specific claimer
        ]
        if len(kept) != len(p.account_codes):
            p.account_codes = kept
            p.match.account_codes = list(kept)

    # CONTESTED SWEEPS ARE DROPPED. A code no row claims exactly but two or
    # more rows claim via group/fuzzy is ambiguous ("Cotorra Music Group"
    # swept by both "MMG" and "Monk Music Group…") — auto-applying it would
    # again make apply order pick the owner. Nobody gets it; it stays with its
    # placeholder and surfaces in the resolution queue for a human call.
    claimers: Dict[str, set] = {}
    for p in plans:
        for code in p.account_codes:
            if code not in exact_owner_ids:
                claimers.setdefault(code, set()).add(id(p.row))
    contested = {code for code, who in claimers.items() if len(who) > 1}
    if contested:
        for p in plans:
            kept = [c for c in p.account_codes if c not in contested]
            if len(kept) != len(p.account_codes):
                p.account_codes = kept
                p.match.account_codes = list(kept)

    all_codes = {a.account_code for a in index.accounts}
    findings = validate_rows(rows, matches=matches, all_account_codes=all_codes)
    return plans, findings, all_codes


def preview(db: Session, path: str) -> dict:
    """Compute the diff + findings without writing anything."""
    return preview_rows(db, parse_client_list(path))


def preview_rows(db: Session, rows: List[ClientRow]) -> dict:
    plans, findings, all_codes = _plan(db, rows)
    matched = sum(1 for p in plans if p.match.matched)
    covered = set()
    for p in plans:
        if p.match.matched:
            covered.update(p.account_codes)
    return {
        "row_count": len(rows),
        "stats": {
            "rows_matched": matched,
            "rows_unmatched": len(rows) - matched,
            "accounts_total": len(all_codes),
            "accounts_covered": len(covered),
            "accounts_unlisted": len(all_codes - covered),
            "by_confidence": {
                c: sum(1 for p in plans if p.match.confidence == c)
                for c in ("exact", "probable", "none")
            },
        },
        "findings_summary": summarize(findings),
        "findings": [f.as_dict() for f in findings],
        "rows": [p.as_dict() for p in plans],
    }


def pair_names_to_emails(emails, names):
    """Decide which contact name belongs to which address.

    The client list gives both as comma-separated cells, and they are NOT
    guaranteed to line up: 295 of 888 rows carry a different number of names
    than addresses. One name and two addresses, seven addresses and six names,
    one address and two names — all real, all in the delivered file.

    Pairing them by position regardless (`names[i]` for `emails[i]`) is what put
    "Che" on accounting@monkmusic.co and "Nate" on che@chekothari.com: every
    name after a gap slides onto somebody else's address. A wrong name on a
    contact is worse than no name, because it is the name an admin reads when
    deciding who to send a portal invite to.

    So:
      * counts match -> trust the position. That is the spreadsheet's own
        convention and it holds for the other two thirds of the file.
      * counts differ -> only claim a pairing the ADDRESS itself supports, by
        looking for the name inside the local part (che -> che@chekothari.com,
        nate -> nate@machelmontano.com). Anything left over stays unnamed and
        can be filled in by hand, which is a smaller job than finding the ones
        that are quietly wrong.

    Returns {email: name or None}.
    """
    emails = list(emails or [])
    names = list(names or [])
    out = {e: None for e in emails}

    if len(emails) == len(names):
        return dict(zip(emails, names))

    def norm(v):
        v = strip_accents(str(v or "")).lower()
        return re.sub(r"[^a-z0-9]+", "", v)

    unclaimed = set(emails)
    for name in names:
        # a name may be "Manuel (Eanz)" — try each word, longest first, so a
        # two-letter fragment cannot claim an address
        tokens = [t for t in re.split(r"[^A-Za-z0-9]+", strip_accents(name)) if len(t) >= 3]
        tokens.sort(key=len, reverse=True)
        for token in tokens:
            hit = next(
                (e for e in emails
                 if e in unclaimed and norm(token) and norm(token) in norm(e.split("@")[0])),
                None,
            )
            if hit is not None:
                out[hit] = name
                unclaimed.discard(hit)
                break
    return out


def _get_or_create_contact(db: Session, email: str, name: Optional[str],
                           lang: Optional[str]):
    """Returns (contact, created).

    Look-then-insert is a race, and this one bit for real: an import of 888 rows
    died on `duplicate key value violates unique constraint "ix_contact_email"`
    for an address the SELECT had just reported missing. Between the two
    statements another transaction — a second import, or the same one submitted
    twice — committed that contact. Under READ COMMITTED our snapshot could not
    see it, so the insert was the first time we heard about it.

    132 addresses are shared across writers, so the same contact is reached from
    many rows and the window is hit often rather than rarely.

    The insert therefore runs inside a SAVEPOINT. Without one, the failed
    statement poisons the whole transaction and every remaining row of the
    import dies with it: one duplicated address cost all 888. With one, losing
    the race just means the other side created it, which is the result we
    wanted anyway — so re-read it and carry on.
    """
    contact = db.query(Contact).filter(Contact.email == email).first()
    if contact is not None:
        return contact, False
    try:
        with db.begin_nested():
            contact = Contact(email=email, display_name=name, preferred_language=lang)
            db.add(contact)
            db.flush()
        return contact, True
    except IntegrityError:
        # The savepoint rolled back, the outer transaction is intact, and the
        # winner's row is now visible.
        contact = db.query(Contact).filter(Contact.email == email).first()
        if contact is None:
            raise
        return contact, False


def _resolve_publisher_id(db: Session, account_codes: List[str]) -> Optional[int]:
    """Publisher for a new client writer: inherit from a matched account's
    placeholder writer, else fall back to the sole Publisher row."""
    for code in account_codes:
        acct = (
            db.query(BeneficiaryAccount)
            .filter(BeneficiaryAccount.account_code == code)
            .first()
        )
        if acct is not None:
            old = db.get(Writer, acct.writer_id)
            if old is not None and old.publisher_id is not None:
                return old.publisher_id
    # Fall back to the sole/default publisher, creating it if the DB has none
    # (mirrors statement_ingest.sorter._get_or_create_publisher).
    pub = db.query(Publisher).first()
    if pub is None:
        pub = Publisher(name="Regalias Digitales")
        db.add(pub)
        db.flush()
    return pub.id


def _resolve_writer(
    db: Session,
    row: ClientRow,
    publisher_id: Optional[int],
    all_row_names: Optional[set] = None,
) -> Writer:
    """Find the roster entry for this row, else create it.

    An entry is identified by NAME **and ROLE**, not by name alone. The same
    person legitimately appears on both sheets — a client for their own catalog,
    a commission partner on other people's — and those are two entries holding
    two different bodies of money.

    Matching on name alone merged them onto one row, which put commission
    statements (CS*) and royalty statements on a single entry. Once merged
    nothing downstream could separate them: the portal switcher works per entry,
    and so does `publisher_owned`, so a writer shown that row saw their
    commission and an acquired catalog they had sold, together, with no way to
    tell them apart. 18 entries in the delivered data are in that state.

    Placeholder ingestion writers (kind IS NULL) are never reused here — we only
    reuse a previously client-import-created entry of the SAME role.

    The client's identity is the Artist/Publisher Name. The payee is merely who
    the money is made out to (stored on payee_name) — many distinct clients can
    share one payee, so naming the entry after the payee (as this code once did)
    collapsed unrelated artists into one identity."""
    target_name = (row.name or "").strip() or row.payee_name
    norm = normalize(target_name)

    row_kind = _KIND_MAP[row.kind]

    def _find(name_exact: str, name_norm: str) -> Optional[Writer]:
        # Same role as well as same name: a partner row must never adopt the
        # client entry that happens to share its name.
        base = db.query(Writer).filter(Writer.kind == row_kind)
        w = base.filter(Writer.canonical_name == name_exact).first()
        if w is None:
            for cand in base.all():
                if normalize(cand.canonical_name) == name_norm:
                    return cand
        return w

    existing = _find(target_name, norm)
    if existing is None and row.payee_name:
        # Legacy adoption: earlier imports named the writer after the PAYEE.
        # Reuse (and rename) that writer — but only when the payee name is not
        # itself another client row (a shared payee like "Mastered Trax" stays
        # its own client; each artist row gets its own identity instead).
        payee_norm = normalize(row.payee_name)
        if payee_norm != norm and (all_row_names is None or payee_norm not in all_row_names):
            legacy = _find(row.payee_name, payee_norm)
            if legacy is not None:
                _add_alias(db, legacy, legacy.canonical_name)
                legacy.canonical_name = target_name
                existing = legacy
    catalogs = [_CATALOG_MAP[c].value for c in row.catalogs if c in _CATALOG_MAP]
    # "Quarterly Client?" → cadence. A blank/no means the default: semiannual.
    cadence = Cadence.QUARTERLY if row.is_quarterly else Cadence.SEMIANNUAL
    if existing is None:
        existing = Writer(
            publisher_id=publisher_id,
            canonical_name=target_name,
            kind=row_kind,
            payee_name=row.payee_name,
            preferred_language=row.preferred_language,
            expected_catalogs=catalogs or None,
            cadence=cadence,
        )
        db.add(existing)
        db.flush()
    else:
        existing.kind = row_kind
        existing.payee_name = row.payee_name
        existing.preferred_language = row.preferred_language
        existing.cadence = cadence
        if catalogs:
            existing.expected_catalogs = catalogs
    # One entry, one role. A name on both sheets yields two entries, each
    # flagged for its own sheet — never one row flagged as both.
    existing.is_client = row.kind == ParsedKind.CLIENT
    existing.is_commission_partner = row.kind != ParsedKind.CLIENT
    # inherit publisher from a matched account if we don't have one
    return existing


def _add_alias(db: Session, writer: Writer, name: str):
    if not name:
        return
    exists = (
        db.query(WriterAlias)
        .filter(WriterAlias.writer_id == writer.id, WriterAlias.alias_name == name)
        .first()
    )
    if exists is None:
        db.add(WriterAlias(writer_id=writer.id, alias_name=name))


def apply(db: Session, path: str, confirmed_only: bool = True) -> dict:
    """Apply the import. By default only 'exact' matches are auto-applied;
    'probable'/'none' rows are left for the admin resolution queue.

    Re-pointing a matched account merges its placeholder writer into the real
    client writer. A placeholder left with no accounts is deleted.
    """
    return apply_rows(db, parse_client_list(path), confirmed_only=confirmed_only)


def _empty_counters() -> dict:
    return {
        "created_writers": 0,
        "reused_writers": 0,
        "repointed_accounts": 0,
        "merged_placeholders": 0,
        "created_contacts": 0,
        "created_links": 0,
    }


def _apply_row(db: Session, row: ClientRow, account_codes: List[str], c: dict,
               all_row_names: Optional[set] = None):
    """Resolve one client row against an explicit set of account codes:
    get/create the client Writer, re-point those accounts (merging emptied
    placeholders), wire contacts. Mutates the counter dict `c`; caller commits.

    Used both by the auto-apply pass (exact matches) and by manual resolution
    (admin-chosen accounts), so the merge logic lives in exactly one place.
    """
    before = db.query(Writer).filter(
        Writer.kind.isnot(None),
        Writer.canonical_name == ((row.name or "").strip() or row.payee_name),
    ).first()
    publisher_id = _resolve_publisher_id(db, account_codes)
    writer = _resolve_writer(db, row, publisher_id, all_row_names=all_row_names)
    if before is None:
        c["created_writers"] += 1
    else:
        c["reused_writers"] += 1

    placeholders_to_check = set()
    for code in account_codes:
        acct = (
            db.query(BeneficiaryAccount)
            .filter(BeneficiaryAccount.account_code == code)
            .first()
        )
        if acct is None:
            continue
        if acct.writer_id != writer.id:
            old = db.get(Writer, acct.writer_id)
            if old is not None and old.kind is None:
                _add_alias(db, writer, old.canonical_name)
                placeholders_to_check.add(old.id)
            acct.writer_id = writer.id
            c["repointed_accounts"] += 1
    db.flush()

    _add_alias(db, writer, row.name)
    if row.payee_name:
        _add_alias(db, writer, row.payee_name)

    paired = pair_names_to_emails(row.valid_emails, row.contact_names)
    # The first address on the row is the primary contact; the rest are managers.
    # That is positional by design and unrelated to which NAME belongs where.
    for index, email in enumerate(row.valid_emails):
        name = paired.get(email)
        contact, was_created = _get_or_create_contact(
            db, email, name, row.preferred_language)
        if was_created:
            c["created_contacts"] += 1
        if contact not in [wl.contact for wl in writer.contact_links]:
            link = (
                db.query(WriterContact)
                .filter(WriterContact.writer_id == writer.id,
                        WriterContact.contact_id == contact.id)
                .first()
            )
            if link is None:
                role = ContactRole.PRIMARY if index == 0 else ContactRole.MANAGER
                db.add(WriterContact(writer_id=writer.id, contact_id=contact.id, role=role))
                c["created_links"] += 1
        db.flush()
        if name and contact.display_name is None:
            contact.display_name = name

    for pid in placeholders_to_check:
        remaining = (
            db.query(BeneficiaryAccount)
            .filter(BeneficiaryAccount.writer_id == pid)
            .count()
        )
        if remaining == 0:
            for al in db.query(WriterAlias).filter(WriterAlias.writer_id == pid).all():
                _add_alias(db, writer, al.alias_name)
                db.delete(al)
            db.delete(db.get(Writer, pid))
            c["merged_placeholders"] += 1
    db.flush()
    return writer


# Near-matches at/above this confidence apply automatically on import; below it
# they stay in the resolution queue for an admin to confirm. Tuned so typos,
# name suffixes ("… NEW"), and subset names ("Peezy" ⊆ "OMB Peezy") flow through
# without manual work, while genuinely ambiguous rows still get reviewed.
AUTO_APPLY_SCORE = 0.70


def _should_auto_apply(m: MatchResult) -> bool:
    if not m.matched:
        return False
    if m.confidence == "exact":
        return True
    # a group hit maps one row to many accounts — always review those
    if m.method == "group":
        return False
    return m.confidence == "probable" and (m.score or 0) >= AUTO_APPLY_SCORE


_NEW_SUFFIX_RE = re.compile(r"\bnew\b\s*$")


def mark_publisher_owned_counterparts(db: Session) -> int:
    """Infer which entries are catalogs the publisher acquired.

    The client list encodes this in the NAME and nowhere else. Where a writer
    sold their old catalog and kept recording under a new one, the sheet holds
    two client rows:

        Likybo NEW   -> the catalog that is still THEIRS
        Likybo       -> the old catalog, now the publisher's

    So a trailing "NEW" is a statement about its counterpart: the row without
    it is the acquired one. This marks that counterpart `publisher_owned`,
    which removes it from the writer's portal, from their account switcher, and
    from anything invitable.

    Two rules keep it from over-reaching:

    * ONLY client-sheet rows are ever marked. Somebody who is also a commission
      partner has a same-named row on the partner sheet — J Swey, Likybo and
      Dante Storch all do — and that is commission they earned, their money.
      Marking it would hide their own income from them.
    * It only ever SETS the flag. An admin who marked something by hand, or
      unmarked it deliberately, is not overruled by the next import.

    Verified against the delivered list (2026-09-10): 10 rows end in NEW, 9 have
    a counterpart, all 9 correct. The tenth has none and is left alone.
    """
    writers = db.query(Writer).all()
    by_norm = {}
    for w in writers:
        by_norm.setdefault(normalize(w.canonical_name), []).append(w)

    marked = 0
    for w in writers:
        n = normalize(w.canonical_name)
        if not _NEW_SUFFIX_RE.search(n):
            continue
        base = _NEW_SUFFIX_RE.sub("", n).strip()
        if not base:
            continue
        for other in by_norm.get(base, []):
            if other.id == w.id or other.publisher_owned:
                continue
            # never the commission-partner side, and never a house account
            if other.is_commission_partner or other.is_house_account:
                continue
            other.publisher_owned = True
            marked += 1
    if marked:
        db.flush()
    return marked


def apply_rows(db: Session, rows: List[ClientRow], confirmed_only: bool = True) -> dict:
    """Apply the client list.

    EVERY row becomes a client identity — the roster is the client list, so a
    row that matched no statement account still yields a client (with its
    contacts, cadence, language and expected catalogs); it simply owns no
    accounts yet and shows as "no statements". Only ACCOUNT LINKING is gated on
    match confidence: unconfident matches attach nothing and go to the
    resolution queue, so a weak guess never re-points someone's royalties.
    """
    plans, findings, _all = _plan(db, rows)
    c = _empty_counters()
    all_row_names = {normalize(r.name) for r in rows if r.name}
    # The uploaded list is the authority for roster membership: clear the flags
    # first so anyone dropped from the sheets stops being counted.
    db.query(Writer).update(
        {Writer.is_client: False, Writer.is_commission_partner: False},
        synchronize_session=False,
    )
    for p in plans:
        row, m = p.row, p.match
        codes = m.account_codes if (not confirmed_only or _should_auto_apply(m)) else []
        _apply_row(db, row, codes, c, all_row_names=all_row_names)
    # After every row exists: work out which entries are acquired catalogs, from
    # the "X NEW" / "X" pairing the sheet uses to express it.
    c["publisher_owned_marked"] = mark_publisher_owned_counterparts(db)
    db.commit()
    return {**c, "findings_summary": summarize(findings)}


def resolve_row(db: Session, row: ClientRow, account_codes: List[str]) -> dict:
    """Manually resolve one row against admin-chosen account codes (infra PRD
    §7.2 resolution queue). Empty `account_codes` is legitimate — it creates
    the writer + contacts for someone who simply didn't earn this period."""
    c = _empty_counters()
    writer = _apply_row(db, row, account_codes, c)
    db.commit()
    return {**c, "writer_id": writer.id}


def client_row_from_dict(d: dict) -> ClientRow:
    """Reconstruct a ClientRow from a stored diff entry (importer preview) so
    manual resolution needs only the ClientImport record, not the spreadsheet."""
    from .parser import ParsedEmail
    kind = (ParsedKind.COMMISSION_PARTNER
            if d.get("kind") == "commission_partner" else ParsedKind.CLIENT)
    emails = [ParsedEmail(address=e, is_valid=True) for e in d.get("emails", [])]
    return ClientRow(
        sheet=d.get("sheet", "-"),
        row_no=int(d.get("row_no", 0)),
        kind=kind,
        name=d.get("name", ""),
        payee_name=d.get("payee_name"),
        emails=emails,
        contact_names=list(d.get("contact_names", [])),
        catalogs=list(d.get("catalogs", [])),
        preferred_language=d.get("language"),
        is_quarterly=bool(d.get("quarterly", False)),
    )
