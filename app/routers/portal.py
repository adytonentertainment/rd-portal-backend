"""Writer-portal API (infra PRD §7.2, §7.3, §10).

Two audiences, both here:
  - Contact-scoped `/me/*`: a logged-in writer/manager sees only the writers
    they're linked to, and can share access by email (Dropbox-style).
  - Public `/portal/*`: preview + accept an invite (no auth — the token *is*
    the auth), minting a session on accept.

Admin bootstrap invites live under `/admin/writers/{id}/invites` (require_admin).
All contact-scoped queries go through `current_contact` + explicit writer-access
checks; a `writer_id` in the path is only ever honored inside that scope.

OWNERSHIP IS RESOLVED THROUGH THE ACCOUNT, NEVER FROM Distribution.writer_id.
`Distribution.writer_id` records who a statement was published to at the time —
it is an audit fact, frozen at publish. Accounts get re-pointed afterwards (a
client-list import correcting who an account belongs to), and nothing rewrites
those old rows. Scoping the portal on the frozen value therefore kept serving
the PREVIOUS client the statement, its PDF and its full line detail — one
client reading another's royalties — while the rightful owner saw nothing.
Every read below joins Statement -> BeneficiaryAccount and filters on the
account's CURRENT writer, so re-pointing takes effect immediately.
"""

import os
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from jose import jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database.session import get_session
from app.logger.logger import get_logger
from app.models.models import User
from app.models.statements import (
    BeneficiaryAccount,
    Contact,
    ContactRole,
    Distribution,
    PortalInvite,
    Statement,
    StatementLine,
    Writer,
    WriterContact,
)
from app.routers.auth import ALGORITHM, SECRET_KEY, bcrypt_context, get_user
from app.routers.statements_admin import require_admin
from app.services.portal import invites as invite_svc
from app.services.statement_ingest.storage import resolve_stored_path
from app.services.portal.invite_delivery import invite_url, send_invite_email

logger = get_logger("portal")

me_router = APIRouter(prefix="/me", tags=["Writer Portal"])
portal_router = APIRouter(prefix="/portal", tags=["Writer Portal"])
writer_invites_admin_router = APIRouter(prefix="/admin/writers", tags=["Writer Portal Admin"])

# Portal UI languages. Spanish is not an afterthought here: most of this
# publisher's writers are Spanish-speaking.
SUPPORTED_LANGUAGES = {"en", "es"}


# --- schemas -----------------------------------------------------------------

class InviteRequest(BaseModel):
    email: EmailStr
    role: str = "manager"


class AcceptRequest(BaseModel):
    token: str
    password: Optional[str] = None


def _role(value: str) -> ContactRole:
    try:
        return ContactRole(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid role {value!r}")


# --- dependencies ------------------------------------------------------------

def current_contact(
    user: User = Depends(get_user), db: Session = Depends(get_session)
) -> Contact:
    contact = invite_svc.contact_for_user(db, user)
    if contact is None:
        raise HTTPException(status_code=403, detail="No portal access for this account")
    return contact


def _require_writer_access(db: Session, contact: Contact, writer_id: int) -> Writer:
    link = (
        db.query(WriterContact)
        .filter(WriterContact.writer_id == writer_id, WriterContact.contact_id == contact.id)
        .first()
    )
    if link is None:
        # 404 (not 403) so we don't reveal that a writer exists to a non-member.
        raise HTTPException(status_code=404, detail="Writer not found")
    return db.get(Writer, writer_id)


def _writer_card(db: Session, writer: Writer) -> dict:
    account_count = (
        db.query(BeneficiaryAccount)
        .filter(BeneficiaryAccount.writer_id == writer.id)
        .count()
    )
    return {
        "id": writer.id,
        "name": writer.canonical_name,
        "kind": writer.kind.value if writer.kind else None,
        "account_count": account_count,
    }


def _mint_token(user: User) -> str:
    payload = {
        "sub": user.username,
        "id": user.id,
        "email": user.email,
        "exp": datetime.now() + timedelta(days=7),
    }
    return jwt.encode(payload, key=SECRET_KEY, algorithm=ALGORITHM)


# --- contact-scoped: /me -----------------------------------------------------

@me_router.get("")
async def get_me(
    contact: Contact = Depends(current_contact), db: Session = Depends(get_session)
):
    writer_ids = invite_svc.writer_ids_for_contact(db, contact)
    writers = db.query(Writer).filter(Writer.id.in_(writer_ids or [0])).all()
    return {
        "email": contact.email,
        "display_name": contact.display_name,
        "preferred_language": contact.preferred_language,
        "writers": [_writer_card(db, w) for w in writers],
    }


@me_router.get("/writers")
async def list_my_writers(
    contact: Contact = Depends(current_contact), db: Session = Depends(get_session)
):
    writer_ids = invite_svc.writer_ids_for_contact(db, contact)
    writers = db.query(Writer).filter(Writer.id.in_(writer_ids or [0])).all()
    return [_writer_card(db, w) for w in writers]


@me_router.get("/statements")
async def list_my_statements(
    writer_id: Optional[int] = None,
    contact: Contact = Depends(current_contact),
    db: Session = Depends(get_session),
):
    """Portal-visible distributions across the contact's writers (the writer
    switcher passes ?writer_id to narrow, but only within scope)."""
    scope_ids = invite_svc.writer_ids_for_contact(db, contact)
    if writer_id is not None:
        if writer_id not in scope_ids:
            raise HTTPException(status_code=404, detail="Writer not found")
        scope_ids = [writer_id]

    rows = (
        db.query(Distribution, Statement, Writer)
        .join(Statement, Distribution.statement_id == Statement.id)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
        .filter(
            BeneficiaryAccount.writer_id.in_(scope_ids or [0]),
            Distribution.portal_visible.is_(True),
            Distribution.superseded_by.is_(None),
        )
        .order_by(Distribution.period_code.desc(), Distribution.catalog)
        .all()
    )
    return [
        {
            "distribution_id": d.id,
            "writer_id": d.writer_id,
            "writer_name": w.canonical_name,
            "period_code": d.period_code,
            "catalog": d.catalog.value,
            "payable": str(s.payable) if s.payable is not None else None,
            "published_at": d.published_at.isoformat() if d.published_at else None,
            "line_count": s.line_count,
        }
        for d, s, w in rows
    ]


# Period code (e.g. "PUB25Q4", "PUB26H1") → human label + a representative date
# inside the period, so the earnings page can bucket by quarter and range-filter.
def _period_label(code: str) -> str:
    m = code or ""
    import re

    g = re.search(r"PUB(\d{2})([QH]\d)", m)
    return f"{g.group(2)} 20{g.group(1)}" if g else (code or "")


def _period_date(code: str) -> str:
    import re

    g = re.search(r"PUB(\d{2})([QH])(\d)", code or "")
    if not g:
        return ""
    year = 2000 + int(g.group(1))
    kind, n = g.group(2), int(g.group(3))
    month = {"Q": {1: 3, 2: 6, 3: 9, 4: 12}, "H": {1: 6, 2: 12}}[kind][n]
    return f"{year:04d}-{month:02d}-15"


# Songs returned for the "top songs" list. The charts never need every work —
# only the leaders — and song title is what multiplies the payload.
TOP_SONGS_LIMIT = 200


@me_router.get("/transactions")
async def list_my_transactions(
    writer_id: Optional[int] = None,
    contact: Contact = Depends(current_contact),
    db: Session = Depends(get_session),
):
    """Aggregated royalty data for the portal's earnings visuals.

    Every visual on the page is an aggregate — by income type, source/platform,
    territory, and period — plus a top-songs list. Shipping one row per
    (song x country x source x income type) to draw a six-slice pie was
    ruinous: the heaviest writer produced 475,886 rows / 179 MB / 826 MB of
    server memory for a single page load, enough to OOM a small VPS and take
    every writer's portal down with it.

    Song title is what multiplies the payload, so it is dropped from the
    dimension key and returned separately as the top N works. The remaining
    key (period x country x source x income type) is naturally bounded —
    894 rows / 0.14 MB for that same writer, a 435x reduction — and every
    chart still re-aggregates to exactly the same totals.

    Shape is unchanged (the client re-aggregates the same fields), so rows are
    simply coarser: `title` is absent on dimension rows and present on song
    rows.
    """
    scope_ids = invite_svc.writer_ids_for_contact(db, contact)
    if writer_id is not None:
        if writer_id not in scope_ids:
            raise HTTPException(status_code=404, detail="Writer not found")
        scope_ids = [writer_id]

    dists = (
        db.query(Distribution, Statement)
        .join(Statement, Distribution.statement_id == Statement.id)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .filter(
            BeneficiaryAccount.writer_id.in_(scope_ids or [0]),
            Distribution.portal_visible.is_(True),
            Distribution.superseded_by.is_(None),
        )
        .all()
    )
    if not dists:
        return []

    meta = {
        s.id: (_period_label(d.period_code), _period_date(d.period_code), d.catalog.value)
        for d, s in dists
    }
    statement_ids = list(meta)

    out = []

    # 1) dimension rows: everything the pies, globe and time bars need
    dimension_rows = (
        db.query(
            StatementLine.statement_id,
            StatementLine.country,
            StatementLine.income_source,
            StatementLine.income_type,
            func.sum(StatementLine.earnings),
            func.sum(StatementLine.units),
        )
        .filter(StatementLine.statement_id.in_(statement_ids))
        .group_by(
            StatementLine.statement_id,
            StatementLine.country,
            StatementLine.income_source,
            StatementLine.income_type,
        )
        .all()
    )
    for sid, country, source, income_type, earnings, units in dimension_rows:
        label, date, catalog = meta[sid]
        terr = (country or "").strip().upper()
        src = source or "Unknown"
        inc = income_type or "Not Specified"
        out.append(
            {
                "amount": float(earnings or 0),
                "units": float(units or 0),
                "territory": terr,
                "territoryName": terr,
                "platform": src,
                "source": src,
                "incomeName": inc,
                "category": inc,
                "sourceCategory": inc,
                "period": label,
                "incomePeriod": label,
                "date": date,
                "catalog": catalog,
                "statementId": sid,
            }
        )

    # 2) top works, so the songs list still has real titles. Marked
    # `is_song_row` so the client can use these for the songs list only and
    # never double-count them into the money totals above.
    song_rows = (
        db.query(
            StatementLine.statement_id,
            StatementLine.song_title,
            func.sum(StatementLine.earnings),
            func.sum(StatementLine.units),
        )
        .filter(StatementLine.statement_id.in_(statement_ids))
        .group_by(StatementLine.statement_id, StatementLine.song_title)
        .order_by(func.sum(StatementLine.earnings).desc())
        .limit(TOP_SONGS_LIMIT)
        .all()
    )
    for sid, title, earnings, units in song_rows:
        label, date, catalog = meta[sid]
        out.append(
            {
                "amount": float(earnings or 0),
                "units": float(units or 0),
                "title": title or "Untitled",
                "product": title or "Untitled",
                "period": label,
                "incomePeriod": label,
                "date": date,
                "catalog": catalog,
                "statementId": sid,
                "is_song_row": True,
            }
        )
    return out


@me_router.get("/earnings")
async def my_earnings(
    writer_id: Optional[int] = None,
    contact: Contact = Depends(current_contact),
    db: Session = Depends(get_session),
):
    """The writer's money, as their statement states it.

    A single "total" is not honest here: `payable` is not a slice of this
    period's gross. The statement is a waterfall —

        royalties earned this period      (detail_sum / calculated)
      + balance brought forward           (carried_forward_in)
      - recouped against advances         (recouped, stored negative)
      - carried to the next period        (carried_forward_out, below threshold)
      = subtotal before commission        (before_tax)
      - commission / withholding
      = PAYABLE                           (what the writer is actually paid)

    so a writer can be owed MORE than they earned this period (carry-forward)
    or nothing at all (fully recouped). The portal headline must be `payable`,
    matching the PDF they can download; the rest is returned so the difference
    is explained rather than hidden.
    """
    scope_ids = invite_svc.writer_ids_for_contact(db, contact)
    if writer_id is not None:
        if writer_id not in scope_ids:
            raise HTTPException(status_code=404, detail="Writer not found")
        scope_ids = [writer_id]

    rows = (
        db.query(Distribution, Statement)
        .join(Statement, Distribution.statement_id == Statement.id)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .filter(
            BeneficiaryAccount.writer_id.in_(scope_ids or [0]),
            Distribution.portal_visible.is_(True),
            Distribution.superseded_by.is_(None),
        )
        .all()
    )

    def _d(value):
        return value if value is not None else Decimal("0")

    fields = ("gross", "carried_forward_in", "recouped", "carried_forward_out",
              "before_tax", "payable")
    totals = {f: Decimal("0") for f in fields}
    by_period: Dict[str, dict] = {}
    for dist, st in rows:
        gross = st.detail_sum if st.detail_sum is not None else _d(st.calculated)
        values = {
            "gross": _d(gross),
            "carried_forward_in": _d(st.carried_forward_in),
            "recouped": _d(st.recouped),
            "carried_forward_out": _d(st.carried_forward_out),
            "before_tax": _d(st.before_tax),
            "payable": _d(st.payable),
        }
        for f in fields:
            totals[f] += values[f]
        bucket = by_period.setdefault(
            dist.period_code,
            {"period_code": dist.period_code, "label": _period_label(dist.period_code),
             "statements": 0, **{f: Decimal("0") for f in fields}},
        )
        bucket["statements"] += 1
        for f in fields:
            bucket[f] += values[f]

    periods = sorted(by_period.values(), key=lambda p: p["period_code"], reverse=True)
    return {
        # the headline: what the publisher actually pays out
        "payable": str(totals["payable"]),
        # how it got there
        "gross": str(totals["gross"]),
        "carried_forward_in": str(totals["carried_forward_in"]),
        "recouped": str(totals["recouped"]),
        "carried_forward_out": str(totals["carried_forward_out"]),
        "before_tax": str(totals["before_tax"]),
        "commission": str(totals["before_tax"] - totals["payable"]),
        "statements": len(rows),
        "periods": [{**p, **{f: str(p[f]) for f in fields}} for p in periods],
    }


def _scoped_distribution(
    db: Session, contact: Contact, distribution_id: int
) -> Tuple[Distribution, Statement, Writer]:
    """Fetch a visible distribution only if it belongs to a writer the contact
    can access; 404 otherwise (existence hidden). Reused by summary/pdf/breakdown."""
    scope_ids = invite_svc.writer_ids_for_contact(db, contact)
    row = (
        db.query(Distribution, Statement, Writer)
        .join(Statement, Distribution.statement_id == Statement.id)
        .join(BeneficiaryAccount, Statement.account_id == BeneficiaryAccount.id)
        .join(Writer, BeneficiaryAccount.writer_id == Writer.id)
        .filter(
            Distribution.id == distribution_id,
            BeneficiaryAccount.writer_id.in_(scope_ids or [0]),
            Distribution.portal_visible.is_(True),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Statement not found")
    return row


@me_router.get("/statements/{distribution_id}")
async def get_my_statement(
    distribution_id: int,
    contact: Contact = Depends(current_contact),
    db: Session = Depends(get_session),
):
    """One statement's summary — scoped: 404 unless it belongs to a writer the
    contact can access and is currently visible."""
    d, s, w = _scoped_distribution(db, contact, distribution_id)
    return {
        "distribution_id": d.id,
        "writer_name": w.canonical_name,
        "period_code": d.period_code,
        "catalog": d.catalog.value,
        "payable": str(s.payable) if s.payable is not None else None,
        "calculated": str(s.calculated) if s.calculated is not None else None,
        "line_count": s.line_count,
        "published_at": d.published_at.isoformat() if d.published_at else None,
        "has_pdf": s.pdf_path is not None,
    }


@me_router.get("/statements/{distribution_id}/pdf")
async def get_my_statement_pdf(
    distribution_id: int,
    contact: Contact = Depends(current_contact),
    db: Session = Depends(get_session),
):
    """Download the statement PDF (scoped). With local storage this streams the
    file; once object storage is wired (§6) this becomes a short-lived signed
    URL — the ownership check stays identical."""
    d, s, w = _scoped_distribution(db, contact, distribution_id)
    # Paths are stored relative to the storage root (older rows are absolute);
    # resolve handles both so a download works before and after the backfill.
    pdf_path = resolve_stored_path(s.pdf_path) if s.pdf_path else None
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(status_code=404, detail="PDF not available for this statement")
    filename = f"{w.canonical_name} - {d.period_code} ({d.catalog.value}).pdf"
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


def _breakdown_by(db: Session, statement_id: int, column):
    rows = (
        db.query(column, func.sum(StatementLine.earnings), func.count(StatementLine.id))
        .filter(StatementLine.statement_id == statement_id)
        .group_by(column)
        .all()
    )
    out = [
        {"key": key or "Unknown",
         "earnings": str(earnings if earnings is not None else 0),
         "lines": n}
        for key, earnings, n in rows
    ]
    out.sort(key=lambda r: float(r["earnings"]), reverse=True)
    return out


@me_router.get("/statements/{distribution_id}/breakdown")
async def get_my_statement_breakdown(
    distribution_id: int,
    contact: Contact = Depends(current_contact),
    db: Session = Depends(get_session),
):
    """Earnings split by income type, source, country, and channel (scoped),
    aggregated from the statement's line items for the portal charts."""
    d, s, w = _scoped_distribution(db, contact, distribution_id)
    return {
        "distribution_id": d.id,
        "period_code": d.period_code,
        "catalog": d.catalog.value,
        "by_income_type": _breakdown_by(db, s.id, StatementLine.income_type),
        "by_source": _breakdown_by(db, s.id, StatementLine.income_source),
        "by_country": _breakdown_by(db, s.id, StatementLine.country),
        "by_channel": _breakdown_by(db, s.id, StatementLine.channel),
    }


@me_router.get("/writers/{writer_id}/members")
async def list_writer_members(
    writer_id: int,
    contact: Contact = Depends(current_contact),
    db: Session = Depends(get_session),
):
    """Who can see this writer, plus pending shares — the Dropbox share panel."""
    _require_writer_access(db, contact, writer_id)
    members = (
        db.query(WriterContact, Contact)
        .join(Contact, WriterContact.contact_id == Contact.id)
        .filter(WriterContact.writer_id == writer_id)
        .all()
    )
    now = datetime.now()
    pending = [
        {"id": i.id, "email": i.email, "role": i.role.value,
         "expires_at": i.expires_at.isoformat()}
        for i in db.query(PortalInvite).filter(PortalInvite.writer_id == writer_id)
        if i.is_active(now)
    ]
    return {
        "members": [
            {"email": c.email, "display_name": c.display_name, "role": wc.role.value,
             "active": c.user_id is not None}
            for wc, c in members
        ],
        "pending_invites": pending,
    }


@me_router.put("/language")
async def set_my_language(
    body: dict = Body(...),
    contact: Contact = Depends(current_contact),
    db: Session = Depends(get_session),
):
    """Remember the writer's UI language on their contact record.

    Stored server-side rather than only in the browser so the choice follows
    them to a phone or a new machine — most of this roster reads Spanish, and
    re-picking it on every device is exactly the kind of small friction that
    makes a portal feel foreign.
    """
    lang = (body.get("language") or "").strip().lower()
    if lang not in SUPPORTED_LANGUAGES:
        raise HTTPException(
            status_code=400,
            detail=f"language must be one of: {', '.join(sorted(SUPPORTED_LANGUAGES))}",
        )
    contact.preferred_language = lang
    db.commit()
    return {"language": lang}


@me_router.post("/writers/{writer_id}/invites", status_code=201)
async def share_writer_access(
    writer_id: int,
    body: InviteRequest,
    background: BackgroundTasks,
    contact: Contact = Depends(current_contact),
    db: Session = Depends(get_session),
):
    """Share access to a writer with another email (like adding someone to a
    Dropbox folder). The inviter must already have access to that writer."""
    _require_writer_access(db, contact, writer_id)
    inviter_user_id = contact.user_id
    try:
        invite, raw = invite_svc.create_invite(
            db, writer_id, body.email, _role(body.role),
            invited_by_user_id=inviter_user_id, is_admin_invite=False,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    logger.info(f"contact {contact.id} shared writer {writer_id} with {body.email}")
    # Sent in the background: SMTP does a full TLS handshake and login per
    # message, and the invite is already usable without it.
    background.add_task(send_invite_email, invite.id, raw)
    # The link is still returned. Email can bounce, land in spam, or be sent to
    # a dead address, and the admin needs a way to hand it over regardless.
    return {
        "invite_id": invite.id,
        "email": invite.email,
        "role": invite.role.value,
        "expires_at": invite.expires_at.isoformat(),
        "token": raw,
        "invite_url": invite_url(raw),
        "delivery_status": invite.delivery_status,
    }


@me_router.post("/invites/{invite_id}/revoke")
async def revoke_my_invite(
    invite_id: int,
    contact: Contact = Depends(current_contact),
    db: Session = Depends(get_session),
):
    inv = db.get(PortalInvite, invite_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invite not found")
    _require_writer_access(db, contact, inv.writer_id)  # must own the folder
    invite_svc.revoke_invite(db, invite_id)
    return {"invite_id": invite_id, "status": "revoked"}


# --- admin bootstrap: /admin/writers/{id}/invites ----------------------------

@writer_invites_admin_router.post("/{writer_id}/invites", status_code=201)
async def admin_invite_to_writer(
    writer_id: int,
    body: InviteRequest,
    background: BackgroundTasks,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """The bootstrap invite: get the first contact per writer into the portal."""
    try:
        invite, raw = invite_svc.create_invite(
            db, writer_id, body.email, _role(body.role),
            invited_by_user_id=user.id, is_admin_invite=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    background.add_task(send_invite_email, invite.id, raw)
    return {
        "invite_id": invite.id,
        "writer_id": writer_id,
        "email": invite.email,
        "expires_at": invite.expires_at.isoformat(),
        "token": raw,
        "invite_url": invite_url(raw),
        "delivery_status": invite.delivery_status,
    }


@writer_invites_admin_router.post("/{writer_id}/invites/{invite_id}/resend", status_code=202)
async def admin_resend_invite(
    writer_id: int,
    invite_id: int,
    background: BackgroundTasks,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Send the invite email again — for the spam-folder case.

    The old token is single-use and hashed, so it cannot be read back out of
    the database to re-send. Issuing a fresh invite replaces it (create_invite
    revokes the prior one for the same writer+email), which also means a link
    that leaked earlier stops working.
    """
    inv = db.get(PortalInvite, invite_id)
    if inv is None or inv.writer_id != writer_id:
        raise HTTPException(status_code=404, detail="Invite not found")
    if inv.accepted_at is not None:
        raise HTTPException(status_code=409, detail="Invite was already accepted")

    try:
        invite, raw = invite_svc.create_invite(
            db, writer_id, inv.email, inv.role,
            invited_by_user_id=user.id, is_admin_invite=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    background.add_task(send_invite_email, invite.id, raw)
    return {
        "invite_id": invite.id,
        "replaces_invite_id": invite_id,
        "email": invite.email,
        "expires_at": invite.expires_at.isoformat(),
        "token": raw,
        "invite_url": invite_url(raw),
        "delivery_status": invite.delivery_status,
    }


@writer_invites_admin_router.get("/{writer_id}/invites")
async def admin_list_writer_invites(
    writer_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    now = datetime.now()
    invs = db.query(PortalInvite).filter(PortalInvite.writer_id == writer_id).all()
    return [
        {"id": i.id, "email": i.email, "role": i.role.value,
         "active": i.is_active(now),
         "accepted": i.accepted_at is not None,
         "expires_at": i.expires_at.isoformat(),
         "delivery_status": i.delivery_status,
         "delivery_error": i.delivery_error,
         "sent_at": i.sent_at.isoformat() if i.sent_at else None}
        for i in invs
    ]


# --- public accept: /portal --------------------------------------------------

@portal_router.get("/invites/{token}")
async def preview_invite(token: str, db: Session = Depends(get_session)):
    """Show what an invite grants before the recipient accepts (no auth)."""
    inv = invite_svc.get_active_invite(db, token)
    if inv is None:
        raise HTTPException(status_code=404, detail="Invite is invalid, revoked, or expired")
    writer = db.get(Writer, inv.writer_id)
    existing = db.query(User).filter(User.email == inv.email).first()
    has_login = existing is not None
    return {
        "email": inv.email,
        "writer_name": writer.canonical_name if writer else None,
        # A password is ALWAYS required now: a new one to create the login, or
        # the existing account's password to prove ownership.
        "needs_password": True,
        "has_login": has_login,
        # OAuth-only accounts have no password to type — they must sign in first.
        "requires_sign_in": bool(has_login and not existing.hashed_password),
    }


def _authenticated_email(request: Request) -> Optional[str]:
    """Email of the caller IF they present a valid session, else None. Never
    raises: the accept-invite flow is public, and being signed in is only one of
    the ways to prove you own an already-registered email."""
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    try:
        payload = jwt.decode(header[7:].strip(), SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        return None
    return payload.get("email")


@portal_router.post("/accept-invite")
async def accept_invite(
    request: Request,
    body: AcceptRequest = Body(...),
    db: Session = Depends(get_session),
):
    """Redeem an invite: create/link the login and return a session token.

    An invite link alone never authenticates an EXISTING account — see
    invite_svc.accept_invite. 401 means "prove it's you" (sign in or send the
    account's password); 400 means the link itself is bad.
    """
    try:
        contact, user, inv = invite_svc.accept_invite(
            db,
            body.token,
            body.password,
            bcrypt_context,
            authenticated_email=_authenticated_email(request),
        )
    except invite_svc.InviteAuthRequired as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    writer_ids = invite_svc.writer_ids_for_contact(db, contact)
    writers = db.query(Writer).filter(Writer.id.in_(writer_ids or [0])).all()
    return {
        "access_token": _mint_token(user),
        "token_type": "bearer",
        "email": contact.email,
        "writers": [_writer_card(db, w) for w in writers],
    }
