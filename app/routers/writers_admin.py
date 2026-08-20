"""Admin Writer/Client Manager API (PRD: Writer-Scale UX, Feature C).

CRUD + paginated/searchable listing over the `Writer` roster, sized for the
real ~1,300-client roster (server-side pagination + search — never fetch-all).
The user-facing surface calls these people "clients"; the code-level entity
stays `Writer`. Admin-gated via the shared `require_admin` dependency.

Read-only surfaces (beneficiary accounts) are exposed on the detail endpoint;
re-pointing an account to a different writer is the client-import resolution
flow's job, not this editor's. Contact-email add/remove and portal invites live
in `portal.py`; the one admin-only gap this file fills is a thin contact-unlink
and an admin invite-revoke (portal.py's revoke is contact-self-service).
"""

import os
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, text
from sqlalchemy.orm import Session

from app.database.session import get_session
from app.logger.logger import get_logger
from app.models.models import User
from app.models.statements import (
    AliasSource,
    BeneficiaryAccount,
    Cadence,
    Catalog,
    Contact,
    ContactRole,
    Distribution,
    ParseStatus,
    PortalInvite,
    Publisher,
    Statement,
    StatementBatch,
    Writer,
    WriterAlias,
    WriterContact,
    WriterKind,
    WriterStatus,
)
from app.routers.statements_admin import require_admin
from app.services.client_import.matcher import normalize as normalize_name
from app.services.portal import invites as invite_svc
from app.services.writers.suggest import suggest_clients_for

logger = get_logger("writers_admin")

writers_admin_router = APIRouter(prefix="/admin/writers", tags=["Writers Admin"])


# --- schemas -----------------------------------------------------------------

class WriterCreate(BaseModel):
    canonical_name: str
    payee_name: Optional[str] = None
    kind: Optional[str] = None
    expected_catalogs: Optional[List[str]] = None
    preferred_language: Optional[str] = None
    cadence: Optional[str] = None


class ContactCreate(BaseModel):
    email: str
    display_name: Optional[str] = None
    role: Optional[str] = "primary"


class WriterUpdate(BaseModel):
    # Every field optional — PATCH semantics. `None` in the payload is
    # indistinguishable from "omitted" for JSON, so a field is only applied
    # when present in the request's model_fields_set (see _apply_update).
    canonical_name: Optional[str] = None
    payee_name: Optional[str] = None
    kind: Optional[str] = None
    expected_catalogs: Optional[List[str]] = None
    preferred_language: Optional[str] = None
    cadence: Optional[str] = None
    status: Optional[str] = None


# --- helpers -----------------------------------------------------------------

def _resolve_publisher_id(db: Session) -> int:
    """The sole/default publisher, creating it if the DB has none (mirrors
    client_import.importer and statement_ingest.sorter fallbacks)."""
    pub = db.query(Publisher).first()
    if pub is None:
        pub = Publisher(name="Regalias Digitales")
        db.add(pub)
        db.flush()
    return pub.id


def _parse_kind(value: Optional[str]) -> Optional[WriterKind]:
    if value is None:
        return None
    try:
        return WriterKind(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid kind {value!r}")


def _parse_cadence(value: Optional[str]) -> Optional[Cadence]:
    if value is None:
        return None
    try:
        return Cadence(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid cadence {value!r}")


def _parse_status(value: Optional[str]) -> Optional[WriterStatus]:
    if value is None:
        return None
    try:
        return WriterStatus(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid status {value!r}")


def _parse_contact_role(value: Optional[str]) -> ContactRole:
    try:
        return ContactRole(value or "primary")
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid role {value!r}")


def _parse_catalogs(value: Optional[List[str]]) -> Optional[List[str]]:
    if value is None:
        return None
    out = []
    for c in value:
        try:
            out.append(Catalog(c).value)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid catalog {c!r}")
    return out or None


def _get_writer_or_404(db: Session, writer_id: int) -> Writer:
    w = db.get(Writer, writer_id)
    if w is None:
        raise HTTPException(status_code=404, detail="Writer not found")
    return w


def _contacts_for(db: Session, writer_id: int):
    """[(WriterContact, Contact)] links for a writer, primary role first."""
    rows = (
        db.query(WriterContact, Contact)
        .join(Contact, WriterContact.contact_id == Contact.id)
        .filter(WriterContact.writer_id == writer_id)
        .all()
    )
    return rows


def _missing_info(w: Writer) -> List[str]:
    """Baseline client-list fields a writer needs before its data can be judged
    complete or distributed. A writer auto-created from statement files alone
    has none of these, so we can't know what it's *supposed* to have — it's
    flagged "needs info" until an admin supplies them."""
    # House accounts are the publisher's own (e.g. "Regalias Digitales"), not
    # clients — never flag them for client info.
    if w.is_house_account:
        return []
    # A kind-less writer is not a client missing fields — it's an UNMATCHED
    # statement account (nobody on the client list claims it). It needs a human
    # to assign an owner, not a cadence form; surfaced separately.
    if w.kind is None:
        return []
    missing = []
    if not w.expected_catalogs:
        missing.append("revenue type")
    if w.cadence is None:
        missing.append("payment cadence")
    return missing


def _writer_ids_with_data_gap(db: Session) -> List[int]:
    """Payees whose statement data is missing or only partial.

    Two different gaps, both of which block a clean send:
      * NO statements at all — nothing arrived for them this cycle.
      * PARTIAL — they are expected to have both revenue types (mechanical and
        YouTube) and only one turned up. A roster count says "has statements"
        and looks fine, which is exactly why this one goes unnoticed.

    House accounts are the publisher's own books and are never a client gap.
    """
    expected_by_writer = {
        w.id: set(w.expected_catalogs or [])
        for w in db.query(Writer).filter(Writer.is_house_account.is_(False)).all()
    }

    received = {}
    for writer_id, catalog in (
        db.query(BeneficiaryAccount.writer_id, BeneficiaryAccount.catalog)
        .join(Statement, Statement.account_id == BeneficiaryAccount.id)
        .distinct()
        .all()
    ):
        if catalog is not None:
            received.setdefault(writer_id, set()).add(catalog.value)
        else:
            received.setdefault(writer_id, set())

    gaps = []
    for writer_id, expected in expected_by_writer.items():
        got = received.get(writer_id)
        if not got:
            gaps.append(writer_id)          # nothing arrived
        elif expected and not expected.issubset(got):
            gaps.append(writer_id)          # only some of what they should have
    return gaps


def _portal_status_for(links, invites) -> str:
    """none | invited | active — about THIS client's portal.

    `active` means somebody accepted an invite to this client. It used to also
    return active when any linked contact merely had a login, which made a
    recorded contact email look like a live portal: paste the address of a
    manager who already runs another client's portal into this client's contact
    field and the row read "Portal active" though nobody had been invited here.

    That badge is how an admin decides whether to send an invite, so it has to
    answer for the client in front of them, not for the email address.
    """
    now = datetime.now()
    if any(inv.accepted_at is not None for inv in invites):
        return "active"
    if any(inv.is_active(now) for inv in invites):
        return "invited"
    return "none"


def _serialize_list_row(w: Writer, links, invites, account_count: int, coverage=None,
                        last_dist=None, suggestion=None, account_name=None) -> dict:
    primary = None
    emails = []
    for link, contact in links:
        emails.append(contact.email)
        if primary is None and link.role.value == "primary":
            primary = contact.email
    if primary is None and emails:
        primary = emails[0]
    cov = coverage or {}
    return {
        "id": w.id,
        "canonical_name": w.canonical_name,
        "payee_name": w.payee_name,
        "kind": w.kind.value if w.kind else None,
        "status": w.status.value if w.status else None,
        "cadence": w.cadence.value if w.cadence else None,
        "preferred_language": w.preferred_language,
        "expected_catalogs": w.expected_catalogs or [],
        "is_house_account": w.is_house_account,
        # baseline client-list fields still missing (empty list = fully set up)
        "missing_info": _missing_info(w),
        "needs_info": bool(_missing_info(w)),
        # on the roster but no statement at all — a distribution blocker
        "no_statements": (cov or {}).get("statements", 0) == 0 and not w.is_house_account,
        # a statement account no client-list row claims — resolve, don't fill in
        "is_unmatched": w.kind is None and not w.is_house_account,
        # For an unmatched row: the name printed on the statement filename (the
        # account's real identity) and the closest client to it, so the
        # publisher can answer "is this someone we already have?" without
        # searching the roster by hand. A proposal only — nothing is re-pointed.
        "account_name": account_name,
        "suggested_client": suggestion,
        "primary_email": primary,
        "contact_emails": emails,
        "account_count": account_count,
        # data-completeness signals for the roster: which royalty types (catalogs)
        # this writer has data for, and how many of their statements are fully
        # paired (PDF+XLSX) and reconciled (Σ line earnings == PDF ledger total).
        "received_catalogs": sorted(cov.get("catalogs", [])),
        # revenue types that have a received (paired) statement — the axis the
        # dashboard measures completeness on
        "covered_catalogs": sorted(cov.get("covered", [])),
        "statement_count": cov.get("statements", 0),
        "paired_count": cov.get("paired", 0),
        "reconciled_count": cov.get("reconciled", 0),
        # when this client last had a statement published to their portal
        "last_distributed_at": last_dist[0].isoformat() if last_dist and last_dist[0] else None,
        "last_distributed_period": last_dist[1] if last_dist else None,
        "portal_status": _portal_status_for(links, invites),
    }


# --- endpoints ---------------------------------------------------------------

@writers_admin_router.get("")
async def list_writers(
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    search: Optional[str] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    needs_info: Optional[bool] = None,
    needs_fix: Optional[bool] = None,
    unmatched: Optional[bool] = None,
    include_unmatched: Optional[bool] = None,
    data_gap: Optional[bool] = None,
    membership: Optional[str] = None,
    include_house: bool = False,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Paginated, searchable roster. Search matches canonical_name / payee_name
    / any linked contact email (case-insensitive substring). Returns
    {items, total, page, page_size} so the frontend can render page controls.

    House accounts are the publisher's own accounts, not clients, so they're
    excluded by default — this keeps the "N clients" headline consistent with
    the dashboard's "Active clients" rollup (which also excludes them). Pass
    include_house=true to see them."""
    q = db.query(Writer)

    if not include_house:
        q = q.filter(Writer.is_house_account.is_(False))
    # The roster IS the client list. An "unmatched" record is one that exists
    # ONLY to hold a statement account no client-list row claims: no client type
    # AND it owns at least one beneficiary account. (A manually added client
    # that simply has no type set owns no accounts, so it stays a client.) These
    # are excluded from the client list by default and shown via unmatched=true
    # or needs_fix.
    holds_accounts = db.query(BeneficiaryAccount.writer_id).distinct()
    is_unmatched = and_(Writer.kind.is_(None), Writer.id.in_(holds_accounts))
    if unmatched is True:
        q = q.filter(is_unmatched)
    elif include_unmatched:
        # One list, not two. An unmatched account is a payee the publisher has
        # to make a decision about, exactly like a client with no statements —
        # splitting them into a separate panel hides the one that needs the most
        # thought. Callers that want the strict client list simply omit this.
        pass
    elif unmatched is False or not needs_fix:
        q = q.filter(~is_unmatched)
    # Roster membership, from the imported client list. Without this the roster
    # returns clients AND commission partners together, so the dashboard said
    # "876 active clients" when the client list holds 810 — the other 65 are
    # commission partners, who are payees but not clients.
    if membership == "client":
        q = q.filter(Writer.is_client.is_(True))
    elif membership == "commission_partner":
        q = q.filter(Writer.is_commission_partner.is_(True))
    elif membership not in (None, "", "any"):
        raise HTTPException(
            status_code=400,
            detail="membership must be 'client', 'commission_partner', or 'any'",
        )

    if data_gap:
        # "Show me who is missing data." A payee with NO statement at all, or
        # with statements for only some of the revenue types they are supposed
        # to have — partial delivery looks fine on a roster count and is the
        # thing that quietly ships someone half their money.
        gap_ids = _writer_ids_with_data_gap(db)
        q = q.filter(Writer.id.in_(gap_ids or [0]))

    if kind:
        q = q.filter(Writer.kind == _parse_kind(kind))
    if status:
        q = q.filter(Writer.status == _parse_status(status))
    if needs_info is not None:
        # missing any baseline field (client type / revenue type / cadence);
        # house accounts are never "clients", so never need client info
        incomplete = and_(
            Writer.is_house_account.is_(False),
            or_(
                Writer.kind.is_(None),
                Writer.expected_catalogs.is_(None),
                Writer.cadence.is_(None),
            ),
        )
        q = q.filter(incomplete if needs_info else ~incomplete)
    if needs_fix:
        # "Needs attention before sending": missing baseline info OR no statement
        # at all — the two distribution blockers, shown together.
        writers_with_statements = db.query(BeneficiaryAccount.writer_id).join(
            Statement, Statement.account_id == BeneficiaryAccount.id
        )
        q = q.filter(
            Writer.is_house_account.is_(False),
            or_(
                Writer.kind.is_(None),
                Writer.expected_catalogs.is_(None),
                Writer.cadence.is_(None),
                Writer.id.notin_(writers_with_statements),
            ),
        )

    if search and search.strip():
        term = f"%{search.strip().lower()}%"
        # match by name/payee, OR by an email on one of the writer's contacts
        email_writer_ids = (
            db.query(WriterContact.writer_id)
            .join(Contact, WriterContact.contact_id == Contact.id)
            .filter(func.lower(Contact.email).like(term))
        )
        q = q.filter(
            or_(
                func.lower(Writer.canonical_name).like(term),
                func.lower(func.coalesce(Writer.payee_name, "")).like(term),
                Writer.id.in_(email_writer_ids),
            )
        )

    total = q.count()
    writers = (
        q.order_by(func.lower(Writer.canonical_name))
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    ids = [w.id for w in writers]
    # Batch the per-row joins so a page render is a fixed handful of queries,
    # never N+1 across 25–50 rows.
    links_by_writer = {i: [] for i in ids}
    invites_by_writer = {i: [] for i in ids}
    counts_by_writer = {i: 0 for i in ids}
    coverage_by_writer = {
        i: {"catalogs": set(), "covered": set(), "statements": 0, "paired": 0, "reconciled": 0}
        for i in ids
    }
    if ids:
        for link, contact in (
            db.query(WriterContact, Contact)
            .join(Contact, WriterContact.contact_id == Contact.id)
            .filter(WriterContact.writer_id.in_(ids))
            .all()
        ):
            links_by_writer[link.writer_id].append((link, contact))
        for inv in db.query(PortalInvite).filter(PortalInvite.writer_id.in_(ids)).all():
            invites_by_writer[inv.writer_id].append(inv)
        for wid, cnt, catalog in (
            db.query(
                BeneficiaryAccount.writer_id,
                func.count(BeneficiaryAccount.id),
                BeneficiaryAccount.catalog,
            )
            .filter(BeneficiaryAccount.writer_id.in_(ids))
            .group_by(BeneficiaryAccount.writer_id, BeneficiaryAccount.catalog)
            .all()
        ):
            counts_by_writer[wid] += cnt
            if catalog is not None:
                coverage_by_writer[wid]["catalogs"].add(catalog.value)
        # per-writer statement completeness (paired PDF+XLSX, reconciled amounts).
        # `covered` = revenue types (catalogs) that have a RECEIVED (paired)
        # statement — the axis "completeness" is measured on. Reconciliation is
        # tracked separately as a data-quality flag, never as "missing".
        recon_tol = Decimal("0.01")
        for wid, pdf_path, xlsx_path, detail_sum, calculated, catalog in (
            db.query(
                BeneficiaryAccount.writer_id,
                Statement.pdf_path,
                Statement.xlsx_path,
                Statement.detail_sum,
                Statement.calculated,
                BeneficiaryAccount.catalog,
            )
            .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
            .filter(BeneficiaryAccount.writer_id.in_(ids))
            .all()
        ):
            cov = coverage_by_writer[wid]
            cov["statements"] += 1
            if pdf_path and xlsx_path:
                cov["paired"] += 1
                if catalog is not None:
                    cov["covered"].add(catalog.value)
            if detail_sum is not None and calculated is not None and abs(detail_sum - calculated) <= recon_tol:
                cov["reconciled"] += 1

    # latest portal distribution per writer (published_at + which period)
    last_dist_by_writer = {}
    if ids:
        for wid, published_at, period_code in (
            db.query(Distribution.writer_id, Distribution.published_at, Distribution.period_code)
            .filter(Distribution.writer_id.in_(ids), Distribution.portal_visible.is_(True))
            .all()
        ):
            cur = last_dist_by_writer.get(wid)
            if cur is None or (published_at and cur[0] and published_at > cur[0]) or (published_at and not cur[0]):
                last_dist_by_writer[wid] = (published_at, period_code)

    # "Did you mean this client?" for the unmatched rows on this page only —
    # batched, so a page render stays a fixed number of queries.
    unmatched_ids = [w.id for w in writers if w.kind is None and not w.is_house_account]
    suggestions = suggest_clients_for(db, unmatched_ids) if unmatched_ids else {}
    account_names = {}
    if unmatched_ids:
        for acct in db.query(BeneficiaryAccount).filter(
            BeneficiaryAccount.writer_id.in_(unmatched_ids)
        ):
            if acct.display_name and acct.writer_id not in account_names:
                account_names[acct.writer_id] = acct.display_name

    items = [
        _serialize_list_row(
            w,
            links_by_writer[w.id],
            invites_by_writer[w.id],
            counts_by_writer[w.id],
            coverage_by_writer[w.id],
            last_dist_by_writer.get(w.id),
            suggestions.get(w.id),
            account_names.get(w.id),
        )
        for w in writers
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@writers_admin_router.get("/summary")
async def roster_summary(
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Roster-wide rollup for the Client Manager dashboard: how many clients,
    what's blocking a payout (needs-info / unreconciled), how much is staged,
    and whether it's safe to send to everyone."""
    active_q = db.query(Writer).filter(
        Writer.status == WriterStatus.ACTIVE, Writer.is_house_account.is_(False)
    )
    active_clients = active_q.count()
    # Roster membership comes from the client list's two sheets. Someone on both
    # is counted in BOTH totals, so these can sum to more than active_clients.
    client_count = active_q.filter(Writer.is_client.is_(True)).count()
    commission_partner_count = active_q.filter(
        Writer.is_commission_partner.is_(True)
    ).count()
    needs_info = active_q.filter(
        Writer.kind.isnot(None),
        or_(
            Writer.expected_catalogs.is_(None),
            Writer.cadence.is_(None),
        ),
    ).count()

    # Statement accounts owned by kind-less placeholder writers: money arrived
    # for someone the client list doesn't name. Not clients, never "needs info"
    # — they block sending until a human assigns them (fix the list and
    # re-import, or resolve manually).
    unmatched_rows = (
        db.query(BeneficiaryAccount, Writer)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
        .filter(
            Writer.kind.is_(None),
            Writer.is_house_account.is_(False),
            Writer.status == WriterStatus.ACTIVE,
        )
        .all()
    )
    unmatched_accounts = len(unmatched_rows)
    # The closest client to each unmatched name, so the dashboard can ask "did
    # you mean X?" instead of only naming a code nobody recognises. Batched in
    # one pass over the roster; a proposal only, never applied.
    unmatched_suggestions = suggest_clients_for(db, [w.id for _, w in unmatched_rows[:100]])
    unmatched_samples = [
        {"account_code": a.account_code, "name": a.display_name or w.canonical_name,
         "writer_id": w.id, "suggested_client": unmatched_suggestions.get(w.id)}
        for a, w in unmatched_rows[:100]
    ]

    # Pending payout = money still waiting to reach a CLIENT portal: UNdistributed
    # AND actually distributable. That excludes house-account statements (your own
    # share), offboarded clients, unmatched placeholders, unparsed statements, and
    # quarters already covered by a distributed semiannual (cadence dedup) — none
    # of those ever distribute, so counting them made "pending" look like stuck
    # money when it isn't.
    from app.services.distribution.periods import covers

    distributed_ids = {r[0] for r in db.query(Distribution.statement_id).all()}
    dist_periods: dict = {}  # writer_id -> {distributed period codes}, for cadence coverage
    for wid, pc in (
        db.query(Distribution.writer_id, Distribution.period_code)
        .filter(Distribution.portal_visible.is_(True), Distribution.superseded_by.is_(None))
        .all()
    ):
        dist_periods.setdefault(wid, set()).add(pc)

    pending_amount = Decimal("0")
    pending_statements = 0
    held_amount = Decimal("0")     # unmatched accounts: money with no named client
    house_amount = Decimal("0")    # the publisher's own share
    total_amount = Decimal("0")    # every parsed statement, so the math is visible
    latest_period = None
    for stmt, writer in (
        db.query(Statement, Writer)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
        .all()
    ):
        if stmt.period_code and (latest_period is None or stmt.period_code > latest_period):
            latest_period = stmt.period_code
        amount_all = stmt.detail_sum if stmt.detail_sum is not None else stmt.payable
        if amount_all is not None:
            total_amount += amount_all
            if writer.is_house_account:
                house_amount += amount_all
            elif writer.kind is None:
                held_amount += amount_all
        if stmt.id in distributed_ids:
            continue
        if (
            writer.is_house_account
            or writer.status == WriterStatus.OFFBOARDED
            or writer.kind is None
            or stmt.parse_status != ParseStatus.PARSED
        ):
            continue  # never goes to a client portal
        if any(covers(dp, stmt.period_code) for dp in dist_periods.get(writer.id, ())):
            continue  # a distributed semiannual already covers this quarter
        pending_statements += 1
        amount = stmt.detail_sum if stmt.detail_sum is not None else stmt.payable
        if amount is not None:
            pending_amount += amount

    # portal claim status across active clients
    portal_active = (
        db.query(func.count(func.distinct(WriterContact.writer_id)))
        .filter(WriterContact.user_id.isnot(None))
        .scalar()
        or 0
    )

    # The only thing that must be complete before sending is the CLIENT setup
    # (revenue type / cadence / type) — not the statement math.
    # HARD blockers stop sending outright (correctness: can't verify the client).
    # Warnings demand explicit acknowledgment in the send confirmation instead —
    # a normal period legitimately has clients with no earnings and accounts the
    # list doesn't name yet, and neither can misdeliver money (unmatched
    # accounts are never distributed at all).
    blockers = []
    if needs_info:
        blockers.append(f"{needs_info} client{'s' if needs_info != 1 else ''} missing info")
    warnings = []
    if unmatched_accounts:
        warnings.append(
            f"{unmatched_accounts} unmatched statement account"
            f"{'s' if unmatched_accounts != 1 else ''} (held, not sent)"
        )

    needs_info_clients = [
        {"id": w.id, "name": w.canonical_name, "missing": _missing_info(w)}
        for w in active_q.filter(
            Writer.kind.isnot(None),
            or_(
                Writer.expected_catalogs.is_(None),
                Writer.cadence.is_(None),
            ),
        )
        .order_by(func.lower(Writer.canonical_name))
        .limit(200)
        .all()
    ]

    # Active clients that have NO statement at all — on the roster (e.g. imported
    # from the client list) but with nothing to send. This is a blocker: you
    # shouldn't send a distribution while clients are missing their statements —
    # either their file hasn't been uploaded/matched, or they don't belong in the
    # active roster this period.
    writers_with_statements = {
        r[0]
        for r in db.query(BeneficiaryAccount.writer_id)
        .join(Statement, Statement.account_id == BeneficiaryAccount.id)
        .distinct()
    }
    # Explain WHY each one has nothing, so the list is actionable. The common
    # non-problem: the client list carries the same artist twice (e.g. "Zampler
    # (Loudness Music)" and "…NEW", or two rows sharing one payee) — the
    # statements landed on the twin, so this row is a duplicate to clean up in
    # the spreadsheet, not missing data.
    import re as _re

    def _base(name: str) -> str:
        return normalize_name(_re.sub(r"[\s\-]*\bnew\b\s*$", "", name or "", flags=_re.I))

    earning = [w for w in active_q.all() if w.id in writers_with_statements]
    by_base = {}
    by_payee = {}
    for w in earning:
        by_base.setdefault(_base(w.canonical_name), w)
        if w.payee_name:
            by_payee.setdefault(normalize_name(w.payee_name), w)

    no_statement_clients = []
    for w in active_q.order_by(func.lower(Writer.canonical_name)).all():
        if w.id in writers_with_statements:
            continue
        twin = by_base.get(_base(w.canonical_name)) or (
            by_payee.get(normalize_name(w.payee_name)) if w.payee_name else None
        )
        no_statement_clients.append({
            "id": w.id,
            "name": w.canonical_name,
            "reason": "duplicate_row" if twin else "no_earnings",
            "duplicate_of": twin.canonical_name if twin else None,
        })
    clients_without_statements = len(no_statement_clients)
    duplicate_rows = sum(1 for c in no_statement_clients if c["reason"] == "duplicate_row")
    if clients_without_statements:
        warnings.append(
            f"{clients_without_statements} client"
            f"{'s' if clients_without_statements != 1 else ''} with no statements"
        )

    return {
        "active_clients": active_clients,
        "client_count": client_count,
        "commission_partner_count": commission_partner_count,
        "needs_info": needs_info,
        "portal_active": int(portal_active),
        "pending_statements": pending_statements,
        "pending_amount": str(pending_amount),
        "total_amount": str(total_amount),
        "held_amount": str(held_amount),
        "house_amount": str(house_amount),
        "current_period": latest_period,
        "clients_without_statements": clients_without_statements,
        "duplicate_rows": duplicate_rows,
        "unmatched_accounts": unmatched_accounts,
        "blockers": blockers,
        "warnings": warnings,
        "ready_to_send": not blockers and pending_statements > 0,
        "issues": {
            "needs_info": needs_info_clients,
            "no_statements": no_statement_clients[:100],
            "unmatched_accounts": unmatched_samples,
        },
    }


class DistributeAllRequest(BaseModel):
    # Acknowledge the send-time warnings (clients with no statements, unmatched
    # accounts). Hard blockers (missing info, failed ingestion audit) can never
    # be forced.
    force: bool = False


@writers_admin_router.post("/distribute-all")
async def distribute_all(
    body: Optional[DistributeAllRequest] = None,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Publish every ready batch's statements to client portals in one action.
    Refused while any client is missing baseline info — you can't send what you
    can't verify. Batches that fail their own gate are skipped and reported."""
    from app.services.distribution.publish import GateNotReady, distribute_batch

    needs_info = (
        db.query(Writer)
        .filter(
            Writer.status == WriterStatus.ACTIVE,
            Writer.is_house_account.is_(False),
            Writer.kind.isnot(None),
            or_(
                Writer.expected_catalogs.is_(None),
                Writer.cadence.is_(None),
            ),
        )
        .count()
    )
    if needs_info:
        raise HTTPException(
            status_code=409,
            detail=f"{needs_info} client(s) still need info — complete every client before sending.",
        )

    unmatched = (
        db.query(BeneficiaryAccount)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
        .filter(
            Writer.kind.is_(None),
            Writer.is_house_account.is_(False),
            Writer.status == WriterStatus.ACTIVE,
        )
        .count()
    )
    force = bool(body and body.force)
    if unmatched and not force:
        raise HTTPException(
            status_code=409,
            detail=f"{unmatched} statement account(s) match no client on your list — "
            "they will be held back, not sent. Confirm sending to proceed anyway.",
        )

    # Also refuse while active clients have no statements at all (e.g. imported
    # from the client list but never matched to a statement) — you shouldn't send
    # a distribution with clients missing their data.
    with_statements = {
        r[0]
        for r in db.query(BeneficiaryAccount.writer_id)
        .join(Statement, Statement.account_id == BeneficiaryAccount.id)
        .distinct()
    }
    no_statements = (
        db.query(Writer.id)
        .filter(
            Writer.status == WriterStatus.ACTIVE,
            Writer.is_house_account.is_(False),
            Writer.kind.isnot(None),
            Writer.id.notin_(with_statements or [0]),
        )
        .count()
    )
    if no_statements and not (body and body.force):
        raise HTTPException(
            status_code=409,
            detail=f"{no_statements} client(s) have no statements and would receive nothing — "
            "confirm sending to proceed anyway.",
        )

    # Never send while an upload is still ingesting: numbers exist only after
    # parse, so a mid-ingest send pushes partial figures to writer portals.
    from app.services.distribution.publish import assert_no_ingest_in_flight

    try:
        assert_no_ingest_in_flight(db)
    except GateNotReady as e:
        raise HTTPException(status_code=409, detail=(e.gate or {}).get("reasons", ["Ingest in progress"])[0])

    # Final safety: never distribute while the ingestion audit shows the DB out
    # of sync with the source files (misattributed accounts = wrong writer paid).
    from app.services.statement_ingest.reconcile import reconcile_ingestion

    audit = reconcile_ingestion(db)
    if not audit["ok"]:
        issues = sum(audit["violation_counts"].values())
        raise HTTPException(
            status_code=409,
            detail=f"Ingestion audit failed ({issues} issue{'s' if issues != 1 else ''}) — "
            "see /admin/statements/reconcile and fix before sending.",
        )

    sent, skipped = [], []
    for b in db.query(StatementBatch).all():
        try:
            res = distribute_batch(db, b.id, published_by=user.id)
            sent.append({"batch_id": b.id, "result": res})
        except GateNotReady as e:
            skipped.append({"batch_id": b.id, "reasons": (e.gate or {}).get("reasons", [])})
        except Exception as exc:  # never let one batch abort the whole run
            skipped.append({"batch_id": b.id, "reasons": [str(exc)]})
    logger.info(f"admin {user.id} distribute-all: {len(sent)} sent, {len(skipped)} skipped")
    return {"sent_batches": len(sent), "skipped_batches": len(skipped), "sent": sent, "skipped": skipped}


@writers_admin_router.get("/{writer_id}")
async def get_writer(
    writer_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Detail: writer fields + contacts + read-only beneficiary accounts +
    portal invites."""
    w = _get_writer_or_404(db, writer_id)
    links = _contacts_for(db, writer_id)
    invites = (
        db.query(PortalInvite).filter(PortalInvite.writer_id == writer_id).all()
    )
    accounts = (
        db.query(BeneficiaryAccount)
        .filter(BeneficiaryAccount.writer_id == writer_id)
        .order_by(BeneficiaryAccount.account_code)
        .all()
    )
    # Statement history — per period + royalty type, with pairing (PDF+XLSX) and
    # reconciliation (Σ line earnings == PDF ledger) so the detail view can show
    # completeness by timeframe (the demo's "statement history" view).
    recon_tol = Decimal("0.01")
    stmt_rows = (
        db.query(Statement, BeneficiaryAccount.account_code)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .filter(BeneficiaryAccount.writer_id == writer_id)
        .order_by(Statement.period_code.desc())
        .all()
    )
    statements = []
    for stmt, account_code in stmt_rows:
        pdf_present = bool(stmt.pdf_path)
        xlsx_present = bool(stmt.xlsx_path)
        reconciled = None
        if stmt.detail_sum is not None and stmt.calculated is not None:
            reconciled = abs(stmt.detail_sum - stmt.calculated) <= recon_tol
        amount = stmt.detail_sum if stmt.detail_sum is not None else stmt.payable
        statements.append({
            "statement_id": stmt.id,
            "account_code": account_code,
            "period_code": stmt.period_code,
            "catalog": stmt.batch.catalog.value if stmt.batch else None,
            "parse_status": stmt.parse_status.value if stmt.parse_status else None,
            "pdf_present": pdf_present,
            "xlsx_present": xlsx_present,
            "paired": pdf_present and xlsx_present,
            "reconciled": reconciled,
            "amount": str(amount) if amount is not None else None,
        })
    now = datetime.now()
    return {
        "id": w.id,
        "canonical_name": w.canonical_name,
        "payee_name": w.payee_name,
        "kind": w.kind.value if w.kind else None,
        "status": w.status.value if w.status else None,
        "cadence": w.cadence.value if w.cadence else None,
        "preferred_language": w.preferred_language,
        "expected_catalogs": w.expected_catalogs or [],
        "is_house_account": w.is_house_account,
        "missing_info": _missing_info(w),
        "needs_info": bool(_missing_info(w)),
        "portal_status": _portal_status_for(links, invites),
        "contacts": [
            {
                "contact_id": c.id,
                "email": c.email,
                "display_name": c.display_name,
                "role": link.role.value,
                # Claimed THIS client — not "this address has a login somewhere".
                "has_login": link.user_id is not None,
            }
            for link, c in links
        ],
        "accounts": [
            {
                "id": a.id,
                "account_code": a.account_code,
                "catalog": a.catalog.value if a.catalog else None,
                "status": a.status.value if a.status else None,
            }
            for a in accounts
        ],
        "invites": [
            {
                "id": i.id,
                "email": i.email,
                "role": i.role.value,
                "active": i.is_active(now),
                "accepted": i.accepted_at is not None,
                "expires_at": i.expires_at.isoformat(),
            }
            for i in invites
        ],
        "statements": statements,
    }


@writers_admin_router.post("", status_code=201)
async def create_writer(
    body: WriterCreate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    name = (body.canonical_name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="canonical_name is required")
    writer = Writer(
        publisher_id=_resolve_publisher_id(db),
        canonical_name=name,
        status=WriterStatus.ACTIVE,
        kind=_parse_kind(body.kind),
        payee_name=(body.payee_name or None),
        preferred_language=(body.preferred_language or None),
        expected_catalogs=_parse_catalogs(body.expected_catalogs),
        cadence=_parse_cadence(body.cadence),
    )
    db.add(writer)
    db.commit()
    db.refresh(writer)
    logger.info(f"admin {user.id} created writer {writer.id} ({name})")
    return await get_writer(writer.id, user=user, db=db)


# Tables wiped by the testing reset, in FK-safe order (children first). Keeps
# User (logins) and Publisher so the admin can immediately re-ingest.
_RESET_TABLES = [
    "statement_line",
    "validation_finding",
    "validation_run",
    "distribution",
    "statement",
    "statement_batch",
    "beneficiary_account",
    "portal_invite",
    "writer_contact",
    "contact",
    "writer",
    "client_import",
    "statement_upload",
]


@writers_admin_router.post("/reset-all")
async def reset_all_data(
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """DEV/testing only: wipe all client + statement data (clients, accounts,
    statements, batches, uploads, distributions, contacts, invites, client
    imports) so a tester can start from a clean slate. Refused in PRODUCTION.
    Users and the publisher are preserved so the admin stays logged in."""
    env = (os.getenv("ENVIRONMENT") or "").upper()
    if env == "PRODUCTION":
        raise HTTPException(status_code=403, detail="Data reset is disabled in production")
    deleted = {}
    for table in _RESET_TABLES:
        res = db.execute(text(f"DELETE FROM {table}"))
        deleted[table] = res.rowcount if res.rowcount is not None else 0
    db.commit()

    # Also clear stored files on disk. Upload ids restart at 1 after the wipe,
    # so leaving old incoming/{id} dirs would make the next upload re-ingest
    # stale files. Remove everything under the storage root.
    import shutil

    from app.services.statement_ingest.storage import get_storage_root

    root = get_storage_root()
    try:
        if os.path.isdir(root):
            for entry in os.listdir(root):
                path = os.path.join(root, entry)
                shutil.rmtree(path, ignore_errors=True) if os.path.isdir(path) else os.remove(path)
    except OSError as exc:
        logger.warning(f"reset: could not fully clear storage root {root}: {exc}")

    logger.warning(f"admin {user.id} reset ALL client/statement data ({env or 'unknown env'})")
    return {"status": "reset", "deleted": deleted}


@writers_admin_router.patch("/{writer_id}")
async def update_writer(
    writer_id: int,
    body: WriterUpdate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    w = _get_writer_or_404(db, writer_id)
    fields = body.model_fields_set  # only touch keys the caller actually sent
    if "canonical_name" in fields:
        name = (body.canonical_name or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="canonical_name cannot be empty")
        w.canonical_name = name
    if "payee_name" in fields:
        w.payee_name = (body.payee_name or None)
    if "kind" in fields:
        w.kind = _parse_kind(body.kind)
    if "expected_catalogs" in fields:
        w.expected_catalogs = _parse_catalogs(body.expected_catalogs)
    if "preferred_language" in fields:
        w.preferred_language = (body.preferred_language or None)
    if "cadence" in fields:
        w.cadence = _parse_cadence(body.cadence)
    if "status" in fields:
        parsed = _parse_status(body.status)
        if parsed is not None:
            w.status = parsed
    db.commit()
    logger.info(f"admin {user.id} updated writer {writer_id}")
    return await get_writer(writer_id, user=user, db=db)


@writers_admin_router.post("/{writer_id}/archive")
async def archive_writer(
    writer_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Soft-remove: set status=OFFBOARDED. Statements/distributions still
    reference the writer, so we never hard-delete."""
    w = _get_writer_or_404(db, writer_id)
    w.status = WriterStatus.OFFBOARDED
    db.commit()
    logger.info(f"admin {user.id} archived writer {writer_id}")
    return {"id": writer_id, "status": w.status.value}


class BulkRemoveRequest(BaseModel):
    writer_ids: List[int]


class AssignAccountsRequest(BaseModel):
    target_writer_id: int


@writers_admin_router.post("/{writer_id}/assign")
async def assign_unmatched_to_client(
    writer_id: int,
    body: AssignAccountsRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Hand an unmatched account's statements to the client they belong to.

    This is the "did you mean X?" answer being accepted. It re-points the
    beneficiary accounts from the placeholder row onto the real client, which
    is the ONLY thing that has to change: portal reads resolve ownership
    through the account's CURRENT writer, so the client sees the statements
    immediately and nothing has to rewrite historical distribution rows.

    Deliberately narrow, because assigning the wrong owner sends one client's
    royalties to another and is close to unrecoverable:
      * only an UNMATCHED row can be assigned (a real client's accounts are not
        moved by a name guess),
      * the target must be an actual client on the list, and
      * a placeholder that has already had statements DISTRIBUTED is refused —
        somebody has already been shown that money, and silently moving it is
        a different and much bigger decision than resolving an identity.

    The placeholder's name is kept as an alias on the client, so the next
    import recognises the spelling instead of re-creating the same orphan.
    """
    placeholder = db.get(Writer, writer_id)
    if placeholder is None:
        raise HTTPException(status_code=404, detail="Account not found")
    if placeholder.is_house_account or placeholder.kind is not None:
        raise HTTPException(
            status_code=409,
            detail="Only an unmatched account can be assigned to a client",
        )

    target = db.get(Writer, body.target_writer_id)
    if target is None:
        raise HTTPException(status_code=404, detail="Client not found")
    if target.id == placeholder.id:
        raise HTTPException(status_code=422, detail="Cannot assign a row to itself")
    if target.kind is None or target.is_house_account:
        raise HTTPException(
            status_code=409, detail="Target must be a client on your list"
        )

    already_sent = (
        db.query(Distribution.id).filter(Distribution.writer_id == placeholder.id).first()
    )
    if already_sent is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "These statements have already been distributed under this name. "
                "Unpublish them first — reassigning sent money is a separate decision."
            ),
        )

    accounts = (
        db.query(BeneficiaryAccount)
        .filter(BeneficiaryAccount.writer_id == placeholder.id)
        .all()
    )
    moved = [a.account_code for a in accounts]
    for a in accounts:
        # Re-point through the RELATIONSHIP, not the raw FK: deleting the
        # placeholder below cascades over its `accounts` collection, and a
        # collection that still lists these rows would null the very FK we just
        # set — silently orphaning the statements instead of moving them.
        a.writer = target

    # Remember the spelling that did not match, so the next client-list import
    # resolves it instead of creating this same orphan again.
    orphan_name = (accounts[0].display_name if accounts else None) or placeholder.canonical_name
    exists = (
        db.query(WriterAlias)
        .filter(WriterAlias.writer_id == target.id, WriterAlias.alias_name == orphan_name)
        .first()
    )
    if exists is None and orphan_name and orphan_name != target.canonical_name:
        db.add(WriterAlias(writer_id=target.id, alias_name=orphan_name,
                           source=AliasSource.MANUAL))

    # The placeholder existed only to hold those accounts; with them moved it
    # references nothing, so it goes rather than lingering as a phantom client.
    db.query(WriterContact).filter(WriterContact.writer_id == placeholder.id).delete(
        synchronize_session=False
    )
    db.query(PortalInvite).filter(PortalInvite.writer_id == placeholder.id).delete(
        synchronize_session=False
    )
    db.query(WriterAlias).filter(WriterAlias.writer_id == placeholder.id).delete(
        synchronize_session=False
    )
    db.delete(placeholder)
    db.commit()

    logger.info(
        f"admin {user.id} assigned {len(moved)} account(s) {moved} from unmatched "
        f"'{orphan_name}' to client {target.id} '{target.canonical_name}'"
    )
    return {
        "assigned_to": {"id": target.id, "name": target.canonical_name},
        "account_codes": moved,
        "alias_recorded": orphan_name,
    }


@writers_admin_router.post("/bulk-remove")
async def bulk_remove_writers(
    body: BulkRemoveRequest,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Remove several clients at once, from the "needs attention" cleanup view.

    Two different things are called "delete" here, and conflating them loses
    money:

      * A row that owns NOTHING — no statement accounts, no distributions — is
        a junk entry (a typo'd import line, a client added twice). Nothing
        references it, so it is genuinely deleted.

      * A row that owns statement accounts is holding real royalties. Hard
        deleting it would orphan those statements and the money on them, so it
        is OFFBOARDED instead, exactly as the single-client Remove does. Its
        statements and distribution history stay intact.

    The response says which happened to each one, so the caller can tell the
    admin the truth rather than implying 87 rows were erased.
    """
    ids = list(dict.fromkeys(body.writer_ids or []))
    if not ids:
        raise HTTPException(status_code=422, detail="No clients selected")
    if len(ids) > 500:
        raise HTTPException(status_code=422, detail="Select at most 500 clients at a time")

    deleted, archived, skipped = [], [], []
    for writer_id in ids:
        w = db.get(Writer, writer_id)
        if w is None:
            skipped.append({"id": writer_id, "reason": "not found"})
            continue
        # House accounts are the publisher's own books, never a cleanup target.
        if w.is_house_account:
            skipped.append({"id": writer_id, "name": w.canonical_name,
                            "reason": "house account"})
            continue

        holds_accounts = (
            db.query(BeneficiaryAccount.id)
            .filter(BeneficiaryAccount.writer_id == writer_id)
            .first()
            is not None
        )
        holds_distributions = (
            db.query(Distribution.id).filter(Distribution.writer_id == writer_id).first()
            is not None
        )

        if holds_accounts or holds_distributions:
            w.status = WriterStatus.OFFBOARDED
            archived.append({"id": writer_id, "name": w.canonical_name})
            continue

        # Nothing points at this row but its own links — clear those, then drop it.
        db.query(WriterContact).filter(WriterContact.writer_id == writer_id).delete(
            synchronize_session=False
        )
        db.query(PortalInvite).filter(PortalInvite.writer_id == writer_id).delete(
            synchronize_session=False
        )
        db.query(WriterAlias).filter(WriterAlias.writer_id == writer_id).delete(
            synchronize_session=False
        )
        name = w.canonical_name
        db.delete(w)
        deleted.append({"id": writer_id, "name": name})

    db.commit()
    logger.info(
        f"admin {user.id} bulk-removed {len(ids)} clients: "
        f"{len(deleted)} deleted, {len(archived)} offboarded, {len(skipped)} skipped"
    )
    return {
        "requested": len(ids),
        "deleted": deleted,
        "archived": archived,
        "skipped": skipped,
    }


@writers_admin_router.post("/{writer_id}/contacts")
async def add_contact(
    writer_id: int,
    body: ContactCreate,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Record a contact email for this client (no invite sent). Reuses an
    existing Contact row if the email already exists, then links it to the
    writer. Sending them a portal login is the separate Invite action."""
    _get_writer_or_404(db, writer_id)
    email = (body.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="A valid email is required")
    role = _parse_contact_role(body.role)
    contact = db.query(Contact).filter(func.lower(Contact.email) == email).first()
    if contact is None:
        contact = Contact(email=email, display_name=(body.display_name or None))
        db.add(contact)
        db.flush()
    elif body.display_name and not contact.display_name:
        contact.display_name = body.display_name.strip()
    link = (
        db.query(WriterContact)
        .filter(WriterContact.writer_id == writer_id, WriterContact.contact_id == contact.id)
        .first()
    )
    if link is None:
        db.add(WriterContact(writer_id=writer_id, contact_id=contact.id, role=role))
    db.commit()
    logger.info(f"admin {user.id} added contact {email} to writer {writer_id}")
    return await get_writer(writer_id, user=user, db=db)


@writers_admin_router.delete("/{writer_id}/contacts/{contact_id}")
async def unlink_contact(
    writer_id: int,
    contact_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Remove a contact's access to this writer (deletes the WriterContact
    link only — the Contact row and any other writers it's linked to stay)."""
    _get_writer_or_404(db, writer_id)
    link = (
        db.query(WriterContact)
        .filter(
            WriterContact.writer_id == writer_id,
            WriterContact.contact_id == contact_id,
        )
        .first()
    )
    if link is None:
        raise HTTPException(status_code=404, detail="Contact is not linked to this writer")
    db.delete(link)
    db.commit()
    logger.info(f"admin {user.id} unlinked contact {contact_id} from writer {writer_id}")
    return {"writer_id": writer_id, "contact_id": contact_id, "status": "unlinked"}


@writers_admin_router.post("/{writer_id}/invites/{invite_id}/revoke")
async def admin_revoke_invite(
    writer_id: int,
    invite_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Admin revoke of a pending portal invite (portal.py's revoke is
    contact-self-service and 403s an admin caller). Verifies the invite
    belongs to this writer, then wraps invite_svc.revoke_invite."""
    _get_writer_or_404(db, writer_id)
    inv = db.get(PortalInvite, invite_id)
    if inv is None or inv.writer_id != writer_id:
        raise HTTPException(status_code=404, detail="Invite not found for this writer")
    invite_svc.revoke_invite(db, invite_id)
    logger.info(f"admin {user.id} revoked invite {invite_id} on writer {writer_id}")
    return {"writer_id": writer_id, "invite_id": invite_id, "status": "revoked"}
